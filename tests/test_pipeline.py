"""R2/R3 — orquestração: isolamento de falhas, reuso do cache e export CSV."""

import datetime as dt

import pandas as pd
import pytest

from src.data.provider import DataProvider, FetchError
from src.pipeline import (
    build_metrics,
    export_csv,
    fetch_all,
    mark_partial,
    market_hours_for,
)
from src.watchlist import parse_watchlist
from tests.conftest import make_bars

AGORA = dt.datetime(2026, 9, 7, 20, 0)  # segunda-feira, semana de 31/08 já fechada


class FakeProvider(DataProvider):
    """Fonte controlada: devolve barras sintéticas e falha nos símbolos pedidos."""

    def __init__(self, falham=(), weeks=30):
        self.falham = set(falham)
        self.weeks = weeks
        self.chamadas: list[str] = []
        self.acoes: list[str] = []

    def daily_bars(self, symbol, weeks):
        """A fonte real entrega diário; aqui basta uma barra por semana —
        `aggregate_weekly` monta a semanal em cima do que vier."""
        self.chamadas.append(symbol)
        if symbol in self.falham:
            raise FetchError(f"{symbol}: ticker inexistente")
        # Termina na semana de 31/08/2026, já fechada em relação a AGORA.
        fim = pd.Timestamp("2026-08-31")
        inicio = fim - pd.Timedelta(days=7 * (self.weeks - 1))
        return make_bars(self.weeks, start=inicio.strftime("%Y-%m-%d"))

    def weekly_bars(self, symbol, weeks):
        return self.daily_bars(symbol, weeks)

    def corporate_actions(self, symbol):
        self.acoes.append(symbol)
        return pd.DataFrame(columns=["date", "kind", "value"])


def wl_simples():
    return parse_watchlist({"tickers": [
        {"symbol": "PETR4.SA", "market": "b3"},
        {"symbol": "VALE3.SA", "market": "b3"},
        {"symbol": "BAC", "market": "us"},
    ]})


# --------------------------- coleta ---------------------------

def test_fetch_coleta_papeis_e_benchmarks(config, tmp_cache):
    wl = wl_simples()
    provider = FakeProvider()
    report = fetch_all(wl, config, provider, tmp_cache, now=AGORA)
    assert set(provider.chamadas) == {"PETR4.SA", "VALE3.SA", "BAC", "^BVSP", "^GSPC"}
    assert len(report.ok) == 5 and not report.errors


def test_falha_de_um_ticker_nao_aborta_os_demais(config, tmp_cache):
    """R2: a execução tem que sobreviver a um ticker quebrado."""
    wl = wl_simples()
    report = fetch_all(wl, config, FakeProvider(falham={"VALE3.SA"}), tmp_cache, now=AGORA)
    assert [e.symbol for e in report.errors] == ["VALE3.SA"]
    assert len(report.ok) == 4
    assert "inexistente" in report.errors[0].message
    assert not tmp_cache.get_bars("PETR4.SA").empty


def test_benchmark_quebrado_nao_derruba_a_coleta(config, tmp_cache):
    report = fetch_all(wl_simples(), config, FakeProvider(falham={"^GSPC"}), tmp_cache, now=AGORA)
    assert [e.symbol for e in report.errors] == ["^GSPC"]
    assert len(tmp_cache.get_bars("BAC")) == 30


def test_reexecucao_no_mesmo_dia_usa_o_cache(config, tmp_cache):
    wl = wl_simples()
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    segundo = FakeProvider()
    report = fetch_all(wl, config, segundo, tmp_cache, now=AGORA)
    assert segundo.chamadas == []
    assert all(s.status == "cached" for s in report.statuses)


def test_force_rebaixa_mesmo_no_mesmo_dia(config, tmp_cache):
    wl = wl_simples()
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    segundo = FakeProvider()
    fetch_all(wl, config, segundo, tmp_cache, force=True, now=AGORA)
    assert len(segundo.chamadas) == 5


def test_dia_seguinte_rebaixa_sozinho(config, tmp_cache):
    wl = wl_simples()
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    segundo = FakeProvider()
    fetch_all(wl, config, segundo, tmp_cache, now=AGORA + dt.timedelta(days=1))
    assert len(segundo.chamadas) == 5


def test_refetch_same_day_no_config_desliga_o_cache(config, tmp_cache):
    config.data["data"]["refetch_same_day"] = True
    wl = wl_simples()
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    segundo = FakeProvider()
    fetch_all(wl, config, segundo, tmp_cache, now=AGORA)
    assert len(segundo.chamadas) == 5


# --------------------------- candle em formação ---------------------------

def test_mark_partial_separa_semana_fechada_da_em_formacao(config):
    bars = make_bars(3, start="2026-08-24")   # 24/08, 31/08, 07/09
    hours = market_hours_for(config, "b3")
    out = mark_partial(bars, hours, AGORA)    # AGORA = segunda 07/09 20h
    assert out["is_partial"].tolist() == [False, False, True]


def test_metricas_ignoram_a_semana_em_formacao(config, tmp_cache):
    from src.metrics import latest_row

    wl = parse_watchlist({"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]})

    class ComSemanaAberta(FakeProvider):
        def daily_bars(self, symbol, weeks):
            self.chamadas.append(symbol)
            return make_bars(30, start="2026-02-16")  # termina em 07/09, em formação

    fetch_all(wl, config, ComSemanaAberta(), tmp_cache, now=AGORA)
    metrics, _ = build_metrics(wl, config, tmp_cache)
    df = metrics["PETR4.SA"]
    assert bool(df["is_partial"].iloc[-1]) is True
    assert latest_row(df, closed_only=True).name == pd.Timestamp("2026-08-31")
    assert latest_row(df, closed_only=False).name == pd.Timestamp("2026-09-07")


# --------------------------- métricas + export ---------------------------

def test_build_metrics_produz_colunas_de_forca_relativa(config, tmp_cache):
    wl = wl_simples()
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    metrics, problems = build_metrics(wl, config, tmp_cache)
    assert set(metrics) == {"PETR4.SA", "VALE3.SA", "BAC"}
    assert "rs_12w" in metrics["BAC"].columns
    assert not problems


def test_papel_sem_dados_vira_problema_e_nao_excecao(config, tmp_cache):
    wl = wl_simples()
    fetch_all(wl, config, FakeProvider(falham={"VALE3.SA"}), tmp_cache, now=AGORA)
    metrics, problems = build_metrics(wl, config, tmp_cache)
    assert "VALE3.SA" not in metrics
    assert any(p.symbol == "VALE3.SA" and "sem candles" in p.message for p in problems)


def test_historico_curto_gera_aviso(config, tmp_cache):
    wl = parse_watchlist({"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]})
    fetch_all(wl, config, FakeProvider(weeks=10), tmp_cache, now=AGORA)
    _, problems = build_metrics(wl, config, tmp_cache)
    assert any("histórico curto" in p.message for p in problems)


def test_export_csv_gera_completo_e_resumo(config, tmp_cache, tmp_path):
    config.data["output"]["csv_dir"] = str(tmp_path / "exports")
    wl = wl_simples()
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    metrics, _ = build_metrics(wl, config, tmp_cache)
    full_path, latest_path = export_csv(metrics, wl, config, AGORA)

    full = pd.read_csv(full_path)
    assert set(full["symbol"]) == {"PETR4.SA", "VALE3.SA", "BAC"}
    assert len(full) == 90
    assert {"volume_ratio", "spread_ratio", "close_position", "rs_4w"} <= set(full.columns)

    latest = pd.read_csv(latest_path)
    assert len(latest) == 3
    assert set(latest["week_start"]) == {"2026-08-31"}
    assert set(latest["market"]) == {"b3", "us"}


def test_market_hours_desconhecido_falha_claro(config):
    with pytest.raises(ValueError, match="market_hours"):
        market_hours_for(config, "cripto")


# --------------------------- proventos na janela ---------------------------

def test_flag_ex_dates_marca_a_semana_do_provento(config, tmp_cache):
    from src.pipeline import flag_ex_dates

    wl = parse_watchlist({"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]})
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    metrics, _ = build_metrics(wl, config, tmp_cache)
    base = metrics["PETR4.SA"]

    actions = pd.DataFrame([
        {"date": dt.date(2026, 9, 2), "kind": "dividend", "value": 1.0},   # semana de 31/08
        {"date": dt.date(2026, 8, 26), "kind": "split", "value": 2.0},     # semana de 24/08
    ])
    out = flag_ex_dates(base, actions)
    assert bool(out.loc[pd.Timestamp("2026-08-31"), "ex_dividend"]) is True
    assert bool(out.loc[pd.Timestamp("2026-08-24"), "ex_split"]) is True
    assert bool(out.loc[pd.Timestamp("2026-08-24"), "ex_dividend"]) is False
    assert not out["ex_dividend"].drop(pd.Timestamp("2026-08-31")).any()


def test_flag_ex_dates_ignora_proventos_fora_da_janela(config, tmp_cache):
    from src.pipeline import flag_ex_dates

    wl = parse_watchlist({"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]})
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    base = build_metrics(wl, config, tmp_cache)[0]["PETR4.SA"]
    actions = pd.DataFrame([{"date": dt.date(2019, 1, 3), "kind": "dividend", "value": 1.0}])
    out = flag_ex_dates(base, actions)
    assert not out["ex_dividend"].any()


def test_build_metrics_traz_as_colunas_de_provento(config, tmp_cache):
    wl = wl_simples()
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    metrics, _ = build_metrics(wl, config, tmp_cache)
    assert {"ex_dividend", "ex_split"} <= set(metrics["PETR4.SA"].columns)


def test_trading_days_chega_ate_as_metricas(config, tmp_cache):
    wl = parse_watchlist({"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]})
    fetch_all(wl, config, FakeProvider(), tmp_cache, now=AGORA)
    metrics, _ = build_metrics(wl, config, tmp_cache)
    assert "trading_days" in metrics["PETR4.SA"].columns


def test_proventos_sao_coletados_por_default(config, tmp_cache):
    provider = FakeProvider()
    fetch_all(wl_simples(), config, provider, tmp_cache, now=AGORA)
    assert provider.acoes == provider.chamadas


def test_fetch_actions_falso_poupa_a_requisicao_mais_cara(config, tmp_cache):
    """Proventos custam mais que as 120 semanas de preço (1,21s contra 0,98s).
    Numa varredura de universo isso é metade do tempo por uma anotação que a
    triagem não usa."""
    provider = FakeProvider()
    report = fetch_all(wl_simples(), config, provider, tmp_cache, now=AGORA,
                       fetch_actions=False)
    assert provider.acoes == []
    # E os candles continuam inteiros: o que se pula é a segunda requisição.
    assert provider.chamadas and all(s.status == "ok" for s in report.statuses)
    assert not tmp_cache.get_bars("PETR4.SA").empty
