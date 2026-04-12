"""Tests for the strategy template + underlying profile resolution layer."""

from __future__ import annotations

from vol_crush.core.models import (
    StrategyTemplate,
    StrategyType,
    UnderlyingProfile,
    resolve_all_strategies,
    resolve_strategy,
)


def _template(template_id: str, structure: str) -> StrategyTemplate:
    return StrategyTemplate.from_dict(
        {
            "id": template_id,
            "name": template_id,
            "structure": structure,
            "filters": {
                "iv_rank_min": 25,
                "dte_range": [30, 45],
                "delta_range": [0.14, 0.18],
            },
            "management": {
                "profit_target_pct": 50,
                "max_loss_multiple": 2.0,
                "roll_dte_trigger": 21,
            },
            "allowed_regimes": ["high_iv", "normal_iv"],
        }
    )


def _profile(
    profile_id: str, symbols: list[str], allowed: list[str], bpr: float = 15.0
) -> UnderlyingProfile:
    return UnderlyingProfile(
        profile_id=profile_id,
        symbols=symbols,
        allowed_structures=allowed,
        max_bpr_pct=bpr,
        max_per_position_pct=10.0,
        max_positions=3,
    )


def test_resolve_strategy_merges_template_and_profile() -> None:
    template = _template("ps", "put_spread")
    profile = _profile("idx", ["SPY", "QQQ"], ["put_spread"], bpr=20.0)

    resolved = resolve_strategy(template, profile)

    assert resolved.id == "ps:idx"
    assert resolved.structure == StrategyType.PUT_SPREAD
    assert resolved.filters.underlyings == ["SPY", "QQQ"]
    assert resolved.allocation.max_bpr_pct == 20.0
    assert resolved.management.profit_target_pct == 50.0
    assert resolved.filters.iv_rank_min == 25
    assert resolved.allowed_regimes == ["high_iv", "normal_iv"]


def test_resolve_all_strategies_cross_product() -> None:
    """Each template should produce one resolved strategy per eligible profile."""
    templates = [
        _template("ps", "put_spread"),
        _template("ic", "iron_condor"),
    ]
    profiles = [
        _profile("idx", ["SPY", "QQQ"], ["put_spread", "iron_condor"], bpr=20.0),
        _profile("bond", ["TLT"], ["put_spread"], bpr=10.0),
    ]

    resolved = resolve_all_strategies(templates, profiles)

    assert len(resolved) == 3
    ps_on_idx = [
        s for s in resolved if s.id == "ps:idx" and "SPY" in s.filters.underlyings
    ]
    ps_on_bond = [
        s for s in resolved if s.id == "ps:bond" and "TLT" in s.filters.underlyings
    ]
    ic_on_idx = [
        s for s in resolved if s.id == "ic:idx" and "SPY" in s.filters.underlyings
    ]
    assert len(ps_on_idx) == 1
    assert len(ps_on_bond) == 1
    assert len(ic_on_idx) == 1
    assert ps_on_idx[0].allocation.max_bpr_pct == 20.0
    assert ps_on_bond[0].allocation.max_bpr_pct == 10.0


def test_resolve_all_strategies_excludes_ineligible_profiles() -> None:
    """Iron condor template should NOT resolve against a profile that doesn't allow it."""
    templates = [_template("ic", "iron_condor")]
    profiles = [_profile("bond", ["TLT"], ["put_spread"])]

    resolved = resolve_all_strategies(templates, profiles)

    assert len(resolved) == 0


def test_strategy_template_round_trip() -> None:
    raw = {
        "id": "test",
        "name": "Test Template",
        "structure": "short_put",
        "filters": {"iv_rank_min": 30, "dte_range": [25, 60]},
        "management": {"profit_target_pct": 40},
        "allowed_regimes": ["high_iv"],
        "avoid_earnings": True,
    }
    template = StrategyTemplate.from_dict(raw)

    assert template.structure == StrategyType.SHORT_PUT
    assert template.filters.iv_rank_min == 30
    assert template.filters.dte_range == (25, 60)
    assert template.management.profit_target_pct == 40
    assert template.allowed_regimes == ["high_iv"]
    assert template.avoid_earnings is True


def test_underlying_profile_round_trip() -> None:
    raw = {
        "profile_id": "test",
        "name": "Test Profile",
        "symbols": ["SPY", "QQQ"],
        "allowed_structures": ["put_spread", "iron_condor"],
        "max_bpr_pct": 25.0,
        "max_positions": 4,
        "earnings_sensitive": False,
    }
    profile = UnderlyingProfile.from_dict(raw)

    assert profile.symbols == ["SPY", "QQQ"]
    assert profile.allowed_structures == ["put_spread", "iron_condor"]
    assert profile.max_bpr_pct == 25.0
    assert profile.earnings_sensitive is False


def test_config_loader_parses_real_files() -> None:
    """Smoke test: the actual strategy_templates.yaml and underlying_profiles.yaml parse cleanly."""
    from vol_crush.core.config import load_strategy_templates, load_underlying_profiles

    templates = load_strategy_templates()
    profiles = load_underlying_profiles()

    assert len(templates) >= 3
    assert len(profiles) >= 3
    assert all("id" in t for t in templates)
    assert all("profile_id" in p for p in profiles)

    # Resolve and verify no empty results
    resolved = resolve_all_strategies(
        [StrategyTemplate.from_dict(t) for t in templates],
        [UnderlyingProfile.from_dict(p) for p in profiles],
    )
    assert len(resolved) >= 5
