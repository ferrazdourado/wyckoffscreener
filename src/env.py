"""Carga do arquivo `.env` (segredos da rotina).

Por que existe: R9 lê token de bot e senha de SMTP de variável de ambiente, e o
`config.yaml` guarda só o NOME da variável — o arquivo vai para o git, o segredo
não. Só que exportar as variáveis à mão toda sexta é atrito que ninguém mantém;
o `.env` é a convenção que resolve isso, e sem este módulo ele seria um arquivo
que parece configurar e não configura nada.

Três regras, nesta ordem de importância:

1. **O ambiente real vence o arquivo.** Variável já exportada na sessão nunca é
   sobrescrita — assim `WYCKOFF_TELEGRAM_CHAT_ID=outro wyckoff notify` continua
   funcionando para um envio pontual, e um segredo de CI não é substituído pelo
   `.env` esquecido no diretório.
2. **Ausência não é erro.** Quem exporta as variáveis por outro caminho não
   precisa do arquivo; `wyckoff validate` não pode falhar por isso.
3. **Valor nenhum é devolvido ou registrado** — as funções falam em nomes de
   variáveis. Um segredo impresso num log deixa de ser segredo.
"""

from __future__ import annotations

import os
from pathlib import Path

NOME_PADRAO = ".env"
# Raiz do projeto (pai de src/), para o comando funcionar de qualquer diretório.
RAIZ = Path(__file__).resolve().parent.parent


def parse_env(texto: str) -> dict[str, str]:
    """`KEY=valor` por linha -> dicionário. Tolerante, como o formato pede.

    Aceita `export KEY=valor`, aspas simples ou duplas em volta do valor, linhas
    em branco e comentários. Linha sem `=` é ignorada em silêncio: o arquivo é
    editado à mão e um erro de digitação não pode derrubar a rotina inteira.
    """
    valores: dict[str, str] = {}
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.removeprefix("export ").strip()
        if not chave:
            continue
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        else:
            # Comentário no fim da linha só conta com espaço antes do `#`:
            # um token pode legitimamente conter `#` no meio.
            corte = valor.find(" #")
            if corte != -1:
                valor = valor[:corte].rstrip()
        valores[chave] = valor
    return valores


def find_env(explicit: str | Path | None = None) -> Path | None:
    """Arquivo a carregar: o indicado, senão `.env` no diretório atual ou na raiz."""
    if explicit:
        caminho = Path(explicit)
        return caminho if caminho.is_file() else None
    for candidato in (Path.cwd() / NOME_PADRAO, RAIZ / NOME_PADRAO):
        if candidato.is_file():
            return candidato
    return None


def load_env(explicit: str | Path | None = None, environ: dict | None = None) -> list[str]:
    """Põe o que estiver no `.env` no ambiente e devolve os NOMES carregados.

    Nomes, nunca valores — o retorno é usado para dizer ao usuário o que foi
    lido sem imprimir segredo na tela.
    """
    ambiente = os.environ if environ is None else environ
    caminho = find_env(explicit)
    if caminho is None:
        return []
    try:
        texto = caminho.read_text(encoding="utf-8")
    except OSError:
        return []
    carregados = []
    for chave, valor in parse_env(texto).items():
        if chave in ambiente:          # regra 1: o ambiente real vence
            continue
        ambiente[chave] = valor
        carregados.append(chave)
    return carregados
