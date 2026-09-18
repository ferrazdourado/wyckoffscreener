"""Carga do config.yaml (todas as fases).

Regra do projeto: nenhum threshold vive no código. Os defaults abaixo existem
só para que o sistema rode com um config parcial; qualquer chave presente no
arquivo do usuário vence.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "data": {
        "history_weeks": 120,
        "min_weeks_for_metrics": 21,
        "cache_path": "data/wyckoff.sqlite",
        "refetch_same_day": False,
        "market_hours": {
            "b3": {"timezone": "America/Sao_Paulo", "close": "18:00"},
            "us": {"timezone": "America/New_York", "close": "16:00"},
        },
        # P2: fonte plugável. `default` atende todo mundo; `by_market` troca a
        # fonte de um mercado; `fallback` é tentado quando a principal falha.
        "source": {"default": "yfinance", "by_market": {}, "fallback": []},
        # Ritmo da coleta — o Yahoo corta o IP em varredura grande e o corte
        # se disfarça de ticker deslistado. Ver src/data/throttle.py.
        "fetch": {
            "min_interval": 0.5,
            "retries": 2,
            "backoff": [3, 15],
            "cooldown_after": 5,
            "cooldown": 60,
        },
        "brapi": {
            "token_env": "WYCKOFF_BRAPI_TOKEN",
            "base_url": "https://brapi.dev/api",
            "timeout": 20,
            "max_range": "5y",
        },
        # Tolerâncias de `wyckoff sources`: abaixo disto é arredondamento da
        # fonte, não divergência.
        "compare": {"tolerance_pct": 0.005, "volume_tolerance_pct": 0.01, "max_rows": 12},
    },
    "metrics": {
        "volume_sma_weeks": 20,
        "atr_weeks": 20,
        "atr_method": "sma",
        "relative_strength_weeks": [4, 12],
        "close_position_on_zero_range": "neutral",
    },
    "output": {
        "csv_dir": "exports",
        "reports_dir": "reports",
        "decimals": 3,
        "chart_weeks": 60,
        "embed_charts": True,
        # P2: exportação para PDF. Os motores são tentados nesta ordem.
        "pdf": {
            "engines": ["chrome", "weasyprint", "wkhtmltopdf"],
            "chrome_binary": "",
            "timeout": 120,
            "expand_details": True,
        },
    },
    "ranges": {
        "min_weeks": 8,
        "max_close_dispersion_pct": 0.12,
        "max_gap_weeks": 8,
        "manual_overlap_tolerance": 0.10,
    },
    "events": {
        "selling_climax": {"min_spread_atr": 1.5, "min_volume_ratio": 2.0,
                           "trend_weeks": 8, "max_trend_return": -0.10},
        "buying_climax": {"min_spread_atr": 1.5, "min_volume_ratio": 2.0,
                          "trend_weeks": 8, "min_trend_return": 0.10},
        "spring": {"max_volume_ratio": 1.2, "recovery_within_candles": 2,
                   "min_support_touches": 1, "touch_tolerance_atr": 0.5},
        "test": {"within_weeks": 6, "max_distance_to_spring_low_atr": 1.0},
        "sos": {"min_spread_atr": 1.3, "min_volume_ratio": 1.5,
                "min_close_position": 0.6667, "internal_resistance_weeks": 4},
        "sow": {"min_spread_atr": 1.3, "min_volume_ratio": 1.5,
                "max_close_position": 0.3333, "internal_support_weeks": 4},
        "lps": {"min_declining_weeks": 2, "within_weeks": 8},
        "lpsy": {"min_declining_weeks": 2, "within_weeks": 8},
        "upthrust": {"min_volume_ratio": 1.5, "utad_min_range_weeks": 12,
                     "min_resistance_touches": 1, "touch_tolerance_atr": 0.5},
        "effort_vs_result": {"min_volume_ratio": 1.5, "max_progress_atr": 0.5},
    },
    "phases": {
        "bias_lookback_weeks": 8,
        "bias_min_return": 0.08,
        "breakout_confirm_weeks": 2,
    },
    "alerts": {
        "calendar_horizon_days": 14,
        "recent_event_weeks": 1,
    },
    "notify": {
        "enabled": False,
        "backend": "telegram",
        "max_chars": 4096,
        "attach_pdf": False,
        "telegram": {
            "token_env": "WYCKOFF_TELEGRAM_TOKEN",
            "chat_id_env": "WYCKOFF_TELEGRAM_CHAT_ID",
        },
        "email": {
            "host": "smtp.gmail.com",
            "port": 587,
            "use_tls": True,
            "user_env": "WYCKOFF_SMTP_USER",
            "password_env": "WYCKOFF_SMTP_PASSWORD",
            "sender": "",
            "to": [],
            "subject_prefix": "[Wyckoff]",
        },
    },
    "pnf": {
        "enabled": True,
        "box_mode": "percent",
        "box_percent": 0.02,
        "box_atr_fraction": 0.25,
        "box_absolute": 1.0,
        "reversal": 3,
        "count_line": "widest",
    },
    "backtest": {
        "horizons": [4, 8, 13, 26],
        "causal": True,
        "min_observations": 5,
    },
    "screener": {
        "universe_path": "universe.yaml",
        "default_universe": "b3_liquidas",
        "phases": ["C", "D"],
        "top": 25,
        # Piso de liquidez do candidato: mediana do volume financeiro semanal
        # das últimas 12 semanas. Existe por causa dos universos amplos.
        "min_weekly_volume": 2_000_000,
        # Idade máxima do evento que instalou a fase: fase não expira, mas
        # triagem de swing semanal não se faz com evento de meio ano atrás.
        "max_weeks_since_event": 6,
        # Proventos do universo: a requisição mais cara da coleta, e a triagem
        # não precisa dela. Ver `refresh` em screener.py.
        "fetch_actions": False,
        "report_universes": ["b3_completa", "us_completa"],
        # Lista curta por momentum de cada universo de `report_universes`.
        # Ver src/momentum.py para o porquê de 52/4.
        "momentum": {
            "enabled": True,
            "lookback_weeks": 52,
            "skip_weeks": 4,
            "top": 10,
        },
    },
}


class ConfigError(Exception):
    """Config malformado — mensagem já pronta para o usuário."""


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Acesso por caminho pontuado: cfg.get("metrics.atr_weeks")."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        sentinel = object()
        value = self.get(path, sentinel)
        if value is sentinel:
            raise ConfigError(f"config.yaml: chave obrigatória ausente: {path}")
        return value

    def __repr__(self) -> str:  # pragma: no cover - debug
        return f"Config({self.data!r})"


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    if not path.exists():
        return Config(copy.deepcopy(DEFAULTS))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: YAML inválido — {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: raiz do arquivo deve ser um mapeamento (chave: valor).")
    return Config(_deep_merge(DEFAULTS, raw))
