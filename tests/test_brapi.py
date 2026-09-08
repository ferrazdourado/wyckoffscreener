"""P2 — fonte alternativa brapi.dev: parsing, ajuste e tradução de erro.

Nenhum teste toca a rede: o transporte é injetado (`fetch_fn`) e devolve um
payload igual em forma ao que a API entregou em 08/09/2026.
"""

import datetime as dt
import json

import pandas as pd
import pytest

from src.data.brapi import (
    BrapiProvider,
    adjust_ohlc,
    adjust_volume,
    parse_actions,
    parse_history,
    range_for,
    split_factors,
    to_brapi_symbol,
)
from src.data.provider import FetchError

# 03:00Z = meia-noite em São Paulo; é assim que a fonte carimba o pregão.
SEG = 1786330800   # 2026-08-10
TER = 1786417200   # 2026-08-11


def barra(ts, close, adj=None, volume=1000.0):
    return {"date": ts, "open": close - 1, "high": close + 1, "low": close - 2,
            "close": close, "volume": volume,
            "adjustedClose": close if adj is None else adj}


def payload(barras, dividendos=None, splits=None):
    resultado = {"symbol": "PETR4", "historicalDataPrice": barras}
    if dividendos is not None or splits is not None:
        resultado["dividendsData"] = {
            "cashDividends": dividendos or [],
            "stockDividends": splits or [],
            "subscriptions": [],
        }
    return {"results": [resultado]}


def provider(pay, **kwargs):
    return BrapiProvider(fetch_fn=lambda url: json.dumps(pay).encode(), **kwargs)


# --------------------------- símbolo e janela ---------------------------

def test_sufixo_sa_do_yahoo_sai_antes_de_chamar_a_fonte():
    assert to_brapi_symbol("PETR4.SA") == "PETR4"
    assert to_brapi_symbol("petr4.sa") == "petr4"


def test_indice_passa_inalterado():
    assert to_brapi_symbol("^BVSP") == "^BVSP"


@pytest.mark.parametrize("semanas,esperado", [(4, "1mo"), (5, "3mo"), (52, "1y"), (120, "5y")])
def test_pede_a_menor_janela_que_cobre_o_historico(semanas, esperado):
    assert range_for(semanas, "5y") == esperado


def test_teto_de_janela_impede_pedir_faixa_de_plano_pago():
    assert range_for(600, "5y") == "5y"


def test_teto_invalido_falha_com_a_lista_do_que_serve():
    with pytest.raises(FetchError, match="range máximo inválido"):
        range_for(120, "3 anos")


# --------------------------- parsing ---------------------------

def test_timestamp_vira_a_data_do_pregao_em_sao_paulo():
    df = parse_history(payload([barra(SEG, 10.0), barra(TER, 11.0)]), "PETR4")
    assert list(df.index.date) == [dt.date(2026, 8, 10), dt.date(2026, 8, 11)]


def test_barra_sem_fechamento_e_descartada_sem_derrubar_a_serie():
    bruta = barra(TER, 11.0)
    bruta["close"] = None
    df = parse_history(payload([barra(SEG, 10.0), bruta]), "PETR4")
    assert len(df) == 1


def test_papel_sem_historico_falha_com_mensagem_de_coleta():
    with pytest.raises(FetchError, match="sem histórico"):
        parse_history(payload([]), "XPTO4")


def test_resposta_vazia_sugere_ticker_inexistente():
    with pytest.raises(FetchError, match="não retornou resultados"):
        parse_history({"results": []}, "XPTO4")


def test_colunas_sao_as_do_contrato_do_dataprovider():
    df = parse_history(payload([barra(SEG, 10.0)]), "PETR4")
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


# --------------------------- ajuste de preço ---------------------------

def test_fator_do_fechamento_ajustado_vale_para_o_candle_inteiro():
    """Sem isto, R2 quebra: fechamento ajustado com máxima/mínima em outra base."""
    df = parse_history(payload([barra(SEG, close=10.0, adj=9.0)]), "PETR4")
    linha = df.iloc[0]
    assert linha["close"] == pytest.approx(9.0)
    assert linha["high"] == pytest.approx(11.0 * 0.9)
    assert linha["low"] == pytest.approx(8.0 * 0.9)
    assert linha["open"] == pytest.approx(9.0 * 0.9)


def test_barra_sem_ajuste_util_fica_com_fator_um():
    bruto = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0], "adjusted_close": [None]},
        index=pd.to_datetime(["2026-08-10"]),
    )
    ajustado = adjust_ohlc(bruto)
    assert ajustado.iloc[0]["close"] == 10.0


def test_ajuste_preserva_a_posicao_do_fechamento_no_candle():
    """A métrica de R3 que mais depende da coerência interna da barra."""
    df = parse_history(payload([barra(SEG, close=10.0, adj=8.5)]), "PETR4")
    linha = df.iloc[0]
    posicao = (linha["close"] - linha["low"]) / (linha["high"] - linha["low"])
    assert posicao == pytest.approx((10.0 - 8.0) / (11.0 - 8.0))


# --------------------------- ajuste de volume ---------------------------

def test_volume_anterior_ao_grupamento_entra_na_escala_de_hoje():
    """Conferido contra o yfinance em MGLU3 (grupamento de 24/05/2024)."""
    fatores = [(pd.Timestamp("2026-08-10"), 0.1, True)]
    df = pd.DataFrame({"volume": [1000.0, 1000.0]},
                      index=pd.to_datetime(["2026-08-10", "2026-08-11"]))
    ajustado = adjust_volume(df, fatores)
    assert ajustado["volume"].tolist() == [100.0, 1000.0]


def test_fatores_de_eventos_sucessivos_se_acumulam():
    fatores = [(pd.Timestamp("2026-08-10"), 0.1, True), (pd.Timestamp("2026-08-12"), 1.05, True)]
    df = pd.DataFrame({"volume": [1000.0, 1000.0, 1000.0]},
                      index=pd.to_datetime(["2026-08-10", "2026-08-11", "2026-08-12"]))
    assert adjust_volume(df, fatores)["volume"].iloc[0] == pytest.approx(105.0)


def test_data_com_ajusta_o_proprio_pregao_e_ex_date_nao():
    """A borda vale um pregão: `lastDatePrior` é inclusive, `exDate` não."""
    com = split_factors(payload([], splits=[{"factor": 0.1, "lastDatePrior": "2026-08-10T03:00:00.000Z",
                                             "exDate": None}]))
    ex = split_factors(payload([], splits=[{"factor": 0.1, "exDate": "2026-08-11T03:00:00.000Z"}]))
    assert com[0][2] is True and com[0][0] == pd.Timestamp("2026-08-10")
    assert ex[0][2] is False and ex[0][0] == pd.Timestamp("2026-08-11")


def test_fator_neutro_nao_vira_evento():
    assert split_factors(payload([], splits=[{"factor": 1, "exDate": "2026-08-11T03:00:00.000Z"}])) == []


def test_daily_bars_ajusta_o_volume_na_mesma_requisicao():
    pay = payload([barra(SEG, 10.0, volume=1000.0), barra(TER, 10.0, volume=1000.0)],
                  splits=[{"factor": 0.1, "lastDatePrior": "2026-08-10T03:00:00.000Z", "exDate": None}])
    df = provider(pay).daily_bars("PETR4.SA", 4)
    assert df["volume"].tolist() == [100.0, 1000.0]


# --------------------------- proventos ---------------------------

def test_provento_usa_a_data_ex_quando_existe():
    frame = parse_actions(payload([], dividendos=[
        {"rate": 0.5, "exDate": "2026-08-11T03:00:00.000Z", "lastDatePrior": "2026-08-10T03:00:00.000Z"}
    ]))
    assert frame.iloc[0]["date"] == dt.date(2026, 8, 11)
    assert frame.iloc[0]["kind"] == "dividend"


def test_provento_sem_data_ex_cai_para_a_data_com():
    frame = parse_actions(payload([], dividendos=[
        {"rate": 0.5, "exDate": None, "lastDatePrior": "2026-08-10T03:00:00.000Z"}
    ]))
    assert frame.iloc[0]["date"] == dt.date(2026, 8, 10)


def test_provento_sem_data_nenhuma_e_ignorado():
    assert parse_actions(payload([], dividendos=[{"rate": 0.5}])).empty


def test_papel_sem_proventos_devolve_frame_no_contrato():
    frame = parse_actions(payload([]))
    assert frame.empty and list(frame.columns) == ["date", "kind", "value"]


# --------------------------- erros ---------------------------

def test_401_sem_token_ensina_a_conseguir_um():
    def explode(url):
        raise FetchError("brapi HTTP 401 — Token de autenticação não fornecido")

    with pytest.raises(FetchError, match="brapi.dev e exporte-o"):
        BrapiProvider(fetch_fn=explode).daily_bars("BBAS3.SA", 4)


def test_401_com_token_aponta_para_a_credencial_e_nao_para_o_cadastro():
    def explode(url):
        raise FetchError("brapi HTTP 401")

    with pytest.raises(FetchError, match="recusado"):
        BrapiProvider(token="abc", fetch_fn=explode).daily_bars("BBAS3.SA", 4)


def test_resposta_que_nao_e_json_vira_erro_de_coleta():
    with pytest.raises(FetchError, match="não-JSON"):
        BrapiProvider(fetch_fn=lambda url: b"<html>manutencao</html>").daily_bars("PETR4.SA", 4)


def test_token_vai_na_url_quando_configurado():
    vistas = []

    def espia(url):
        vistas.append(url)
        return json.dumps(payload([barra(SEG, 10.0)])).encode()

    BrapiProvider(token="segredo", fetch_fn=espia).daily_bars("PETR4.SA", 4)
    assert "token=segredo" in vistas[0] and "/quote/PETR4?" in vistas[0]


def test_serie_e_recortada_ao_historico_pedido():
    barras = [barra(SEG - 86400 * dias, 10.0) for dias in range(120, -1, -1)]
    df = provider(payload(barras)).daily_bars("PETR4.SA", 4)
    assert len(df) < len(barras)
    assert (df.index.max() - df.index.min()).days <= 7 * 5


def test_weekly_bars_vem_de_graca_pela_agregacao_do_diario():
    barras = [barra(SEG + 86400 * dias, 10.0) for dias in range(5)]
    semanal = provider(payload(barras)).weekly_bars("PETR4.SA", 4)
    assert len(semanal) == 1 and semanal.iloc[0]["volume"] == 5000.0
