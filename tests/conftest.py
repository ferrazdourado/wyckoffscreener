"""Fixtures sintéticas: OHLCV construído à mão, com valores que dá para
conferir na cabeça. Nenhum teste toca a rede."""

from __future__ import annotations

import pandas as pd
import pytest

from src.config import DEFAULTS, Config


def weeks_index(n: int, start: str = "2026-01-05") -> pd.DatetimeIndex:
    """`n` segundas-feiras consecutivas (2026-01-05 é segunda)."""
    return pd.date_range(start=start, periods=n, freq="7D", name="week_start")


def make_bars(
    n: int = 25,
    open_=10.0,
    high=12.0,
    low=10.0,
    close=11.0,
    volume=100.0,
    start: str = "2026-01-05",
) -> pd.DataFrame:
    """Candle idêntico repetido `n` vezes — a linha de base contra a qual
    os testes injetam uma única anomalia."""
    idx = weeks_index(n, start)
    return pd.DataFrame(
        {
            "open": [float(open_)] * n,
            "high": [float(high)] * n,
            "low": [float(low)] * n,
            "close": [float(close)] * n,
            "volume": [float(volume)] * n,
        },
        index=idx,
    )


def set_bar(bars: pd.DataFrame, pos: int, **values) -> pd.DataFrame:
    """Substitui campos de um candle específico (pos negativo = do fim)."""
    bars = bars.copy()
    label = bars.index[pos]
    for key, value in values.items():
        bars.loc[label, key] = float(value)
    return bars


@pytest.fixture
def config() -> Config:
    import copy

    return Config(copy.deepcopy(DEFAULTS))


@pytest.fixture
def tmp_cache(tmp_path):
    from src.data.cache import Cache

    with Cache(tmp_path / "test.sqlite") as cache:
        yield cache


def with_metrics(bars, config, benchmark=None):
    """Métricas de R3 sobre uma fixture — é delas que as regras de R5 vivem."""
    from src.metrics import compute_metrics

    return compute_metrics(bars, config, benchmark)


def ranged_bars(n: int = 25, low: float = 10.0, high: float = 12.0,
                close: float = 11.0, volume: float = 100.0, start: str = "2026-01-05"):
    """Candle idêntico repetido: lateralização perfeita entre `low` e `high`.

    Dispersão de fechamentos = 0, então `find_ranges` marca um range de 0 a n-1
    com suporte `low` e resistência `high`. É a tela em branco sobre a qual cada
    teste de evento injeta uma anomalia e mais nada.
    """
    return make_bars(n, open_=close, high=high, low=low, close=close, volume=volume, start=start)


def only_event(events, kind):
    """Os eventos de um tipo, para o teste não depender do que mais foi detectado."""
    return [e for e in events if e.kind == kind]
