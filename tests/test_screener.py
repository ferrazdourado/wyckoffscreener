"""R12 — universo, varredura e ranking. Sem rede."""

import pytest

from src.screener import (
    Universe,
    UniverseError,
    check_universe,
    load_universes,
    parse_universes,
    refresh,
    screen,
    to_frame,
)
from tests.conftest import ranged_bars, set_bar
from tests.test_events import sos_bar


# --------------------------- universo ---------------------------

def bloco(market="b3", benchmark="^BVSP", tickers=("PETR4.SA", "VALE3.SA")):
    return {"universes": {"teste": {"market": market, "benchmark": benchmark,
                                    "tickers": list(tickers)}}}


def test_universo_valido():
    u = parse_universes(bloco())["teste"]
    assert u.tickers == ("PETR4.SA", "VALE3.SA")
    assert u.market == "b3" and u.benchmark == "^BVSP"


def test_universo_vira_watchlist_com_o_benchmark_do_bloco():
    wl = parse_universes(bloco())["teste"].as_watchlist()
    assert [i.symbol for i in wl] == ["PETR4.SA", "VALE3.SA"]
    assert {i.benchmark for i in wl} == {"^BVSP"}


def test_simbolo_normalizado():
    u = parse_universes(bloco(tickers=[" petr4.sa "]))["teste"]
    assert u.tickers == ("PETR4.SA",)


def test_chave_universes_ausente():
    with pytest.raises(UniverseError, match="`universes` ausente"):
        parse_universes({"tickers": []})


def test_mercado_invalido():
    with pytest.raises(UniverseError, match="market"):
        parse_universes(bloco(market="bovespa"))


def test_sufixo_sa_conferido_nos_dois_sentidos():
    with pytest.raises(UniverseError, match=r"sufixo `\.SA`"):
        parse_universes(bloco(tickers=["PETR4"]))
    with pytest.raises(UniverseError, match=r"sufixo `\.SA`"):
        parse_universes(bloco(market="us", benchmark="^GSPC", tickers=["AAPL.SA"]))


def test_duplicata_reprova():
    with pytest.raises(UniverseError, match="duplicado"):
        parse_universes(bloco(tickers=["PETR4.SA", "PETR4.SA"]))


def test_todos_os_erros_numa_passada():
    """Como a watchlist de R1: o usuário corrige tudo de uma vez."""
    raw = {"universes": {"a": {"market": "xx", "benchmark": "^BVSP", "tickers": ["X.SA"]},
                         "b": {"market": "b3", "benchmark": "", "tickers": ["Y.SA"]},
                         "c": {"market": "b3", "benchmark": "^BVSP", "tickers": ["SEM_SUFIXO"]}}}
    with pytest.raises(UniverseError) as erro:
        parse_universes(raw)
    assert "3 erro(s)" in str(erro.value)


def test_arquivo_do_projeto_e_valido():
    """O universe.yaml versionado precisa passar na própria validação."""
    universos = load_universes("universe.yaml")
    assert "b3_liquidas" in universos and "us_large" in universos
    assert all(t.endswith(".SA") for t in universos["b3_liquidas"].tickers)
    assert not any(t.endswith(".SA") for t in universos["us_large"].tickers)


# --------------------------- varredura ---------------------------

def popular(cache, symbol, bars):
    bars = bars.copy()
    bars["is_partial"] = False
    cache.upsert_bars(symbol, bars)


@pytest.fixture(autouse=True)
def sem_piso_de_liquidez(config):
    """As fixtures sintéticas negociam R$ 1.000 por semana — abaixo de qualquer
    piso realista. Os testes de fase/ordenação desligam o filtro; os testes de
    liquidez ligam de volta, explicitamente."""
    config.data["screener"]["min_weekly_volume"] = 0


@pytest.fixture
def universo():
    return Universe("teste", "b3", "^BVSP",
                    ("FASE_D.SA", "FASE_C.SA", "FASE_B.SA", "CURTO.SA"))


@pytest.fixture
def cache_povoado(tmp_cache, universo):
    popular(tmp_cache, "^BVSP", ranged_bars(25))
    popular(tmp_cache, "FASE_D.SA", sos_bar(ranged_bars(25), i=24))          # SOS na última
    popular(tmp_cache, "FASE_C.SA", set_bar(ranged_bars(25), 24, low=9.5, volume=80.0))
    popular(tmp_cache, "FASE_B.SA", ranged_bars(25))                          # só lateraliza
    popular(tmp_cache, "CURTO.SA", ranged_bars(10))                           # histórico curto
    return tmp_cache


def test_varredura_filtra_pelas_fases_pedidas(config, universo, cache_povoado):
    r = screen(universo, config, cache_povoado, phases=("C", "D"))
    assert [c.symbol for c in r.candidates] == ["FASE_D.SA", "FASE_C.SA"]
    assert r.scanned == 3          # CURTO.SA ficou de fora por histórico


def test_fase_b_entra_se_voce_pedir(config, universo, cache_povoado):
    r = screen(universo, config, cache_povoado, phases=("B", "C", "D"))
    assert "FASE_B.SA" in [c.symbol for c in r.candidates]


def test_fase_d_vem_antes_de_fase_c(config, universo, cache_povoado):
    r = screen(universo, config, cache_povoado, phases=("C", "D"))
    assert r.candidates[0].phase_rank > r.candidates[1].phase_rank


def test_historico_curto_vira_problema_e_nao_excecao(config, universo, cache_povoado):
    r = screen(universo, config, cache_povoado)
    assert [p.symbol for p in r.problems] == ["CURTO.SA"]
    assert "histórico curto" in r.problems[0].message


def test_papel_sem_cache_vira_problema(config, tmp_cache):
    popular(tmp_cache, "^BVSP", ranged_bars(25))
    u = Universe("t", "b3", "^BVSP", ("NAOTEM.SA",))
    r = screen(u, config, tmp_cache)
    assert r.candidates == [] and r.problems[0].symbol == "NAOTEM.SA"


def test_limite_corta_a_lista(config, universo, cache_povoado):
    r = screen(universo, config, cache_povoado, phases=("B", "C", "D"), limit=1)
    assert len(r.candidates) == 1


def test_evento_recente_segue_o_vies_da_fase(config, tmp_cache):
    """Um upthrust não pode recomendar um candidato a acumulação."""
    popular(tmp_cache, "^BVSP", ranged_bars(30))
    bars = set_bar(ranged_bars(30), 20, low=9.5, volume=80.0)     # spring (acumulação)
    bars = set_bar(bars, 28, high=12.5, volume=200.0)             # upthrust (distribuição)
    popular(tmp_cache, "MISTO.SA", bars)
    u = Universe("t", "b3", "^BVSP", ("MISTO.SA",))
    r = screen(u, config, tmp_cache, phases=("B", "C", "D"))
    candidato = r.candidates[0]
    assert candidato.last_event.bias == candidato.analysis.phase.bias


def test_tabela_traz_as_colunas_que_justificam_a_ordem(config, universo, cache_povoado):
    frame = to_frame(screen(universo, config, cache_povoado), config)
    for coluna in ("posicao", "symbol", "fase", "semanas_desde_evento",
                   "evento_da_fase", "rs_12w", "volume_ratio", "proximo_esperado"):
        assert coluna in frame.columns
    assert list(frame["posicao"]) == [1, 2]


def test_tabela_vazia_nao_quebra(config, tmp_cache):
    popular(tmp_cache, "^BVSP", ranged_bars(25))
    popular(tmp_cache, "SOB.SA", ranged_bars(25))
    u = Universe("t", "b3", "^BVSP", ("SOB.SA",))
    assert to_frame(screen(u, config, tmp_cache, phases=("D",)), config).empty


# --------------------------- conferência de tickers ---------------------------

def test_check_universe_separa_vivos_de_mortos():
    class FonteFalsa:
        def weekly_bars(self, symbol, weeks):
            if symbol == "MORTO.SA":
                raise RuntimeError("deslistado")
            if symbol == "VAZIO.SA":
                return ranged_bars(0)
            return ranged_bars(weeks)

        def corporate_actions(self, symbol):
            raise NotImplementedError

    u = Universe("t", "b3", "^BVSP", ("VIVO.SA", "MORTO.SA", "VAZIO.SA"))
    vivos, mortos = check_universe(u, FonteFalsa())
    assert vivos == ["VIVO.SA"]
    assert [s for s, _ in mortos] == ["MORTO.SA", "VAZIO.SA"]
    assert "deslistado" in mortos[0][1]


# --------------------------- liquidez e exclusão (universo amplo) ---------------------------

def test_liquidez_usa_mediana_e_nao_media():
    """Uma semana de leilão não pode promover papel que não negocia no resto."""
    import pandas as pd

    from src.screener import weekly_liquidity

    metrics = pd.DataFrame({
        "close": [10.0] * 12,
        "volume": [100.0] * 11 + [1_000_000.0],
    })
    assert weekly_liquidity(metrics) == pytest.approx(1000.0)


def test_liquidez_de_serie_vazia_e_zero():
    import pandas as pd

    from src.screener import weekly_liquidity

    assert weekly_liquidity(pd.DataFrame()) == 0.0


def test_papel_abaixo_do_piso_de_liquidez_sai_da_lista(config, universo, cache_povoado):
    """Num universo amplo é o que impede a lista de encher de papel intradável."""
    config.data["screener"]["min_weekly_volume"] = 10**12
    resultado = screen(universo, config, cache_povoado)
    assert resultado.candidates == []
    assert resultado.illiquid > 0


def test_piso_zero_desliga_o_filtro(config, universo, cache_povoado):
    config.data["screener"]["min_weekly_volume"] = 0
    assert screen(universo, config, cache_povoado).illiquid == 0


def test_papel_da_watchlist_nao_volta_como_candidato(config, universo, cache_povoado):
    """A seção chama-se "fora da watchlist": repetir o que já está no relatório
    gastaria as primeiras linhas, que são as que o usuário lê."""
    todos = screen(universo, config, cache_povoado)
    assert todos.candidates, "fixture precisa produzir ao menos um candidato"
    alvo = todos.candidates[0].symbol
    filtrado = screen(universo, config, cache_povoado, exclude={alvo})
    assert alvo not in [c.symbol for c in filtrado.candidates]
    assert filtrado.scanned == todos.scanned - 1


# --------------------------- custo da coleta ---------------------------

def test_varredura_de_universo_nao_busca_proventos(config, tmp_cache):
    """A requisição de proventos custa mais que as 120 semanas de preço, e o
    universo é triagem: quem passa dela entra na watchlist, que coleta tudo."""
    from tests.test_pipeline import FakeProvider
    universo = parse_universes(bloco())["teste"]
    provider = FakeProvider()
    refresh(universo, config, provider, tmp_cache)
    assert provider.chamadas, "os candles têm que ser coletados"
    assert provider.acoes == []


def test_screener_fetch_actions_ligado_volta_a_buscar(config, tmp_cache):
    from tests.test_pipeline import FakeProvider
    config.data["screener"]["fetch_actions"] = True
    universo = parse_universes(bloco())["teste"]
    provider = FakeProvider()
    refresh(universo, config, provider, tmp_cache)
    assert provider.acoes == provider.chamadas


def test_lista_cortada_guarda_quantos_passaram_no_filtro(config, tmp_cache):
    """"25 em Fase C/D" numa semana com 142 lê como censo e é teto — e some
    justamente o número que diz se o corte está apertado."""
    universo = parse_universes(bloco(tickers=("A3.SA", "B3.SA", "C3.SA")))["teste"]
    for symbol in ("A3.SA", "B3.SA", "C3.SA", "^BVSP"):
        tmp_cache.upsert_bars(symbol, ranged_bars(60))
    resultado = screen(universo, config, tmp_cache, phases=("B",), limit=2)
    assert len(resultado.candidates) == 2
    assert resultado.matched == 3
    assert resultado.truncated


def test_lista_inteira_nao_se_diz_cortada(config, tmp_cache):
    universo = parse_universes(bloco(tickers=("A3.SA", "B3.SA")))["teste"]
    for symbol in ("A3.SA", "B3.SA", "^BVSP"):
        tmp_cache.upsert_bars(symbol, ranged_bars(60))
    resultado = screen(universo, config, tmp_cache, phases=("B",), limit=25)
    assert resultado.matched == len(resultado.candidates)
    assert not resultado.truncated


# --------------------------- recência do evento ---------------------------

def test_evento_velho_nao_entra_na_triagem(config, tmp_cache):
    """Fase não expira — um SOS de 82 semanas atrás mantém o papel em Fase D.
    Certo para ler o gráfico, errado para triar a semana."""
    from unittest.mock import patch
    universo = parse_universes(bloco(tickers=("A3.SA",)))["teste"]
    for symbol in ("A3.SA", "^BVSP"):
        tmp_cache.upsert_bars(symbol, ranged_bars(60))
    config.data["screener"]["max_weeks_since_event"] = 6
    with patch("src.screener._phase_driver_age", return_value=(40, object())):
        resultado = screen(universo, config, tmp_cache, phases=("B",))
    assert resultado.candidates == []
    assert resultado.stale == 1
    assert resultado.max_weeks_since_event == 6


def test_evento_recente_passa(config, tmp_cache):
    from unittest.mock import patch
    universo = parse_universes(bloco(tickers=("A3.SA",)))["teste"]
    for symbol in ("A3.SA", "^BVSP"):
        tmp_cache.upsert_bars(symbol, ranged_bars(60))
    config.data["screener"]["max_weeks_since_event"] = 6
    with patch("src.screener._phase_driver_age", return_value=(2, object())):
        resultado = screen(universo, config, tmp_cache, phases=("B",))
    assert len(resultado.candidates) == 1
    assert resultado.stale == 0


def test_fase_sem_evento_para_datar_nao_e_descartada(config, tmp_cache):
    """Fase B é a causa sendo construída e não tem evento. Um filtro de recência
    que a esvaziasse estaria fazendo o que ninguém pediu."""
    universo = parse_universes(bloco(tickers=("A3.SA",)))["teste"]
    for symbol in ("A3.SA", "^BVSP"):
        tmp_cache.upsert_bars(symbol, ranged_bars(60))
    config.data["screener"]["max_weeks_since_event"] = 1
    resultado = screen(universo, config, tmp_cache, phases=("B",))
    assert len(resultado.candidates) == 1
    assert resultado.candidates[0].weeks_since_event is None
    assert resultado.stale == 0


def test_teto_zero_desliga_o_filtro(config, tmp_cache):
    from unittest.mock import patch
    universo = parse_universes(bloco(tickers=("A3.SA",)))["teste"]
    for symbol in ("A3.SA", "^BVSP"):
        tmp_cache.upsert_bars(symbol, ranged_bars(60))
    config.data["screener"]["max_weeks_since_event"] = 0
    with patch("src.screener._phase_driver_age", return_value=(200, object())):
        resultado = screen(universo, config, tmp_cache, phases=("B",))
    assert len(resultado.candidates) == 1
    assert resultado.stale == 0


def bars_fase_velha_com_evento_novo():
    """Fase D instalada por um SOS antigo, e um spring recente que a máquina
    de estados ignora de propósito (dentro do mesmo viés a fase não retrocede
    de D para C). É a forma do NSC em 16/09/2026: leitura de 60 semanas atrás
    abrindo a lista americana porque um spring imprimira na semana anterior.
    """
    import pandas as pd

    bars = pd.concat([ranged_bars(20),
                      ranged_bars(40, low=12.5, high=14.5, close=13.5, start="2026-05-25")])
    bars.index = pd.date_range("2026-01-05", periods=60, freq="7D", name="week_start")
    bars = set_bar(bars, 20, open=12.7, high=15.6, low=12.6, close=15.0, volume=200.0)
    return set_bar(bars, 57, low=12.2, close=13.5, volume=80.0)


def test_idade_da_fase_e_a_do_evento_que_a_instalou(config, tmp_cache):
    """O spring recente não rejuvenesce uma Fase D de 39 semanas atrás."""
    from src.analysis import analyze
    from src.metrics import compute_metrics
    from src.screener import _last_aligned_event, _phase_driver_age
    from src.watchlist import WatchItem

    bars = bars_fase_velha_com_evento_novo()
    analise = analyze(WatchItem("A3.SA", "b3", "^BVSP"), compute_metrics(bars, config), config)
    assert analise.phase.code == "D_acumulacao"
    assert analise.phase.driver.kind == "sos"
    idade_fase, driver = _phase_driver_age(analise)
    idade_alinhado, alinhado = _last_aligned_event(analise)
    assert driver.kind == "sos" and idade_fase == 39
    assert alinhado.kind == "spring" and idade_alinhado == 2   # o que enganava o filtro


def test_fase_velha_nao_entra_na_triagem_por_evento_novo(config, tmp_cache):
    """Regressão do NSC: o filtro de recência data a LEITURA, não o último
    evento do mesmo viés que passou por perto."""
    universo = parse_universes(bloco(tickers=("A3.SA",)))["teste"]
    popular(tmp_cache, "^BVSP", ranged_bars(60))
    popular(tmp_cache, "A3.SA", bars_fase_velha_com_evento_novo())
    config.data["screener"]["max_weeks_since_event"] = 6
    resultado = screen(universo, config, tmp_cache, phases=("C", "D"))
    assert resultado.candidates == []
    assert resultado.stale == 1


def test_csv_traz_as_duas_idades(config, tmp_cache):
    """Quando as duas divergem, é no CSV que se vê por quê."""
    universo = parse_universes(bloco(tickers=("A3.SA",)))["teste"]
    popular(tmp_cache, "^BVSP", ranged_bars(60))
    popular(tmp_cache, "A3.SA", bars_fase_velha_com_evento_novo())
    config.data["screener"]["max_weeks_since_event"] = 0     # desligado, para o papel entrar
    frame = to_frame(screen(universo, config, tmp_cache, phases=("C", "D")), config)
    linha = frame.iloc[0]
    assert linha["evento_da_fase"].startswith("SOS") and linha["semanas_desde_evento"] == 39
    assert linha["ultimo_evento_alinhado"] == "Spring" and linha["semanas_desde_ultimo"] == 2
