"""Ritmo e reteste da coleta: o que fazer quando a fonte corta o IP.

Varrer `us_completa` (518 papéis) revelou o buraco: depois de ~318 requisições
seguidas o Yahoo parou de responder e os 200 papéis seguintes voltaram com
"possibly delisted; no price data found". Nenhum deles estava deslistado — HII,
HLT e ZTS respondiam normalmente um minuto depois, um a um. A fonte não diz
"desacelere": ela devolve série vazia, exatamente o que devolveria para um
ticker morto.

**É por isso que o remédio não pode ser só reteste por símbolo.** Se cada
símbolo insistisse três vezes, um universo estrangulado gastaria horas
reperguntando a mesma coisa, e um universo cheio de ticker morto — o caso da
B3, onde papel morre de verdade — pagaria o mesmo preço à toa.

O que separa os dois casos não está numa requisição, está na sequência delas:
ticker morto é falha **isolada** no meio de sucessos; estrangulamento é falha
**em série**. Daí as três defesas, nesta ordem:

1. `min_interval` — piso de tempo entre requisições, para não provocar o corte.
2. `retries`/`backoff` — reteste curto do símbolo, que cobre o soluço isolado.
3. `cooldown_after`/`cooldown` — quando as falhas viram série, pausa longa uma
   vez e segue. É a única defesa que enxerga o estrangulamento pelo que ele é.

A classe decora qualquer `DataProvider`, então vale para brapi também, e é
montada por fonte (ver `factory.build_single`): o ritmo é propriedade de quem
serve o dado, não da cadeia. Um estrangulamento do Yahoo assim não gasta o
fallback da brapi antes de ter insistido.

`sleep` é injetável para o teste não dormir de verdade.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pandas as pd

from ..config import Config
from .provider import DataProvider, FetchError


@dataclass(frozen=True)
class ThrottlePolicy:
    """Quanto esperar, quantas vezes insistir, quando desconfiar da série."""

    min_interval: float = 0.0
    retries: int = 0
    backoff: tuple[float, ...] = ()
    cooldown_after: int = 0
    cooldown: float = 0.0

    @property
    def active(self) -> bool:
        return bool(self.min_interval or self.retries or self.cooldown_after)

    def wait_before_retry(self, attempt: int) -> float:
        """Espera antes da tentativa `attempt` (0 = primeiro reteste).

        Backoff mais curto que o número de retentativas repete o último valor,
        então `backoff: [5]` com `retries: 3` significa cinco segundos sempre.
        """
        if not self.backoff:
            return 0.0
        return float(self.backoff[min(attempt, len(self.backoff) - 1)])


def policy_from_config(config: Config) -> ThrottlePolicy:
    backoff = config.get("data.fetch.backoff", []) or []
    return ThrottlePolicy(
        min_interval=float(config.get("data.fetch.min_interval", 0) or 0),
        retries=int(config.get("data.fetch.retries", 0) or 0),
        backoff=tuple(float(b) for b in backoff),
        cooldown_after=int(config.get("data.fetch.cooldown_after", 0) or 0),
        cooldown=float(config.get("data.fetch.cooldown", 0) or 0),
    )


class ThrottledProvider(DataProvider):
    """Um `DataProvider` com ritmo, reteste e pausa longa em falha em série."""

    def __init__(self, inner: DataProvider, policy: ThrottlePolicy,
                 sleep=time.sleep, clock=time.monotonic):
        self.inner = inner
        self.policy = policy
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None
        #: falhas consecutivas — o sinal de que a fonte cortou, não de que o
        #: papel morreu.
        self.failure_streak = 0
        #: quantas pausas longas foram necessárias, para quem quiser relatar.
        self.cooldowns = 0

    # -- ritmo -------------------------------------------------------------

    def _pace(self) -> None:
        if self.policy.min_interval <= 0:
            return
        if self._last_call is not None:
            folga = self.policy.min_interval - (self._clock() - self._last_call)
            if folga > 0:
                self._sleep(folga)
        self._last_call = self._clock()

    def _note_failure(self) -> None:
        self.failure_streak += 1
        limite = self.policy.cooldown_after
        if limite and self.failure_streak >= limite and self.policy.cooldown > 0:
            self._sleep(self.policy.cooldown)
            self.cooldowns += 1
            # A pausa é a tentativa de desarmar o corte: a série recomeça do
            # zero para não disparar outra pausa no símbolo seguinte.
            self.failure_streak = 0
            self._last_call = None

    def _call(self, metodo: str, symbol: str, *args):
        ultima: FetchError | None = None
        for tentativa in range(self.policy.retries + 1):
            if tentativa:
                espera = self.policy.wait_before_retry(tentativa - 1)
                if espera > 0:
                    self._sleep(espera)
            self._pace()
            try:
                resultado = getattr(self.inner, metodo)(symbol, *args)
            except FetchError as exc:
                ultima = exc
                continue
            self.failure_streak = 0
            return resultado
        self._note_failure()
        assert ultima is not None
        raise ultima

    # -- DataProvider ------------------------------------------------------

    def daily_bars(self, symbol: str, weeks: int) -> pd.DataFrame:
        return self._call("daily_bars", symbol, weeks)

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        return self._call("corporate_actions", symbol)
