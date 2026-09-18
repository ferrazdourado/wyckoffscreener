"""R9 — notificação. Nenhum teste abre socket: o transporte é injetado."""

import datetime as dt
from pathlib import Path

import pytest

from src.notify import (
    EmailNotifier,
    NotifyError,
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
    msg = build_message(m)
    assert m["tag"] in msg.subject                     # a etiqueta vem dos dados
    assert msg.body.startswith(f"Wyckoff — semana {m['tag']}")
    assert "PETR4.SA" in msg.body
    assert "vol " in msg.body


def test_invalidacao_vem_antes_de_tudo(config):
    """Quem lê no celular na sexta precisa ver o que exige ação primeiro."""
    m = modelo(config, invalidation=Invalidation(price=11.5, direction="below"))
    corpo = build_message(m).body
    assert corpo.index("INVALIDAÇÕES") < corpo.index("EVENTOS DA SEMANA")
    assert corpo.index("INVALIDAÇÕES") < corpo.index("WATCHLIST")
    assert "⚠ PETR4.SA" in corpo      # marcada também na tabela


def test_invalidacao_nova_ganha_marca(config):
    bars = set_bar(ranged_bars(25), 24, low=9.5, close=10.6, volume=80.0)
    m = modelo(config, bars=bars, invalidation=Invalidation(price=10.8, direction="below"))
    assert "[NOVO]" in build_message(m).body


def test_semana_sem_evento_diz_isso(config):
    assert "EVENTOS DA SEMANA: nenhum" in build_message(modelo(config)).body


def test_evento_da_semana_aparece(config):
    bars = set_bar(ranged_bars(25), 24, low=9.5, volume=80.0)
    corpo = build_message(modelo(config, bars=bars)).body
    assert "Spring" in corpo


def test_truncamento_corta_em_linha_inteira(config):
    """Watchlist grande não pode virar meia linha de tabela no fim da mensagem."""
    df = with_metrics(ranged_bars(25), config)
    df["is_partial"] = False
    simbolos = [f"TICK{i}.SA" for i in range(40)]
    wl = Watchlist(items=[item(s) for s in simbolos])
    m = build_model(wl, {s: df for s in simbolos}, config, AGORA)

    completo = build_message(m).body
    assert len(completo) > 300 and "truncado" not in completo

    corpo = build_message(m, max_chars=300).body
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
    assert "ERROS DE COLETA (1): XPTO.SA" in build_message(m).body


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


# --------------------------- anexo do PDF ---------------------------

@pytest.fixture
def com_pdf(telegram_config, tmp_path):
    """Config apontando para um diretório de relatórios com o PDF da semana."""
    telegram_config.data["notify"]["attach_pdf"] = True
    telegram_config.data["output"]["reports_dir"] = str(tmp_path)
    return telegram_config


def pdf_da_semana(config, model, tamanho=2048):
    caminho = Path(config.get("output.reports_dir")) / f"{model['tag']}.pdf"
    caminho.write_bytes(b"%PDF-1.4\n" + b"\0" * tamanho)
    return caminho


def coletor(erro_no_documento=None):
    """Transporte falso: registra as chamadas e devolve o que o teste mandar."""
    enviados = []

    def falso(url, payload, files=None):
        enviados.append((url, payload, files))
        if erro_no_documento and url.endswith("sendDocument"):
            return {"ok": False, "description": erro_no_documento}
        return {"ok": True}

    return enviados, falso


def test_pdf_vai_como_documento_depois_do_texto(com_pdf):
    """Documento separado, não legenda: a legenda corta em 1024 caracteres."""
    m = modelo(com_pdf)
    caminho = pdf_da_semana(com_pdf, m)
    enviados, falso = coletor()

    destino = notify(m, com_pdf, send_fn=falso)

    assert [u.rsplit("/", 1)[1] for u, _, _ in enviados] == ["sendMessage", "sendDocument"]
    _, payload, files = enviados[1]
    assert payload["chat_id"] == "42"
    assert files == [("document", caminho)]
    assert destino.endswith(f"+ {caminho.name}")


def test_sem_pdf_no_disco_manda_só_o_texto(com_pdf):
    """Quem não roda com --pdf não muda de comportamento."""
    enviados, falso = coletor()
    destino = notify(modelo(com_pdf), com_pdf, send_fn=falso)
    assert len(enviados) == 1
    assert destino == "Telegram, chat 42"


def test_attach_pdf_desligado_ignora_o_pdf_existente(com_pdf):
    m = modelo(com_pdf)
    pdf_da_semana(com_pdf, m)
    com_pdf.data["notify"]["attach_pdf"] = False
    enviados, falso = coletor()
    notify(m, com_pdf, send_fn=falso)
    assert len(enviados) == 1


def test_anexo_explícito_vence_o_do_diretório(com_pdf, tmp_path):
    """`report --pdf` sabe o caminho que acabou de escrever; ele manda."""
    m = modelo(com_pdf)
    pdf_da_semana(com_pdf, m)
    outro = tmp_path / "outro.pdf"
    outro.write_bytes(b"%PDF-1.4\n")
    enviados, falso = coletor()

    notify(m, com_pdf, send_fn=falso, attachment=outro)

    assert enviados[1][2] == [("document", outro)]


def test_documento_recusado_não_anula_o_texto_entregue(com_pdf):
    """O resumo já chegou ao celular; dizer 'não enviado' seria mentira."""
    m = modelo(com_pdf)
    pdf_da_semana(com_pdf, m)
    _, falso = coletor(erro_no_documento="file too big")

    destino = notify(m, com_pdf, send_fn=falso)

    assert destino.startswith("Telegram, chat 42")
    assert "PDF não anexado" in destino and "file too big" in destino


def test_pdf_acima_do_limite_do_bot_nem_é_tentado(com_pdf, monkeypatch):
    m = modelo(com_pdf)
    pdf_da_semana(com_pdf, m)
    monkeypatch.setattr("src.notify.TELEGRAM_DOC_MAX_MB", 0.001)   # 1 KB
    enviados, falso = coletor()

    destino = notify(m, com_pdf, send_fn=falso)

    assert len(enviados) == 1                      # só o texto foi à rede
    assert "acima do limite" in destino


def test_multipart_carrega_nome_e_bytes_do_arquivo(tmp_path):
    """Encoder escrito à mão: o teste é o que garante que o corpo é válido."""
    from src.notify import _multipart

    arquivo = tmp_path / "2026-37.pdf"
    arquivo.write_bytes(b"%PDF-1.4\nconteudo")
    corpo, content_type = _multipart({"chat_id": "42"}, [("document", arquivo)])

    boundary = content_type.split("boundary=")[1]
    assert content_type.startswith("multipart/form-data; ")
    assert b'name="chat_id"\r\n\r\n42\r\n' in corpo
    assert b'filename="2026-37.pdf"' in corpo
    assert b"Content-Type: application/pdf" in corpo
    assert b"%PDF-1.4\nconteudo" in corpo
    assert corpo.endswith(f"--{boundary}--\r\n".encode())


def test_email_leva_o_pdf_como_anexo(email_config, tmp_path):
    email_config.data["notify"]["attach_pdf"] = True
    email_config.data["output"]["reports_dir"] = str(tmp_path)
    m = modelo(email_config)
    caminho = pdf_da_semana(email_config, m)
    enviadas = []

    destino = notify(m, email_config, send_fn=enviadas.append, attachment=caminho)

    anexos = [p for p in enviadas[0].iter_attachments()]
    assert len(anexos) == 1
    assert anexos[0].get_filename() == caminho.name
    assert anexos[0].get_content_type() == "application/pdf"
    assert destino.endswith(f"+ {caminho.name}")
