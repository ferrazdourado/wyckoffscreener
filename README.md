# Wyckoff Screener

Análise técnica Wyckoff semanal sobre watchlist B3 + NYSE/Nasdaq.
Implementação da [spec](Claude.md). **Fases 1, 2 e 3 concluídas** — R1 a R12 —
mais os três itens **P2**: fonte de dados plugável, exportação para PDF e
dashboard local.

## Instalação

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Segredos (token do bot, senha de SMTP, token da brapi) vão num `.env` na raiz,
que o `wyckoff` carrega sozinho a cada comando — `cp .env.example .env` e
preencha. O `config.yaml` guarda só o **nome** da variável, nunca o valor, e
variável já exportada na sessão vence o arquivo. `wyckoff validate` lista o que
foi carregado, pelo nome.

Extras opcionais — a rotina semanal não depende de nenhum deles:

```bash
pip install -e ".[dashboard]"   # wyckoff dashboard (Streamlit)
pip install -e ".[pdf]"         # motor de PDF para máquina sem Chrome/Chromium
```

## Uso

```bash
wyckoff report              # ROTINA DE SEXTA: coleta, analisa e gera .md + .html
wyckoff report --notify     # o mesmo, e manda o resumo pelo canal configurado

wyckoff screen b3_liquidas  # varre 78 papéis e ranqueia quem está em Fase C/D
wyckoff backtest            # mede o que vem depois de cada regra (calibragem)
wyckoff notify --dry-run    # mostra a mensagem sem enviar
wyckoff universe --check    # confere quais tickers do universo ainda existem

wyckoff validate            # confere config.yaml e watchlist.yaml
wyckoff fetch               # coleta, atualiza o cache e calcula as métricas
wyckoff metrics             # recalcula do cache, sem rede
wyckoff analyze [TICKER]    # fase, range, contagem de causa e eventos
wyckoff show PETR4.SA       # últimas 12 semanas com todas as métricas
wyckoff verify PETR4.SA     # abre a conta de cada métrica p/ conferência manual
wyckoff add WEGE3.SA --invalidation 45.50

wyckoff report --pdf        # gera também reports/AAAA-SS.pdf          (P2)
wyckoff pdf                 # converte para PDF um relatório já gerado (P2)
wyckoff dashboard           # dashboard local sobre o mesmo SQLite     (P2)
wyckoff sources PETR4.SA    # põe duas fontes lado a lado no mesmo papel (P2)
```

Quase todo comando aceita `--offline` (usa só o cache, sem rede).

Saídas: `reports/AAAA-SS.md`, `reports/AAAA-SS.html` (autocontido, com os
gráficos embutidos), `reports/AAAA-SS.pdf` (com `--pdf`) e
`reports/AAAA-SS/charts/*.png`; `exports/metrics_AAAA-SS.csv` e
`exports/latest_AAAA-SS.csv`.

Referência de 07-08/09/2026, offline: relatório **3,0 s** para 12 papéis ·
screener **24 s** para 78 · backtest causal **10 s** para 12 × 120 semanas ·
PDF **2,9 s** (17 páginas, 1,4 MB).

## Decisões que valem registro

### Coleta (Fase 1)

**Candle semanal é agregado do diário, não baixado pronto.** O `interval="1wk"`
do yfinance tem dois defeitos que atrapalham exatamente esta metodologia:

1. *Volume errado na barra mais recente.* Em 07/09/2026, PETR4 semana de 31/08
   vinha com 281.701.900 contra 252.576.000 somando os 5 pregões — 11,5% a
   mais, justamente na semana que interessa. Como `volume_ratio` é a métrica
   central do Wyckoff, isso desloca sinal (BAC passou de 1,09 para 0,94 depois
   da correção, cruzando a média).
2. *Bases de ajuste misturadas na semana do provento.* BAC, semana com data-ex
   em 04/09: abertura cum-dividendo e fechamento ex-dividendo no mesmo candle,
   distorcendo o spread em 0,5%. Num split seria catastrófico.

Agregando de candles diários ajustados, o volume é a soma real dos pregões e
todos os preços ficam na mesma base.

**Cada barra guarda `trading_days`.** Semana curta por feriado é informação, não
erro — B3 e US têm calendários distintos (18 das 120 semanas do EMBJ3 têm menos
de 5 pregões). **Semanas com data-ex ficam marcadas** (`ex_dividend`/`ex_split`).

### Análise (Fase 2)

**A análise roda só sobre semanas fechadas.** O candle em formação tem volume
parcial e mínima provisória; deixá-lo entrar produziria springs que somem na
sexta seguinte. Ele é reportado à parte, como aviso.

**Dispersão nos fechamentos, limites nas extremas** (`ranges.py`). A
lateralização é medida por fechamento — é ele que diz onde o papel parou; os
limites saem das máximas e mínimas, que é o que o preço testa. Assim um spring,
que perfura com o pavio e fecha de volta dentro, não quebra a detecção do range.

**Suporte e resistência "anteriores".** Se o suporte incluísse a mínima do
próprio spring, nada perfuraria nada. `levels_before` devolve o nível formado
pelas barras estritamente anteriores à candidata — o mesmo que estava desenhado
no gráfico na semana anterior. Sem isso, todo evento de perfuração é lookahead.

**Todo sinal carrega a conta aberta.** Cada evento traz a lista de `Check` que o
disparou — valor medido, operador e limite exigido. O relatório não diz "spring
detectado", diz `mínima 18.78 (< 19.08 exigido)` / `volume/média 0.74×
(< 1.20× exigido)`. Conferido à mão contra o SQLite: o SOS de BBAS3 em 31/08/2026
reproduz nos cinco números.

**Comparação com NaN é falsa, e isso é proposital.** Nas primeiras 20 semanas,
enquanto as médias não têm janela cheia, nenhuma regra dispara. Sem métrica não
há sinal — tem teste garantindo.

**Sinal que falha expira.** Uma leitura de Fase C ou D vive do nível que o
evento deixou. Se o preço volta para o lado errado desse nível e fica lá, a fase
recua para B. Sem essa regra, o SOS da VALE3 em dezembro mantinha o papel em
"Fase D — demanda no controle" durante os oito meses seguintes de queda.

**Evento de viés contrário sempre vale.** Dentro do mesmo viés a fase só avança
(um spring depois de um SOS não devolve D para C). Mas evento do lado oposto é
*change of character* e reabre a leitura — foi o caso do BBAS3 em 31/08/2026,
em markdown e imprimindo um SOS acima da resistência do range.

### Fase 3

**Telegram e e-mail, os dois.** A spec §8 deixou a escolha em aberto. Em vez de
travar numa pergunta, os dois canais existem atrás da mesma interface
`Notifier` — a decisão virou `notify.backend` no config.

**Segredo não entra em arquivo versionado.** O config guarda o *nome* da
variável de ambiente; o código lê o valor de lá. Token de bot e senha de SMTP
nunca tocam o `config.yaml`. Faltando a variável, o erro diz qual exportar em
vez de devolver um 401 da API.

**Nada é enviado sem pedido.** `notify.enabled` nasce `false`, e mesmo ligado só
`wyckoff notify` ou `wyckoff report --notify` disparam mensagem. Gerar relatório
nunca notifica sozinho — tem teste garantindo.

**P&F precisou do candle diário, e isso mudou o `DataProvider`.** A primeira
versão da contagem de causa (R10) usava o candle semanal e produzia alvos
*abaixo* do preço atual em quase todo papel: uma semana inteira vira no máximo
uma coluna por direção, então os ranges rendiam 1–5 colunas e a contagem saía
vazia de causa. O provider já baixava o diário para agregar o semanal e o jogava
fora. Agora o primitivo do `DataProvider` é `daily_bars` (o semanal vem da
agregação, de graça) e o diário fica no SQLite. Com ele, 2–11 colunas por range
e alvos com sentido.

**A projeção anda em índices de box, não em reais.** Numa grade geométrica o box
vale menos em reais quanto mais baixo o preço. Subtrair `N × largura_do_box`
medida na linha de contagem projetava −66% para a MDLZ; percorrer N boxes na
grade dá −49%. Mesma ideia, aritmética certa.

**Contar na extremidade do range subestima.** A linha de contagem default é a
mais larga da congestão, não o suporte: no suporte só as colunas que fizeram as
mínimas cruzam, e o alvo sai aquém do preço. Quando ainda assim sai aquém, o
relatório diz "causa insuficiente" em vez de apresentar passado como projeção.

**O backtest é causal por default.** A detecção de range olha a série inteira:
se a barra `i` está dentro de um range depende de barras *posteriores* a ela. Um
backtest rodado sobre a detecção final saberia em `i` algo que só o futuro
conta. No modo causal a leitura é refeita semana a semana com o que havia até
ali — e o retorno é medido da semana em que o sinal ficou *visível*, não da
semana do candle (spring que confirma dois candles depois só é acionável dois
candles depois). `--fast` existe para varredura exploratória e avisa do viés.

**A linha de base não é opcional.** Em mercado de alta tudo rende à frente. A
tabela sempre traz `_qualquer_semana` para comparação — sem ela, "spring rende
+3,3% em 13 semanas" não diz nada quando qualquer semana rendeu +5,6%.

### P2 — fonte plugável, PDF e dashboard

**A brapi.dev entrou como alternativa, não como substituta.** O `DataProvider`
já era a interface; o que faltava era outra implementação e uma forma de
escolher. `data.source` decide: `default` para todo mundo, `by_market` para
trocar a fonte de um mercado (a brapi só tem B3), `fallback` para a cadeia
tentada quando a principal falha. O fallback dispara em **erro de coleta**, não
em suspeita de buraco — trocar de fonte no meio de uma série sem avisar
esconderia justamente o problema que a §8 quer investigar.

**O token da brapi é lido de variável de ambiente**, como o do Telegram: o
`config.yaml` guarda só o nome da variável. Sem token a fonte atende poucos
papéis (em 08/09/2026: PETR4, VALE3, ITUB4, MGLU3); o resto responde 401, e a
mensagem diz como conseguir um em vez de deixar o 401 cru chegar ao usuário.

**Volume da brapi não vem ajustado por desdobramento — e isso quebraria metade
das regras.** Medido em MGLU3: no grupamento de 24/05/2024 a série mantém o
volume na escala do papel velho, e a razão contra o yfinance é exatamente
`0,1 × 1,05`, o produto dos dois grupamentos posteriores. Sem corrigir, o
`volume_ratio` lê **0,21 onde o yfinance lê 1,92** nas 15 semanas seguintes:
nenhum climax (≥ 2×) e nenhum SOS (≥ 1,5×) seria detectado, em silêncio. O
provider reaplica os fatores; o histórico e os proventos vêm na mesma
requisição, então a correção não custa uma chamada a mais. Depois dela, as
divergências de volume em 120 semanas caíram de 410 para 1.

**O PDF é conversão do HTML, não uma terceira renderização.** Markdown, HTML e
PDF continuam saindo do mesmo modelo de dados. Três motores são tentados na
ordem do config — Chrome/Chromium headless, weasyprint, wkhtmltopdf — e, se
nenhum existir, a falha diz o que instalar. Uma diferença deliberada entre tela
e papel: os números que justificam cada sinal ficam recolhidos em `<details>` no
HTML e **abertos no PDF**, porque no papel não há o que clicar e auditabilidade
é requisito da spec.

**O Chrome escreve o PDF e não sai.** Medido em 08/09/2026: `--print-to-pdf`
gera o arquivo inteiro e válido, e o processo continua vivo — 45 s, 10 min, em
`--headless=new` e `--headless=old`. Esperar pelo código de saída pendurava o
comando por 2 minutos com o PDF pronto no disco. Agora espera-se o **marcador
`%%EOF` no arquivo** e encerra-se o navegador: 2,9 s.

**O dashboard não coleta.** Lê o mesmo SQLite e reusa o mesmo `build_model` do
relatório — se a tela e o relatório de sexta discordassem, um dos dois estaria
mentindo. Mostra no topo de quando é o dado e avisa quando o cache passa de uma
semana, porque página aberta há três dias parece atual. Escuta só em
`localhost` (o default do Streamlit publica na rede local) e sobe com a
telemetria desligada.

## Divergências da spec, e por quê

Três pontos onde a implementação vai além da letra do §R5/R6. Todos
configuráveis, todos com o default reproduzindo o comportamento pedido.

| O quê | Por quê |
|---|---|
| `events.spring.min_support_touches` (default **1** = regra literal da spec) | A spec define spring como "mínima perfura suporte do range". Com o suporte lido como mínima corrente, *toda nova mínima* do range vira spring. Subir para 2 exige um suporte já testado; nos dados de 07/09/2026 isso descarta 7 dos 42 springs. O default fica na spec — o calibre existe para R11. |
| Eventos `sow` e `lpsy` | R5 não os lista, mas R6 exige "equivalentes de distribuição" para a máquina de estados. São espelhos exatos de SOS e LPS. |
| `events.*.trend_weeks`, `test.max_distance_to_spring_low_atr`, `sos.internal_resistance_weeks` | A spec diz "após tendência de baixa", "recuo pós-spring", "resistência interna" sem números. Cada um virou parâmetro documentado no `config.yaml` em vez de constante no código. |

**AR e ST não são detectados.** R6 descreve a Fase A como "climax/AR/ST", mas
R5 — a lista normativa de eventos — não os inclui. A Fase A é aberta pelo climax
apenas. Limitação conhecida, não esquecimento.

## Auditoria de 07/09/2026

**Fase 1 revalidada.** Recomputação independente das métricas de R3 a partir do
SQLite (12 papéis × 120 semanas × 13 colunas, sem reusar `src/`): divergência
relativa máxima de 9,4e-16 — ruído de ponto flutuante. Candles semanais
conferidos contra download diário fresco do yfinance: idênticos.

**Fase 2, três defeitos encontrados no caminho:**

| # | Defeito | Impacto |
|---|---------|---------|
| 1 | Máquina de estados engolia evento de viés contrário quando já estava em Fase E | BBAS3 aparecia em **markdown na semana em que imprimiu um SOS** rompendo a resistência do range. Regressão em `test_evento_do_lado_oposto_reabre_a_leitura` |
| 2 | LPS: a sequência de volume decrescente não parava quando o preço perdia o nível rompido | Um recuo continuava contando depois de devolver o rompimento — e aí não é mais *last point of support* |
| 3 | Seção 5 do relatório repetia o mesmo ticker uma vez por etapa que tropeçou nele | Papel quebrado gerava 3 linhas idênticas; a seção que devia ser varrida em segundos triplicava |


## O que o backtest disse (07/09/2026, 12 papéis × 120 semanas, modo causal)

Primeira rodada de R11. **Amostra pequena, um período, um regime de mercado
altista** — serve para calibrar, não para concluir.

| evento | n | mediana 4s | mediana 13s | acerto 13s | vs. qualquer semana (13s) |
|---|---:|---:|---:|---:|---:|
| qualquer semana (base) | 1032 | +1,5% | +5,6% | — | — |
| SOS | 16 | +6,7% | +17,5% | 67% | +11,9 p.p. |
| esforço × resultado | 27 | +2,9% | +4,3% | 89% | −1,3 p.p. |
| teste do spring | 19 | −0,3% | +8,7% | 62% | +3,1 p.p. |
| spring | 50 | **−1,1%** | +3,3% | 66% | **−2,3 p.p.** |
| SOW (baixa) | 16 | +2,0% | +8,1% | 33% | — |
| upthrust (baixa) | 7 | +3,2% | +2,2% | 33% | — |

Duas leituras que valem ação:

**O spring, na regra literal da spec, rende menos que uma semana qualquer.** Era
a suspeita levantada na Fase 2 — com o suporte lido como mínima corrente, *toda
nova mínima do range* vira spring. O calibre `min_support_touches` confirma:

| toques exigidos | n (4s) | mediana 4s | acerto 4s | acerto 13s | mediana 26s |
|---:|---:|---:|---:|---:|---:|
| 1 (default, regra da spec) | 50 | −1,1% | 44% | 66% | +14,3% |
| 2 | 43 | +0,4% | 51% | 70% | +14,3% |
| 3 | 32 | +0,6% | 56% | **79%** | **+16,7%** |

Exigir suporte já testado troca quantidade por qualidade, monotonicamente.
**O default segue em 1 — a decisão de subir é sua**, e é o tipo de coisa que a
spec §R11 diz explicitamente que o backtest existe para informar.

**As regras de baixa não separam nada neste período.** SOW e upthrust acertam
33% das vezes num mercado que subiu; o excesso contra o índice fica em torno de
zero. Pode ser o regime, pode ser a regra — com n de 7 a 16 não dá para saber.
Vale reavaliar quando houver um trecho de baixa na amostra.

## O que as duas fontes dizem do mesmo papel (08/09/2026, 120 semanas)

A §8 pergunta se o Yahoo entrega bem proventos e splits de papel brasileiro e
manda "definir fallback (brapi.dev) se houver buracos". `wyckoff sources` é a
régua. Comparação nos quatro papéis que a brapi atende sem token:

| papel | pregões | faltando | preço >0,5% | pior preço | volume >1% | spread/ATR mediana | spread/ATR máx | pos. fech. máx |
|---|---|---|---|---|---|---|---|---|
| PETR4.SA | 581 | 0 | 2047 | 3,15% | 0 | 0,0027 | 0,094 | 0,139 |
| VALE3.SA | 581 | 0 | 1598 | 2,88% | 2 | 0,0016 | 0,090 | 0,181 |
| ITUB4.SA | 581 | 0 | 1586 | 1,50% | 0 | 0,0019 | 0,044 | 0,033 |
| MGLU3.SA | 581 | 0 | 3 | 2,06% | 1 | 0,0001 | 0,039 | 0,005 |

("preço >0,5%" conta cada campo OHLC divergente; as três últimas colunas são o
efeito nas métricas de R3, já em cima do candle semanal.)

**Buraco não é o problema — nenhuma fonte perdeu um pregão sequer.** As duas
entregaram os mesmos 581 pregões em todos os papéis. O volume também bate:
mediana de divergência 0,000%.

**O que diverge é o nível do preço ajustado.** A razão entre as duas séries é
1,000 hoje e cresce para trás em degraus — 22 deles em PETR4, cada um numa
data-ex —, chegando a 2,7% dois anos atrás. Ou seja: as fontes discordam do
tamanho de alguns dividendos e JCP, não da existência deles.

**Para o que este sistema faz, isso quase não muda nada, e o "quase" tem nome.**
As métricas de R3 são razões calculadas em janelas locais, então um fator que
varia devagar se cancela: a mediana da diferença em `spread/ATR` é 0,002 e em
posição do fechamento é ~0,00001. As exceções são as semanas que **atravessam
uma data-ex sobre a qual as fontes discordam** — ali o candle semanal mistura
dois fatores diferentes e o `spread/ATR` sai até 0,09 fora (PETR4, semana de
23/12/2024: spread 0,53 contra 0,43). E há um caso em que a discordância não é
de ajuste: na semana de 04/05/2026 as duas fontes registram **fechamentos
diferentes para a sexta-feira** de VALE3, 0,9% além do fator, o que move a
posição do fechamento de 0,74 para 0,92.

**Conclusão:** não há motivo para trocar a fonte padrão. A brapi entra como
`fallback` — que é como está entregue, desligada por padrão — e vale a pena ter
porque cobre o caso em que o Yahoo simplesmente não responde por um ticker.
Ligar assim:

```yaml
data:
  source:
    default: yfinance
    fallback: [brapi]      # só é usada quando o yfinance falha
```

Exercitado de ponta a ponta em 08/09/2026 com roteamento `b3: brapi` +
`fallback: [yfinance]`: VALE3 servida pela brapi, BBAS3 e ^BVSP (401 na brapi)
caíram no yfinance, nenhum papel ficou sem dado.

## Conferência visual no navegador (08/09/2026)

O relatório e o dashboard foram abertos no Chrome, não só validados por
estrutura. O que ficou comprovado:

**Relatório HTML** — as cinco seções na ordem da spec, tema escuro do sistema
respeitado, os 12 gráficos embutidos, o `<details>` de cada papel abrindo com os
números de cada regra (evento, limiar exigido, valor medido). A tabela larga
(1234 px) rola dentro da própria caixa e **a página não rola de lado** —
verificado medindo `scrollWidth` contra `innerWidth`, tanto em tela cheia quanto
a 390 px.

**Em 390 px** (R8, "legível em celular"): a media query entra, a fonte cai para
14 px, os cartões e a lista de auditoria quebram linha corretamente e o gráfico
encolhe de 1217 px para 368 px sem estourar a margem. Nesse tamanho o candle é
legível em traço geral, mas os rótulos de eixo e as etiquetas SOS/SPR/LPS ficam
pequenos — é o preço de espremer um gráfico semanal de 60 candles num celular;
o zoom do navegador resolve.

**Dashboard** — as três abas funcionam, o gráfico ocupa a largura toda, o painel
de Ponto & Figura mostra o alvo e abre a contagem passo a passo, e os números
coincidem com o terminal e com o relatório (BBAS3: alvo 30,15, +33,9%,
7 colunas, linha 19,89).

**Dois defeitos que só apareceram no navegador:**

1. **O painel de P&F quebrava a página.** Chamava `CauseCount.summary`, que não
   existe (o certo é `describe()` + `audit_lines()`). Os testes não pegaram
   porque a série sintética da fixture era lisa: sem viés de fase declarado, o
   `for_analysis` devolvia `None` e o painel nunca era exercitado. A fixture
   passou a ter queda de 25% seguida de lateralização oscilante — o que produz
   fase, range e contagem de causa de verdade — e há teste de regressão em cima
   da linha do P&F.
2. **Layout do papel espremido.** O `st.columns([3, 2])` reduzia o gráfico a
   ~350 px e quebrava o texto da ficha em uma palavra por linha. Agora o
   gráfico usa a largura inteira e a ficha vem embaixo, em duas colunas.

Também caiu o botão "Deploy" da barra do Streamlit: não há nuvem para onde
publicar num screener local.

## Estrutura

```
config.yaml         parâmetros das heurísticas (nada hardcoded no código)
watchlist.yaml      papéis, invalidações, ranges manuais, calendário
src/
  config.py         carga do config, merge sobre defaults
  watchlist.py      R1 — schema + validação (todos os erros numa passada)
  data/provider.py  R2 — DataProvider + YFinanceProvider + agregação semanal
  data/brapi.py     P2 — fonte alternativa da B3 (brapi.dev)
  data/factory.py   P2 — roteamento por mercado + cadeia de fallback
  data/cache.py     R2 — SQLite: candles diários e semanais, proventos, log
  metrics.py        R3 — funções puras
  ranges.py         R4 — detecção de lateralização
  events.py         R5 — uma função pura por evento, cada sinal com seus números
  phases.py         R6 — máquina de estados A–E
  alerts.py         R7 — invalidação e calendário
  analysis.py       montagem da leitura por papel
  charts.py         R8 — gráfico semanal anotado (mplfinance)
  report.py         R8 — modelo único -> Markdown + HTML
  notify.py         R9 — Notifier + Telegram + e-mail; segredos por env
  pnf.py            R10 — Ponto & Figura e contagem de causa
  backtest.py       R11 — calibragem, com modo causal
  screener.py       R12 — universos e ranking de candidatos
  env.py            carga do .env; ambiente real vence o arquivo
  sources.py        P2 — comparação entre fontes (a régua da §8)
  pdf.py            P2 — HTML -> PDF, três motores, o que existir na máquina
  dashboard.py      P2 — Streamlit sobre o mesmo cache e o mesmo modelo
  pipeline.py       orquestração; falha de um ticker não derruba o lote
  cli.py            argparse
templates/          report.md.j2 + report.html.j2
universe.yaml       universos do screener (lista de partida, você mantém)
tests/              454 testes, sem rede
```

## Estado das fases

- **Fase 1** — R1 (watchlist), R2 (coleta + cache), R3 (métricas + CSV): pronta.
- **Fase 2** — R4 (ranges), R5 (eventos), R6 (fases), R7 (invalidação +
  calendário), R8 (relatório .md/.html + gráficos): pronta.
- **Fase 3** — R9 (notificação), R12 (screener), R10 (contagem de causa),
  R11 (backtest): pronta, na ordem de valor que a spec pediu.
- **P2** — fonte plugável (brapi.dev atrás do `DataProvider`), exportação para
  PDF e dashboard Streamlit: prontos. A spec os marca como "não implementar
  agora, mas não bloquear arquiteturalmente"; foram feitos a pedido, depois de
  as três fases de entrega estarem fechadas.

## Pendências da spec §8

- ~~Qualidade dos dados do Yahoo para B3, e fallback~~ — investigado duas
  vezes: EMBJ3 sem descontinuidade e sem semanas de volume zero (07/09/2026); e
  agora contra a própria brapi.dev, com números na seção "O que as duas fontes
  dizem do mesmo papel". Fallback definido e implementado, desligado por padrão.
- **Níveis de invalidação por papel (R7).** A máquina está pronta e testada, mas
  **nenhum papel tem nível preenchido** na watchlist — a seção 1 do relatório
  avisa isso em toda execução e não tem como alertar nada até você preencher.
  Use `wyckoff add TICKER --invalidation PREÇO` ou edite a watchlist:
  ```yaml
  - symbol: PETR4.SA
    market: b3
    invalidation: {price: 44.00, direction: below}
  ```
- ~~Telegram ou e-mail para R9~~ — os dois implementados; escolha em
  `notify.backend`. Falta você exportar as credenciais:
  ```bash
  export WYCKOFF_TELEGRAM_TOKEN='...'   # ou WYCKOFF_SMTP_USER/PASSWORD
  export WYCKOFF_TELEGRAM_CHAT_ID='...'
  ```
  e ligar `notify.enabled: true` no config.

## O que ainda não foi verificado

- ~~HTML e dashboard não vistos num navegador~~ — conferidos no Chrome em
  08/09/2026; ver "Conferência visual".
- O **PDF** foi conferido visualmente: 17 páginas, 12 gráficos embutidos, os
  números de cada sinal abertos, tabela inteira dentro da margem.
- A brapi.dev foi exercitada contra a API real só nos quatro papéis que a
  camada gratuita atende sem token (PETR4, VALE3, ITUB4, MGLU3). O caminho **com
  token** — que é o que serve para valer — está testado só com transporte falso;
  o primeiro uso real é seu.
- A calibragem contra a **sua** leitura manual (métrica do mês 1: ≥ 90% de
  recall, ≤ 3 falsos positivos por relatório) ainda depende de você revisar
  alguns relatórios. O backtest mede o que acontece *depois* do sinal; não mede
  se o sinal é o que você teria marcado no gráfico.
- **Nenhuma notificação foi enviada de verdade.** O canal foi exercitado só com
  transporte falso nos testes e `--dry-run` no terminal; o primeiro envio real
  é seu.
- O universo do screener foi conferido contra a fonte em 07/09/2026 e limpo, mas
  apodrece sozinho: rode `wyckoff universe --check` de vez em quando.

---

*Ferramenta de estudo pessoal. Sinais heurísticos, sem garantia. Não constitui
recomendação de investimento.*
