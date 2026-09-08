"""R4 — detecção de trading range."""

import pandas as pd
import pytest

from src.ranges import (
    active_range,
    close_dispersion,
    find_ranges,
    governing_range,
    levels_before,
    manual_range_span,
    range_at,
    resolve_ranges,
)
from src.watchlist import ManualRange
from tests.conftest import make_bars, ranged_bars, set_bar


def test_dispersao_e_amplitude_sobre_a_media():
    # min 90, max 110, média 100 -> 20/100 = 0,20
    assert close_dispersion(pd.Series([90.0, 100.0, 110.0])) == pytest.approx(0.20)


def test_lateralizacao_perfeita_vira_um_range_unico(config):
    ranges = find_ranges(ranged_bars(20), config)
    assert len(ranges) == 1
    tr = ranges[0]
    assert (tr.start, tr.end, tr.weeks) == (0, 19, 20)
    assert (tr.support, tr.resistance) == (10.0, 12.0)


def test_janela_curta_demais_nao_vira_range(config):
    """min_weeks = 8: sete semanas de lateralização não bastam."""
    assert find_ranges(ranged_bars(7), config) == []
    assert len(find_ranges(ranged_bars(8), config)) == 1


def test_dispersao_no_limite_decide_se_a_barra_entra(config):
    """Fronteira exata do max_close_dispersion_pct (0,12), sem margem.

    Nove semanas a 11,00 e uma décima destoando: a décima entra no range ou
    não conforme a dispersão dos dez fechamentos passe de 12%. O range das
    nove primeiras existe nos dois casos — o que muda é onde ele termina.
    """
    bars = ranged_bars(10)
    dentro = set_bar(bars, -1, close=12.30)   # 1,30 / 11,13 = 0,1168 -> entra
    fora = set_bar(bars, -1, close=12.50)     # 1,50 / 11,15 = 0,1345 -> fica de fora
    assert find_ranges(dentro, config)[0].end == 9
    assert find_ranges(fora, config)[0].end == 8


def test_ranges_nao_se_sobrepoem(config):
    """Uma semana pertence a uma lateralização só."""
    a, b = ranged_bars(12, close=11.0), ranged_bars(12, low=20.0, high=24.0, close=22.0,
                                                    start="2026-04-06")
    bars = pd.concat([a, b])
    ranges = find_ranges(bars, config)
    assert len(ranges) == 2
    assert ranges[0].end < ranges[1].start


def test_range_ativo_so_se_alcanca_a_ultima_barra(config):
    bars = ranged_bars(20)
    ranges = find_ranges(bars, config)
    assert active_range(ranges, len(bars)) is ranges[0]
    assert active_range(ranges, len(bars) + 3) is None  # range parou 3 semanas atrás


def test_levels_before_ignora_a_propria_barra(config):
    """O nível que o spring perfura é o de ANTES dele — senão nada perfura nada."""
    bars = set_bar(ranged_bars(20), 15, low=8.0)
    tr = find_ranges(bars, config)[0]
    assert levels_before(bars, tr, 15) == (10.0, 12.0)   # sem a mínima 8,0
    assert levels_before(bars, tr, 16) == (8.0, 12.0)    # já com ela
    assert levels_before(bars, tr, tr.start) is None     # nada desenhado ainda


def test_governing_range_sobrevive_ao_fim_do_range(config):
    """SOS e LPS acontecem na borda ou logo acima: o range ainda governa."""
    bars = ranged_bars(20)
    ranges = find_ranges(bars, config)
    assert range_at(ranges, 25) is None
    assert governing_range(ranges, 25, max_gap_weeks=8) is ranges[0]   # 6 semanas depois
    assert governing_range(ranges, 30, max_gap_weeks=8) is None        # 11 semanas depois


def test_range_manual_prevalece_sobre_o_detectado(config):
    bars = ranged_bars(20)
    manual = ManualRange(support=9.5, resistance=12.5)
    ranges = resolve_ranges(bars, config, manual)
    assert len(ranges) == 1
    assert (ranges[0].support, ranges[0].resistance) == (9.5, 12.5)
    assert ranges[0].source == "manual"


def test_range_manual_para_onde_o_preco_saiu_da_faixa(config):
    """As 10 primeiras semanas estão longe da faixa manual; o trecho só pega as últimas."""
    longe = make_bars(10, open_=30.0, high=32.0, low=30.0, close=31.0)
    perto = ranged_bars(12, start="2026-03-16")
    bars = pd.concat([longe, perto])
    span = manual_range_span(bars, ManualRange(support=10.0, resistance=12.0), config)
    assert span.start == 10 and span.end == len(bars) - 1


def test_range_manual_fora_do_alcance_nao_vira_range(config):
    bars = ranged_bars(20)
    assert resolve_ranges(bars, config, ManualRange(support=90.0, resistance=95.0)) == []


def test_serie_vazia_nao_explode(config):
    vazio = make_bars(0)
    assert find_ranges(vazio, config) == []
    assert active_range([], 0) is None
