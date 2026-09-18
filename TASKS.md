# Inconsistências encontradas em 16/09/2026 — fila de ataque

Ordem: defeito de comportamento primeiro, depois documentação que mente sobre o
comportamento, depois superfície morta. Cada tarefa fecha em um commit.

## T1 — `max_weeks_since_event` não mede o que o config diz que mede
`config.yaml` descreve "idade do **evento que instalou a fase**"; `screener.
_weeks_since_event` devolve o último evento **alinhado ao viés**, qualquer que
seja. NSC entra em 1º lugar com Fase D instalada há 61 semanas porque um spring
imprimiu na semana passada — exatamente o caso que o filtro diz remover.
Resolvido em favor do texto: a idade passou a ser a do evento que instalou (ou
confirmou por último) a fase, via `phase.driver`. De quebra, `_Machine.goto`
agora limpa o driver quando a letra muda sem evento, senão uma Fase B carregava
o SOS de uma Fase D anterior. Medido: 144 -> 59 candidatos nos EUA (era 66), com
5 papéis de fase entre 34 e 60 semanas saindo. O último evento alinhado virou
coluna própria no CSV. Três testes de regressão, um deles com a forma do NSC.
- [x] feito

## T2 — `notify.enabled`: comentário contradiz o valor
`config.yaml:196` diz "`enabled: false` aqui"; a linha 202 traz `true`.
O comentário passou a dizer o que é verdade: nasce `false` no DEFAULTS e está
ligado aqui de propósito desde 13/09/2026, e mesmo ligado só `notify` e
`report --notify` disparam mensagem.
- [x] feito

## T3 — comentário do workflow contradiz o README
`semanal.yml:56` diz "o fetch só busca a semana nova"; o README e o código dizem
que as 120 semanas são rebaixadas inteiras. Mesmo cabeçalho: "~5 min"/"20 min por
mês" contra os "9,0 min"/"40 min" medidos. Os três corrigidos: o comentário do
cache agora diz o que o cache faz (história quando a coleta falha, releitura sem
rede) em vez de prometer coleta incremental que o `auto_adjust` impede.
- [x] feito

## T4 — números vencidos no README
"25 de 142" (hoje sai 25 de 66) · "o default segue em 1" para
`min_support_touches` (subiu para 2 em 12/09) · "493 testes" (são 497) ·
"quase todo comando aceita `--offline`" (só `report` e `screen`).
O 142 também está fossilizado em `screener.py:171`.
- [x] feito — números que envelhecem trocados por forma ("25 de N", contagem via `pytest -q`); o 142 fica só como registro datado de 16/09.

## T5 — `min_resistance_touches` se diz espelho do spring e não é
`config.yaml:180` comenta "espelho do calibre do spring" com 1 contra os 2 do
spring. Subir junto ou corrigir o comentário.
- [x] feito — medido (backtest causal, 889 papéis): 2 toques não mudam a qualidade; fica 1 e o comentário diz isso.

## T6 — otimização de numpy abandonada em `find_ranges`
`ranges.py:101` extrai `closes` para numpy e nunca usa; o laço segue em
`.iloc[]`. Vale o conserto: `find_ranges` roda uma vez por semana por papel no
backtest causal, que é a justificativa que `events.py` dá para a mesma extração.
- [ ] feito

## T7 — dois mapas divergentes de índice -> mercado
`pipeline.BENCHMARK_MARKETS` e `factory.B3_INDEXES` guardam conhecimento
duplicado e discordante. Unificar num lugar só.
- [ ] feito

## T8 — dois freios independentes em `universe --check`
`--pause` (0,3s) dorme além do `min_interval` (0,5s) do `ThrottledProvider`.
- [ ] feito

## T9 — código de saída de `wyckoff fetch` depende de `--no-metrics`
Com a flag, erro de coleta sai 1; sem ela, os mesmos erros saem 0.
- [ ] feito

## T10 — superfície morta e miudezas
`YFinanceProvider(session=)` guardado e nunca lido · imports não usados em
`cli.py`, `brapi.py`, `phases.py`, `report.py` · `config` inerte em
`build_message` e `render_with_weasyprint` · docstring da brapi anuncia três
itens e lista quatro · `ruff`/`mypy` exigidos pelo CLAUDE.md e ausentes do
`[dev]` · `exports/screen_top{100,150,200}_*.csv` de universos extintos ·
`cmd_sources` monta os dois providers antes da validação barata.
- [ ] feito

---

## Fora da fila, decisão sua
`watchlist.yaml` tem alteração não commitada dando invalidação 88,13 a
**EMBJ3.SA** — o papel que o README diz estar sem nível de propósito (série com
mudança de ticker em 2025). Se a decisão mudou, o README precisa acompanhar.
