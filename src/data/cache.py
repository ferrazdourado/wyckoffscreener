"""Cache local em SQLite (R2).

Guarda candles semanais ajustados, proventos/splits e o log de coletas.
O log é o que permite cumprir "re-execução no mesmo dia não rebaixa dados":
antes de bater no yfinance, o pipeline pergunta ao cache se já houve coleta
bem-sucedida hoje para aquele símbolo.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Self

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_bars (
    symbol      TEXT NOT NULL,
    week_start  TEXT NOT NULL,          -- segunda-feira da semana, ISO AAAA-MM-DD
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    trading_days INTEGER NOT NULL DEFAULT 0,
    is_partial  INTEGER NOT NULL DEFAULT 0,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, week_start)
);

CREATE TABLE IF NOT EXISTS daily_bars (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,          -- pregão, ISO AAAA-MM-DD
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- dividend | split
    value       REAL NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, date, kind)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    symbol      TEXT NOT NULL,
    fetched_on  TEXT NOT NULL,          -- data local AAAA-MM-DD
    fetched_at  TEXT NOT NULL,
    status      TEXT NOT NULL,          -- ok | error
    rows        INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (symbol, fetched_on)
);
"""

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


class Cache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Colunas acrescentadas depois do banco já existir. O cache é
        reconstruível, mas apagá-lo custaria uma recoleta inteira à toa."""
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(weekly_bars)")}
        if "trading_days" not in existing:
            self.conn.execute("ALTER TABLE weekly_bars ADD COLUMN trading_days INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------- candles ----------------

    def upsert_bars(self, symbol: str, bars: pd.DataFrame, now: dt.datetime | None = None) -> int:
        """Insere/atualiza candles. `bars` indexado por data (segunda da semana).

        Idempotente: rodar duas vezes com os mesmos dados não duplica linhas.
        """
        if bars.empty:
            return 0
        stamp = (now or dt.datetime.now()).isoformat(timespec="seconds")
        rows = []
        for date, row in bars.iterrows():
            rows.append(
                (
                    symbol,
                    pd.Timestamp(date).date().isoformat(),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                    int(row.get("trading_days", 0) or 0),
                    int(bool(row.get("is_partial", False))),
                    stamp,
                )
            )
        self.conn.executemany(
            """
            INSERT INTO weekly_bars
                (symbol, week_start, open, high, low, close, volume, trading_days,
                 is_partial, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, week_start) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume,
                trading_days=excluded.trading_days,
                is_partial=excluded.is_partial, fetched_at=excluded.fetched_at
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_bars(self, symbol: str, limit_weeks: int | None = None) -> pd.DataFrame:
        """Últimas `limit_weeks` semanas do símbolo, em ordem cronológica."""
        sql = (
            "SELECT week_start, open, high, low, close, volume, trading_days, is_partial "
            "FROM weekly_bars WHERE symbol = ? ORDER BY week_start DESC"
        )
        params: list = [symbol]
        if limit_weeks:
            sql += " LIMIT ?"
            params.append(int(limit_weeks))
        df = pd.read_sql_query(sql, self.conn, params=params)
        if df.empty:
            return pd.DataFrame(columns=BAR_COLUMNS + ["trading_days", "is_partial"]).rename_axis("week_start")
        df["week_start"] = pd.to_datetime(df["week_start"])
        df = df.sort_values("week_start").set_index("week_start")
        df["is_partial"] = df["is_partial"].astype(bool)
        return df

    # ---------------- candles diários ----------------

    def upsert_daily(self, symbol: str, bars: pd.DataFrame, now: dt.datetime | None = None) -> int:
        """Guarda o candle diário que a coleta já baixou para formar o semanal.

        Não é redundância: a contagem de Ponto & Figura (R10) precisa da
        granularidade diária — uma semana inteira vira no máximo uma coluna por
        direção, o que esvazia a contagem de causa. O dado já vem na mesma
        requisição; jogá-lo fora seria pagar de novo depois.
        """
        if bars is None or bars.empty:
            return 0
        stamp = (now or dt.datetime.now()).isoformat(timespec="seconds")
        rows = [
            (symbol, pd.Timestamp(date).date().isoformat(), float(r["open"]), float(r["high"]),
             float(r["low"]), float(r["close"]), float(r["volume"]), stamp)
            for date, r in bars.iterrows()
        ]
        self.conn.executemany(
            """
            INSERT INTO daily_bars (symbol, date, open, high, low, close, volume, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume, fetched_at=excluded.fetched_at
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_daily_bars(
        self, symbol: str, start: dt.date | None = None, end: dt.date | None = None
    ) -> pd.DataFrame:
        """Pregões do símbolo no intervalo (inclusive), em ordem cronológica."""
        sql = "SELECT date, open, high, low, close, volume FROM daily_bars WHERE symbol = ?"
        params: list = [symbol]
        if start is not None:
            sql += " AND date >= ?"
            params.append(start.isoformat())
        if end is not None:
            sql += " AND date <= ?"
            params.append(end.isoformat())
        sql += " ORDER BY date"
        df = pd.read_sql_query(sql, self.conn, params=params)
        if df.empty:
            return pd.DataFrame(columns=BAR_COLUMNS).rename_axis("date")
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")

    # ---------------- proventos ----------------

    def upsert_actions(self, symbol: str, actions: pd.DataFrame, now: dt.datetime | None = None) -> int:
        """`actions` com colunas date/kind/value."""
        if actions is None or actions.empty:
            return 0
        stamp = (now or dt.datetime.now()).isoformat(timespec="seconds")
        rows = [
            (symbol, pd.Timestamp(r["date"]).date().isoformat(), str(r["kind"]), float(r["value"]), stamp)
            for _, r in actions.iterrows()
        ]
        self.conn.executemany(
            """
            INSERT INTO corporate_actions (symbol, date, kind, value, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date, kind) DO UPDATE SET
                value=excluded.value, fetched_at=excluded.fetched_at
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def get_actions(self, symbol: str, since: dt.date | None = None) -> pd.DataFrame:
        sql = "SELECT date, kind, value FROM corporate_actions WHERE symbol = ?"
        params: list = [symbol]
        if since is not None:
            sql += " AND date >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY date"
        df = pd.read_sql_query(sql, self.conn, params=params)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    # ---------------- log de coleta ----------------

    def record_fetch(
        self,
        symbol: str,
        status: str,
        rows: int = 0,
        message: str = "",
        now: dt.datetime | None = None,
    ) -> None:
        now = now or dt.datetime.now()
        self.conn.execute(
            """
            INSERT INTO fetch_log (symbol, fetched_on, fetched_at, status, rows, message)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, fetched_on) DO UPDATE SET
                fetched_at=excluded.fetched_at, status=excluded.status,
                rows=excluded.rows, message=excluded.message
            """,
            (symbol, now.date().isoformat(), now.isoformat(timespec="seconds"), status, int(rows), message),
        )
        self.conn.commit()

    def last_fetch(self, symbol: str | None = None) -> dt.datetime | None:
        """Quando a última coleta bem-sucedida entrou no cache.

        O dashboard lê o SQLite sem tocar a rede, então precisa dizer na tela de
        quando é o dado — senão o usuário confunde página aberta com dado fresco.
        """
        sql = "SELECT MAX(fetched_at) AS quando FROM fetch_log WHERE status = 'ok'"
        params: list = []
        if symbol is not None:
            sql += " AND symbol = ?"
            params.append(symbol)
        linha = self.conn.execute(sql, params).fetchone()
        if linha is None or not linha["quando"]:
            return None
        try:
            return dt.datetime.fromisoformat(linha["quando"])
        except ValueError:
            return None

    def fetched_today(self, symbol: str, today: dt.date | None = None) -> bool:
        """True se já houve coleta OK hoje — base para não rebaixar dados."""
        today = today or dt.date.today()
        cur = self.conn.execute(
            "SELECT 1 FROM fetch_log WHERE symbol = ? AND fetched_on = ? AND status = 'ok'",
            (symbol, today.isoformat()),
        )
        return cur.fetchone() is not None
