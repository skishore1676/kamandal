"""
Core configuration loader.

Loads config.yaml and strategies.yaml, validates required fields,
and provides typed access throughout the application.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return int(value)


def _env_float(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return float(value)


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load application config from YAML file.

    Looks for config/config.yaml by default.
    Falls back to config/config.example.yaml if config.yaml doesn't exist.
    Environment variables override: VOL_CRUSH_OPENAI_API_KEY, etc.
    """
    _load_dotenv()

    if config_path is None:
        config_path = _CONFIG_DIR / "config.yaml"
        if not Path(config_path).exists():
            config_path = _CONFIG_DIR / "config.example.yaml"

    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(_CONFIG_DIR / "config.example.yaml") as f:
        base_config = yaml.safe_load(f) or {}

    with open(config_path) as f:
        config = _deep_merge(base_config, yaml.safe_load(f) or {})

    # Environment variable overrides
    env_overrides = {
        "llm.provider": os.environ.get("VOL_CRUSH_LLM_PROVIDER"),
        "llm.model": os.environ.get("VOL_CRUSH_LLM_MODEL"),
        "llm.fallback_model": os.environ.get("VOL_CRUSH_LLM_MODEL_BACKUP")
        or os.environ.get("VOL_CRUSH_LLM_FALLBACK_MODEL"),
        "llm.api_key": os.environ.get("VOL_CRUSH_LLM_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY"),
        "llm.base_url": os.environ.get("VOL_CRUSH_LLM_BASE_URL"),
        "openai.api_key": os.environ.get("VOL_CRUSH_OPENAI_API_KEY"),
        "broker.tastytrade.username": os.environ.get("VOL_CRUSH_TT_USERNAME"),
        "broker.tastytrade.password": os.environ.get("VOL_CRUSH_TT_PASSWORD"),
        "broker.tastytrade.account_id": os.environ.get("VOL_CRUSH_TT_ACCOUNT_ID"),
        "broker.public.secret_token": os.environ.get("PUBLIC_SECRET_TOKEN"),
        "broker.public.api_base_url": os.environ.get("PUBLIC_API_BASE_URL"),
        "broker.public.auth_endpoint": os.environ.get("PUBLIC_AUTH_ENDPOINT"),
        "broker.public.account_id": os.environ.get("PUBLIC_ACCOUNT_ID"),
        "broker.public.session_file": os.environ.get("PUBLIC_SESSION_FILE"),
        "broker.public.account_cache_file": os.environ.get("PUBLIC_ACCOUNT_CACHE_FILE"),
        "broker.public.token_validity_minutes": os.environ.get(
            "PUBLIC_TOKEN_VALIDITY_MINUTES"
        ),
        "broker.public.api_requests_per_second": os.environ.get(
            "API_REQUESTS_PER_SECOND"
        ),
        "broker.public.api_burst_limit": os.environ.get("API_BURST_LIMIT"),
        "google_sheets.spreadsheet_id": os.environ.get("KAMANDAL_SHEET_ID")
        or os.environ.get("VOL_CRUSH_GSHEET_ID"),
        "google_sheets.credentials_file": os.environ.get(
            "GOOGLE_API_CREDENTIALS_PATH"
        ),
        "google_sheets.enabled": (
            os.environ.get("ENABLE_SHEETS_SYNC", "").lower() in {"1", "true", "yes"}
            if os.environ.get("ENABLE_SHEETS_SYNC") is not None
            else None
        ),
        "backtesting.polygon.api_key": os.environ.get("VOL_CRUSH_POLYGON_API_KEY"),
        "idea_sources.youtube.limit": _env_int("VOL_CRUSH_YOUTUBE_LIMIT"),
        "execution.bypass_daily_plan_approval": (
            os.environ.get("VOL_CRUSH_BYPASS_DAILY_PLAN_APPROVAL", "").lower()
            in {"1", "true", "yes"}
            if os.environ.get("VOL_CRUSH_BYPASS_DAILY_PLAN_APPROVAL") is not None
            else None
        ),
        "execution.auto_approve_ideas": (
            os.environ.get("VOL_CRUSH_AUTO_APPROVE_IDEAS", "").lower()
            in {"1", "true", "yes"}
            if os.environ.get("VOL_CRUSH_AUTO_APPROVE_IDEAS") is not None
            else None
        ),
        "execution.shadow_net_liquidation_value": _env_float(
            "VOL_CRUSH_SHADOW_NLV"
        ),
        "portfolio.constraints.max_single_underlying_pct": _env_float(
            "VOL_CRUSH_MAX_SINGLE_UNDERLYING_PCT"
        ),
    }

    for dotted_key, value in env_overrides.items():
        if value is not None:
            _set_nested(config, dotted_key, value)

    theta_min = _env_float("VOL_CRUSH_DAILY_THETA_MIN_PCT")
    theta_max = _env_float("VOL_CRUSH_DAILY_THETA_MAX_PCT")
    if theta_min is not None or theta_max is not None:
        current = (
            (config.get("portfolio") or {}).get("constraints") or {}
        ).get("daily_theta_pct", [0.10, 0.30])
        if not isinstance(current, list) or len(current) < 2:
            current = [0.10, 0.30]
        lower = theta_min if theta_min is not None else current[0]
        upper = theta_max if theta_max is not None else current[1]
        _set_nested(config, "portfolio.constraints.daily_theta_pct", [lower, upper])

    return config


def _load_dotenv() -> None:
    """Load a simple project-level .env file when present."""
    env_path = _PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _set_nested(d: dict, dotted_key: str, value: Any) -> None:
    """Set a value in a nested dict using dotted key notation."""
    keys = dotted_key.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def load_strategies(strategies_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load approved strategies from strategies.yaml."""
    if strategies_path is None:
        strategies_path = _CONFIG_DIR / "strategies.yaml"

    strategies_path = Path(strategies_path)
    if not strategies_path.exists():
        return []

    with open(strategies_path) as f:
        data = yaml.safe_load(f) or {}

    return data.get("strategies", []) or []


def save_strategies(
    strategies: list[dict[str, Any]], strategies_path: str | Path | None = None
) -> Path:
    """Save strategies to strategies.yaml."""
    if strategies_path is None:
        strategies_path = _CONFIG_DIR / "strategies.yaml"

    strategies_path = Path(strategies_path)
    strategies_path.parent.mkdir(parents=True, exist_ok=True)

    data = {"strategies": strategies}

    with open(strategies_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, width=120)

    return strategies_path


def load_strategy_templates(
    templates_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Load structure-level strategy templates from strategy_templates.yaml."""
    if templates_path is None:
        templates_path = _CONFIG_DIR / "strategy_templates.yaml"

    templates_path = Path(templates_path)
    if not templates_path.exists():
        return []

    with open(templates_path) as f:
        data = yaml.safe_load(f) or {}

    return data.get("templates", []) or []


def load_underlying_profiles(
    profiles_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Load underlying universe profiles from underlying_profiles.yaml."""
    if profiles_path is None:
        profiles_path = _CONFIG_DIR / "underlying_profiles.yaml"

    profiles_path = Path(profiles_path)
    if not profiles_path.exists():
        return []

    with open(profiles_path) as f:
        data = yaml.safe_load(f) or {}

    return data.get("profiles", []) or []


def get_project_root() -> Path:
    """Return the project root directory."""
    return _PROJECT_ROOT


def get_transcripts_dir() -> Path:
    """Return the data/transcripts directory."""
    return _PROJECT_ROOT / "data" / "transcripts"


def get_data_dir() -> Path:
    """Return the root data directory."""
    return _PROJECT_ROOT / "data"


def shadow_net_liquidation_value(config: Mapping[str, Any]) -> float | None:
    """Return the configured shadow-mode NLV override when set."""
    raw = (config.get("execution") or {}).get("shadow_net_liquidation_value")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
