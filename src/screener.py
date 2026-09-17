"""Screener de novos candidatos (R12).

Varre um universo amplo — os líquidos da B3, por exemplo — e ranqueia os papéis
que estão nas fases que interessam. A leitura é a mesma da watchlist: os mesmos
ranges de R4, os mesmos eventos de R5, a mesma máquina de R6. O screener não
tem heurística própria; ele só aplica a de sempre em mais gente.

**Ordenação léxica, não nota ponderada.** Um score com pesos arbitrários dá uma
falsa precisão — "8,3" não quer dizer nada e ninguém consegue auditar de onde
veio. A ordem aqui é explícita: primeiro a fase (D antes de C), depois quem tem a
leitura mais recente — a idade do evento que INSTALOU a fase, não a de um evento
qualquer que passou por perto —, depois a força relativa. Cada critério aparece
como coluna, então a posição de qualquer papel na lista é conferível a olho.

**O universo é seu.** `universe.yaml` nasce com uma lista de partida, não com a
carteira oficial do IBOV — ela muda a cada quadrimestre e sai na B3. Papel que
não existe mais vira erro de coleta e não derruba a varredura.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from .analysis import TickerAnalysis, analyze
from .config import Config
from .data.cache import Cache
from .data.provider import DataProvider
from .metrics import compute_metrics
from .pipeline import SymbolStatus, fetch_all, flag_ex_dates
from .watchlist import MARKETS, WatchItem, Watchlist

# Ordem de interesse das fases. D (oferta/demanda no controle) vem antes de C
# (teste) porque está mais perto da resolução; o resto não é candidato.
PHASE_RANK = {"D": 3, "C": 2, "B": 1}


class UniverseError(Exception):
    """Universo inválido. A mensagem lista todos os problemas encontrados."""


@dataclass(frozen=True)
class Universe:
    name: str
    market: str
    benchmark: str
    tickers: tuple[str, ...]

    def as_watchlist(self) -> Watchlist:
        """Universo no formato que o resto do sistema já sabe consumir."""
        return Watchlist(items=[
            WatchItem(symbol=t, market=self.market, benchmark=self.benchmark)
            for t in self.tickers
        ])


def parse_universes(raw, source: str = "universe.yaml") -> dict[str, Universe]:
    """Valida o arquivo inteiro numa passada, como a watchlist de R1."""
    erros: list[str] = []
    if not isinstance(raw, dict):
        raise UniverseError(f"{source}: raiz do arquivo deve ser um mapeamento com `universes`.")
    blocos = raw.get("universes")
    if blocos is None:
        raise UniverseError(f"{source}: chave `universes` ausente.")
    if not isinstance(blocos, dict) or not blocos:
        raise UniverseError(f"{source}: `universes` deve ser um mapeamento não vazio.")

    out: dict[str, Universe] = {}
    for nome, bloco in blocos.items():
        onde = f"universes.{nome}"
        if not isinstance(bloco, dict):
            erros.append(f"{onde}: esperado mapeamento com `market`, `benchmark` e `tickers`")
            continue
        market = bloco.get("market")
        if market not in MARKETS:
            erros.append(f"{onde}.market: esperado um de {list(MARKETS)}, recebido {market!r}")
            continue
        benchmark = bloco.get("benchmark")
        if not isinstance(benchmark, str) or not benchmark.strip():
            erros.append(f"{onde}.benchmark: esperado texto não vazio (ex.: ^BVSP)")
            continue
        tickers = bloco.get("tickers")
        if not isinstance(tickers, list) or not tickers:
            erros.append(f"{onde}.tickers: esperado uma lista não vazia")
            continue

        limpos: list[str] = []
        vistos: set[str] = set()
        for i, t in enumerate(tickers):
            if not isinstance(t, str) or not t.strip():
                erros.append(f"{onde}.tickers[{i}]: esperado texto não vazio")
                continue
            simbolo = t.strip().upper()
            if market == "b3" and not simbolo.endswith(".SA"):
                erros.append(f"{onde}.tickers[{i}] ({simbolo}): mercado `b3` exige sufixo `.SA`")
                continue
            if market == "us" and simbolo.endswith(".SA"):
                erros.append(f"{onde}.tickers[{i}] ({simbolo}): sufixo `.SA` não vale para `us`")
                continue
            if simbolo in vistos:
                erros.append(f"{onde}.tickers[{i}] ({simbolo}): símbolo duplicado")
                continue
            vistos.add(simbolo)
            limpos.append(simbolo)
        if limpos:
            out[str(nome)] = Universe(str(nome), market, benchmark.strip(), tuple(limpos))

    if erros:
        joined = "\n  - ".join(erros)
        raise UniverseError(f"{source}: {len(erros)} erro(s) de validação:\n  - {joined}")
    return out


def load_universes(path: str | Path = "universe.yaml") -> dict[str, Universe]:
    path = Path(path)
    if not path.exists():
        raise UniverseError(f"{path}: arquivo não encontrado. Crie o universo antes de varrer.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise UniverseError(f"{path}: YAML inválido — {exc}") from exc
    return parse_universes(raw, source=str(path))


@dataclass
class Candidate:
    analysis: TickerAnalysis
    #: Idade e identidade do evento que instalou a fase — o que filtra e ordena.
    weeks_since_event: int | None
    last_event: object | None
    liquidity: float = 0.0       # volume financeiro semanal típico
    #: Último evento do mesmo viés, que pode ser bem mais novo que a fase.
    #: Só informa; vai para o CSV para explicar divergências entre os dois.
    weeks_since_aligned: int | None = None
    last_aligned: object | None = None

    @property
    def symbol(self) -> str:
        return self.analysis.symbol

    @property
    def phase_rank(self) -> int:
        return PHASE_RANK.get(self.analysis.phase.letter or "", 0)

    def rs(self, weeks: int) -> float | None:
        value = self.analysis.value(f"rs_{weeks}w")
        return None if value is None else float(value)

    def sort_key(self, rs_weeks: int) -> tuple:
        """Fase, depois recência da LEITURA, depois força relativa. Nessa ordem."""
        rs = self.rs(rs_weeks)
        return (
            -self.phase_rank,
            self.weeks_since_event if self.weeks_since_event is not None else 10**6,
            -(rs if rs is not None else -10.0),
            self.symbol,
        )


@dataclass
class ScreenResult:
    universe: Universe
    candidates: list[Candidate] = field(default_factory=list)
    scanned: int = 0
    problems: list[SymbolStatus] = field(default_factory=list)
    phases: tuple[str, ...] = ()
    illiquid: int = 0            # descartados pelo piso de liquidez
    min_liquidity: float = 0.0
    stale: int = 0               # descartados por fase instalada há tempo demais
    max_weeks_since_event: int = 0
    #: Quantos papéis passaram no filtro de fase E no piso de liquidez, ANTES
    #: do corte do `top`. Sem ele o relatório dizia "25 em Fase C/D" numa semana
    #: com 142: a frase lia como censo e era teto, e some justamente o que diria
    #: se 25 está apertado ou folgado.
    matched: int = 0

    @property
    def truncated(self) -> bool:
        return self.matched > len(self.candidates)


def weekly_liquidity(metrics: pd.DataFrame, weeks: int = 12) -> float:
    """Mediana do volume financeiro semanal (preço × volume) das últimas semanas.

    Mediana, e não média: uma única semana de leilão ou de notícia levantaria a
    média de um papel que não negocia no resto do tempo.
    """
    if metrics is None or metrics.empty:
        return 0.0
    janela = metrics.tail(weeks)
    financeiro = (janela["close"] * janela["volume"]).dropna()
    return float(financeiro.median()) if not financeiro.empty else 0.0


def _phase_driver_age(analysis: TickerAnalysis) -> tuple[int | None, object | None]:
    """Semanas desde o evento que INSTALOU (ou confirmou por último) a fase atual.

    É esta a idade que data a leitura, e não a do último evento qualquer do mesmo
    viés — a distinção decide quem entra na triagem. Em 16/09/2026 o NSC abria a
    lista americana com Fase D instalada por um SOS de **60 semanas** atrás, e
    passava porque um spring imprimira na semana anterior. Só que a máquina de
    estados ignora esse spring de propósito (dentro do mesmo viés a fase não
    retrocede de D para C), então a leitura que o relatório mostra continuava
    sendo a de 60 semanas atrás. Datar pelo spring dizia "mudou agora" sobre um
    estado que não mudava havia mais de um ano — o inventário que
    `screener.max_weeks_since_event` existe para não deixar entrar.

    Fase sem evento que a date — a B, que é a causa sendo construída — devolve
    `None`, e quem chama deixa passar.
    """
    driver = analysis.phase.driver
    if driver is None:
        return None, None
    return len(analysis.closed) - 1 - driver.index, driver


def _last_aligned_event(analysis: TickerAnalysis) -> tuple[int | None, object | None]:
    """Último evento do mesmo viés da fase — informativo, não filtra nem ordena.

    Alinhado ao viés porque um upthrust não diz nada sobre um candidato a
    acumulação. Vai para o CSV ao lado da idade da fase: quando os dois números
    divergem, é ali que se vê por quê.
    """
    bias = analysis.phase.bias
    alinhados = [e for e in analysis.events if e.bias == bias]
    if not alinhados:
        return None, None
    ultimo = alinhados[-1]
    return len(analysis.closed) - 1 - ultimo.index, ultimo


def screen(
    universe: Universe,
    config: Config,
    cache: Cache,
    phases: tuple[str, ...] = ("C", "D"),
    limit: int | None = None,
    exclude: frozenset[str] | set[str] | None = None,
) -> ScreenResult:
    """Ranqueia os papéis do universo que estão nas fases pedidas.

    Lê só do cache — quem coleta é `refresh`. Assim a varredura pode ser
    repetida à vontade (mexer em `--phases`, em thresholds do config) sem
    bater na fonte de novo.

    `exclude` tira da lista o que você já acompanha: uma seção chamada
    "candidatos fora da watchlist" que devolve papel da watchlist gasta as
    primeiras linhas — as que você lê — repetindo o que já está no relatório.
    """
    exclude = frozenset(exclude or ())
    min_weeks = int(config.get("data.min_weeks_for_metrics", 21))
    rs_windows = [int(w) for w in config.require("metrics.relative_strength_weeks")]
    rs_weeks = rs_windows[-1] if rs_windows else 12
    piso = float(config.get("screener.min_weekly_volume", 0) or 0)
    teto_idade = int(config.get("screener.max_weeks_since_event", 0) or 0)

    bench_bars = cache.get_bars(universe.benchmark)
    resultado = ScreenResult(universe=universe, phases=tuple(phases), min_liquidity=piso,
                             max_weeks_since_event=teto_idade)

    for item in universe.as_watchlist():
        if item.symbol in exclude:
            continue
        bars = cache.get_bars(item.symbol)
        if bars.empty:
            resultado.problems.append(
                SymbolStatus(item.symbol, "error", 0, "sem candles no cache")
            )
            continue
        if len(bars) < min_weeks:
            resultado.problems.append(
                SymbolStatus(item.symbol, "error", len(bars),
                             f"histórico curto: {len(bars)} semanas (< {min_weeks})")
            )
            continue
        metrics = compute_metrics(bars, config, bench_bars if not bench_bars.empty else None)
        metrics = flag_ex_dates(metrics, cache.get_actions(item.symbol, since=bars.index[0].date()))
        analysis = analyze(item, metrics, config)
        resultado.scanned += 1
        if (analysis.phase.letter or "") not in phases:
            continue
        # Num universo amplo, o piso de liquidez é o que impede a lista de
        # encher de papel que não dá para comprar: a leitura Wyckoff de um
        # candle semanal formado por três negócios é ruído com nome de sinal.
        liquidez = weekly_liquidity(metrics)
        if piso and liquidez < piso:
            resultado.illiquid += 1
            continue
        idade, driver = _phase_driver_age(analysis)
        idade_alinhado, alinhado = _last_aligned_event(analysis)
        # Fase é estado e não tem prazo: um SOS de 82 semanas atrás mantém o
        # papel em Fase D para sempre. Para LER o gráfico isso é correto; para
        # TRIAR a semana, não — o que se procura é o que mudou há pouco. O que
        # envelhece é a LEITURA, então a idade medida é a do evento que a
        # instalou (ver `_phase_driver_age`); um evento novo que a máquina de
        # estados ignorou de propósito não rejuvenesce nada.
        #
        # Papel sem evento que date a fase passa direto, em vez de ser
        # descartado: é o caso da Fase B, que é a causa sendo construída e não
        # tem evento para datar. Descartá-la faria este filtro esvaziar em
        # silêncio uma fase que só entra na lista quando alguém a pede em
        # `screener.phases` — filtro que faz o que não foi pedido é defeito.
        if teto_idade and idade is not None and idade > teto_idade:
            resultado.stale += 1
            continue
        resultado.candidates.append(
            Candidate(analysis, idade, driver, liquidez, idade_alinhado, alinhado))

    resultado.candidates.sort(key=lambda c: c.sort_key(rs_weeks))
    resultado.matched = len(resultado.candidates)
    if limit:
        resultado.candidates = resultado.candidates[:limit]
    return resultado


def refresh(
    universe: Universe,
    config: Config,
    provider: DataProvider,
    cache: Cache,
    force: bool = False,
    now: dt.datetime | None = None,
):
    """Coleta os candles do universo. Falha de um papel não aborta os outros (R2).

    Proventos ficam de fora por default (`screener.fetch_actions`): é a
    requisição mais cara da coleta e o universo é triagem, não leitura. Quem
    passa da triagem entra na watchlist, onde tudo é coletado.
    """
    return fetch_all(universe.as_watchlist(), config, provider, cache, force=force, now=now,
                     fetch_actions=bool(config.get("screener.fetch_actions", False)))


def to_frame(result: ScreenResult, config: Config) -> pd.DataFrame:
    """Ranking em tabela, com as colunas que justificam a ordem."""
    rs_windows = [int(w) for w in config.require("metrics.relative_strength_weeks")]
    linhas = []
    for posicao, c in enumerate(result.candidates, start=1):
        a = c.analysis
        tr = a.governing_range
        linha = {
            "posicao": posicao,
            "symbol": c.symbol,
            "fase": a.phase.code,
            "semanas_na_fase": a.phase.weeks_in_phase(len(a.closed)),
            "evento_da_fase": c.last_event.label if c.last_event else "",
            "semanas_desde_evento": c.weeks_since_event,
            "ultimo_evento_alinhado": c.last_aligned.label if c.last_aligned else "",
            "semanas_desde_ultimo": c.weeks_since_aligned,
            "proximo_esperado": a.phase.pending,
            "close": a.value("close"),
            "volume_ratio": a.value("volume_ratio"),
            "range_suporte": tr.support if tr else None,
            "range_resistencia": tr.resistance if tr else None,
            "range_ativo": bool(a.active_range),
            "liquidez_semanal": round(c.liquidity),
        }
        for w in rs_windows:
            linha[f"rs_{w}w"] = a.value(f"rs_{w}w")
        linhas.append(linha)
    return pd.DataFrame(linhas)


def export(result: ScreenResult, config: Config, tag: str) -> Path:
    """Escreve o ranking em CSV, ao lado dos outros exports."""
    out_dir = Path(config.get("output.csv_dir", "exports"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"screen_{result.universe.name}_{tag}.csv"
    to_frame(result, config).to_csv(path, index=False)
    return path

def check_universe(
    universe: Universe, provider: DataProvider, weeks: int = 4, pause: float = 0.0
) -> tuple[list[str], list[tuple[str, str]]]:
    """Confere quais tickers do universo ainda respondem na fonte.

    Existe porque ticker de bolsa morre e uma lista estática apodrece calada: o
    screener continua rodando, só que varrendo menos papéis do que você pensa.
    Devolve (vivos, [(morto, motivo)]) — quem chama decide o que fazer.
    """
    import time

    vivos: list[str] = []
    mortos: list[tuple[str, str]] = []
    for symbol in universe.tickers:
        if pause:
            time.sleep(pause)
        try:
            bars = provider.weekly_bars(symbol, weeks)
        except Exception as exc:
            mortos.append((symbol, str(exc)))
            continue
        if bars is None or bars.empty:
            mortos.append((symbol, "fonte não retornou candles"))
        else:
            vivos.append(symbol)
    return vivos, mortos
