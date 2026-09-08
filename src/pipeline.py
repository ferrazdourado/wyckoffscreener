"""Orquestração da Fase 1: coletar -> cachear -> calcular -> exportar.

Regra central de R2: a falha de um ticker nunca aborta a execução. Todo erro
vira uma linha em `FetchReport.errors`, que a CLI imprime e a Fase 2 vai
transformar na seção "erros de coleta" do relatório.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .config import Config
from .data.cache import Cache
from .data.provider import (
    DataProvider,
    FetchError,
    MarketHours,
    aggregate_weekly,
    is_week_closed,
)
from .metrics import compute_metrics, latest_row
from .watchlist import Watchlist

BENCHMARK_MARKETS = {"^BVSP": "b3", "^GSPC": "us", "^IXIC": "us", "^DJI": "us"}


@dataclass
class SymbolStatus:
    symbol: str
    status: str  # ok | cached | error
    rows: int = 0
    message: str = ""


@dataclass
class FetchReport:
    statuses: list[SymbolStatus] = field(default_factory=list)

    @property
    def errors(self) -> list[SymbolStatus]:
        return [s for s in self.statuses if s.status == "error"]

    @property
    def ok(self) -> list[SymbolStatus]:
        return [s for s in self.statuses if s.status in ("ok", "cached")]


def market_hours_for(config: Config, market: str) -> MarketHours:
    hours = config.get(f"data.market_hours.{market}")
    if not hours:
        raise ValueError(f"config.yaml: data.market_hours.{market} não definido")
    return MarketHours(timezone=hours["timezone"], close=hours["close"])


def _market_of(symbol: str, watchlist: Watchlist) -> str:
    """Mercado do símbolo, para saber em que fuso a semana fecha.

    Para um índice, herda o mercado dos papéis que o usam como referência —
    assim um índice B3 fora do mapa fixo (^IBXX, ^IDIV) não acaba com horário
    de Nova York.
    """
    item = watchlist.get(symbol)
    if item is not None:
        return item.market
    for candidate in watchlist:
        if candidate.benchmark == symbol:
            return candidate.market
    return BENCHMARK_MARKETS.get(symbol, "us")


def mark_partial(bars: pd.DataFrame, hours: MarketHours, now: dt.datetime | None = None) -> pd.DataFrame:
    """Marca candles cuja semana ainda não fechou (R2)."""
    bars = bars.copy()
    bars["is_partial"] = [
        not is_week_closed(pd.Timestamp(idx).date(), hours, now) for idx in bars.index
    ]
    return bars


def flag_ex_dates(metrics: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Marca as semanas que contêm data-ex de provento ou split (R2).

    Mesmo agregando do diário ajustado, a semana de um split é a mais frágil da
    série: convém o relatório dizer isso em vez de o usuário descobrir olhando
    um spread esquisito.
    """
    metrics = metrics.copy()
    metrics["ex_dividend"] = False
    metrics["ex_split"] = False
    if actions is None or actions.empty or metrics.empty:
        return metrics
    for _, action in actions.iterrows():
        week = pd.Timestamp(action["date"]) - pd.Timedelta(days=pd.Timestamp(action["date"]).weekday())
        if week not in metrics.index:
            continue
        column = "ex_split" if action["kind"] == "split" else "ex_dividend"
        metrics.loc[week, column] = True
    return metrics


def fetch_all(
    watchlist: Watchlist,
    config: Config,
    provider: DataProvider,
    cache: Cache,
    force: bool = False,
    now: dt.datetime | None = None,
) -> FetchReport:
    now = now or dt.datetime.now()
    weeks = int(config.require("data.history_weeks"))
    refetch_same_day = bool(config.get("data.refetch_same_day", False))

    symbols = list(dict.fromkeys(watchlist.symbols + watchlist.benchmarks))
    report = FetchReport()

    for symbol in symbols:
        if not force and not refetch_same_day and cache.fetched_today(symbol, now.date()):
            rows = len(cache.get_bars(symbol))
            report.statuses.append(
                SymbolStatus(symbol, "cached", rows, "já coletado hoje — cache reutilizado")
            )
            continue
        try:
            hours = market_hours_for(config, _market_of(symbol, watchlist))
            # Uma requisição, dois usos: o diário alimenta o semanal (agregação
            # de R2) e fica guardado para a contagem de P&F de R10.
            daily = provider.daily_bars(symbol, weeks)
            bars = aggregate_weekly(daily).tail(weeks)
            if bars.empty:
                raise FetchError(f"{symbol}: nenhum candle semanal pôde ser formado")
            bars = mark_partial(bars, hours, now)
            n = cache.upsert_bars(symbol, bars, now)
            cache.upsert_daily(symbol, daily.loc[daily.index >= bars.index[0]], now)
            try:
                cache.upsert_actions(symbol, provider.corporate_actions(symbol), now)
            except FetchError as exc:
                # Proventos são complemento; a ausência não invalida os candles.
                report.statuses.append(SymbolStatus(symbol, "ok", n, f"candles ok; proventos falharam — {exc}"))
                cache.record_fetch(symbol, "ok", n, now=now)
                continue
            cache.record_fetch(symbol, "ok", n, now=now)
            report.statuses.append(SymbolStatus(symbol, "ok", n))
        except FetchError as exc:
            cache.record_fetch(symbol, "error", 0, str(exc), now=now)
            report.statuses.append(SymbolStatus(symbol, "error", 0, str(exc)))
        except Exception as exc:  # bug nosso ou fonte fora do contrato: não derruba o lote
            cache.record_fetch(symbol, "error", 0, repr(exc), now=now)
            report.statuses.append(SymbolStatus(symbol, "error", 0, f"erro inesperado: {exc!r}"))

    return report


def build_metrics(
    watchlist: Watchlist, config: Config, cache: Cache
) -> tuple[dict[str, pd.DataFrame], list[SymbolStatus]]:
    """Calcula as métricas de cada papel da watchlist a partir do cache."""
    min_weeks = int(config.get("data.min_weeks_for_metrics", 21))
    bench_cache: dict[str, pd.DataFrame] = {}
    results: dict[str, pd.DataFrame] = {}
    problems: list[SymbolStatus] = []

    for item in watchlist:
        bars = cache.get_bars(item.symbol)
        if bars.empty:
            problems.append(SymbolStatus(item.symbol, "error", 0, "sem candles no cache — rode `wyckoff fetch`"))
            continue
        if item.benchmark not in bench_cache:
            bench_cache[item.benchmark] = cache.get_bars(item.benchmark)
        bench = bench_cache[item.benchmark]
        if bench.empty:
            problems.append(
                SymbolStatus(item.symbol, "error", 0, f"benchmark {item.benchmark} sem dados — força relativa indisponível")
            )
            bench = None
        metrics = compute_metrics(bars, config, bench)
        metrics = flag_ex_dates(metrics, cache.get_actions(item.symbol, since=bars.index[0].date()))
        if len(bars) < min_weeks:
            problems.append(
                SymbolStatus(
                    item.symbol,
                    "error",
                    len(bars),
                    f"histórico curto: {len(bars)} semanas (< {min_weeks}) — médias de 20 semanas ainda não válidas",
                )
            )
        results[item.symbol] = metrics

    return results, problems


def _iso_week_tag(date: dt.date) -> str:
    year, week, _ = date.isocalendar()
    return f"{year}-{week:02d}"


def _data_week_tag(metrics: dict[str, pd.DataFrame], fallback: dt.date) -> str:
    """Etiqueta pela última semana FECHADA dos dados, não pelo dia da rodada.

    Rodar na sexta à noite ou na segunda seguinte tem que produzir o mesmo
    arquivo: o que identifica o relatório é a semana analisada.
    """
    semanas = []
    for df in metrics.values():
        if df.empty:
            continue
        fechadas = df[~df["is_partial"].astype(bool)] if "is_partial" in df.columns else df
        if not fechadas.empty:
            semanas.append(fechadas.index[-1])
    if not semanas:
        return _iso_week_tag(fallback)
    return _iso_week_tag(max(semanas).date())


def export_csv(
    metrics: dict[str, pd.DataFrame],
    watchlist: Watchlist,
    config: Config,
    now: dt.datetime | None = None,
) -> tuple[Path, Path]:
    """Escreve o CSV completo (todas as semanas) e o resumo da última semana fechada."""
    now = now or dt.datetime.now()
    out_dir = Path(config.get("output.csv_dir", "exports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = _data_week_tag(metrics, now.date())

    frames = []
    for symbol, df in metrics.items():
        frame = df.reset_index()
        frame.insert(0, "symbol", symbol)
        frames.append(frame)
    full = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    full_path = out_dir / f"metrics_{tag}.csv"
    full.to_csv(full_path, index=False)

    rows = []
    for symbol, df in metrics.items():
        row = latest_row(df, closed_only=True)
        if row is None:
            continue
        item = watchlist.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "market": item.market if item else "",
                "benchmark": item.benchmark if item else "",
                "week_start": row.name.date().isoformat(),
                **{k: row[k] for k in row.index if k not in ("is_partial",)},
                "weeks_in_cache": len(df),
                "partial_week_pending": bool(df["is_partial"].astype(bool).iloc[-1]) if "is_partial" in df else False,
            }
        )
    latest = pd.DataFrame(rows)
    latest_path = out_dir / f"latest_{tag}.csv"
    latest.to_csv(latest_path, index=False)
    return full_path, latest_path
