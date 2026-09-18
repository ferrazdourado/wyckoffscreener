"""Montagem da fonte de dados a partir do config (P2 da spec).

A spec pede que a fonte seja plugável sem tocar em métricas, eventos ou
relatório — o resto do sistema só conhece `DataProvider`. Este módulo é o único
lugar que sabe quais implementações existem e qual atende cada mercado.

Duas composições cobrem o que a §8 pede:

* **Roteamento por mercado** — brapi.dev só tem B3; papel americano continua no
  yfinance. `data.source.by_market` diz quem atende o quê.
* **Cadeia de fallback** — `data.source.fallback` lista as fontes tentadas
  quando a principal falha, na ordem. O fallback dispara em **erro de coleta**,
  não em suspeita de buraco: decidir sozinho que uma série está incompleta é
  chute, e trocar de fonte no meio de uma série silenciosamente é pior do que
  falhar visivelmente. Para comparar as duas fontes e decidir com número na
  mão, use `wyckoff sources TICKER`.

Segredo nenhum entra no `config.yaml`: o token da brapi vem da variável de
ambiente que `data.brapi.token_env` nomeia.
"""

from __future__ import annotations

import os

import pandas as pd

from ..config import Config
from .provider import DataProvider, FetchError, YFinanceProvider
from .throttle import ThrottledProvider, policy_from_config

# Sufixo do Yahoo para papel brasileiro; usado só quando ninguém informou o
# mercado do símbolo (o screener e a watchlist informam).
B3_SUFFIX = ".SA"
B3_INDEXES = {"^BVSP", "^IBXX", "^IBX50", "^IDIV", "^SMLL"}


class ProviderError(Exception):
    """Fonte pedida no config que não existe ou não pode ser montada."""


def market_of_symbol(symbol: str) -> str:
    """Palpite de mercado quando o chamador não sabe dizer."""
    if symbol.upper().endswith(B3_SUFFIX) or symbol.upper() in B3_INDEXES:
        return "b3"
    return "us"


class ChainProvider(DataProvider):
    """Tenta as fontes em ordem; a primeira que responder serve a série.

    Guarda em `used` quem atendeu cada símbolo, para o relatório poder dizer de
    onde veio o dado — uma série servida pelo fallback merece ser identificada.
    """

    def __init__(self, providers: list[tuple[str, DataProvider]]):
        if not providers:
            raise ProviderError("cadeia de fontes vazia")
        self.providers = providers
        self.used: dict[str, str] = {}

    @property
    def names(self) -> list[str]:
        return [nome for nome, _ in self.providers]

    def _try(self, symbol: str, metodo: str, *args):
        falhas = []
        for nome, provider in self.providers:
            try:
                resultado = getattr(provider, metodo)(*args)
            except FetchError as exc:
                falhas.append(f"{nome}: {exc}")
                continue
            self.used[symbol] = nome
            return resultado
        raise FetchError(f"{symbol}: nenhuma fonte respondeu — " + " | ".join(falhas))

    def daily_bars(self, symbol: str, weeks: int) -> pd.DataFrame:
        return self._try(symbol, "daily_bars", symbol, weeks)

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        return self._try(symbol, "corporate_actions", symbol)


class RoutingProvider(DataProvider):
    """Cada mercado com a sua fonte; `market_of` diz a que mercado o papel pertence."""

    def __init__(self, default: DataProvider, by_market: dict[str, DataProvider] | None = None,
                 market_of=None):
        self.default = default
        self.by_market = by_market or {}
        self.market_of = market_of or market_of_symbol

    def _for(self, symbol: str) -> DataProvider:
        return self.by_market.get(self.market_of(symbol), self.default)

    def daily_bars(self, symbol: str, weeks: int) -> pd.DataFrame:
        return self._for(symbol).daily_bars(symbol, weeks)

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        return self._for(symbol).corporate_actions(symbol)


def _brapi(config: Config) -> DataProvider:
    from .brapi import BASE_URL, BrapiProvider

    var = config.get("data.brapi.token_env")
    token = os.environ.get(str(var), "").strip() if var else ""
    return BrapiProvider(
        token=token,
        base_url=str(config.get("data.brapi.base_url", BASE_URL)),
        timeout=float(config.get("data.brapi.timeout", 20)),
        max_range=str(config.get("data.brapi.max_range", "5y")),
    )


BUILDERS = {
    "yfinance": lambda config: YFinanceProvider(),
    "brapi": _brapi,
}


def build_single(name: str, config: Config) -> DataProvider:
    """Uma fonte pelo nome que o config usa, já com ritmo de coleta.

    O ritmo (`data.fetch`) é montado **por fonte**, não em volta da cadeia: se
    o Yahoo estrangula o IP, a insistência tem que acontecer antes de a cadeia
    desistir e chamar a brapi — trocar de fonte por causa de um corte temporário
    é trocar por causa de nada.
    """
    builder = BUILDERS.get(str(name).lower())
    if builder is None:
        raise ProviderError(
            f"fonte de dados desconhecida: `{name}` — as disponíveis são "
            f"{', '.join(sorted(BUILDERS))}."
        )
    provider = builder(config)
    policy = policy_from_config(config)
    return ThrottledProvider(provider, policy) if policy.active else provider


def _chain(primeira: str, config: Config) -> DataProvider:
    nomes = [primeira]
    for extra in config.get("data.source.fallback", []) or []:
        if str(extra).lower() not in [n.lower() for n in nomes]:
            nomes.append(str(extra))
    if len(nomes) == 1:
        return build_single(nomes[0], config)
    return ChainProvider([(n, build_single(n, config)) for n in nomes])


def build_provider(config: Config, watchlist=None, market: str | None = None) -> DataProvider:
    """Fonte pronta para o pipeline, já roteada e encadeada conforme o config.

    `watchlist` e `market` só servem para saber o mercado de cada símbolo:
    a watchlist sabe papel a papel (e herda o mercado para o índice de
    referência); `market` fixa o mercado quando o lote inteiro é de um só (uma
    varredura de universo). Sem nenhum dos dois, vale o palpite pelo sufixo.
    """
    padrao = str(config.get("data.source.default", "yfinance"))
    principal = _chain(padrao, config)

    by_market_cfg = config.get("data.source.by_market", {}) or {}
    by_market = {str(k): _chain(str(v), config) for k, v in by_market_cfg.items()}
    if not by_market:
        return principal

    if market is not None:
        market_of = lambda _symbol: market
    elif watchlist is not None:
        from ..pipeline import _market_of
        market_of = lambda symbol: _market_of(symbol, watchlist)
    else:
        market_of = market_of_symbol
    return RoutingProvider(principal, by_market, market_of)
