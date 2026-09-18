"""Fonte alternativa para a B3: brapi.dev (P2 da spec).

Por que existe: a §8 da spec deixou em aberto a qualidade do Yahoo para papéis
brasileiros em proventos e splits, com brapi.dev como fallback a definir. Para
responder isso é preciso ter as duas fontes rodando lado a lado — é o que este
módulo e o comando `wyckoff sources` fazem.

Quatro coisas que a fonte impõe e o código precisa absorver:

1. **Só B3.** Não há papel americano aqui; o roteamento (`factory.py`) manda os
   tickers `us` para o yfinance. O sufixo `.SA`, que é convenção do Yahoo, é
   removido antes da chamada.

2. **Preço ajustado tem de ser reconstruído.** A resposta traz OHLC bruto e
   apenas `adjustedClose`. Aplicamos o mesmo fator (`adjustedClose / close`) a
   abertura, máxima e mínima — ajuste proporcional, que é o que mantém o candle
   coerente. Sem isso, R2 ("usar preços ajustados") não se cumpre: a série teria
   fechamento ajustado convivendo com máxima e mínima em outra base.

3. **Volume não vem ajustado por desdobramento.** Comprovado em 08/09/2026 com
   MGLU3: no grupamento de 24/05/2024 a série da brapi mantém o volume antigo
   em escala de papel velho, e a razão contra o yfinance é exatamente
   0,1 × 1,05 — o produto dos dois grupamentos posteriores. Sem corrigir isso,
   `volume_ratio` lê 0,21 onde o yfinance lê 1,92 nas 15 semanas seguintes a um
   evento desses: nenhum climax (≥ 2×) e nenhum SOS (≥ 1,5×) seria detectado, em
   silêncio. `adjust_volume` reaplica os fatores; o histórico e os proventos vêm
   na MESMA requisição, então a correção não custa uma chamada extra.

4. **A camada gratuita sem token atende uma lista curta.** Em 08/09/2026,
   PETR4, VALE3, ITUB4 e MGLU3 respondiam sem credencial; BBAS3, B3SA3, PRIO3,
   BBSE3, EMBJ3 e o índice ^BVSP devolviam 401. O token vem de variável de
   ambiente — nunca do `config.yaml`, que vai para o git — e a mensagem de erro
   diz qual exportar em vez de deixar um 401 cru chegar ao usuário.

O transporte é injetável (`fetch_fn`) para os testes exercitarem parsing,
ajuste e tratamento de erro sem tocar a rede.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from .provider import BAR_COLUMNS, DataProvider, FetchError

BASE_URL = "https://brapi.dev/api"
TIMEZONE = "America/Sao_Paulo"

# Janelas que a API aceita, da menor para a maior, com o alcance de cada uma em
# semanas. Pedimos sempre a menor que cobre o histórico solicitado: puxar `max`
# por padrão gastaria banda da fonte para jogar o excedente fora.
RANGES: tuple[tuple[str, int], ...] = (
    ("1mo", 4), ("3mo", 13), ("6mo", 26), ("1y", 52),
    ("2y", 104), ("5y", 260), ("10y", 520), ("max", 10_000),
)

# `stockDividends` mistura desdobramento, grupamento e bonificação. Todos
# reprecificam o papel, então todos entram como `split` — o que o relatório usa
# é a DATA (marcar a semana frágil), não o fator.
STOCK_ACTION = "split"


def to_brapi_symbol(symbol: str) -> str:
    """`PETR4.SA` -> `PETR4`. Índices (`^BVSP`) passam inalterados."""
    return symbol[:-3] if symbol.upper().endswith(".SA") else symbol


def range_for(weeks: int, max_range: str = "5y") -> str:
    """Menor janela da API que cobre `weeks`, limitada por `max_range`.

    O teto existe porque `10y`/`max` podem ser de plano pago: quem não tem
    direito prefere receber 5 anos a receber 402.
    """
    permitidos = [nome for nome, _ in RANGES]
    if max_range not in permitidos:
        raise FetchError(f"brapi: range máximo inválido `{max_range}` — use um de {permitidos}")
    teto = permitidos.index(max_range)
    for i, (nome, alcance) in enumerate(RANGES):
        if i > teto:
            break
        if alcance >= weeks:
            return nome
    return max_range


def adjust_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica a razão de ajuste do fechamento a abertura/máxima/mínima.

    Barra sem `adjustedClose` utilizável (ausente, zero ou NaN) fica com fator
    1: é o caso das barras mais recentes, que ainda não sofreram ajuste nenhum.
    """
    df = df.copy()
    fator = pd.Series(1.0, index=df.index)
    valido = df["adjusted_close"].notna() & (df["close"] > 0) & (df["adjusted_close"] > 0)
    fator[valido] = df.loc[valido, "adjusted_close"] / df.loc[valido, "close"]
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * fator
    return df.drop(columns=["adjusted_close"])


def split_factors(payload: dict) -> list[tuple[pd.Timestamp, float, bool]]:
    """Desdobramentos/grupamentos como (corte, fator, corte_inclusivo).

    A borda importa e não é a mesma nos dois campos: `exDate` é o primeiro
    pregão JÁ na escala nova (ajusta quem vem ANTES dela), enquanto
    `lastDatePrior` é a data-com, o último pregão na escala velha (ajusta ele
    INCLUSIVE). Errar isso desloca o degrau de volume em um pregão.
    """
    results = payload.get("results") or []
    dados = (results[0].get("dividendsData") or {}) if results else {}
    fatores = []
    for item in dados.get("stockDividends") or []:
        fator = item.get("factor")
        if not fator or float(fator) <= 0 or float(fator) == 1.0:
            continue
        inclusivo = not item.get("exDate")
        bruta = item.get("exDate") or item.get("lastDatePrior")
        if not bruta:
            continue
        data = pd.to_datetime(bruta, errors="coerce", utc=True)
        if pd.isna(data):
            continue
        corte = pd.Timestamp(data.tz_convert(TIMEZONE).date())
        fatores.append((corte, float(fator), inclusivo))
    return sorted(fatores)


def adjust_volume(df: pd.DataFrame, fatores: list[tuple[pd.Timestamp, float, bool]]) -> pd.DataFrame:
    """Traz o volume anterior a cada desdobramento para a escala de hoje.

    Barra antiga é multiplicada pelo produto dos fatores de todos os eventos
    posteriores a ela — a mesma convenção do yfinance, conferida contra ele.
    """
    if not fatores or df.empty:
        return df
    df = df.copy()
    acumulado = pd.Series(1.0, index=df.index)
    for corte, fator, inclusivo in fatores:
        antes = df.index <= corte if inclusivo else df.index < corte
        acumulado[antes] *= fator
    df["volume"] = df["volume"] * acumulado
    return df


def parse_history(payload: dict, symbol: str) -> pd.DataFrame:
    """JSON do endpoint /quote -> OHLCV diário ajustado, índice datetime."""
    results = payload.get("results") or []
    if not results:
        raise FetchError(f"{symbol}: brapi não retornou resultados "
                         f"(ticker inexistente na B3?)")
    barras = results[0].get("historicalDataPrice") or []
    if not barras:
        raise FetchError(f"{symbol}: brapi retornou o papel sem histórico de preços")

    linhas = []
    for barra in barras:
        if barra.get("date") is None or barra.get("close") is None:
            continue          # barra de pregão sem negócio; não é candle
        linhas.append(
            {
                "date": barra["date"],
                "open": barra.get("open"),
                "high": barra.get("high"),
                "low": barra.get("low"),
                "close": barra.get("close"),
                "volume": barra.get("volume") or 0.0,
                "adjusted_close": barra.get("adjustedClose"),
            }
        )
    if not linhas:
        raise FetchError(f"{symbol}: brapi retornou histórico sem nenhuma barra utilizável")

    df = pd.DataFrame(linhas)
    # Timestamps vêm em segundos, na meia-noite de São Paulo. Converter no fuso
    # do pregão (e não em UTC) mantém a data certa nos anos em que o Brasil
    # ainda tinha horário de verão.
    df["date"] = (
        pd.to_datetime(df["date"], unit="s", utc=True)
        .dt.tz_convert(TIMEZONE)
        .dt.tz_localize(None)
        .dt.normalize()
    )
    df = df.set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df.dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        raise FetchError(f"{symbol}: brapi retornou histórico sem nenhuma barra com preço")
    df = adjust_ohlc(df.astype(float))
    df.index.name = "date"
    return df[BAR_COLUMNS]


def parse_actions(payload: dict) -> pd.DataFrame:
    """`dividendsData` -> colunas date/kind/value do contrato do DataProvider.

    `exDate` é o que interessa (é quando o preço anda) e às vezes vem nulo; aí
    cai para `lastDatePrior`, a data-com, que é o pregão imediatamente anterior.
    """
    results = payload.get("results") or []
    dados = (results[0].get("dividendsData") or {}) if results else {}
    linhas = []
    for chave, kind, campo in (("cashDividends", "dividend", "rate"),
                               ("stockDividends", STOCK_ACTION, "factor")):
        for item in dados.get(chave) or []:
            bruta = item.get("exDate") or item.get("lastDatePrior")
            if not bruta:
                continue
            data = pd.to_datetime(bruta, errors="coerce", utc=True)
            if pd.isna(data):
                continue
            valor = item.get(campo)
            linhas.append(
                {
                    "date": data.tz_convert(TIMEZONE).date(),
                    "kind": kind,
                    "value": float(valor) if valor is not None else 0.0,
                }
            )
    frame = pd.DataFrame(linhas, columns=["date", "kind", "value"])
    if frame.empty:
        return frame
    return frame.drop_duplicates(subset=["date", "kind"]).sort_values("date").reset_index(drop=True)


def _http_get(url: str, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resposta:
            return resposta.read()
    except urllib.error.HTTPError as exc:
        corpo = ""
        try:
            corpo = json.loads(exc.read().decode("utf-8")).get("message", "")
        except Exception:
            pass
        raise FetchError(f"brapi HTTP {exc.code}{f' — {corpo}' if corpo else ''}") from exc
    except Exception as exc:
        raise FetchError(f"brapi: falha de rede — {exc}") from exc


class BrapiProvider(DataProvider):
    """Candles da B3 pela brapi.dev. Não atende papel americano."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        max_range: str = "5y",
        fetch_fn=None,
    ):
        self.token = (token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_range = max_range
        self._fetch = fetch_fn or (lambda url: _http_get(url, self.timeout))

    # ---------------- infraestrutura ----------------

    def _url(self, symbol: str, params: dict[str, str]) -> str:
        if self.token:
            params = {**params, "token": self.token}
        alvo = urllib.parse.quote(to_brapi_symbol(symbol), safe="")
        return f"{self.base_url}/quote/{alvo}?{urllib.parse.urlencode(params)}"

    def _get(self, symbol: str, params: dict[str, str]) -> dict:
        try:
            bruto = self._fetch(self._url(symbol, params))
        except FetchError as exc:
            raise FetchError(f"{symbol}: {self._explicar(exc)}") from exc
        try:
            payload = json.loads(bruto)
        except (ValueError, TypeError) as exc:
            raise FetchError(f"{symbol}: brapi devolveu resposta não-JSON") from exc
        if not isinstance(payload, dict):
            raise FetchError(f"{symbol}: brapi devolveu JSON fora do formato esperado")
        return payload

    def _explicar(self, exc: FetchError) -> str:
        """Traduz o 401 mais comum da fonte em instrução acionável."""
        texto = str(exc)
        if "401" in texto and not self.token:
            return (f"{texto}. A camada gratuita da brapi sem token atende poucos papéis; "
                    f"crie um token em brapi.dev e exporte-o na variável de ambiente "
                    f"apontada por `data.brapi.token_env`.")
        if "401" in texto or "403" in texto:
            return f"{texto}. Token da brapi recusado — confira se ainda é válido."
        return texto

    # ---------------- contrato do DataProvider ----------------

    def daily_bars(self, symbol: str, weeks: int) -> pd.DataFrame:
        # `dividends=true` na mesma chamada: é de onde saem os fatores de
        # desdobramento que o volume precisa (ver item 3 do cabeçalho).
        payload = self._get(
            symbol,
            {"range": range_for(weeks, self.max_range), "interval": "1d", "dividends": "true"},
        )
        diario = adjust_volume(parse_history(payload, symbol), split_factors(payload))
        # A API só oferece janelas fixas; recortamos o excedente aqui para a
        # série ter o mesmo tamanho que a do yfinance.
        corte = diario.index.max() - pd.Timedelta(weeks=weeks + 1)
        return diario.loc[diario.index >= corte]

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        payload = self._get(symbol, {"range": "1d", "interval": "1d", "dividends": "true"})
        return parse_actions(payload)
