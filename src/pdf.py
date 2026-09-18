"""Exportação do relatório para PDF (P2 da spec).

O relatório já é gerado em HTML autocontido (gráficos em base64), então o PDF é
uma conversão, não uma terceira renderização: Markdown, HTML e PDF continuam
contando a mesma história, que é a razão de o modelo de dados ser único.

Três motores, tentados na ordem que o config define:

* **chrome** — Chrome/Chromium/Edge em modo headless. Primeiro da fila porque
  imprime exatamente o que o navegador mostra, incluindo as fontes e o CSS
  responsivo que já foram pensados para a tela.
* **weasyprint** — biblioteca Python pura em cima do CSS de impressão; boa
  quando não há navegador na máquina, mas exige bibliotecas de sistema
  (pango/cairo).
* **wkhtmltopdf** — binário legado, último recurso.

Se nenhum estiver disponível, a falha diz o que instalar em vez de deixar um
"comando não encontrado" cru chegar ao usuário.

Uma diferença deliberada entre a tela e o papel: no HTML, os números que
justificam cada sinal ficam em `<details>` recolhidos, para a leitura de sexta
não virar uma parede de números. No PDF não há o que clicar — então eles são
abertos antes de imprimir. Auditabilidade é requisito da spec; um PDF que
esconde a conta não serve.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Config

CHROME_PATHS = {
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ],
    "Linux": [
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge", "/snap/bin/chromium",
    ],
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
}
CHROME_COMMANDS = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                   "microsoft-edge", "brave-browser"]

INSTALL_HINT = (
    "nenhum motor de PDF disponível. Instale um destes:\n"
    "  • Google Chrome ou Chromium (nada a configurar; é procurado sozinho)\n"
    "  • pip install weasyprint      (precisa de pango/cairo no sistema)\n"
    "  • brew install wkhtmltopdf    (ou o pacote equivalente da sua distro)\n"
    "Se o navegador estiver num caminho fora do comum, aponte-o em "
    "`output.pdf.chrome_binary`."
)


class PdfError(Exception):
    """Falha na conversão, com a mensagem já pronta para o usuário."""


def expand_details(html: str) -> str:
    """Abre os `<details>` do relatório para a versão impressa.

    Função pura de texto: não reinterpreta o HTML, só troca a tag de abertura.
    """
    return html.replace("<details>", "<details open>")


def find_chrome(config: Config | None = None) -> str | None:
    """Caminho do navegador, na ordem: config, PATH, instalações conhecidas."""
    if config is not None:
        indicado = config.get("output.pdf.chrome_binary")
        if indicado:
            if not Path(str(indicado)).exists():
                raise PdfError(f"`output.pdf.chrome_binary` aponta para um arquivo que não "
                               f"existe: {indicado}")
            return str(indicado)
    for comando in CHROME_COMMANDS:
        achado = shutil.which(comando)
        if achado:
            return achado
    for caminho in CHROME_PATHS.get(platform.system(), []):
        if Path(caminho).exists():
            return caminho
    return None


def _run(comando: list[str], timeout: float, motor: str) -> None:
    try:
        resultado = subprocess.run(comando, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PdfError(f"{motor}: passou de {timeout:.0f}s convertendo o relatório") from exc
    except OSError as exc:
        raise PdfError(f"{motor}: não foi possível executar — {exc}") from exc
    if resultado.returncode != 0:
        erro = (resultado.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        detalhe = erro[-1] if erro else f"código {resultado.returncode}"
        raise PdfError(f"{motor}: falhou — {detalhe}")


def pdf_completo(pdf_path: Path) -> bool:
    """O PDF terminou de ser escrito?

    Um PDF válido acaba no marcador `%%EOF`. É o sinal de conclusão usado no
    lugar da saída do processo — ver `render_with_chrome`.
    """
    try:
        if pdf_path.stat().st_size < 32:
            return False
        with pdf_path.open("rb") as arquivo:
            arquivo.seek(-32, os.SEEK_END)
            return b"%%EOF" in arquivo.read()
    except OSError:
        return False


def render_with_chrome(html_path: Path, pdf_path: Path, config: Config) -> None:
    """Imprime pelo navegador, esperando o ARQUIVO — não a saída do processo.

    Medido em 08/09/2026 (Chrome no macOS): o `--print-to-pdf` escreve o PDF
    inteiro, válido, e o processo continua vivo indefinidamente — 45s, 10min,
    sem sair, em `--headless=new` e `--headless=old`. Esperar pelo `exit code`
    faz o comando pendurar e depois estourar por timeout com o PDF pronto no
    disco, que foi exatamente o que aconteceu na primeira versão daqui.
    Então: espera-se o marcador de fim do PDF e encerra-se o navegador.
    """
    binario = find_chrome(config)
    if binario is None:
        raise PdfError("chrome: nenhum navegador Chromium encontrado")
    timeout = float(config.get("output.pdf.timeout", 120))
    pdf_path.unlink(missing_ok=True)   # senão o PDF da rodada anterior parece pronto

    # Perfil descartável: sem ele, o Chrome headless briga com a janela que o
    # usuário já tem aberta (mesmo diretório de perfil, um só dono).
    with tempfile.TemporaryDirectory(prefix="wyckoff-chrome-") as perfil:
        comando = [
            binario, "--headless=new", "--disable-gpu", f"--user-data-dir={perfil}",
            "--no-first-run", "--no-pdf-header-footer", "--disable-extensions",
            f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
        ]
        try:
            processo = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise PdfError(f"chrome: não foi possível executar — {exc}") from exc
        try:
            limite = time.monotonic() + timeout
            while time.monotonic() < limite:
                if pdf_completo(pdf_path):
                    return
                if processo.poll() is not None:
                    if pdf_completo(pdf_path):
                        return
                    erro = (processo.stderr.read() or b"").decode("utf-8", "replace").strip()
                    ultima = erro.splitlines()[-1] if erro else f"código {processo.returncode}"
                    raise PdfError(f"chrome: saiu sem escrever o PDF — {ultima}")
                time.sleep(0.2)
            raise PdfError(f"chrome: passou de {timeout:.0f}s sem terminar o PDF")
        finally:
            if processo.poll() is None:
                processo.terminate()
                try:
                    processo.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    processo.kill()


def render_with_weasyprint(html_path: Path, pdf_path: Path, config: Config) -> None:
    # `config` fica pela assinatura comum de ENGINES; o weasyprint não tem o
    # que configurar (sem binário, sem timeout: roda no mesmo processo).
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise PdfError("weasyprint: não instalado (`pip install weasyprint`)") from exc
    except OSError as exc:   # pango/cairo ausentes
        raise PdfError(f"weasyprint: instalado, mas sem as bibliotecas de sistema — {exc}") from exc
    try:
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
    except Exception as exc:
        raise PdfError(f"weasyprint: falhou — {exc}") from exc


def render_with_wkhtmltopdf(html_path: Path, pdf_path: Path, config: Config) -> None:
    binario = shutil.which("wkhtmltopdf")
    if binario is None:
        raise PdfError("wkhtmltopdf: não encontrado no PATH")
    _run(
        [binario, "--enable-local-file-access", "--quiet",
         str(html_path.resolve()), str(pdf_path)],
        float(config.get("output.pdf.timeout", 120)),
        "wkhtmltopdf",
    )


ENGINES = {
    "chrome": render_with_chrome,
    "weasyprint": render_with_weasyprint,
    "wkhtmltopdf": render_with_wkhtmltopdf,
}


def to_pdf(
    html_path: str | Path,
    pdf_path: str | Path | None = None,
    config: Config | None = None,
    engines: list[str] | None = None,
) -> tuple[Path, str]:
    """Converte o HTML do relatório em PDF. Devolve (caminho, motor usado)."""
    html_path = Path(html_path)
    if not html_path.exists():
        raise PdfError(f"HTML do relatório não encontrado: {html_path} — rode `wyckoff report`.")
    pdf_path = Path(pdf_path) if pdf_path else html_path.with_suffix(".pdf")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    nomes = engines or (config.get("output.pdf.engines") if config else None) or list(ENGINES)
    desconhecidos = [n for n in nomes if n not in ENGINES]
    if desconhecidos:
        raise PdfError(f"motor de PDF desconhecido: {', '.join(desconhecidos)} — "
                       f"os disponíveis são {', '.join(ENGINES)}.")

    fonte = html_path
    temporario = None
    if config is None or bool(config.get("output.pdf.expand_details", True)):
        aberto = expand_details(html_path.read_text(encoding="utf-8"))
        # Ao lado do original: o HTML referencia os PNGs por caminho relativo
        # quando `output.embed_charts` está desligado.
        temporario = html_path.with_name(f".{html_path.stem}-print.html")
        temporario.write_text(aberto, encoding="utf-8")
        fonte = temporario

    falhas = []
    try:
        for nome in nomes:
            try:
                ENGINES[nome](fonte, pdf_path, config or Config({}))
            except PdfError as exc:
                falhas.append(str(exc))
                continue
            if not pdf_path.exists() or pdf_path.stat().st_size == 0:
                falhas.append(f"{nome}: terminou sem escrever o PDF")
                continue
            return pdf_path, nome
    finally:
        if temporario is not None:
            temporario.unlink(missing_ok=True)

    raise PdfError("não foi possível gerar o PDF.\n  " + "\n  ".join(falhas) + "\n" + INSTALL_HINT)
