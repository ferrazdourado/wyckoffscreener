"""CLI do Wyckoff Screener (Fases 1 e 2).

    wyckoff validate            valida config.yaml + watchlist.yaml
    wyckoff fetch               baixa/atualiza dados e calcula as métricas
    wyckoff metrics             recalcula do cache, sem rede
    wyckoff report              relatório semanal completo (.md + .html)
    wyckoff notify              manda o resumo da semana pelo canal configurado
    wyckoff screen              varre um universo amplo e ranqueia candidatos
    wyckoff backtest            mede o que vem depois de cada regra (calibragem)
    wyckoff universe            lista os universos ou confere se os tickers vivem
    wyckoff sources TICKER      compara duas fontes de dados no mesmo papel
    wyckoff pdf [SEMANA]        converte o relatório da semana para PDF
    wyckoff dashboard           abre o dashboard local (Streamlit) sobre o cache
    wyckoff analyze [TICKER]    fase, range e eventos no terminal
    wyckoff show TICKER         últimas N semanas de um papel, com os números
    wyckoff verify TICKER       abre a conta de cada métrica p/ conferência
    wyckoff add TICKER          acrescenta um papel à watchlist
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import yaml

from .config import Config, ConfigError, load_config
from .data.cache import Cache
from .data.factory import ProviderError, build_provider, build_single
from .metrics import latest_row
from .pipeline import build_metrics, export_csv, fetch_all
from .screener import UniverseError
from .watchlist import WatchlistError, load_watchlist


def summary_columns(config: Config) -> list[tuple[str, str, int]]:
    """Colunas da tabela-resumo (coluna, rótulo, casas decimais).

    Derivadas do config: mudar `relative_strength_weeks` muda a tabela, em vez
    de deixá-la procurando colunas que não existem mais.
    """
    vol_weeks = int(config.require("metrics.volume_sma_weeks"))
    atr_weeks = int(config.require("metrics.atr_weeks"))
    cols = [
        ("close", "close", 2),
        ("volume_ratio", f"vol/méd{vol_weeks}", 2),
        ("spread_ratio", f"spread/ATR{atr_weeks}", 2),
        ("close_position", "pos.fech.", 2),
    ]
    for weeks in config.require("metrics.relative_strength_weeks"):
        cols.append((f"rs_{int(weeks)}w", f"FR {int(weeks)}s", 3))
    return cols


def _fmt(value, decimals: int) -> str:
    if value is None or value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{float(value):.{decimals}f}"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    head = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    return "\n".join([head, sep, *body])


def _summary_table(metrics: dict[str, pd.DataFrame], watchlist, config: Config) -> str:
    columns = summary_columns(config)
    headers = ["ticker", "semana", *[label for _, label, _ in columns], "semanas"]
    rows = []
    for item in watchlist:
        df = metrics.get(item.symbol)
        if df is None or df.empty:
            rows.append([item.symbol, "—", *["—"] * len(columns), "0"])
            continue
        row = latest_row(df, closed_only=True)
        if row is None:
            rows.append([item.symbol, "(só semana em aberto)", *["—"] * len(columns), str(len(df))])
            continue
        cells = [_fmt(row.get(col), dec) for col, _, dec in columns]
        rows.append([item.symbol, row.name.date().isoformat(), *cells, str(len(df))])
    return _table(rows, headers)


def _load(args) -> tuple[Config, object]:
    return load_config(args.config), load_watchlist(args.watchlist)


def cmd_validate(args) -> int:
    config, watchlist = _load(args)
    print(f"config.yaml    ok — ATR {config.get('metrics.atr_weeks')}s "
          f"({config.get('metrics.atr_method')}), volume SMA {config.get('metrics.volume_sma_weeks')}s, "
          f"histórico {config.get('data.history_weeks')}s, "
          f"força relativa {config.get('metrics.relative_strength_weeks')}s")
    print(f"watchlist.yaml ok — {len(watchlist)} papéis, benchmarks {watchlist.benchmarks}")
    for item in watchlist:
        extras = []
        if item.invalidation:
            extras.append(f"invalidação: {item.invalidation.describe()}")
        if item.manual_range:
            extras.append(f"range manual {item.manual_range.support:g}–{item.manual_range.resistance:g}")
        if item.calendar:
            extras.append(f"{len(item.calendar)} evento(s) de calendário")
        suffix = f"  [{'; '.join(extras)}]" if extras else ""
        print(f"  {item.symbol:<10} {item.market:<3} vs {item.benchmark}{suffix}")
    return 0


def _report_problems(title: str, problems) -> None:
    if not problems:
        return
    print(f"\n{title}")
    for p in problems:
        print(f"  ! {p.symbol}: {p.message}")


def cmd_fetch(args) -> int:
    config, watchlist = _load(args)
    now = dt.datetime.now()
    with Cache(config.require("data.cache_path")) as cache:
        print(f"Coletando {len(watchlist)} papéis + {len(watchlist.benchmarks)} índices "
              f"({config.get('data.history_weeks')} semanas)...")
        provider = build_provider(config, watchlist=watchlist)
        report = fetch_all(watchlist, config, provider, cache, force=args.force, now=now)
        for status in report.statuses:
            mark = {"ok": "+", "cached": "=", "error": "!"}[status.status]
            note = f"  {status.message}" if status.message else ""
            print(f"  {mark} {status.symbol:<10} {status.rows:>4} semanas{note}")
        if args.no_metrics:
            return 1 if report.errors else 0
        return _metrics_stage(config, watchlist, cache, now, extra_problems=report.errors)


def cmd_metrics(args) -> int:
    config, watchlist = _load(args)
    with Cache(config.require("data.cache_path")) as cache:
        return _metrics_stage(config, watchlist, cache, dt.datetime.now())


def _metrics_stage(config, watchlist, cache, now, extra_problems=None) -> int:
    metrics, problems = build_metrics(watchlist, config, cache)
    if not metrics:
        print("\nNenhum papel com dados suficientes. Rode `wyckoff fetch` primeiro.", file=sys.stderr)
        return 1
    full_path, latest_path = export_csv(metrics, watchlist, config, now)
    print("\nÚltima semana FECHADA por papel:\n")
    print(_summary_table(metrics, watchlist, config))
    print(f"\nCSV completo: {full_path}")
    print(f"CSV resumo:   {latest_path}")

    pending = [s for s, df in metrics.items()
               if "is_partial" in df.columns and len(df) and bool(df["is_partial"].astype(bool).iloc[-1])]
    if pending:
        print(f"\nSemana em formação (candle NÃO fechado, excluído do resumo): {', '.join(sorted(pending))}")

    _report_problems("Avisos:", problems)
    _report_problems("Erros de coleta:", extra_problems or [])
    return 0


def cmd_show(args) -> int:
    config, watchlist = _load(args)
    symbol = args.ticker.strip().upper()
    with Cache(config.require("data.cache_path")) as cache:
        metrics, _ = build_metrics(watchlist, config, cache)
    if symbol not in metrics:
        print(f"{symbol}: sem métricas. Está na watchlist? Rodou `wyckoff fetch`?", file=sys.stderr)
        return 1
    df = metrics[symbol].tail(args.weeks)
    cols = ["open", "high", "low", "close", "volume", "trading_days", "volume_sma",
            "volume_ratio", "atr", "spread", "spread_ratio", "close_position"]
    cols += [f"rs_{int(w)}w" for w in config.require("metrics.relative_strength_weeks")]
    cols += ["ex_dividend", "ex_split", "is_partial"]
    cols = [c for c in cols if c in df.columns]
    item = watchlist.get(symbol)
    print(f"{symbol} ({item.market}) vs {item.benchmark} — últimas {len(df)} semanas\n")
    with pd.option_context("display.width", 250, "display.max_columns", 50):
        print(df[cols].round(config.get("output.decimals", 3)).to_string())
    return 0


def cmd_verify(args) -> int:
    """Abre a conta de cada métrica da última semana fechada, para conferência manual."""
    config, watchlist = _load(args)
    symbol = args.ticker.strip().upper()
    item = watchlist.get(symbol)
    if item is None:
        print(f"{symbol}: não está na watchlist.", file=sys.stderr)
        return 1

    vol_weeks = int(config.require("metrics.volume_sma_weeks"))
    atr_weeks = int(config.require("metrics.atr_weeks"))
    atr_method = str(config.require("metrics.atr_method"))
    rs_windows = [int(w) for w in config.require("metrics.relative_strength_weeks")]

    with Cache(config.require("data.cache_path")) as cache:
        bars = cache.get_bars(symbol)
        bench = cache.get_bars(item.benchmark)
        actions = cache.get_actions(symbol, since=bars.index[0].date()) if not bars.empty else None
    if bars.empty:
        print(f"{symbol}: sem candles no cache — rode `wyckoff fetch`.", file=sys.stderr)
        return 1

    from .metrics import compute_metrics, true_range
    from .pipeline import flag_ex_dates

    metrics = compute_metrics(bars, config, bench if not bench.empty else None)
    metrics = flag_ex_dates(metrics, actions)
    closed = metrics[~metrics["is_partial"].astype(bool)] if "is_partial" in metrics else metrics
    if closed.empty:
        print(f"{symbol}: nenhuma semana fechada no cache.", file=sys.stderr)
        return 1
    row = closed.iloc[-1]
    i = metrics.index.get_loc(row.name)

    print(f"\n{symbol} ({item.market}) vs {item.benchmark} — conferência da semana de {row.name.date()}")
    print(f"histórico no cache: {len(bars)} semanas ({bars.index[0].date()} a {bars.index[-1].date()})\n")

    print("CANDLE (agregado dos pregões da semana, preços ajustados por proventos)")
    print(f"  open  {row['open']:.4f}   high {row['high']:.4f}   low {row['low']:.4f}   close {row['close']:.4f}")
    print(f"  volume {row['volume']:,.0f}   pregões na semana: {int(row.get('trading_days', 0))}")
    if bool(row.get("ex_split", False)):
        print("  ATENÇÃO: houve SPLIT com data-ex nesta semana — leia o candle com ressalva.")
    elif bool(row.get("ex_dividend", False)):
        print("  Nota: houve provento com data-ex nesta semana.")

    print(f"\nVOLUME RELATIVO  (volume / SMA{vol_weeks})")
    if i + 1 < vol_weeks:
        print(f"  janela incompleta: {i + 1} semanas antes desta, precisa de {vol_weeks} "
              f"-> métrica indefinida (NaN), sem conta a conferir")
    else:
        vols = metrics["volume"].iloc[i - vol_weeks + 1 : i + 1]
        print(f"  soma dos últimos {vol_weeks} volumes = {vols.sum():,.0f}")
        print(f"  SMA{vol_weeks} = {vols.sum():,.0f} / {vol_weeks} = {row['volume_sma']:,.2f}")
        print(f"  {row['volume']:,.0f} / {row['volume_sma']:,.2f} = {row['volume_ratio']:.4f}")

    print(f"\nSPREAD RELATIVO  ((high-low) / ATR{atr_weeks}, método {atr_method})")
    tr = true_range(metrics)
    if i == 0:
        print(f"  TR da semana = high-low = {tr.iloc[i]:.4f} (primeira barra, sem fechamento anterior)")
    else:
        prev_close = metrics["close"].iloc[i - 1]
        print(f"  TR da semana = max(high-low={row['high'] - row['low']:.4f}, "
              f"|high-fech.ant|={abs(row['high'] - prev_close):.4f}, "
              f"|low-fech.ant|={abs(row['low'] - prev_close):.4f}) = {tr.iloc[i]:.4f}")
    if i + 1 < atr_weeks:
        print(f"  janela incompleta: precisa de {atr_weeks} semanas -> ATR indefinido (NaN)")
    else:
        if atr_method == "sma":
            trs = tr.iloc[i - atr_weeks + 1 : i + 1]
            print(f"  ATR{atr_weeks} = soma dos {atr_weeks} TRs ({trs.sum():.4f}) / {atr_weeks} = {row['atr']:.4f}")
        else:
            print(f"  ATR{atr_weeks} (Wilder) = {row['atr']:.4f}")
        print(f"  spread = {row['high']:.4f} - {row['low']:.4f} = {row['spread']:.4f}")
        print(f"  {row['spread']:.4f} / {row['atr']:.4f} = {row['spread_ratio']:.4f}")

    print("\nPOSIÇÃO DO FECHAMENTO  ((close-low) / (high-low))")
    print(f"  ({row['close']:.4f} - {row['low']:.4f}) / ({row['high']:.4f} - {row['low']:.4f}) "
          f"= {row['close'] - row['low']:.4f} / {row['spread']:.4f} = {row['close_position']:.4f}")

    print(f"\nFORÇA RELATIVA  (papel - {item.benchmark})")
    bench_aligned = bench["close"].reindex(metrics.index) if not bench.empty else None
    for weeks in rs_windows:
        base_i = i - weeks
        if base_i < 0:
            print(f"  {weeks}s: histórico insuficiente ({i + 1} semanas antes desta)")
            continue
        c0, c1 = metrics["close"].iloc[base_i], row["close"]
        line = (f"  {weeks}s (base {metrics.index[base_i].date()}): papel {c0:.4f} -> {c1:.4f} = "
                f"{row[f'perf_{weeks}w'] * 100:+.2f}%")
        bench_perf = row.get(f"bench_perf_{weeks}w")
        if bench_aligned is not None and bench_perf is not None and not pd.isna(bench_perf):
            line += (f" | índice {bench_aligned.iloc[base_i]:,.2f} -> {bench_aligned.iloc[i]:,.2f} = "
                     f"{bench_perf * 100:+.2f}% | FR = {row[f'rs_{weeks}w'] * 100:+.2f} p.p.")
        print(line)

    n_actions = 0 if actions is None else len(actions)
    semanas_ex = int(metrics["ex_dividend"].sum() + metrics["ex_split"].sum())
    print(f"\nPROVENTOS/SPLITS na janela de {len(metrics)} semanas: {n_actions} (em {semanas_ex} semanas)")
    if n_actions:
        for _, a in actions.tail(5).iterrows():
            print(f"  {a['date']}  {a['kind']:<9} {a['value']}")
        if n_actions > 5:
            print(f"  ... (+{n_actions - 5} anteriores)")
    return 0


def cmd_add(args) -> int:
    path = Path(args.watchlist)
    _, watchlist = _load(args)
    symbol = args.ticker.strip().upper()
    if watchlist.get(symbol):
        print(f"{symbol} já está na watchlist.", file=sys.stderr)
        return 1

    entry: dict = {"symbol": symbol, "market": args.market or ("b3" if symbol.endswith(".SA") else "us")}
    if args.benchmark:
        entry["benchmark"] = args.benchmark
    if args.invalidation is not None:
        entry["invalidation"] = {"price": args.invalidation, "direction": args.direction}
    if args.notes:
        entry["notes"] = args.notes

    # safe_dump cuida do escape — montar YAML com f-string corrompe a watchlist
    # em notas com aspas.
    snippet = yaml.safe_dump([entry], allow_unicode=True, sort_keys=False, default_flow_style=False)
    block = "\n".join("  " + line for line in snippet.rstrip("\n").splitlines())

    original = path.read_text(encoding="utf-8")
    path.write_text(original.rstrip("\n") + "\n" + block + "\n", encoding="utf-8")
    try:
        load_watchlist(path)
    except WatchlistError:
        path.write_text(original, encoding="utf-8")  # nunca deixar o arquivo quebrado
        raise
    print(f"{symbol} adicionado a {path} (mercado {entry['market']}).")
    return 0


def cmd_report(args) -> int:
    """R8: relatório semanal completo. O comando único da rotina de sexta."""
    from .report import generate

    config, watchlist = _load(args)
    now = dt.datetime.now()
    fetch_errors = []
    with Cache(config.require("data.cache_path")) as cache:
        if not args.offline:
            print(f"Coletando {len(watchlist)} papéis + {len(watchlist.benchmarks)} índices...")
            provider = build_provider(config, watchlist=watchlist)
            report = fetch_all(watchlist, config, provider, cache, force=args.force, now=now)
            for status in report.statuses:
                if status.status == "error":
                    print(f"  ! {status.symbol}: {status.message}")
            fetch_errors = report.errors
        metrics, problems = build_metrics(watchlist, config, cache)
        # Contagem de causa (R10) precisa do candle diário, não do semanal.
        daily = {s: cache.get_daily_bars(s) for s in metrics}
    if not metrics:
        print("\nNenhum papel com dados. Rode `wyckoff fetch` primeiro.", file=sys.stderr)
        return 1

    print(f"Analisando e desenhando {len(metrics)} papéis...")
    paths, model = generate(watchlist, metrics, config, now, fetch_errors, problems, daily)

    print(f"\nRelatório da semana {model['tag']} ({model['week_start']:%d/%m/%Y}):")
    print(f"  Markdown: {paths.markdown}")
    print(f"  HTML:     {paths.html}")
    novos = [a for a in model["invalidations"] if a.is_new]
    if model["invalidations"]:
        print(f"\n  {len(model['invalidations'])} invalidação(ões) violada(s)"
              f"{f', {len(novos)} nova(s)' if novos else ''}:")
        for alert in model["invalidations"]:
            print(f"    {'[NOVO] ' if alert.is_new else ''}{alert.summary}")
    if model["week_events"]:
        print(f"\n  {len(model['week_events'])} evento(s) na semana:")
        for item in model["week_events"]:
            print(f"    {item['symbol']:<10} {item['event'].label}")
    else:
        print("\n  Nenhum evento Wyckoff na semana.")
    if fetch_errors:
        print(f"\n  {len(fetch_errors)} erro(s) de coleta — ver seção 5 do relatório.")
    codigo = _exportar_pdf(paths.html, config) if args.pdf else 0
    if args.notify:
        return _notificar(model, config) or codigo
    return codigo


def cmd_analyze(args) -> int:
    """Mesma leitura do relatório, no terminal, com os números de cada regra."""
    from .analysis import analyze_all

    from . import pnf

    config, watchlist = _load(args)
    with Cache(config.require("data.cache_path")) as cache:
        metrics, _ = build_metrics(watchlist, config, cache)
        analyses, missing = analyze_all(watchlist, metrics, config)
        contagens = {a.symbol: pnf.for_analysis(a, config, cache.get_daily_bars(a.symbol))
                     for a in analyses}
    if args.ticker:
        symbol = args.ticker.strip().upper()
        analyses = [a for a in analyses if a.symbol == symbol]
        if not analyses:
            print(f"{symbol}: sem análise. Está na watchlist? Rodou `wyckoff fetch`?", file=sys.stderr)
            return 1

    for a in analyses:
        tr = a.governing_range
        print(f"\n{a.symbol} — {a.phase.label}")
        semanas = a.phase.weeks_in_phase(len(a.closed))
        print(f"  desde {a.phase.since_date} ({semanas} "
              f"{'semana' if semanas == 1 else 'semanas'}) | gatilho: {a.phase.reason[:90]}")
        print(f"  próximo evento esperado: {a.phase.pending}")
        if tr:
            estado = "ativo" if a.active_range else f"encerrado em {tr.end_date}"
            print(f"  range: {tr.describe()} — {estado}")
        else:
            print("  range: nenhum em vigor")
        if a.alert:
            print(f"  ** INVALIDAÇÃO VIOLADA: {a.alert.summary}")
        contagem = contagens.get(a.symbol)
        if contagem is not None:
            print(f"  contagem de causa (P&F): {contagem.describe()}")
            for linha in contagem.audit_lines():
                print(f"      · {linha}")
        janela = int(config.get("output.chart_weeks", 60))
        visiveis = [e for e in a.events if e.index >= len(a.closed) - janela]
        if visiveis:
            print(f"  eventos ({len(visiveis)} nas últimas {janela} semanas):")
            for e in visiveis[-args.limit:]:
                marca = "" if e.confirmed else " [não confirmado]"
                print(f"    {e.date}  {e.label}{marca}")
                for line in e.audit_lines():
                    print(f"        · {line}")
        for w in a.warnings:
            print(f"  aviso: {w}")
    if missing:
        print(f"\nSem dados: {', '.join(missing)}")
    return 0


def _notificar(model, config) -> int:
    """Envia o resumo. Falha de envio não invalida o relatório já gerado."""
    from .notify import NotifyError, notify

    try:
        destino = notify(model, config)
    except NotifyError as exc:
        print(f"\nNotificação NÃO enviada: {exc}", file=sys.stderr)
        return 1
    print(f"\nResumo enviado por {destino}.")
    return 0


def cmd_notify(args) -> int:
    """R9: monta o relatório da semana em memória e manda o resumo."""
    from .analysis import analyze_all
    from .report import build_model

    config, watchlist = _load(args)
    if not args.force and not config.get("notify.enabled", False):
        print("notify.enabled = false no config.yaml. Ligue lá ou rode com --force.",
              file=sys.stderr)
        return 2
    with Cache(config.require("data.cache_path")) as cache:
        metrics, problems = build_metrics(watchlist, config, cache)
    if not metrics:
        print("Nenhum papel com dados. Rode `wyckoff fetch` primeiro.", file=sys.stderr)
        return 1
    # Sem charts_root: o resumo é texto, não precisa desenhar nada.
    model = build_model(watchlist, metrics, config, dt.datetime.now(), metric_warnings=problems)
    if args.dry_run:
        from .notify import build_message

        msg = build_message(model, config, int(config.get("notify.max_chars", 4096)))
        print(f"--- assunto ---\n{msg.subject}\n--- corpo ({len(msg.body)} caracteres) ---")
        print(msg.body)
        return 0
    return _notificar(model, config)


def cmd_screen(args) -> int:
    """R12: varre um universo amplo e ranqueia quem está em Fase C/D."""
    from .analysis import week_tag
    from .screener import UniverseError, export, load_universes, refresh, screen, to_frame

    config, _ = _load(args)
    caminho = args.universe_file or config.get("screener.universe_path", "universe.yaml")
    universos = load_universes(caminho)
    nome = args.universe or config.get("screener.default_universe")
    if nome not in universos:
        print(f"universo {nome!r} não existe em {caminho}. "
              f"Disponíveis: {', '.join(sorted(universos))}", file=sys.stderr)
        return 2
    universo = universos[nome]

    fases = tuple(f.strip().upper() for f in args.phases.split(",")) if args.phases \
        else tuple(str(f).upper() for f in config.get("screener.phases", ["C", "D"]))
    limite = args.top if args.top is not None else int(config.get("screener.top", 25))
    now = dt.datetime.now()

    with Cache(config.require("data.cache_path")) as cache:
        if not args.offline:
            print(f"Coletando {len(universo.tickers)} papéis do universo {nome} "
                  f"(+ {universo.benchmark})...")
            provider = build_provider(config, market=universo.market)
            relatorio = refresh(universo, config, provider, cache, force=args.force, now=now)
            for status in relatorio.errors:
                print(f"  ! {status.symbol}: {status.message}")
        resultado = screen(universo, config, cache, phases=fases, limit=limite)

    if not resultado.scanned:
        print("\nNenhum papel com histórico suficiente. Rode sem --offline primeiro.",
              file=sys.stderr)
        return 1

    print(f"\n{len(resultado.candidates)} candidato(s) em Fase {'/'.join(fases)} "
          f"de {resultado.scanned} papéis analisados ({nome}):\n")
    frame = to_frame(resultado, config)
    if frame.empty:
        print("  (nenhum papel nas fases pedidas nesta semana)")
    else:
        rs_cols = [f"rs_{int(w)}w" for w in config.require("metrics.relative_strength_weeks")]
        colunas = ["posicao", "symbol", "fase", "semanas_na_fase", "evento_recente",
                   "semanas_desde_evento", *rs_cols, "volume_ratio", "close"]
        colunas = [c for c in colunas if c in frame.columns]
        with pd.option_context("display.width", 220, "display.max_columns", 40):
            print(frame[colunas].round(config.get("output.decimals", 3)).to_string(index=False))

    tag = _iso_tag(resultado, now)
    caminho_csv = export(resultado, config, tag)
    print(f"\nCSV: {caminho_csv}")
    if resultado.problems:
        print(f"\n{len(resultado.problems)} papel(is) fora da análise:")
        for problema in resultado.problems[:10]:
            print(f"  ! {problema.symbol}: {problema.message}")
        if len(resultado.problems) > 10:
            print(f"  ... (+{len(resultado.problems) - 10})")
    return 0


def _iso_tag(resultado, now) -> str:
    """Etiqueta AAAA-SS da semana analisada, como nos demais arquivos."""
    semanas = [c.analysis.latest.name for c in resultado.candidates
               if c.analysis.latest is not None]
    data = max(semanas).date() if semanas else now.date()
    ano, semana, _ = data.isocalendar()
    return f"{ano}-{semana:02d}"


def cmd_universe(args) -> int:
    """Lista os universos e, com --check, confere ticker por ticker na fonte."""
    from .screener import check_universe, load_universes

    config, _ = _load(args)
    caminho = args.universe_file or config.get("screener.universe_path", "universe.yaml")
    universos = load_universes(caminho)

    if not args.check:
        print(f"{caminho} — {len(universos)} universo(s):")
        for nome, u in sorted(universos.items()):
            marca = " (default)" if nome == config.get("screener.default_universe") else ""
            print(f"  {nome:<16} {len(u.tickers):>3} papéis  {u.market} vs {u.benchmark}{marca}")
        return 0

    nomes = [args.name] if args.name else sorted(universos)
    faltando = [n for n in nomes if n not in universos]
    if faltando:
        print(f"universo(s) inexistente(s): {', '.join(faltando)}. "
              f"Disponíveis: {', '.join(sorted(universos))}", file=sys.stderr)
        return 2

    total_mortos = 0
    for nome in nomes:
        u = universos[nome]
        print(f"\nConferindo {nome} ({len(u.tickers)} papéis) contra a fonte...")
        vivos, mortos = check_universe(u, build_provider(config, market=u.market), pause=args.pause)
        total_mortos += len(mortos)
        print(f"  {len(vivos)} respondem, {len(mortos)} não:")
        for symbol, motivo in mortos:
            print(f"    ! {symbol}: {motivo}")
        if mortos:
            print(f"\n  Para limpar, tire estes de `universes.{nome}.tickers` em {caminho}:")
            print("    " + " ".join(symbol for symbol, _ in mortos))
    return 1 if total_mortos else 0


def _exportar_pdf(html_path, config) -> int:
    """Conversão compartilhada por `report --pdf` e `pdf` (P2)."""
    from .pdf import PdfError, to_pdf

    try:
        caminho, motor = to_pdf(html_path, config=config)
    except PdfError as exc:
        print(f"\nPDF não gerado — {exc}", file=sys.stderr)
        return 1
    tamanho = caminho.stat().st_size / 1024
    print(f"  PDF:      {caminho}  ({tamanho:,.0f} KB, via {motor})")
    return 0


def cmd_pdf(args) -> int:
    """P2: converte para PDF um relatório já gerado."""
    config, _ = _load(args)
    out_dir = Path(config.get("output.reports_dir", "reports"))
    if args.tag:
        html_path = out_dir / f"{args.tag}.html"
    else:
        existentes = sorted(out_dir.glob("[0-9]*.html"))
        if not existentes:
            print(f"nenhum relatório em {out_dir}/ — rode `wyckoff report` antes.", file=sys.stderr)
            return 1
        html_path = existentes[-1]
    print(f"Convertendo {html_path}...")
    return _exportar_pdf(html_path, config)


def cmd_dashboard(args) -> int:
    """P2: sobe o dashboard local (Streamlit) sobre o mesmo cache."""
    import shutil
    import subprocess

    config, _ = _load(args)          # falha cedo se config/watchlist estiverem quebrados
    script = Path(__file__).resolve().parent / "dashboard.py"
    executavel = shutil.which("streamlit")
    comando = [executavel] if executavel else [sys.executable, "-m", "streamlit"]
    try:
        import streamlit  # noqa: F401
    except ImportError:
        if executavel is None:
            print("\nStreamlit não está instalado — o dashboard é um extra opcional:\n"
                  "    pip install 'wyckoff-screener[dashboard]'\n"
                  "    (ou: pip install streamlit)\n"
                  "A rotina semanal não depende dele: `wyckoff report` continua funcionando.",
                  file=sys.stderr)
            return 1

    comando += [
        "run", str(script),
        "--server.port", str(args.port),
        # Só a própria máquina: o default do streamlit escuta em 0.0.0.0 e
        # anuncia a URL externa, publicando a watchlist na rede local.
        "--server.address", args.address,
        # Uso pessoal e local, como a spec pede — sem telemetria.
        "--browser.gatherUsageStats", "false",
    ]
    if args.headless:
        comando += ["--server.headless", "true"]
    comando += ["--", "--config", args.config, "--watchlist", args.watchlist]

    print(f"Subindo o dashboard em http://localhost:{args.port} (Ctrl+C encerra).")
    print("Ele lê o cache e não coleta: rode `wyckoff fetch` para dados novos.")
    try:
        return subprocess.call(comando)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"\nnão foi possível executar o streamlit — {exc}", file=sys.stderr)
        return 1


def cmd_sources(args) -> int:
    """P2: põe duas fontes lado a lado no mesmo papel e mostra onde discordam.

    É a régua que a §8 da spec pede para decidir o fallback da B3 — em vez de
    trocar de fonte por desconfiança, troca-se com a divergência medida.
    """
    from .sources import compare_sources

    config, watchlist = _load(args)
    semanas = args.weeks or int(config.require("data.history_weeks"))
    try:
        esquerda = (args.source, build_single(args.source, config))
        direita = (args.against, build_single(args.against, config))
    except ProviderError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    if args.source == args.against:
        print("as duas fontes são a mesma; informe --against diferente de --source", file=sys.stderr)
        return 2

    print(f"Baixando {args.ticker} ({semanas} semanas) em {args.source} e {args.against}...")
    resultado = compare_sources(
        args.ticker, semanas, esquerda, direita,
        tolerance_pct=float(config.get("data.compare.tolerance_pct", 0.005)),
        volume_tolerance_pct=float(config.get("data.compare.volume_tolerance_pct", 0.01)),
        with_actions=not args.no_actions,
    )
    limite = int(config.get("data.compare.max_rows", 12))

    print(f"\n{resultado.symbol}: {resultado.left_name} {resultado.left_bars} pregões, "
          f"{resultado.right_name} {resultado.right_bars} — {resultado.common} em comum")
    for erro in resultado.errors:
        print(f"  ! {erro}")

    for datas, dono in ((resultado.only_left, resultado.left_name),
                        (resultado.only_right, resultado.right_name)):
        if datas:
            print(f"\n  Pregões só em {dono} ({len(datas)}):")
            for data in datas[:limite]:
                print(f"    {data:%d/%m/%Y}")
            if len(datas) > limite:
                print(f"    ... e mais {len(datas) - limite}")

    for titulo, divergencias in (("Preços divergentes", resultado.price_divergences),
                                 ("Volumes divergentes", resultado.volume_divergences)):
        if divergencias:
            piores = sorted(divergencias, key=lambda d: abs(d.diff_pct), reverse=True)
            print(f"\n  {titulo} ({len(divergencias)}), maiores primeiro:")
            for divergencia in piores[:limite]:
                print("    " + divergencia.describe(resultado.left_name, resultado.right_name))
            if len(divergencias) > limite:
                print(f"    ... e mais {len(divergencias) - limite}")

    for proventos, dono in ((resultado.only_left_actions, resultado.left_name),
                            (resultado.only_right_actions, resultado.right_name)):
        if proventos:
            print(f"\n  Proventos/splits só em {dono} ({len(proventos)}):")
            for data, kind in proventos[:limite]:
                print(f"    {data:%d/%m/%Y}  {kind}")
            if len(proventos) > limite:
                print(f"    ... e mais {len(proventos) - limite}")

    print(f"\n  {resultado.verdict()}")
    return 0 if resultado.agree else 1


def cmd_backtest(args) -> int:
    """R11: mede o retorno à frente de cada regra, contra a linha de base."""
    from .backtest import DISCLAIMER, run

    config, watchlist = _load(args)
    horizontes = ([int(h) for h in args.horizons.split(",")] if args.horizons
                  else [int(h) for h in config.get("backtest.horizons", [4, 8, 13, 26])])
    causal = config.get("backtest.causal", True) and not args.fast
    tipos = tuple(k.strip() for k in args.kind.split(",")) if args.kind else None

    with Cache(config.require("data.cache_path")) as cache:
        metrics, _ = build_metrics(watchlist, config, cache)
        benchmarks = {i.symbol: cache.get_bars(i.benchmark) for i in watchlist
                      if i.symbol in metrics}
    if not metrics:
        print("Nenhum papel com dados. Rode `wyckoff fetch` primeiro.", file=sys.stderr)
        return 1

    modo = "causal (redetecta semana a semana)" if causal else "rápido (COM lookahead no range)"
    print(f"Backtest de {len(metrics)} papéis, horizontes {horizontes} semanas, modo {modo}...")
    tabela, observacoes = run(metrics, config, horizontes, benchmarks, causal=causal, kinds=tipos)
    if tabela.empty:
        print("Nenhuma observação. Histórico curto demais?", file=sys.stderr)
        return 1

    minimo = int(config.get("backtest.min_observations", 5))
    visivel = tabela[(tabela["n"] >= minimo) | (tabela["evento"] == "_qualquer_semana")]
    saida = visivel.copy()
    for col in ("mediana", "media", "excesso_mediana"):
        saida[col] = saida[col].map(lambda v: f"{v:+.1%}" if pd.notna(v) else "—")
    for col in ("acerto", "excesso_acerto"):
        saida[col] = saida[col].map(lambda v: f"{v:.0%}" if pd.notna(v) else "—")
    saida["atraso_medio_semanas"] = saida["atraso_medio_semanas"].map(lambda v: f"{v:.1f}")
    saida = saida.rename(columns={"horizonte_semanas": "sem.", "excesso_mediana": "exc.mediana",
                                  "excesso_acerto": "exc.acerto",
                                  "atraso_medio_semanas": "atraso"})
    print()
    with pd.option_context("display.width", 200, "display.max_rows", 200):
        print(saida.to_string(index=False))

    escondidos = tabela[(tabela["n"] < minimo) & (tabela["evento"] != "_qualquer_semana")]
    if not escondidos.empty:
        nomes = sorted(set(escondidos["evento"]))
        print(f"\nOmitidos por n < {minimo}: {', '.join(nomes)}")

    if args.csv:
        out_dir = Path(config.get("output.csv_dir", "exports"))
        out_dir.mkdir(parents=True, exist_ok=True)
        sufixo = "causal" if causal else "rapido"
        caminho = out_dir / f"backtest_{sufixo}.csv"
        tabela.to_csv(caminho, index=False)
        detalhe = out_dir / f"backtest_{sufixo}_observacoes.csv"
        pd.DataFrame([{
            "symbol": o.symbol, "evento": o.kind, "vies": o.bias, "data": o.date,
            "confirmado": o.confirmed, "atraso_semanas": o.lag,
            **{f"ret_{h}s": v for h, v in o.forward.items()},
            **{f"exc_{h}s": v for h, v in o.excess.items()},
        } for o in observacoes]).to_csv(detalhe, index=False)
        print(f"\nCSV: {caminho}\n     {detalhe}")

    print(f"\n{DISCLAIMER}")
    if not causal:
        print("ATENÇÃO: modo --fast tem lookahead na definição dos ranges. "
              "Use o causal para concluir qualquer coisa.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wyckoff", description="Screener Wyckoff semanal (B3 + US)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--watchlist", default="watchlist.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="valida config.yaml e watchlist.yaml").set_defaults(func=cmd_validate)

    p_fetch = sub.add_parser("fetch", help="baixa dados, atualiza o cache e calcula as métricas")
    p_fetch.add_argument("--force", action="store_true", help="rebaixa mesmo se já coletado hoje")
    p_fetch.add_argument("--no-metrics", action="store_true", help="só coleta, não calcula")
    p_fetch.set_defaults(func=cmd_fetch)

    sub.add_parser("metrics", help="recalcula as métricas do cache (offline)").set_defaults(func=cmd_metrics)

    p_report = sub.add_parser("report", help="gera o relatório semanal (.md + .html) — comando único da rotina")
    p_report.add_argument("--offline", action="store_true", help="não coleta; usa só o que está no cache")
    p_report.add_argument("--force", action="store_true", help="rebaixa mesmo se já coletado hoje")
    p_report.add_argument("--pdf", action="store_true",
                          help="exporta também em PDF (P2)")
    p_report.add_argument("--notify", action="store_true",
                          help="manda o resumo pelo canal de notify.backend depois de gerar")
    p_report.set_defaults(func=cmd_report)

    p_notify = sub.add_parser("notify", help="manda o resumo da semana pelo canal configurado")
    p_notify.add_argument("--dry-run", action="store_true",
                          help="imprime a mensagem no terminal em vez de enviar")
    p_notify.add_argument("--force", action="store_true",
                          help="envia mesmo com notify.enabled = false")
    p_notify.set_defaults(func=cmd_notify)

    p_analyze = sub.add_parser("analyze", help="fase, range e eventos de um papel (ou de todos)")
    p_analyze.add_argument("ticker", nargs="?")
    p_analyze.add_argument("-n", "--limit", type=int, default=8, help="quantos eventos mostrar por papel")
    p_analyze.set_defaults(func=cmd_analyze)

    p_screen = sub.add_parser("screen", help="varre um universo amplo e ranqueia candidatos (R12)")
    p_screen.add_argument("universe", nargs="?", help="nome do universo em universe.yaml")
    p_screen.add_argument("--universe-file", help="outro arquivo de universos")
    p_screen.add_argument("--phases", help="fases que interessam, ex.: C,D (default: config)")
    p_screen.add_argument("--top", type=int, help="quantos candidatos listar")
    p_screen.add_argument("--offline", action="store_true", help="não coleta; usa só o cache")
    p_screen.add_argument("--force", action="store_true", help="rebaixa mesmo se já coletado hoje")
    p_screen.set_defaults(func=cmd_screen)

    p_universe = sub.add_parser("universe", help="lista os universos ou confere se os tickers vivem")
    p_universe.add_argument("name", nargs="?", help="universo a conferir (default: todos)")
    p_universe.add_argument("--check", action="store_true",
                            help="bate cada ticker na fonte e aponta os que morreram")
    p_universe.add_argument("--universe-file", help="outro arquivo de universos")
    p_universe.add_argument("--pause", type=float, default=0.3,
                            help="segundos entre chamadas, para não estrangular a fonte")
    p_universe.set_defaults(func=cmd_universe)

    p_bt = sub.add_parser("backtest", help="mede o que vem depois de cada regra (R11 — calibragem)")
    p_bt.add_argument("--horizons", help="semanas à frente, ex.: 4,13,26")
    p_bt.add_argument("--kind", help="só estes eventos, ex.: spring,sos")
    p_bt.add_argument("--fast", action="store_true",
                      help="detecção única (rápido, mas com lookahead no range)")
    p_bt.add_argument("--csv", action="store_true", help="exporta tabela e observações")
    p_bt.set_defaults(func=cmd_backtest)

    p_dash = sub.add_parser("dashboard", help="abre o dashboard local no navegador (P2)")
    p_dash.add_argument("--port", type=int, default=8501)
    p_dash.add_argument("--address", default="localhost",
                        help="endereço de escuta (default: só esta máquina)")
    p_dash.add_argument("--headless", action="store_true",
                        help="não abre o navegador sozinho")
    p_dash.set_defaults(func=cmd_dashboard)

    p_pdf = sub.add_parser("pdf", help="converte um relatório já gerado para PDF (P2)")
    p_pdf.add_argument("tag", nargs="?", help="semana, ex.: 2026-36 (default: o mais recente)")
    p_pdf.set_defaults(func=cmd_pdf)

    p_src = sub.add_parser("sources", help="compara duas fontes no mesmo papel (P2)")
    p_src.add_argument("ticker")
    p_src.add_argument("--source", default="yfinance", help="fonte de referência (default: yfinance)")
    p_src.add_argument("--against", default="brapi", help="fonte comparada (default: brapi)")
    p_src.add_argument("--weeks", type=int, help="semanas comparadas (default: data.history_weeks)")
    p_src.add_argument("--no-actions", action="store_true", help="não comparar proventos/splits")
    p_src.set_defaults(func=cmd_sources)

    p_show = sub.add_parser("show", help="mostra as últimas semanas de um papel com todos os números")
    p_show.add_argument("ticker")
    p_show.add_argument("-n", "--weeks", type=int, default=12)
    p_show.set_defaults(func=cmd_show)

    p_verify = sub.add_parser("verify", help="abre a conta de cada métrica da última semana fechada")
    p_verify.add_argument("ticker")
    p_verify.set_defaults(func=cmd_verify)

    p_add = sub.add_parser("add", help="adiciona um papel à watchlist")
    p_add.add_argument("ticker")
    p_add.add_argument("--market", choices=["b3", "us"])
    p_add.add_argument("--benchmark")
    p_add.add_argument("--invalidation", type=float)
    p_add.add_argument("--direction", choices=["below", "above"], default="below")
    p_add.add_argument("--notes")
    p_add.set_defaults(func=cmd_add)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, WatchlistError, UniverseError, ProviderError) as exc:
        print(f"\nErro de configuração:\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
