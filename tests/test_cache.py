"""R2 — cache SQLite: idempotência, ordenação e o guard de 'já coletado hoje'."""

import datetime as dt

import pandas as pd
import pytest

from tests.conftest import make_bars


def com_partial(bars, partial=False):
    bars = bars.copy()
    bars["is_partial"] = partial
    return bars


def test_upsert_e_leitura_roundtrip(tmp_cache):
    bars = com_partial(make_bars(5))
    assert tmp_cache.upsert_bars("PETR4.SA", bars) == 5
    out = tmp_cache.get_bars("PETR4.SA")
    assert len(out) == 5
    assert out["close"].iloc[-1] == pytest.approx(11.0)
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "trading_days", "is_partial"]


def test_upsert_e_idempotente(tmp_cache):
    bars = com_partial(make_bars(5))
    tmp_cache.upsert_bars("PETR4.SA", bars)
    tmp_cache.upsert_bars("PETR4.SA", bars)
    assert len(tmp_cache.get_bars("PETR4.SA")) == 5


def test_upsert_atualiza_candle_existente(tmp_cache):
    """A semana em formação é reescrita a cada coleta, sem duplicar linha."""
    bars = com_partial(make_bars(3), partial=True)
    tmp_cache.upsert_bars("PETR4.SA", bars)
    bars.loc[bars.index[-1], "close"] = 99.0
    tmp_cache.upsert_bars("PETR4.SA", bars)
    out = tmp_cache.get_bars("PETR4.SA")
    assert len(out) == 3
    assert out["close"].iloc[-1] == pytest.approx(99.0)


def test_ordem_cronologica_e_limite_pega_as_mais_recentes(tmp_cache):
    tmp_cache.upsert_bars("PETR4.SA", com_partial(make_bars(30)))
    out = tmp_cache.get_bars("PETR4.SA", limit_weeks=5)
    assert len(out) == 5
    assert out.index.is_monotonic_increasing
    assert out.index[-1] == pd.Timestamp("2026-01-05") + pd.Timedelta(days=29 * 7)


def test_simbolos_nao_se_misturam(tmp_cache):
    tmp_cache.upsert_bars("PETR4.SA", com_partial(make_bars(5)))
    tmp_cache.upsert_bars("VALE3.SA", com_partial(make_bars(3)))
    assert len(tmp_cache.get_bars("PETR4.SA")) == 5
    assert len(tmp_cache.get_bars("VALE3.SA")) == 3


def test_simbolo_desconhecido_devolve_vazio(tmp_cache):
    assert tmp_cache.get_bars("XXXX").empty


def test_flag_is_partial_persiste(tmp_cache):
    bars = com_partial(make_bars(3), partial=False)
    bars.loc[bars.index[-1], "is_partial"] = True
    tmp_cache.upsert_bars("PETR4.SA", bars)
    out = tmp_cache.get_bars("PETR4.SA")
    assert out["is_partial"].tolist() == [False, False, True]


# --------------------------- proventos ---------------------------

def test_proventos_roundtrip_e_idempotencia(tmp_cache):
    actions = pd.DataFrame([
        {"date": dt.date(2026, 4, 15), "kind": "dividend", "value": 1.25},
        {"date": dt.date(2026, 6, 1), "kind": "split", "value": 2.0},
    ])
    tmp_cache.upsert_actions("PETR4.SA", actions)
    tmp_cache.upsert_actions("PETR4.SA", actions)
    out = tmp_cache.get_actions("PETR4.SA")
    assert len(out) == 2
    assert set(out["kind"]) == {"dividend", "split"}


def test_proventos_filtrados_por_data(tmp_cache):
    actions = pd.DataFrame([
        {"date": dt.date(2026, 1, 10), "kind": "dividend", "value": 1.0},
        {"date": dt.date(2026, 8, 10), "kind": "dividend", "value": 2.0},
    ])
    tmp_cache.upsert_actions("PETR4.SA", actions)
    out = tmp_cache.get_actions("PETR4.SA", since=dt.date(2026, 6, 1))
    assert len(out) == 1 and out["value"].iloc[0] == 2.0


def test_proventos_vazios_nao_quebram(tmp_cache):
    assert tmp_cache.upsert_actions("PETR4.SA", pd.DataFrame()) == 0
    assert tmp_cache.upsert_actions("PETR4.SA", None) == 0


# --------------------------- log de coleta ---------------------------

def test_fetched_today_so_conta_coleta_ok(tmp_cache):
    hoje = dt.datetime(2026, 9, 7, 20, 0)
    assert not tmp_cache.fetched_today("PETR4.SA", hoje.date())
    tmp_cache.record_fetch("PETR4.SA", "error", 0, "timeout", now=hoje)
    assert not tmp_cache.fetched_today("PETR4.SA", hoje.date())
    tmp_cache.record_fetch("PETR4.SA", "ok", 120, now=hoje)
    assert tmp_cache.fetched_today("PETR4.SA", hoje.date())


def test_fetched_today_expira_no_dia_seguinte(tmp_cache):
    hoje = dt.datetime(2026, 9, 7, 20, 0)
    tmp_cache.record_fetch("PETR4.SA", "ok", 120, now=hoje)
    assert not tmp_cache.fetched_today("PETR4.SA", dt.date(2026, 9, 8))


def test_erro_apos_ok_no_mesmo_dia_rebaixa_o_status(tmp_cache):
    hoje = dt.datetime(2026, 9, 7, 20, 0)
    tmp_cache.record_fetch("PETR4.SA", "ok", 120, now=hoje)
    tmp_cache.record_fetch("PETR4.SA", "error", 0, "fonte caiu", now=hoje)
    assert not tmp_cache.fetched_today("PETR4.SA", hoje.date())


def test_cache_persiste_entre_conexoes(tmp_path):
    from src.data.cache import Cache

    with Cache(tmp_path / "c.sqlite") as c:
        c.upsert_bars("PETR4.SA", com_partial(make_bars(4)))
    with Cache(tmp_path / "c.sqlite") as c:
        assert len(c.get_bars("PETR4.SA")) == 4
