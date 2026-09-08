"""Métricas por candle semanal (R3).

Funções puras: DataFrame entra, DataFrame sai. Nenhum I/O, nenhuma constante
mágica — todas as janelas vêm do config. Cada coluna derivada carrega junto a
coluna crua que a gerou, para o relatório poder mostrar os números que
justificam qualquer sinal (princípio 3 da spec: auditabilidade > concisão).
"""

from __future__ import annotations

import pandas as pd

from .config import Config

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


def volume_sma(volume: pd.Series, weeks: int) -> pd.Series:
    """Média móvel simples do volume em `weeks` semanas."""
    return volume.rolling(window=weeks, min_periods=weeks).mean()


def volume_ratio(volume: pd.Series, weeks: int) -> pd.Series:
    """volume / SMA(volume, weeks). 1,0 = volume na média."""
    sma = volume_sma(volume, weeks)
    return volume.divide(sma.where(sma > 0))


def true_range(bars: pd.DataFrame) -> pd.Series:
    """True Range clássico. Na primeira barra, sem fechamento anterior, TR = high-low."""
    if bars.empty:
        return pd.Series(dtype=float, index=bars.index)
    prev_close = bars["close"].shift(1)
    hl = bars["high"] - bars["low"]
    hc = (bars["high"] - prev_close).abs()
    lc = (bars["low"] - prev_close).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    tr.iloc[0] = hl.iloc[0]
    return tr


def atr(bars: pd.DataFrame, weeks: int, method: str = "sma") -> pd.Series:
    """ATR semanal. `sma` = média simples do TR; `wilder` = suavização de Wilder."""
    tr = true_range(bars)
    if method == "wilder":
        return tr.ewm(alpha=1.0 / weeks, adjust=False, min_periods=weeks).mean()
    if method == "sma":
        return tr.rolling(window=weeks, min_periods=weeks).mean()
    raise ValueError(f"metrics.atr_method inválido: {method!r} (use 'sma' ou 'wilder')")


def spread_ratio(bars: pd.DataFrame, atr_series: pd.Series) -> pd.Series:
    """(high - low) / ATR. >1 = candle mais largo que a volatilidade típica."""
    spread = bars["high"] - bars["low"]
    return spread.divide(atr_series.where(atr_series > 0))


def close_position(bars: pd.DataFrame, on_zero_range: str = "neutral") -> pd.Series:
    """(close - low) / (high - low). 1,0 = fechou na máxima; 0,0 = na mínima."""
    rng = bars["high"] - bars["low"]
    pos = (bars["close"] - bars["low"]).divide(rng.where(rng > 0))
    if on_zero_range == "neutral":
        pos = pos.fillna(0.5).where(rng.notna())
    elif on_zero_range != "nan":
        raise ValueError(
            f"metrics.close_position_on_zero_range inválido: {on_zero_range!r} (use 'neutral' ou 'nan')"
        )
    return pos


def performance(close: pd.Series, weeks: int) -> pd.Series:
    """Retorno simples em `weeks` semanas, em fração (0,05 = +5%)."""
    return close.pct_change(periods=weeks)


def relative_strength(
    close: pd.Series, benchmark_close: pd.Series, weeks: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Força relativa contra o índice em `weeks` semanas.

    Devolve (perf_papel, perf_índice, diferença em pontos percentuais).
    O índice é realinhado ao calendário do papel — B3 e US têm feriados
    diferentes (princípio 5 da spec) e datas sem par viram NaN, não zero.
    """
    bench = benchmark_close.reindex(close.index)
    perf_asset = performance(close, weeks)
    perf_bench = performance(bench, weeks)
    return perf_asset, perf_bench, perf_asset - perf_bench


def compute_metrics(
    bars: pd.DataFrame,
    config: Config,
    benchmark_bars: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Todas as métricas de R3 para um papel, uma linha por semana."""
    missing = [c for c in BAR_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars sem as colunas obrigatórias: {missing}")

    vol_weeks = int(config.require("metrics.volume_sma_weeks"))
    atr_weeks = int(config.require("metrics.atr_weeks"))
    atr_method = str(config.require("metrics.atr_method"))
    rs_windows = list(config.require("metrics.relative_strength_weeks"))
    zero_range = str(config.get("metrics.close_position_on_zero_range", "neutral"))

    out = bars.copy()
    if out.empty:
        return out

    out["volume_sma"] = volume_sma(out["volume"], vol_weeks)
    out["volume_ratio"] = volume_ratio(out["volume"], vol_weeks)

    out["true_range"] = true_range(out)
    out["atr"] = atr(out, atr_weeks, atr_method)
    out["spread"] = out["high"] - out["low"]
    out["spread_ratio"] = spread_ratio(out, out["atr"])

    out["close_position"] = close_position(out, zero_range)
    out["weekly_return"] = out["close"].pct_change()

    for weeks in rs_windows:
        weeks = int(weeks)
        if benchmark_bars is not None and not benchmark_bars.empty:
            perf_asset, perf_bench, diff = relative_strength(
                out["close"], benchmark_bars["close"], weeks
            )
            out[f"perf_{weeks}w"] = perf_asset
            out[f"bench_perf_{weeks}w"] = perf_bench
            out[f"rs_{weeks}w"] = diff
        else:
            out[f"perf_{weeks}w"] = performance(out["close"], weeks)
            out[f"bench_perf_{weeks}w"] = pd.NA
            out[f"rs_{weeks}w"] = pd.NA

    return out


def latest_row(metrics: pd.DataFrame, closed_only: bool = True) -> pd.Series | None:
    """Última semana disponível — por padrão a última já fechada (R2)."""
    if metrics.empty:
        return None
    df = metrics
    if closed_only and "is_partial" in df.columns:
        df = df[~df["is_partial"].astype(bool)]
    if df.empty:
        return None
    return df.iloc[-1]
