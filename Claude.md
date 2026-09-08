# SPEC — Wyckoff Screener: sistema de análise técnica semanal (B3 + NYSE/Nasdaq)

**Autor:** Bruno · **Data:** 08/07/2026 · **Versão:** 1.0
**Uso:** entregar este documento ao Claude Code como base de implementação. Implementar em fases, na ordem definida. Não avançar de fase sem os critérios de aceite da anterior passando.

---

## 1. Problema

Mantenho uma rotina semanal de análise Wyckoff (gráfico semanal, 10+ semanas de candles) sobre uma watchlist de ações brasileiras e americanas. O processo manual é lento e sujeito a erro: coletar OHLCV, comparar volume com a média de 20 semanas, classificar eventos (spring, teste, SOS, LPS, upthrust), verificar níveis de invalidação e força relativa contra o índice. Quero um sistema local que automatize a coleta e a classificação heurística e gere um relatório semanal pronto para minha revisão de sexta-feira.

## 2. Objetivos

1. Reduzir a rotina semanal de ~2h para <15 min de revisão de um relatório gerado automaticamente.
2. Classificar cada papel da watchlist em fase Wyckoff (A–E, acumulação/distribuição/markup/markdown) com justificativa numérica transparente.
3. Detectar e sinalizar eventos da semana (spring, teste, SOS, LPS, UT/UTAD, climax) com as regras de volume/spread explícitas em cada sinal.
4. Alertar violação de níveis de invalidação definidos por mim por papel.
5. Zero custo de dados (fontes gratuitas) e execução 100% local.

## 3. Não-objetivos (v1)

- **Execução de ordens / integração com corretora** — risco desnecessário; decisão de trade é humana.
- **Dados intraday ou tempo real** — a metodologia usa candle semanal fechado; tempo real não agrega e encarece.
- **Garantia de sinal** — o sistema é um assistente heurístico, não um oráculo; toda saída inclui os números que a justificam.
- **Interface web multiusuário** — uso pessoal, CLI + relatório em arquivo bastam na v1 (dashboard é P2).
- **Machine learning** — regras determinísticas e auditáveis primeiro; ML só depois de baseline validado.

## 4. Usuário e histórias

Usuário único (analista pessoa física, perfil técnico).

- Como analista, quero cadastrar tickers B3 e US numa watchlist com níveis de invalidação e notas, para acompanhar meus setups sem retrabalho.
- Como analista, quero rodar um comando único na sexta à noite que baixe os dados e gere o relatório da semana, para revisar tudo de uma vez.
- Como analista, quero que cada evento detectado mostre os números que o justificam (volume vs média, spread, posição do fechamento), para auditar o sinal antes de confiar nele.
- Como analista, quero ver a força relativa de cada papel contra seu índice (IBOV/SPX), para filtrar candidatos com change of character.
- Como analista, quero ser avisado quando um fechamento semanal violar um nível de invalidação, para agir sem depender de olhar o gráfico.
- Como analista, quero gráficos semanais anotados com os eventos detectados, para validar visualmente a leitura.

## 5. Requisitos

### P0 — Must have (Fase 1 + Fase 2)

**R1. Watchlist configurável (arquivo `watchlist.yaml`)**
- Campos por ticker: símbolo, mercado (`b3`/`us`), índice de referência (`^BVSP`/`^GSPC`), nível de invalidação (preço + direção), range mapeado (suporte/resistência, opcional), notas, datas de eventos (balanço, assembleia).
- [ ] Adicionar/remover ticker sem tocar em código
- [ ] Validação de schema com mensagens de erro claras

**R2. Coleta de dados (yfinance)**
- Candles semanais OHLCV, mínimo 120 semanas de histórico, tickers B3 com sufixo `.SA`.
- Cache local em SQLite; re-execução no mesmo dia não rebaixa dados.
- [ ] Falha de um ticker não aborta a execução (log + seção de erros no relatório)
- [ ] Detecta e sinaliza candle semanal ainda não fechado (rodar antes de sexta 18h BRT)
- [ ] Ajuste por proventos: usar preços ajustados e registrar no relatório quando houve evento (split/dividendo) na janela

**R3. Métricas por candle semanal**
- Volume relativo: volume / SMA(volume, 20 semanas)
- Spread relativo: (high−low) / ATR(20 semanas)
- Posição do fechamento no candle: (close−low)/(high−low)
- Força relativa: performance 4/12 semanas do papel vs índice de referência
- [ ] Métricas exportadas em tabela (CSV) além do relatório

**R4. Detecção de trading range**
- Heurística: janela deslizante que identifica lateralização (desvio de fechamentos < X% por N≥8 semanas) e marca suporte/resistência do range; parâmetros no `config.yaml`.
- Se o usuário definiu range manual na watchlist, o manual prevalece.
- [ ] Saída: range ativo (sim/não), limites, idade em semanas

**R5. Classificação de eventos Wyckoff (regras determinísticas, parametrizáveis)**
Cada regra referencia as métricas de R3. Valores default abaixo, ajustáveis no config:
- **Selling Climax:** queda com spread ≥ 1,5× ATR20 e volume ≥ 2× média, após tendência de baixa
- **Spring:** mínima perfura suporte do range; volume < 1,2× média; fechamento retorna acima do suporte em ≤ 2 candles
- **Teste:** recuo pós-spring com volume < volume do spring e mínima acima da mínima do spring
- **SOS:** candle de alta, spread ≥ 1,3× ATR20, volume ≥ 1,5× média, fechamento no terço superior, rompendo resistência interna
- **LPS:** recuo pós-SOS com volume decrescente por ≥ 2 semanas segurando acima do nível rompido
- **Upthrust/UTAD:** máxima perfura resistência e fecha de volta dentro do range com volume alto
- **Esforço × resultado:** volume ≥ 1,5× média com progresso de preço < 0,5× ATR20 → flag de anomalia (absorção ou distribuição, conforme contexto)
- [ ] Cada detecção emite: evento, data, números que dispararam a regra, e frase-resumo
- [ ] Nenhum evento é emitido fora de contexto de fase (ex.: spring exige range ativo)

**R6. Classificação de fase**
- Máquina de estados por ticker: Sem range → Fase A (climax/AR/ST) → Fase B → Fase C (spring/UT) → Fase D (SOS/LPS) → Fase E (markup/markdown), com equivalentes de distribuição.
- [ ] Fase atual + evento pendente ("aguardando teste do spring") no relatório

**R7. Verificação de invalidação e calendário**
- [ ] Fechamento semanal violando nível de invalidação gera alerta destacado no topo do relatório
- [ ] Eventos de calendário (balanços etc.) nos próximos 14 dias listados por ticker

**R8. Relatório semanal (Markdown + HTML)**
- Estrutura: (1) alertas de invalidação; (2) eventos detectados na semana; (3) tabela-resumo da watchlist (fase, volume relativo, força relativa, próximo evento esperado); (4) seção por ticker com gráfico; (5) erros de coleta.
- Gráfico semanal (mplfinance ou plotly): candles + volume + range + anotações de eventos + nível de invalidação.
- [ ] Comando único: `wyckoff report` gera `reports/AAAA-SS.md` e `.html`
- [ ] Relatório legível em celular (HTML responsivo simples)

### P1 — Should have (Fase 3)

- **R9. Notificação:** envio do resumo por Telegram bot ou e-mail após geração.
- **R10. Contagem de causa (Ponto & Figura):** projeção de alvo a partir do range, com box size configurável.
- **R11. Backtest simples das heurísticas:** varrer o histórico, marcar eventos detectados e medir o retorno N semanas à frente — para calibrar parâmetros, não para prometer performance.
- **R12. Screener de novos candidatos:** varrer lista ampla (ex.: componentes do IBOV) e ranquear papéis em Fase C/D.

### P2 — Futuro (não implementar agora, mas não bloquear arquiteturalmente)

- Dashboard local (Streamlit) lendo o mesmo SQLite.
- Fonte de dados alternativa plugável (brapi.dev para B3) atrás de uma interface `DataProvider`.
- Exportação do relatório para PDF.

## 6. Stack e estrutura sugeridas

- Python 3.11+, `yfinance`, `pandas`, `mplfinance` (ou `plotly`), `pyyaml`, `sqlite3` (stdlib), `jinja2` para o HTML, `pytest`.
- CLI com `typer` ou `argparse`: `wyckoff fetch`, `wyckoff report`, `wyckoff add TICKER`, `wyckoff backtest` (P1).

```
wyckoff-screener/
├── config.yaml            # parâmetros das heurísticas
├── watchlist.yaml
├── src/
│   ├── data/provider.py   # interface DataProvider + YFinanceProvider
│   ├── data/cache.py      # SQLite
│   ├── metrics.py         # R3
│   ├── ranges.py          # R4
│   ├── events.py          # R5 (uma função pura por evento, testável)
│   ├── phases.py          # R6 (máquina de estados)
│   ├── report.py          # R8
│   └── cli.py
├── templates/report.html.j2
├── tests/                 # fixtures com OHLCV sintético por evento
└── reports/
```

**Princípios de implementação (instruções ao Claude Code):**
1. Funções de detecção puras (DataFrame → lista de eventos), sem I/O — testar cada evento com fixture sintética que o dispara e outra que quase dispara.
2. Todo threshold vem do `config.yaml`; nada hardcoded.
3. Toda saída de sinal inclui os números (auditabilidade > concisão).
4. Watchlist inicial para desenvolvimento: BBAS3.SA, B3SA3.SA, VALE3.SA, PETR4.SA, EMBJ3.SA, PRIO3.SA, BBSE3.SA, ITUB4.SA, BAC, SBUX, JNJ, MDLZ.
5. Tratar B3 e US com calendários de pregão distintos (feriados ≠).

## 7. Métricas de sucesso

- **Semana 1 pós-MVP:** relatório gerado em < 5 min para 15 tickers, sem intervenção manual.
- **Mês 1:** ≥ 90% dos eventos que eu identificaria manualmente no gráfico aparecem no relatório (validação manual amostral); falsos positivos ≤ 3 por relatório.
- **Contínuo:** zero violação de invalidação percebida com atraso (alerta sempre no relatório da própria semana).

## 8. Questões em aberto

- [engenharia, não-bloqueante] Qualidade dos dados do Yahoo para B3 em proventos/splits — validar EMBJ3 (mudança de ticker em 2025) e definir fallback (brapi.dev) se houver buracos.
- [usuário, bloqueante p/ R7] Confirmar lista inicial de níveis de invalidação (posso extrair do meu plano de julho/2026).
- [usuário, não-bloqueante] Telegram ou e-mail para R9?

## 9. Fases de entrega

1. **Fase 1 (MVP de dados):** R1, R2, R3 + tabela CSV. *Aceite: rodar `wyckoff fetch` e obter métricas corretas para 3 tickers validadas à mão.*
2. **Fase 2 (cérebro + relatório):** R4–R8. *Aceite: relatório completo da watchlist com eventos auditáveis e gráficos anotados.*
3. **Fase 3 (conveniências):** R9–R12, na ordem de valor: R9 → R12 → R10 → R11.

Regras de saída:
- toda saída de comando passa por grep/tail; nunca mais de 20 linhas
- ruff, mypy e pytest só mostram a linha de resultado
- não imprima o conteúdo do que acabou de escrever para conferir
- leia cada seção de README.md uma vez; não releia o que já está no contexto
- não rode a suíte inteira a cada tarefa, só no fim da fase
- Ao terminar cada tarefa: commite, e diga em uma linha o que verificou.

---

*Disclaimer para o rodapé do relatório gerado: "Ferramenta de estudo pessoal. Sinais heurísticos, sem garantia. Não constitui recomendação de investimento."*
