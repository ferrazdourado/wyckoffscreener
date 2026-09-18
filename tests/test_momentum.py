"""Lista curta por momentum. Sem rede: o cache é povoado com séries sintéticas."""

import numpy as np
import pandas as pd
import pytest

from src.momentum import momentum_return, rank_universe
from src.notify import build_message
from src.report import _env, build_model
from src.screener import Universe
from src.watchlist import Invalidation, WatchItem, Watchlist
from tests.conftest import ranged_bars, weeks_index, with_metrics

N = 60  # semanas: sobra acima das 53 que 52/4 exige


def serie(closes, volume=1_000_000.0, start="2025-01-06", partial_last=False) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    bars = pd.DataFrame({
        "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": volume,
    }, index=weeks_index(len(closes), start))
    bars["is_partial"] = False
    if partial_last:
        bars.iloc[-1, bars.columns.get_loc("is_partial")] = True
    return bars


def subida(ganho: float, n: int = N) -> np.ndarray:
    """Série que sobe `ganho` no total, em ritmo constante."""
    return 10.0 * (1.0 + ganho) ** (np.arange(n) / (n - 1))


@pytest.fixture
def universo():
    return Universe("teste", "b3", "^BVSP", ("FORTE.SA", "MEDIO.SA", "FRACO.SA", "CAINDO.SA"))


@pytest.fixture
def povoado(tmp_cache):
    tmp_cache.upsert_bars("^BVSP", serie([10.0] * N))
    tmp_cache.upsert_bars("FORTE.SA", serie(subida(1.0)))
    tmp_cache.upsert_bars("MEDIO.SA", serie(subida(0.5)))
    tmp_cache.upsert_bars("FRACO.SA", serie(subida(0.1)))
    tmp_cache.upsert_bars("CAINDO.SA", serie(subida(-0.3)))
    return tmp_cache


# --------------------------- a conta ---------------------------

def test_momentum_pula_as_ultimas_semanas():
    closes = pd.Series([10.0] * 49 + [20.0] * 4)   # 53 semanas: t-52 = 10, t-4 = 10
    assert momentum_return(closes, 52, 4) == pytest.approx(0.0)
    assert momentum_return(closes, 52, 0) == pytest.approx(1.0)


def test_momentum_sem_historico_suficiente_e_none():
    assert momentum_return(pd.Series([10.0] * 52), 52, 4) is None


def test_momentum_com_janela_invalida_reprova():
    with pytest.raises(ValueError):
        momentum_return(pd.Series([10.0] * 60), 4, 4)


# --------------------------- o ranking ---------------------------

def test_ordena_do_mais_forte_para_o_mais_fraco(config, universo, povoado):
    r = rank_universe(universo, config, povoado)
    assert [p.symbol for p in r.picks] == ["FORTE.SA", "MEDIO.SA", "FRACO.SA", "CAINDO.SA"]
    assert r.picks[0].momentum > r.picks[1].momentum > 0 > r.picks[-1].momentum
    assert r.ranked == 4 and r.week == weeks_index(N, "2025-01-06")[-1].date()


def test_top_corta_depois_de_contar(config, universo, povoado):
    config.data["screener"]["momentum"]["top"] = 2
    r = rank_universe(universo, config, povoado)
    assert [p.symbol for p in r.picks] == ["FORTE.SA", "MEDIO.SA"]
    assert r.ranked == 4


def test_piso_de_liquidez_tira_da_lista(config, universo, povoado):
    povoado.upsert_bars("FORTE.SA", serie(subida(1.0), volume=10.0))
    r = rank_universe(universo, config, povoado)
    assert "FORTE.SA" not in [p.symbol for p in r.picks]
    assert r.illiquid == 1


def test_historico_curto_e_contado(config, universo, povoado):
    povoado.conn.execute("DELETE FROM weekly_bars WHERE symbol = 'MEDIO.SA'")
    povoado.upsert_bars("MEDIO.SA", serie(subida(0.5, n=30), start="2025-07-28"))
    r = rank_universe(universo, config, povoado)
    assert r.short_history == 1 and "MEDIO.SA" not in [p.symbol for p in r.picks]


def test_papel_com_candle_desatualizado_fica_de_fora(config, universo, povoado):
    povoado.conn.execute("DELETE FROM weekly_bars WHERE symbol = 'FORTE.SA'")
    povoado.upsert_bars("FORTE.SA", serie(subida(1.0, n=N - 3)))   # parou 3 semanas antes
    r = rank_universe(universo, config, povoado)
    assert r.outdated == 1 and r.picks[0].symbol == "MEDIO.SA"


def test_semana_em_formacao_nao_entra_na_conta(config, universo, povoado):
    closes = list(subida(0.1)) + [100.0]              # salto só na semana em aberto
    povoado.upsert_bars("FRACO.SA", serie(closes, partial_last=True))
    r = rank_universe(universo, config, povoado)
    fraco = next(p for p in r.picks if p.symbol == "FRACO.SA")
    assert fraco.close == pytest.approx(subida(0.1)[-1])


def test_papel_da_watchlist_entra_marcado(config, universo, povoado):
    r = rank_universe(universo, config, povoado, watchlist={"MEDIO.SA"})
    assert [p.symbol for p in r.picks if p.in_watchlist] == ["MEDIO.SA"]


def test_papel_sem_cache_e_contado(config, povoado):
    u = Universe("teste", "b3", "^BVSP", ("FORTE.SA", "SUMIU.SA"))
    r = rank_universe(u, config, povoado)
    assert r.missing == 1 and r.scanned == 1


def test_forca_relativa_contra_o_indice(config, universo, povoado):
    r = rank_universe(universo, config, povoado)
    assert all(p.rs is not None for p in r.picks)
    assert r.picks[0].rs > 0 > r.picks[-1].rs


# --------------------------- saída ---------------------------

def modelo(config, momentum, invalidation=None):
    bars = ranged_bars(25)
    df = with_metrics(bars, config)
    df["is_partial"] = False
    wl = Watchlist(items=[WatchItem("PETR4.SA", "b3", "^BVSP", invalidation=invalidation)])
    return build_model(wl, {"PETR4.SA": df}, config, pd.Timestamp("2026-09-07 20:00").to_pydatetime(),
                       momentum_results=momentum)


def test_telegram_traz_a_lista_depois_das_invalidacoes(config, universo, povoado):
    r = rank_universe(universo, config, povoado, watchlist={"MEDIO.SA"})
    corpo = build_message(modelo(config, [r], Invalidation(price=20.0, direction="below"))).body
    assert "TOP 4 MOMENTUM — B3" in corpo
    assert "1. FORTE.SA +" in corpo and "MEDIO.SA ★" in corpo
    assert corpo.index("INVALIDAÇÕES") < corpo.index("MOMENTUM") < corpo.index("EVENTOS DA SEMANA")


def test_telegram_sem_momentum_nao_muda(config):
    assert "MOMENTUM" not in build_message(modelo(config, None)).body


def test_relatorio_numera_a_secao_e_empurra_as_seguintes(config, universo, povoado):
    m = modelo(config, [rank_universe(universo, config, povoado)])
    md = _env().get_template("report.md.j2").render(**m)
    assert "## 4. Top por momentum" in md and "## 5. Papel a papel" in md
    assert "## 6. Erros de coleta" in md
    sem = _env().get_template("report.md.j2").render(**modelo(config, None))
    assert "## 4. Papel a papel" in sem and "Top por momentum" not in sem
