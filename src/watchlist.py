"""Watchlist configurável + validação de schema (R1).

Validação artesanal (sem jsonschema) para que a mensagem de erro aponte o
caminho exato do problema: `tickers[3].invalidation.direction: ...`.
Todos os erros do arquivo são coletados numa passada só — o usuário corrige
tudo de uma vez em vez de descobrir um por execução.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MARKETS = ("b3", "us")
DIRECTIONS = ("below", "above")
DEFAULT_BENCHMARKS = {"b3": "^BVSP", "us": "^GSPC"}


class WatchlistError(Exception):
    """Watchlist inválida. A mensagem lista todos os problemas encontrados."""


@dataclass(frozen=True)
class Invalidation:
    price: float
    direction: str  # "below" = alerta se fechar abaixo

    def is_violated(self, close: float) -> bool:
        return close < self.price if self.direction == "below" else close > self.price

    def describe(self) -> str:
        seta = "abaixo de" if self.direction == "below" else "acima de"
        return f"fechamento semanal {seta} {self.price:g}"


@dataclass(frozen=True)
class ManualRange:
    support: float
    resistance: float


@dataclass(frozen=True)
class CalendarEvent:
    date: dt.date
    label: str


@dataclass(frozen=True)
class WatchItem:
    symbol: str
    market: str
    benchmark: str
    invalidation: Invalidation | None = None
    manual_range: ManualRange | None = None
    notes: str = ""
    calendar: tuple[CalendarEvent, ...] = ()


@dataclass
class Watchlist:
    items: list[WatchItem] = field(default_factory=list)
    path: Path | None = None

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def symbols(self) -> list[str]:
        return [i.symbol for i in self.items]

    @property
    def benchmarks(self) -> list[str]:
        """Benchmarks distintos, preservando a ordem de aparição."""
        seen: dict[str, None] = {}
        for item in self.items:
            seen.setdefault(item.benchmark, None)
        return list(seen)

    def get(self, symbol: str) -> WatchItem | None:
        for item in self.items:
            if item.symbol == symbol:
                return item
        return None


class _Validator:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def number(self, where: str, value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(where, f"esperado número, recebido {type(value).__name__} ({value!r})")
            return None
        return float(value)


def _parse_invalidation(v: _Validator, where: str, raw: Any) -> Invalidation | None:
    if not isinstance(raw, dict):
        v.fail(where, "esperado mapeamento com as chaves `price` e `direction`")
        return None
    unknown = set(raw) - {"price", "direction"}
    if unknown:
        v.fail(where, f"chaves desconhecidas: {sorted(unknown)}")
    if "price" not in raw:
        v.fail(f"{where}.price", "campo obrigatório quando `invalidation` está presente")
        return None
    price = v.number(f"{where}.price", raw["price"])
    direction = raw.get("direction", "below")
    if direction not in DIRECTIONS:
        v.fail(f"{where}.direction", f"esperado um de {list(DIRECTIONS)}, recebido {direction!r}")
        return None
    if price is None:
        return None
    if price <= 0:
        v.fail(f"{where}.price", f"deve ser positivo, recebido {price:g}")
        return None
    return Invalidation(price=price, direction=direction)


def _parse_range(v: _Validator, where: str, raw: Any) -> ManualRange | None:
    if not isinstance(raw, dict):
        v.fail(where, "esperado mapeamento com as chaves `support` e `resistance`")
        return None
    unknown = set(raw) - {"support", "resistance"}
    if unknown:
        v.fail(where, f"chaves desconhecidas: {sorted(unknown)}")
    missing = [k for k in ("support", "resistance") if k not in raw]
    if missing:
        v.fail(where, f"campos obrigatórios ausentes: {missing}")
        return None
    support = v.number(f"{where}.support", raw["support"])
    resistance = v.number(f"{where}.resistance", raw["resistance"])
    if support is None or resistance is None:
        return None
    if support >= resistance:
        v.fail(where, f"support ({support:g}) deve ser menor que resistance ({resistance:g})")
        return None
    return ManualRange(support=support, resistance=resistance)


def _parse_calendar(v: _Validator, where: str, raw: Any) -> tuple[CalendarEvent, ...]:
    if not isinstance(raw, list):
        v.fail(where, "esperado uma lista de eventos")
        return ()
    events: list[CalendarEvent] = []
    for i, entry in enumerate(raw):
        item_where = f"{where}[{i}]"
        if not isinstance(entry, dict):
            v.fail(item_where, "esperado mapeamento com `date` e `label`")
            continue
        unknown = set(entry) - {"date", "label"}
        if unknown:
            v.fail(item_where, f"chaves desconhecidas: {sorted(unknown)}")
        if "date" not in entry:
            v.fail(f"{item_where}.date", "campo obrigatório")
            continue
        date = entry["date"]
        if isinstance(date, dt.datetime):
            date = date.date()
        elif isinstance(date, str):
            try:
                date = dt.date.fromisoformat(date)
            except ValueError:
                v.fail(f"{item_where}.date", f"data inválida {date!r} — use AAAA-MM-DD")
                continue
        elif not isinstance(date, dt.date):
            v.fail(f"{item_where}.date", f"esperado data AAAA-MM-DD, recebido {date!r}")
            continue
        events.append(CalendarEvent(date=date, label=str(entry.get("label", "")).strip()))
    return tuple(events)


def _parse_item(v: _Validator, index: int, raw: Any, defaults: dict) -> WatchItem | None:
    where = f"tickers[{index}]"
    if not isinstance(raw, dict):
        v.fail(where, f"esperado mapeamento, recebido {type(raw).__name__}")
        return None

    known = {"symbol", "market", "benchmark", "invalidation", "range", "notes", "calendar"}
    unknown = set(raw) - known
    if unknown:
        v.fail(where, f"chaves desconhecidas: {sorted(unknown)} (válidas: {sorted(known)})")

    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        v.fail(f"{where}.symbol", "campo obrigatório, texto não vazio")
        return None
    symbol = symbol.strip().upper()
    where = f"tickers[{index}] ({symbol})"

    market = raw.get("market")
    if market not in MARKETS:
        v.fail(f"{where}.market", f"esperado um de {list(MARKETS)}, recebido {market!r}")
        return None
    if market == "b3" and not symbol.endswith(".SA"):
        v.fail(f"{where}.symbol", "ticker de mercado `b3` precisa do sufixo `.SA` (ex.: PETR4.SA)")
        return None
    if market == "us" and symbol.endswith(".SA"):
        v.fail(f"{where}.symbol", "sufixo `.SA` não vale para mercado `us`")
        return None

    benchmark = raw.get("benchmark") or defaults.get(market) or DEFAULT_BENCHMARKS[market]
    if not isinstance(benchmark, str) or not benchmark.strip():
        v.fail(f"{where}.benchmark", "esperado texto não vazio (ex.: ^BVSP)")
        return None

    invalidation = None
    if raw.get("invalidation") is not None:
        invalidation = _parse_invalidation(v, f"{where}.invalidation", raw["invalidation"])

    manual_range = None
    if raw.get("range") is not None:
        manual_range = _parse_range(v, f"{where}.range", raw["range"])

    calendar: tuple[CalendarEvent, ...] = ()
    if raw.get("calendar") is not None:
        calendar = _parse_calendar(v, f"{where}.calendar", raw["calendar"])

    return WatchItem(
        symbol=symbol,
        market=market,
        benchmark=benchmark.strip(),
        invalidation=invalidation,
        manual_range=manual_range,
        notes=str(raw.get("notes") or "").strip(),
        calendar=calendar,
    )


def parse_watchlist(raw: Any, source: str = "watchlist.yaml") -> Watchlist:
    """Valida a estrutura já desserializada. Levanta WatchlistError com todos os erros."""
    v = _Validator()
    if not isinstance(raw, dict):
        raise WatchlistError(f"{source}: raiz do arquivo deve ser um mapeamento com a chave `tickers`.")

    defaults = {}
    raw_defaults = raw.get("defaults")
    if raw_defaults is not None:
        if not isinstance(raw_defaults, dict):
            v.fail("defaults", "esperado mapeamento")
        else:
            bench = raw_defaults.get("benchmark") or {}
            if not isinstance(bench, dict):
                v.fail("defaults.benchmark", "esperado mapeamento mercado -> índice")
            else:
                defaults = {k: str(val) for k, val in bench.items()}

    tickers = raw.get("tickers")
    if tickers is None:
        raise WatchlistError(f"{source}: chave `tickers` ausente.")
    if not isinstance(tickers, list):
        raise WatchlistError(f"{source}: `tickers` deve ser uma lista.")
    if not tickers:
        raise WatchlistError(f"{source}: `tickers` está vazia — adicione ao menos um papel.")

    items: list[WatchItem] = []
    seen: dict[str, int] = {}
    for i, entry in enumerate(tickers):
        item = _parse_item(v, i, entry, defaults)
        if item is None:
            continue
        if item.symbol in seen:
            v.fail(f"tickers[{i}] ({item.symbol})", f"símbolo duplicado (já definido em tickers[{seen[item.symbol]}])")
            continue
        seen[item.symbol] = i
        items.append(item)

    if v.errors:
        joined = "\n  - ".join(v.errors)
        raise WatchlistError(f"{source}: {len(v.errors)} erro(s) de validação:\n  - {joined}")

    return Watchlist(items=items)


def load_watchlist(path: str | Path = "watchlist.yaml") -> Watchlist:
    path = Path(path)
    if not path.exists():
        raise WatchlistError(f"{path}: arquivo não encontrado. Crie a watchlist antes de rodar o screener.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WatchlistError(f"{path}: YAML inválido — {exc}") from exc
    wl = parse_watchlist(raw, source=str(path))
    wl.path = path
    return wl
