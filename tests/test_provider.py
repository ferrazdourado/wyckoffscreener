"""R2 — normalização da fonte e detecção de candle semanal não fechado."""

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.data.provider import (
    FetchError,
    MarketHours,
    is_week_closed,
    normalize_ohlcv,
    week_start,
)

B3 = MarketHours(timezone="America/Sao_Paulo", close="18:00")
US = MarketHours(timezone="America/New_York", close="16:00")
SEGUNDA = dt.date(2026, 8, 31)  # semana 31/08 a 04/09


def em(tz: str, *args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=ZoneInfo(tz))


# --------------------------- semana fechada ---------------------------

def test_semana_ainda_em_formacao_na_quarta():
    assert not is_week_closed(SEGUNDA, B3, em("America/Sao_Paulo", 2026, 9, 2, 12, 0))


def test_sexta_um_minuto_antes_do_fechamento_ainda_nao_fechou():
    assert not is_week_closed(SEGUNDA, B3, em("America/Sao_Paulo", 2026, 9, 4, 17, 59))


def test_sexta_no_horario_de_fechamento_ja_fechou():
    assert is_week_closed(SEGUNDA, B3, em("America/Sao_Paulo", 2026, 9, 4, 18, 0))


def test_semana_seguinte_esta_fechada():
    assert is_week_closed(SEGUNDA, B3, em("America/Sao_Paulo", 2026, 9, 7, 9, 0))


def test_mercados_fecham_em_horarios_diferentes():
    """16h ET = 17h BRT: às 17h30 BRT de sexta o US já fechou, a B3 não."""
    agora = em("America/Sao_Paulo", 2026, 9, 4, 17, 30)
    assert is_week_closed(SEGUNDA, US, agora)
    assert not is_week_closed(SEGUNDA, B3, agora)


def test_now_em_outro_fuso_e_convertido():
    utc = dt.datetime(2026, 9, 4, 20, 59, tzinfo=dt.UTC)  # 17:59 BRT
    assert not is_week_closed(SEGUNDA, B3, utc)
    assert is_week_closed(SEGUNDA, B3, utc + dt.timedelta(minutes=1))


@pytest.mark.parametrize("dia,esperado", [
    (dt.date(2026, 8, 31), dt.date(2026, 8, 31)),  # segunda
    (dt.date(2026, 9, 4), dt.date(2026, 8, 31)),   # sexta
    (dt.date(2026, 9, 6), dt.date(2026, 8, 31)),   # domingo
    (dt.date(2026, 9, 7), dt.date(2026, 9, 7)),    # segunda seguinte
])
def test_week_start(dia, esperado):
    assert week_start(dia) == esperado


# --------------------------- normalização ---------------------------

def _multiindex_df(symbol="PETR4.SA"):
    idx = pd.date_range("2026-08-17", periods=3, freq="7D")
    cols = pd.MultiIndex.from_product(
        [["Close", "High", "Low", "Open", "Volume"], [symbol]], names=["Price", "Ticker"]
    )
    return pd.DataFrame(
        [[11, 12, 10, 10.5, 100], [11.5, 13, 11, 11, 200], [12, 14, 11.5, 11.5, 300]],
        index=idx, columns=cols,
    )


def test_normaliza_multiindex_do_yfinance():
    out = normalize_ohlcv(_multiindex_df(), "PETR4.SA")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out["close"].iloc[-1] == 12.0
    assert out.index.name == "date"
    assert out.index.tz is None


def test_normaliza_colunas_planas():
    idx = pd.date_range("2026-08-17", periods=2, freq="7D")
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [3, 4], "Low": [0.5, 1], "Close": [2, 3], "Volume": [10, 20]},
        index=idx,
    )
    out = normalize_ohlcv(df, "XPTO")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


def test_adj_close_vira_close_quando_nao_ha_close():
    idx = pd.date_range("2026-08-17", periods=2, freq="7D")
    df = pd.DataFrame(
        {"Open": [1, 2], "High": [3, 4], "Low": [0.5, 1], "Adj Close": [2, 3], "Volume": [10, 20]},
        index=idx,
    )
    assert normalize_ohlcv(df, "XPTO")["close"].tolist() == [2.0, 3.0]


def test_linhas_sem_preco_sao_descartadas():
    df = _multiindex_df()
    df.iloc[1, :] = float("nan")
    out = normalize_ohlcv(df, "PETR4.SA")
    assert len(out) == 2


def test_volume_ausente_vira_zero():
    df = _multiindex_df()
    df.loc[df.index[1], ("Volume", "PETR4.SA")] = float("nan")
    out = normalize_ohlcv(df, "PETR4.SA")
    assert out["volume"].iloc[1] == 0.0


def test_datas_duplicadas_mantem_a_ultima():
    df = _multiindex_df()
    df.index = [df.index[0], df.index[0], df.index[2]]
    out = normalize_ohlcv(df, "PETR4.SA")
    assert len(out) == 2


def test_resposta_vazia_devolve_dataframe_vazio():
    assert normalize_ohlcv(pd.DataFrame(), "XPTO").empty
    assert normalize_ohlcv(None, "XPTO").empty


def test_colunas_faltando_falham_com_mensagem_util():
    idx = pd.date_range("2026-08-17", periods=2, freq="7D")
    df = pd.DataFrame({"Open": [1, 2], "Close": [2, 3]}, index=idx)
    with pytest.raises(FetchError, match="colunas ausentes"):
        normalize_ohlcv(df, "XPTO")


# --------------------------- agregação diário -> semanal ---------------------------

from src.data.provider import aggregate_weekly


def diario(datas, **series):
    idx = pd.to_datetime(datas)
    n = len(idx)
    base = {"open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n,
            "close": [1.0] * n, "volume": [1.0] * n}
    base.update(series)
    return pd.DataFrame(base, index=idx)


SEMANA = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def test_agregacao_pega_primeira_abertura_e_ultimo_fechamento():
    d = diario(SEMANA, open=[10, 11, 12, 13, 14], close=[11, 12, 13, 14, 15])
    w = aggregate_weekly(d)
    assert len(w) == 1
    assert w["open"].iloc[0] == 10.0
    assert w["close"].iloc[0] == 15.0


def test_agregacao_pega_maxima_e_minima_da_semana():
    d = diario(SEMANA, high=[10, 20, 15, 12, 11], low=[9, 8, 3, 7, 6])
    w = aggregate_weekly(d)
    assert w["high"].iloc[0] == 20.0
    assert w["low"].iloc[0] == 3.0


def test_agregacao_soma_o_volume_dos_pregoes():
    """O motivo de existir desta função: o volume semanal do yfinance não bate."""
    d = diario(SEMANA, volume=[68431400, 49726600, 54091600, 51488400, 28838000])
    assert aggregate_weekly(d)["volume"].iloc[0] == 252576000.0


def test_agregacao_conta_pregoes_e_ancora_na_segunda():
    w = aggregate_weekly(diario(SEMANA))
    assert w.index[0] == pd.Timestamp("2026-08-31")
    assert w["trading_days"].iloc[0] == 5


def test_semana_curta_por_feriado_e_registrada_nao_descartada():
    """07/09 (Independência) e 08/09 sem pregão: a semana existe com 3 dias."""
    d = diario(["2026-09-09", "2026-09-10", "2026-09-11"])
    w = aggregate_weekly(d)
    assert len(w) == 1
    assert w.index[0] == pd.Timestamp("2026-09-07")
    assert w["trading_days"].iloc[0] == 3


def test_semanas_sem_pregao_nao_viram_candle():
    d = diario(["2026-08-31", "2026-09-14"])   # pula a semana de 07/09 inteira
    w = aggregate_weekly(d)
    assert list(w.index) == [pd.Timestamp("2026-08-31"), pd.Timestamp("2026-09-14")]


def test_semanas_distintas_nao_se_misturam():
    d = diario(["2026-08-28", "2026-08-31"], close=[5.0, 9.0], volume=[100.0, 200.0])
    w = aggregate_weekly(d)
    assert len(w) == 2
    assert w["close"].tolist() == [5.0, 9.0]
    assert w["volume"].tolist() == [100.0, 200.0]


def test_agregacao_de_dataframe_vazio():
    assert aggregate_weekly(pd.DataFrame()).empty
    assert aggregate_weekly(None).empty
