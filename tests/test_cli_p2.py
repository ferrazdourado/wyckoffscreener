"""CLI dos itens P2: `sources`, `pdf` e `dashboard`. Sem rede e sem navegador."""


import pandas as pd
import pytest
import yaml

from src import pdf
from src.cli import main
from src.data.cache import Cache
from tests.conftest import ranged_bars


@pytest.fixture
def projeto(tmp_path, config):
    """Projeto completo em disco, com cache já povoado."""
    cache_path = tmp_path / "wyckoff.sqlite"
    bars = ranged_bars(25)
    bars["is_partial"] = False
    with Cache(cache_path) as cache:
        cache.upsert_bars("PETR4.SA", bars)
        cache.upsert_bars("^BVSP", bars)

    cfg = dict(config.data)
    cfg["data"] = {**cfg["data"], "cache_path": str(cache_path)}
    # Um motor só, e substituído nos testes: nada aqui abre navegador, e o
    # resultado não muda conforme a máquina tenha wkhtmltopdf instalado.
    cfg["output"] = {**cfg["output"], "reports_dir": str(tmp_path / "reports"),
                     "csv_dir": str(tmp_path / "exports"),
                     "pdf": {**cfg["output"].get("pdf", {}), "engines": ["chrome"]}}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(yaml.safe_dump(
        {"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]}), encoding="utf-8")
    return ["--config", str(config_path), "--watchlist", str(watchlist_path)], tmp_path


def serie(precos: dict[str, float]) -> pd.DataFrame:
    valores = list(precos.values())
    return pd.DataFrame(
        {"open": valores, "high": [v + 1 for v in valores], "low": [v - 1 for v in valores],
         "close": valores, "volume": [1000.0] * len(valores)},
        index=pd.to_datetime(list(precos)),
    )


IGUAL = {"2026-08-10": 10.0, "2026-08-11": 11.0, "2026-08-12": 12.0}


def dublar(monkeypatch, esquerda: pd.DataFrame, direita: pd.DataFrame) -> None:
    """Troca as duas fontes por séries prontas — `sources` nunca vai à rede aqui."""
    from src.data.provider import DataProvider

    class Falsa(DataProvider):
        def __init__(self, bars):
            self.bars = bars

        def daily_bars(self, symbol, weeks):
            return self.bars

        def corporate_actions(self, symbol):
            return pd.DataFrame(columns=["date", "kind", "value"])

    fontes = {"yfinance": Falsa(esquerda), "brapi": Falsa(direita)}
    monkeypatch.setattr("src.cli.build_single", lambda nome, config: fontes[nome])


# --------------------------- sources ---------------------------

def test_sources_com_fontes_iguais_sai_zero(projeto, monkeypatch, capsys):
    args, _ = projeto
    dublar(monkeypatch, serie(IGUAL), serie(IGUAL))
    assert main([*args, "sources", "PETR4.SA", "--weeks", "4"]) == 0
    assert "mesma história" in capsys.readouterr().out


def test_sources_com_divergencia_sai_um_e_mostra_os_dois_numeros(projeto, monkeypatch, capsys):
    """Código de saída ≠ 0 para dar para encadear num script de conferência."""
    args, _ = projeto
    dublar(monkeypatch, serie(IGUAL), serie(dict(IGUAL, **{"2026-08-11": 13.0})))
    assert main([*args, "sources", "PETR4.SA", "--weeks", "4"]) == 1
    saida = capsys.readouterr().out
    assert "Preços divergentes" in saida and "yfinance=11.0000" in saida


def test_sources_aponta_pregao_que_so_uma_fonte_tem(projeto, monkeypatch, capsys):
    args, _ = projeto
    faltando = {k: v for k, v in IGUAL.items() if k != "2026-08-11"}
    dublar(monkeypatch, serie(IGUAL), serie(faltando))
    main([*args, "sources", "PETR4.SA", "--weeks", "4"])
    assert "Pregões só em yfinance" in capsys.readouterr().out


def test_sources_recusa_comparar_a_fonte_com_ela_mesma(projeto, capsys):
    args, _ = projeto
    assert main([*args, "sources", "PETR4.SA", "--against", "yfinance"]) == 2


def test_sources_com_fonte_inexistente_lista_as_que_existem(projeto, capsys):
    args, _ = projeto
    assert main([*args, "sources", "PETR4.SA", "--against", "bloomberg"]) == 2
    assert "desconhecida" in capsys.readouterr().err


# --------------------------- pdf ---------------------------

def test_pdf_converte_o_relatorio_da_semana(projeto, monkeypatch, capsys):
    args, tmp_path = projeto
    main([*args, "report", "--offline"])
    monkeypatch.setitem(pdf.ENGINES, "chrome", lambda h, p, c: p.write_bytes(b"%PDF ... %%EOF"))
    assert main([*args, "pdf"]) == 0
    saida = capsys.readouterr().out
    assert "PDF:" in saida and "via chrome" in saida
    assert list((tmp_path / "reports").glob("*.pdf"))


def test_pdf_sem_relatorio_gerado_manda_gerar(projeto, capsys):
    args, _ = projeto
    assert main([*args, "pdf"]) == 1
    assert "wyckoff report" in capsys.readouterr().err


def test_pdf_de_semana_inexistente_falha_sem_apagar_nada(projeto, capsys):
    args, _ = projeto
    main([*args, "report", "--offline"])
    assert main([*args, "pdf", "1999-01"]) == 1
    assert "não encontrado" in capsys.readouterr().err


def test_report_com_pdf_gera_os_tres_arquivos(projeto, monkeypatch, capsys):
    args, tmp_path = projeto
    monkeypatch.setitem(pdf.ENGINES, "chrome", lambda h, p, c: p.write_bytes(b"%PDF ... %%EOF"))
    assert main([*args, "report", "--offline", "--pdf"]) == 0
    reports = tmp_path / "reports"
    assert len(list(reports.glob("*.md"))) == 1
    assert len(list(reports.glob("*.html"))) == 1
    assert len(list(reports.glob("*.pdf"))) == 1


def test_report_sem_pdf_nao_gera_pdf(projeto):
    args, tmp_path = projeto
    main([*args, "report", "--offline"])
    assert list((tmp_path / "reports").glob("*.pdf")) == []


def test_falha_do_pdf_nao_invalida_o_relatorio_ja_escrito(projeto, monkeypatch, capsys):
    """O relatório é o entregável; o PDF é conveniência."""
    from src.pdf import PdfError

    args, tmp_path = projeto

    def explode(h, p, c):
        raise PdfError("sem navegador")

    monkeypatch.setitem(pdf.ENGINES, "chrome", explode)
    codigo = main([*args, "report", "--offline", "--pdf"])
    assert codigo == 1
    assert list((tmp_path / "reports").glob("*.html"))
    assert "PDF não gerado" in capsys.readouterr().err


# --------------------------- .env ---------------------------

def test_env_file_abastece_o_ambiente_antes_do_comando(projeto, tmp_path, monkeypatch, capsys):
    """Sem isto o `.env` é um arquivo que parece configurar e não configura."""
    args, _ = projeto
    monkeypatch.delenv("WYCKOFF_TELEGRAM_TOKEN", raising=False)
    env_file = tmp_path / "segredos.env"
    env_file.write_text("WYCKOFF_TELEGRAM_TOKEN=abc123\n", encoding="utf-8")
    main([*args, "--env-file", str(env_file), "validate"])
    import os
    assert os.environ["WYCKOFF_TELEGRAM_TOKEN"] == "abc123"


def test_validate_lista_os_segredos_pelo_nome_e_nunca_pelo_valor(projeto, tmp_path,
                                                                 monkeypatch, capsys):
    args, _ = projeto
    monkeypatch.delenv("WYCKOFF_SMTP_PASSWORD", raising=False)
    env_file = tmp_path / "segredos.env"
    env_file.write_text("WYCKOFF_SMTP_PASSWORD=abracadabra\n", encoding="utf-8")
    main([*args, "--env-file", str(env_file), "validate"])
    saida = capsys.readouterr().out
    assert "WYCKOFF_SMTP_PASSWORD" in saida and "definida" in saida
    assert "abracadabra" not in saida


def test_sem_env_file_validate_diz_como_seguir(projeto, tmp_path, monkeypatch, capsys):
    args, _ = projeto
    from src import env as env_mod
    monkeypatch.setattr(env_mod, "RAIZ", tmp_path / "sem-raiz")
    monkeypatch.chdir(tmp_path)
    main([*args, "validate"])
    assert "exporte as variáveis à mão" in capsys.readouterr().out


# --------------------------- dashboard ---------------------------

def test_dashboard_manda_o_streamlit_ler_os_mesmos_arquivos(projeto, monkeypatch, capsys):
    args, _ = projeto
    visto = {}

    def falso_call(comando):
        visto["comando"] = comando
        return 0

    monkeypatch.setattr("subprocess.call", falso_call)
    assert main([*args, "dashboard", "--port", "8599"]) == 0
    comando = visto["comando"]
    assert "run" in comando and comando[-4:-2] == ["--config", args[1]]
    assert "8599" in comando


def test_dashboard_escuta_so_na_propria_maquina(projeto, monkeypatch):
    """O default do streamlit publica na rede local; a watchlist é pessoal."""
    args, _ = projeto
    visto = {}
    monkeypatch.setattr("subprocess.call", lambda c: visto.setdefault("c", c) and 0 or 0)
    main([*args, "dashboard"])
    comando = visto["c"]
    assert comando[comando.index("--server.address") + 1] == "localhost"


def test_dashboard_sobe_sem_o_botao_deploy(projeto, monkeypatch):
    """Não há nuvem para onde publicar num screener local."""
    args, _ = projeto
    visto = {}
    monkeypatch.setattr("subprocess.call", lambda c: visto.setdefault("c", c) and 0 or 0)
    main([*args, "dashboard"])
    comando = visto["c"]
    assert comando[comando.index("--client.toolbarMode") + 1] == "minimal"


def test_dashboard_nao_manda_telemetria(projeto, monkeypatch):
    args, _ = projeto
    visto = {}
    monkeypatch.setattr("subprocess.call", lambda c: visto.setdefault("c", c) and 0 or 0)
    main([*args, "dashboard"])
    comando = visto["c"]
    assert comando[comando.index("--browser.gatherUsageStats") + 1] == "false"


# --------------------------- report --screen ---------------------------

def _universo_de_teste(tmp_path, symbols=("PETR4.SA",)):
    import yaml as _yaml
    caminho = tmp_path / "universo.yaml"
    caminho.write_text(_yaml.safe_dump(
        {"universes": {"teste": {"market": "b3", "benchmark": "^BVSP",
                                 "tickers": list(symbols)}}}), encoding="utf-8")
    return caminho


def test_report_sem_screen_nao_tem_secao_de_candidatos(projeto):
    args, tmp_path = projeto
    main([*args, "report", "--offline"])
    texto = next((tmp_path / "reports").glob("*.md")).read_text(encoding="utf-8")
    assert "Candidatos fora da watchlist" not in texto
    assert "## 4. Papel a papel" in texto and "## 5. Erros de coleta" in texto


def test_report_com_screen_acrescenta_a_secao_e_renumera(projeto, tmp_path, monkeypatch):
    """A numeração das seções da spec continua contígua com a seção nova."""
    args, raiz = projeto
    universo = _universo_de_teste(tmp_path, ["OUTRO.SA"])
    monkeypatch.setattr("src.screener.load_universes",
                        lambda caminho: __import__("src.screener", fromlist=["load_universes"])
                        .parse_universes(__import__("yaml").safe_load(universo.read_text()), str(universo)))
    main([*args, "report", "--offline", "--screen", "--universes", "teste"])
    texto = next((raiz / "reports").glob("*.md")).read_text(encoding="utf-8")
    assert "## 4. Top por momentum" in texto
    assert "## 5. Candidatos fora da watchlist" in texto
    assert "## 6. Papel a papel" in texto
    assert "## 7. Erros de coleta" in texto


def test_papel_da_watchlist_nao_aparece_como_candidato_no_relatorio(projeto, tmp_path, monkeypatch):
    args, raiz = projeto
    universo = _universo_de_teste(tmp_path, ["PETR4.SA"])   # o mesmo da watchlist
    monkeypatch.setattr("src.screener.load_universes",
                        lambda caminho: __import__("src.screener", fromlist=["load_universes"])
                        .parse_universes(__import__("yaml").safe_load(universo.read_text()), str(universo)))
    main([*args, "report", "--offline", "--screen", "--universes", "teste"])
    texto = next((raiz / "reports").glob("*.md")).read_text(encoding="utf-8")
    secao = texto.split("## 5. Candidatos")[1].split("## 6.")[0]
    assert "PETR4.SA" not in secao


def test_universo_inexistente_nao_derruba_o_relatorio(projeto, tmp_path, monkeypatch, capsys):
    """O entregável de sexta é a leitura da watchlist; a varredura é extra."""
    args, raiz = projeto
    universo = _universo_de_teste(tmp_path)
    monkeypatch.setattr("src.screener.load_universes",
                        lambda caminho: __import__("src.screener", fromlist=["load_universes"])
                        .parse_universes(__import__("yaml").safe_load(universo.read_text()), str(universo)))
    assert main([*args, "report", "--offline", "--screen", "--universes", "nao_existe"]) == 0
    assert "não existe" in capsys.readouterr().err
    assert list((raiz / "reports").glob("*.html"))


def test_notify_manda_a_lista_de_momentum_lida_do_cache(projeto, monkeypatch, capsys):
    """Sem varredura: `wyckoff notify` ranqueia o que já está no cache."""
    from src.screener import Universe

    args, raiz = projeto
    semanas = pd.date_range("2025-01-06", periods=60, freq="7D", name="week_start")
    closes = [10.0 * 1.02 ** i for i in range(60)]
    bars = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": 1_000_000.0, "is_partial": False}, index=semanas)
    with Cache(raiz / "wyckoff.sqlite") as cache:
        cache.upsert_bars("SOBE.SA", bars)
    monkeypatch.setattr("src.screener.load_universes", lambda caminho: {
        "b3_completa": Universe("b3_completa", "b3", "^BVSP", ("SOBE.SA",))})
    assert main([*args, "notify", "--dry-run", "--force"]) == 0
    saida = capsys.readouterr().out
    assert "TOP 1 MOMENTUM — B3" in saida and "1. SOBE.SA +" in saida
