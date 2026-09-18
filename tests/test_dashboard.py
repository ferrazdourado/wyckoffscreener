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

from pathlib import Path

from streamlit.testing.v1 import AppTest

from src.data.cache import Cache
from tests.conftest import weeks_index

APP = str(Path(__file__).resolve().parent.parent / "src" / "dashboard.py")


def semanal(escala: float = 1.0):
    """Queda de 25% e depois lateralização oscilando dentro do range.

    Não é enfeite: a queda é o que declara o viés de acumulação, e a oscilação
    é o que faz o Ponto & Figura ter colunas para contar. Uma série lisa
    atravessa o dashboard sem acionar nem fase nem contagem de causa — foi
    exatamente por isso que um erro no painel de P&F passou pelos testes uma
    vez.
    """
    import math

    import pandas as pd

    queda = [14.0 - 0.29 * i for i in range(12)]
    onda = [10.5 + 0.5 * math.sin(i * math.pi / 4) for i in range(34)]
    precos = [p * escala for p in queda + onda]
    n = len(precos)
    bars = pd.DataFrame(
        {"open": precos, "close": precos,
         "high": [p + 0.2 * escala for p in precos], "low": [p - 0.2 * escala for p in precos],
         "volume": [1000.0] * n},
        index=weeks_index(n, start="2025-01-06"),
    )
    bars["is_partial"] = False
    return bars


def diario(semanas):
    """Cinco pregões por semana, para a contagem de P&F usar o insumo real."""
    import pandas as pd

    linhas = []
    for inicio, semana in semanas.iterrows():
        for dia in range(5):
            linhas.append({"date": pd.Timestamp(inicio) + pd.Timedelta(days=dia),
                           "open": semana["open"], "high": semana["high"],
                           "low": semana["low"], "close": semana["close"],
                           "volume": semana["volume"] / 5})
    return pd.DataFrame(linhas).set_index("date")


@pytest.fixture
def projeto(tmp_path):
    """config + watchlist + SQLite povoado, tudo dentro do tmp_path."""
    cache_path = tmp_path / "cache.sqlite"
    papel, indice = semanal(), semanal(escala=4.0)
    with Cache(cache_path) as cache:
        cache.upsert_bars("PETR4.SA", papel)
        cache.upsert_bars("^BVSP", indice)
        cache.upsert_daily("PETR4.SA", diario(papel))
        cache.record_fetch("PETR4.SA", "ok", len(papel))

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


def test_contagem_de_causa_aparece_com_os_numeros_que_a_produziram(projeto, monkeypatch):
    """Regressão: o painel chamava um atributo que o CauseCount não tem."""
    app = rodar(*projeto, monkeypatch)
    assert not app.exception
    assert any("Contagem de causa (P&F)" in m.value for m in app.markdown)


def test_config_inexistente_vira_mensagem_e_nao_stacktrace(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dashboard.py", "--config", str(tmp_path / "nao.yaml"),
                                      "--watchlist", str(tmp_path / "nada.yaml")])
    app = AppTest.from_file(APP, default_timeout=90).run()
    assert not app.exception
    assert app.error and "Não foi possível carregar" in app.error[0].value


def test_disclaimer_da_spec_esta_na_tela(projeto, monkeypatch):
    app = rodar(*projeto, monkeypatch)
    assert any("Não constitui recomendação" in c.value for c in app.sidebar.caption)
