"""R7 — invalidação e calendário."""

import datetime as dt

import pandas as pd
import pytest

from src.alerts import (
    check_invalidation,
    collect_alerts,
    distance_to_invalidation,
    levels_configured,
    upcoming_calendar,
)
from src.watchlist import CalendarEvent, Invalidation, WatchItem, Watchlist
from tests.conftest import ranged_bars, set_bar, with_metrics


def item(symbol="PETR4.SA", price=None, direction="below", calendar=()):
    inv = Invalidation(price=price, direction=direction) if price else None
    return WatchItem(symbol=symbol, market="b3", benchmark="^BVSP",
                     invalidation=inv, calendar=tuple(calendar))


def metrics_fechando(closes, config):
    bars = ranged_bars(len(closes))
    for i, c in enumerate(closes):
        bars = set_bar(bars, i, close=c, low=min(c, 10.0), high=max(c, 12.0))
    df = with_metrics(bars, config)
    df["is_partial"] = False
    return df


def test_fechamento_abaixo_do_nivel_alerta(config):
    df = metrics_fechando([11.0] * 24 + [9.0], config)
    alerta = check_invalidation(item(price=10.0), df)
    assert alerta is not None
    assert alerta.is_new and alerta.weeks_violated == 1
    assert alerta.distance_pct == pytest.approx(-0.10)
    assert "9.00" in alerta.summary


def test_fechamento_acima_do_nivel_nao_alerta(config):
    assert check_invalidation(item(price=10.0), metrics_fechando([11.0] * 25, config)) is None


def test_fechamento_exatamente_no_nivel_nao_alerta(config):
    """`below` é estritamente abaixo — encostar não invalida."""
    df = metrics_fechando([11.0] * 24 + [10.0], config)
    assert check_invalidation(item(price=10.0), df) is None


def test_direcao_above(config):
    df = metrics_fechando([11.0] * 24 + [13.0], config)
    assert check_invalidation(item(price=12.0, direction="above"), df) is not None
    assert check_invalidation(item(price=14.0, direction="above"), df) is None


def test_conta_as_semanas_de_violacao(config):
    """Violado há 3 semanas não pode ter o mesmo destaque do violado nesta."""
    df = metrics_fechando([11.0] * 22 + [9.5, 9.2, 9.0], config)
    alerta = check_invalidation(item(price=10.0), df)
    assert alerta.weeks_violated == 3
    assert alerta.is_new is False
    assert "há 3 semanas" in alerta.summary


def test_semana_em_aberto_nao_dispara_alerta(config):
    """A metodologia é semanal: intrassemana que a sexta desfaz não viola nada."""
    df = metrics_fechando([11.0] * 25, config)
    df.loc[df.index[-1], "close"] = 9.0
    df.loc[df.index[-1], "is_partial"] = True
    assert check_invalidation(item(price=10.0), df) is None


def test_papel_sem_nivel_nunca_alerta(config):
    assert check_invalidation(item(price=None), metrics_fechando([5.0] * 25, config)) is None


def test_folga_ate_o_nivel(config):
    assert distance_to_invalidation(item(price=10.0), 11.0) == pytest.approx(0.10)
    assert distance_to_invalidation(item(price=10.0), 9.0) == pytest.approx(-0.10)
    assert distance_to_invalidation(item(price=10.0, direction="above"), 9.0) == pytest.approx(0.10)
    assert distance_to_invalidation(item(price=None), 9.0) is None


def test_calendario_respeita_o_horizonte():
    hoje = dt.date(2026, 9, 7)
    eventos = [
        CalendarEvent(dt.date(2026, 9, 6), "ontem"),
        CalendarEvent(dt.date(2026, 9, 7), "hoje"),
        CalendarEvent(dt.date(2026, 9, 20), "dentro"),
        CalendarEvent(dt.date(2026, 9, 22), "fora"),
    ]
    achados = upcoming_calendar(item(calendar=eventos), hoje, horizon_days=14)
    assert [ev.label for ev, _ in achados] == ["hoje", "dentro"]
    assert [dias for _, dias in achados] == [0, 13]


def test_collect_alerts_poe_os_novos_na_frente(config):
    antigo = metrics_fechando([11.0] * 22 + [9.5, 9.2, 9.0], config)
    novo = metrics_fechando([11.0] * 24 + [9.0], config)
    wl = Watchlist(items=[item("ANTIGO.SA", price=10.0), item("NOVO.SA", price=10.0)])
    alertas, _ = collect_alerts(wl, {"ANTIGO.SA": antigo, "NOVO.SA": novo}, config,
                                dt.date(2026, 9, 7))
    assert [a.symbol for a in alertas] == ["NOVO.SA", "ANTIGO.SA"]


def test_conta_quantos_papeis_tem_nivel():
    wl = Watchlist(items=[item("A.SA", price=10.0), item("B.SA"), item("C.SA")])
    assert levels_configured(wl) == (1, 3)
