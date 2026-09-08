"""Montagem da leitura por papel — o contrato que o relatório consome."""

import pandas as pd

from src.analysis import analyze, analyze_all, week_tag
from src.watchlist import ManualRange, WatchItem, Watchlist
from tests.conftest import ranged_bars, set_bar, with_metrics


def item(symbol="PETR4.SA", manual=None, invalidation=None):
    return WatchItem(symbol=symbol, market="b3", benchmark="^BVSP",
                     manual_range=manual, invalidation=invalidation)


def metrics(bars, config, partial_last=False):
    df = with_metrics(bars, config)
    df["is_partial"] = False
    if partial_last:
        df.loc[df.index[-1], "is_partial"] = True
    return df


def test_leitura_completa_de_um_papel(config):
    bars = set_bar(ranged_bars(25), 22, low=9.5, volume=80.0)
    a = analyze(item(), metrics(bars, config), config)
    assert a.symbol == "PETR4.SA"
    assert a.active_range is not None
    assert a.phase.code == "C_acumulacao"
    assert any(e.kind == "spring" for e in a.events)
    assert a.latest is not None


def test_semana_em_aberto_fica_fora_da_analise(config):
    """O candle em formação tem volume parcial: deixá-lo entrar cria sinal que some na sexta."""
    bars = set_bar(ranged_bars(25), 24, low=9.0, volume=80.0)
    df = metrics(bars, config, partial_last=True)

    aberta = analyze(item(), df, config)
    assert aberta.partial_week is not None
    assert len(aberta.closed) == 24
    assert aberta.latest.name == df.index[-2]
    assert all(e.index < 24 for e in aberta.events)

    df.loc[df.index[-1], "is_partial"] = False    # a semana fecha
    fechada = analyze(item(), df, config)
    assert fechada.partial_week is None
    assert any(e.index == 24 for e in fechada.events)


def test_evento_recente_e_so_a_ultima_semana_fechada(config):
    """`recent_events` alimenta a seção 2 do relatório: só a semana analisada.

    O candle da última semana dispara upthrust *e* esforço × resultado — máxima
    fora da resistência, fechamento de volta e corpo zero com volume 1,9× a
    média são as duas leituras do mesmo candle, e as duas devem aparecer.
    """
    bars = ranged_bars(25)
    bars = set_bar(bars, 20, low=9.5, volume=80.0)     # spring antigo, fora da semana
    bars = set_bar(bars, 24, high=12.5, volume=200.0)  # upthrust na última
    a = analyze(item(), metrics(bars, config), config)
    assert "upthrust" in [e.kind for e in a.recent_events]
    assert "spring" not in [e.kind for e in a.recent_events]
    assert all(e.index == 24 for e in a.recent_events)


def test_range_manual_prevalece_na_analise(config):
    a = analyze(item(manual=ManualRange(support=9.0, resistance=13.0)),
                metrics(ranged_bars(25), config), config)
    assert a.governing_range.source == "manual"
    assert (a.governing_range.support, a.governing_range.resistance) == (9.0, 13.0)


def test_range_manual_fora_de_alcance_vira_aviso(config):
    a = analyze(item(manual=ManualRange(support=90.0, resistance=95.0)),
                metrics(ranged_bars(25), config), config)
    assert a.governing_range is None
    assert any("não encosta" in w for w in a.warnings)


def test_historico_curto_vira_aviso(config):
    a = analyze(item(), metrics(ranged_bars(10), config), config)
    assert any("histórico curto" in w for w in a.warnings)
    assert a.events == []      # sem médias, nenhuma regra dispara


def test_papel_sem_dados_nao_derruba_o_lote(config):
    wl = Watchlist(items=[item("A.SA"), item("B.SA")])
    analyses, missing = analyze_all(wl, {"A.SA": metrics(ranged_bars(25), config)}, config)
    assert [a.symbol for a in analyses] == ["A.SA"]
    assert missing == ["B.SA"]


def test_etiqueta_da_semana_vem_dos_dados(config):
    """Rodar sexta ou segunda produz a mesma etiqueta: manda a semana analisada."""
    import datetime as dt

    a = analyze(item(), metrics(ranged_bars(25), config), config)
    esperado = "{}-{:02d}".format(*a.latest.name.date().isocalendar()[:2])
    assert week_tag([a], dt.date(2030, 1, 1)) == esperado
