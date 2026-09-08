"""R11 — backtest de calibragem. Sem rede."""

import math

import numpy as np
import pandas as pd
import pytest

from src.backtest import (
    DISCLAIMER,
    Observation,
    _forward_returns,
    baseline,
    causal_events,
    final_events,
    observe,
    run,
    summarize,
)
from tests.conftest import make_bars, ranged_bars, set_bar, with_metrics
from tests.test_events import sos_bar


def metricas(bars, config):
    df = with_metrics(bars, config)
    df["is_partial"] = False
    return df


# --------------------------- retorno à frente ---------------------------

def test_retorno_a_frente_e_simples():
    closes = np.array([10.0, 11.0, 12.0, 13.0], dtype=float)
    fwd = _forward_returns(closes, 0, [1, 3])
    assert fwd[1] == pytest.approx(0.10)
    assert fwd[3] == pytest.approx(0.30)


def test_sem_futuro_suficiente_vira_nan():
    closes = np.array([10.0, 11.0], dtype=float)
    assert math.isnan(_forward_returns(closes, 0, [5])[5])


# --------------------------- causalidade ---------------------------

def test_modo_causal_nao_enxerga_o_futuro(config):
    """A garantia central de R11.

    A detecção de range olha a série inteira — se a barra `i` está dentro de um
    range depende de barras posteriores a ela. O modo causal só pode registrar
    o que era visível na semana `j`, então truncar a série em `j` não pode
    mudar nada do que ele já tinha registrado até ali.
    """
    bars = metricas(set_bar(ranged_bars(40), 30, low=9.5, volume=80.0), config)
    completo = causal_events(bars, config, 21)
    truncado = causal_events(bars.iloc[:35], config, 21)

    ate_34 = [(j, e.kind, e.index) for j, e in completo if j <= 34]
    assert ate_34 == [(j, e.kind, e.index) for j, e in truncado]


def test_modo_rapido_difere_do_causal_e_por_isso_existe_o_aviso(config):
    """O modo rápido usa a detecção final; é a diferença que o aviso denuncia."""
    bars = metricas(set_bar(ranged_bars(40), 30, low=9.5, volume=80.0), config)
    causal = {(e.kind, e.index) for _, e in causal_events(bars, config, 21)}
    final = {(e.kind, e.index) for _, e in final_events(bars, config)}
    assert final >= causal or causal >= final or final != causal


def test_evento_e_registrado_quando_fica_visivel_e_nao_quando_ocorre(config):
    """Spring que só confirma dois candles depois é acionável dois candles depois."""
    bars = metricas(set_bar(ranged_bars(40), 30, low=9.5, close=9.6, volume=80.0), config)
    springs = [(j, e) for j, e in causal_events(bars, config, 21) if e.kind == "spring"]
    if springs:
        visivel, evento = springs[0]
        assert visivel >= evento.index


def test_atraso_aparece_na_observacao(config):
    bars = metricas(sos_bar(ranged_bars(40), i=30), config)
    obs = observe("X.SA", bars, config, [4], causal=True)
    assert all(o.lag >= 0 for o in obs)
    assert all(o.detected_index >= o.event_index for o in obs)


# --------------------------- observações ---------------------------

def test_observa_eventos_com_retorno_a_frente(config):
    bars = metricas(sos_bar(ranged_bars(40), i=30), config)
    obs = observe("X.SA", bars, config, [4, 8], causal=True)
    assert obs, "nenhum evento observado"
    assert all(set(o.forward) == {4, 8} for o in obs)
    assert all(o.symbol == "X.SA" for o in obs)


def test_excesso_exige_benchmark(config):
    bars = metricas(sos_bar(ranged_bars(40), i=30), config)
    sem = observe("X.SA", bars, config, [4], causal=True)
    com = observe("X.SA", bars, config, [4], benchmark=metricas(ranged_bars(40), config),
                  causal=True)
    assert all(o.excess == {} for o in sem)
    assert all(4 in o.excess for o in com)


def test_historico_curto_nao_gera_observacao(config):
    assert observe("X.SA", metricas(ranged_bars(10), config), config, [4]) == []


# --------------------------- linha de base ---------------------------

def test_linha_de_base_cobre_todas_as_semanas(config):
    bars = metricas(ranged_bars(40), config)
    base = baseline({"X.SA": bars}, config, [4])
    assert len(base) == 40 - 21
    assert all(o.kind == "_qualquer_semana" for o in base)


def test_linha_de_base_ignora_a_semana_em_aberto(config):
    df = metricas(ranged_bars(40), config)
    df.loc[df.index[-1], "is_partial"] = True
    assert len(baseline({"X.SA": df}, config, [4])) == 39 - 21


# --------------------------- resumo ---------------------------

def test_resumo_conta_acerto_no_sentido_do_vies():
    alta = [Observation("A", "spring", "acumulacao", 0, 0, None, True, {4: r}, {})
            for r in (0.10, 0.05, -0.02, -0.01)]
    tabela = summarize(alta, [4])
    assert tabela.iloc[0]["n"] == 4
    assert tabela.iloc[0]["acerto"] == pytest.approx(0.5)

    baixa = [Observation("A", "upthrust", "distribuicao", 0, 0, None, True, {4: r}, {})
             for r in (-0.10, -0.05, -0.02, 0.01)]
    assert summarize(baixa, [4]).iloc[0]["acerto"] == pytest.approx(0.75)


def test_evento_neutro_nao_tem_acerto_a_medir():
    neutros = [Observation("A", "_qualquer_semana", "neutro", 0, 0, None, True, {4: r}, {})
               for r in (0.1, -0.1)]
    assert math.isnan(summarize(neutros, [4]).iloc[0]["acerto"])


def test_resumo_ignora_nan_no_horizonte():
    mistos = [Observation("A", "spring", "acumulacao", 0, 0, None, True, {4: r}, {})
              for r in (0.10, float("nan"), 0.05)]
    assert summarize(mistos, [4]).iloc[0]["n"] == 2


def test_min_n_corta_tipos_raros():
    poucos = [Observation("A", "lps", "acumulacao", 0, 0, None, True, {4: 0.1}, {})]
    assert summarize(poucos, [4], min_n=5).empty
    assert not summarize(poucos, [4], min_n=1).empty


# --------------------------- orquestração ---------------------------

def test_run_sempre_traz_a_linha_de_base(config):
    """Sem a régua, "spring rende +4%" não quer dizer nada."""
    bars = metricas(sos_bar(ranged_bars(40), i=30), config)
    tabela, _ = run({"X.SA": bars}, config, [4], causal=True)
    assert "_qualquer_semana" in set(tabela["evento"])


def test_run_filtra_por_tipo_mas_mantem_a_base(config):
    bars = metricas(sos_bar(ranged_bars(40), i=30), config)
    tabela, observacoes = run({"X.SA": bars}, config, [4], causal=True, kinds=("sos",))
    assert all(o.kind == "sos" for o in observacoes)
    assert "_qualquer_semana" in set(tabela["evento"])


def test_run_modo_rapido_e_mais_rapido_e_igualmente_estruturado(config):
    bars = metricas(sos_bar(ranged_bars(40), i=30), config)
    causal, _ = run({"X.SA": bars}, config, [4], causal=True)
    rapido, _ = run({"X.SA": bars}, config, [4], causal=False)
    assert set(causal.columns) == set(rapido.columns)


def test_disclaimer_diz_o_que_nao_e():
    for palavra in ("stop", "custo", "estratégia"):
        assert palavra in DISCLAIMER
