"""Ranking semanal por momentum: a lista curta de cada mercado.

Existe porque o estudo de 18/09/2026 não achou indicador melhor para reduzir o
universo a uma dúzia de nomes. Dez anos de candles semanais (2016–2026, 890
papéis), toda semana os 10 primeiros de cada indicador contra a média dos
papéis líquidos nas 13 semanas seguintes: pico de volume empatou ou perdeu, as
regras Wyckoff também, e o momentum de 12 meses sem o último ficou à frente nos
dois mercados — +2,7% nos EUA (com a composição do S&P 500 corrigida pela data
de entrada) e +1,8% na B3. Nenhum passou de t ≈ 1,7: é **filtro de atenção**,
não sinal de compra. Vale até aparecer um conjunto de indicadores que meça
melhor, e por isso tudo aqui vem de `screener.momentum` no config.

**Doze meses, pulando o último.** É a definição clássica (Jegadeesh & Titman,
1993): o mês mais recente tende a devolver parte do que andou, e medi-lo junto
mistura dois efeitos opostos. Por isso a lista mostra os dois números — o
momentum que ordena e o retorno das semanas puladas, que só informa.

**Todo mundo na mesma semana.** Papel cuja última semana fechada é anterior à
do resto do universo (coleta falhou, ticker suspenso) fica de fora e é contado:
comparar o retorno de um papel parado há um mês com o dos outros é comparar
janelas diferentes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from .config import Config
from .data.cache import Cache
from .metrics import compute_metrics
from .screener import Universe, weekly_liquidity


@dataclass(frozen=True)
class MomentumPick:
    symbol: str
    momentum: float              # retorno de `lookback` semanas sem as `skip` últimas
    recent: float                # retorno das `skip` semanas puladas — só informa
    rs: float | None             # força relativa contra o índice (última janela de R3)
    close: float
    liquidity: float
    in_watchlist: bool = False


@dataclass
class MomentumResult:
    universe: Universe
    lookback: int
    skip: int
    picks: list[MomentumPick] = field(default_factory=list)
    week: dt.date | None = None  # última semana fechada, a mesma para todos
    scanned: int = 0
    ranked: int = 0              # líquidos, com histórico e em dia — antes do corte
    illiquid: int = 0
    short_history: int = 0
    outdated: int = 0
    missing: int = 0             # sem candles no cache


def momentum_return(closes: pd.Series, lookback: int, skip: int) -> float | None:
    """`close[t-skip] / close[t-lookback] - 1`, com `t` a última semana da série.

    Devolve None sem histórico suficiente ou com preço-base inválido.
    """
    if skip < 0 or lookback <= skip:
        raise ValueError(f"momentum: lookback ({lookback}) precisa ser maior que skip ({skip})")
    if len(closes) < lookback + 1:
        return None
    base = float(closes.iloc[-1 - lookback])
    fim = float(closes.iloc[-1 - skip])
    if not base > 0 or pd.isna(fim):
        return None
    return fim / base - 1.0


def rank_universe(
    universe: Universe,
    config: Config,
    cache: Cache,
    watchlist: frozenset[str] | set[str] | None = None,
) -> MomentumResult:
    """Os `screener.momentum.top` papéis líquidos do universo com maior momentum.

    Lê só do cache, como `screener.screen`: quem coleta é `screener.refresh`.
    Papel da watchlist entra e sai marcado — a lista é "os 10 mais fortes do
    mercado", e esconder um deles por já ser acompanhado mentiria sobre ela.
    """
    lookback = int(config.require("screener.momentum.lookback_weeks"))
    skip = int(config.require("screener.momentum.skip_weeks"))
    top = int(config.require("screener.momentum.top"))
    piso = float(config.get("screener.min_weekly_volume", 0) or 0)
    rs_windows = [int(w) for w in config.require("metrics.relative_strength_weeks")]
    rs_col = f"rs_{rs_windows[-1]}w" if rs_windows else None
    watchlist = frozenset(watchlist or ())

    resultado = MomentumResult(universe=universe, lookback=lookback, skip=skip)
    bench = cache.get_bars(universe.benchmark)
    bench = bench[~bench["is_partial"]] if not bench.empty else None

    medidos: list[tuple[dt.date, MomentumPick]] = []
    for symbol in universe.tickers:
        bars = cache.get_bars(symbol)
        if bars.empty:
            resultado.missing += 1
            continue
        resultado.scanned += 1
        fechadas = bars[~bars["is_partial"]]
        mom = momentum_return(fechadas["close"], lookback, skip)
        if mom is None:
            resultado.short_history += 1
            continue
        liquidez = weekly_liquidity(fechadas)
        if piso and liquidez < piso:
            resultado.illiquid += 1
            continue
        metricas = compute_metrics(fechadas, config, bench)
        rs = metricas[rs_col].iloc[-1] if rs_col and rs_col in metricas.columns else None
        recente = momentum_return(fechadas["close"], skip, 0) if skip else 0.0
        medidos.append((pd.Timestamp(fechadas.index[-1]).date(), MomentumPick(
            symbol=symbol,
            momentum=mom,
            recent=recente if recente is not None else float("nan"),
            rs=None if rs is None or pd.isna(rs) else float(rs),
            close=float(fechadas["close"].iloc[-1]),
            liquidity=liquidez,
            in_watchlist=symbol in watchlist,
        )))

    if not medidos:
        return resultado
    resultado.week = max(semana for semana, _ in medidos)
    em_dia = [pick for semana, pick in medidos if semana == resultado.week]
    resultado.outdated = len(medidos) - len(em_dia)
    em_dia.sort(key=lambda p: (-p.momentum, p.symbol))
    resultado.ranked = len(em_dia)
    resultado.picks = em_dia[:top] if top else em_dia
    return resultado
