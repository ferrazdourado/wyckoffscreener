"""Carga do `.env`: precedência, parsing tolerante e sigilo do valor."""

import pytest

from src import env
from src.env import find_env, load_env, parse_env


@pytest.fixture(autouse=True)
def sem_env_real(tmp_path, monkeypatch):
    """Isola do `.env` de verdade do projeto — senão o teste lê segredo do dono."""
    monkeypatch.setattr(env, "RAIZ", tmp_path / "raiz-inexistente")
    monkeypatch.chdir(tmp_path)


def escrever(tmp_path, texto: str):
    caminho = tmp_path / ".env"
    caminho.write_text(texto, encoding="utf-8")
    return caminho


# --------------------------- parsing ---------------------------

def test_linha_simples():
    assert parse_env("WYCKOFF_TELEGRAM_CHAT_ID=123") == {"WYCKOFF_TELEGRAM_CHAT_ID": "123"}


def test_prefixo_export_e_espacos():
    assert parse_env("export  TOKEN = abc ") == {"TOKEN": "abc"}


def test_aspas_saem_do_valor():
    assert parse_env("A='um dois'\nB=\"três\"") == {"A": "um dois", "B": "três"}


def test_comentario_e_linha_em_branco_sao_ignorados():
    assert parse_env("# comentário\n\nA=1\n") == {"A": "1"}


def test_cerquilha_dentro_do_valor_continua_no_valor():
    """Token pode conter `#`; só conta como comentário depois de um espaço."""
    assert parse_env("T=abc#def")["T"] == "abc#def"
    assert parse_env("T=abc # sobra")["T"] == "abc"


def test_cerquilha_dentro_de_aspas_nunca_e_comentario():
    assert parse_env("T='abc # def'")["T"] == "abc # def"


def test_linha_sem_igual_nao_derruba_o_arquivo():
    """O arquivo é editado à mão: erro de digitação não pode matar a rotina."""
    assert parse_env("lixo\nA=1") == {"A": "1"}


def test_valor_vazio_e_valor_valido():
    assert parse_env("A=") == {"A": ""}


# --------------------------- precedência ---------------------------

def test_ambiente_real_vence_o_arquivo(tmp_path):
    """Permite `VAR=outro wyckoff notify` para um envio pontual."""
    escrever(tmp_path, "TOKEN=do-arquivo")
    ambiente = {"TOKEN": "do-ambiente"}
    carregados = load_env(environ=ambiente)
    assert ambiente["TOKEN"] == "do-ambiente"
    assert carregados == []


def test_variavel_ausente_e_preenchida_pelo_arquivo(tmp_path):
    escrever(tmp_path, "TOKEN=do-arquivo")
    ambiente = {}
    assert load_env(environ=ambiente) == ["TOKEN"]
    assert ambiente["TOKEN"] == "do-arquivo"


# --------------------------- ausência e sigilo ---------------------------

def test_sem_arquivo_nao_e_erro(tmp_path):
    """Quem exporta por outro caminho não precisa do arquivo."""
    ambiente = {}
    assert load_env(environ=ambiente) == []
    assert ambiente == {}


def test_arquivo_indicado_que_nao_existe_e_ignorado(tmp_path):
    assert load_env(tmp_path / "nao-existe", environ={}) == []


def test_retorno_traz_nomes_e_nunca_valores(tmp_path):
    """Um segredo impresso em log deixa de ser segredo."""
    escrever(tmp_path, "SENHA=abracadabra")
    carregados = load_env(environ={})
    assert carregados == ["SENHA"]
    assert "abracadabra" not in "".join(carregados)


def test_arquivo_indicado_vence_o_do_diretorio(tmp_path):
    escrever(tmp_path, "QUEM=diretorio")
    outro = tmp_path / "outro.env"
    outro.write_text("QUEM=indicado", encoding="utf-8")
    ambiente = {}
    load_env(outro, environ=ambiente)
    assert ambiente["QUEM"] == "indicado"


def test_raiz_do_projeto_e_o_segundo_lugar_procurado(tmp_path, monkeypatch):
    """Roda de qualquer diretório: o `.env` mora junto do config."""
    raiz = tmp_path / "projeto"
    raiz.mkdir()
    (raiz / ".env").write_text("ONDE=raiz", encoding="utf-8")
    outro = tmp_path / "outro-dir"
    outro.mkdir()
    monkeypatch.setattr(env, "RAIZ", raiz)
    monkeypatch.chdir(outro)
    assert find_env() == raiz / ".env"
