"""Montagem da leitura Wyckoff de um papel: métricas -> ranges -> eventos -> fase.

Existe para que `report.py` só se preocupe em renderizar e a CLI possa pedir a
mesma leitura sem passar por relatório nenhum.

**A análise roda só sobre semanas FECHADAS.** O candle da semana em curso tem
volume parcial e mínima provisória; deixá-lo entrar produziria springs que
somem na sexta seguinte. O candle em aberto é reportado à parte, como aviso.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from .alerts import InvalidationAlert, check_invalidation, distance_to_invalidation
from .config import Config
from .events import Event, detect_all
from .phases import PhaseState, classify
from .ranges import (
    TradingRange,
    active_range,
    governing_range,
    resolve_ranges,
    summarize,
)
from .watchlist import WatchItem


@dataclass
class TickerAnalysis:
    item: WatchItem
    metrics: pd.DataFrame          # série completa, inclusive semana em aberto
    closed: pd.DataFrame           # só semanas fechadas — base da análise
    ranges: list[TradingRange]
    active_range: TradingRange | None
    governing_range: TradingRange | None
    events: list[Event]
    recent_events: list[Event]
    phase: PhaseState
    alert: InvalidationAlert | None
    latest: pd.Series | None
    partial_week: pd.Timestamp | None
    warnings: list[str] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return self.item.symbol

    @property
    def range_summary(self) -> dict:
        return summarize(self.governing_range, len(self.closed))

    @property
    def invalidation_gap(self) -> float | None:
        if self.latest is None:
            return None
        return distance_to_invalidation(self.item, float(self.latest["close"]))

    def value(self, column: str):
        if self.latest is None or column not in self.latest.index:
            return None
        value = self.latest[column]
        return None if pd.isna(value) else value


def analyze(item: WatchItem, metrics: pd.DataFrame, config: Config) -> TickerAnalysis:
    """Leitura completa de um papel a partir das métricas de R3."""
    warnings: list[str] = []
    if "is_partial" in metrics.columns:
        mask = metrics["is_partial"].astype(bool)
        closed = metrics[~mask]
        partial = metrics.index[-1] if len(metrics) and bool(mask.iloc[-1]) else None
    else:
        closed, partial = metrics, None
    if partial is not None:
        warnings.append(
            f"semana de {pd.Timestamp(partial).date()} ainda em formação — fora da análise"
        )

    ranges = resolve_ranges(closed, config, item.manual_range)
    if item.manual_range is not None and not ranges:
        warnings.append(
            f"range manual {item.manual_range.support:g}–{item.manual_range.resistance:g} não "
            f"encosta em nenhum candle recente — nenhum range em vigor"
        )
    events = detect_all(closed, ranges, config)
    phase = classify(closed, ranges, events, config)

    n = len(closed)
    recent_weeks = int(config.get("alerts.recent_event_weeks", 1))
    recent = [e for e in events if e.index >= n - recent_weeks] if n else []

    min_weeks = int(config.get("data.min_weeks_for_metrics", 21))
    if n < min_weeks:
        warnings.append(
            f"histórico curto: {n} semanas fechadas (< {min_weeks}) — médias de "
            f"{config.get('metrics.volume_sma_weeks')} semanas ainda não válidas, "
            f"nenhuma regra de volume dispara"
        )

    return TickerAnalysis(
        item=item,
        metrics=metrics,
        closed=closed,
        ranges=ranges,
        active_range=active_range(ranges, n),
        governing_range=governing_range(ranges, n - 1, int(config.require("ranges.max_gap_weeks"))) if n else None,
        events=events,
        recent_events=recent,
        phase=phase,
        alert=check_invalidation(item, metrics),
        latest=closed.iloc[-1] if n else None,
        partial_week=pd.Timestamp(partial) if partial is not None else None,
        warnings=warnings,
    )


def analyze_all(
    watchlist, metrics: dict[str, pd.DataFrame], config: Config
) -> tuple[list[TickerAnalysis], list[str]]:
    """Leitura de toda a watchlist. Papel sem dados vira aviso, não exceção."""
    out: list[TickerAnalysis] = []
    missing: list[str] = []
    for item in watchlist:
        df = metrics.get(item.symbol)
        if df is None or df.empty:
            missing.append(item.symbol)
            continue
        out.append(analyze(item, df, config))
    return out, missing


def week_tag(analyses: list[TickerAnalysis], fallback: dt.date) -> str:
    """Etiqueta AAAA-SS da semana analisada (a última fechada dos dados)."""
    weeks = [a.latest.name for a in analyses if a.latest is not None]
    date = max(weeks).date() if weeks else fallback
    year, week, _ = date.isocalendar()
    return f"{year}-{week:02d}"
