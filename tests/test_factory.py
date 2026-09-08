"""P2 — montagem da fonte: roteamento por mercado, cadeia de fallback, token.

Nenhum teste toca a rede: as fontes são dublês que respondem ou explodem.
"""

import pandas as pd
import pytest

from src.config import Config, DEFAULTS
from src.data.factory import (
    ChainProvider,
    ProviderError,
    RoutingProvider,
    build_provider,
    build_single,
    market_of_symbol,
)
from src.data.provider import DataProvider, FetchError


class Falsa(DataProvider):
    def __init__(self, nome: str, quebra: bool = False):
        self.nome, self.quebra, self.chamadas = nome, quebra, []

    def daily_bars(self, symbol, weeks):
        self.chamadas.append(symbol)
        if self.quebra:
            raise FetchError(f"{self.nome} não atende {symbol}")
        return pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                             "volume": [1.0], "fonte": [self.nome]},
                            index=pd.to_datetime(["2026-08-10"]))

    def corporate_actions(self, symbol):
        return pd.DataFrame(columns=["date", "kind", "value"])


def config(**data) -> Config:
    import copy
    bruto = copy.deepcopy(DEFAULTS)
    bruto["data"].update(data)
    return Config(bruto)


# --------------------------- palpite de mercado ---------------------------

@pytest.mark.parametrize("symbol,mercado", [
    ("PETR4.SA", "b3"), ("^BVSP", "b3"), ("BAC", "us"), ("^GSPC", "us"),
])
def test_sufixo_e_indice_dizem_o_mercado_quando_ninguem_informa(symbol, mercado):
    assert market_of_symbol(symbol) == mercado


# --------------------------- cadeia ---------------------------

def test_cadeia_usa_a_primeira_fonte_que_responde():
    boa, ruim = Falsa("boa"), Falsa("ruim", quebra=True)
    cadeia = ChainProvider([("ruim", ruim), ("boa", boa)])
    assert cadeia.daily_bars("PETR4.SA", 4)["fonte"].iloc[0] == "boa"


def test_cadeia_registra_quem_serviu_cada_papel():
    cadeia = ChainProvider([("ruim", Falsa("ruim", quebra=True)), ("boa", Falsa("boa"))])
    cadeia.daily_bars("PETR4.SA", 4)
    assert cadeia.used == {"PETR4.SA": "boa"}


def test_cadeia_so_falha_quando_todas_falham_e_diz_o_que_cada_uma_disse():
    cadeia = ChainProvider([("a", Falsa("a", quebra=True)), ("b", Falsa("b", quebra=True))])
    with pytest.raises(FetchError, match="nenhuma fonte respondeu.*a não atende.*b não atende"):
        cadeia.daily_bars("PETR4.SA", 4)


def test_fonte_principal_intacta_nao_chama_o_fallback():
    fallback = Falsa("fallback")
    ChainProvider([("boa", Falsa("boa")), ("fallback", fallback)]).daily_bars("PETR4.SA", 4)
    assert fallback.chamadas == []


def test_cadeia_vazia_e_erro_de_configuracao():
    with pytest.raises(ProviderError):
        ChainProvider([])


# --------------------------- roteamento ---------------------------

def test_cada_mercado_vai_para_a_sua_fonte():
    b3, us = Falsa("b3"), Falsa("us")
    router = RoutingProvider(us, {"b3": b3})
    assert router.daily_bars("PETR4.SA", 4)["fonte"].iloc[0] == "b3"
    assert router.daily_bars("BAC", 4)["fonte"].iloc[0] == "us"


def test_mercado_sem_fonte_propria_cai_no_padrao():
    padrao = Falsa("padrao")
    assert RoutingProvider(padrao, {"b3": Falsa("b3")}).daily_bars("BAC", 4)["fonte"].iloc[0] == "padrao"


def test_market_of_injetado_vence_o_palpite_pelo_sufixo():
    """O índice ^BVSP tem de acompanhar o mercado dos papéis que o usam."""
    b3 = Falsa("b3")
    router = RoutingProvider(Falsa("us"), {"b3": b3}, market_of=lambda s: "b3")
    assert router.daily_bars("QUALQUER", 4)["fonte"].iloc[0] == "b3"


# --------------------------- build_provider ---------------------------

def test_config_padrao_entrega_o_yfinance_puro():
    from src.data.provider import YFinanceProvider
    assert isinstance(build_provider(config()), YFinanceProvider)


def test_fonte_desconhecida_falha_dizendo_quais_existem():
    with pytest.raises(ProviderError, match="brapi.*yfinance"):
        build_single("bloomberg", config())


def test_fallback_configurado_vira_cadeia():
    provider = build_provider(config(source={"default": "yfinance", "by_market": {},
                                             "fallback": ["brapi"]}))
    assert isinstance(provider, ChainProvider) and provider.names == ["yfinance", "brapi"]


def test_fallback_igual_a_principal_nao_duplica_a_fonte():
    from src.data.provider import YFinanceProvider
    provider = build_provider(config(source={"default": "yfinance", "by_market": {},
                                             "fallback": ["yfinance"]}))
    assert isinstance(provider, YFinanceProvider)


def test_by_market_vira_roteamento():
    provider = build_provider(config(source={"default": "yfinance", "by_market": {"b3": "brapi"},
                                             "fallback": []}), market="b3")
    from src.data.brapi import BrapiProvider
    assert isinstance(provider, RoutingProvider)
    assert isinstance(provider._for("PETR4.SA"), BrapiProvider)


def test_token_da_brapi_vem_do_ambiente_e_nunca_do_arquivo(monkeypatch):
    monkeypatch.setenv("WYCKOFF_BRAPI_TOKEN", "segredo-do-ambiente")
    provider = build_single("brapi", config())
    assert provider.token == "segredo-do-ambiente"


def test_sem_variavel_exportada_a_fonte_roda_sem_token(monkeypatch):
    """A camada gratuita atende alguns papéis; falhar aqui seria prematuro —
    o 401 é que explica o que fazer."""
    monkeypatch.delenv("WYCKOFF_BRAPI_TOKEN", raising=False)
    assert build_single("brapi", config()).token == ""
