"""R3 — cada métrica testada com um caso que dispara e um que quase dispara."""

import math

import pytest

from src.metrics import (
    atr,
    close_position,
    compute_metrics,
    performance,
    relative_strength,
    spread_ratio,
    true_range,
    volume_ratio,
    volume_sma,
)
from tests.conftest import make_bars, set_bar


# --------------------------- volume ---------------------------

def test_volume_ratio_valor_exato():
    # 24 semanas a 100 + 1 semana a 300.
    # SMA20 da última = (19*100 + 300)/20 = 110  ->  300/110 = 2,7272...
    bars = set_bar(make_bars(25, volume=100.0), -1, volume=300.0)
    ratio = volume_ratio(bars["volume"], 20)
    assert ratio.iloc[-1] == pytest.approx(300 / 110)
    assert volume_sma(bars["volume"], 20).iloc[-1] == pytest.approx(110.0)


def test_volume_ratio_e_1_quando_volume_constante():
    bars = make_bars(25, volume=100.0)
    assert volume_ratio(bars["volume"], 20).iloc[-1] == pytest.approx(1.0)


def test_volume_ratio_exige_janela_cheia():
    """19 semanas não bastam para uma média de 20 — NaN, nunca um número torto."""
    bars = make_bars(20, volume=100.0)
    ratio = volume_ratio(bars["volume"], 20)
    assert math.isnan(ratio.iloc[18])   # 19ª semana: janela incompleta
    assert ratio.iloc[19] == pytest.approx(1.0)  # 20ª: primeira válida


def test_volume_ratio_nao_divide_por_zero():
    bars = make_bars(25, volume=0.0)
    assert volume_ratio(bars["volume"], 20).isna().all()


# --------------------------- ATR / spread ---------------------------

def test_true_range_usa_fechamento_anterior():
    bars = make_bars(3, high=12.0, low=10.0, close=11.0)
    bars = set_bar(bars, -1, high=15.0, low=14.0, close=14.5)
    tr = true_range(bars)
    assert tr.iloc[0] == pytest.approx(2.0)          # 1ª barra: high-low
    assert tr.iloc[-1] == pytest.approx(4.0)         # max(1, |15-11|, |14-11|) = 4


def test_atr_sma_e_spread_ratio_valores_exatos():
    # Base: TR = 2,0 em todas as semanas. Última: high 20 / low 10 / close 19.
    # TR_última = max(10, |20-11|, |10-11|) = 10
    # ATR20 = (19*2 + 10)/20 = 2,4   ->  spread_ratio = 10/2,4 = 4,1666...
    bars = set_bar(make_bars(25), -1, high=20.0, low=10.0, close=19.0)
    a = atr(bars, 20, "sma")
    assert a.iloc[-1] == pytest.approx(2.4)
    assert spread_ratio(bars, a).iloc[-1] == pytest.approx(10 / 2.4)


def test_atr_base_constante():
    bars = make_bars(25)
    assert atr(bars, 20, "sma").iloc[-1] == pytest.approx(2.0)
    assert spread_ratio(bars, atr(bars, 20, "sma")).iloc[-1] == pytest.approx(1.0)


def test_atr_wilder_difere_da_sma_no_choque():
    bars = set_bar(make_bars(25), -1, high=20.0, low=10.0, close=19.0)
    wilder = atr(bars, 20, "wilder").iloc[-1]
    # Wilder: 2,0 + (10 - 2,0)/20 = 2,4 nesta configuração de TR constante
    assert wilder == pytest.approx(2.0 + (10.0 - 2.0) / 20)


def test_atr_metodo_invalido_falha_claro():
    with pytest.raises(ValueError, match="atr_method"):
        atr(make_bars(25), 20, "media_movel_exponencial")


# --------------------------- posição do fechamento ---------------------------

@pytest.mark.parametrize("close,esperado", [(10.0, 0.0), (11.0, 0.5), (12.0, 1.0), (11.5, 0.75)])
def test_close_position(close, esperado):
    bars = set_bar(make_bars(3), -1, close=close)
    assert close_position(bars).iloc[-1] == pytest.approx(esperado)


def test_close_position_candle_sem_range():
    bars = set_bar(make_bars(3), -1, high=11.0, low=11.0, close=11.0)
    assert close_position(bars, "neutral").iloc[-1] == pytest.approx(0.5)
    assert math.isnan(close_position(bars, "nan").iloc[-1])


# --------------------------- força relativa ---------------------------

def test_performance_4_semanas():
    bars = make_bars(10)
    bars.loc[bars.index[-1], "close"] = 11.0 * 1.10
    perf = performance(bars["close"], 4)
    assert perf.iloc[-1] == pytest.approx(0.10)


def test_relative_strength_papel_forte_que_o_indice():
    papel = make_bars(10, close=100.0)
    papel.loc[papel.index[-1], "close"] = 110.0     # +10% em 4 semanas
    indice = make_bars(10, close=100.0)
    indice.loc[indice.index[-1], "close"] = 105.0   # +5%
    p, b, diff = relative_strength(papel["close"], indice["close"], 4)
    assert p.iloc[-1] == pytest.approx(0.10)
    assert b.iloc[-1] == pytest.approx(0.05)
    assert diff.iloc[-1] == pytest.approx(0.05)


def test_relative_strength_papel_mais_fraco_da_negativo():
    papel = make_bars(10, close=100.0)
    papel.loc[papel.index[-1], "close"] = 98.0
    indice = make_bars(10, close=100.0)
    indice.loc[indice.index[-1], "close"] = 104.0
    _, _, diff = relative_strength(papel["close"], indice["close"], 4)
    assert diff.iloc[-1] == pytest.approx(-0.06)


def test_relative_strength_ignora_semanas_sem_par_no_indice():
    """B3 e US têm feriados diferentes: data sem par vira NaN, nunca zero."""
    papel = make_bars(10, close=100.0)
    indice = make_bars(10, close=100.0).drop(index=papel.index[-5])
    _, bench, diff = relative_strength(papel["close"], indice["close"], 4)
    assert math.isnan(bench.iloc[-1])
    assert math.isnan(diff.iloc[-1])


# --------------------------- integração das métricas ---------------------------

def test_compute_metrics_gera_todas_as_colunas(config):
    bars = make_bars(30)
    indice = make_bars(30)
    out = compute_metrics(bars, config, indice)
    esperadas = {
        "volume_sma", "volume_ratio", "true_range", "atr", "spread", "spread_ratio",
        "close_position", "weekly_return", "perf_4w", "bench_perf_4w", "rs_4w",
        "perf_12w", "bench_perf_12w", "rs_12w",
    }
    assert esperadas <= set(out.columns)
    assert len(out) == len(bars)


def test_compute_metrics_sem_benchmark_nao_quebra(config):
    out = compute_metrics(make_bars(30), config, None)
    assert "perf_4w" in out.columns
    assert out["rs_4w"].isna().all()


def test_compute_metrics_respeita_janelas_do_config(config):
    """Nada hardcoded: mudar o config muda o resultado."""
    config.data["metrics"]["volume_sma_weeks"] = 5
    bars = set_bar(make_bars(25, volume=100.0), -1, volume=300.0)
    out = compute_metrics(bars, config, None)
    # SMA5 = (4*100 + 300)/5 = 140  ->  300/140
    assert out["volume_ratio"].iloc[-1] == pytest.approx(300 / 140)


def test_compute_metrics_rejeita_dataframe_incompleto(config):
    with pytest.raises(ValueError, match="colunas obrigatórias"):
        compute_metrics(make_bars(25).drop(columns=["volume"]), config, None)
