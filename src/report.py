"""Relatório semanal em Markdown e HTML (R8).

Monta um modelo de dados único e renderiza os dois formatos a partir dele, para
que Markdown e HTML nunca contem histórias diferentes.

A ordem das seções é a da spec e não é arbitrária: primeiro o que exige ação
(invalidação violada), depois o que mudou (eventos da semana), depois o
panorama (tabela), depois o detalhe por papel e, por último, o que falhou na
coleta — porque um erro de coleta silencioso é pior do que um sinal errado.

Só entram no detalhe os eventos que aparecem no gráfico: o que o usuário lê na
lista é o que ele consegue conferir no candle logo acima.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import charts
from .alerts import collect_alerts, levels_configured, suggest_invalidation
from . import pnf
from .analysis import TickerAnalysis, analyze_all, week_tag
from .config import Config
from .phases import INDEFINIDO

DISCLAIMER = ("Ferramenta de estudo pessoal. Sinais heurísticos, sem garantia. "
              "Não constitui recomendação de investimento.")

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

BIAS_LABEL = {"acumulacao": "acumulação", "distribuicao": "distribuição", INDEFINIDO: "indefinido"}


@dataclass
class ReportPaths:
    markdown: Path
    html: Path
    charts_dir: Path


def _fmt(value, spec: str = "{:.2f}") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        return spec.format(float(value))
    except (TypeError, ValueError):
        return str(value)


def _semanas(n) -> str:
    """"1 semana" / "3 semanas" — plural certo em texto que o usuário lê toda sexta."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    return "1 semana" if n == 1 else f"{n} semanas"


def _rs_columns(config: Config) -> list[tuple[str, str]]:
    return [(f"rs_{int(w)}w", f"FR {int(w)}s") for w in config.require("metrics.relative_strength_weeks")]


def build_model(
    watchlist,
    metrics: dict[str, pd.DataFrame],
    config: Config,
    now: dt.datetime,
    fetch_errors: list | None = None,
    metric_warnings: list | None = None,
    charts_root: Path | None = None,
    daily: dict[str, pd.DataFrame] | None = None,
    screen_results: list | None = None,
) -> dict:
    """Todo o conteúdo do relatório, pronto para os dois templates.

    `charts_root` é o diretório dos relatórios; os PNGs vão para
    `<charts_root>/<tag>/charts`. A etiqueta da semana só é conhecida depois de
    analisar, então quem decide o destino dos gráficos é esta função — passá-lo
    pronto de fora obrigaria a rodar a análise inteira duas vezes.
    """
    analyses, missing = analyze_all(watchlist, metrics, config)
    tag = week_tag(analyses, now.date())
    charts_dir = (charts_root / tag / "charts") if charts_root is not None else None
    invalidations, calendar = collect_alerts(watchlist, metrics, config, now.date())
    rs_cols = _rs_columns(config)

    week_events = []
    for a in analyses:
        for ev in a.recent_events:
            week_events.append({"symbol": a.symbol, "event": ev, "analysis": a})
    ordem = {"acumulacao": 0, "distribuicao": 1}
    week_events.sort(key=lambda d: (ordem.get(d["event"].bias, 2), d["symbol"]))

    rows = []
    for a in analyses:
        rows.append({
            "symbol": a.symbol,
            "market": a.item.market,
            "phase": a.phase.label,
            "phase_code": a.phase.code,
            "bias": BIAS_LABEL.get(a.phase.bias, a.phase.bias),
            "weeks_in_phase": a.phase.weeks_in_phase(len(a.closed)),
            "pending": a.phase.pending,
            "close": _fmt(a.value("close")),
            "volume_ratio": _fmt(a.value("volume_ratio"), "{:.2f}×"),
            "spread_ratio": _fmt(a.value("spread_ratio"), "{:.2f}×"),
            "close_position": _fmt(a.value("close_position"), "{:.0%}"),
            "rs": [_fmt(a.value(col), "{:+.1%}") for col, _ in rs_cols],
            "range": (f"{a.governing_range.support:.2f}–{a.governing_range.resistance:.2f}"
                      if a.governing_range else "—"),
            "range_active": bool(a.active_range),
            "range_weeks": a.governing_range.weeks if a.governing_range else 0,
            "invalidation": (_fmt(a.item.invalidation.price) if a.item.invalidation else "—"),
            "invalidation_gap": (_fmt(a.invalidation_gap, "{:+.1%}") if a.invalidation_gap is not None else "—"),
            "violated": a.alert is not None,
            "events_this_week": len(a.recent_events),
        })

    chart_weeks = int(config.get("output.chart_weeks", 60))
    embed = bool(config.get("output.embed_charts", True))
    sections = []
    for a in analyses:
        png = charts.render(a, config, charts_dir) if charts_dir is not None else None
        visible_from = len(a.closed) - min(chart_weeks, len(a.closed))
        detailed = [e for e in a.events if e.index >= visible_from]
        sections.append({
            "analysis": a,
            "symbol": a.symbol,
            "cause": pnf.for_analysis(a, config, (daily or {}).get(a.symbol)),
            "chart_path": png,
            "chart_rel": (png.parent.name + "/" + png.name) if png else None,
            "chart_uri": charts.as_data_uri(png) if (png and embed) else None,
            "detailed_events": list(reversed(detailed)),
            "hidden_events": len(a.events) - len(detailed),
            "calendar": [(ev, days) for sym, ev, days in calendar if sym == a.symbol],
            "rs": [(label, _fmt(a.value(col), "{:+.1%}")) for col, label in rs_cols],
        })

    analysis_warnings = [(a.symbol, w) for a in analyses for w in a.warnings
                         if "em formação" not in w]
    with_level, total = levels_configured(watchlist)
    return {
        "tag": tag,
        "week_start": max((a.latest.name.date() for a in analyses if a.latest is not None),
                          default=now.date()),
        "generated_at": now,
        "invalidations": invalidations,
        "calendar": calendar,
        "calendar_horizon": int(config.get("alerts.calendar_horizon_days", 14)),
        "week_events": week_events,
        "rows": rows,
        "candidates": _candidate_blocks(screen_results, config),
        "rs_labels": [label for _, label in rs_cols],
        "sections": sections,
        "problems": _merge_problems(fetch_errors, missing, metric_warnings, analysis_warnings),
        "errors": list(fetch_errors or []),
        "missing": missing,
        "partials": [{"symbol": a.symbol, "week": a.partial_week.date()}
                     for a in analyses if a.partial_week is not None],
        "levels_configured": with_level,
        "levels_total": total,
        "config": config,
        "disclaimer": DISCLAIMER,
        "params": {
            "volume_sma_weeks": config.get("metrics.volume_sma_weeks"),
            "atr_weeks": config.get("metrics.atr_weeks"),
            "range_min_weeks": config.get("ranges.min_weeks"),
            "range_dispersion": config.get("ranges.max_close_dispersion_pct"),
            "chart_weeks": chart_weeks,
        },
    }


def _merge_problems(
    fetch_errors, missing, metric_warnings, analysis_warnings
) -> list[dict]:
    """Uma linha por papel na seção 5, não uma por etapa que tropeçou nele.

    Um ticker inexistente falha na coleta, depois "não está no cache", depois
    "sem candles" — três frases para a mesma causa. A seção que deveria ser
    varrida em cinco segundos triplicava de tamanho a cada papel quebrado, então
    vence a mensagem mais a montante, que é a que explica as outras.
    """
    ordem = {"coleta": 0, "cache": 1, "métricas": 2, "análise": 3}
    por_simbolo: dict[str, dict] = {}

    def registrar(symbol: str, origem: str, message: str) -> None:
        message = str(message)
        prefixo = f"{symbol}: "
        while message.startswith(prefixo):   # a fonte já carimba o símbolo na mensagem
            message = message[len(prefixo):]
        atual = por_simbolo.get(symbol)
        if atual is None or ordem[origem] < ordem[atual["origem"]]:
            por_simbolo[symbol] = {"symbol": symbol, "origem": origem, "message": message}

    for status in fetch_errors or []:
        registrar(status.symbol, "coleta", status.message)
    for symbol in missing or []:
        registrar(symbol, "cache", "sem dados no cache — rode `wyckoff fetch`.")
    for status in metric_warnings or []:
        registrar(status.symbol, "métricas", status.message)
    for symbol, warning in analysis_warnings or []:
        registrar(symbol, "análise", warning)
    return sorted(por_simbolo.values(), key=lambda d: d["symbol"])


def _candidate_blocks(screen_results, config: Config) -> list[dict]:
    """Resultados do screener no formato que os dois templates consomem.

    Cada candidato já sai com a linha `wyckoff add` pronta: achar o papel e não
    saber o que fazer com ele deixaria a seção a meio caminho.
    """
    rs_windows = [int(w) for w in config.require("metrics.relative_strength_weeks")]
    rs_col = f"rs_{rs_windows[-1]}w" if rs_windows else "rs_12w"
    blocos = []
    for resultado in screen_results or []:
        itens = []
        for c in resultado.candidates:
            sugestao = suggest_invalidation(c.analysis)
            itens.append({
                "symbol": c.symbol,
                "phase": c.analysis.phase.label,
                "event": c.last_event.label if c.last_event else "—",
                "weeks_since": c.weeks_since_event,
                "rs": _fmt(c.analysis.value(rs_col), "{:+.1%}"),
                "liquidity": c.liquidity,
                "close": _fmt(c.analysis.value("close")),
                "invalidation": f"{sugestao[0]:.2f}" if sugestao else None,
                "direction": sugestao[1] if sugestao else None,
            })
        blocos.append({
            "universe": resultado.universe.name,
            "market": resultado.universe.market,
            "scanned": resultado.scanned,
            "matched": resultado.matched,
            "illiquid": resultado.illiquid,
            "stale": resultado.stale,
            "max_weeks_since_event": resultado.max_weeks_since_event,
            "min_liquidity": resultado.min_liquidity,
            "phases": list(resultado.phases),
            "candidates": itens,
        })
    return blocos


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["fmt"] = _fmt
    env.filters["semanas"] = _semanas
    return env


def render(model: dict, config: Config) -> ReportPaths:
    """Escreve reports/AAAA-SS.md e reports/AAAA-SS.html."""
    out_dir = Path(config.get("output.reports_dir", "reports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    env = _env()
    md_path = out_dir / f"{model['tag']}.md"
    html_path = out_dir / f"{model['tag']}.html"
    md_path.write_text(env.get_template("report.md.j2").render(**model), encoding="utf-8")
    html_path.write_text(env.get_template("report.html.j2").render(**model), encoding="utf-8")
    return ReportPaths(markdown=md_path, html=html_path, charts_dir=out_dir / model["tag"])


def generate(
    watchlist,
    metrics: dict[str, pd.DataFrame],
    config: Config,
    now: dt.datetime | None = None,
    fetch_errors: list | None = None,
    metric_warnings: list | None = None,
    daily: dict[str, pd.DataFrame] | None = None,
    screen_results: list | None = None,
) -> tuple[ReportPaths, dict]:
    """Caminho completo de `wyckoff report`: analisa, desenha, renderiza."""
    now = now or dt.datetime.now()
    out_dir = Path(config.get("output.reports_dir", "reports"))
    model = build_model(watchlist, metrics, config, now, fetch_errors, metric_warnings,
                        out_dir, daily, screen_results)
    return render(model, config), model
