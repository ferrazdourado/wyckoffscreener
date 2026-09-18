"""Classificação de fase Wyckoff (R6).

Máquina de estados por papel, alimentada em ordem cronológica por dois tipos de
gatilho: os eventos de R5 e as bordas dos ranges de R4. O estado é o par
(letra A–E, viés acumulação/distribuição) mais o **evento pendente** — a
pergunta que a próxima semana precisa responder ("aguardando teste do spring").

Duas escolhas que moldam a leitura:

**Viés antes de evento.** Quando um range aparece sem climax detectado, o viés
vem da tendência que o antecedeu: caiu forte antes de lateralizar, candidato a
acumulação; subiu forte, a distribuição; sem tendência clara, `indefinido` —
que é uma resposta honesta e não um chute com cara de diagnóstico.

**Fase E exige confirmação.** Um fechamento único fora do range não vira markup:
são `phases.breakout_confirm_weeks` fechamentos seguidos do lado de fora. Sem
isso, todo upthrust viraria markup por uma semana e voltaria.

**Sinal que falha expira.** Uma leitura de Fase C ou D vive do nível que o
evento deixou — o suporte do spring, a resistência que o SOS rompeu. Se o preço
volta para o lado errado desse nível e fica lá, a leitura não se sustentou e a
fase recua para B, onde a causa continua sendo construída. Sem essa regra, um
SOS de janeiro mantinha o papel em "Fase D — demanda no controle" pelos oito
meses seguintes de queda.

**Evento de viés contrário sempre vale.** Dentro de um mesmo viés a fase só
avança — um spring depois de um SOS não devolve a leitura de D para C. Mas um
evento do lado oposto é *change of character* e reabre a leitura: em 31/08/2026
o BBAS3 estava em markdown e imprimiu um SOS fechando 22,52, acima da
resistência do range (21,59). Uma regra que só olhasse "não regride a fase"
engoliria justamente o sinal mais importante da semana.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from .config import Config
from .events import ACUMULACAO, DISTRIBUICAO, Event
from .ranges import TradingRange, governing_range

INDEFINIDO = "indefinido"

SEM_RANGE = "sem_range"

LABELS = {
    SEM_RANGE: "Sem range — tendência ou indefinição",
    "A_acumulacao": "Fase A — acumulação (parada da tendência de baixa)",
    "B_acumulacao": "Fase B — acumulação (construção da causa)",
    "C_acumulacao": "Fase C — acumulação (teste da oferta)",
    "D_acumulacao": "Fase D — acumulação (demanda no controle)",
    "E_acumulacao": "Fase E — markup",
    "A_distribuicao": "Fase A — distribuição (parada da tendência de alta)",
    "B_distribuicao": "Fase B — distribuição (construção da causa)",
    "C_distribuicao": "Fase C — distribuição (teste da demanda)",
    "D_distribuicao": "Fase D — distribuição (oferta no controle)",
    "E_distribuicao": "Fase E — markdown",
    "B_indefinido": "Fase B — lateralização (viés indefinido)",
    "A_indefinido": "Fase A — parada de tendência (viés indefinido)",
    "C_indefinido": "Fase C — teste (viés indefinido)",
    "D_indefinido": "Fase D (viés indefinido)",
    "E_indefinido": "Fase E — tendência fora do range",
}

# Pergunta que a próxima semana precisa responder, por estado.
PENDING = {
    SEM_RANGE: "aguardando climax ou lateralização que forme um range",
    "A_acumulacao": "aguardando a lateralização se firmar (Fase B)",
    "A_distribuicao": "aguardando a lateralização se firmar (Fase B)",
    "A_indefinido": "aguardando a lateralização se firmar (Fase B)",
    "B_acumulacao": "aguardando spring no suporte (Fase C)",
    "B_distribuicao": "aguardando upthrust na resistência (Fase C)",
    "B_indefinido": "aguardando spring ou upthrust que revele o lado (Fase C)",
    "C_acumulacao": "aguardando teste do spring",
    "C_distribuicao": "aguardando SOW ou nova falha na resistência",
    "C_indefinido": "aguardando definição do lado",
    "D_acumulacao": "aguardando LPS (recuo com volume decrescente)",
    "D_distribuicao": "aguardando LPSY (repique com volume decrescente)",
    "D_indefinido": "aguardando confirmação",
    "E_acumulacao": "markup em curso — acompanhar recuos e nível de invalidação",
    "E_distribuicao": "markdown em curso — acompanhar repiques e nível de invalidação",
    "E_indefinido": "tendência fora do range — acompanhar",
}


@dataclass(frozen=True)
class Transition:
    index: int
    date: dt.date
    code: str
    reason: str


@dataclass(frozen=True)
class PhaseState:
    code: str
    since_index: int
    since_date: dt.date | None
    pending: str
    reason: str
    transitions: tuple[Transition, ...] = ()
    driver: Event | None = None

    @property
    def letter(self) -> str | None:
        return None if self.code == SEM_RANGE else self.code.split("_")[0]

    @property
    def bias(self) -> str:
        return INDEFINIDO if self.code == SEM_RANGE else self.code.split("_", 1)[1]

    @property
    def label(self) -> str:
        return LABELS.get(self.code, self.code)

    def weeks_in_phase(self, n_bars: int) -> int:
        return max(0, n_bars - self.since_index)


def _code(letter: str | None, bias: str) -> str:
    if letter is None:
        return SEM_RANGE
    return f"{letter}_{bias}"


def infer_bias(bars: pd.DataFrame, tr: TradingRange, config: Config) -> str:
    """Viés de um range pela tendência que o antecedeu (R6)."""
    weeks = int(config.require("phases.bias_lookback_weeks"))
    threshold = float(config.require("phases.bias_min_return"))
    base = tr.start - weeks
    if base < 0 or tr.start == 0:
        return INDEFINIDO
    start = float(bars["close"].iloc[base])
    end = float(bars["close"].iloc[tr.start])
    if start <= 0:
        return INDEFINIDO
    ret = end / start - 1.0
    if ret <= -threshold:
        return ACUMULACAO
    if ret >= threshold:
        return DISTRIBUICAO
    return INDEFINIDO


# Letra que cada evento instala e o viés que ele impõe.
_EVENT_PHASE = {
    "selling_climax": ("A", ACUMULACAO),
    "buying_climax": ("A", DISTRIBUICAO),
    "spring": ("C", ACUMULACAO),
    "upthrust": ("C", DISTRIBUICAO),
    "sos": ("D", ACUMULACAO),
    "sow": ("D", DISTRIBUICAO),
}
# Onde cada evento deixou o nível que a leitura precisa segurar. Se o preço
# volta para o lado errado desse nível, o sinal falhou.
_DRIVER_LEVEL = {
    "spring": "support",
    "upthrust": "resistance",
    "sos": "level",
    "sow": "level",
    "lps": "level",
    "lpsy": "level",
}

# Eventos que confirmam sem mudar de letra.
_EVENT_CONFIRMS = {
    "test": ("C", ACUMULACAO, "aguardando SOS (rompimento com spread e volume)"),
    "lps": ("D", ACUMULACAO, "aguardando rompimento sustentado da resistência (Fase E)"),
    "lpsy": ("D", DISTRIBUICAO, "aguardando perda sustentada do suporte (Fase E)"),
}


class _Machine:
    def __init__(self, bars: pd.DataFrame, ranges: list[TradingRange], config: Config):
        self.bars = bars
        self.ranges = ranges
        self.config = config
        self.letter: str | None = None
        self.bias = INDEFINIDO
        self.since = 0
        self.pending_override: str | None = None
        self.driver: Event | None = None
        self.driver_letter: str | None = None
        self.transitions: list[Transition] = []

    @property
    def code(self) -> str:
        return _code(self.letter, self.bias)

    def goto(self, i: int, letter: str | None, bias: str, reason: str, driver: Event | None = None,
             pending: str | None = None) -> None:
        new_code = _code(letter, bias)
        changed = new_code != self.code
        letra_mudou = letter != self.letter
        self.letter, self.bias = letter, bias
        self.pending_override = pending
        if driver is not None:
            self.driver = driver
            self.driver_letter = letter
        elif letra_mudou:
            # Transição sem evento — nasce um range, um rompimento confirma a
            # Fase E — não deixa nível de referência. Guardar o driver da letra
            # anterior faria `PhaseState.driver` apontar para o SOS que instalou
            # uma Fase D enquanto a leitura corrente já é de Fase B, e quem lê o
            # driver para datar a fase leria a idade do evento errado.
            self.driver = None
            self.driver_letter = None
        if changed:
            self.since = i
            self.transitions.append(
                Transition(index=i, date=pd.Timestamp(self.bars.index[i]).date(), code=new_code, reason=reason)
            )
        elif pending is not None:
            # Mesma fase, nova expectativa (teste confirma a Fase C, LPS a D).
            self.transitions.append(
                Transition(index=i, date=pd.Timestamp(self.bars.index[i]).date(), code=new_code, reason=reason)
            )


def classify(
    bars: pd.DataFrame,
    ranges: list[TradingRange],
    events: list[Event],
    config: Config,
) -> PhaseState:
    """Percorre a série e devolve a fase da última barra, com a trilha até ela."""
    n = len(bars)
    if n == 0:
        return PhaseState(SEM_RANGE, 0, None, PENDING[SEM_RANGE], "sem candles")

    confirm_weeks = int(config.require("phases.breakout_confirm_weeks"))
    max_gap = int(config.require("ranges.max_gap_weeks"))
    starts = {tr.start: tr for tr in ranges}
    by_index: dict[int, list[Event]] = {}
    for ev in events:
        by_index.setdefault(ev.index, []).append(ev)

    m = _Machine(bars, ranges, config)

    for i in range(n):
        # 1. Nasce um range: Fase B (a causa começa a ser construída).
        tr = starts.get(i)
        if tr is not None and m.letter in (None, "A", "E"):
            bias = m.bias if (m.letter == "A" and m.bias != INDEFINIDO) else infer_bias(bars, tr, config)
            origem = ("herdado do climax da Fase A" if m.letter == "A" and m.bias != INDEFINIDO
                      else f"inferido da tendência das {config.require('phases.bias_lookback_weeks')} semanas anteriores")
            m.goto(i, "B", bias, f"range de {tr.weeks} semanas começa em {tr.start_date} (viés {bias}, {origem})")

        # 2. Eventos da semana.
        for ev in by_index.get(i, []):
            if ev.kind in _EVENT_PHASE:
                letter, bias = _EVENT_PHASE[ev.kind]
                if bias == m.bias and _rank(letter) < _rank(m.letter):
                    continue  # mesma leitura já mais adiantada: o evento não retrocede a fase
                pending = None
                if ev.kind == "spring" and not ev.confirmed:
                    pending = "aguardando confirmação do spring (fechamento de volta acima do suporte)"
                m.goto(i, letter, bias, ev.summary, driver=ev, pending=pending)
            elif ev.kind in _EVENT_CONFIRMS:
                letter, bias, pending = _EVENT_CONFIRMS[ev.kind]
                if m.letter == letter and m.bias == bias:
                    m.goto(i, letter, bias, ev.summary, driver=ev, pending=pending)

        # 3. Rompimento sustentado -> Fase E.
        gov = governing_range(ranges, i, max_gap)
        if gov is not None and i > gov.end and i - confirm_weeks + 1 >= 0 and m.letter != "E":
            closes = bars["close"].iloc[i - confirm_weeks + 1 : i + 1]
            if len(closes) == confirm_weeks:
                if (closes > gov.resistance).all():
                    m.goto(i, "E", ACUMULACAO,
                           f"{confirm_weeks} fechamentos seguidos acima da resistência do range "
                           f"({gov.resistance:.2f}) — markup confirmado")
                elif (closes < gov.support).all():
                    m.goto(i, "E", DISTRIBUICAO,
                           f"{confirm_weeks} fechamentos seguidos abaixo do suporte do range "
                           f"({gov.support:.2f}) — markdown confirmado")

        # 4. Leitura de Fase C/D que perdeu o nível que a sustentava volta para B.
        #    Sem isto, um SOS de janeiro deixaria o papel em "Fase D" até o fim da
        #    série mesmo depois de o preço devolver tudo e voltar ao fundo do range:
        #    a tabela-resumo diria "demanda no controle" sobre um papel que passou
        #    meses caindo. A confirmação usa a mesma janela do rompimento, para uma
        #    semana solta não ficar alternando a fase.
        if m.letter in ("C", "D") and m.driver is not None and i - confirm_weeks + 1 >= 0:
            level = _driver_level(m.driver)
            if level is not None and i > m.driver.index:
                closes = bars["close"].iloc[i - confirm_weeks + 1 : i + 1]
                falhou = ((closes < level).all() if m.bias == ACUMULACAO
                          else (closes > level).all())
                if len(closes) == confirm_weeks and falhou:
                    lado = "abaixo" if m.bias == ACUMULACAO else "acima"
                    m.goto(i, "B", m.bias,
                           f"{confirm_weeks} fechamentos seguidos {lado} de {level:.2f}, o nível "
                           f"do {m.driver.label} de {m.driver.date.strftime('%d/%m/%Y')} — "
                           f"a leitura de Fase {m.driver_letter} não se sustentou")
                    m.driver = None  # o nível caiu: não serve mais de referência

        # 5. Range antigo demais e nenhuma tendência declarada: volta a "sem range".
        if gov is None and m.letter not in (None, "E"):
            m.goto(i, None, INDEFINIDO, f"último range terminou há mais de {max_gap} semanas")

    pending = m.pending_override or PENDING.get(m.code, "—")
    reason = m.transitions[-1].reason if m.transitions else "nenhum gatilho de fase na janela analisada"
    return PhaseState(
        code=m.code,
        since_index=m.since,
        since_date=pd.Timestamp(bars.index[m.since]).date(),
        pending=pending,
        reason=reason,
        transitions=tuple(m.transitions),
        driver=m.driver,
    )


def _driver_level(event: Event) -> float | None:
    """Nível que o evento deixou como referência, se ele tiver um."""
    key = _DRIVER_LEVEL.get(event.kind)
    if key is None:
        return None
    value = event.refs.get(key)
    return float(value) if isinstance(value, (int, float)) else None


_ORDER = {None: 0, "A": 1, "B": 2, "C": 3, "D": 4, "E": 5}


def _rank(letter: str | None) -> int:
    return _ORDER.get(letter, 0)
