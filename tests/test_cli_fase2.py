"""CLI da Fase 2: `wyckoff report` e `wyckoff analyze`, sem rede."""

import pathlib

import pandas as pd
import pytest
import yaml

from src.cli import main
from src.data.cache import Cache
from tests.conftest import ranged_bars, set_bar
from tests.test_events import sos_bar


@pytest.fixture
def projeto(tmp_path, config):
    """Um projeto completo em disco: config, watchlist e cache já populado."""
    cache_path = tmp_path / "wyckoff.sqlite"
    bars = set_bar(ranged_bars(25), 22, low=9.5, volume=80.0)
    bars["is_partial"] = False
    with Cache(cache_path) as cache:
        cache.upsert_bars("PETR4.SA", bars)
        cache.upsert_bars("^BVSP", bars)

    cfg = dict(config.data)
    cfg["data"] = {**cfg["data"], "cache_path": str(cache_path)}
    cfg["output"] = {**cfg["output"], "reports_dir": str(tmp_path / "reports"),
                     "csv_dir": str(tmp_path / "exports")}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(yaml.safe_dump(
        # nível acima dos fechamentos de 11,00: a invalidação sai violada
        {"tickers": [{"symbol": "PETR4.SA", "market": "b3",
                      "invalidation": {"price": 11.5, "direction": "below"}}]}
    ), encoding="utf-8")
    return ["--config", str(config_path), "--watchlist", str(watchlist_path)], tmp_path


def test_report_offline_gera_os_dois_arquivos(projeto, capsys):
    args, tmp_path = projeto
    assert main([*args, "report", "--offline"]) == 0
    saida = capsys.readouterr().out
    assert "Markdown:" in saida and "HTML:" in saida
    gerados = sorted(p.name for p in (tmp_path / "reports").iterdir())
    assert any(n.endswith(".md") for n in gerados)
    assert any(n.endswith(".html") for n in gerados)


def test_report_offline_nao_toca_a_rede(projeto, monkeypatch):
    """`--offline` tem de ser realmente offline: qualquer download é erro."""
    args, _ = projeto

    def explode(*a, **k):
        raise AssertionError("o relatório offline tentou baixar dados")

    monkeypatch.setattr("src.data.provider.YFinanceProvider.weekly_bars", explode)
    monkeypatch.setattr("src.data.provider.YFinanceProvider.corporate_actions", explode)
    assert main([*args, "report", "--offline"]) == 0


def test_report_resume_alerta_e_evento_no_terminal(projeto, capsys):
    args, _ = projeto
    main([*args, "report", "--offline"])
    saida = capsys.readouterr().out
    assert "invalidação(ões) violada(s)" in saida
    assert "PETR4.SA" in saida


def test_analyze_de_um_papel(projeto, capsys):
    args, _ = projeto
    assert main([*args, "analyze", "PETR4.SA"]) == 0
    saida = capsys.readouterr().out
    assert "Fase C" in saida
    assert "Spring" in saida
    assert "próximo evento esperado" in saida
    assert "INVALIDAÇÃO VIOLADA" in saida


def test_analyze_ticker_desconhecido_falha_com_mensagem(projeto, capsys):
    args, _ = projeto
    assert main([*args, "analyze", "XPTO.SA"]) == 1
    assert "sem análise" in capsys.readouterr().err


def test_analyze_sem_ticker_percorre_a_watchlist(projeto, capsys):
    args, _ = projeto
    assert main([*args, "analyze"]) == 0
    assert "PETR4.SA" in capsys.readouterr().out


# --------------------------- R9: notificação ---------------------------

def test_notify_desligado_no_config_nao_envia(projeto, capsys):
    """`notify.enabled: false` é o default: nada sai sem pedido explícito."""
    args, _ = projeto
    assert main([*args, "notify"]) == 2
    assert "notify.enabled" in capsys.readouterr().err


def test_notify_dry_run_imprime_sem_enviar(projeto, capsys, monkeypatch):
    args, _ = projeto

    def explode(*a, **k):
        raise AssertionError("dry-run tentou enviar de verdade")

    monkeypatch.setattr("src.notify.TelegramNotifier.send", explode)
    monkeypatch.setattr("src.notify.EmailNotifier.send", explode)
    assert main([*args, "notify", "--dry-run", "--force"]) == 0
    saida = capsys.readouterr().out
    assert "--- corpo" in saida and "PETR4.SA" in saida


def test_report_nao_notifica_sem_a_flag(projeto, monkeypatch):
    """Gerar relatório nunca dispara mensagem por conta própria."""
    args, _ = projeto

    def explode(*a, **k):
        raise AssertionError("report sem --notify tentou notificar")

    monkeypatch.setattr("src.notify.notify", explode)
    assert main([*args, "report", "--offline"]) == 0


def test_falha_de_envio_nao_invalida_o_relatorio(projeto, capsys, monkeypatch):
    """O relatório já está em disco; o envio que falhou vira aviso, não perda."""
    from src.notify import NotifyError

    args, tmp_path = projeto

    def recusa(*a, **k):
        raise NotifyError("chat not found")

    monkeypatch.setattr("src.notify.notify", recusa)
    assert main([*args, "report", "--offline", "--notify"]) == 1
    assert "Notificação NÃO enviada" in capsys.readouterr().err
    assert any(p.suffix == ".html" for p in (tmp_path / "reports").iterdir())


# --------------------------- R12: screener ---------------------------

@pytest.fixture
def projeto_com_universo(projeto, tmp_path):
    """O projeto base mais um universe.yaml apontando para o cache já povoado."""
    import yaml as _yaml

    caminho = tmp_path / "universe.yaml"
    caminho.write_text(_yaml.safe_dump(
        {"universes": {"meu": {"market": "b3", "benchmark": "^BVSP",
                               "tickers": ["PETR4.SA"]}}}
    ), encoding="utf-8")
    args, _ = projeto
    # O papel sintético negocia R$ 1.000 por semana — qualquer piso realista o
    # descartaria, e o que estes testes medem é o ranqueamento.
    config_path = pathlib.Path(args[1])
    cfg = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
    cfg.setdefault("screener", {})["min_weekly_volume"] = 0
    config_path.write_text(_yaml.safe_dump(cfg), encoding="utf-8")
    return args, caminho


def test_screen_offline_ranqueia_e_exporta(projeto_com_universo, capsys, tmp_path):
    """O papel sintético negocia R$ 1.000 por semana; o piso de liquidez real
    o descartaria, e o que este teste mede é o ranqueamento."""
    args, universo = projeto_com_universo
    assert main([*args, "screen", "meu", "--universe-file", str(universo), "--offline",
                 "--phases", "B,C,D"]) == 0
    saida = capsys.readouterr().out
    assert "PETR4.SA" in saida and "candidato" in saida
    assert any(p.name.startswith("screen_meu_") for p in (tmp_path / "exports").iterdir())


def test_screen_offline_nao_toca_a_rede(projeto_com_universo, monkeypatch):
    args, universo = projeto_com_universo

    def explode(*a, **k):
        raise AssertionError("screen --offline tentou baixar dados")

    monkeypatch.setattr("src.data.provider.YFinanceProvider.weekly_bars", explode)
    assert main([*args, "screen", "meu", "--universe-file", str(universo), "--offline"]) == 0


def test_screen_universo_inexistente_lista_os_validos(projeto_com_universo, capsys):
    args, universo = projeto_com_universo
    assert main([*args, "screen", "xpto", "--universe-file", str(universo), "--offline"]) == 2
    assert "meu" in capsys.readouterr().err


def test_universe_lista_os_blocos(projeto_com_universo, capsys):
    args, universo = projeto_com_universo
    assert main([*args, "universe", "--universe-file", str(universo)]) == 0
    saida = capsys.readouterr().out
    assert "meu" in saida and "1 papéis" in saida


# --------------------------- R10 / R11 ---------------------------

@pytest.fixture
def projeto_longo(tmp_path, config):
    """Série longa o bastante para o backtest ter futuro que medir.

    A fixture `projeto` tem 25 semanas: descontado o warm-up de 21 das médias,
    sobram 4 semanas e nenhum horizonte cabe. Aqui são 60 semanais mais os
    pregões diários que a contagem de P&F precisa.
    """
    cache_path = tmp_path / "longo.sqlite"
    semanal = sos_bar(ranged_bars(60), i=40)
    semanal["is_partial"] = False
    # O diário precisa cobrir TODO o intervalo do semanal — 60 semanas são 420
    # dias corridos. Cobrindo menos, o range em vigor cai fora da janela diária
    # e a contagem volta silenciosamente para o candle semanal.
    dias = 7 * len(semanal)
    diario = ranged_bars(dias, low=10.0, high=12.0, close=11.0)
    diario.index = pd.date_range(semanal.index[0], periods=dias, freq="D")
    with Cache(cache_path) as cache:
        for simbolo in ("PETR4.SA", "^BVSP"):
            cache.upsert_bars(simbolo, semanal)
            cache.upsert_daily(simbolo, diario)

    cfg = dict(config.data)
    cfg["data"] = {**cfg["data"], "cache_path": str(cache_path)}
    cfg["output"] = {**cfg["output"], "reports_dir": str(tmp_path / "reports"),
                     "csv_dir": str(tmp_path / "exports")}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(yaml.safe_dump(
        {"tickers": [{"symbol": "PETR4.SA", "market": "b3"}]}), encoding="utf-8")
    return ["--config", str(config_path), "--watchlist", str(watchlist_path)]


def test_analyze_mostra_a_contagem_de_causa(projeto_longo, capsys):
    """R10 no terminal: a projeção sai com a conta aberta."""
    assert main([*projeto_longo, "analyze", "PETR4.SA"]) == 0
    saida = capsys.readouterr().out
    assert "contagem de causa" in saida
    assert "linha de contagem" in saida
    assert "insumo: candle diário" in saida


def test_backtest_traz_a_linha_de_base_e_o_disclaimer(projeto_longo, capsys):
    assert main([*projeto_longo, "backtest", "--horizons", "4"]) == 0
    saida = capsys.readouterr().out
    assert "_qualquer_semana" in saida
    assert "não backtest de estratégia" in saida


def test_backtest_fast_avisa_do_lookahead(projeto_longo, capsys):
    """O modo rápido é utilizável, desde que o usuário saiba do viés."""
    assert main([*projeto_longo, "backtest", "--horizons", "4", "--fast"]) == 0
    assert "lookahead" in capsys.readouterr().out.lower()


def test_backtest_nao_toca_a_rede(projeto_longo, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("backtest tentou baixar dados")

    monkeypatch.setattr("src.data.provider.YFinanceProvider.daily_bars", explode)
    assert main([*projeto_longo, "backtest", "--horizons", "4"]) == 0
