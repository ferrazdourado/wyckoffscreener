"""Config: merge sobre os defaults e acesso por caminho pontuado."""

import pytest

from src.config import ConfigError, load_config


def escreve(tmp_path, texto):
    path = tmp_path / "config.yaml"
    path.write_text(texto, encoding="utf-8")
    return path


def test_arquivo_ausente_usa_defaults(tmp_path):
    cfg = load_config(tmp_path / "nao_existe.yaml")
    assert cfg.get("metrics.atr_weeks") == 20


def test_config_parcial_faz_merge_profundo(tmp_path):
    """Sobrescrever uma chave não apaga as irmãs."""
    cfg = load_config(escreve(tmp_path, "metrics:\n  atr_weeks: 14\n"))
    assert cfg.get("metrics.atr_weeks") == 14
    assert cfg.get("metrics.volume_sma_weeks") == 20
    assert cfg.get("data.history_weeks") == 120


def test_caminho_inexistente_devolve_default():
    cfg = load_config("config.yaml")
    assert cfg.get("metrics.inexistente", "x") == "x"
    assert cfg.get("nada.de.nada") is None


def test_require_falha_com_o_caminho_na_mensagem():
    cfg = load_config("config.yaml")
    with pytest.raises(ConfigError, match="metrics.nao_existe"):
        cfg.require("metrics.nao_existe")


def test_yaml_invalido(tmp_path):
    with pytest.raises(ConfigError, match="YAML inválido"):
        load_config(escreve(tmp_path, "metrics: [\n  - a\n"))


def test_raiz_nao_mapeamento(tmp_path):
    with pytest.raises(ConfigError, match="mapeamento"):
        load_config(escreve(tmp_path, "- a\n- b\n"))


def test_config_do_projeto_tem_o_que_a_fase_1_precisa():
    cfg = load_config("config.yaml")
    for chave in ("data.history_weeks", "data.cache_path", "metrics.volume_sma_weeks",
                  "metrics.atr_weeks", "metrics.atr_method", "metrics.relative_strength_weeks",
                  "data.market_hours.b3.timezone", "data.market_hours.us.close"):
        assert cfg.require(chave) is not None
