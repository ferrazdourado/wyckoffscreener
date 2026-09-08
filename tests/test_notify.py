"""R9 — notificação. Nenhum teste abre socket: o transporte é injetado."""

import datetime as dt

import pytest

from src.notify import (
    EmailNotifier,
    Message,
    NotifyError,
    TelegramNotifier,
    build_message,
    build_notifier,
    notify,
)
from src.report import build_model
from src.watchlist import Invalidation, WatchItem, Watchlist
from tests.conftest import ranged_bars, set_bar, with_metrics

AGORA = dt.datetime(2026, 9, 7, 20, 0)


def item(symbol="PETR4.SA", invalidation=None):
    return WatchItem(symbol=symbol, market="b3", benchmark="^BVSP", invalidation=invalidation)


def modelo(config, bars=None, invalidation=None):
    bars = ranged_bars(25) if bars is None else bars
    df = with_metrics(bars, config)
    df["is_partial"] = False
    wl = Watchlist(items=[item(invalidation=invalidation)])
    return build_model(wl, {"PETR4.SA": df}, config, AGORA)


@pytest.fixture
def telegram_config(config, monkeypatch):
    monkeypatch.setenv("WYCKOFF_TELEGRAM_TOKEN", "123:ABC")
    monkeypatch.setenv("WYCKOFF_TELEGRAM_CHAT_ID", "42")
    config.data["notify"]["backend"] = "telegram"
    return config


# --------------------------- formatação ---------------------------

def test_resumo_traz_semana_fase_e_volume(config):
    m = modelo(config)
    msg = build_message(m, config)
    assert m["tag"] in msg.subject                     # a etiqueta vem dos dados
    assert msg.body.startswith(f"Wyckoff — semana {m['tag']}")
    assert "PETR4.SA" in msg.body
    assert "vol " in msg.body


def test_invalidacao_vem_antes_de_tudo(config):
    """Quem lê no celular na sexta precisa ver o que exige ação primeiro."""
    m = modelo(config, invalidation=Invalidation(price=11.5, direction="below"))
    corpo = build_message(m, config).body
    assert corpo.index("INVALIDAÇÕES") < corpo.index("EVENTOS DA SEMANA")
    assert corpo.index("INVALIDAÇÕES") < corpo.index("WATCHLIST")
    assert "⚠ PETR4.SA" in corpo      # marcada também na tabela


def test_invalidacao_nova_ganha_marca(config):
    bars = set_bar(ranged_bars(25), 24, low=9.5, close=10.6, volume=80.0)
    m = modelo(config, bars=bars, invalidation=Invalidation(price=10.8, direction="below"))
    assert "[NOVO]" in build_message(m, config).body


def test_semana_sem_evento_diz_isso(config):
    assert "EVENTOS DA SEMANA: nenhum" in build_message(modelo(config), config).body


def test_evento_da_semana_aparece(config):
    bars = set_bar(ranged_bars(25), 24, low=9.5, volume=80.0)
    corpo = build_message(modelo(config, bars=bars), config).body
    assert "Spring" in corpo


def test_truncamento_corta_em_linha_inteira(config):
    """Watchlist grande não pode virar meia linha de tabela no fim da mensagem."""
    df = with_metrics(ranged_bars(25), config)
    df["is_partial"] = False
    simbolos = [f"TICK{i}.SA" for i in range(40)]
    wl = Watchlist(items=[item(s) for s in simbolos])
    m = build_model(wl, {s: df for s in simbolos}, config, AGORA)

    completo = build_message(m, config).body
    assert len(completo) > 300 and "truncado" not in completo

    corpo = build_message(m, config, max_chars=300).body
    assert len(corpo) <= 300
    assert "truncado" in corpo
    # a última linha de conteúdo antes do aviso é uma linha de papel inteira
    conteudo = [l for l in corpo.splitlines() if l.startswith("• ")]
    assert conteudo[-1].endswith("%") or conteudo[-1].endswith("—")


def test_erros_de_coleta_entram_no_resumo(config):
    from src.pipeline import SymbolStatus

    df = with_metrics(ranged_bars(25), config)
    df["is_partial"] = False
    wl = Watchlist(items=[item(), item("XPTO.SA")])
    m = build_model(wl, {"PETR4.SA": df}, config, AGORA,
                    fetch_errors=[SymbolStatus("XPTO.SA", "error", 0, "não existe")])
    assert "ERROS DE COLETA (1): XPTO.SA" in build_message(m, config).body


# --------------------------- segredos ---------------------------

def test_segredo_ausente_diz_qual_variavel_exportar(config, monkeypatch):
    monkeypatch.delenv("WYCKOFF_TELEGRAM_TOKEN", raising=False)
    config.data["notify"]["backend"] = "telegram"
    with pytest.raises(NotifyError) as erro:
        build_notifier(config)
    assert "WYCKOFF_TELEGRAM_TOKEN" in str(erro.value)
    assert "export" in str(erro.value)


def test_segredo_nunca_vem_do_arquivo(config, monkeypatch):
    """Pôr o token direto no config não funciona — é o ponto da indireção."""
    monkeypatch.delenv("WYCKOFF_TELEGRAM_TOKEN", raising=False)
    config.data["notify"]["telegram"]["token_env"] = "123:TOKEN-LITERAL"
    config.data["notify"]["backend"] = "telegram"
    with pytest.raises(NotifyError):
        build_notifier(config)


def test_backend_desconhecido_lista_os_validos(config):
    config.data["notify"]["backend"] = "pombo-correio"
    with pytest.raises(NotifyError) as erro:
        build_notifier(config)
    assert "telegram" in str(erro.value) and "email" in str(erro.value)


# --------------------------- telegram ---------------------------

def test_telegram_envia_para_o_chat(telegram_config):
    enviados = []

    def falso(url, payload):
        enviados.append((url, payload))
        return {"ok": True}

    destino = notify(modelo(telegram_config), telegram_config, send_fn=falso)
    url, payload = enviados[0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert payload["chat_id"] == "42"
    assert "Wyckoff" in payload["text"]
    assert destino == "Telegram, chat 42"


def test_telegram_recusado_vira_erro_legivel(telegram_config):
    def recusa(url, payload):
        return {"ok": False, "description": "chat not found"}

    with pytest.raises(NotifyError) as erro:
        notify(modelo(telegram_config), telegram_config, send_fn=recusa)
    assert "chat not found" in str(erro.value)


# --------------------------- e-mail ---------------------------

@pytest.fixture
def email_config(config, monkeypatch):
    monkeypatch.setenv("WYCKOFF_SMTP_USER", "eu@exemplo.com")
    monkeypatch.setenv("WYCKOFF_SMTP_PASSWORD", "senha-de-app")
    config.data["notify"]["backend"] = "email"
    config.data["notify"]["email"]["to"] = ["eu@exemplo.com"]
    return config


def test_email_monta_a_mensagem(email_config):
    enviadas = []
    destino = notify(modelo(email_config), email_config, send_fn=enviadas.append)
    mail = enviadas[0]
    assert mail["To"] == "eu@exemplo.com"
    assert mail["From"] == "eu@exemplo.com"
    assert mail["Subject"].startswith("[Wyckoff] Wyckoff ")
    assert modelo(email_config)["tag"] in mail["Subject"]
    assert "PETR4.SA" in mail.get_content()
    assert destino == "e-mail para eu@exemplo.com"


def test_email_sem_destinatario_falha_cedo(email_config):
    email_config.data["notify"]["email"]["to"] = []
    with pytest.raises(NotifyError) as erro:
        build_notifier(email_config)
    assert "destinatário" in str(erro.value)


def test_email_aceita_destinatario_unico_como_texto(email_config):
    email_config.data["notify"]["email"]["to"] = "eu@exemplo.com"
    assert EmailNotifier(email_config, send_fn=lambda m: None).to == ["eu@exemplo.com"]


def test_sender_explicito_prevalece(email_config):
    email_config.data["notify"]["email"]["sender"] = "bot@exemplo.com"
    enviadas = []
    notify(modelo(email_config), email_config, send_fn=enviadas.append)
    assert enviadas[0]["From"] == "bot@exemplo.com"
