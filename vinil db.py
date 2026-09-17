#!/usr/bin/env python3
"""
vinil_db.py - base de precos e avaliacao de discos para a loja.

Coleta vendas concluidas no eBay (comparaveis), sugestao de preco e menor oferta no Discogs,
registra suas proprias vendas e calcula o valor de mercado e o preco sugerido de loja
por condicao (padrao Goldmine), com faixa, tendencia e nivel de confianca.

Comandos:
  add "Artista" "Titulo" --discogs 123456 [--ebay "busca"] [--custo 40] [--obrigatorias "racional,vol"] [--excluir "cd,7"]
  importar discos.csv           colunas: artista,titulo,discogs_release_id,ebay_busca,custo,obrigatorias,excluir
  coletar [--dias 365] [--id N] baixa comps do eBay e dados do Discogs para todos (ou um) discos
  venda ID --cond VG+ --preco 120 [--data 2026-09-15] [--dias-estoque 30]
  valor ID --cond VG+ [--custo 40]
  relatorio [--cond VG+]        imprime e grava relatorio.csv
  sync                          le discos.csv e minhas_vendas.csv (upsert) - usado pelo GitHub Actions
  pagina                        gera docs/index.html (consulta no celular) e RELATORIO.md
  listar

Token do Discogs: variavel de ambiente DISCOGS_TOKEN (secret no GitHub) ou config.json.
"""
import os
import argparse, csv, json, re, sqlite3, sys, time, statistics
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

BASE = Path(__file__).resolve().parent
DB = BASE / "vinil.db"
CFG_PATH = BASE / "config.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

DEFAULT_CFG = {
    "discogs_token": "",
    "moeda_loja": "BRL",
    "ebay_site": "https://www.ebay.com",
    "max_ebay_por_rodada": 30,        # discos consultados no eBay por rodada (rotacao pelos mais antigos)
    "posicionamento": 0.55,          # percentil dos comps usado como preco de loja
    "ajuste_loja": 1.0,              # multiplicador final (ex.: 1.1 = 10% acima do mercado)
    "margem_minima": 0.5,            # 50% sobre o custo, quando o custo e informado
    "meia_vida_dias": 180,           # peso de um comp cai pela metade a cada 180 dias
    "multiplicadores": {"M": 1.1, "NM": 1.0, "VG+": 0.6, "VG": 0.35, "G+": 0.2, "G": 0.12, "F": 0.05, "P": 0.03},
    "excluir_padrao": ["cd", "cassette", "cassete", "dvd", "blu-ray", "poster", "shirt", "camiseta", "book", "livro", "laserdisc"],
    "cambio_fallback": {"USD": 5.5, "EUR": 6.0, "GBP": 7.0, "CAD": 4.0, "AUD": 3.6, "BRL": 1.0},
}

# --------------------------------------------------------------------------- infra

def cfg():
    c = dict(DEFAULT_CFG)
    if CFG_PATH.exists():
        c.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
    if os.environ.get("DISCOGS_TOKEN"):
        c["discogs_token"] = os.environ["DISCOGS_TOKEN"]
    if not c["discogs_token"] or c["discogs_token"].startswith("COLE_"):
        c["discogs_token"] = ""
    return c


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS discos(id INTEGER PRIMARY KEY, artista TEXT, titulo TEXT,
        discogs_release_id INTEGER, ebay_busca TEXT, custo REAL, obrigatorias TEXT, excluir TEXT, criado TEXT);
    CREATE TABLE IF NOT EXISTS comps(id INTEGER PRIMARY KEY, disco_id INTEGER, fonte TEXT, item_id TEXT,
        data TEXT, preco REAL, moeda TEXT, preco_brl REAL, condicao TEXT, cond_inferida INTEGER,
        tipo TEXT, titulo_anuncio TEXT, UNIQUE(fonte, item_id));
    CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, disco_id INTEGER, data TEXT,
        menor_preco_brl REAL, a_venda INTEGER, sugestoes_brl TEXT);
    CREATE TABLE IF NOT EXISTS minhas_vendas(id INTEGER PRIMARY KEY, disco_id INTEGER, data TEXT,
        condicao TEXT, preco REAL, dias_estoque INTEGER);
    CREATE TABLE IF NOT EXISTS cambio(moeda TEXT, data TEXT, taxa REAL, PRIMARY KEY(moeda, data));
    CREATE TABLE IF NOT EXISTS coletas(id INTEGER PRIMARY KEY, disco_id INTEGER, fonte TEXT, data TEXT,
        status TEXT, novos INTEGER, obs TEXT);
    """)
    return con


def hoje():
    return date.today().isoformat()

# --------------------------------------------------------------------------- cambio (PTAX BCB)

def taxa(con, moeda, dia, c):
    """Cotacao de venda PTAX da moeda em BRL no dia (ou dia util anterior). Cache em tabela."""
    moeda = moeda.upper()
    if moeda == "BRL":
        return 1.0
    d = datetime.strptime(dia, "%Y-%m-%d").date()
    for _ in range(8):
        row = con.execute("SELECT taxa FROM cambio WHERE moeda=? AND data=?", (moeda, d.isoformat())).fetchone()
        if row:
            return row["taxa"]
        url = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
               f"CotacaoMoedaDia(moeda=@moeda,dataCotacao=@dataCotacao)?@moeda='{moeda}'"
               f"&@dataCotacao='{d:%m-%d-%Y}'&$top=100&$format=json")
        try:
            vals = requests.get(url, timeout=15).json().get("value", [])
        except Exception:
            vals = None
        if vals is None:
            break
        if vals:
            t = float(vals[-1]["cotacaoVenda"])
            con.execute("INSERT OR REPLACE INTO cambio VALUES(?,?,?)", (moeda, d.isoformat(), t))
            con.commit()
            return t
        d -= timedelta(days=1)
    return float(c["cambio_fallback"].get(moeda, 1.0))

# --------------------------------------------------------------------------- condicao

COND_PATTERNS = [
    ("NM",  r"\bNM\b|\bM-\b|NEAR\s*MINT|\bEX\+?\b|EXCELLENT"),
    ("M",   r"\bMINT\b|SEALED|LACRADO|\bSS\b|STILL\s*SEALED"),
    ("VG+", r"VG\s*\+\+?|VERY\s*GOOD\s*PLUS|OTIMO\s*ESTADO"),
    ("VG",  r"\bVG\b|VERY\s*GOOD|BOM\s*ESTADO"),
    ("G+",  r"\bG\s*\+|GOOD\s*PLUS"),
    ("G",   r"\bGOOD\b|\bG\b"),
    ("F",   r"\bFAIR\b|\bF\b"),
    ("P",   r"\bPOOR\b"),
]

def inferir_cond(texto):
    t = texto.upper()
    for cond, pat in COND_PATTERNS:
        if re.search(pat, t):
            return cond
    return None


def norm_cond(s):
    s = (s or "").strip().upper().replace("VG PLUS", "VG+")
    mapa = {"MINT": "M", "NEAR MINT": "NM", "M-": "NM", "VERY GOOD PLUS": "VG+", "VERY GOOD": "VG",
            "GOOD PLUS": "G+", "GOOD": "G", "FAIR": "F", "POOR": "P"}
    return mapa.get(s, s)

# --------------------------------------------------------------------------- eBay vendidos

PRECO_RE = re.compile(r"(C \$|AU \$|US \$|R\$|\$|£|€)\s?([\d.,]+)")
MOEDAS = {"$": "USD", "US $": "USD", "C $": "CAD", "AU $": "AUD", "£": "GBP", "€": "EUR", "R$": "BRL"}
DATA_RE = re.compile(r"(?:Sold|Vendido(?: em)?)\s+(\d{1,2}\s+[A-Za-z]{3}\.?\s+\d{4}|[A-Za-z]{3}\s+\d{1,2},\s+\d{4})")


def parse_preco(txt):
    m = PRECO_RE.search(txt.replace("\xa0", " "))
    if not m or " to " in txt or " a " in txt.lower().replace("r$", ""):
        return None, None
    num = m.group(2)
    num = num.replace(".", "").replace(",", ".") if MOEDAS.get(m.group(1)) == "BRL" else num.replace(",", "")
    try:
        return float(num), MOEDAS.get(m.group(1), "USD")
    except ValueError:
        return None, None


def parse_data(txt):
    m = DATA_RE.search(txt)
    if not m:
        return None
    s = m.group(1).replace(".", "")
    for f in ("%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def ebay_vendidos(html):
    """Extrai vendas concluidas de uma pagina de resultados do eBay (filtros Sold + Completed)."""
    if BeautifulSoup is None:
        sys.exit("instale: pip install beautifulsoup4")
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.s-item, div.s-item, li[data-view*='mi:']"):
        a = li.select_one("a[href*='/itm/']")
        if not a:
            continue
        mid = re.search(r"/itm/(\d{9,})", a.get("href", ""))
        titulo_el = li.select_one(".s-item__title, [class*='title']")
        titulo = titulo_el.get_text(" ", strip=True) if titulo_el else a.get_text(" ", strip=True)
        if not mid or not titulo or titulo.lower().startswith("shop on ebay"):
            continue
        preco_el = li.select_one(".s-item__price, [class*='price']")
        preco, moeda = parse_preco(preco_el.get_text(" ", strip=True) if preco_el else "")
        if not preco:
            continue
        data = parse_data(li.get_text(" ", strip=True))
        tipo = "leilao" if li.select_one(".s-item__bids, [class*='bids']") else "compre_ja"
        out.append({"item_id": mid.group(1), "titulo": titulo, "preco": preco, "moeda": moeda,
                    "data": data or hoje(), "tipo": tipo})
    return out


def ebay_get(url):
    """Baixa uma pagina do eBay. Com SCRAPERAPI_KEY (ou PROXY_TEMPLATE) a consulta passa por proxy
    residencial, necessario quando o script roda em servidor (GitHub Actions), pois o eBay bloqueia
    IPs de datacenter com HTTP 403."""
    key, tpl = os.environ.get("SCRAPERAPI_KEY"), os.environ.get("PROXY_TEMPLATE")
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if key:
        return requests.get("https://api.scraperapi.com/", params={"api_key": key, "url": url, "country_code": "us"},
                            timeout=90), "scraperapi"
    if tpl:
        return requests.get(tpl.replace("{url}", requests.utils.quote(url, safe="")), timeout=90), "proxy"
    return requests.get(url, headers=headers, timeout=30), "direto"


def coletar_ebay(con, disco, c, dias):
    q = disco["ebay_busca"] or f"{disco['artista']} {disco['titulo']} vinyl"
    url = (f"{c['ebay_site']}/sch/i.html?_nkw={requests.utils.quote(q)}&_sacat=176985"
           f"&LH_Sold=1&LH_Complete=1&_ipg=120&_sop=13")
    r, via = ebay_get(url)
    if r.status_code != 200:
        print(f"  [ebay] HTTP {r.status_code} via {via} para '{q}'")
        con.execute("INSERT INTO coletas(disco_id,fonte,data,status,novos,obs) VALUES(?,?,?,?,?,?)",
                    (disco["id"], "ebay", hoje(), f"HTTP {r.status_code}", 0, via)); con.commit()
        return 0
    obrig = [w.strip().lower() for w in (disco["obrigatorias"] or "").split(",") if w.strip()]
    if not obrig:
        obrig = [w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", disco["artista"])]
    excl = [w.strip().lower() for w in (disco["excluir"] or "").split(",") if w.strip()] + c["excluir_padrao"]
    limite = (date.today() - timedelta(days=dias)).isoformat()
    novos, brutos = 0, ebay_vendidos(r.text)
    for it in brutos:
        t = it["titulo"].lower()
        if any(w not in t for w in obrig) or any(re.search(rf"\b{re.escape(w)}\b", t) for w in excl):
            continue
        if it["data"] < limite:
            continue
        cond = inferir_cond(it["titulo"])
        brl = round(it["preco"] * taxa(con, it["moeda"], it["data"], c), 2)
        cur = con.execute(
            "INSERT OR IGNORE INTO comps(disco_id,fonte,item_id,data,preco,moeda,preco_brl,condicao,cond_inferida,tipo,titulo_anuncio)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (disco["id"], "ebay", it["item_id"], it["data"], it["preco"], it["moeda"], brl,
             cond or "VG+", 1 if cond is None else 0, it["tipo"], it["titulo"]))
        novos += cur.rowcount
    obs = f"{via}; {len(brutos)} anuncios lidos"
    if not brutos:
        titulo = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
        obs += "; pagina sem resultados: " + (titulo.group(1).strip()[:60] if titulo else "sem titulo")
    con.execute("INSERT INTO coletas(disco_id,fonte,data,status,novos,obs) VALUES(?,?,?,?,?,?)",
                (disco["id"], "ebay", hoje(), "ok", novos, obs))
    con.commit()
    return novos

# --------------------------------------------------------------------------- Discogs

def discogs_get(path, token, params=None):
    h = {"User-Agent": "VinilDB/1.0", "Authorization": f"Discogs token={token}"}
    for _ in range(3):
        r = requests.get(f"https://api.discogs.com{path}", headers=h, params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(10); continue
        if r.status_code in (403, 404):
            return None
        r.raise_for_status()
        return r.json()
    return None


def coletar_discogs(con, disco, c):
    tok, rid = c.get("discogs_token"), disco["discogs_release_id"]
    if not tok or not rid:
        return
    st = discogs_get(f"/marketplace/stats/{rid}", tok, {"curr_abbr": "BRL"}) or {}
    low = (st.get("lowest_price") or {}).get("value")
    sug = discogs_get(f"/marketplace/price_suggestions/{rid}", tok) or {}
    sug_brl = {}
    for k, v in sug.items():
        cond = norm_cond(re.sub(r"\s*\(.*\)", "", k).strip())
        if cond == "MINT (M)": cond = "M"
        sug_brl[cond] = round(float(v["value"]) * taxa(con, v.get("currency", "USD"), hoje(), c), 2)
    con.execute("INSERT INTO snapshots(disco_id,data,menor_preco_brl,a_venda,sugestoes_brl) VALUES(?,?,?,?,?)",
                (disco["id"], hoje(), low, st.get("num_for_sale"), json.dumps(sug_brl)))
    con.commit()

# --------------------------------------------------------------------------- avaliacao

def wpercentil(vals, pesos, p):
    pares = sorted(zip(vals, pesos))
    total = sum(pesos)
    acc = 0.0
    for v, w in pares:
        acc += w
        if acc >= p * total:
            return v
    return pares[-1][0]


def arredondar(v):
    passo = 5 if v < 200 else 10 if v < 1000 else 50
    return int(-(-v // passo) * passo)


def avaliar(con, disco, cond, c, custo=None):
    mult = c["multiplicadores"]
    cond = norm_cond(cond)
    if cond not in mult:
        sys.exit(f"condicao invalida: {cond}. Use {', '.join(mult)}")
    alvo = mult[cond]
    hl = c["meia_vida_dias"]
    limite = (date.today() - timedelta(days=365)).isoformat()
    corte90 = (date.today() - timedelta(days=90)).isoformat()

    comps = con.execute("SELECT * FROM comps WHERE disco_id=? AND data>=? ORDER BY data", (disco["id"], limite)).fetchall()
    vals, pesos, rec, ant = [], [], [], []
    for r in comps:
        idade = (date.today() - date.fromisoformat(r["data"])).days
        v = r["preco_brl"] * alvo / mult.get(r["condicao"], mult["VG+"])   # normaliza para a condicao alvo
        w = 0.5 ** (idade / hl) * (0.6 if r["cond_inferida"] else 1.0)
        vals.append(v); pesos.append(w)
        (rec if r["data"] >= corte90 else ant).append(v)

    res = {"disco": disco, "cond": cond, "n_comps": len(vals), "comps": comps}
    partes = []   # (valor, peso)
    if vals:
        res["mediana"] = wpercentil(vals, pesos, 0.5)
        res["p25"], res["p75"] = wpercentil(vals, pesos, 0.25), wpercentil(vals, pesos, 0.75)
        res["loja_base"] = wpercentil(vals, pesos, c["posicionamento"])
        partes.append((res["mediana"], min(1.0, len(vals) / 8)))
        if len(rec) >= 2 and len(ant) >= 2:
            res["tendencia"] = (statistics.median(rec) / statistics.median(ant) - 1) * 100

    snap = con.execute("SELECT * FROM snapshots WHERE disco_id=? ORDER BY data DESC, id DESC LIMIT 1", (disco["id"],)).fetchone()
    if snap:
        sug = json.loads(snap["sugestoes_brl"] or "{}")
        res["discogs_menor"], res["discogs_a_venda"] = snap["menor_preco_brl"], snap["a_venda"]
        if cond in sug:
            res["discogs_sug"] = sug[cond]
            partes.append((sug[cond], 0.6))
        elif "NM" in sug:
            res["discogs_sug"] = sug["NM"] * alvo / mult["NM"]
            partes.append((res["discogs_sug"], 0.5))

    minhas = con.execute("SELECT * FROM minhas_vendas WHERE disco_id=? AND data>=? ORDER BY data DESC", (disco["id"], limite)).fetchall()
    if minhas:
        mv = [m["preco"] * alvo / mult.get(norm_cond(m["condicao"]), mult["VG+"]) for m in minhas]
        res["minhas"] = minhas
        res["minhas_mediana"] = statistics.median(mv)
        partes.append((res["minhas_mediana"], 1.2 * min(1.0, len(mv) / 3)))

    if not partes:
        res["valor"] = None
        return res
    res["valor"] = sum(v * w for v, w in partes) / sum(w for _, w in partes)
    evid = len(vals) + 3 * len(minhas) + (2 if "discogs_sug" in res else 0)
    res["confianca"] = "alta" if evid >= 10 else "media" if evid >= 4 else "baixa"

    base = res.get("loja_base") if len(vals) >= 3 else res["valor"]
    preco = base * c["ajuste_loja"]
    custo = custo if custo is not None else disco["custo"]
    if custo:
        piso = custo * (1 + c["margem_minima"])
        if preco < piso:
            res["aviso_margem"] = f"mercado (R$ {preco:.0f}) abaixo do piso de margem (R$ {piso:.0f}); avalie se vale estocar"
    res["preco_loja"] = arredondar(preco)
    return res


def fmt(v):
    return "-" if v is None else f"R$ {v:,.0f}".replace(",", ".")


def imprimir(res):
    d = res["disco"]
    print(f"\n{d['artista']} - {d['titulo']}   [id {d['id']}]   condicao alvo: {res['cond']}")
    if res["n_comps"]:
        linha = (f"  Comps eBay 12 meses: {res['n_comps']} vendas | mediana {fmt(res['mediana'])}"
                 f" | faixa P25-P75 {fmt(res['p25'])} a {fmt(res['p75'])}")
        if "tendencia" in res:
            linha += f" | tendencia 90d {res['tendencia']:+.0f}%"
        print(linha)
        for r in res["comps"][-5:]:
            print(f"     {r['data']}  {fmt(r['preco_brl'])}  {r['condicao']}{'?' if r['cond_inferida'] else ''}  {r['tipo']}  {r['titulo_anuncio'][:60]}")
    else:
        print("  Comps eBay 12 meses: nenhum (rode 'coletar' ou ajuste ebay_busca/obrigatorias)")
    if "discogs_menor" in res:
        print(f"  Discogs: sugestao {res['cond']} {fmt(res.get('discogs_sug'))} | menor a venda {fmt(res['discogs_menor'])} ({res['discogs_a_venda'] or 0} ofertas)")
    if res.get("minhas"):
        print(f"  Suas vendas 12 meses: {len(res['minhas'])} | mediana normalizada {fmt(res['minhas_mediana'])}")
        for m in res["minhas"][:3]:
            print(f"     {m['data']}  {fmt(m['preco'])}  {m['condicao']}  {m['dias_estoque'] or '-'} dias em estoque")
    if res["valor"] is None:
        print("  => Sem dados suficientes para avaliar.")
        return
    print(f"  => Valor de mercado estimado ({res['cond']}): {fmt(res['valor'])}   confianca: {res['confianca']}")
    print(f"  => PRECO SUGERIDO LOJA: {fmt(res['preco_loja'])}")
    if "aviso_margem" in res:
        print(f"  ! {res['aviso_margem']}")

# --------------------------------------------------------------------------- comandos

def cmd_add(con, a):
    con.execute("INSERT INTO discos(artista,titulo,discogs_release_id,ebay_busca,custo,obrigatorias,excluir,criado) VALUES(?,?,?,?,?,?,?,?)",
                (a.artista, a.titulo, a.discogs, a.ebay, a.custo, a.obrigatorias, a.excluir, hoje()))
    con.commit()
    print(f"disco adicionado, id {con.execute('SELECT last_insert_rowid()').fetchone()[0]}")


def cmd_importar(con, a):
    n = 0
    with open(a.arquivo, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            con.execute("INSERT INTO discos(artista,titulo,discogs_release_id,ebay_busca,custo,obrigatorias,excluir,criado) VALUES(?,?,?,?,?,?,?,?)",
                        (r["artista"], r["titulo"], r.get("discogs_release_id") or None, r.get("ebay_busca") or None,
                         float(r["custo"]) if r.get("custo") else None, r.get("obrigatorias") or None, r.get("excluir") or None, hoje()))
            n += 1
    con.commit()
    print(f"{n} discos importados")


def discos(con, so_id=None):
    if so_id:
        return con.execute("SELECT * FROM discos WHERE id=?", (so_id,)).fetchall()
    return con.execute("SELECT * FROM discos ORDER BY artista, titulo").fetchall()


def cmd_coletar(con, a, c):
    todos = discos(con, a.id)
    fila = con.execute("""SELECT d.id FROM discos d LEFT JOIN
        (SELECT disco_id, MAX(data) ult FROM coletas WHERE fonte='ebay' AND status='ok' GROUP BY disco_id) u
        ON u.disco_id=d.id ORDER BY u.ult IS NOT NULL, u.ult, d.id""").fetchall()
    limite = len(todos) if a.id or a.todos else int(c.get("max_ebay_por_rodada", 30))
    com_ebay = {r["id"] for r in fila[:limite]}
    for d in todos:
        print(f"{d['artista']} - {d['titulo']}")
        if d["id"] in com_ebay:
            try:
                print(f"  eBay: {coletar_ebay(con, d, c, a.dias)} vendas novas")
            except Exception as e:
                print(f"  [ebay] erro: {e}")
                con.execute("INSERT INTO coletas(disco_id,fonte,data,status,novos,obs) VALUES(?,?,?,?,?,?)",
                            (d["id"], "ebay", hoje(), "erro", 0, str(e)[:120])); con.commit()
        try:
            coletar_discogs(con, d, c); print("  Discogs: ok" if c.get("discogs_token") else "  Discogs: sem token, pulado")
        except Exception as e:
            print(f"  [discogs] erro: {e}")
        time.sleep(1.5)


def cmd_venda(con, a):
    con.execute("INSERT INTO minhas_vendas(disco_id,data,condicao,preco,dias_estoque) VALUES(?,?,?,?,?)",
                (a.id, a.data or hoje(), norm_cond(a.cond), a.preco, a.dias_estoque))
    con.commit(); print("venda registrada")


def cmd_valor(con, a, c):
    for d in discos(con, a.id):
        imprimir(avaliar(con, d, a.cond, c, a.custo))


def cmd_relatorio(con, a, c):
    linhas = []
    for d in discos(con):
        r = avaliar(con, d, a.cond, c)
        linhas.append({"id": d["id"], "artista": d["artista"], "titulo": d["titulo"], "condicao": r["cond"],
                       "n_comps": r["n_comps"], "mediana_brl": round(r.get("mediana") or 0, 2) or "",
                       "discogs_sug_brl": round(r.get("discogs_sug") or 0, 2) or "",
                       "valor_mercado_brl": round(r["valor"], 2) if r["valor"] else "",
                       "preco_loja_brl": r.get("preco_loja", ""), "confianca": r.get("confianca", ""),
                       "tendencia_pct": round(r["tendencia"], 1) if "tendencia" in r else "",
                       "aviso": r.get("aviso_margem", "")})
        print(f"{d['id']:>4}  {d['artista'][:22]:<22} {d['titulo'][:30]:<30} {r['cond']:<4} comps {r['n_comps']:>3}  "
              f"mercado {fmt(r['valor']):>10}  loja {fmt(r.get('preco_loja')):>10}  {r.get('confianca','')}")
    with open(BASE / "relatorio.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()) if linhas else ["id"])
        w.writeheader(); w.writerows(linhas)
    print(f"\nrelatorio.csv gravado ({len(linhas)} discos)")


def _chave(a, t):
    return f"{a.strip().lower()}|{t.strip().lower()}"


def cmd_sync(con, a):
    """Upsert de discos.csv e importacao de minhas_vendas.csv (idempotente)."""
    disc_csv, vend_csv = BASE / "discos.csv", BASE / "minhas_vendas.csv"
    existentes = {_chave(d["artista"], d["titulo"]): d["id"] for d in discos(con)}
    n_new = n_upd = 0
    if disc_csv.exists():
        with disc_csv.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if not (r.get("artista") and r.get("titulo")):
                    continue
                vals = (int(r["discogs_release_id"]) if r.get("discogs_release_id", "").strip() else None,
                        r.get("ebay_busca") or None, float(r["custo"]) if r.get("custo", "").strip() else None,
                        r.get("obrigatorias") or None, r.get("excluir") or None)
                k = _chave(r["artista"], r["titulo"])
                if k in existentes:
                    con.execute("UPDATE discos SET discogs_release_id=?,ebay_busca=?,custo=?,obrigatorias=?,excluir=? WHERE id=?", vals + (existentes[k],))
                    n_upd += 1
                else:
                    con.execute("INSERT INTO discos(artista,titulo,discogs_release_id,ebay_busca,custo,obrigatorias,excluir,criado) VALUES(?,?,?,?,?,?,?,?)",
                                (r["artista"].strip(), r["titulo"].strip()) + vals + (hoje(),))
                    existentes[k] = con.execute("SELECT last_insert_rowid()").fetchone()[0]
                    n_new += 1
    n_v = 0
    if vend_csv.exists():
        with vend_csv.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                k = _chave(r.get("artista", ""), r.get("titulo", ""))
                if k not in existentes or not r.get("preco", "").strip():
                    continue
                cond, preco, data = norm_cond(r.get("condicao")), float(r["preco"]), r.get("data") or hoje()
                dias = int(r["dias_estoque"]) if r.get("dias_estoque", "").strip() else None
                if con.execute("SELECT 1 FROM minhas_vendas WHERE disco_id=? AND data=? AND condicao=? AND preco=?",
                               (existentes[k], data, cond, preco)).fetchone():
                    continue
                con.execute("INSERT INTO minhas_vendas(disco_id,data,condicao,preco,dias_estoque) VALUES(?,?,?,?,?)",
                            (existentes[k], data, cond, preco, dias))
                n_v += 1
    con.commit()
    print(f"sync: {n_new} discos novos, {n_upd} atualizados, {n_v} vendas novas")


CONDS_PAGINA = ["VG", "VG+", "NM"]

HTML_TOPO = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Preços da loja</title>
<style>
:root{--ink:#000;--paper:#fff;--mute:#5c5c5c;--line:#d9d9d9;--tag:#1d3fbf;--tagink:#fff;--soft:#f2f2f2}
@media(prefers-color-scheme:dark){:root{--ink:#f3f3f3;--paper:#161616;--mute:#a3a3a3;--line:#333;--tag:#8fa4ff;--tagink:#0b1230;--soft:#222}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.45 -apple-system,"Helvetica Neue",Helvetica,Arial,sans-serif}
header{position:sticky;top:0;background:var(--paper);padding:14px 16px 10px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0 0 8px;font-weight:600}h1 small{color:var(--mute);font-weight:400;font-size:13px;margin-left:8px}
input{width:100%;font:inherit;font-size:17px;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--soft);color:var(--ink)}
input:focus{outline:2px solid var(--tag);outline-offset:1px}
main{padding:0 16px 40px;max-width:720px;margin:0 auto}
.n{color:var(--mute);font-size:13px;padding:10px 0 4px}
article{border-bottom:1px solid var(--line);padding:14px 0}
.t{font-weight:600}.a{color:var(--mute)}
.p{display:flex;gap:8px;margin-top:10px}.p div{flex:1;border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.p b{display:block;font-size:20px;font-weight:600;color:var(--tag)}.p span{color:var(--mute);font-size:12px}.p em{font-style:normal;font-size:12px;color:var(--mute)}
.m{margin-top:8px;font-size:13px;color:var(--mute)}
details{margin-top:6px;font-size:13px}summary{color:var(--mute);cursor:pointer}details ul{margin:6px 0 0;padding-left:18px}
.hide{display:none}.aviso{color:#b3261e}.vazio{text-align:center;color:var(--mute);padding:40px 0}
</style></head><body><header><h1>Preços da loja<small>atualizado em {DATA}</small></h1>
<input id="q" type="search" placeholder="Buscar artista ou disco" autocomplete="off"></header><main><div class="n" id="n"></div>
"""

HTML_FIM = """<p class="vazio hide" id="vazio">Nenhum disco encontrado.</p></main>
<script>
const q=document.getElementById('q'),arts=[...document.querySelectorAll('article')],n=document.getElementById('n'),vz=document.getElementById('vazio');
const norm=s=>s.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
function filtra(){const v=norm(q.value.trim());let k=0;arts.forEach(a=>{const ok=!v||norm(a.dataset.k).includes(v);a.classList.toggle('hide',!ok);if(ok)k++});
n.textContent=k+(k===1?' disco':' discos');vz.classList.toggle('hide',k>0)}
q.addEventListener('input',filtra);filtra();
</script></body></html>"""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cmd_pagina(con, a, c):
    linhas_html, linhas_md = [], []
    for d in discos(con):
        res = {cond: avaliar(con, d, cond, c) for cond in CONDS_PAGINA}
        ref = res["VG+"]
        chips = []
        for cond in CONDS_PAGINA:
            r = res[cond]
            chips.append(f"<div><span>{cond}</span><b>{fmt(r.get('preco_loja'))}</b><em>mercado {fmt(r.get('valor'))}</em></div>")
        meta = [f"{ref['n_comps']} vendas eBay em 12 meses"]
        if "tendencia" in ref: meta.append(f"tendência 90d {ref['tendencia']:+.0f}%")
        if "discogs_menor" in ref: meta.append(f"Discogs menor {fmt(ref['discogs_menor'])} ({ref['discogs_a_venda'] or 0} ofertas)")
        if ref.get("minhas"): meta.append(f"{len(ref['minhas'])} venda(s) sua(s)")
        meta.append(f"confiança {ref.get('confianca', 'sem dados')}")
        aviso = f"<div class='m aviso'>{esc(ref['aviso_margem'])}</div>" if ref.get("aviso_margem") else ""
        comps = "".join(f"<li>{r['data']} {fmt(r['preco_brl'])} {r['condicao']}{'?' if r['cond_inferida'] else ''} {r['tipo'].replace('_', ' ')}: {esc(r['titulo_anuncio'][:70])}</li>"
                        for r in ref["comps"][-6:][::-1])
        det = f"<details><summary>Últimas vendas</summary><ul>{comps}</ul></details>" if comps else ""
        linhas_html.append(f"<article data-k=\"{esc(d['artista'])} {esc(d['titulo'])}\"><div class='t'>{esc(d['titulo'])}</div><div class='a'>{esc(d['artista'])}</div>"
                           f"<div class='p'>{''.join(chips)}</div><div class='m'>{' · '.join(meta)}</div>{aviso}{det}</article>")
        linhas_md.append(f"| {d['artista']} | {d['titulo']} | " + " | ".join(fmt(res[x].get('preco_loja')) for x in CONDS_PAGINA)
                         + f" | {fmt(ref.get('valor'))} | {ref['n_comps']} | {ref.get('confianca', '-')} |")
    (BASE / "docs").mkdir(exist_ok=True)
    (BASE / "docs" / "index.html").write_text(HTML_TOPO.replace("{DATA}", datetime.now().strftime("%d/%m/%Y")) + "\n".join(linhas_html) + HTML_FIM, encoding="utf-8")
    md = [f"# Preços da loja", f"Atualizado em {datetime.now():%d/%m/%Y}. Preço de loja por condição; mercado = valor estimado VG+.", "",
          "| Artista | Disco | VG | VG+ | NM | Mercado VG+ | Vendas eBay | Confiança |", "|---|---|---|---|---|---|---|---|", *linhas_md]
    ult = con.execute("SELECT MAX(data) FROM coletas").fetchone()[0]
    if ult:
        rows = con.execute("SELECT c.status, c.novos, c.obs, d.artista, d.titulo FROM coletas c JOIN discos d ON d.id=c.disco_id "
                           "WHERE c.data=? AND c.fonte='ebay' ORDER BY c.id", (ult,)).fetchall()
        ok = [r for r in rows if r["status"] == "ok"]
        md += ["", f"## Última coleta no eBay ({ult})",
               f"{len(rows)} discos consultados, {len(ok)} com resposta, {sum(r['novos'] for r in ok)} vendas novas, "
               f"{len(rows) - len(ok)} com erro ou bloqueio.", ""]
        for r in rows:
            md.append(f"- {r['artista']}, {r['titulo']}: {r['status']}, {r['novos']} novas ({r['obs']})")
    (BASE / "RELATORIO.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"docs/index.html e RELATORIO.md gerados ({len(linhas_html)} discos)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("add"); p.add_argument("artista"); p.add_argument("titulo")
    p.add_argument("--discogs", type=int); p.add_argument("--ebay"); p.add_argument("--custo", type=float)
    p.add_argument("--obrigatorias"); p.add_argument("--excluir")
    p = sp.add_parser("importar"); p.add_argument("arquivo")
    p = sp.add_parser("coletar"); p.add_argument("--dias", type=int, default=365); p.add_argument("--id", type=int)
    p.add_argument("--todos", action="store_true", help="ignora max_ebay_por_rodada e consulta o eBay para todos")
    p = sp.add_parser("venda"); p.add_argument("id", type=int); p.add_argument("--cond", required=True)
    p.add_argument("--preco", type=float, required=True); p.add_argument("--data"); p.add_argument("--dias-estoque", type=int)
    p = sp.add_parser("valor"); p.add_argument("id", type=int); p.add_argument("--cond", default="VG+"); p.add_argument("--custo", type=float)
    p = sp.add_parser("relatorio"); p.add_argument("--cond", default="VG+")
    sp.add_parser("listar"); sp.add_parser("sync"); sp.add_parser("pagina")
    a = ap.parse_args()
    con, c = db(), cfg()
    if a.cmd == "add": cmd_add(con, a)
    elif a.cmd == "importar": cmd_importar(con, a)
    elif a.cmd == "coletar": cmd_coletar(con, a, c)
    elif a.cmd == "venda": cmd_venda(con, a)
    elif a.cmd == "valor": cmd_valor(con, a, c)
    elif a.cmd == "relatorio": cmd_relatorio(con, a, c)
    elif a.cmd == "sync": cmd_sync(con, a)
    elif a.cmd == "pagina": cmd_pagina(con, a, c)
    elif a.cmd == "listar":
        for d in discos(con):
            print(f"{d['id']:>4}  {d['artista']} - {d['titulo']}  (discogs {d['discogs_release_id'] or '-'})")


if __name__ == "__main__":
    main()
