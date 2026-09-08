"""Contagem de causa por Ponto & Figura (R10).

A ideia de Wyckoff: a largura do trading range mede a causa acumulada, e a
causa projeta o efeito. Em P&F isso vira aritmética — conta-se quantas colunas
o range ocupa numa linha de contagem e multiplica-se por (box × reversão).

Três coisas que o usuário precisa saber antes de confiar no número:

**É estimativa grosseira, e a fonte é o motivo.** P&F clássico se constrói com
dado intradiário; aqui o insumo é o candle semanal, que é o que o sistema
coleta. Uma semana inteira vira no máximo um movimento por direção, então
colunas curtas somem e a contagem sai conservadora. O alvo é ordem de grandeza,
não preço-alvo.

**O box percentual é o default por ser o que faz sentido em ação.** Um box de
R$ 1,00 é enorme em papel de R$ 8,00 e irrelevante em papel de R$ 280,00. Com
box percentual a grade é geométrica: em espaço logarítmico, cada box tem a
mesma largura relativa em qualquer preço. É isso que `PercentScale` faz.

**A contagem só existe com range.** Sem lateralização não há causa medida, e o
módulo devolve `None` em vez de inventar um alvo.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from .config import Config
from .ranges import TradingRange

UP, DOWN = "X", "O"


class BoxScale(ABC):
    """Grade de boxes: converte preço em índice de box e de volta."""

    @abstractmethod
    def index(self, price: float) -> int:
        """Índice do box que contém o preço (piso)."""

    @abstractmethod
    def price(self, index: int) -> float:
        """Borda inferior do box."""

    @abstractmethod
    def describe(self) -> str:
        ...


@dataclass(frozen=True)
class AbsoluteScale(BoxScale):
    box: float

    def index(self, price: float) -> int:
        return int(math.floor(price / self.box))

    def price(self, index: int) -> float:
        return index * self.box

    def describe(self) -> str:
        return f"box de {self.box:.2f} (absoluto)"


@dataclass(frozen=True)
class PercentScale(BoxScale):
    """Grade geométrica: cada box vale `pct` sobre o anterior.

    Em log, `pct` vira largura constante — por isso o índice sai de um logaritmo
    e não de uma divisão. É o que mantém a contagem comparável entre um papel de
    R$ 8,00 e um de R$ 280,00.
    """

    pct: float

    def __post_init__(self) -> None:
        if self.pct <= 0:
            raise ValueError(f"pnf.box_percent deve ser positivo, recebido {self.pct}")

    @property
    def ratio(self) -> float:
        return 1.0 + self.pct

    def index(self, price: float) -> int:
        if price <= 0:
            raise ValueError(f"preço não positivo em escala percentual: {price}")
        return int(math.floor(math.log(price) / math.log(self.ratio)))

    def price(self, index: int) -> float:
        return float(self.ratio**index)

    def describe(self) -> str:
        return f"box de {self.pct:.1%} (geométrico)"


def build_scale(config: Config, atr: float | None = None) -> BoxScale:
    """Escala de boxes conforme `pnf.box_mode`."""
    mode = str(config.get("pnf.box_mode", "percent")).lower()
    if mode == "percent":
        return PercentScale(float(config.require("pnf.box_percent")))
    if mode == "absolute":
        return AbsoluteScale(float(config.require("pnf.box_absolute")))
    if mode == "atr":
        if atr is None or not (atr > 0):
            raise ValueError("pnf.box_mode = atr exige um ATR positivo na barra de referência")
        return AbsoluteScale(float(config.require("pnf.box_atr_fraction")) * atr)
    raise ValueError(f"pnf.box_mode inválido: {mode!r} (use percent, atr ou absolute)")


@dataclass
class Column:
    """Uma coluna do P&F, em índices de box."""

    direction: str  # X (alta) | O (baixa)
    low: int
    high: int

    def covers(self, box: int) -> bool:
        return self.low <= box <= self.high


def build_columns(bars: pd.DataFrame, scale: BoxScale, reversal: int) -> list[Column]:
    """Constrói as colunas de P&F a partir das máximas e mínimas de cada barra.

    Cada barra é lida na ordem "continua o movimento, senão testa a reversão" —
    a convenção clássica. Com barra semanal não dá para saber se a máxima veio
    antes da mínima, então essa ordem é uma escolha, e ela é conservadora: gera
    menos reversões, logo menos colunas, logo alvo menor.
    """
    if bars.empty:
        return []
    colunas: list[Column] = []
    atual: Column | None = None

    for _, bar in bars.iterrows():
        alta, baixa = float(bar["high"]), float(bar["low"])
        if alta <= 0 or baixa <= 0:
            continue
        box_alta, box_baixa = scale.index(alta), scale.index(baixa)

        if atual is None:
            # Primeira coluna: a direção do próprio candle abre o gráfico.
            subiu = float(bar["close"]) >= float(bar["open"])
            atual = Column(UP if subiu else DOWN, box_baixa, box_alta)
            colunas.append(atual)
            continue

        if atual.direction == UP:
            if box_alta > atual.high:
                atual.high = box_alta
            elif box_baixa <= atual.high - reversal:
                atual = Column(DOWN, box_baixa, atual.high - 1)
                colunas.append(atual)
        else:
            if box_baixa < atual.low:
                atual.low = box_baixa
            elif box_alta >= atual.low + reversal:
                atual = Column(UP, atual.low + 1, box_alta)
                colunas.append(atual)
    return colunas


@dataclass(frozen=True)
class CauseCount:
    """Projeção de alvo com todos os números que a produziram."""

    direction: str          # "alta" | "baixa"
    columns: int
    box_size: float         # largura do box na linha de contagem, em preço
    reversal: int
    count_line: float
    target: float
    scale: str
    range_weeks: int
    current: float | None = None
    granularity: str = "semanal"
    columns_total: int = 0
    boxes: int = 0

    @property
    def upside(self) -> float | None:
        """Distância do preço atual até o alvo, em fração."""
        if not self.current:
            return None
        return self.target / self.current - 1.0

    @property
    def reaches_beyond_price(self) -> bool:
        """O alvo fica além do preço atual, no sentido projetado?

        Quando não fica, a causa medida já foi consumida pelo movimento — dizer
        "alvo 19,90" para um papel a 22,52 seria apresentar como projeção o que
        na verdade é passado.
        """
        if self.current is None:
            return True
        return self.target > self.current if self.direction == "alta" else self.target < self.current

    def describe(self) -> str:
        seta = "↑" if self.direction == "alta" else "↓"
        if not self.reaches_beyond_price:
            return (f"causa insuficiente: a contagem projeta {self.target:.2f}, aquém do "
                    f"fechamento {self.current:.2f} — o range já entregou o que media "
                    f"({self.columns} colunas em {self.count_line:.2f}, {self.granularity})")
        extra = f" ({self.upside:+.1%} do fechamento atual)" if self.upside is not None else ""
        return (f"{seta} alvo {self.target:.2f}{extra} — {self.columns} colunas × "
                f"{self.box_size:.2f} × reversão {self.reversal}, contando a partir de "
                f"{self.count_line:.2f} ({self.scale}, {self.granularity})")

    def audit_lines(self) -> list[str]:
        return [
            f"insumo: candle {self.granularity} ({self.columns_total} colunas no range)",
            f"linha de contagem {self.count_line:.2f}",
            f"colunas do range cruzando a linha: {self.columns}",
            f"largura do box na linha: {self.box_size:.2f} ({self.scale})",
            f"reversão: {self.reversal} boxes",
            f"projeção: {self.columns} colunas × {self.reversal} = {self.boxes} boxes "
            f"{'acima' if self.direction == 'alta' else 'abaixo'} da linha",
            f"alvo = {self.count_line:.2f} percorrendo {self.boxes} boxes na grade "
            f"= {self.target:.2f}",
        ]


def widest_row(columns: list[Column], scale: BoxScale, tr: TradingRange) -> int | None:
    """Índice do box cruzado por mais colunas dentro do range.

    É a linha de contagem clássica: a causa se mede onde a congestão é mais
    larga, não na extremidade. Contar no suporte subestima sistematicamente —
    só as colunas que fizeram as mínimas chegam lá, e o alvo sai abaixo do
    preço atual, que é pior do que não responder.
    """
    if not columns:
        return None
    lo, hi = scale.index(tr.support), scale.index(tr.resistance)
    if hi < lo:
        return None
    larguras = {box: sum(1 for c in columns if c.covers(box)) for box in range(lo, hi + 1)}
    if not larguras or max(larguras.values()) == 0:
        return None
    # Empate: o box mais baixo, que é o mais conservador para alvo de alta.
    return min((box for box, n in larguras.items() if n == max(larguras.values())))


def count_line_box(
    tr: TradingRange,
    config: Config,
    scale: BoxScale,
    columns: list[Column],
    driver_level: float | None = None,
) -> int | None:
    """ÍNDICE DE BOX em que a contagem horizontal é feita.

    Devolve índice e não preço porque é em índice que a projeção anda — e
    porque converter para preço e voltar perde casos válidos: numa grade
    absoluta com box maior que o preço, `price(0)` é 0,00 e qualquer guarda de
    "preço positivo" descartaria uma contagem que existe.
    """
    escolha = str(config.get("pnf.count_line", "widest")).lower()
    if escolha == "widest":
        return widest_row(columns, scale, tr)
    if escolha == "mid":
        preco = tr.mid
    elif escolha == "driver" and driver_level is not None:
        preco = float(driver_level)
    elif escolha == "resistance":
        preco = tr.resistance
    else:
        preco = tr.support
    if preco <= 0:
        return None
    return scale.index(preco)


def count_cause(
    bars: pd.DataFrame,
    tr: TradingRange,
    config: Config,
    direction: str,
    current: float | None = None,
    driver_level: float | None = None,
    source_bars: pd.DataFrame | None = None,
) -> CauseCount | None:
    """Contagem horizontal do range. `direction` em {"alta", "baixa"}.

    Devolve None quando não há colunas cruzando a linha de contagem — acontece
    em range estreito demais para o box escolhido, e é uma resposta melhor do
    que um alvo colado no preço atual.
    """
    janela = bars.iloc[tr.start : tr.end + 1]
    if janela.empty:
        return None
    granularidade = "semanal"
    if source_bars is not None and not source_bars.empty:
        # O range é definido em semanas; o P&F é desenhado nos pregões dessas
        # semanas. Recorte pelo intervalo de datas, não por posição.
        inicio = pd.Timestamp(janela.index[0])
        fim = pd.Timestamp(janela.index[-1]) + pd.Timedelta(days=6)
        recorte = source_bars.loc[(source_bars.index >= inicio) & (source_bars.index <= fim)]
        if len(recorte) > len(janela):
            janela, granularidade = recorte, "diário"

    atr = None
    if "atr" in bars.columns:
        valor = bars["atr"].iloc[tr.end]
        atr = float(valor) if pd.notna(valor) else None
    scale = build_scale(config, atr)
    reversal = int(config.get("pnf.reversal", 3))

    colunas = build_columns(janela, scale, reversal)
    if not colunas:
        return None

    box_linha = count_line_box(tr, config, scale, colunas, driver_level)
    if box_linha is None:
        return None
    linha = scale.price(box_linha)
    cruzam = sum(1 for c in colunas if c.covers(box_linha))
    if cruzam == 0:
        return None

    # A projeção anda em ÍNDICES DE BOX, não em reais. Numa grade geométrica o
    # box vale menos em reais quanto mais baixo o preço, então somar ou subtrair
    # `N × largura` medida na linha de contagem distorce — para baixo, muito:
    # MDLZ projetava −66% porque 33 boxes de 2% viravam 33 subtrações do valor
    # do box lá em cima. Em índice, descer 33 boxes é multiplicar por 1,02⁻³³.
    boxes = cruzam * reversal
    alvo_box = box_linha + boxes if direction == "alta" else box_linha - boxes
    alvo = scale.price(alvo_box)
    largura = scale.price(box_linha + 1) - scale.price(box_linha)
    return CauseCount(
        direction=direction,
        columns=cruzam,
        box_size=largura,
        boxes=boxes,
        reversal=reversal,
        count_line=linha,
        target=max(alvo, 0.0),
        scale=scale.describe(),
        range_weeks=tr.weeks,
        current=current,
        granularity=granularidade,
        columns_total=len(colunas),
    )


def for_analysis(analysis, config: Config, daily: pd.DataFrame | None = None) -> CauseCount | None:
    """Contagem para um `TickerAnalysis`, no sentido do viés da fase.

    `daily` é opcional: sem ele a contagem cai no candle semanal e o resultado
    diz isso, em vez de fingir a mesma precisão.
    """
    if not config.get("pnf.enabled", True):
        return None
    tr = analysis.governing_range
    if tr is None or analysis.latest is None:
        return None
    bias = analysis.phase.bias
    if bias == "distribuicao":
        direction = "baixa"
    elif bias == "acumulacao":
        direction = "alta"
    else:
        return None  # sem viés declarado não há sentido a projetar
    driver = analysis.phase.driver
    nivel = None
    if driver is not None:
        for chave in ("level", "support", "resistance"):
            valor = driver.refs.get(chave)
            if isinstance(valor, (int, float)):
                nivel = float(valor)
                break
    try:
        return count_cause(analysis.closed, tr, config, direction,
                           current=float(analysis.latest["close"]), driver_level=nivel,
                           source_bars=daily)
    except ValueError:
        return None
