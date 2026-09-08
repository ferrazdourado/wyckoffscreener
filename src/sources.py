"""Comparação entre fontes de dados (P2 — sustenta a questão §8 da spec).

A §8 pergunta se o Yahoo entrega bem proventos e splits de papel brasileiro e
manda "definir fallback (brapi.dev) se houver buracos". Definir com o quê? Esta
é a régua: baixa a mesma série nas duas fontes e mostra, em números, onde elas
discordam — pregão que só uma tem, preço que difere além da tolerância, volume
fora de escala, provento registrado por uma e não pela outra.

A comparação é feita no candle DIÁRIO, que é o primitivo: um buraco de um
pregão some ao agregar a semana, mas é exatamente ele que desloca máxima,
mínima e volume da barra semanal que as heurísticas leem.

As funções de comparação são puras (DataFrame -> resultado); só
`compare_sources` fala com as fontes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from .data.provider import DataProvider, FetchError

PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class Divergence:
    date: dt.date
    column: str
    left: float
    right: float

    @property
    def diff_pct(self) -> float:
        base = abs(self.left) if self.left else abs(self.right)
        return (self.right - self.left) / base if base else 0.0

    def describe(self, left_name: str, right_name: str) -> str:
        return (f"{self.date:%d/%m/%Y}  {self.column:<6} "
                f"{left_name}={self.left:,.4f}  {right_name}={self.right:,.4f}  "
                f"({self.diff_pct:+.2%})")


@dataclass
class Comparison:
    symbol: str
    left_name: str
    right_name: str
    left_bars: int
    right_bars: int
    common: int
    only_left: list[dt.date] = field(default_factory=list)
    only_right: list[dt.date] = field(default_factory=list)
    price_divergences: list[Divergence] = field(default_factory=list)
    volume_divergences: list[Divergence] = field(default_factory=list)
    only_left_actions: list[tuple[dt.date, str]] = field(default_factory=list)
    only_right_actions: list[tuple[dt.date, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def agree(self) -> bool:
        return not (self.only_left or self.only_right or self.price_divergences
                    or self.volume_divergences or self.errors)

    @property
    def worst_price(self) -> Divergence | None:
        if not self.price_divergences:
            return None
        return max(self.price_divergences, key=lambda d: abs(d.diff_pct))

    def verdict(self) -> str:
        """Uma frase que responde à pergunta que motivou a comparação."""
        if self.errors:
            return "comparação incompleta: " + "; ".join(self.errors)
        if self.agree:
            return (f"as duas fontes contam a mesma história em {self.common} pregões — "
                    f"nada que justifique trocar de fonte para este papel.")
        partes = []
        if self.only_left:
            partes.append(f"{len(self.only_left)} pregão(ões) só em {self.left_name}")
        if self.only_right:
            partes.append(f"{len(self.only_right)} só em {self.right_name}")
        if self.price_divergences:
            pior = self.worst_price
            partes.append(f"{len(self.price_divergences)} preço(s) divergente(s) "
                          f"(pior: {pior.diff_pct:+.2%} em {pior.date:%d/%m/%Y})")
        if self.volume_divergences:
            partes.append(f"{len(self.volume_divergences)} volume(s) divergente(s)")
        if self.only_left_actions or self.only_right_actions:
            partes.append(f"{len(self.only_left_actions) + len(self.only_right_actions)} "
                          f"provento(s) registrado(s) por uma fonte só")
        return "; ".join(partes) + "."


def compare_bars(
    left: pd.DataFrame,
    right: pd.DataFrame,
    tolerance_pct: float,
    volume_tolerance_pct: float,
) -> tuple[list[dt.date], list[dt.date], list[Divergence], list[Divergence]]:
    """Divergências entre duas séries OHLCV, restritas ao período em comum.

    O recorte ao período comum não é detalhe: as fontes têm janelas diferentes
    (a brapi só oferece faixas fixas), e sem ele o começo de uma série apareceria
    como centenas de "pregões faltando" na outra.
    """
    if left.empty or right.empty:
        return [], [], [], []
    inicio = max(left.index.min(), right.index.min())
    fim = min(left.index.max(), right.index.max())
    esq = left.loc[(left.index >= inicio) & (left.index <= fim)]
    dir_ = right.loc[(right.index >= inicio) & (right.index <= fim)]

    datas_esq, datas_dir = set(esq.index), set(dir_.index)
    so_esq = sorted(d.date() for d in datas_esq - datas_dir)
    so_dir = sorted(d.date() for d in datas_dir - datas_esq)

    precos: list[Divergence] = []
    volumes: list[Divergence] = []
    for data in sorted(datas_esq & datas_dir):
        a, b = esq.loc[data], dir_.loc[data]
        for coluna in PRICE_COLUMNS:
            va, vb = float(a[coluna]), float(b[coluna])
            base = abs(va) or abs(vb)
            if base and abs(vb - va) / base > tolerance_pct:
                precos.append(Divergence(data.date(), coluna, va, vb))
        va, vb = float(a["volume"]), float(b["volume"])
        base = abs(va) or abs(vb)
        if base and abs(vb - va) / base > volume_tolerance_pct:
            volumes.append(Divergence(data.date(), "volume", va, vb))
    return so_esq, so_dir, precos, volumes


def compare_actions(
    left: pd.DataFrame, right: pd.DataFrame, window: tuple[dt.date, dt.date] | None = None
) -> tuple[list[tuple[dt.date, str]], list[tuple[dt.date, str]]]:
    """Proventos que uma fonte registra e a outra não, dentro da janela comum."""
    def conjunto(frame: pd.DataFrame) -> set[tuple[dt.date, str]]:
        if frame is None or frame.empty:
            return set()
        itens = {(pd.Timestamp(r["date"]).date(), str(r["kind"])) for _, r in frame.iterrows()}
        if window:
            itens = {i for i in itens if window[0] <= i[0] <= window[1]}
        return itens

    esq, dir_ = conjunto(left), conjunto(right)
    return sorted(esq - dir_), sorted(dir_ - esq)


def compare_sources(
    symbol: str,
    weeks: int,
    left: tuple[str, DataProvider],
    right: tuple[str, DataProvider],
    tolerance_pct: float = 0.005,
    volume_tolerance_pct: float = 0.01,
    with_actions: bool = True,
) -> Comparison:
    """Baixa a mesma série nas duas fontes e devolve as divergências."""
    (nome_esq, fonte_esq), (nome_dir, fonte_dir) = left, right
    resultado = Comparison(symbol, nome_esq, nome_dir, 0, 0, 0)

    series = {}
    for nome, fonte in ((nome_esq, fonte_esq), (nome_dir, fonte_dir)):
        try:
            series[nome] = fonte.daily_bars(symbol, weeks)
        except FetchError as exc:
            resultado.errors.append(f"{nome}: {exc}")
            series[nome] = pd.DataFrame()
    esq, dir_ = series[nome_esq], series[nome_dir]
    resultado.left_bars, resultado.right_bars = len(esq), len(dir_)
    if esq.empty or dir_.empty:
        return resultado

    so_esq, so_dir, precos, volumes = compare_bars(esq, dir_, tolerance_pct, volume_tolerance_pct)
    resultado.only_left, resultado.only_right = so_esq, so_dir
    resultado.price_divergences, resultado.volume_divergences = precos, volumes
    inicio = max(esq.index.min(), dir_.index.min())
    fim = min(esq.index.max(), dir_.index.max())
    resultado.common = len(esq.loc[(esq.index >= inicio) & (esq.index <= fim)]) - len(so_esq)

    if with_actions:
        proventos = {}
        for nome, fonte in ((nome_esq, fonte_esq), (nome_dir, fonte_dir)):
            try:
                proventos[nome] = fonte.corporate_actions(symbol)
            except FetchError as exc:
                resultado.errors.append(f"{nome} (proventos): {exc}")
                proventos[nome] = pd.DataFrame()
        resultado.only_left_actions, resultado.only_right_actions = compare_actions(
            proventos[nome_esq], proventos[nome_dir], (inicio.date(), fim.date())
        )
    return resultado
