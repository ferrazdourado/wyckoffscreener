"""R6 — máquina de estados de fase."""

import pandas as pd

from src.events import detect_all
from src.phases import INDEFINIDO, SEM_RANGE, classify, infer_bias
from src.ranges import find_ranges
from tests.conftest import make_bars, ranged_bars, set_bar, with_metrics
from tests.test_events import sos_bar


def fase(bars, config):
    metrics = with_metrics(bars, config)
    ranges = find_ranges(metrics, config)
    return classify(metrics, ranges, detect_all(metrics, ranges, config), config), metrics, ranges


def evento(kind, index, bars, bias):
    """Evento sintético para exercitar a máquina sem depender da detecção."""
    from src.events import Event

    return Event(kind=kind, index=index, date=pd.Timestamp(bars.index[index]).date(),
                 bias=bias, summary=f"{kind} sintético")


def queda(n=20, de=40.0, taxa=0.04, start="2026-01-05"):
    """Tendência de baixa sem lateralização."""
    closes = [de * (1 - taxa) ** i for i in range(n)]
    bars = make_bars(n, start=start)
    bars["close"] = closes
    bars["open"] = [c * 1.02 for c in closes]
    bars["high"] = [c * 1.03 for c in closes]
    bars["low"] = [c * 0.97 for c in closes]
    return bars


def test_sem_candles_e_sem_range(config):
    estado = classify(with_metrics(make_bars(0), config), [], [], config)
    assert estado.code == SEM_RANGE
    assert estado.letter is None


def test_tendencia_pura_nunca_sai_de_sem_range(config):
    estado, _, ranges = fase(queda(25), config)
    assert ranges == []
    assert estado.code == SEM_RANGE
    assert "climax ou lateralização" in estado.pending


def test_lateralizacao_leva_a_fase_b(config):
    estado, _, _ = fase(ranged_bars(25), config)
    assert estado.code.startswith("B_")
    assert estado.letter == "B"


def test_vies_vem_da_tendencia_anterior_ao_range(config):
    """Caiu 30% antes de lateralizar -> candidato a acumulação."""
    bars = pd.concat([queda(12), ranged_bars(15, low=20.0, high=23.0, close=21.5,
                                             start="2026-03-30")])
    metrics = with_metrics(bars, config)
    ranges = find_ranges(metrics, config)
    assert infer_bias(metrics, ranges[-1], config) == "acumulacao"


def test_vies_indefinido_quando_nao_houve_tendencia(config):
    """Sem tendência antes do range, o sistema diz `indefinido` em vez de chutar."""
    bars = ranged_bars(30)
    metrics = with_metrics(bars, config)
    ranges = find_ranges(metrics, config)
    # o range começa na barra 0: não há 8 semanas anteriores para medir
    assert infer_bias(metrics, ranges[0], config) == INDEFINIDO


def test_spring_leva_a_fase_c_com_teste_pendente(config):
    bars = set_bar(ranged_bars(25), 22, low=9.5, volume=80.0)
    estado, _, _ = fase(bars, config)
    assert estado.code == "C_acumulacao"
    assert "teste do spring" in estado.pending


def test_spring_nao_confirmado_pede_confirmacao(config):
    bars = set_bar(ranged_bars(25), 24, low=9.5, close=9.7, volume=80.0)
    estado, _, _ = fase(bars, config)
    assert estado.code == "C_acumulacao"
    assert "confirmação do spring" in estado.pending


def test_sos_leva_a_fase_d_com_lps_pendente(config):
    """SOS na última barra: nada depois dele ainda pôde derrubar a leitura."""
    estado, _, _ = fase(sos_bar(ranged_bars(25), i=24), config)
    assert estado.code == "D_acumulacao"
    assert "LPS" in estado.pending


def test_upthrust_leva_a_fase_c_de_distribuicao(config):
    bars = set_bar(ranged_bars(25), 22, high=12.5, volume=200.0)
    estado, _, _ = fase(bars, config)
    assert estado.code == "C_distribuicao"


def test_rompimento_sustentado_vira_markup(config):
    """Duas semanas seguidas fechando acima da resistência (breakout_confirm_weeks)."""
    bars = ranged_bars(25)
    for i, (lo, hi, c) in enumerate([(12.2, 13.5, 13.0), (12.8, 14.0, 13.8)], start=23):
        bars = set_bar(bars, i, open=12.3, low=lo, high=hi, close=c, volume=150.0)
    estado, _, _ = fase(bars, config)
    assert estado.code == "E_acumulacao"
    assert "markup" in estado.label


def test_um_fechamento_fora_nao_basta_para_markup(config):
    bars = set_bar(ranged_bars(25), 24, open=12.3, low=12.2, high=13.5, close=13.0,
                   volume=150.0)
    estado, _, _ = fase(bars, config)
    assert estado.code != "E_acumulacao"


def test_perda_sustentada_do_suporte_vira_markdown(config):
    bars = ranged_bars(25)
    for i, (lo, hi, c) in enumerate([(8.5, 9.8, 9.0), (7.8, 9.0, 8.2)], start=23):
        bars = set_bar(bars, i, open=9.7, low=lo, high=hi, close=c, volume=150.0)
    estado, _, _ = fase(bars, config)
    assert estado.code == "E_distribuicao"


def test_evento_do_lado_oposto_reabre_a_leitura(config):
    """Regressão: em markdown, um SOS é change of character e tem de valer.

    Aconteceu com BBAS3 em 31/08/2026 — o papel estava em markdown e imprimiu
    um SOS fechando acima da resistência do range. A versão anterior da máquina
    engolia o evento por "não regredir a fase" e o relatório da semana saía
    dizendo markdown no dia do rompimento.
    """
    bars = ranged_bars(30)
    for i, (lo, hi, c) in enumerate([(8.5, 9.8, 9.0), (7.8, 9.0, 8.2)], start=22):
        bars = set_bar(bars, i, open=9.7, low=lo, high=hi, close=c, volume=150.0)
    markdown, _, _ = fase(bars.iloc[:24], config)
    assert markdown.code == "E_distribuicao"

    # Barra larga: o markdown inflou o ATR, então o SOS precisa de amplitude real.
    # Na última barra, para o teste isolar a transição e não a expiração do sinal.
    bars = sos_bar(bars, i=29, high=14.5, low=10.8, close=14.2, volume=300.0)
    estado, metrics, ranges = fase(bars, config)
    assert any(e.kind == "sos" for e in detect_all(metrics, ranges, config))
    assert estado.code == "D_acumulacao"
    assert estado.driver.kind == "sos"


def test_evento_do_mesmo_lado_nao_regride_a_fase(config):
    """Um spring depois de um SOS não devolve a leitura de D para C.

    A máquina é exercitada com uma lista de eventos montada à mão: sintetizar
    preço que produza um SOS e depois um spring no MESMO range é impossível —
    o candle do SOS estoura a dispersão e encerra a lateralização, que é
    justamente o que a Fase D significa. O que está sob teste aqui é a regra de
    transição, não a detecção.
    """
    bars = with_metrics(ranged_bars(25), config)
    ranges = find_ranges(bars, config)
    sos = evento("sos", 22, bars, "acumulacao")
    spring = evento("spring", 23, bars, "acumulacao")
    assert classify(bars, ranges, [sos], config).code == "D_acumulacao"
    assert classify(bars, ranges, [sos, spring], config).code == "D_acumulacao"


def test_evento_do_lado_oposto_reabre_mesmo_vindo_de_fase_adiantada(config):
    """Espelho do teste acima: viés contrário reabre a leitura mesmo regredindo a letra."""
    bars = with_metrics(ranged_bars(25), config)
    ranges = find_ranges(bars, config)
    sos = evento("sos", 22, bars, "acumulacao")
    upthrust = evento("upthrust", 23, bars, "distribuicao")
    assert classify(bars, ranges, [sos, upthrust], config).code == "C_distribuicao"


def test_transicoes_guardam_a_trilha(config):
    bars = set_bar(ranged_bars(25), 22, low=9.5, volume=80.0)
    estado, _, _ = fase(bars, config)
    assert len(estado.transitions) >= 2
    assert estado.transitions[0].code.startswith("B_")
    assert estado.transitions[-1].code == "C_acumulacao"
    assert estado.reason


def test_semanas_na_fase(config):
    bars = set_bar(ranged_bars(25), 22, low=9.5, volume=80.0)
    estado, metrics, _ = fase(bars, config)
    assert estado.weeks_in_phase(len(metrics)) == 3   # barras 22, 23, 24


def test_leitura_de_fase_d_expira_quando_o_nivel_cai(config):
    """Um SOS que o preço devolve não pode deixar o papel em Fase D para sempre.

    Era o caso da VALE3 em 07/09/2026: SOS em dezembro, oito meses de queda de
    volta ao fundo do range e a tabela-resumo ainda dizendo "Fase D — demanda no
    controle". Dois fechamentos abaixo do nível rompido devolvem a leitura à
    Fase B, onde a causa segue em construção.
    """
    bars = sos_bar(ranged_bars(30), i=22)   # rompe 12,00; as barras seguintes voltam a 11,00
    estado, metrics, ranges = fase(bars, config)
    assert any(e.kind == "sos" for e in detect_all(metrics, ranges, config))
    assert estado.code == "B_acumulacao"
    assert "não se sustentou" in estado.reason

    # uma semana só abaixo do nível não basta
    parcial, _, _ = fase(bars.iloc[:24], config)
    assert parcial.code == "D_acumulacao"


def test_expiracao_tambem_vale_para_distribuicao(config):
    """Espelho: um SOW que o preço devolve volta para Fase B de distribuição."""
    bars = ranged_bars(30)
    bars = set_bar(bars, 22, open=11.0, high=11.2, low=8.5, close=8.8, volume=200.0)
    estado, metrics, ranges = fase(bars, config)
    assert any(e.kind == "sow" for e in detect_all(metrics, ranges, config))
    assert estado.code == "B_distribuicao"


def test_spring_que_perde_o_suporte_nao_deixa_o_papel_em_fase_c(config):
    """O spring vive do suporte que ele perfurou e recuperou.

    Para um spring esse suporte é o próprio suporte do range, então perdê-lo
    com confirmação é markdown — a regra de rompimento (passo 3) chega antes da
    de expiração (passo 4) e dá a leitura mais forte das duas. O que o teste
    garante é que a Fase C não sobrevive ao fim do seu próprio nível.
    """
    bars = set_bar(ranged_bars(30), 22, low=9.5, volume=80.0)
    assert fase(bars.iloc[:24], config)[0].code == "C_acumulacao"
    for i in (26, 27):
        bars = set_bar(bars, i, open=9.9, high=10.0, low=8.8, close=9.0, volume=120.0)
    estado, _, _ = fase(bars, config)
    assert estado.code == "E_distribuicao"
