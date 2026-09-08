"""Classificação de eventos Wyckoff (R5).

Funções puras: DataFrame de métricas entra, lista de `Event` sai. Nenhum I/O,
nenhum threshold no código — tudo vem de `config.events.*`.

Cada evento carrega a lista de `Check` que o disparou: valor medido, operador e
limite exigido. O relatório não escreve "spring detectado", escreve "spring:
mínima 18,40 perfurou o suporte 18,90; volume 0,84× (< 1,20× exigido)". É o
princípio 3 da spec — auditabilidade acima de concisão — virando estrutura de
dados em vez de string formatada na hora.

Comparação com NaN é sempre falsa em Python, e isso é intencional aqui: nas
primeiras 20 semanas, enquanto `volume_ratio` e `spread_ratio` ainda não têm
janela cheia, nenhuma regra dispara. Sem métrica não há sinal.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import pandas as pd

from .config import Config
from .ranges import TradingRange, governing_range, levels_before, range_at

ACUMULACAO = "acumulacao"
DISTRIBUICAO = "distribuicao"
NEUTRO = "neutro"

# Rótulo humano de cada tipo de evento.
LABELS = {
    "selling_climax": "Selling Climax",
    "buying_climax": "Buying Climax",
    "spring": "Spring",
    "test": "Teste do spring",
    "sos": "SOS (Sign of Strength)",
    "sow": "SOW (Sign of Weakness)",
    "lps": "LPS (Last Point of Support)",
    "lpsy": "LPSY (Last Point of Supply)",
    "upthrust": "Upthrust",
    "effort_vs_result": "Esforço × resultado",
}

_OPS = {
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
}


@dataclass(frozen=True)
class Check:
    """Uma comparação numérica que a regra exigiu. `threshold=None` = só informa."""

    label: str
    value: float
    op: str | None = None
    threshold: float | None = None
    fmt: str = "{:.2f}"

    def passed(self) -> bool:
        if self.op is None or self.threshold is None:
            return True
        if value_is_nan(self.value) or value_is_nan(self.threshold):
            return False
        return _OPS[self.op](float(self.value), float(self.threshold))

    def describe(self) -> str:
        got = "—" if value_is_nan(self.value) else self.fmt.format(float(self.value))
        if self.op is None or self.threshold is None:
            return f"{self.label} {got}"
        want = self.fmt.format(float(self.threshold))
        return f"{self.label} {got} ({self.op} {want} exigido)"


def value_is_nan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


@dataclass(frozen=True)
class Event:
    kind: str
    index: int
    date: dt.date
    bias: str
    summary: str
    checks: tuple[Check, ...] = ()
    confirmed: bool = True
    refs: dict = field(default_factory=dict)  # elos: spring do teste, SOS do LPS...

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, self.kind)

    @property
    def numbers(self) -> dict[str, float]:
        return {c.label: c.value for c in self.checks}

    def audit_lines(self) -> list[str]:
        return [c.describe() for c in self.checks]


# --------------------------------------------------------------------------
# utilidades internas
# --------------------------------------------------------------------------

# Colunas que as regras leem barra a barra. Extraí-las para numpy uma vez por
# detecção é o que torna o backtest causal de R11 viável: percorrendo `bars[c].
# iloc[i]` a cada acesso, 156 mil chamadas de `_val` levavam 4,3 s por papel só
# em maquinaria do pandas — a leitura em array custa uma fração disso e o
# resultado é bit a bit o mesmo.
_NUMERIC = ("open", "high", "low", "close", "volume", "volume_ratio",
            "spread_ratio", "close_position", "atr", "volume_sma", "spread")


def _arrays(bars) -> dict:
    """Colunas numéricas em numpy. Idempotente: um dict já pronto passa direto."""
    if isinstance(bars, dict):
        return bars
    return {c: bars[c].to_numpy(dtype=float) for c in _NUMERIC if c in bars.columns}


def _val(arrays: dict, column: str, i: int) -> float:
    coluna = arrays.get(column)
    if coluna is None or i < 0 or i >= len(coluna):
        return float("nan")
    return float(coluna[i])


def _date(bars: pd.DataFrame, i: int) -> dt.date:
    return pd.Timestamp(bars.index[i]).date()


def _all_passed(checks: list[Check]) -> bool:
    return all(c.passed() for c in checks)


def _min_bars_for_levels(config: Config) -> int:
    """Barras que o range precisa ter ANTES da candidata para o nível existir.

    Metade da janela mínima de lateralização, derivada do knob que já existe em
    vez de inventar outro. Sem isto, a segunda barra de um range já poderia
    "perfurar o suporte" formado por uma barra só.
    """
    return max(2, int(config.require("ranges.min_weeks")) // 2)


def _level_touches(arrays: dict, tr: TradingRange, i: int, level: float,
                   column: str, tolerance_atr: float) -> int:
    """Quantas barras do range ANTES de `i` encostaram no nível (dentro de `tolerance_atr`).

    Um suporte com um toque só é a mínima mais recente, não uma linha desenhada
    no gráfico. Serve de calibre para R11: com `min_*_touches: 1` (default) a
    regra é exatamente a da spec; subir para 2 exige que o nível já tenha sido
    testado antes de o spring valer.
    """
    atr = _val(arrays, "atr", i)
    if value_is_nan(atr) or atr <= 0:
        return 0
    janela = arrays[column][tr.start : i]
    if janela.size == 0:
        return 0
    return int((abs(janela - level) <= tolerance_atr * atr).sum())


def _trend_return(arrays: dict, i: int, weeks: int) -> float:
    """Retorno das `weeks` semanas ANTERIORES à barra `i`, sem incluí-la.

    O candle do climax é justamente a exaustão da tendência; se ele entrasse na
    conta, a própria queda climática satisfaria a exigência de "após tendência
    de baixa" e a regra viraria tautologia.
    """
    base = i - 1 - weeks
    if base < 0 or i - 1 < 0:
        return float("nan")
    start = _val(arrays, "close", base)
    end = _val(arrays, "close", i - 1)
    if value_is_nan(start) or value_is_nan(end) or start <= 0:
        return float("nan")
    return end / start - 1.0


# --------------------------------------------------------------------------
# R5.1 — climax de venda e de compra
# --------------------------------------------------------------------------

def detect_climax(bars: pd.DataFrame, ranges: list[TradingRange], config: Config,
                  arrays: dict | None = None) -> list[Event]:
    """Selling/Buying Climax: barra larga, volume extremo, no fim de uma tendência.

    Contexto de fase: o climax é o que ABRE a Fase A e dá origem ao range. Uma
    barra climática no meio de um range de meses é outra coisa (spring, upthrust
    ou absorção), então a regra só vale fora de range ou na largada dele.
    """
    A = _arrays(arrays if arrays is not None else bars)
    min_weeks = int(config.require("ranges.min_weeks"))
    out: list[Event] = []
    for i in range(len(bars)):
        tr = range_at(ranges, i)
        if tr is not None and (i - tr.start) >= min_weeks:
            continue  # fundo de range: não é Fase A
        for kind, bias, direction in (
            ("selling_climax", ACUMULACAO, "down"),
            ("buying_climax", DISTRIBUICAO, "up"),
        ):
            cfg = f"events.{kind}"
            close, open_ = _val(A, "close", i), _val(A, "open", i)
            if value_is_nan(close) or value_is_nan(open_):
                continue
            if (close >= open_) if direction == "down" else (close <= open_):
                continue
            trend_weeks = int(config.require(f"{cfg}.trend_weeks"))
            trend = _trend_return(A, i, trend_weeks)
            checks = [
                Check("spread/ATR", _val(A, "spread_ratio", i), ">=",
                      float(config.require(f"{cfg}.min_spread_atr")), "{:.2f}×"),
                Check("volume/média", _val(A, "volume_ratio", i), ">=",
                      float(config.require(f"{cfg}.min_volume_ratio")), "{:.2f}×"),
            ]
            if direction == "down":
                checks.append(Check(f"retorno das {trend_weeks}s anteriores", trend, "<=",
                                    float(config.require(f"{cfg}.max_trend_return")), "{:+.1%}"))
            else:
                checks.append(Check(f"retorno das {trend_weeks}s anteriores", trend, ">=",
                                    float(config.require(f"{cfg}.min_trend_return")), "{:+.1%}"))
            checks.append(Check("posição do fechamento", _val(A, "close_position", i)))
            if not _all_passed(checks):
                continue
            verbo = "Queda" if direction == "down" else "Alta"  # o rótulo do evento já vem à parte
            out.append(Event(
                kind=kind, index=i, date=_date(bars, i), bias=bias,
                summary=(f"{verbo} climática: spread {_val(A, 'spread_ratio', i):.2f}× ATR com "
                         f"volume {_val(A, 'volume_ratio', i):.2f}× a média, encerrando "
                         f"{trend:+.1%} em {trend_weeks} semanas."),
                checks=tuple(checks),
            ))
    return out


# --------------------------------------------------------------------------
# R5.2 — spring
# --------------------------------------------------------------------------

def detect_springs(bars: pd.DataFrame, ranges: list[TradingRange], config: Config,
                   arrays: dict | None = None) -> list[Event]:
    """Spring: mínima perfura o suporte, volume contido, fechamento volta pra dentro.

    Contexto de fase: exige range ATIVO na barra — spring é evento de Fase C,
    dentro da lateralização. Perfurar suporte sem range é só tendência de baixa.

    Quando a janela de recuperação ainda não terminou (o spring é da última
    semana), o evento sai com `confirmed=False`: o relatório mostra "aguardando
    fechamento de volta acima do suporte" em vez de esconder o sinal mais novo,
    que é justamente o que interessa na revisão de sexta.
    """
    A = _arrays(arrays if arrays is not None else bars)
    max_vol = float(config.require("events.spring.max_volume_ratio"))
    recovery = int(config.require("events.spring.recovery_within_candles"))
    min_touches = int(config.get("events.spring.min_support_touches", 1))
    touch_tol = float(config.get("events.spring.touch_tolerance_atr", 0.5))
    min_prior = _min_bars_for_levels(config)
    n = len(bars)
    out: list[Event] = []

    for i in range(n):
        tr = range_at(ranges, i)
        if tr is None or (i - tr.start) < min_prior:
            continue
        levels = levels_before(bars, tr, i)
        if levels is None:
            continue
        support, _resistance = levels
        low = _val(A, "low", i)
        touches = _level_touches(A, tr, i, support, "low", touch_tol)
        checks = [
            Check("mínima", low, "<", support, "{:.2f}"),
            Check("volume/média", _val(A, "volume_ratio", i), "<", max_vol, "{:.2f}×"),
            Check("toques prévios no suporte", float(touches), ">=", float(min_touches), "{:.0f}"),
        ]
        if not _all_passed(checks):
            continue

        recovered_at = None
        for j in range(i, min(i + recovery + 1, n)):
            if _val(A, "close", j) > support:
                recovered_at = j
                break
        window_complete = (i + recovery) < n
        if recovered_at is None and window_complete:
            continue  # perfurou e não voltou: rompimento de suporte, não spring

        confirmed = recovered_at is not None
        if confirmed:
            atraso = recovered_at - i
            quando = "no próprio candle" if atraso == 0 else f"{atraso} semana(s) depois"
            checks.append(Check("fechamento de volta acima do suporte (semanas)", float(atraso),
                                "<=", float(recovery), "{:.0f}"))
            resumo = (f"Mínima {low:.2f} perfurou o suporte {support:.2f} com volume "
                      f"{_val(A, 'volume_ratio', i):.2f}× a média; fechamento voltou acima "
                      f"do suporte {quando}.")
        else:
            resumo = (f"Mínima {low:.2f} perfurou o suporte {support:.2f} com "
                      f"volume {_val(A, 'volume_ratio', i):.2f}× a média; aguardando fechamento "
                      f"de volta acima de {support:.2f} (até {recovery} semana(s)).")
        out.append(Event(
            kind="spring", index=i, date=_date(bars, i), bias=ACUMULACAO,
            summary=resumo, checks=tuple(checks), confirmed=confirmed,
            refs={"support": support, "low": low, "volume": _val(A, "volume", i),
                  "range_start": tr.start_date},
        ))
    return out


# --------------------------------------------------------------------------
# R5.3 — teste do spring
# --------------------------------------------------------------------------

def detect_tests(
    bars: pd.DataFrame, springs: list[Event], config: Config, arrays: dict | None = None
) -> list[Event]:
    """Teste: recuo pós-spring, volume abaixo do spring, mínima acima da do spring.

    Contexto de fase: só existe atrelado a um spring confirmado. A exigência de
    proximidade (`max_distance_to_spring_low_atr`) é o que separa um teste de
    qualquer semana morta que aparecer nas seis seguintes — teste é
    reaproximação do suporte, não ausência de volume.
    """
    A = _arrays(arrays if arrays is not None else bars)
    within = int(config.require("events.test.within_weeks"))
    max_dist_atr = float(config.require("events.test.max_distance_to_spring_low_atr"))
    n = len(bars)
    out: list[Event] = []

    for spring in springs:
        if not spring.confirmed:
            continue
        i = spring.index
        spring_low = float(spring.refs["low"])
        spring_vol = float(spring.refs["volume"])
        for j in range(i + 1, min(i + within + 1, n)):
            low = _val(A, "low", j)
            atr = _val(A, "atr", j)
            dist_atr = (low - spring_low) / atr if atr and not value_is_nan(atr) and atr > 0 else float("nan")
            checks = [
                Check("mínima do teste", low, ">", spring_low, "{:.2f}"),
                Check("volume", _val(A, "volume", j), "<", spring_vol, "{:,.0f}"),
                Check("distância até a mínima do spring (ATR)", dist_atr, "<=", max_dist_atr, "{:.2f}"),
            ]
            if not _all_passed(checks):
                continue
            out.append(Event(
                kind="test", index=j, date=_date(bars, j), bias=ACUMULACAO,
                summary=(f"Recuo testou o spring de {spring.date.strftime('%d/%m/%Y')}: mínima "
                         f"{low:.2f} acima da mínima do spring {spring_low:.2f} ({dist_atr:.2f} ATR "
                         f"de distância) com volume {_val(A, 'volume', j):,.0f} contra "
                         f"{spring_vol:,.0f} do spring."),
                checks=tuple(checks),
                refs={"spring_index": i, "spring_date": spring.date},
            ))
            break  # o teste é o primeiro recuo que qualifica; os seguintes são outra coisa
    return out


# --------------------------------------------------------------------------
# R5.4 — SOS e SOW
# --------------------------------------------------------------------------

def _detect_strength(
    bars: pd.DataFrame, ranges: list[TradingRange], config: Config, kind: str,
    arrays: dict | None = None
) -> list[Event]:
    """Motor comum de SOS (alta) e SOW (baixa) — as regras são espelhadas."""
    A = _arrays(arrays if arrays is not None else bars)
    up = kind == "sos"
    cfg = f"events.{kind}"
    lookback = int(config.require(f"{cfg}.internal_resistance_weeks" if up else f"{cfg}.internal_support_weeks"))
    max_gap = int(config.require("ranges.max_gap_weeks"))
    n = len(bars)
    out: list[Event] = []

    for i in range(n):
        # Contexto de fase: SOS/SOW são Fase D — na borda do range ou logo além.
        tr = governing_range(ranges, i, max_gap)
        if tr is None or i < lookback:
            continue
        close, open_ = _val(A, "close", i), _val(A, "open", i)
        if value_is_nan(close) or value_is_nan(open_):
            continue
        if (close <= open_) if up else (close >= open_):
            continue
        janela_hi = A["high"][i - lookback : i]
        janela_lo = A["low"][i - lookback : i]
        level = float(janela_hi.max()) if up else float(janela_lo.min())
        checks = [
            Check("fechamento", close, ">" if up else "<", level, "{:.2f}"),
            Check("spread/ATR", _val(A, "spread_ratio", i), ">=",
                  float(config.require(f"{cfg}.min_spread_atr")), "{:.2f}×"),
            Check("volume/média", _val(A, "volume_ratio", i), ">=",
                  float(config.require(f"{cfg}.min_volume_ratio")), "{:.2f}×"),
        ]
        if up:
            checks.append(Check("posição do fechamento", _val(A, "close_position", i), ">=",
                                float(config.require(f"{cfg}.min_close_position")), "{:.2f}"))
        else:
            checks.append(Check("posição do fechamento", _val(A, "close_position", i), "<=",
                                float(config.require(f"{cfg}.max_close_position")), "{:.2f}"))
        if not _all_passed(checks):
            continue

        crossed_range = close > tr.resistance if up else close < tr.support
        nivel = "a resistência interna" if up else "o suporte interno"
        extra = ""
        if crossed_range:
            borda = tr.resistance if up else tr.support
            extra = (f" Fechou {'acima da resistência' if up else 'abaixo do suporte'} do range "
                     f"({borda:.2f}).")
        out.append(Event(
            kind=kind, index=i, date=_date(bars, i), bias=ACUMULACAO if up else DISTRIBUICAO,
            summary=(f"Fechamento {close:.2f} {'rompeu' if up else 'perdeu'} {nivel} de "
                     f"{lookback} semanas ({level:.2f}) com spread "
                     f"{_val(A, 'spread_ratio', i):.2f}× ATR e volume "
                     f"{_val(A, 'volume_ratio', i):.2f}× a média.{extra}"),
            checks=tuple(checks),
            refs={"level": level, "crossed_range": crossed_range, "close": close},
        ))
    return out


def detect_sos(bars: pd.DataFrame, ranges: list[TradingRange], config: Config,
               arrays: dict | None = None) -> list[Event]:
    return _detect_strength(bars, ranges, config, "sos", arrays)


def detect_sow(bars: pd.DataFrame, ranges: list[TradingRange], config: Config,
               arrays: dict | None = None) -> list[Event]:
    return _detect_strength(bars, ranges, config, "sow", arrays)


# --------------------------------------------------------------------------
# R5.5 — LPS e LPSY
# --------------------------------------------------------------------------

def _detect_last_point(
    bars: pd.DataFrame, drivers: list[Event], config: Config, kind: str,
    arrays: dict | None = None
) -> list[Event]:
    """Motor comum de LPS (pós-SOS) e LPSY (pós-SOW).

    "Volume decrescente por ≥ N semanas" é lido como N barras consecutivas, cada
    uma com volume abaixo da anterior — a contagem começa contra o volume do
    próprio SOS/SOW, que é o pico que precisa secar. O evento é marcado na
    ÚLTIMA barra da sequência: é ali que está o "last point".
    """
    A = _arrays(arrays if arrays is not None else bars)
    up = kind == "lps"
    cfg = f"events.{kind}"
    min_decl = int(config.require(f"{cfg}.min_declining_weeks"))
    within = int(config.require(f"{cfg}.within_weeks"))
    n = len(bars)
    out: list[Event] = []

    for driver in drivers:
        i = driver.index
        level = float(driver.refs["level"])
        driver_close = float(driver.refs["close"])
        limit = min(i + within + 1, n)
        found = False
        for start in range(i + 1, limit):
            if found:
                break
            # A sequência cresce enquanto o volume seca E o nível segura. Truncar no
            # nível importa: sem isso, o recuo continuaria contando depois de o preço
            # já ter devolvido o rompimento — e aí não é mais "last point of support".
            run_end = start - 1
            while run_end + 1 < limit:
                nxt = run_end + 1
                if _val(A, "volume", nxt) >= _val(A, "volume", run_end):
                    break
                if (_val(A, "low", nxt) < level) if up else (_val(A, "high", nxt) > level):
                    break
                run_end = nxt
            run_len = run_end - start + 1
            if run_len < min_decl:
                continue
            seg_lo = A["low"][start : run_end + 1]
            seg_hi = A["high"][start : run_end + 1]
            holds = (float(seg_lo.min()) >= level) if up else (float(seg_hi.max()) <= level)
            pulled_back = (_val(A, "close", run_end) < driver_close) if up else (_val(A, "close", run_end) > driver_close)
            checks = [
                Check("semanas de volume decrescente", float(run_len), ">=", float(min_decl), "{:.0f}"),
                Check("mínima do recuo" if up else "máxima do repique",
                      float(seg_lo.min()) if up else float(seg_hi.max()),
                      ">=" if up else "<=", level, "{:.2f}"),
                Check(f"volume da última semana / média", _val(A, "volume_ratio", run_end)),
            ]
            if not (holds and pulled_back and _all_passed(checks)):
                continue
            palavra = "Recuo" if up else "Repique"
            out.append(Event(
                kind=kind, index=run_end, date=_date(bars, run_end),
                bias=ACUMULACAO if up else DISTRIBUICAO,
                summary=(f"{palavra} de {run_len} semanas com volume decrescente "
                         f"({_val(A, 'volume', start - 1):,.0f} → {_val(A, 'volume', run_end):,.0f}) "
                         f"{'segurando acima' if up else 'contido abaixo'} do nível rompido pelo "
                         f"{'SOS' if up else 'SOW'} de {driver.date.strftime('%d/%m/%Y')} "
                         f"({level:.2f})."),
                checks=tuple(checks),
                refs={"driver_index": i, "driver_date": driver.date, "level": level},
            ))
            found = True
    return out


def detect_lps(bars: pd.DataFrame, sos_events: list[Event], config: Config,
               arrays: dict | None = None) -> list[Event]:
    return _detect_last_point(bars, sos_events, config, "lps", arrays)


def detect_lpsy(bars: pd.DataFrame, sow_events: list[Event], config: Config,
                arrays: dict | None = None) -> list[Event]:
    return _detect_last_point(bars, sow_events, config, "lpsy", arrays)


# --------------------------------------------------------------------------
# R5.6 — upthrust / UTAD
# --------------------------------------------------------------------------

def detect_upthrusts(bars: pd.DataFrame, ranges: list[TradingRange], config: Config,
                     arrays: dict | None = None) -> list[Event]:
    """Upthrust: máxima perfura a resistência e o fechamento volta pra dentro, com volume alto.

    Contexto de fase: exige range ATIVO, como o spring. Num range maduro
    (`utad_min_range_weeks` ou mais), o mesmo candle é lido como UTAD — o
    upthrust after distribution que fecha a Fase C do lado vendedor.
    """
    A = _arrays(arrays if arrays is not None else bars)
    min_vol = float(config.require("events.upthrust.min_volume_ratio"))
    utad_weeks = int(config.require("events.upthrust.utad_min_range_weeks"))
    min_touches = int(config.get("events.upthrust.min_resistance_touches", 1))
    touch_tol = float(config.get("events.upthrust.touch_tolerance_atr", 0.5))
    min_prior = _min_bars_for_levels(config)
    out: list[Event] = []

    for i in range(len(bars)):
        tr = range_at(ranges, i)
        if tr is None or (i - tr.start) < min_prior:
            continue
        levels = levels_before(bars, tr, i)
        if levels is None:
            continue
        _support, resistance = levels
        high, close = _val(A, "high", i), _val(A, "close", i)
        touches = _level_touches(A, tr, i, resistance, "high", touch_tol)
        checks = [
            Check("máxima", high, ">", resistance, "{:.2f}"),
            Check("fechamento (de volta pra dentro)", close, "<", resistance, "{:.2f}"),
            Check("volume/média", _val(A, "volume_ratio", i), ">=", min_vol, "{:.2f}×"),
            Check("toques prévios na resistência", float(touches), ">=", float(min_touches), "{:.0f}"),
        ]
        if not _all_passed(checks):
            continue
        idade = i - tr.start + 1
        utad = idade >= utad_weeks
        out.append(Event(
            kind="upthrust", index=i, date=_date(bars, i), bias=DISTRIBUICAO,
            summary=(f"Máxima {high:.2f} perfurou a resistência {resistance:.2f} e o "
                     f"fechamento voltou para {close:.2f}, com volume "
                     f"{_val(A, 'volume_ratio', i):.2f}× a média"
                     + (f", em range de {idade} semanas (leitura de UTAD)." if utad else ".")),
            checks=tuple(checks),
            refs={"resistance": resistance, "utad": utad, "range_weeks": idade},
        ))
    return out


# --------------------------------------------------------------------------
# R5.7 — esforço × resultado
# --------------------------------------------------------------------------

def detect_effort_vs_result(
    bars: pd.DataFrame, ranges: list[TradingRange], config: Config,
    arrays: dict | None = None
) -> list[Event]:
    """Volume alto sem resultado de preço: absorção ou distribuição, conforme o contexto.

    "Progresso de preço" é o corpo do candle (|close − open|): é o que a semana
    de fato entregou depois de todo aquele volume. O contexto vem da posição
    dentro do range — metade de baixo, demanda absorvendo oferta; metade de
    cima, oferta absorvendo demanda. Sem range, cai na posição do fechamento.
    """
    A = _arrays(arrays if arrays is not None else bars)
    min_vol = float(config.require("events.effort_vs_result.min_volume_ratio"))
    max_prog = float(config.require("events.effort_vs_result.max_progress_atr"))
    max_gap = int(config.require("ranges.max_gap_weeks"))
    out: list[Event] = []

    for i in range(len(bars)):
        atr = _val(A, "atr", i)
        if value_is_nan(atr) or atr <= 0:
            continue
        progress = abs(_val(A, "close", i) - _val(A, "open", i)) / atr
        checks = [
            Check("volume/média", _val(A, "volume_ratio", i), ">=", min_vol, "{:.2f}×"),
            Check("progresso do preço (corpo/ATR)", progress, "<", max_prog, "{:.2f}"),
        ]
        if not _all_passed(checks):
            continue
        tr = governing_range(ranges, i, max_gap)
        if tr is not None and tr.height > 0:
            pos = tr.position_of(_val(A, "close", i))
            onde = f"a {pos:.0%} da altura do range {tr.support:.2f}–{tr.resistance:.2f}"
            checks.append(Check("posição no range", pos, fmt="{:.0%}"))
        else:
            pos = _val(A, "close_position", i)
            onde = f"com fechamento a {pos:.0%} do candle (sem range de referência)"
            checks.append(Check("posição do fechamento", pos, fmt="{:.0%}"))
        if value_is_nan(pos):
            continue
        absorcao = pos < 0.5
        leitura = ("absorção (demanda absorvendo oferta)" if absorcao
                   else "distribuição (oferta absorvendo demanda)")
        out.append(Event(
            kind="effort_vs_result", index=i, date=_date(bars, i),
            bias=ACUMULACAO if absorcao else DISTRIBUICAO,
            summary=(f"Volume {_val(A, 'volume_ratio', i):.2f}× a média com "
                     f"corpo de apenas {progress:.2f} ATR, {onde} → {leitura}."),
            checks=tuple(checks),
            refs={"absorption": absorcao, "position": pos},
        ))
    return out


# --------------------------------------------------------------------------
# orquestração
# --------------------------------------------------------------------------

def detect_all(bars: pd.DataFrame, ranges: list[TradingRange], config: Config) -> list[Event]:
    """Todos os eventos de R5 para um papel, em ordem cronológica.

    A ordem das chamadas não é estética: teste depende de spring, LPS depende de
    SOS, LPSY depende de SOW.
    """
    if bars.empty:
        return []
    A = _arrays(bars)  # uma extração para os nove detectores
    springs = detect_springs(bars, ranges, config, A)
    sos = detect_sos(bars, ranges, config, A)
    sow = detect_sow(bars, ranges, config, A)
    events = [
        *detect_climax(bars, ranges, config, A),
        *springs,
        *detect_tests(bars, springs, config, A),
        *sos,
        *sow,
        *detect_lps(bars, sos, config, A),
        *detect_lpsy(bars, sow, config, A),
        *detect_upthrusts(bars, ranges, config, A),
        *detect_effort_vs_result(bars, ranges, config, A),
    ]
    order = list(LABELS)
    return sorted(events, key=lambda e: (e.index, order.index(e.kind) if e.kind in order else 99))
