"""Dashboard local em Streamlit (P2 da spec).

Lê o MESMO SQLite que a CLI e reusa o MESMO `build_model` do relatório — se a
tela e o relatório de sexta discordassem, um dos dois estaria mentindo. A
diferença é só o meio: aqui dá para filtrar, ordenar e abrir um papel de cada
vez sem regerar arquivo nenhum.

**Não coleta.** O dashboard nunca vai à rede: mostra o que está no cache e diz
de quando é o dado, com o comando a rodar para atualizá-lo. Uma página aberta
há três dias parece atual; o carimbo no topo impede essa confusão.

Rode por `wyckoff dashboard` — que resolve os caminhos e chama o streamlit —
ou direto:

    streamlit run src/dashboard.py -- --config config.yaml --watchlist watchlist.yaml
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import tempfile
from pathlib import Path

# Executado pelo streamlit como script solto: o pacote `src` precisa estar no
# caminho antes de qualquer import do projeto.
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import pandas as pd
import streamlit as st

from src import charts
from src.config import load_config
from src.data.cache import Cache
from src.pipeline import build_metrics
from src.report import DISCLAIMER, build_model
from src.watchlist import load_watchlist


def _conferencias(evento) -> pd.DataFrame:
    """Os números que dispararam a regra — a auditabilidade que a spec exige."""
    return pd.DataFrame([{"conferência": c.describe(), "passou": "sim" if c.passed() else "não"}
                         for c in evento.checks])


def argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--watchlist", default="watchlist.yaml")
    conhecidos, _ = parser.parse_known_args(sys.argv[1:])
    return conhecidos


def _carimbo(caminho: str) -> float:
    """mtime do arquivo, para a cache do streamlit invalidar sozinha quando o
    cache do SQLite ou a watchlist mudarem."""
    try:
        return Path(caminho).stat().st_mtime
    except OSError:
        return 0.0


@st.cache_resource(show_spinner="Lendo o cache e analisando...")
def carregar(config_path: str, watchlist_path: str, _selos: tuple[float, ...]):
    """Modelo completo do relatório, sem desenhar gráfico (isso é sob demanda).

    `_selos` não é usado no corpo: entra na assinatura só para a cache do
    streamlit expirar quando os arquivos mudam.
    """
    config = load_config(config_path)
    watchlist = load_watchlist(watchlist_path)
    with Cache(config.require("data.cache_path")) as cache:
        metrics, problemas = build_metrics(watchlist, config, cache)
        diario = {s: cache.get_daily_bars(s) for s in metrics}
        ultima_coleta = cache.last_fetch()
    modelo = build_model(watchlist, metrics, config, dt.datetime.now(),
                         None, problemas, None, diario)
    return config, watchlist, modelo, ultima_coleta


@st.cache_data(show_spinner=False)
def desenhar(symbol: str, tag: str, _analise, _config) -> bytes | None:
    """PNG do gráfico anotado, gerado sob demanda e mantido em memória."""
    with tempfile.TemporaryDirectory(prefix="wyckoff-chart-") as tmp:
        caminho = charts.render(_analise, _config, Path(tmp))
        return caminho.read_bytes() if caminho else None


def cabecalho(modelo: dict, ultima_coleta) -> None:
    st.title(f"Wyckoff Screener — semana {modelo['tag']}")
    quando = ultima_coleta.strftime("%d/%m/%Y %H:%M") if ultima_coleta else "nunca"
    idade = (dt.datetime.now() - ultima_coleta).days if ultima_coleta else None
    linha = (f"Semana analisada **{modelo['week_start']:%d/%m/%Y}** (candle fechado) · "
             f"{len(modelo['rows'])} papéis · dados coletados em **{quando}**")
    st.caption(linha)
    if idade is not None and idade >= 7:
        st.warning(f"O cache tem {idade} dias. Rode `wyckoff fetch` para atualizar — "
                   f"o dashboard não coleta sozinho.")
    elif ultima_coleta is None:
        st.error("Cache vazio. Rode `wyckoff fetch` antes.")


def painel_alertas(modelo: dict) -> None:
    invalidacoes = modelo["invalidations"]
    if invalidacoes:
        st.subheader(f"1. Invalidações violadas ({len(invalidacoes)})")
        for alerta in invalidacoes:
            marca = "🆕 " if alerta.is_new else ""
            st.error(f"{marca}**{alerta.symbol}** — {alerta.summary}")
    else:
        with_level, total = modelo["levels_configured"], modelo["levels_total"]
        st.subheader("1. Invalidações violadas")
        if with_level == 0:
            st.info(f"Nenhum dos {total} papéis tem nível de invalidação definido — "
                    f"preencha `invalidation` na watchlist para este painel funcionar.")
        else:
            st.success(f"Nenhuma violação. {with_level} de {total} papéis com nível definido.")


def painel_eventos(modelo: dict) -> None:
    eventos = modelo["week_events"]
    st.subheader(f"2. Eventos da semana ({len(eventos)})")
    if not eventos:
        st.caption("Nenhum evento Wyckoff na última semana fechada.")
        return
    for item in eventos:
        evento = item["event"]
        with st.expander(f"**{item['symbol']}** — {evento.label} · {evento.summary}"):
            st.caption(f"{evento.date:%d/%m/%Y} · viés {evento.bias}")
            st.table(_conferencias(evento))


def painel_tabela(modelo: dict) -> None:
    st.subheader("3. Watchlist")
    linhas = []
    for linha in modelo["rows"]:
        registro = {
            "papel": linha["symbol"], "fase": linha["phase"], "viés": linha["bias"],
            "semanas": linha["weeks_in_phase"], "fech.": linha["close"],
            "vol/méd": linha["volume_ratio"], "spread/ATR": linha["spread_ratio"],
            "fech. no candle": linha["close_position"], "range": linha["range"],
            "invalidação": linha["invalidation"], "folga": linha["invalidation_gap"],
            "próximo esperado": linha["pending"],
        }
        for rotulo, valor in zip(modelo["rs_labels"], linha["rs"]):
            registro[rotulo] = valor
        linhas.append(registro)
    st.dataframe(pd.DataFrame(linhas), use_container_width=True, hide_index=True)


def painel_papel(modelo: dict, config, symbol: str) -> None:
    secao = next((s for s in modelo["sections"] if s["symbol"] == symbol), None)
    if secao is None:
        st.warning(f"{symbol} não está no relatório desta semana.")
        return
    analise = secao["analysis"]
    fase = analise.phase

    # Gráfico na largura inteira: dividir a tela em duas colunas encolhia o
    # candle e ainda quebrava o texto da ficha em uma palavra por linha.
    png = desenhar(symbol, modelo["tag"], analise, config)
    if png:
        st.image(png, use_container_width=True)
    else:
        st.caption("Sem gráfico: histórico insuficiente.")

    esquerda, direita = st.columns(2)
    with esquerda:
        # Rótulo em negrito, não em `###`: numa coluna de meia tela o título
        # grande quebrava em quatro linhas.
        st.markdown(f"**{fase.label}**")
        st.caption(f"há {fase.weeks_in_phase(len(analise.closed))} semana(s), "
                   f"desde {fase.since_date:%d/%m/%Y}")
        st.markdown(f"**Gatilho:** {fase.reason}")
        st.markdown(f"**Próximo evento esperado:** {fase.pending}")
        if analise.governing_range:
            ativo = "ativo" if analise.active_range else "encerrado"
            st.markdown(f"**Range ({ativo}):** {analise.governing_range.describe()}")
        else:
            st.markdown("**Range:** nenhum em vigor.")
        if secao["cause"]:
            st.markdown(f"**Contagem de causa (P&F):** {secao['cause'].describe()}")
            with st.expander("como a contagem foi feita"):
                for linha in secao["cause"].audit_lines():
                    st.caption(f"· {linha}")
    with direita:
        if analise.alert:
            st.error(analise.alert.summary)
        for evento, dias in secao["calendar"]:
            st.info(f"📅 {evento.label} em {dias} dia(s) ({evento.date:%d/%m/%Y})")
        if analise.latest is not None:
            st.markdown(f"**Última semana fechada ({analise.latest.name:%d/%m/%Y})**")
            st.caption(" · ".join(
                f"{rotulo} {analise.latest[coluna]:{formato}}"
                for coluna, rotulo, formato in (
                    ("close", "fech.", ".2f"), ("volume_ratio", "vol/méd", ".2f"),
                    ("spread_ratio", "spread/ATR", ".2f"), ("close_position", "pos. fech.", ".0%"),
                ) if coluna in analise.latest.index and pd.notna(analise.latest[coluna])
            ))

    st.markdown("#### Eventos detectados")
    if not secao["detailed_events"]:
        st.caption("Nenhum evento no período do gráfico.")
    for evento in secao["detailed_events"]:
        with st.expander(f"{evento.date:%d/%m/%Y} · **{evento.label}** — {evento.summary}"):
            st.table(_conferencias(evento))
    if secao["hidden_events"]:
        st.caption(f"{secao['hidden_events']} evento(s) mais antigos que o gráfico, omitidos.")

    st.markdown("#### Últimas semanas")
    colunas = [c for c in ("open", "high", "low", "close", "volume", "volume_ratio",
                           "spread_ratio", "close_position") if c in analise.closed.columns]
    st.dataframe(analise.closed[colunas].tail(12).iloc[::-1], use_container_width=True)


def painel_problemas(modelo: dict) -> None:
    st.subheader("5. Coleta")
    if modelo["problems"]:
        st.dataframe(pd.DataFrame(modelo["problems"]), use_container_width=True, hide_index=True)
    else:
        st.success("Nenhum problema de coleta.")
    if modelo["partials"]:
        st.caption("Semana em formação (não entra na análise): " +
                   ", ".join(f"{p['symbol']} ({p['week']:%d/%m})" for p in modelo["partials"]))
    with st.expander("Parâmetros em vigor"):
        st.json(modelo["params"])


def main() -> None:
    st.set_page_config(page_title="Wyckoff Screener", page_icon="📈", layout="wide")
    args = argumentos()
    try:
        config, _, modelo, ultima_coleta = carregar(
            args.config, args.watchlist,
            (_carimbo(args.config), _carimbo(args.watchlist),
             _carimbo(str(load_config(args.config).require("data.cache_path")))),
        )
    except Exception as exc:  # noqa: BLE001 — qualquer falha vira mensagem na tela, não traceback
        st.error(f"Não foi possível carregar: {exc}")
        st.caption("Confira os caminhos em `wyckoff dashboard --config ... --watchlist ...` "
                   "e se `wyckoff fetch` já rodou.")
        return

    cabecalho(modelo, ultima_coleta)
    with st.sidebar:
        st.header("Wyckoff")
        if st.button("Recarregar do cache", use_container_width=True):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.rerun()
        st.caption("O dashboard não coleta. Para dados novos:\n\n`wyckoff fetch`")
        st.divider()
        simbolos = [linha["symbol"] for linha in modelo["rows"]]
        escolhido = st.selectbox("Papel", simbolos) if simbolos else None
        st.divider()
        st.caption(DISCLAIMER)

    panorama, papel, coleta = st.tabs(["Panorama", "Papel a papel", "Coleta"])
    with panorama:
        painel_alertas(modelo)
        painel_eventos(modelo)
        painel_tabela(modelo)
    with papel:
        if escolhido:
            painel_papel(modelo, config, escolhido)
    with coleta:
        painel_problemas(modelo)


if __name__ == "__main__":
    main()
