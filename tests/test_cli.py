"""CLI: contratos de saída e código de retorno (sem rede)."""

import shutil

import pytest

from src.cli import main
from src.pipeline import FetchReport, SymbolStatus
from src.watchlist import load_watchlist


@pytest.fixture
def projeto(tmp_path, monkeypatch):
    """Cópia isolada do config/watchlist do projeto."""
    shutil.copy("config.yaml", tmp_path / "config.yaml")
    shutil.copy("watchlist.yaml", tmp_path / "watchlist.yaml")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(*args) -> int:
    return main(list(args))


def test_validate_ok(projeto, capsys):
    assert run("validate") == 0
    out = capsys.readouterr().out
    assert "12 papéis" in out and "PETR4.SA" in out


def test_validate_reporta_watchlist_quebrada(projeto, capsys):
    (projeto / "watchlist.yaml").write_text("tickers:\n  - symbol: PETR4\n    market: b3\n", encoding="utf-8")
    assert run("validate") == 2
    assert "sufixo `.SA`" in capsys.readouterr().err


def test_add_acrescenta_e_revalida(projeto, capsys):
    assert run("add", "WEGE3.SA", "--invalidation", "45.5") == 0
    wl = load_watchlist(projeto / "watchlist.yaml")
    item = wl.get("WEGE3.SA")
    assert item.market == "b3" and item.benchmark == "^BVSP"
    assert item.invalidation.price == 45.5 and item.invalidation.direction == "below"


def test_add_infere_mercado_us_sem_sufixo(projeto):
    assert run("add", "NVDA") == 0
    assert load_watchlist(projeto / "watchlist.yaml").get("NVDA").market == "us"


def test_add_recusa_duplicado(projeto, capsys):
    assert run("add", "PETR4.SA") == 1
    assert "já está na watchlist" in capsys.readouterr().err


def test_metrics_sem_cache_falha_com_orientacao(projeto, capsys):
    assert run("metrics") == 1
    assert "wyckoff fetch" in capsys.readouterr().err


def test_show_de_ticker_sem_dados(projeto, capsys):
    assert run("show", "PETR4.SA") == 1
    assert "sem métricas" in capsys.readouterr().err


@pytest.mark.parametrize("statuses,codigo", [
    ([SymbolStatus("BAC", "ok", 120), SymbolStatus("XYZ", "error", 0, "sem dados")], 0),
    ([SymbolStatus("XYZ", "error", 0, "sem dados")], 1),
])
def test_fetch_no_metrics_so_falha_quando_nada_foi_coletado(projeto, monkeypatch, statuses, codigo):
    # Com --no-metrics, um papel com erro saía 1; sem a flag, os mesmos erros
    # saíam 0. Falha de um papel não aborta (R2) nos dois caminhos.
    monkeypatch.setattr("src.cli.build_provider", lambda *a, **k: None)
    monkeypatch.setattr("src.cli.fetch_all", lambda *a, **k: FetchReport(list(statuses)))
    assert run("fetch", "--no-metrics") == codigo
