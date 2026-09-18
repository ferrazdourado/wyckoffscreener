"""Regressões da auditoria de 07/09/2026.

Um teste por defeito encontrado, nomeado pelo sintoma — se algum voltar,
a falha diz qual é.
"""

import copy
import datetime as dt

import pandas as pd
import pytest

from src.cli import main, summary_columns
from src.config import Config, DEFAULTS
from src.metrics import compute_metrics, true_range
from src.pipeline import _data_week_tag, _market_of, export_csv
from src.watchlist import load_watchlist, parse_watchlist
from tests.conftest import make_bars


# --- 1. `add` corrompia a watchlist com nota contendo aspas ---

@pytest.fixture
def projeto(tmp_path, monkeypatch):
    import shutil

    shutil.copy("config.yaml", tmp_path / "config.yaml")
    shutil.copy("watchlist.yaml", tmp_path / "watchlist.yaml")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("nota", [
    'o "range" do Bruno\'s plano',
    "aspas simples: 'isso'",
    'aspas duplas: "isso"',
    "dois pontos: valor",
    "quebra\nde linha",
    "# não é comentário",
])
def test_add_escapa_notas_sem_corromper_a_watchlist(projeto, nota):
    assert main(["add", "TESTE3.SA", "--notes", nota]) == 0
    wl = load_watchlist(projeto / "watchlist.yaml")
    assert wl.get("TESTE3.SA").notes == nota.strip()
    assert len(wl) == 13


def test_add_restaura_o_arquivo_se_a_validacao_falhar(projeto, capsys):
    original = (projeto / "watchlist.yaml").read_text(encoding="utf-8")
    assert main(["add", "XPTO", "--market", "b3"]) == 2  # us sem .SA declarado como b3
    assert "sufixo `.SA`" in capsys.readouterr().err
    assert (projeto / "watchlist.yaml").read_text(encoding="utf-8") == original


# --- 2. `verify` inventava a conta quando o histórico era curto ---

def test_verify_nao_inventa_janela_com_historico_curto(projeto, capsys, monkeypatch):
    from src.data.cache import Cache
    from src.pipeline import fetch_all
    from tests.test_pipeline import FakeProvider

    config = Config(copy.deepcopy(DEFAULTS))
    config.data["data"]["cache_path"] = str(projeto / "c.sqlite")
    wl = parse_watchlist({"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]})
    (projeto / "watchlist.yaml").write_text(
        "tickers:\n  - symbol: PETR4.SA\n    market: b3\n", encoding="utf-8")
    (projeto / "config.yaml").write_text(
        f"data:\n  cache_path: {projeto / 'c.sqlite'}\n", encoding="utf-8")
    with Cache(projeto / "c.sqlite") as cache:
        fetch_all(wl, config, FakeProvider(weeks=10), cache, now=dt.datetime(2026, 9, 7, 20, 0))

    assert main(["verify", "PETR4.SA"]) == 0
    out = capsys.readouterr().out
    assert "janela incompleta" in out
    # o defeito antigo imprimia uma soma de 10 semanas rotulada como 20
    assert "soma dos últimos 20 volumes" not in out


# --- 3. tabela-resumo ignorava janelas de FR configuradas ---

def test_colunas_do_resumo_seguem_o_config():
    config = Config(copy.deepcopy(DEFAULTS))
    config.data["metrics"]["relative_strength_weeks"] = [3, 10]
    config.data["metrics"]["volume_sma_weeks"] = 8
    nomes = [c for c, _, _ in summary_columns(config)]
    rotulos = [r for _, r, _ in summary_columns(config)]
    assert "rs_3w" in nomes and "rs_10w" in nomes
    assert "rs_4w" not in nomes
    assert "vol/méd8" in rotulos


def test_colunas_do_resumo_existem_de_fato_nas_metricas():
    """O acoplamento que falhou: a tabela pedia coluna que compute_metrics não gerava."""
    config = Config(copy.deepcopy(DEFAULTS))
    config.data["metrics"]["relative_strength_weeks"] = [3, 10]
    out = compute_metrics(make_bars(30), config, make_bars(30))
    for nome, _, _ in summary_columns(config):
        assert nome in out.columns, f"tabela pede {nome}, métricas não geram"


# --- 4. índice B3 fora do mapa fixo virava mercado US ---

def test_benchmark_herda_o_mercado_de_quem_o_referencia():
    wl = parse_watchlist({"tickers": [
        {"symbol": "PETR4.SA", "market": "b3", "benchmark": "^IBXX"},
    ]})
    assert _market_of("^IBXX", wl) == "b3"


def test_benchmark_conhecido_continua_correto():
    wl = parse_watchlist({"tickers": [{"symbol": "BAC", "market": "us"}]})
    assert _market_of("^GSPC", wl) == "us"
    assert _market_of("PETR4.SA", parse_watchlist(
        {"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]})) == "b3"


def test_simbolo_fora_da_watchlist_usa_o_mesmo_palpite_da_fabrica():
    # Havia dois mapas índice -> mercado; o do pipeline não tinha ^IDIV nem
    # olhava o sufixo, e um .SA fora da watchlist fechava a semana em NY.
    wl = parse_watchlist({"tickers": [{"symbol": "BAC", "market": "us"}]})
    assert _market_of("^IDIV", wl) == "b3"
    assert _market_of("VALE3.SA", wl) == "b3"
    assert _market_of("^DJI", wl) == "us"


# --- 5. nome do CSV vinha do dia da rodada, não da semana dos dados ---

def test_etiqueta_do_csv_vem_da_semana_dos_dados():
    bars = make_bars(3, start="2026-08-17")     # 17/08, 24/08, 31/08
    bars["is_partial"] = False
    metrics = {"PETR4.SA": bars}
    # Sexta (04/09) e quarta seguinte (09/09) caem em semanas ISO diferentes...
    sexta = _data_week_tag(metrics, dt.date(2026, 9, 4))
    quarta = _data_week_tag(metrics, dt.date(2026, 9, 9))
    assert sexta == quarta == "2026-36"          # ...mas os dados são os mesmos


def test_semana_em_formacao_nao_define_a_etiqueta():
    bars = make_bars(3, start="2026-08-24")     # 24/08, 31/08, 07/09
    bars["is_partial"] = [False, False, True]
    assert _data_week_tag({"X": bars}, dt.date(2026, 9, 9)) == "2026-36"


def test_export_csv_nomeia_pelo_dado(tmp_path):
    config = Config(copy.deepcopy(DEFAULTS))
    config.data["output"]["csv_dir"] = str(tmp_path)
    bars = make_bars(25, start="2026-03-16")
    bars["is_partial"] = False
    wl = parse_watchlist({"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]})
    metrics = {"PETR4.SA": compute_metrics(bars, config, None).assign(is_partial=False)}
    full, latest = export_csv(metrics, wl, config, dt.datetime(2026, 9, 9, 20, 0))
    esperado = _data_week_tag(metrics, dt.date(2026, 9, 9))
    assert esperado in full.name and esperado in latest.name


# --- 6. true_range estourava com DataFrame vazio ---

def test_true_range_de_dataframe_vazio():
    vazio = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert true_range(vazio).empty


def test_compute_metrics_de_dataframe_vazio():
    config = Config(copy.deepcopy(DEFAULTS))
    vazio = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert compute_metrics(vazio, config, None).empty
