# Preços da loja de discos, rodando no GitHub (só com o iPhone)

O GitHub executa o `vinil_db.py` toda segunda-feira, coleta vendas concluídas no eBay e dados do Discogs, recalcula o valor e o preço de loja de cada disco e grava o resultado no próprio repositório. Você consulta os preços pelo `RELATORIO.md` (abre no app do GitHub) ou pela página de busca (GitHub Pages).

## Arquivos
- `vinil_db.py`, `config.json`, `requirements.txt`: o sistema.
- `discos.csv`: seu catálogo. Uma linha por disco.
- `minhas_vendas.csv`: vendas que você fez na loja (alimentam o cálculo).
- `coletar.yml`: agendamento. Precisa ficar em `.github/workflows/coletar.yml` (passo 4).

## Passo a passo no iPhone (Safari)
1. Salve os arquivos deste chat no app Arquivos: toque em cada arquivo, Compartilhar, "Salvar em Arquivos".
2. Em github.com crie a conta (se não tiver) e um repositório novo, por exemplo `loja-discos`. Privado funciona; para usar a página de busca no GitHub Pages, deixe público.
3. No repositório: "Add file" > "Upload files", escolha `vinil_db.py`, `config.json`, `requirements.txt`, `discos.csv`, `minhas_vendas.csv` e `README.md` no app Arquivos, e "Commit changes".
4. "Add file" > "Create new file". No nome, digite exatamente `.github/workflows/coletar.yml`. Abra `coletar.yml` no app Arquivos, selecione tudo, copie e cole no editor. "Commit changes".
5. Token do Discogs: gere em discogs.com/settings/developers. No GitHub, Settings > Secrets and variables > Actions > "New repository secret", nome `DISCOGS_TOKEN`, cole o token. Se o menu Settings não aparecer no celular, toque em "AA" na barra do Safari > "Solicitar site para computador".
6. Teste: aba Actions > "coletar precos" > "Run workflow". Em 1 a 2 minutos aparecem `RELATORIO.md`, `relatorio.csv` e `docs/index.html` no repositório.
7. Opcional, página de busca: Settings > Pages > Source "Deploy from a branch", branch `main`, pasta `/docs`, Save. O endereço fica `https://SEU-USUARIO.github.io/loja-discos/`. Adicione à tela de início do iPhone.

## Uso no dia a dia
- Disco novo: abra `discos.csv` no GitHub, toque no lápis, acrescente a linha e faça commit. Colunas: `artista,titulo,discogs_release_id,ebay_busca,custo,obrigatorias,excluir`. O `discogs_release_id` é o número da URL da prensagem no Discogs.
- Venda feita: mesma coisa em `minhas_vendas.csv`: `artista,titulo,data,condicao,preco,dias_estoque`, ex.: `Tim Maia,Racional Vol. 1,2026-09-15,VG+,1150,20`. Artista e título precisam ser iguais aos do `discos.csv`.
- A próxima rodada (segunda, 8h) incorpora tudo. Para não esperar, use "Run workflow".
- Ajustes de cálculo em `config.json`: `posicionamento` (0,55 = um pouco acima da mediana), `ajuste_loja`, `margem_minima`.

## eBay pelo proxy (obrigatório no GitHub)
O eBay bloqueia os servidores do GitHub (HTTP 403). A consulta passa por um proxy residencial: crie uma conta gratuita no ScraperAPI (scraperapi.com), copie a API key e cadastre como secret `SCRAPERAPI_KEY` no GitHub. O plano grátis dá 1.000 créditos por mês, por isso o script consulta o eBay para no máximo `max_ebay_por_rodada` discos por dia (os que estão há mais tempo sem atualização), enquanto o Discogs é atualizado para todos. Com 30 por dia, um catálogo de 200 discos é renovado a cada semana. Acompanhe o consumo no painel do ScraperAPI e ajuste o número no `config.json`. O `RELATORIO.md` traz, no final, o diagnóstico da última coleta (quantos consultados, bloqueios, vendas novas).

## Se algo falhar
- Actions em vermelho: abra a rodada e leia a última linha. `HTTP 403` ou `503` do eBay significa bloqueio temporário do IP do GitHub; rode de novo mais tarde. Se persistir, o plano B é rodar o script no celular (a-Shell ou Pythonista).
- Sem `DISCOGS_TOKEN` o script pula o Discogs e segue só com o eBay.
- Se o agendamento parar, o GitHub pausa rotinas em repositórios sem atividade por 60 dias; qualquer commit reativa.
