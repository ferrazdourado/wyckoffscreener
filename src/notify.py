"""Notificação do resumo semanal (R9).

A spec §8 deixou "Telegram ou e-mail?" em aberto. Os dois estão aqui atrás da
mesma interface `Notifier`, então a escolha é uma linha do `config.yaml` e não
uma reescrita.

**Segredos nunca no arquivo.** `config.yaml` vai para o git; token de bot e
senha de SMTP, não. O config guarda o *nome da variável de ambiente* que
carrega cada segredo, e o código lê de lá. Se a variável não existir, a falha é
explícita e diz qual exportar — em vez de um 401 críptico da API.

**Nada sai daqui sem pedido explícito.** `notify.enabled` nasce `false` e o
envio só acontece com `wyckoff notify` ou `wyckoff report --notify`. Gerar
relatório nunca dispara mensagem por conta própria.

O transporte é injetável (`send_fn`) para os testes exercitarem formatação,
truncamento e tratamento de erro sem tocar a rede.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .config import Config

# Limite de uma mensagem do Telegram. O e-mail não tem limite prático, mas um
# resumo que não cabe numa tela deixa de ser resumo.
TELEGRAM_MAX = 4096


class NotifyError(Exception):
    """Falha de envio, com a mensagem já pronta para o usuário."""


@dataclass(frozen=True)
class Message:
    subject: str
    body: str


def _secret(config: Config, path: str, what: str) -> str:
    """Lê um segredo da variável de ambiente que o config nomeia."""
    var = config.get(path)
    if not var:
        raise NotifyError(f"config.yaml: `{path}` não definido — informe o nome da "
                          f"variável de ambiente que guarda {what}.")
    value = os.environ.get(str(var), "").strip()
    if not value:
        raise NotifyError(f"variável de ambiente {var} vazia ou ausente — exporte {what} "
                          f"antes de notificar:\n    export {var}='...'")
    return value


def build_message(model: dict, config: Config, max_chars: int = TELEGRAM_MAX) -> Message:
    """Resumo compacto do relatório: o que exige ação primeiro.

    A ordem é a mesma do relatório e pelo mesmo motivo — quem lê no celular na
    sexta à noite precisa ver invalidação violada antes de qualquer outra coisa.
    """
    tag = model["tag"]
    semana = model["week_start"].strftime("%d/%m/%Y")
    linhas = [f"Wyckoff — semana {tag} ({semana})", ""]

    invalidacoes = model["invalidations"]
    if invalidacoes:
        novas = sum(1 for a in invalidacoes if a.is_new)
        cabeca = f"INVALIDAÇÕES ({len(invalidacoes)}"
        cabeca += f", {novas} nova(s))" if novas else ")"
        linhas.append(cabeca)
        for alerta in invalidacoes:
            marca = "[NOVO] " if alerta.is_new else ""
            linhas.append(f"• {marca}{alerta.symbol}: fechou {alerta.close:.2f} contra nível "
                          f"{alerta.level:.2f} ({abs(alerta.distance_pct):.1%} além)")
        linhas.append("")

    eventos = model["week_events"]
    if eventos:
        linhas.append(f"EVENTOS DA SEMANA ({len(eventos)})")
        for item in eventos:
            evento = item["event"]
            marca = "" if evento.confirmed else " (não confirmado)"
            linhas.append(f"• {item['symbol']} — {evento.label}{marca}")
    else:
        linhas.append("EVENTOS DA SEMANA: nenhum")
    linhas.append("")

    linhas.append("WATCHLIST")
    for row in model["rows"]:
        marca = "⚠ " if row["violated"] else ""
        fr = row["rs"][0] if row["rs"] else "—"
        linhas.append(f"• {marca}{row['symbol']} — {row['phase_code']} · "
                      f"vol {row['volume_ratio']} · FR {fr}")

    problemas = model["problems"]
    if problemas:
        linhas += ["", f"ERROS DE COLETA ({len(problemas)}): "
                       + ", ".join(p["symbol"] for p in problemas)]

    corpo = "\n".join(linhas)
    if len(corpo) > max_chars:
        # Cortar em linha inteira: meia linha de tabela não informa nada. A
        # reserva é o tamanho real do aviso — uma folga fixa estouraria o
        # limite justamente na mensagem grande, que é quando ele importa.
        aviso = f"\n\n[…truncado — relatório completo em reports/{tag}.html]"
        sobra = max(0, max_chars - len(aviso))
        corte = corpo[:sobra].rsplit("\n", 1)[0] if "\n" in corpo[:sobra] else corpo[:sobra]
        corpo = corte + aviso
    return Message(subject=f"Wyckoff {tag} — semana de {semana}", body=corpo)


class Notifier(ABC):
    """Contrato mínimo de um canal de notificação."""

    @abstractmethod
    def send(self, message: Message) -> str:
        """Envia e devolve uma linha descrevendo o destino, para o log da CLI."""


class TelegramNotifier(Notifier):
    """Bot do Telegram via HTTP. Usa urllib para não acrescentar dependência."""

    API = "https://api.telegram.org"

    def __init__(self, config: Config, send_fn=None):
        self.token = _secret(config, "notify.telegram.token_env", "o token do bot")
        self.chat_id = _secret(config, "notify.telegram.chat_id_env", "o chat_id de destino")
        self._send = send_fn or self._post

    def _post(self, url: str, payload: dict) -> dict:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detalhe = exc.read().decode("utf-8", "replace")[:300]
            raise NotifyError(f"Telegram recusou o envio (HTTP {exc.code}): {detalhe}") from exc
        except urllib.error.URLError as exc:
            raise NotifyError(f"não foi possível falar com a API do Telegram: {exc.reason}") from exc

    def send(self, message: Message) -> str:
        resposta = self._send(
            f"{self.API}/bot{self.token}/sendMessage",
            {"chat_id": self.chat_id, "text": message.body, "disable_web_page_preview": "true"},
        )
        if isinstance(resposta, dict) and not resposta.get("ok", True):
            raise NotifyError(f"Telegram recusou o envio: {resposta.get('description', resposta)}")
        return f"Telegram, chat {self.chat_id}"


class EmailNotifier(Notifier):
    """E-mail por SMTP com STARTTLS."""

    def __init__(self, config: Config, send_fn=None):
        self.host = str(config.require("notify.email.host"))
        self.port = int(config.get("notify.email.port", 587))
        self.use_tls = bool(config.get("notify.email.use_tls", True))
        self.user = _secret(config, "notify.email.user_env", "o usuário do SMTP")
        self.password = _secret(config, "notify.email.password_env", "a senha do SMTP")
        self.sender = str(config.get("notify.email.sender") or self.user)
        destinatarios = config.get("notify.email.to") or []
        if isinstance(destinatarios, str):
            destinatarios = [destinatarios]
        self.to = [str(d).strip() for d in destinatarios if str(d).strip()]
        if not self.to:
            raise NotifyError("config.yaml: `notify.email.to` vazio — informe ao menos um destinatário.")
        self.prefix = str(config.get("notify.email.subject_prefix", "")).strip()
        self._send = send_fn or self._smtp

    def _smtp(self, message) -> None:
        import smtplib

        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                if self.use_tls:
                    smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise NotifyError(f"SMTP recusou as credenciais de {self.user}: {exc}") from exc
        except OSError as exc:
            raise NotifyError(f"não foi possível falar com {self.host}:{self.port} — {exc}") from exc

    def send(self, message: Message) -> str:
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["Subject"] = f"{self.prefix} {message.subject}".strip()
        mail["From"] = self.sender
        mail["To"] = ", ".join(self.to)
        mail.set_content(message.body)
        self._send(mail)
        return f"e-mail para {', '.join(self.to)}"


BACKENDS = {"telegram": TelegramNotifier, "email": EmailNotifier}


def build_notifier(config: Config, send_fn=None) -> Notifier:
    """Instancia o canal escolhido em `notify.backend`."""
    backend = str(config.get("notify.backend", "") or "").strip().lower()
    if backend not in BACKENDS:
        raise NotifyError(
            f"config.yaml: `notify.backend` = {backend!r} — use um de {sorted(BACKENDS)}."
        )
    return BACKENDS[backend](config, send_fn=send_fn)


def notify(model: dict, config: Config, send_fn=None) -> str:
    """Monta o resumo e envia. Erros viram NotifyError com instrução acionável."""
    notifier = build_notifier(config, send_fn=send_fn)
    limite = int(config.get("notify.max_chars", TELEGRAM_MAX))
    return notifier.send(build_message(model, config, max_chars=limite))
