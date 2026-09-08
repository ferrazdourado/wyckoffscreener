"""Backtest das heurísticas (R11).

**Isto não mede performance de estratégia.** Não há entrada, saída, stop,
custo, slippage nem dimensionamento de posição. O que se mede é uma coisa só:
depois que a regra dispara, o preço anda para que lado nas N semanas seguintes,
e isso é diferente do que acontece numa semana qualquer? Serve para calibrar
threshold — descobrir que `min_volume_ratio: 1.5` separa melhor que `1.2` — e
para nada além disso.

**O modo causal é o default, e o motivo é grave.** A detecção de range olha a
série inteira: se a barra `i` pertence a um range depende de barras posteriores
a `i`, porque é a dispersão dos fechamentos futuros que estende ou encerra a
lateralização. Um backtest rodado sobre a detecção final saberia, em `i`, algo
que só o futuro conta — e sairia otimista. No modo causal a leitura é refeita
semana a semana usando só o passado, exatamente como você a teria visto.

**O retorno é medido da semana em que o sinal ficou visível**, não da semana do
candle. Um spring confirmado dois candles depois só é acionável no dia em que
confirma; medir de antes seria contar como ganho o movimento que produziu a
confirmação.

**A linha de base é obrigatória na leitura.** Em mercado de alta tudo tem
retorno positivo à frente. A tabela traz `qualquer semana` justamente para você
comparar: um evento que rende menos que a semana média é um evento que atrapalha.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Config
from .events import Event, detect_all
from .ranges import resolve_ranges

DISCLAIMER = ("Calibragem de heurística, não backtest de estratégia: sem entrada, saída, "
              "stop, custo ou dimensionamento. Retorno passado não projeta retorno futuro.")


@dataclass
class Observation:
    """Um disparo de regra e o que veio depois dele."""

    symbol: str
    kind: str
    bias: str
    event_index: int      # semana do candle que disparou
    detected_index: int   # semana em que o sinal ficou visível
    date: dt.date
    confirmed: bool
    forward: dict[int, float] = field(default_factory=dict)
    excess: dict[int, float] = field(default_factory=dict)

    @property
    def lag(self) -> int:
        """Semanas entre o candle e o momento em que dava para agir."""
        return self.detected_index - self.event_index


def _forward_returns(closes: np.ndarray, i: int, horizons: list[int]) -> dict[int, float]:
    """Retorno simples de `i` até `i + h`, por horizonte. NaN se faltar futuro."""
    out: dict[int, float] = {}
    base = closes[i]
    for h in horizons:
        j = i + h
        out[h] = float(closes[j] / base - 1.0) if (j < len(closes) and base > 0) else float("nan")
    return out


def causal_events(bars: pd.DataFrame, config: Config, min_bars: int) -> list[tuple[int, Event]]:
    """(semana em que ficou visível, evento), redetectando a cada semana.

    Um evento é registrado na primeira vez que aparece; se depois reaparecer
    confirmado, o registro é atualizado para a semana da confirmação — é ali
    que ele vira acionável.
    """
    achados: dict[tuple[str, int], tuple[int, Event]] = {}
    for j in range(min_bars, len(bars)):
        janela = bars.iloc[: j + 1]
        eventos = detect_all(janela, resolve_ranges(janela, config), config)
        for evento in eventos:
            chave = (evento.kind, evento.index)
            anterior = achados.get(chave)
            if anterior is None:
                achados[chave] = (j, evento)
            elif not anterior[1].confirmed and evento.confirmed:
                achados[chave] = (j, evento)   # só agora dava para agir
    return sorted(achados.values(), key=lambda par: (par[0], par[1].index))


def final_events(bars: pd.DataFrame, config: Config) -> list[tuple[int, Event]]:
    """Modo rápido: detecção única sobre a série inteira.

    Contém lookahead na definição dos ranges (ver o cabeçalho do módulo). Serve
    para varredura exploratória de parâmetro, não para conclusão.
    """
    eventos = detect_all(bars, resolve_ranges(bars, config), config)
    return [(e.index, e) for e in eventos]


def observe(
    symbol: str,
    metrics: pd.DataFrame,
    config: Config,
    horizons: list[int],
    benchmark: pd.DataFrame | None = None,
    causal: bool = True,
) -> list[Observation]:
    """Todos os disparos de regra de um papel, com o que veio depois."""
    bars = metrics[~metrics["is_partial"].astype(bool)] if "is_partial" in metrics.columns else metrics
    min_bars = int(config.get("data.min_weeks_for_metrics", 21))
    if len(bars) <= min_bars:
        return []

    closes = bars["close"].to_numpy(dtype=float)
    bench_closes = None
    if benchmark is not None and not benchmark.empty:
        alinhado = benchmark["close"].reindex(bars.index)
        bench_closes = alinhado.to_numpy(dtype=float)

    pares = causal_events(bars, config, min_bars) if causal else final_events(bars, config)
    out: list[Observation] = []
    for visivel, evento in pares:
        adiante = _forward_returns(closes, visivel, horizons)
        excesso: dict[int, float] = {}
        if bench_closes is not None:
            bench_fwd = _forward_returns(bench_closes, visivel, horizons)
            excesso = {h: adiante[h] - bench_fwd[h] for h in horizons}
        out.append(Observation(
            symbol=symbol, kind=evento.kind, bias=evento.bias,
            event_index=evento.index, detected_index=visivel,
            date=pd.Timestamp(bars.index[visivel]).date(),
            confirmed=evento.confirmed, forward=adiante, excess=excesso,
        ))
    return out


def baseline(
    metrics_by_symbol: dict[str, pd.DataFrame],
    config: Config,
    horizons: list[int],
    benchmarks: dict[str, pd.DataFrame] | None = None,
) -> list[Observation]:
    """Retorno de QUALQUER semana — a régua contra a qual os eventos são lidos.

    Sem ela, "spring rende +4% em 13 semanas" não diz nada: pode ser que toda
    semana do período rendesse +6%.
    """
    min_bars = int(config.get("data.min_weeks_for_metrics", 21))
    out: list[Observation] = []
    for symbol, metrics in metrics_by_symbol.items():
        bars = metrics[~metrics["is_partial"].astype(bool)] if "is_partial" in metrics.columns else metrics
        if len(bars) <= min_bars:
            continue
        closes = bars["close"].to_numpy(dtype=float)
        bench_closes = None
        bench = (benchmarks or {}).get(symbol)
        if bench is not None and not bench.empty:
            bench_closes = bench["close"].reindex(bars.index).to_numpy(dtype=float)
        for i in range(min_bars, len(bars)):
            adiante = _forward_returns(closes, i, horizons)
            excesso = {}
            if bench_closes is not None:
                bench_fwd = _forward_returns(bench_closes, i, horizons)
                excesso = {h: adiante[h] - bench_fwd[h] for h in horizons}
            out.append(Observation(
                symbol=symbol, kind="_qualquer_semana", bias="neutro",
                event_index=i, detected_index=i,
                date=pd.Timestamp(bars.index[i]).date(),
                confirmed=True, forward=adiante, excess=excesso,
            ))
    return out


def _hit_rate(valores: np.ndarray, bias: str) -> float:
    """Fração que andou no sentido do viés. Evento neutro não tem sentido a acertar."""
    if valores.size == 0 or bias not in ("acumulacao", "distribuicao"):
        return float("nan")
    acertos = valores > 0 if bias == "acumulacao" else valores < 0
    return float(acertos.mean())


def summarize(
    observations: list[Observation], horizons: list[int], min_n: int = 1
) -> pd.DataFrame:
    """Uma linha por tipo de evento e horizonte, com n, mediana, média e acerto."""
    por_tipo: dict[str, list[Observation]] = {}
    for obs in observations:
        por_tipo.setdefault(obs.kind, []).append(obs)

    linhas = []
    for kind, grupo in sorted(por_tipo.items()):
        bias = grupo[0].bias
        for h in horizons:
            valores = np.array([o.forward[h] for o in grupo if h in o.forward], dtype=float)
            valores = valores[~np.isnan(valores)]
            if valores.size < min_n:
                continue
            excessos = np.array([o.excess.get(h, np.nan) for o in grupo], dtype=float)
            excessos = excessos[~np.isnan(excessos)]
            linhas.append({
                "evento": kind,
                "vies": bias,
                "horizonte_semanas": h,
                "n": int(valores.size),
                "mediana": float(np.median(valores)),
                "media": float(valores.mean()),
                "acerto": _hit_rate(valores, bias),
                "excesso_mediana": float(np.median(excessos)) if excessos.size else float("nan"),
                "excesso_acerto": _hit_rate(excessos, bias) if excessos.size else float("nan"),
                "atraso_medio_semanas": float(np.mean([o.lag for o in grupo])),
            })
    return pd.DataFrame(linhas)


def run(
    metrics_by_symbol: dict[str, pd.DataFrame],
    config: Config,
    horizons: list[int] | None = None,
    benchmarks: dict[str, pd.DataFrame] | None = None,
    causal: bool = True,
    kinds: tuple[str, ...] | None = None,
) -> tuple[pd.DataFrame, list[Observation]]:
    """Backtest completo: eventos + linha de base, resumidos numa tabela só."""
    horizons = horizons or [int(h) for h in config.get("backtest.horizons", [4, 8, 13, 26])]
    observacoes: list[Observation] = []
    for symbol, metrics in metrics_by_symbol.items():
        observacoes += observe(symbol, metrics, config, horizons,
                               (benchmarks or {}).get(symbol), causal=causal)
    if kinds:
        observacoes = [o for o in observacoes if o.kind in kinds]
    tudo = observacoes + baseline(metrics_by_symbol, config, horizons, benchmarks)
    return summarize(tudo, horizons), observacoes
