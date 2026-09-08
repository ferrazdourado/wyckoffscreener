"""R5 — cada evento com uma fixture que dispara e outra que quase dispara.

A tela em branco é sempre `ranged_bars`: 25 semanas de candle idêntico
(10–12, fecha 11, volume 100). Sobre ela cada teste injeta UMA anomalia, então
o que dispara (ou não) é exatamente o que o teste mexeu.

Índice 22 é o alvo padrão: já passou do warm-up de 20 semanas das médias e
sobram duas barras para a janela de recuperação do spring.
"""

import pandas as pd
import pytest

from src.events import (
    detect_all,
    detect_climax,
    detect_effort_vs_result,
    detect_lps,
    detect_sos,
    detect_sow,
    detect_springs,
    detect_tests,
    detect_upthrusts,
)
from src.ranges import find_ranges
from tests.conftest import make_bars, only_event, ranged_bars, set_bar, with_metrics

ALVO = 22


def prep(bars, config):
    """(métricas, ranges) — o par que toda função de detecção recebe."""
    metrics = with_metrics(bars, config)
    return metrics, find_ranges(metrics, config)


# ----------------------------- spring -----------------------------

def test_spring_dispara(config):
    """Perfura o suporte 10,00, volume 0,81× a média, fecha de volta em 11,00."""
    bars = set_bar(ranged_bars(25), ALVO, low=9.5, volume=80.0)
    metrics, ranges = prep(bars, config)
    springs = detect_springs(metrics, ranges, config)
    assert len(springs) == 1
    ev = springs[0]
    assert ev.index == ALVO and ev.confirmed
    assert ev.refs["support"] == pytest.approx(10.0)
    assert ev.numbers["volume/média"] == pytest.approx(80 / 99, rel=1e-6)


def test_spring_nao_dispara_se_so_encosta_no_suporte(config):
    """Mínima 10,00 = suporte: encostar não é perfurar."""
    bars = set_bar(ranged_bars(25), ALVO, low=10.0, volume=80.0)
    metrics, ranges = prep(bars, config)
    assert detect_springs(metrics, ranges, config) == []


def test_spring_nao_dispara_com_volume_alto(config):
    """Perfuração idêntica, volume 1,53× a média: é rompimento, não spring."""
    bars = set_bar(ranged_bars(25), ALVO, low=9.5, volume=160.0)
    metrics, ranges = prep(bars, config)
    assert detect_springs(metrics, ranges, config) == []


def test_spring_nao_dispara_se_o_fechamento_nao_volta(config):
    """Perfura, fecha abaixo do suporte e continua abaixo: perdeu o suporte."""
    bars = ranged_bars(25)
    bars = set_bar(bars, ALVO, low=9.5, close=9.6, volume=80.0)
    bars = set_bar(bars, 23, high=9.9, low=9.0, close=9.4)
    bars = set_bar(bars, 24, high=9.9, low=9.0, close=9.3)
    metrics, ranges = prep(bars, config)
    assert detect_springs(metrics, ranges, config) == []


def test_spring_da_ultima_semana_sai_como_nao_confirmado(config):
    """A janela de recuperação ainda não fechou — o sinal mais novo não pode sumir."""
    bars = set_bar(ranged_bars(25), 24, low=9.5, close=9.7, volume=80.0)
    metrics, ranges = prep(bars, config)
    springs = detect_springs(metrics, ranges, config)
    assert len(springs) == 1
    assert springs[0].confirmed is False
    assert "aguardando" in springs[0].summary


def test_spring_exige_range_ativo(config):
    """Sem lateralização não há suporte de range para perfurar (contexto de fase)."""
    bars = make_bars(25, open_=20.0, high=22.0, low=18.0, close=19.0)
    bars["close"] = [30.0 - i for i in range(25)]  # tendência de baixa, sem range
    metrics, ranges = prep(bars, config)
    assert ranges == []
    assert detect_springs(metrics, ranges, config) == []


def test_spring_exige_nivel_ja_desenhado(config):
    """Nas primeiras semanas do range o suporte ainda não existe como linha."""
    bars = set_bar(ranged_bars(25), 21, low=9.5, volume=80.0)
    metrics, ranges = prep(bars, config)
    # min_weeks//2 = 4 barras de range antes da candidata; aqui há 21, então passa.
    assert len(detect_springs(metrics, ranges, config)) == 1
    config.data["ranges"]["min_weeks"] = 44  # exige 22 barras antes -> 21 não bastam
    assert detect_springs(metrics, find_ranges(metrics, config), config) == []


# ----------------------------- teste do spring -----------------------------

def test_teste_dispara_depois_do_spring(config):
    bars = ranged_bars(25)
    bars = set_bar(bars, 20, low=9.5, volume=80.0)      # spring
    bars = set_bar(bars, 22, low=9.8, volume=60.0)      # teste: mínima acima, volume menor
    metrics, ranges = prep(bars, config)
    springs = detect_springs(metrics, ranges, config)
    testes = detect_tests(metrics, springs, config)
    assert len(testes) == 1
    assert testes[0].index == 22
    assert testes[0].refs["spring_index"] == 20


def test_teste_nao_dispara_com_volume_acima_do_spring(config):
    bars = ranged_bars(25)
    bars = set_bar(bars, 20, low=9.5, volume=80.0)
    bars = set_bar(bars, 22, low=9.8, volume=90.0)      # volume MAIOR que o do spring
    metrics, ranges = prep(bars, config)
    springs = detect_springs(metrics, ranges, config)
    assert detect_tests(metrics, springs, config) == []


def test_teste_nao_dispara_se_afunda_mais_que_o_spring(config):
    bars = ranged_bars(25)
    bars = set_bar(bars, 20, low=9.5, volume=80.0)
    bars = set_bar(bars, 22, low=9.4, volume=60.0)      # mínima ABAIXO da do spring
    metrics, ranges = prep(bars, config)
    springs = detect_springs(metrics, ranges, config)
    assert detect_tests(metrics, springs, config) == []


def test_teste_nao_dispara_longe_do_suporte(config):
    """Semana fraca lá em cima no range não é teste — é semana fraca."""
    config.data["events"]["test"]["max_distance_to_spring_low_atr"] = 0.1
    bars = ranged_bars(25)
    bars = set_bar(bars, 20, low=9.5, volume=80.0)
    bars = set_bar(bars, 22, low=11.5, high=12.0, close=11.8, volume=60.0)
    metrics, ranges = prep(bars, config)
    springs = detect_springs(metrics, ranges, config)
    assert detect_tests(metrics, springs, config) == []


# ----------------------------- SOS -----------------------------

def sos_bar(bars, i=ALVO, volume=200.0, high=13.5, low=10.8, close=13.2):
    return set_bar(bars, i, open=11.0, high=high, low=low, close=close, volume=volume)


def test_sos_dispara(config):
    """spread 1,33× ATR, volume 1,90× a média, fecha a 89% do candle acima de 12,00."""
    metrics, ranges = prep(sos_bar(ranged_bars(25)), config)
    eventos = detect_sos(metrics, ranges, config)
    assert len(eventos) == 1
    ev = eventos[0]
    assert ev.index == ALVO
    assert ev.numbers["spread/ATR"] >= 1.3
    assert ev.numbers["volume/média"] >= 1.5
    assert ev.refs["crossed_range"] is True


def test_sos_nao_dispara_sem_volume(config):
    """Mesmo candle, volume 1,19× a média: rompimento sem participação."""
    metrics, ranges = prep(sos_bar(ranged_bars(25), volume=120.0), config)
    assert detect_sos(metrics, ranges, config) == []


def test_sos_nao_dispara_com_fechamento_fraco(config):
    """Rompe no intradiário e devolve: fecha a 26% do candle, fora do terço superior."""
    metrics, ranges = prep(sos_bar(ranged_bars(25), close=11.5), config)
    assert detect_sos(metrics, ranges, config) == []


def test_sos_nao_dispara_sem_romper_a_resistencia_interna(config):
    """Uma máxima de 14,00 duas semanas antes eleva a resistência interna acima do fechamento."""
    bars = set_bar(ranged_bars(25), 20, high=14.0)
    metrics, ranges = prep(sos_bar(bars), config)
    assert detect_sos(metrics, ranges, config) == []


def test_sos_nao_dispara_com_spread_estreito(config):
    """Volume e fechamento certos, mas candle de 0,54× ATR: esforço sem amplitude."""
    metrics, ranges = prep(sos_bar(ranged_bars(25), high=13.5, low=12.4, close=13.4), config)
    assert detect_sos(metrics, ranges, config) == []


# ----------------------------- SOW -----------------------------

def test_sow_dispara(config):
    """Espelho do SOS: perde o suporte interno fechando a 11% do candle."""
    bars = set_bar(ranged_bars(25), ALVO, open=11.0, high=11.2, low=8.5, close=8.8, volume=200.0)
    metrics, ranges = prep(bars, config)
    eventos = detect_sow(metrics, ranges, config)
    assert len(eventos) == 1
    assert eventos[0].bias == "distribuicao"
    assert eventos[0].refs["crossed_range"] is True


def test_sow_nao_dispara_com_fechamento_forte(config):
    """Afunda e recupera: fecha a 89% do candle, fora do terço inferior."""
    bars = set_bar(ranged_bars(25), ALVO, open=11.0, high=11.2, low=8.5, close=10.9, volume=200.0)
    metrics, ranges = prep(bars, config)
    assert detect_sow(metrics, ranges, config) == []


# ----------------------------- LPS -----------------------------

def test_lps_dispara_apos_sos(config):
    """Duas semanas de volume caindo, seguradas acima do nível rompido (12,00)."""
    bars = sos_bar(ranged_bars(25), i=20)
    bars = set_bar(bars, 21, low=12.4, high=13.4, close=12.9, volume=150.0)
    bars = set_bar(bars, 22, low=12.2, high=13.0, close=12.6, volume=110.0)
    metrics, ranges = prep(bars, config)
    sos = detect_sos(metrics, ranges, config)
    eventos = detect_lps(metrics, sos, config)
    assert len(eventos) == 1
    assert eventos[0].index == 22          # marcado no fim da sequência
    assert eventos[0].numbers["semanas de volume decrescente"] == 2


def test_lps_nao_dispara_com_volume_subindo(config):
    bars = sos_bar(ranged_bars(25), i=20)
    bars = set_bar(bars, 21, low=12.4, high=13.4, close=12.9, volume=150.0)
    bars = set_bar(bars, 22, low=12.2, high=13.0, close=12.6, volume=180.0)  # sobe
    metrics, ranges = prep(bars, config)
    sos = detect_sos(metrics, ranges, config)
    assert detect_lps(metrics, sos, config) == []


def test_lps_nao_dispara_se_perde_o_nivel_rompido(config):
    """Recuo com volume secando, mas devolve tudo: não é last point of SUPPORT."""
    bars = sos_bar(ranged_bars(25), i=20)
    bars = set_bar(bars, 21, low=11.0, high=13.0, close=11.5, volume=150.0)
    bars = set_bar(bars, 22, low=10.5, high=12.0, close=11.0, volume=110.0)
    metrics, ranges = prep(bars, config)
    sos = detect_sos(metrics, ranges, config)
    assert detect_lps(metrics, sos, config) == []


# ----------------------------- upthrust -----------------------------

def test_upthrust_dispara(config):
    """Máxima 12,5 perfura a resistência 12,0, fecha em 11,0 lá dentro, volume 1,90×."""
    bars = set_bar(ranged_bars(25), ALVO, high=12.5, volume=200.0)
    metrics, ranges = prep(bars, config)
    eventos = detect_upthrusts(metrics, ranges, config)
    assert len(eventos) == 1
    assert eventos[0].bias == "distribuicao"
    assert eventos[0].refs["utad"] is True   # range de 23 semanas >= 12


def test_upthrust_nao_dispara_com_volume_baixo(config):
    bars = set_bar(ranged_bars(25), ALVO, high=12.5, volume=100.0)
    metrics, ranges = prep(bars, config)
    assert detect_upthrusts(metrics, ranges, config) == []


def test_upthrust_nao_dispara_se_o_fechamento_segura_fora(config):
    """Fecha ACIMA da resistência: é rompimento, não upthrust."""
    bars = set_bar(ranged_bars(25), ALVO, high=12.5, close=12.4, volume=200.0)
    metrics, ranges = prep(bars, config)
    assert detect_upthrusts(metrics, ranges, config) == []


def test_upthrust_em_range_novo_nao_e_utad(config):
    config.data["events"]["upthrust"]["utad_min_range_weeks"] = 40
    bars = set_bar(ranged_bars(25), ALVO, high=12.5, volume=200.0)
    metrics, ranges = prep(bars, config)
    assert detect_upthrusts(metrics, ranges, config)[0].refs["utad"] is False


# ----------------------------- climax -----------------------------

def climax_bars(n=30, queda=0.04):
    """Tendência de baixa constante, sem lateralização — o contexto que a Fase A exige."""
    closes = [40.0 * (1 - queda) ** i for i in range(n)]
    bars = make_bars(n)
    bars["close"] = closes
    bars["open"] = [c * 1.02 for c in closes]
    bars["high"] = [c * 1.03 for c in closes]
    bars["low"] = [c * 0.98 for c in closes]
    return bars


def test_selling_climax_dispara(config):
    bars = climax_bars()
    i = len(bars) - 1
    fech = float(bars["close"].iloc[i])
    bars = set_bar(bars, i, open=fech * 1.25, high=fech * 1.26, low=fech * 0.97,
                   close=fech, volume=300.0)
    metrics, ranges = prep(bars, config)
    eventos = only_event(detect_climax(metrics, ranges, config), "selling_climax")
    assert len(eventos) == 1
    assert eventos[0].numbers["volume/média"] >= 2.0
    assert eventos[0].numbers["spread/ATR"] >= 1.5


def test_selling_climax_nao_dispara_sem_tendencia_de_baixa(config):
    """Mesmo candle climático numa lateralização: aí é spring ou absorção, não SC."""
    bars = ranged_bars(30)
    bars = set_bar(bars, 29, open=11.0, high=11.2, low=7.0, close=7.5, volume=300.0)
    metrics, ranges = prep(bars, config)
    assert only_event(detect_climax(metrics, ranges, config), "selling_climax") == []


def test_selling_climax_nao_dispara_sem_volume(config):
    bars = climax_bars()
    i = len(bars) - 1
    fech = float(bars["close"].iloc[i])
    bars = set_bar(bars, i, open=fech * 1.25, high=fech * 1.26, low=fech * 0.97,
                   close=fech, volume=150.0)   # 1,58× -> abaixo de 2,0×
    metrics, ranges = prep(bars, config)
    assert only_event(detect_climax(metrics, ranges, config), "selling_climax") == []


def test_climax_nao_dispara_no_fundo_de_um_range(config):
    """Contexto de fase: climax abre a Fase A; no meio do range é outra coisa."""
    bars = ranged_bars(30)
    bars = set_bar(bars, 29, open=11.0, high=15.0, low=10.9, close=14.8, volume=400.0)
    metrics, ranges = prep(bars, config)
    assert detect_climax(metrics, ranges, config) == []


# ----------------------------- esforço x resultado -----------------------------

def test_esforco_sem_resultado_dispara(config):
    """Volume 1,90× a média e corpo zero: alguém absorveu."""
    bars = set_bar(ranged_bars(25), ALVO, open=11.0, close=11.0, volume=200.0)
    metrics, ranges = prep(bars, config)
    eventos = detect_effort_vs_result(metrics, ranges, config)
    assert len(eventos) == 1
    assert eventos[0].numbers["progresso do preço (corpo/ATR)"] == pytest.approx(0.0)


def test_esforco_com_resultado_nao_dispara(config):
    """Mesmo volume, mas o preço andou 0,95 ATR: esforço com resultado é normal."""
    bars = set_bar(ranged_bars(25), ALVO, open=10.2, high=12.2, low=10.1, close=12.1,
                   volume=200.0)
    metrics, ranges = prep(bars, config)
    assert detect_effort_vs_result(metrics, ranges, config) == []


def test_esforco_no_topo_do_range_le_distribuicao(config):
    bars = set_bar(ranged_bars(25), ALVO, open=11.9, high=12.0, low=11.8, close=11.9,
                   volume=200.0)
    metrics, ranges = prep(bars, config)
    eventos = detect_effort_vs_result(metrics, ranges, config)
    assert eventos[0].bias == "distribuicao"
    assert "distribuição" in eventos[0].summary


# ----------------------------- transversais -----------------------------

def test_nada_dispara_durante_o_warmup_das_medias(config):
    """Sem volume_ratio nem ATR (janela de 20 semanas incompleta), nenhuma regra vale."""
    bars = set_bar(ranged_bars(15), 12, low=9.0, high=13.0, close=12.9, volume=500.0)
    metrics, ranges = prep(bars, config)
    assert detect_all(metrics, ranges, config) == []


def test_serie_vazia_nao_explode(config):
    vazio = with_metrics(make_bars(0), config)
    assert detect_all(vazio, [], config) == []


def test_eventos_saem_em_ordem_cronologica(config):
    bars = ranged_bars(30)
    bars = set_bar(bars, 21, low=9.5, volume=80.0)
    bars = set_bar(bars, 23, low=9.8, volume=60.0)
    bars = sos_bar(bars, i=26)
    metrics, ranges = prep(bars, config)
    eventos = detect_all(metrics, ranges, config)
    assert [e.index for e in eventos] == sorted(e.index for e in eventos)


def test_todo_evento_carrega_os_numeros_que_o_dispararam(config):
    """Princípio 3 da spec: nenhum sinal sai sem a conta aberta."""
    bars = ranged_bars(30)
    bars = set_bar(bars, 21, low=9.5, volume=80.0)
    bars = sos_bar(bars, i=26)
    metrics, ranges = prep(bars, config)
    for evento in detect_all(metrics, ranges, config):
        assert evento.checks, f"{evento.kind} saiu sem checks"
        assert all(c.passed() for c in evento.checks), f"{evento.kind} tem check reprovado"
        assert evento.summary and evento.audit_lines()
