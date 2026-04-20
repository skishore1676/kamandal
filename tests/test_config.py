"""Tests for vol_crush.core.config"""

import tempfile
import os
from pathlib import Path

import pytest
import yaml

from vol_crush.core.config import (
    load_config,
    load_strategies,
    save_strategies,
    get_project_root,
    get_transcripts_dir,
    _set_nested,
    _deep_merge,
)


def test_load_config_from_example():
    """Config loader should fall back to config.example.yaml."""
    config = load_config(get_project_root() / "config" / "config.example.yaml")
    assert config["app"]["name"] == "kamandal"
    assert "openai" in config
    assert "portfolio" in config
    assert "storage" in config
    assert "data_sources" in config
    assert "regimes" in config["portfolio"]
    assert config["execution"]["mode"] == "shadow"


def test_load_config_explicit_path():
    """Config loader should accept an explicit file path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump({"app": {"name": "test", "mode": "test"}}, f)
        f.flush()
        config = load_config(f.name)
    assert config["app"]["name"] == "test"
    os.unlink(f.name)


def test_load_config_missing_file():
    """Config loader should raise FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")


def test_load_config_env_override(monkeypatch):
    """Environment variables should override config values."""
    monkeypatch.setenv("VOL_CRUSH_OPENAI_API_KEY", "sk-test-key-123")
    config = load_config()
    assert config["openai"]["api_key"] == "sk-test-key-123"


def test_load_config_daily_plan_bypass_env(monkeypatch):
    monkeypatch.setenv("VOL_CRUSH_BYPASS_DAILY_PLAN_APPROVAL", "true")
    config = load_config(get_project_root() / "config" / "config.example.yaml")
    assert config["execution"]["bypass_daily_plan_approval"] is True


def test_load_config_auto_approve_ideas_env(monkeypatch):
    monkeypatch.setenv("VOL_CRUSH_AUTO_APPROVE_IDEAS", "true")
    config = load_config(get_project_root() / "config" / "config.example.yaml")
    assert config["execution"]["auto_approve_ideas"] is True


def test_load_config_youtube_limit_env(monkeypatch):
    monkeypatch.setenv("VOL_CRUSH_YOUTUBE_LIMIT", "1")
    config = load_config(get_project_root() / "config" / "config.example.yaml")
    assert config["idea_sources"]["youtube"]["limit"] == 1


def test_load_config_constraint_envs(monkeypatch):
    monkeypatch.setenv("VOL_CRUSH_DAILY_THETA_MIN_PCT", "0.0")
    monkeypatch.setenv("VOL_CRUSH_DAILY_THETA_MAX_PCT", "0.5")
    monkeypatch.setenv("VOL_CRUSH_MAX_SINGLE_UNDERLYING_PCT", "60")
    config = load_config(get_project_root() / "config" / "config.example.yaml")
    assert config["portfolio"]["constraints"]["daily_theta_pct"] == [0.0, 0.5]
    assert config["portfolio"]["constraints"]["max_single_underlying_pct"] == 60.0


def test_set_nested():
    """_set_nested should set deeply nested keys."""
    d = {}
    _set_nested(d, "a.b.c", "value")
    assert d["a"]["b"]["c"] == "value"


def test_deep_merge():
    """_deep_merge should recursively merge dicts."""
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    override = {"a": {"y": 99, "z": 100}, "c": 4}
    result = _deep_merge(base, override)
    assert result == {"a": {"x": 1, "y": 99, "z": 100}, "b": 3, "c": 4}


def test_save_and_load_strategies():
    """Round-trip: save strategies then load them back."""
    strategies = [
        {
            "id": "test_strat",
            "name": "Test Strategy",
            "structure": "short_strangle",
            "filters": {"iv_rank_min": 30, "dte_range": [30, 45]},
            "management": {"profit_target_pct": 50},
            "allocation": {"max_bpr_pct": 25},
            "backtest_approved": False,
            "dry_run_passed": False,
        }
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "strategies.yaml"
        save_strategies(strategies, path)
        loaded = load_strategies(path)

    assert len(loaded) == 1
    assert loaded[0]["id"] == "test_strat"
    assert loaded[0]["filters"]["iv_rank_min"] == 30
    assert loaded[0]["management"]["profit_target_pct"] == 50


def test_load_strategies_missing_file():
    """Loading from a nonexistent path returns empty list."""
    result = load_strategies("/nonexistent/strategies.yaml")
    assert result == []


def test_get_project_root():
    root = get_project_root()
    assert (root / "vol_crush").is_dir()


def test_get_transcripts_dir():
    tdir = get_transcripts_dir()
    assert tdir.name == "transcripts"
    assert tdir.parent.name == "data"
