"""R10 — contagem de causa por Ponto & Figura."""


import pandas as pd
import pytest

from src.pnf import (
    AbsoluteScale,
    Column,
    PercentScale,
    build_columns,
    build_scale,
    count_cause,
    widest_row,
)
from src.ranges import find_ranges
from tests.conftest import make_bars, ranged_bars, with_metrics

# --------------------------- grades ---------------------------

def test_grade_absoluta_e_uma_divisao():
    s = AbsoluteScale(1.0)
    assert s.index(10.7) == 10 and s.price(10) == 10.0
    assert s.index(10.0) == 10


def test_grade_geometrica_tem_largura_relativa_constante():
    """2% de box vale 0,20 num papel de R$ 10 e 5,60 num de R$ 280."""
    s = PercentScale(0.02)
    for preco in (10.0, 280.0):
        i = s.index(preco)
        largura = s.price(i + 1) - s.price(i)
        assert largura / s.price(i) == pytest.approx(0.02, rel=1e-9)


def test_grade_geometrica_ida_e_volta():
    s = PercentScale(0.02)
    for preco in (8.0, 47.11, 275.23):
        i = s.index(preco)
        assert s.price(i) <= preco < s.price(i + 1)


def test_grade_geometrica_recusa_preco_nao_positivo():
    with pytest.raises(ValueError):
        PercentScale(0.02).index(0.0)


def test_box_por_atr(config):
    config.data["pnf"]["box_mode"] = "atr"
    config.data["pnf"]["box_atr_fraction"] = 0.25
    assert build_scale(config, atr=4.0).box == pytest.approx(1.0)
    with pytest.raises(ValueError, match="ATR"):
        build_scale(config, atr=None)


def test_box_mode_invalido(config):
    config.data["pnf"]["box_mode"] = "fibonacci"
    with pytest.raises(ValueError, match="box_mode"):
        build_scale(config)


# --------------------------- colunas ---------------------------

def barras(precos):
    """Uma barra por par (mínima, máxima)."""
    n = len(precos)
    df = make_bars(n)
    df["low"] = [p[0] for p in precos]
    df["high"] = [p[1] for p in precos]
    df["open"] = df["low"]
    df["close"] = df["high"]
    return df


def test_alta_continua_estende_a_mesma_coluna():
    cols = build_columns(barras([(10, 11), (10, 12), (10, 13)]), AbsoluteScale(1.0), 3)
    assert len(cols) == 1 and cols[0].direction == "X"
    assert cols[0].high == 13


def test_reversao_exige_o_numero_de_boxes():
    """Com reversão 3, cair 2 boxes não abre coluna nova; cair 3 abre."""
    escala = AbsoluteScale(1.0)
    quase = build_columns(barras([(10, 13), (11, 13)]), escala, 3)   # cai só 2
    assert len(quase) == 1
    vira = build_columns(barras([(10, 13), (9, 13)]), escala, 3)     # cai 4
    assert len(vira) == 2 and vira[1].direction == "O"


def test_serie_vazia_nao_tem_colunas():
    assert build_columns(make_bars(0), AbsoluteScale(1.0), 3) == []


def test_barra_com_preco_nao_positivo_e_ignorada():
    df = barras([(10, 11), (0, 0), (10, 12)])
    assert len(build_columns(df, AbsoluteScale(1.0), 3)) == 1


# --------------------------- linha de contagem ---------------------------

def test_linha_mais_larga_e_onde_mais_colunas_cruzam():
    escala = AbsoluteScale(1.0)
    cols = [Column("X", 10, 15), Column("O", 12, 14), Column("X", 12, 16)]
    tr = find_ranges(ranged_bars(10, low=10.0, high=17.0, close=13.0),
                     type("C", (), {"require": lambda s, k: {"ranges.min_weeks": 8,
                                                             "ranges.max_close_dispersion_pct": 0.5}[k]})())[0]
    # boxes 12, 13 e 14 são cruzados pelas três colunas; o desempate pega o menor
    assert widest_row(cols, escala, tr) == 12


def test_sem_colunas_nao_ha_linha():
    tr = find_ranges(ranged_bars(10),
                     type("C", (), {"require": lambda s, k: {"ranges.min_weeks": 8,
                                                             "ranges.max_close_dispersion_pct": 0.5}[k]})())[0]
    assert widest_row([], AbsoluteScale(1.0), tr) is None


# --------------------------- contagem ---------------------------

def cenario(config, n=30):
    bars = with_metrics(ranged_bars(n, low=10.0, high=12.0, close=11.0), config)
    return bars, find_ranges(bars, config)[0]


def test_range_mais_estreito_que_um_box_vira_coluna_unica(config):
    """Box maior que o range inteiro: nada oscila, uma coluna só."""
    config.data["pnf"]["box_mode"] = "absolute"
    config.data["pnf"]["box_absolute"] = 2.5        # um box cobre o range 10–12 inteiro
    bars, tr = cenario(config)
    c = count_cause(bars, tr, config, "alta")
    assert c.columns == 1 and c.columns_total == 1


def test_janela_vazia_nao_projeta(config):
    """Range apontando para fora das barras: sem insumo, sem alvo."""
    bars, _ = cenario(config)
    vazio = type("TR", (), {"start": 99, "end": 120, "support": 10.0,
                            "resistance": 12.0, "mid": 11.0, "weeks": 22})()
    assert count_cause(bars, vazio, config, "alta") is None


def test_contagem_anda_em_boxes_e_nao_em_reais(config):
    """Regressão: descer N boxes numa grade geométrica é multiplicativo.

    A primeira versão subtraía `N × largura_do_box_na_linha`, medida lá em
    cima, e projetava −66% para a MDLZ. Em índice de box, 33 boxes de 2% abaixo
    de 60,29 dão 60,29 × 1,02⁻³³, não 60,29 − 33 × 1,21.
    """
    config.data["pnf"]["count_line"] = "support"
    escala = PercentScale(0.02)
    bars = with_metrics(barras([(10, 12), (10, 12)] * 15), config)
    tr = type("TR", (), {"start": 0, "end": 29, "support": 10.0, "resistance": 12.0,
                         "mid": 11.0, "weeks": 30})()
    baixa = count_cause(bars, tr, config, "baixa")
    assert baixa is not None
    esperado = escala.price(escala.index(baixa.count_line) - baixa.boxes)
    assert baixa.target == pytest.approx(esperado)
    assert baixa.target > 0                      # nunca negativo
    assert baixa.target < baixa.count_line


def test_alvo_de_alta_e_de_baixa_sao_simetricos_em_boxes(config):
    config.data["pnf"]["count_line"] = "mid"
    bars = with_metrics(barras([(10, 12), (10, 12)] * 15), config)
    tr = type("TR", (), {"start": 0, "end": 29, "support": 10.0, "resistance": 12.0,
                         "mid": 11.0, "weeks": 30})()
    alta = count_cause(bars, tr, config, "alta")
    baixa = count_cause(bars, tr, config, "baixa")
    assert alta.boxes == baixa.boxes
    assert alta.target > alta.count_line > baixa.target


def test_candle_diario_produz_mais_colunas_que_o_semanal(config):
    """O motivo de guardar o diário: a semana esconde as idas e vindas dentro dela.

    Uma semana que sobe, cai, sobe e cai vira UM candle semanal (mínima 10,
    máxima 12) e some com quatro reversões. Nos dados reais de 07/09/2026 a
    diferença foi de 1–5 colunas por range no semanal contra 2–11 no diário — e
    é ela que separa um alvo abaixo do preço atual de uma projeção com sentido.
    """
    semanal = with_metrics(ranged_bars(20, low=10.0, high=12.0, close=11.0), config)
    tr = find_ranges(semanal, config)[0]
    # cinco pregões por semana, oscilando dentro da faixa que a semana resume
    diario = barras([(10.0, 12.0), (10.2, 11.0), (10.0, 11.8), (11.0, 12.0), (10.5, 11.5)] * 20)
    diario.index = pd.date_range(semanal.index[0], periods=100, freq="D")

    so_semanal = count_cause(semanal, tr, config, "alta")
    com_diario = count_cause(semanal, tr, config, "alta", source_bars=diario)
    assert so_semanal.granularity == "semanal"
    assert com_diario.granularity == "diário"
    assert com_diario.columns_total > so_semanal.columns_total


def test_alvo_aquem_do_preco_e_reportado_como_causa_insuficiente(config):
    """Apresentar 19,90 como "alvo" para um papel a 22,52 seria vender passado."""
    config.data["pnf"]["count_line"] = "support"
    bars = with_metrics(barras([(10, 12), (10, 12)] * 15), config)
    tr = type("TR", (), {"start": 0, "end": 29, "support": 10.0, "resistance": 12.0,
                         "mid": 11.0, "weeks": 30})()
    c = count_cause(bars, tr, config, "alta", current=10_000.0)
    assert c.reaches_beyond_price is False
    assert "causa insuficiente" in c.describe()


def test_contagem_carrega_a_conta_aberta(config):
    bars = with_metrics(barras([(10, 12), (10, 12)] * 15), config)
    tr = type("TR", (), {"start": 0, "end": 29, "support": 10.0, "resistance": 12.0,
                         "mid": 11.0, "weeks": 30})()
    c = count_cause(bars, tr, config, "alta", current=11.0)
    linhas = " ".join(c.audit_lines())
    assert "linha de contagem" in linhas and "boxes" in linhas and "insumo" in linhas
    assert str(c.columns) in linhas


def test_desligado_no_config_nao_conta(config):
    from src.analysis import analyze
    from src.pnf import for_analysis
    from src.watchlist import WatchItem

    config.data["pnf"]["enabled"] = False
    df = with_metrics(ranged_bars(25), config)
    df["is_partial"] = False
    a = analyze(WatchItem("PETR4.SA", "b3", "^BVSP"), df, config)
    assert for_analysis(a, config) is None


def test_vies_indefinido_nao_projeta(config):
    """Sem lado declarado não há sentido a projetar."""
    from src.analysis import analyze
    from src.pnf import for_analysis
    from src.watchlist import WatchItem

    df = with_metrics(ranged_bars(25), config)
    df["is_partial"] = False
    a = analyze(WatchItem("PETR4.SA", "b3", "^BVSP"), df, config)
    assert a.phase.bias == "indefinido"
    assert for_analysis(a, config) is None
