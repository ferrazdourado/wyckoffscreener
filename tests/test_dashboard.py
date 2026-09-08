"""P2 — dashboard: o app roda inteiro, sem rede, sobre um cache sintético.

Usa o `AppTest` do próprio streamlit, que executa o script como o servidor
executaria e devolve os elementos renderizados — dá para afirmar que a página
sobe sem exceção, e não só que o arquivo importa. Se o streamlit não estiver
instalado (é extra opcional), o arquivo inteiro é pulado.
"""

from __future__ import annotations

import sys

import pytest
import yaml

pytest.importorskip("streamlit", reason="dashboard é extra opcional: pip install streamlit")

from pathlib import Path  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

from src.data.cache import Cache  # noqa: E402
from tests.conftest import make_bars  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "src" / "dashboard.py")


def semanal(n: int, base: float) -> "pd.DataFrame":
    """Série com tendência leve, para as métricas de 20 semanas ficarem válidas."""
    import pandas as pd

    bars = make_bars(n=n, start="2024-01-01")
    passo = pd.Series(range(n), index=bars.index) * 0.05
    for coluna, ajuste in (("open", 0.0), ("high", 1.0), ("low", -1.0), ("close", 0.5)):
        bars[coluna] = base + passo + ajuste
    bars["volume"] = 1000.0
    bars["is_partial"] = False
    return bars


@pytest.fixture
def projeto(tmp_path):
    """config + watchlist + SQLite povoado, tudo dentro do tmp_path."""
    cache_path = tmp_path / "cache.sqlite"
    with Cache(cache_path) as cache:
        cache.upsert_bars("PETR4.SA", semanal(60, 30.0))
        cache.upsert_bars("^BVSP", semanal(60, 120.0))
        cache.record_fetch("PETR4.SA", "ok", 60)

    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"data": {"cache_path": str(cache_path)},
                                      "output": {"reports_dir": str(tmp_path / "reports")}}),
                      encoding="utf-8")
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(yaml.safe_dump(
        {"defaults": {"benchmark": {"b3": "^BVSP", "us": "^GSPC"}},
         "tickers": [{"symbol": "PETR4.SA", "market": "b3"}]}), encoding="utf-8")
    return config, watchlist


def rodar(config, watchlist, monkeypatch) -> AppTest:
    monkeypatch.setattr(sys, "argv", ["dashboard.py", "--config", str(config),
                                      "--watchlist", str(watchlist)])
    app = AppTest.from_file(APP, default_timeout=90)
    return app.run()


def test_app_sobe_sem_excecao_e_titula_a_semana(projeto, monkeypatch):
    app = rodar(*projeto, monkeypatch)
    assert not app.exception
    assert app.title[0].value.startswith("Wyckoff Screener — semana")


def test_tela_diz_de_quando_sao_os_dados(projeto, monkeypatch):
    """Página aberta há dias parece atual; o carimbo impede a confusão."""
    app = rodar(*projeto, monkeypatch)
    assert any("dados coletados em" in c.value for c in app.caption)


def test_papel_do_cache_aparece_na_tabela(projeto, monkeypatch):
    app = rodar(*projeto, monkeypatch)
    assert any("PETR4.SA" in str(df.value.values) for df in app.dataframe)


def test_sem_nivel_de_invalidacao_a_secao_explica_em_vez_de_ficar_vazia(projeto, monkeypatch):
    app = rodar(*projeto, monkeypatch)
    assert any("sem nível de invalidação" in i.value or "nível de invalidação" in i.value
               for i in app.info)


def test_config_inexistente_vira_mensagem_e_nao_stacktrace(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dashboard.py", "--config", str(tmp_path / "nao.yaml"),
                                      "--watchlist", str(tmp_path / "nada.yaml")])
    app = AppTest.from_file(APP, default_timeout=90).run()
    assert not app.exception
    assert app.error and "Não foi possível carregar" in app.error[0].value


def test_disclaimer_da_spec_esta_na_tela(projeto, monkeypatch):
    app = rodar(*projeto, monkeypatch)
    assert any("Não constitui recomendação" in c.value for c in app.sidebar.caption)
