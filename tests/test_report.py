"""R8 — geração do relatório. Nenhum teste toca a rede."""

import datetime as dt

import pytest

from src.report import DISCLAIMER, build_model, generate
from src.watchlist import CalendarEvent, Invalidation, WatchItem, Watchlist
from tests.conftest import ranged_bars, set_bar, with_metrics

AGORA = dt.datetime(2026, 9, 7, 20, 0)


def item(symbol="PETR4.SA", invalidation=None, calendar=(), notes=""):
    return WatchItem(symbol=symbol, market="b3", benchmark="^BVSP",
                     invalidation=invalidation, calendar=tuple(calendar), notes=notes)


def metrics(bars, config):
    df = with_metrics(bars, config)
    df["is_partial"] = False
    return df


@pytest.fixture
def cenario(config, tmp_path):
    """Um papel em Fase C com spring na última semana e invalidação violada.

    O spring fecha de volta em 10,60, acima do suporte 10,00 (confirmado):
    fechar ABAIXO do suporte estouraria a dispersão do range e a lateralização
    terminaria uma semana antes, deixando a última barra sem range ativo — e
    sem spring. A invalidação em 10,80 fica entre os 11,00 das semanas
    anteriores e esse fechamento, para a violação sair como nova.
    """
    config.data["output"]["reports_dir"] = str(tmp_path / "reports")
    bars = set_bar(ranged_bars(25), 24, low=9.5, close=10.6, volume=80.0)
    wl = Watchlist(items=[item(invalidation=Invalidation(price=10.8, direction="below"),
                              calendar=[CalendarEvent(dt.date(2026, 9, 15), "balanço")],
                              notes="teste")])
    return config, wl, {"PETR4.SA": metrics(bars, config)}


def test_modelo_tem_todas_as_secoes_da_spec(cenario):
    config, wl, m = cenario
    model = build_model(wl, m, config, AGORA)
    for chave in ("invalidations", "week_events", "rows", "sections", "errors", "calendar"):
        assert chave in model
    assert model["rows"][0]["symbol"] == "PETR4.SA"
    assert model["disclaimer"] == DISCLAIMER


def test_invalidacao_violada_entra_no_modelo(cenario):
    config, wl, m = cenario
    model = build_model(wl, m, config, AGORA)
    assert len(model["invalidations"]) == 1
    assert model["invalidations"][0].is_new
    assert model["rows"][0]["violated"] is True


def test_calendario_dentro_do_horizonte(cenario):
    config, wl, m = cenario
    model = build_model(wl, m, config, AGORA)
    assert [ev.label for _, ev, _ in model["calendar"]] == ["balanço"]


def test_evento_da_semana_aparece_com_os_numeros(cenario):
    config, wl, m = cenario
    model = build_model(wl, m, config, AGORA)
    assert len(model["week_events"]) == 1
    evento = model["week_events"][0]["event"]
    assert evento.kind == "spring"
    assert evento.audit_lines()


def test_renderiza_markdown_e_html(cenario):
    config, wl, m = cenario
    paths, _model = generate(wl, m, config, AGORA)
    assert paths.markdown.exists() and paths.html.exists()
    md = paths.markdown.read_text(encoding="utf-8")
    html = paths.html.read_text(encoding="utf-8")
    for texto in (md, html):
        assert "PETR4.SA" in texto
        assert DISCLAIMER in texto
        assert "Fase C" in texto
    assert md.startswith("# Wyckoff Screener")
    assert html.lstrip().startswith("<!doctype html>")
    assert 'name="viewport"' in html          # legível no celular (R8)


def test_html_sai_autocontido_com_os_graficos_embutidos(cenario):
    config, wl, m = cenario
    paths, _ = generate(wl, m, config, AGORA)
    html = paths.html.read_text(encoding="utf-8")
    assert "data:image/png;base64," in html
    assert (paths.charts_dir / "charts" / "PETR4_SA.png").exists()


def test_html_pode_referenciar_o_png_em_vez_de_embutir(cenario):
    config, wl, m = cenario
    config.data["output"]["embed_charts"] = False
    paths, _ = generate(wl, m, config, AGORA)
    html = paths.html.read_text(encoding="utf-8")
    assert "data:image/png;base64," not in html
    assert "charts/PETR4_SA.png" in html


def test_nome_do_arquivo_vem_da_semana_analisada(cenario):
    """Rodar sexta à noite ou segunda de manhã escreve o mesmo arquivo."""
    config, wl, m = cenario
    sexta, _ = generate(wl, m, config, dt.datetime(2026, 6, 26, 20, 0))
    segunda, _ = generate(wl, m, config, dt.datetime(2026, 6, 29, 9, 0))
    assert sexta.markdown == segunda.markdown


def test_erros_de_coleta_chegam_ao_relatorio(cenario):
    from src.pipeline import SymbolStatus

    config, wl, m = cenario
    erro = SymbolStatus("XPTO.SA", "error", 0, "ticker inexistente")
    paths, model = generate(wl, m, config, AGORA, fetch_errors=[erro])
    assert model["errors"] == [erro]
    for texto in (paths.markdown.read_text(encoding="utf-8"),
                  paths.html.read_text(encoding="utf-8")):
        assert "XPTO.SA" in texto and "ticker inexistente" in texto


def test_watchlist_sem_niveis_avisa(config, tmp_path):
    """Pendência da spec §8: sem níveis, a seção 1 não tem como alertar nada."""
    config.data["output"]["reports_dir"] = str(tmp_path / "reports")
    wl = Watchlist(items=[item()])
    m = {"PETR4.SA": metrics(ranged_bars(25), config)}
    paths, model = generate(wl, m, config, AGORA)
    assert model["levels_configured"] == 0 and model["levels_total"] == 1
    assert "sem nível" in paths.markdown.read_text(encoding="utf-8")


def test_semana_sem_evento_nao_quebra_o_relatorio(config, tmp_path):
    config.data["output"]["reports_dir"] = str(tmp_path / "reports")
    wl = Watchlist(items=[item()])
    m = {"PETR4.SA": metrics(ranged_bars(25), config)}
    paths, model = generate(wl, m, config, AGORA)
    assert model["week_events"] == []
    assert "Nenhum evento Wyckoff" in paths.markdown.read_text(encoding="utf-8")


def test_secao_de_erros_traz_uma_linha_por_papel(config, tmp_path):
    """Um ticker quebrado falha em três etapas; a seção 5 mostra a causa, não o eco."""
    from src.pipeline import SymbolStatus

    config.data["output"]["reports_dir"] = str(tmp_path / "reports")
    wl = Watchlist(items=[item("PETR4.SA"), item("XPTO.SA")])
    m = {"PETR4.SA": metrics(ranged_bars(25), config)}
    erro = SymbolStatus("XPTO.SA", "error", 0, "XPTO.SA: ticker inexistente")
    paths, model = generate(wl, m, config, AGORA, fetch_errors=[erro])

    problemas = model["problems"]
    assert [p["symbol"] for p in problemas] == ["XPTO.SA"]
    assert problemas[0]["origem"] == "coleta"          # a causa a montante vence
    assert problemas[0]["message"] == "ticker inexistente"   # sem o símbolo repetido

    md = paths.markdown.read_text(encoding="utf-8")
    secao = md.split("## 5. Erros de coleta")[1]
    assert secao.count("XPTO.SA") == 1
