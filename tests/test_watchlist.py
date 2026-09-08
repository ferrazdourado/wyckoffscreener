"""R1 — schema da watchlist e qualidade das mensagens de erro."""

import datetime as dt

import pytest

from src.watchlist import WatchlistError, load_watchlist, parse_watchlist


def base(**overrides):
    ticker = {"symbol": "PETR4.SA", "market": "b3"}
    ticker.update(overrides)
    return {"tickers": [ticker]}


def test_parse_minimo_aplica_benchmark_default():
    wl = parse_watchlist(base())
    assert len(wl) == 1
    assert wl.items[0].symbol == "PETR4.SA"
    assert wl.items[0].benchmark == "^BVSP"


def test_benchmark_default_por_mercado():
    wl = parse_watchlist({"tickers": [{"symbol": "BAC", "market": "us"}]})
    assert wl.items[0].benchmark == "^GSPC"


def test_defaults_do_arquivo_sobrepoem_o_embutido():
    wl = parse_watchlist({
        "defaults": {"benchmark": {"us": "^IXIC"}},
        "tickers": [{"symbol": "SBUX", "market": "us"}],
    })
    assert wl.items[0].benchmark == "^IXIC"


def test_benchmark_explicito_vence_o_default():
    wl = parse_watchlist(base(benchmark="^IBXX"))
    assert wl.items[0].benchmark == "^IBXX"


def test_symbol_normalizado_para_maiusculo():
    wl = parse_watchlist({"tickers": [{"symbol": " petr4.sa ", "market": "b3"}]})
    assert wl.items[0].symbol == "PETR4.SA"


def test_invalidation_completa():
    wl = parse_watchlist(base(invalidation={"price": 30.5, "direction": "below"}))
    inval = wl.items[0].invalidation
    assert inval.price == 30.5
    assert inval.is_violated(30.4) and not inval.is_violated(30.6)
    assert "abaixo de 30.5" in inval.describe()


def test_invalidation_direcao_above():
    wl = parse_watchlist(base(invalidation={"price": 30.0, "direction": "above"}))
    inval = wl.items[0].invalidation
    assert inval.is_violated(30.1) and not inval.is_violated(29.9)


def test_invalidation_direcao_default_e_below():
    wl = parse_watchlist(base(invalidation={"price": 30.0}))
    assert wl.items[0].invalidation.direction == "below"


def test_range_manual_e_calendario():
    wl = parse_watchlist(base(
        range={"support": 20.0, "resistance": 25.0},
        calendar=[{"date": "2026-11-10", "label": "balanço 3T26"}],
    ))
    item = wl.items[0]
    assert item.manual_range.support == 20.0
    assert item.calendar[0].date == dt.date(2026, 11, 10)
    assert item.calendar[0].label == "balanço 3T26"


def test_benchmarks_distintos_preservam_ordem():
    wl = parse_watchlist({"tickers": [
        {"symbol": "PETR4.SA", "market": "b3"},
        {"symbol": "BAC", "market": "us"},
        {"symbol": "VALE3.SA", "market": "b3"},
    ]})
    assert wl.benchmarks == ["^BVSP", "^GSPC"]


# --------------------------- erros ---------------------------

def erro(raw) -> str:
    with pytest.raises(WatchlistError) as exc:
        parse_watchlist(raw)
    return str(exc.value)


def test_tickers_ausente():
    assert "`tickers` ausente" in erro({"defaults": {}})


def test_tickers_vazia():
    assert "está vazia" in erro({"tickers": []})


def test_symbol_ausente():
    msg = erro({"tickers": [{"market": "b3"}]})
    assert "tickers[0].symbol" in msg and "obrigatório" in msg


def test_market_invalido_lista_os_validos():
    msg = erro({"tickers": [{"symbol": "PETR4.SA", "market": "bovespa"}]})
    assert "tickers[0] (PETR4.SA).market" in msg
    assert "['b3', 'us']" in msg


def test_b3_sem_sufixo_sa():
    msg = erro({"tickers": [{"symbol": "PETR4", "market": "b3"}]})
    assert "sufixo `.SA`" in msg


def test_us_com_sufixo_sa():
    msg = erro({"tickers": [{"symbol": "BAC.SA", "market": "us"}]})
    assert "não vale para mercado `us`" in msg


def test_simbolo_duplicado_aponta_a_primeira_ocorrencia():
    msg = erro({"tickers": [
        {"symbol": "PETR4.SA", "market": "b3"},
        {"symbol": "PETR4.SA", "market": "b3"},
    ]})
    assert "duplicado" in msg and "tickers[0]" in msg


def test_chave_desconhecida_sugere_as_validas():
    msg = erro(base(invalidacao={"price": 1}))
    assert "chaves desconhecidas: ['invalidacao']" in msg


def test_invalidation_sem_price():
    assert "invalidation.price" in erro(base(invalidation={"direction": "below"}))


def test_invalidation_direcao_invalida():
    msg = erro(base(invalidation={"price": 10, "direction": "abaixo"}))
    assert "['below', 'above']" in msg


def test_invalidation_preco_negativo():
    assert "deve ser positivo" in erro(base(invalidation={"price": -3}))


def test_invalidation_preco_nao_numerico():
    assert "esperado número" in erro(base(invalidation={"price": "trinta"}))


def test_range_invertido():
    msg = erro(base(range={"support": 25.0, "resistance": 20.0}))
    assert "deve ser menor que resistance" in msg


def test_calendario_data_invalida():
    msg = erro(base(calendar=[{"date": "10/11/2026", "label": "balanço"}]))
    assert "AAAA-MM-DD" in msg


def test_todos_os_erros_sao_reportados_de_uma_vez():
    """O usuário corrige tudo numa passada, não um erro por execução."""
    msg = erro({"tickers": [
        {"symbol": "PETR4", "market": "b3"},              # falta .SA
        {"symbol": "VALE3.SA", "market": "bovespa"},      # mercado inválido
        {"symbol": "BAC", "market": "us", "range": {"support": 9, "resistance": 5}},
    ]})
    assert "3 erro(s)" in msg
    assert "PETR4" in msg and "VALE3.SA" in msg and "BAC" in msg


def test_arquivo_inexistente():
    with pytest.raises(WatchlistError, match="não encontrado"):
        load_watchlist("nao/existe.yaml")


def test_yaml_invalido(tmp_path):
    path = tmp_path / "wl.yaml"
    path.write_text("tickers: [\n  - symbol: 'x'\n", encoding="utf-8")
    with pytest.raises(WatchlistError, match="YAML inválido"):
        load_watchlist(path)


def test_watchlist_do_projeto_e_valida():
    wl = load_watchlist("watchlist.yaml")
    assert len(wl) == 12
    assert "PETR4.SA" in wl.symbols and "BAC" in wl.symbols
