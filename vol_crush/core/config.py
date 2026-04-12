"""
Core configuration loader.

Loads config.yaml and strategies.yaml, validates required fields,
and provides typed access throughout the application.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


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
        "google_sheets.spreadsheet_id": os.environ.get("VOL_CRUSH_GSHEET_ID"),
        "backtesting.polygon.api_key": os.environ.get("VOL_CRUSH_POLYGON_API_KEY"),
        "regime_bridge.credentials_path": os.environ.get("GOOGLE_API_CREDENTIALS_PATH"),
        "regime_bridge.sheet_id": os.environ.get("TRADE_LAB_BRIDGE_SHEET_ID"),
        "regime_bridge.sheet_name": os.environ.get("TRADE_LAB_BRIDGE_SHEET_NAME"),
    }

    for dotted_key, value in env_overrides.items():
        if value is not None:
            _set_nested(config, dotted_key, value)

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
