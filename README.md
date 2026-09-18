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
wyckoff report --screen     # o mesmo, e garimpa candidatos fora da watchlist
wyckoff report --screen --offline   # relê tudo do cache, sem coletar (25 s)
wyckoff report --notify     # o mesmo, e manda o resumo pelo canal configurado

wyckoff screen b3_completa  # varre as 371 ações da B3 e ranqueia quem está em Fase C/D
wyckoff screen us_completa  # o mesmo para as 518 dos EUA (S&P 500 + Nasdaq-100)
wyckoff screen b3_liquidas  # o mesmo, na lista curta de 78 nomes conferidos
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
wyckoff report --pdf --notify  # o PDF vai junto do resumo, como anexo
wyckoff pdf                 # converte para PDF um relatório já gerado (P2)
wyckoff dashboard           # dashboard local sobre o mesmo SQLite     (P2)
wyckoff sources PETR4.SA    # põe duas fontes lado a lado no mesmo papel (P2)
```

`report` e `screen` aceitam `--offline` (usam só o cache, sem rede). É o que se
usa para **reler o relatório sem varrer tudo de novo**: `wyckoff report --screen
--offline` refaz o documento inteiro, com as duas seções de candidatos, em 25 s
e sem tocar na rede. Sem `--offline` a coleta também é pulada se já houve fetch
bem-sucedido no mesmo dia (`data.refetch_same_day: false`), mas aí depende do
dia; `--offline` vale sempre.

Saídas: `reports/AAAA-SS.md`, `reports/AAAA-SS.html` (autocontido, com os
gráficos embutidos), `reports/AAAA-SS.pdf` (com `--pdf`) e
`reports/AAAA-SS/charts/*.png`; `exports/metrics_AAAA-SS.csv` e
`exports/latest_AAAA-SS.csv`.

Referência offline (16/09/2026): relatório **2,5 s** para 12 papéis ·
`report --screen` **25 s** com os dois universos completos (872 papéis lidos do
cache) · screener **2,0 s** para 78 · backtest causal **10 s** para 12 × 120
semanas · PDF **2,9 s**.

Com coleta (16/09/2026, `--force`, rede): **0,55 s por papel**, e a rotina de
sexta inteira — watchlist, os dois universos completos, gráficos, HTML e PDF —
em **9,0 min**.

## Rodar na nuvem (GitHub Actions)

`.github/workflows/semanal.yml` executa a rotina de sexta sem a máquina ligada.
É um job em lote de ~9 min por semana, não um serviço: o cron do Actions cobre
isso de graça (repo público: ilimitado; privado: ~40 min/mês dos 2.000 do tier
gratuito). Por isso não há servidor a manter.

- **Quando:** sexta, 22:30 UTC (19:30 BRT). Mais tarde que as 18h da spec de
  propósito — a NYSE fecha 21:00 UTC no inverno americano, e 18h BRT pegaria o
  candle semanal dos papéis US ainda aberto.
- **O cache sobrevive entre as semanas** via `actions/cache`, com chave
  `wyckoff-sqlite-<ano-semana>` (a chave precisa mudar toda semana, senão o
  cache nunca é regravado) e `restore-keys` pegando o da semana anterior.
  **Ele não torna a coleta incremental:** toda sexta as 120 semanas de cada
  papel são rebaixadas inteiras (`pipeline.py`, `data.history_weeks`), porque
  com `auto_adjust=True` um provento reescreve a série toda e baixar só a ponta
  misturaria bases de ajuste. O cache serve para o relatório ainda ter história
  quando a coleta falha, e para reler no mesmo dia sem ir à rede.
- **O relatório sai como artifact**, não como commit — `reports/` está no
  `.gitignore`. As 60 primeiras linhas do `.md` vão para o resumo do run, então
  as invalidações se leem sem baixar nada. E o PDF chega no Telegram.
- **Segredos** em Settings → Secrets and variables → Actions:
  `WYCKOFF_TELEGRAM_TOKEN`, `WYCKOFF_TELEGRAM_CHAT_ID` e, se usar,
  `WYCKOFF_BRAPI_TOKEN`. Sem eles o relatório é gerado igual; só o envio falha.
- **`--pdf` funciona no runner**: o `ubuntu-latest` já traz o Chrome em
  `/usr/bin/google-chrome`, o primeiro caminho que o `pdf.py` procura.

Alternativa, se um dia quiser máquina de verdade (dashboard no ar, cron do
sistema, SQLite em disco): **Oracle Cloud Always Free** — VM ARM gratuita em
caráter permanente, não trial. Render, Railway e Fly.io não têm mais tier
gratuito que sirva; o PythonAnywhere free só fala com domínios da whitelist
deles, o que não serve para o Yahoo.

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

**Estrangulamento de IP se disfarça de ticker deslistado.** Varrendo os 518
papéis de `us_completa` em 14/09/2026, o Yahoo parou de responder depois de
~318 requisições: os 200 seguintes voltaram com "possibly delisted; no price
data found". Nenhum estava deslistado — HII, HLT e ZTS respondiam um minuto
depois, um a um. A fonte não avisa que você está rápido demais; ela devolve
série vazia, que é o que devolveria para papel morto. **A truncagem é
alfabética, então o resultado parece uma lista normal** — e num runner do
GitHub Actions ninguém está olhando.

Daí `data.fetch` (`src/data/throttle.py`), que decora qualquer fonte com três
defesas em ordem de custo:

| defesa | para que serve | default |
|---|---|---|
| `min_interval` | não provocar o corte | 0,5s entre requisições |
| `retries`/`backoff` | soluço isolado da fonte | 2 retentativas, 3s e 15s |
| `cooldown_after`/`cooldown` | corte de IP já instalado | 60s após 5 falhas seguidas |

A terceira é a que enxerga o problema pelo que ele é. **O que separa papel
morto de IP cortado não está numa requisição, está na sequência delas:** ticker
morto é falha isolada no meio de sucessos — o caso da B3, onde papel morre de
verdade — e estrangulamento vem em série. Por isso retentativa sozinha não
resolveria: num universo cortado, cada um dos 200 papéis insistiria três vezes
à toa, e num universo cheio de ticker morto o preço seria pago sem motivo.

O ritmo é montado **por fonte**, não em volta da cadeia de fallback: um corte
temporário do Yahoo não pode gastar a brapi antes de ter insistido.

**A requisição cara é a de proventos, não a das 120 semanas.** Cronometrado em
16/09/2026, por papel: 0,98s para baixar 582 pregões de preço e **1,21s** para
perguntar por dividendos e splits — mais da metade do tempo de uma varredura.

Isso derruba a otimização que parece óbvia, a de baixar só a semana nova em vez
das 120: medida, ela economiza 0,25s por papel (0,98 → 0,73), porque o custo é
o ida-e-volta HTTP e não o tamanho da resposta. E cobraria caro em correção —
com `auto_adjust=True` um split reescreve a série inteira, então baixar só a
ponta deixaria as barras velhas numa base de ajuste e as novas em outra, que é
o defeito de "bases de ajuste misturadas" entrando pela porta dos fundos.

O que rende é `screener.fetch_actions: false` (default): **a varredura de
universo não pergunta por proventos**. Os preços não pioram, porque o ajuste já
vem embutido no dado; o que falta é a marca de data-ex na semana, e ela importa
ao ler o gráfico — quando o papel já passou da triagem e está na watchlist, que
continua coletando tudo. Medido antes e depois na mesma varredura de 32 papéis:
**70,8s → 18,2s**. Em 78 papéis da B3, 42,8s, ou 0,55s por papel.

A 0,55s o gargalo passa a ser o próprio `min_interval` — o que antes era grátis
agora é quase todo o tempo. Varrer B3 e EUA completos custa **9,0 min** medidos
(ver "Garimpo de candidatos"), contra os 30 de `timeout-minutes` do workflow.

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

**O PDF vai anexado, não linkado.** A alternativa era mandar no texto um link
para o relatório no GitHub. Do outro lado desse link estaria um `.zip` de
artifact num repositório privado: logar no GitHub, baixar, extrair — sexta à
noite, no celular. E publicar em Pages para evitar o login exporia a watchlist
inteira. O `sendDocument` entrega o arquivo dentro do Telegram, offline, em um
toque, e o histórico do chat vira o arquivo morto dos relatórios. O limite do
bot é 50 MB contra ~1,3 MB do relatório.

**Anexo que falha não vira "nada enviado".** O PDF é um segundo envio, depois do
texto — e de propósito não é legenda do documento, porque a legenda do Telegram
corta em 1024 caracteres e mutilaria justamente o resumo. Se o segundo envio
falhar, o resumo já chegou: o motivo entra na linha de log (`+ 2026-37.pdf` ou
`(PDF não anexado: ...)`), e o comando não mente dizendo que a notificação
falhou.

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

### Garimpo de candidatos dentro da rotina

A spec entregou R12 como comando separado (`wyckoff screen`), e ficou um buraco
contra o objetivo do §2: a rotina de sexta deveria ser **um** comando e **um**
documento para revisar, mas descobrir papel novo exigia lembrar de um segundo
comando cuja saída ia para outro arquivo.

`wyckoff report --screen` fecha isso: acrescenta ao relatório a seção
**"Candidatos fora da watchlist"**, com fase, evento da semana, força relativa,
liquidez e a linha `wyckoff add` pronta para promover o papel. Fica em `--screen`
e não no padrão porque varrer 900 papéis custa minutos de coleta, enquanto o
relatório da watchlist sai em segundos — as duas velocidades servem a momentos
diferentes.

**Os universos varridos são os completos** (`screener.report_universes:
[b3_completa, us_completa]`). Rodado de ponta a ponta em 16/09/2026, com
`--pdf` e `--force`: **9,0 min**, 891 papéis, 890 ok e um erro — `ONCO11.SA`,
que falhou nas três tentativas isolado no meio de sucessos, a assinatura de
papel morto e não de corte de IP. Nenhum estrangulamento, contra os 30 min de
`timeout-minutes` do workflow.

A ressalva é o IP: o teste correu de uma máquina residencial, e o runner do
GitHub sai de um IP compartilhado da Azure, que o Yahoo vê muito mais
movimentado. Não há promessa de que nunca vai cortar lá — há reteste, cooldown,
e um relatório que registra o buraco em vez de escondê-lo.

Cinco decisões dentro dela:

**Papel da watchlist nunca volta como candidato.** Uma seção chamada "fora da
watchlist" que devolvesse VALE3 e ITUB4 gastaria as primeiras linhas — as que
você lê — repetindo o que já está na seção 3. Foi o primeiro defeito que
apareceu ao testar.

**Fase não expira, e triagem precisa que expire.** A máquina de estados de R6
mantém o papel em Fase D enquanto o nível do evento aguentar — correto para ler
o gráfico, e errado para montar a lista da semana. Em 16/09/2026, dos 144
candidatos americanos, **53 tinham evento de mais de 12 semanas e 15 de mais de
26**, um deles um SOS de **82 semanas atrás**. Uma lista de swing semanal
cheia de estado de um ano e meio atrás não é triagem, é inventário.

`screener.max_weeks_since_event` (6) descarta o que não mudou há pouco: 144 → 59
nos EUA. O calibre de spring, que era o candidato óbvio, quase não mordia — com
3 toques dava 131 e com 4, 110 —, porque o problema nunca foi o gatilho estar
frouxo, e sim a leitura não ter prazo.

**O que envelhece é a leitura, não um evento qualquer.** A primeira versão datava
o papel pelo último evento do mesmo viés, e com isso deixava passar justamente o
caso que o filtro existe para barrar: em 16/09/2026 o **NSC** abria a lista
americana com Fase D instalada por um SOS de **60 semanas** atrás, porque um
spring imprimira na semana anterior. Só que a máquina de estados ignora esse
spring de propósito — dentro do mesmo viés a fase não retrocede de D para C —,
então a leitura mostrada continuava sendo a de 60 semanas atrás. Agora a idade é
a do evento que instalou (ou confirmou por último) a fase; eram 5 papéis nessa
situação, com fases de 34 a 60 semanas. O último evento alinhado continua no CSV,
em coluna própria, que é onde se vê por que os dois números divergem.

Papel **sem** evento que date a fase passa direto em vez de ser descartado: é o
caso da Fase B, a causa sendo construída, que não tem evento para datar.
Descartá-la faria o filtro esvaziar em silêncio uma fase que só aparece quando
alguém a pede em `screener.phases`.

**A lista é cortada, e o relatório diz de quanto.** `screener.top` (25 por
universo) limita o que entra no documento. A primeira versão imprimia
`candidates|length`, ou seja o número já cortado — em 16/09/2026 o relatório
dizia "25 em Fase C/D" numa semana com **142** candidatos americanos. A frase
lia como censo e era teto, e escondia justamente o que diz se 25 aperta ou
folga. Agora sai "25 de N", com N contado antes do corte, e o terminal anuncia
o total antes de avisar que está mostrando os primeiros.

**Piso de liquidez.** O universo amplo tem papel que negocia quase nada, e a
leitura Wyckoff de um candle semanal formado por três negócios é ruído com nome
de sinal. `screener.min_weekly_volume` corta pela mediana do volume financeiro
semanal das últimas 12 semanas (na moeda do papel). O relatório e o terminal
sempre dizem quantos papéis o filtro descartou: filtro silencioso vira suspeita
de bug.

**A numeração das seções acompanha.** Sem `--screen` o relatório mantém as cinco
seções da spec; com ele, candidatos entram como 4 e "Papel a papel" e "Erros de
coleta" viram 5 e 6. Os candidatos vêm **antes** do detalhe por papel de
propósito: é decisão de triagem, e triagem não se lê depois de doze gráficos.

### Universo amplo da B3

`b3_completa` tem as **371 ações** do mercado à vista da B3, montadas a partir
da listagem da brapi.dev em 13/09/2026 e filtradas por padrão de código
(ON/PN/PNA/PNB/PNC/PND/UNIT). Ficaram de fora o mercado fracionário (`…F`, que é
o mesmo papel em lote ímpar e duplicaria a lista), direitos e recibos de
subscrição, BDRs e fundos. Não é lista curada — é o mercado inteiro, e é por
isso que o piso de liquidez existe.

### Universo amplo dos EUA

`us_completa` tem **518 papéis**: a união do S&P 500 (503) com o Nasdaq-100
(102), pela Wikipédia em 14/09/2026 — a sobreposição é quase total, e os 15 que
só o Nasdaq-100 traz são em boa parte empresa estrangeira, que o S&P 500 não
aceita por regra (ASML, ARM, SHOP, MELI, PDD, FER, CCEP, TRI). Símbolo de classe
vai com hífen, do jeito que o Yahoo quer: `BRK-B`, não `BRK.B`. E `ON` (ON
Semiconductor) vai entre aspas no YAML, senão é lido como o booleano `true`.

Aqui o piso de liquidez quase não filtra: `screener.min_weekly_volume` é um
número só, em moeda do papel, e os US$ 2M/semana do default não excluem nada num
índice onde o menor nome negocia muito mais que isso. O que limita a lista é
`--top`, e o que a ordena é a fase. Se um dia isso incomodar, o piso precisaria
ser por universo — hoje não é.

## Divergências da spec, e por quê

Três pontos onde a implementação vai além da letra do §R5/R6. Todos
configuráveis, todos com o default reproduzindo o comportamento pedido.

| O quê | Por quê |
|---|---|
| `events.spring.min_support_touches` (**2** no `config.yaml`; 1 = regra literal da spec, que é o que o `DEFAULTS` do código traz) | A spec define spring como "mínima perfura suporte do range". Com o suporte lido como mínima corrente, *toda nova mínima* do range vira spring. Exigir 2 toques pede um suporte já testado; nos dados de 07/09/2026 isso descarta 7 dos 42 springs. Subiu para 2 em 12/09/2026, depois que o backtest de R11 mostrou o spring literal rendendo menos que uma semana qualquer. |
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
| 1 (`DEFAULTS`, regra da spec) | 50 | −1,1% | 44% | 66% | +14,3% |
| 2 (`config.yaml` desde 12/09) | 43 | +0,4% | 51% | 70% | +14,3% |
| 3 | 32 | +0,6% | 56% | **79%** | **+16,7%** |

Exigir suporte já testado troca quantidade por qualidade, monotonicamente.
**Com esta tabela o `config.yaml` subiu para 2 em 12/09/2026**; o `DEFAULTS` do
código segue em 1, a regra literal da spec. É o tipo de decisão que a spec §R11
diz explicitamente que o backtest existe para informar.

**As regras de baixa não separam nada neste período.** SOW e upthrust acertam
33% das vezes num mercado que subiu; o excesso contra o índice fica em torno de
zero. Pode ser o regime, pode ser a regra — com n de 7 a 16 não dá para saber.
Vale reavaliar quando houver um trecho de baixa na amostra.

**O upthrust com mais toques continua sem prever queda.** A pergunta era se o
calibre que melhorou o spring (`min_resistance_touches`, espelho do
`min_support_touches`) faria o mesmo pelo upthrust. Com n de 7 a watchlist não
responde, então a medição foi nos universos completos (18/09/2026, backtest
causal, 371 B3 + 518 US). "Caiu" é retorno negativo; "abaixo do índice" é o
excesso negativo, a coluna justa num período de alta:

| grupo | n | caiu 4s | caiu 13s | caiu 26s | abaixo do índice 4s / 13s / 26s |
|---|---:|---:|---:|---:|---:|
| qualquer semana (base) | 83.492 | 47% | 45% | 41% | 55% / 57% / 60% |
| toques ≥ 1 (`config.yaml`) | 480 | 44% | 41% | 37% | 53% / 57% / 58% |
| toques ≥ 2 | 401 | 44% | 40% | 38% | 53% / 56% / 58% |
| toques ≥ 3 | 311 | 46% | 39% | 39% | 56% / 58% / 61% |
| os 77 cortados de 1 → 2 | 77 | 44% | 47% | 35% | 56% / 63% / 61% |

Os 77 que o segundo toque corta não são piores que os que ficam: a exigência
só reduz a contagem. Com 3 toques o sinal empata com a base, sem superá-la.
B3 e EUA separados dizem o mesmo. Por isso o upthrust segue com 1 toque, a
regra literal. Subir o calibre por simetria com o spring não compraria nada.

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
  data/throttle.py  R2 — ritmo, reteste e pausa longa quando a fonte corta
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
.github/workflows/  semanal.yml — a rotina de sexta rodando no GitHub Actions
templates/          report.md.j2 + report.html.j2
universe.yaml       universos do screener: 371 ações da B3, 78 líquidas, 518 US, 31 US líquidas
tests/              suíte pytest, sem rede (`pytest -q` dá a contagem)
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
- **Níveis de invalidação por papel (R7).** 11 dos 12 preenchidos em
  12/09/2026. Falta **EMBJ3.SA** — de propósito: é o papel cuja série mudou de
  ticker em 2025, e um nível tirado de um range que pode estar contaminado não
  vale nada. Enquanto estiver vazio, R7 não tem o que alertar nesse papel.
  Quando decidir o preço:
  ```bash
  wyckoff add EMBJ3.SA --invalidation PREÇO
  ```
- ~~Telegram ou e-mail para R9~~ — Telegram, funcionando. Credenciais no `.env`
  local e nos secrets do Actions, `notify.enabled: true`. Resumo e PDF chegaram
  ao chat em 13-14/09/2026, gerados na nuvem.

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
- ~~Nenhuma notificação foi enviada de verdade~~ — resumo recebido no Telegram
  em 13/09/2026 e PDF anexado em 14/09/2026, os dois a partir do Actions. O
  `sendDocument` e o multipart escrito à mão passaram pela API real.
- ~~O workflow do Actions nunca rodou~~ — rodou, e resolveu uma incógnita de
  vez: o **Chrome headless roda sem `--no-sandbox`** no `ubuntu-latest`. O
  runner só não tem as fontes da sua máquina: o matplotlib cai na DejaVu, o que
  muda o desenho dos rótulos e nada mais.
- **O runner nunca foi testado no volume de hoje.** Os runs de 13-14/09/2026
  varreram `us_large`, 31 nomes; a sexta agora pede 891 papéis. A versão antiga
  desta linha dizia que "o Yahoo não estrangulou o IP do runner" — verdade para
  o volume daquele dia, e cedo demais como conclusão: em 14/09, daqui, o corte
  veio na requisição ~636. O IP do Actions é compartilhado da Azure, que o Yahoo
  vê muito mais movimentado que um IP residencial. O que existe hoje é reteste,
  cooldown e um relatório que registra o buraco; o primeiro run grande na nuvem
  é que diz se basta.
- **O cron automático ainda não disparou.** Todos os runs até aqui foram
  `workflow_dispatch` manual. O primeiro agendado é sexta, 18/09/2026, 22:30 UTC.
- **O caminho incremental do cache não foi exercitado.** O primeiro run criou o
  SQLite do zero; quem prova o `restore-keys` é o segundo — se falhar, o sintoma
  é um run lento rebaixando 120 semanas, não um relatório errado.
- O universo do screener foi conferido contra a fonte em 07/09/2026 e limpo, mas
  apodrece sozinho: rode `wyckoff universe --check` de vez em quando.

---

*Ferramenta de estudo pessoal. Sinais heurísticos, sem garantia. Não constitui
recomendação de investimento.*
