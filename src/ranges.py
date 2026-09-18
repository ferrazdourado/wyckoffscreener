"""Detecção de trading range (R4).

Um range é uma janela de semanas em que os fechamentos ficam apertados: a
dispersão `(max(close) - min(close)) / média(close)` não passa de
`ranges.max_close_dispersion_pct` por pelo menos `ranges.min_weeks` semanas.

Duas decisões que valem explicar:

**Dispersão nos fechamentos, limites nas extremas.** A lateralização é medida
por fechamento porque é o fechamento que diz onde o papel realmente parou; os
limites do range saem das máximas e mínimas, que é o que o preço testa. Assim
um spring — que perfura com o pavio e fecha de volta lá dentro — não quebra a
detecção do range, que é exatamente o que a metodologia manda.

**Suporte e resistência "anteriores".** Se o suporte do range incluísse a
mínima do próprio spring, nada nunca perfuraria nada. Por isso `levels_before`
devolve os limites formados pelas barras ESTRITAMENTE anteriores à barra sob
exame — o mesmo nível que o operador tinha desenhado no gráfico na semana
anterior. Sem essa separação, todo evento de perfuração vira lookahead.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Config
from .watchlist import ManualRange


@dataclass(frozen=True)
class TradingRange:
    """Uma lateralização. Índices são posicionais nas barras que a geraram."""

    start: int
    end: int  # inclusivo
    start_date: dt.date
    end_date: dt.date
    support: float
    resistance: float
    dispersion: float
    source: str = "detected"  # detected | manual

    @property
    def weeks(self) -> int:
        return self.end - self.start + 1

    @property
    def height(self) -> float:
        return self.resistance - self.support

    @property
    def mid(self) -> float:
        return (self.support + self.resistance) / 2.0

    def contains_index(self, i: int) -> bool:
        return self.start <= i <= self.end

    def position_of(self, price: float) -> float:
        """Onde o preço está dentro do range: 0 = suporte, 1 = resistência."""
        if self.height <= 0:
            return 0.5
        return (price - self.support) / self.height

    def describe(self) -> str:
        origem = "manual" if self.source == "manual" else "detectado"
        plural = "semana" if self.weeks == 1 else "semanas"
        return (
            f"{self.support:.2f}–{self.resistance:.2f} ({origem}, {self.weeks} {plural}, "
            f"desde {self.start_date.strftime('%d/%m/%Y')})"
        )


def close_dispersion(closes: pd.Series) -> float:
    """(max - min) / média. Fração: 0,12 = fechamentos dentro de uma faixa de 12%."""
    if len(closes) == 0:
        return float("nan")
    mean = float(closes.mean())
    if mean <= 0:
        return float("inf")
    return float(closes.max() - closes.min()) / mean


def _dispersion(closes: np.ndarray) -> float:
    """`close_dispersion` sobre array, ignorando NaN como o pandas ignora."""
    mean = float(np.nanmean(closes))
    if mean <= 0:
        return float("inf")
    return float(np.nanmax(closes) - np.nanmin(closes)) / mean


def find_ranges(bars: pd.DataFrame, config: Config) -> list[TradingRange]:
    """Todos os ranges maximais e não sobrepostos da série, em ordem cronológica.

    Varredura gulosa: a partir de cada início candidato, estende enquanto a
    dispersão couber no limite; se a janela alcançou `min_weeks`, vira range e
    a varredura recomeça na barra seguinte ao seu fim. Ranges não se sobrepõem
    porque uma semana pertence a uma lateralização só — se a dispersão voltou a
    estourar, é outro contexto.
    """
    min_weeks = int(config.require("ranges.min_weeks"))
    max_disp = float(config.require("ranges.max_close_dispersion_pct"))
    if min_weeks < 2:
        raise ValueError(f"ranges.min_weeks inválido: {min_weeks} (mínimo 2)")

    # Roda uma vez por semana por papel no backtest causal: o laço lê o array,
    # não `.iloc[]`, que monta uma Series por janela testada.
    closes = bars["close"].to_numpy(dtype=float)
    n = len(closes)
    out: list[TradingRange] = []
    start = 0
    while start + min_weeks <= n:
        end = start + min_weeks - 1
        if _dispersion(closes[start : end + 1]) > max_disp:
            start += 1
            continue
        # Janela mínima cabe: estende enquanto continuar cabendo.
        while end + 1 < n and _dispersion(closes[start : end + 2]) <= max_disp:
            end += 1
        out.append(_range_from_slice(bars, start, end, "detected"))
        start = end + 1
    return out


def _range_from_slice(bars: pd.DataFrame, start: int, end: int, source: str) -> TradingRange:
    window = bars.iloc[start : end + 1]
    return TradingRange(
        start=start,
        end=end,
        start_date=pd.Timestamp(bars.index[start]).date(),
        end_date=pd.Timestamp(bars.index[end]).date(),
        support=float(window["low"].min()),
        resistance=float(window["high"].max()),
        dispersion=close_dispersion(window["close"]),
        source=source,
    )


def manual_range_span(bars: pd.DataFrame, manual: ManualRange, config: Config) -> TradingRange | None:
    """Encaixa o range manual da watchlist sobre os candles (R4: manual prevalece).

    O usuário dá suporte e resistência, não datas. O trecho é obtido andando de
    trás para frente enquanto a barra ainda encosta na faixa — com folga de
    `ranges.manual_overlap_tolerance` da altura do range, para que um spring ou
    um upthrust, que por definição saem da faixa, não encerrem o trecho.
    """
    if bars.empty:
        return None
    tol = float(config.get("ranges.manual_overlap_tolerance", 0.10))
    height = manual.resistance - manual.support
    lo = manual.support - tol * height
    hi = manual.resistance + tol * height

    end = len(bars) - 1
    start = end
    while start >= 0:
        bar = bars.iloc[start]
        if float(bar["low"]) > hi or float(bar["high"]) < lo:
            break
        start -= 1
    start += 1
    if start > end:
        return None
    window = bars.iloc[start : end + 1]
    return TradingRange(
        start=start,
        end=end,
        start_date=pd.Timestamp(bars.index[start]).date(),
        end_date=pd.Timestamp(bars.index[end]).date(),
        support=float(manual.support),
        resistance=float(manual.resistance),
        dispersion=close_dispersion(window["close"]),
        source="manual",
    )


def resolve_ranges(
    bars: pd.DataFrame, config: Config, manual: ManualRange | None = None
) -> list[TradingRange]:
    """Ranges em vigor para um papel. Com range manual, ele é o único (prevalece)."""
    if manual is not None:
        span = manual_range_span(bars, manual, config)
        return [span] if span is not None else []
    return find_ranges(bars, config)


def active_range(ranges: list[TradingRange], n_bars: int) -> TradingRange | None:
    """Range que ainda contém a última barra — o range "ativo" de R4."""
    if not ranges or n_bars == 0:
        return None
    last = ranges[-1]
    return last if last.end == n_bars - 1 else None


def range_at(ranges: list[TradingRange], i: int) -> TradingRange | None:
    """Range que contém a barra `i`."""
    for tr in ranges:
        if tr.contains_index(i):
            return tr
    return None


def governing_range(ranges: list[TradingRange], i: int, max_gap_weeks: int) -> TradingRange | None:
    """Range que governa a leitura da barra `i`.

    O que contém a barra; senão o último encerrado até `max_gap_weeks` semanas
    atrás. SOS e LPS acontecem na borda ou logo acima do range: exigir que a
    barra ainda esteja dentro dele mataria justamente a Fase D.
    """
    inside = range_at(ranges, i)
    if inside is not None:
        return inside
    previous = [tr for tr in ranges if tr.end < i]
    if not previous:
        return None
    last = previous[-1]
    return last if i - last.end <= max_gap_weeks else None


def levels_before(bars: pd.DataFrame, tr: TradingRange, i: int) -> tuple[float, float] | None:
    """(suporte, resistência) formados pelas barras do range ANTERIORES a `i`.

    Devolve None se não houver barras anteriores dentro do range — sem nível
    desenhado não há o que perfurar.
    """
    lo = tr.start
    hi = min(i, tr.end + 1)
    if hi <= lo:
        return None
    window = bars.iloc[lo:hi]
    return float(window["low"].min()), float(window["high"].max())


def summarize(tr: TradingRange | None, n_bars: int) -> dict:
    """Linha de range para a tabela-resumo do relatório (R4)."""
    if tr is None:
        return {"active": False, "support": None, "resistance": None, "weeks": 0, "source": None}
    return {
        "active": tr.end == n_bars - 1,
        "support": tr.support,
        "resistance": tr.resistance,
        "weeks": tr.weeks,
        "source": tr.source,
        "start_date": tr.start_date,
        "dispersion": tr.dispersion,
    }
