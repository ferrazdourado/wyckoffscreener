"""P2 — comparação entre fontes (a régua da questão §8 da spec).

As funções de comparação são puras; só `compare_sources` fala com as fontes, e
aqui elas são dublês.
"""

import datetime as dt

import pandas as pd
import pytest

from src.data.provider import DataProvider, FetchError
from src.sources import compare_actions, compare_bars, compare_sources


def serie(precos: dict[str, float], volume: float = 1000.0) -> pd.DataFrame:
    idx = pd.to_datetime(list(precos))
    valores = list(precos.values())
    return pd.DataFrame(
        {"open": valores, "high": [v + 1 for v in valores], "low": [v - 1 for v in valores],
         "close": valores, "volume": [volume] * len(valores)},
        index=idx,
    )


class Falsa(DataProvider):
    def __init__(self, bars, actions=None, erro=None):
        self.bars, self.actions, self.erro = bars, actions, erro

    def daily_bars(self, symbol, weeks):
        if self.erro:
            raise FetchError(self.erro)
        return self.bars

    def corporate_actions(self, symbol):
        if self.actions is None:
            return pd.DataFrame(columns=["date", "kind", "value"])
        return self.actions


BASE = {"2026-08-10": 10.0, "2026-08-11": 11.0, "2026-08-12": 12.0}


# --------------------------- barras ---------------------------

def test_series_identicas_nao_tem_divergencia():
    so_a, so_b, precos, volumes = compare_bars(serie(BASE), serie(BASE), 0.005, 0.01)
    assert (so_a, so_b, precos, volumes) == ([], [], [], [])


def test_pregao_faltando_no_meio_aparece_como_so_de_uma_fonte():
    faltando = {k: v for k, v in BASE.items() if k != "2026-08-11"}
    so_a, so_b, _, _ = compare_bars(serie(BASE), serie(faltando), 0.005, 0.01)
    assert so_a == [dt.date(2026, 8, 11)] and so_b == []


def test_janelas_diferentes_nao_viram_centenas_de_buracos_falsos():
    """Sem o recorte ao período comum, o começo de uma série seria "buraco" na outra."""
    curta = {"2026-08-11": 11.0, "2026-08-12": 12.0}
    so_a, so_b, _, _ = compare_bars(serie(BASE), serie(curta), 0.005, 0.01)
    assert so_a == [] and so_b == []


def test_diferenca_abaixo_da_tolerancia_e_arredondamento_da_fonte():
    outra = {k: v * 1.001 for k, v in BASE.items()}
    _, _, precos, _ = compare_bars(serie(BASE), serie(outra), 0.005, 0.01)
    assert precos == []


def test_diferenca_acima_da_tolerancia_vira_divergencia_com_os_dois_numeros():
    outra = dict(BASE, **{"2026-08-11": 12.0})
    _, _, precos, _ = compare_bars(serie(BASE), serie(outra), 0.005, 0.01)
    fechamentos = [d for d in precos if d.column == "close"]
    assert len(fechamentos) == 1
    assert (fechamentos[0].left, fechamentos[0].right) == (11.0, 12.0)
    assert fechamentos[0].diff_pct == pytest.approx(1 / 11)


def test_volume_tem_tolerancia_propria():
    _, _, _, volumes = compare_bars(serie(BASE), serie(BASE, volume=1005.0), 0.005, 0.01)
    assert volumes == []
    _, _, _, volumes = compare_bars(serie(BASE), serie(BASE, volume=1100.0), 0.005, 0.01)
    assert len(volumes) == 3


def test_serie_vazia_nao_explode():
    assert compare_bars(pd.DataFrame(), serie(BASE), 0.005, 0.01) == ([], [], [], [])


# --------------------------- proventos ---------------------------

def acoes(*itens):
    return pd.DataFrame([{"date": dt.date.fromisoformat(d), "kind": k, "value": v}
                         for d, k, v in itens])


def test_provento_registrado_por_uma_fonte_so_aparece():
    so_a, so_b = compare_actions(acoes(("2026-08-11", "dividend", 0.5)), acoes())
    assert so_a == [(dt.date(2026, 8, 11), "dividend")] and so_b == []


def test_provento_fora_da_janela_comum_nao_conta():
    so_a, _so_b = compare_actions(
        acoes(("2020-01-02", "dividend", 0.5)), acoes(),
        window=(dt.date(2026, 8, 10), dt.date(2026, 8, 12)),
    )
    assert so_a == []


# --------------------------- comparação completa ---------------------------

def test_fontes_que_concordam_produzem_veredito_de_concordancia():
    resultado = compare_sources("PETR4.SA", 4, ("a", Falsa(serie(BASE))), ("b", Falsa(serie(BASE))))
    assert resultado.agree
    assert "mesma história" in resultado.verdict()


def test_fonte_que_falha_nao_derruba_a_comparacao():
    resultado = compare_sources("PETR4.SA", 4, ("a", Falsa(serie(BASE))),
                                ("b", Falsa(None, erro="ticker inexistente")))
    assert not resultado.agree
    assert "comparação incompleta" in resultado.verdict()
    assert resultado.right_bars == 0


def test_veredito_resume_cada_tipo_de_divergencia():
    outra = dict(BASE, **{"2026-08-11": 13.0})
    resultado = compare_sources("PETR4.SA", 4, ("a", Falsa(serie(BASE))),
                                ("b", Falsa(serie(outra, volume=2000.0))))
    veredito = resultado.verdict()
    assert "preço(s) divergente(s)" in veredito and "volume(s) divergente(s)" in veredito


def test_pior_divergencia_e_a_de_maior_modulo():
    outra = dict(BASE, **{"2026-08-10": 9.0, "2026-08-12": 12.6})
    resultado = compare_sources("PETR4.SA", 4, ("a", Falsa(serie(BASE))), ("b", Falsa(serie(outra))))
    assert resultado.worst_price.date == dt.date(2026, 8, 10)


def test_comparacao_de_proventos_pode_ser_dispensada():
    chamou = []

    class Espia(Falsa):
        def corporate_actions(self, symbol):
            chamou.append(symbol)
            return super().corporate_actions(symbol)

    compare_sources("PETR4.SA", 4, ("a", Espia(serie(BASE))), ("b", Espia(serie(BASE))),
                    with_actions=False)
    assert chamou == []
