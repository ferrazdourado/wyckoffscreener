"""Ritmo, reteste e pausa longa da coleta.

Nenhum teste dorme: `sleep` e `clock` são dublês, e o que se verifica é
exatamente o que a política prometeu — quanto tempo pediu de espera, quando
insistiu, quando desconfiou de que o problema não era o papel e sim a fonte.
"""

import pandas as pd
import pytest

from src.config import Config, DEFAULTS
from src.data.provider import DataProvider, FetchError
from src.data.throttle import ThrottledProvider, ThrottlePolicy, policy_from_config

BARRA = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                      "volume": [1.0]}, index=pd.to_datetime(["2026-08-10"]))


class Fonte(DataProvider):
    """Falha nas `falhas` primeiras chamadas de cada símbolo; ou sempre."""

    def __init__(self, falhas: int = 0, sempre: set[str] | None = None):
        self.falhas, self.sempre = falhas, sempre or set()
        self.chamadas: list[str] = []

    def daily_bars(self, symbol, weeks):
        self.chamadas.append(symbol)
        if symbol in self.sempre or self.chamadas.count(symbol) <= self.falhas:
            raise FetchError(f"{symbol}: fonte não retornou candles")
        return BARRA

    def corporate_actions(self, symbol):
        return pd.DataFrame(columns=["date", "kind", "value"])


class Relogio:
    """Relógio que só anda quando alguém dorme — o tempo aqui é o das pausas."""

    def __init__(self):
        self.agora = 0.0
        self.dormidas: list[float] = []

    def sleep(self, segundos: float) -> None:
        self.dormidas.append(segundos)
        self.agora += segundos

    def clock(self) -> float:
        return self.agora


def montar(policy: ThrottlePolicy, fonte: Fonte) -> tuple[ThrottledProvider, Relogio]:
    relogio = Relogio()
    return ThrottledProvider(fonte, policy, sleep=relogio.sleep, clock=relogio.clock), relogio


# --------------------------- ritmo ---------------------------

def test_primeira_requisicao_nao_espera():
    provider, relogio = montar(ThrottlePolicy(min_interval=0.5), Fonte())
    provider.daily_bars("AAPL", 120)
    assert relogio.dormidas == []


def test_requisicao_seguida_espera_o_que_falta_para_o_piso():
    provider, relogio = montar(ThrottlePolicy(min_interval=0.5), Fonte())
    provider.daily_bars("AAPL", 120)
    provider.daily_bars("MSFT", 120)
    assert relogio.dormidas == [0.5]


def test_piso_zero_nao_impoe_ritmo_nenhum():
    provider, relogio = montar(ThrottlePolicy(), Fonte())
    for symbol in ("AAPL", "MSFT", "NVDA"):
        provider.daily_bars(symbol, 120)
    assert relogio.dormidas == []


# --------------------------- reteste ---------------------------

def test_falha_isolada_e_retestada_e_a_serie_vem_na_segunda():
    fonte = Fonte(falhas=1)
    provider, relogio = montar(ThrottlePolicy(retries=2, backoff=(3, 15)), fonte)
    assert not provider.daily_bars("AAPL", 120).empty
    assert fonte.chamadas == ["AAPL", "AAPL"]
    assert relogio.dormidas == [3]


def test_backoff_cresce_a_cada_retentativa():
    fonte = Fonte(falhas=2)
    provider, relogio = montar(ThrottlePolicy(retries=2, backoff=(3, 15)), fonte)
    provider.daily_bars("AAPL", 120)
    assert relogio.dormidas == [3, 15]


def test_backoff_mais_curto_que_as_retentativas_repete_o_ultimo_valor():
    fonte = Fonte(falhas=3)
    provider, relogio = montar(ThrottlePolicy(retries=3, backoff=(5,)), fonte)
    provider.daily_bars("AAPL", 120)
    assert relogio.dormidas == [5, 5, 5]


def test_esgotadas_as_retentativas_o_erro_da_fonte_chega_inteiro():
    fonte = Fonte(sempre={"MORTO"})
    provider, _ = montar(ThrottlePolicy(retries=2, backoff=(1,)), fonte)
    with pytest.raises(FetchError, match="MORTO: fonte não retornou candles"):
        provider.daily_bars("MORTO", 120)
    assert fonte.chamadas.count("MORTO") == 3


def test_sem_retentativa_configurada_a_fonte_e_chamada_uma_vez_so():
    fonte = Fonte(sempre={"MORTO"})
    provider, _ = montar(ThrottlePolicy(min_interval=0.5), fonte)
    with pytest.raises(FetchError):
        provider.daily_bars("MORTO", 120)
    assert fonte.chamadas == ["MORTO"]


# --------------------------- falha em série ---------------------------

def test_ticker_morto_isolado_nao_dispara_pausa_longa():
    """O caso da B3: papel morre de verdade, e sucesso em volta prova que a
    fonte está de pé. Pagar 60s por isso seria absurdo."""
    fonte = Fonte(sempre={"MORTO"})
    policy = ThrottlePolicy(retries=0, cooldown_after=3, cooldown=60)
    provider, relogio = montar(policy, fonte)
    for symbol in ("AAPL", "MORTO", "MSFT", "MORTO", "NVDA"):
        try:
            provider.daily_bars(symbol, 120)
        except FetchError:
            pass
    assert provider.cooldowns == 0
    assert 60 not in relogio.dormidas


def test_falhas_em_serie_disparam_a_pausa_longa_uma_vez():
    """O caso do Yahoo: corte de IP, que vem como série de 'deslistado'."""
    fonte = Fonte(sempre={"A", "B", "C"})
    policy = ThrottlePolicy(retries=0, cooldown_after=3, cooldown=60)
    provider, relogio = montar(policy, fonte)
    for symbol in ("A", "B", "C"):
        with pytest.raises(FetchError):
            provider.daily_bars(symbol, 120)
    assert provider.cooldowns == 1
    assert relogio.dormidas == [60]


def test_sucesso_zera_a_serie_de_falhas():
    fonte = Fonte(sempre={"A", "B"})
    policy = ThrottlePolicy(retries=0, cooldown_after=3, cooldown=60)
    provider, _ = montar(policy, fonte)
    for symbol in ("A", "B"):
        with pytest.raises(FetchError):
            provider.daily_bars(symbol, 120)
    assert provider.failure_streak == 2
    provider.daily_bars("AAPL", 120)
    assert provider.failure_streak == 0


def test_depois_da_pausa_a_serie_recomeca_do_zero():
    """Senão a pausa dispararia de novo no símbolo seguinte, e o corte de IP
    viraria uma sequência de esperas de um minuto."""
    fonte = Fonte(sempre={"A", "B", "C", "D"})
    policy = ThrottlePolicy(retries=0, cooldown_after=3, cooldown=60)
    provider, relogio = montar(policy, fonte)
    for symbol in ("A", "B", "C", "D"):
        with pytest.raises(FetchError):
            provider.daily_bars(symbol, 120)
    assert provider.cooldowns == 1
    assert provider.failure_streak == 1


# --------------------------- config ---------------------------

def test_policy_sai_do_config():
    policy = policy_from_config(Config(DEFAULTS))
    assert policy.active
    assert policy.min_interval == 0.5
    assert policy.retries == 2
    assert policy.backoff == (3.0, 15.0)
    assert policy.cooldown_after == 5
    assert policy.cooldown == 60.0


def test_policy_vazia_e_inativa():
    assert not ThrottlePolicy().active


def test_corporate_actions_tambem_respeita_o_ritmo():
    provider, relogio = montar(ThrottlePolicy(min_interval=0.5), Fonte())
    provider.corporate_actions("AAPL")
    provider.corporate_actions("MSFT")
    assert relogio.dormidas == [0.5]
