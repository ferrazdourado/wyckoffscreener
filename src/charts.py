"""Gráfico semanal anotado (R8).

Candles + volume + limites do range + nível de invalidação + os eventos
detectados marcados na semana em que ocorreram. É a peça que fecha a história
do usuário "quero validar visualmente a leitura": se o gráfico e a tabela
discordam, quem manda é o gráfico e a heurística precisa de ajuste.

Matplotlib roda em backend `Agg` — nenhuma janela, nenhum display exigido.
"""

from __future__ import annotations

import base64
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

from .analysis import TickerAnalysis
from .config import Config
from .events import ACUMULACAO, DISTRIBUICAO, Event

# Sigla de cada evento no gráfico — o relatório traz o nome por extenso embaixo.
ABBREV = {
    "selling_climax": "SC",
    "buying_climax": "BC",
    "spring": "SPR",
    "test": "T",
    "sos": "SOS",
    "sow": "SOW",
    "lps": "LPS",
    "lpsy": "LPSY",
    "upthrust": "UT",
    "effort_vs_result": "E×R",
}

BULL = "#127c39"
BEAR = "#b3261e"
NEUTRAL = "#8a6d1f"


def _color(event: Event) -> str:
    if event.bias == ACUMULACAO:
        return BULL
    if event.bias == DISTRIBUICAO:
        return BEAR
    return NEUTRAL


def _ohlc(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars[["open", "high", "low", "close", "volume"]].copy()
    df.columns = ["Open", "High", "Low", "Close", "Volume"]
    df.index = pd.DatetimeIndex(df.index)
    return df


def render(analysis: TickerAnalysis, config: Config, out_dir: Path) -> Path | None:
    """Desenha o gráfico do papel e devolve o caminho do PNG (None se não deu)."""
    weeks = int(config.get("output.chart_weeks", 60))
    bars = analysis.closed.tail(weeks)
    if len(bars) < 2:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{analysis.symbol.replace('.', '_')}.png"
    df = _ohlc(bars)
    offset = len(analysis.closed) - len(bars)  # índices globais -> posição no recorte

    hlines, hcolors, hstyles = [], [], []
    tr = analysis.governing_range
    if tr is not None:
        hlines += [tr.support, tr.resistance]
        hcolors += ["#3b6fb0", "#3b6fb0"]
        hstyles += ["--", "--"]
    if analysis.item.invalidation is not None:
        hlines.append(analysis.item.invalidation.price)
        hcolors.append("#b3261e")
        hstyles.append("-")

    kwargs = {
        "type": "candle", "volume": True, "style": "charles", "figratio": (16, 9), "figscale": 1.1,
        "datetime_format": "%d/%m/%y", "xrotation": 0, "tight_layout": True, "returnfig": True,
        "ylabel": "preço", "ylabel_lower": "volume",
        "title": f"\n{analysis.symbol} — semanal — {analysis.phase.label}",
    }
    if hlines:
        kwargs["hlines"] = {"hlines": hlines, "colors": hcolors, "linestyle": hstyles, "linewidths": 1.0}

    fig, axes = mpf.plot(df, **kwargs)
    ax = axes[0]

    # Faixa do range sombreada, para o olho achar a lateralização de longe.
    if tr is not None:
        ax.axhspan(tr.support, tr.resistance, color="#3b6fb0", alpha=0.06, zorder=0)

    span = float(df["High"].max() - df["Low"].min()) or 1.0
    # Eventos em semanas vizinhas do mesmo lado se empilham em vez de escreverem
    # um por cima do outro — um spring e o teste dele caem quase sempre colados.
    ultimo = {True: (-99, 0), False: (-99, 0)}
    extremos: list[float] = []
    for event in analysis.events:
        pos = event.index - offset
        if pos < 0 or pos >= len(df):
            continue
        bullish = event.bias == ACUMULACAO
        sigla = ABBREV.get(event.kind, event.kind[:3].upper())
        if event.kind == "upthrust" and event.refs.get("utad"):
            sigla = "UTAD"
        if event.kind == "spring" and not event.confirmed:
            sigla += "?"
        anterior_pos, nivel = ultimo[bullish]
        nivel = nivel + 1 if pos - anterior_pos <= 1 else 0
        ultimo[bullish] = (pos, nivel)
        recuo = (0.04 + 0.05 * nivel) * span
        y = (float(df["Low"].iloc[pos]) - recuo) if bullish else (float(df["High"].iloc[pos]) + recuo)
        extremos.append(y)
        ax.annotate(
            sigla, xy=(pos, y), ha="center",
            va="top" if bullish else "bottom",
            fontsize=7.5, fontweight="bold", color=_color(event),
            bbox={"boxstyle": "round,pad=0.18", "facecolor": "white",
                      "edgecolor": _color(event), "linewidth": 0.6, "alpha": 0.9},
        )

    # A pilha de anotações pode passar do desenho dos candles; abrir espaço para
    # ela em vez de deixar a sigla cortada na borda do eixo.
    if extremos:
        baixo, alto = ax.get_ylim()
        folga = 0.05 * span
        ax.set_ylim(min(baixo, min(extremos) - folga), max(alto, max(extremos) + folga))

    legenda = []
    if tr is not None:
        origem = "manual" if tr.source == "manual" else "detectado"
        legenda.append(f"range {tr.support:.2f}–{tr.resistance:.2f} ({origem}, {tr.weeks}s)")
    if analysis.item.invalidation is not None:
        legenda.append(f"invalidação {analysis.item.invalidation.price:.2f}")
    if legenda:
        # Rodapé do painel de preço: o topo é do título e das anotações de eventos.
        ax.text(0.005, 0.015, "  ·  ".join(legenda), transform=ax.transAxes,
                fontsize=7.5, va="bottom", color="#555",
                bbox={"boxstyle": "round,pad=0.25", "facecolor": "white",
                          "edgecolor": "none", "alpha": 0.85})

    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return path


def as_data_uri(path: Path) -> str:
    """PNG em base64, para o HTML sair autocontido e viajar bem pro celular."""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
