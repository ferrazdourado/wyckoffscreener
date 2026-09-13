"""Invalidação e calendário (R7).

Duas coisas que o relatório precisa mostrar antes de qualquer análise: um nível
que o usuário definiu foi perdido, e o que está marcado na agenda dos próximos
dias.

O alerta distingue **novo** de **em curso**. A métrica de sucesso da spec é
"zero violação percebida com atraso": um nível perdido há seis semanas não pode
ter o mesmo destaque do que foi perdido nesta — senão o alerta antigo vira
paisagem e o novo passa batido no meio dele.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from .config import Config
from .watchlist import CalendarEvent, Invalidation, WatchItem


@dataclass(frozen=True)
class InvalidationAlert:
    symbol: str
    level: float
    direction: str
    close: float
    week_start: dt.date
    weeks_violated: int
    distance_pct: float  # negativo = já ultrapassou o nível
    summary: str

    @property
    def is_new(self) -> bool:
        """Violou nesta semana pela primeira vez — é o alerta que não pode passar batido."""
        return self.weeks_violated == 1


def check_invalidation(item: WatchItem, metrics: pd.DataFrame) -> InvalidationAlert | None:
    """Alerta se o último fechamento SEMANAL FECHADO violar o nível do papel.

    Só fechamento de semana encerrada conta: a metodologia é semanal e um
    intrassemana que a sexta desfaz não é violação de nada.
    """
    if item.invalidation is None or metrics.empty:
        return None
    closed = metrics[~metrics["is_partial"].astype(bool)] if "is_partial" in metrics.columns else metrics
    if closed.empty:
        return None

    inv: Invalidation = item.invalidation
    row = closed.iloc[-1]
    close = float(row["close"])
    if not inv.is_violated(close):
        return None

    # Há quantas semanas consecutivas o nível está violado.
    weeks = 0
    for value in reversed(closed["close"].tolist()):
        if not inv.is_violated(float(value)):
            break
        weeks += 1

    distance = (close - inv.price) / inv.price
    if inv.direction == "above":
        distance = -distance
    quando = "nesta semana" if weeks == 1 else f"há {weeks} semanas"
    return InvalidationAlert(
        symbol=item.symbol,
        level=inv.price,
        direction=inv.direction,
        close=close,
        week_start=pd.Timestamp(row.name).date(),
        weeks_violated=weeks,
        distance_pct=distance,
        summary=(f"{item.symbol}: fechou {close:.2f}, violando o nível de invalidação "
                 f"({inv.describe()}) {quando} — {abs(distance):.1%} além do nível."),
    )


def distance_to_invalidation(item: WatchItem, close: float) -> float | None:
    """Folga até o nível, em fração do nível. Negativo = já violado."""
    if item.invalidation is None or not close:
        return None
    inv = item.invalidation
    gap = (close - inv.price) / inv.price
    return gap if inv.direction == "below" else -gap


def upcoming_calendar(
    item: WatchItem, today: dt.date, horizon_days: int
) -> list[tuple[CalendarEvent, int]]:
    """Eventos do papel dentro do horizonte, com os dias que faltam."""
    out = []
    for event in item.calendar:
        delta = (event.date - today).days
        if 0 <= delta <= horizon_days:
            out.append((event, delta))
    return sorted(out, key=lambda pair: pair[1])


def collect_alerts(
    watchlist, metrics: dict[str, pd.DataFrame], config: Config, today: dt.date
) -> tuple[list[InvalidationAlert], list[tuple[str, CalendarEvent, int]]]:
    """Alertas de invalidação e agenda de todos os papéis, prontos para o topo do relatório."""
    horizon = int(config.get("alerts.calendar_horizon_days", 14))
    invalidations: list[InvalidationAlert] = []
    calendar: list[tuple[str, CalendarEvent, int]] = []
    for item in watchlist:
        df = metrics.get(item.symbol)
        if df is not None:
            alert = check_invalidation(item, df)
            if alert is not None:
                invalidations.append(alert)
        for event, days in upcoming_calendar(item, today, horizon):
            calendar.append((item.symbol, event, days))
    # Novos primeiro; entre iguais, o que foi mais longe além do nível.
    invalidations.sort(key=lambda a: (a.weeks_violated, -abs(a.distance_pct)))
    calendar.sort(key=lambda triple: (triple[2], triple[0]))
    return invalidations, calendar


def levels_configured(watchlist) -> tuple[int, int]:
    """(papéis com nível de invalidação, total) — a spec §8 deixou isto em aberto."""
    total = len(watchlist)
    with_level = sum(1 for item in watchlist if item.invalidation is not None)
    return with_level, total


def suggest_invalidation(analysis) -> tuple[float, str] | None:
    """Nível de invalidação derivado do range detectado: (preço, direção).

    Ponto de partida, não leitura do usuário — por isso quem mostra sempre diz
    de onde veio. A regra não é a mesma para todas as fases:

    * **distribuição** → a resistência do range: a tese morre se o preço
      escapar por cima.
    * **acumulação em Fase E** → a resistência ROMPIDA, que virou suporte. O
      piso do range já ficou longe demais para trás; um alerta lá só dispararia
      muito depois de a tese ter morrido.
    * **demais fases de acumulação** → o suporte do range.

    Sem range em vigor não há de onde derivar, e devolver um palpite seria pior
    do que não responder.
    """
    tr = getattr(analysis, "governing_range", None)
    if tr is None:
        return None
    fase = analysis.phase
    if fase.bias == "distribuicao":
        return float(tr.resistance), "above"
    if fase.bias == "acumulacao":
        if (fase.letter or "") == "E":
            return float(tr.resistance), "below"
        return float(tr.support), "below"
    return None
