"""P2 — exportação para PDF: escolha de motor, marcador de fim e limpeza.

Nenhum teste abre navegador: os motores são substituídos por funções que
escrevem (ou não) um arquivo. O caminho real com o Chrome foi exercitado à mão
em 08/09/2026 — 17 páginas, 12 gráficos embutidos.
"""

from pathlib import Path

import pytest

from src.config import Config, load_config
from src.pdf import (
    INSTALL_HINT,
    PdfError,
    expand_details,
    find_chrome,
    pdf_completo,
    to_pdf,
)

HTML = """<!doctype html><html><body>
<details><summary>números</summary><ul><li>volume 2.1×</li></ul></details>
</body></html>"""


@pytest.fixture
def relatorio(tmp_path) -> Path:
    caminho = tmp_path / "2026-36.html"
    caminho.write_text(HTML, encoding="utf-8")
    return caminho


def config(**pdf) -> Config:
    return Config({"output": {"pdf": {"engines": ["falso"], "timeout": 5, **pdf}}})


def motor_ok(visto: list):
    def motor(html_path, pdf_path, cfg):
        visto.append(html_path.read_text(encoding="utf-8"))
        pdf_path.write_bytes(b"%PDF-1.4 ... %%EOF")
    return motor


def motor_quebrado(mensagem="falhou"):
    def motor(html_path, pdf_path, cfg):
        raise PdfError(mensagem)
    return motor


# --------------------------- números no papel ---------------------------

def test_details_abre_para_a_versao_impressa():
    """No papel não há o que clicar; a conta do sinal tem de estar visível."""
    assert "<details open>" in expand_details(HTML)


def test_details_ja_aberto_nao_e_alterado():
    assert expand_details("<details open>x</details>") == "<details open>x</details>"


def test_motor_recebe_o_html_com_os_numeros_abertos(relatorio, monkeypatch):
    vistos = []
    monkeypatch.setattr("src.pdf.ENGINES", {"falso": motor_ok(vistos)})
    to_pdf(relatorio, config=config())
    assert "<details open>" in vistos[0]


def test_html_original_nao_e_modificado(relatorio, monkeypatch):
    monkeypatch.setattr("src.pdf.ENGINES", {"falso": motor_ok([])})
    to_pdf(relatorio, config=config())
    assert relatorio.read_text(encoding="utf-8") == HTML


def test_arquivo_temporario_da_impressao_nao_fica_para_tras(relatorio, monkeypatch):
    monkeypatch.setattr("src.pdf.ENGINES", {"falso": motor_ok([])})
    to_pdf(relatorio, config=config())
    assert list(relatorio.parent.glob(".*-print.html")) == []


def test_temporario_some_mesmo_quando_todo_motor_falha(relatorio, monkeypatch):
    monkeypatch.setattr("src.pdf.ENGINES", {"falso": motor_quebrado()})
    with pytest.raises(PdfError):
        to_pdf(relatorio, config=config())
    assert list(relatorio.parent.glob(".*-print.html")) == []


def test_expandir_pode_ser_desligado(relatorio, monkeypatch):
    vistos = []
    monkeypatch.setattr("src.pdf.ENGINES", {"falso": motor_ok(vistos)})
    to_pdf(relatorio, config=config(expand_details=False))
    assert "<details open>" not in vistos[0]


# --------------------------- escolha de motor ---------------------------

def test_primeiro_motor_que_funciona_vence_e_e_identificado(relatorio, monkeypatch):
    monkeypatch.setattr("src.pdf.ENGINES", {"a": motor_quebrado("sem navegador"),
                                            "b": motor_ok([])})
    caminho, motor = to_pdf(relatorio, config=Config(
        {"output": {"pdf": {"engines": ["a", "b"]}}}))
    assert motor == "b" and caminho.exists()


def test_motor_que_termina_sem_escrever_nada_nao_conta_como_sucesso(relatorio, monkeypatch):
    monkeypatch.setattr("src.pdf.ENGINES", {"mudo": lambda h, p, c: None, "bom": motor_ok([])})
    _, motor = to_pdf(relatorio, config=Config({"output": {"pdf": {"engines": ["mudo", "bom"]}}}))
    assert motor == "bom"


def test_todos_falhando_ensina_o_que_instalar(relatorio, monkeypatch):
    monkeypatch.setattr("src.pdf.ENGINES", {"a": motor_quebrado("sem chrome"),
                                            "b": motor_quebrado("sem weasyprint")})
    with pytest.raises(PdfError) as erro:
        to_pdf(relatorio, config=Config({"output": {"pdf": {"engines": ["a", "b"]}}}))
    assert "sem chrome" in str(erro.value) and INSTALL_HINT in str(erro.value)


def test_motor_inexistente_falha_antes_de_tentar(relatorio):
    with pytest.raises(PdfError, match="motor de PDF desconhecido"):
        to_pdf(relatorio, engines=["ghostscript"])


def test_html_ausente_manda_gerar_o_relatorio(tmp_path):
    with pytest.raises(PdfError, match="wyckoff report"):
        to_pdf(tmp_path / "nao-existe.html")


def test_destino_padrao_fica_ao_lado_do_html(relatorio, monkeypatch):
    monkeypatch.setattr("src.pdf.ENGINES", {"falso": motor_ok([])})
    caminho, _ = to_pdf(relatorio, config=config())
    assert caminho == relatorio.with_suffix(".pdf")


# --------------------------- marcador de fim ---------------------------

def test_pdf_sem_marcador_de_fim_ainda_esta_sendo_escrito(tmp_path):
    parcial = tmp_path / "meio.pdf"
    parcial.write_bytes(b"%PDF-1.4" + b"0" * 100)
    assert not pdf_completo(parcial)


def test_pdf_com_marcador_de_fim_esta_pronto(tmp_path):
    pronto = tmp_path / "pronto.pdf"
    pronto.write_bytes(b"%PDF-1.4" + b"0" * 100 + b"\n%%EOF\n")
    assert pdf_completo(pronto)


def test_arquivo_inexistente_nao_e_pdf_pronto(tmp_path):
    assert not pdf_completo(tmp_path / "nada.pdf")


# --------------------------- navegador ---------------------------

def test_caminho_configurado_que_nao_existe_falha_explicitamente():
    with pytest.raises(PdfError, match="chrome_binary"):
        find_chrome(Config({"output": {"pdf": {"chrome_binary": "/nao/existe/chrome"}}}))


def test_caminho_configurado_vence_a_busca(tmp_path):
    falso = tmp_path / "meu-chrome"
    falso.write_text("#!/bin/sh\n")
    assert find_chrome(Config({"output": {"pdf": {"chrome_binary": str(falso)}}})) == str(falso)


def test_config_de_producao_lista_os_tres_motores():
    """Quem edita o config.yaml não pode deixar o default fora de sintonia."""
    from src.pdf import ENGINES
    assert set(load_config("config.yaml").get("output.pdf.engines")) == set(ENGINES)
