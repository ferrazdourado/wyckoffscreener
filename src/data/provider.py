"""Coleta de dados (R2): interface DataProvider + implementação yfinance.

O resto do sistema só conhece `DataProvider`, então trocar a fonte (brapi.dev
para B3, por exemplo — P2 da spec) não toca em métricas, eventos nem relatório.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


class FetchError(Exception):
    """Falha na coleta de um símbolo. Nunca aborta a execução (R2)."""


@dataclass(frozen=True)
class MarketHours:
    timezone: str
    close: str  # "HH:MM" da sexta-feira

    @property
    def close_time(self) -> dt.time:
        hour, minute = self.close.split(":")
        return dt.time(int(hour), int(minute))


def week_start(date: dt.date) -> dt.date:
    """Segunda-feira da semana da data."""
    return date - dt.timedelta(days=date.weekday())


def is_week_closed(bar_week_start: dt.date, hours: MarketHours, now: dt.datetime | None = None) -> bool:
    """A semana daquele candle já fechou?

    Fecha na sexta-feira no horário de fechamento do mercado (18h BRT na B3,
    16h ET nas bolsas US). Antes disso o candle ainda está em formação e as
    métricas em cima dele são provisórias.
    """
    tz = ZoneInfo(hours.timezone)
    now = now.astimezone(tz) if now is not None else dt.datetime.now(tz)
    friday_close = dt.datetime.combine(
        bar_week_start + dt.timedelta(days=4), hours.close_time, tzinfo=tz
    )
    return now >= friday_close


def normalize_ohlcv(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """yfinance -> colunas minúsculas open/high/low/close/volume, índice datetime.

    Lida com o MultiIndex (Price, Ticker) que o `yf.download` devolve.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        levels = [lvl for lvl in range(df.columns.nlevels) if symbol in df.columns.get_level_values(lvl)]
        if levels:
            df = df.xs(symbol, axis=1, level=levels[0])
        else:
            df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower().replace(" ", "_") for c in df.columns]
    if "adj_close" in df.columns and "close" not in df.columns:
        df["close"] = df["adj_close"]
    missing = [c for c in BAR_COLUMNS if c not in df.columns]
    if missing:
        raise FetchError(f"{symbol}: colunas ausentes na resposta da fonte: {missing}")
    df = df[BAR_COLUMNS]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # Linhas sem preço são lixo da fonte; volume ausente vira 0 (feriado/leilão).
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    return df.astype(float)


def aggregate_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Agrega candles diários em semanais ancorados na segunda-feira.

    Por que não usar o `interval="1wk"` do yfinance: em 07/09/2026 comprovamos
    que a barra semanal mais recente vinha com volume ~11% acima da soma dos
    pregões da semana (PETR4: 281.701.900 contra 252.576.000 somando os 5 dias)
    e que, na semana em que um provento fica ex, os preços misturam bases de
    ajuste diferentes (BAC: abertura cum-dividendo e fechamento ex-dividendo,
    distorcendo o spread em 0,5%). Agregando do diário ajustado os dois
    problemas somem: o volume é a soma real dos pregões e todos os preços estão
    na mesma base de ajuste.

    `trading_days` acompanha cada barra — semana curta por feriado é informação,
    não erro (B3 e US têm calendários distintos).
    """
    if daily is None or daily.empty:
        return pd.DataFrame(columns=BAR_COLUMNS + ["trading_days"])
    grouper = daily.resample("W-MON", label="left", closed="left")
    weekly = grouper.agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    weekly["trading_days"] = grouper["close"].count()
    # Semanas sem pregão nenhum (recesso) não viram candle.
    weekly = weekly[weekly["trading_days"] > 0]
    weekly.index.name = "week_start"
    return weekly


class DataProvider(ABC):
    """Contrato mínimo que qualquer fonte precisa cumprir.

    O primitivo é o candle DIÁRIO, não o semanal: é dele que sai a agregação
    correta (ver `aggregate_weekly`) e é ele que a contagem de Ponto & Figura
    de R10 precisa — semana inteira vira no máximo uma coluna por direção, o
    que esvazia a contagem. Uma fonte nova implementa `daily_bars` e ganha
    `weekly_bars` de graça.
    """

    @abstractmethod
    def daily_bars(self, symbol: str, weeks: int) -> pd.DataFrame:
        """OHLCV diário ajustado por proventos, cobrindo `weeks` semanas."""

    def weekly_bars(self, symbol: str, weeks: int) -> pd.DataFrame:
        """OHLCV semanal ajustado, índice = segunda da semana."""
        bars = aggregate_weekly(self.daily_bars(symbol, weeks))
        if bars.empty:
            raise FetchError(f"{symbol}: nenhum candle semanal pôde ser formado")
        return bars.tail(weeks)

    @abstractmethod
    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        """Colunas date/kind/value; kind em {dividend, split}."""


class YFinanceProvider(DataProvider):
    def __init__(self, session=None):
        self._session = session

    def daily_bars(self, symbol: str, weeks: int) -> pd.DataFrame:
        import yfinance as yf

        # Uma semana de folga para a barra semanal mais antiga já nascer completa.
        today = dt.date.today()
        start = week_start(today) - dt.timedelta(weeks=weeks + 1)
        try:
            raw = yf.download(
                symbol,
                start=start.isoformat(),
                end=(today + dt.timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=True,  # R2: preços ajustados por proventos
                progress=False,
                threads=False,
                actions=False,
            )
        except Exception as exc:  # a fonte falha de muitas formas; normalizamos
            raise FetchError(f"{symbol}: falha ao baixar candles diários — {exc}") from exc
        daily = normalize_ohlcv(raw, symbol)
        if daily.empty:
            raise FetchError(f"{symbol}: fonte não retornou candles (ticker inexistente ou deslistado?)")
        return daily

    def corporate_actions(self, symbol: str) -> pd.DataFrame:
        import yfinance as yf

        try:
            actions = yf.Ticker(symbol).actions
        except Exception as exc:
            raise FetchError(f"{symbol}: falha ao baixar proventos — {exc}") from exc
        if actions is None or len(actions) == 0:
            return pd.DataFrame(columns=["date", "kind", "value"])
        actions = actions.reset_index()
        date_col = actions.columns[0]
        rows = []
        for _, row in actions.iterrows():
            date = pd.Timestamp(row[date_col]).tz_localize(None).date()
            for col, kind in (("Dividends", "dividend"), ("Stock Splits", "split")):
                value = row.get(col)
                if value is not None and pd.notna(value) and float(value) != 0.0:
                    rows.append({"date": date, "kind": kind, "value": float(value)})
        return pd.DataFrame(rows, columns=["date", "kind", "value"])
