"""Tests for vol_crush.core.models"""

from vol_crush.core.models import (
    Strategy,
    StrategyType,
    StrategyFilters,
    ManagementRules,
    StrategyAllocation,
    ExtractedStrategyCandidate,
)


def test_strategy_type_enum():
    assert StrategyType.SHORT_STRANGLE.value == "short_strangle"
    assert StrategyType("iron_condor") == StrategyType.IRON_CONDOR


def test_management_rules_defaults():
    rules = ManagementRules()
    assert rules.profit_target_pct == 50.0
    assert rules.max_loss_multiple == 2.0
    assert rules.roll_dte_trigger == 21
    assert rules.roll_for_credit is True


def test_management_rules_from_dict():
    rules = ManagementRules.from_dict(
        {
            "profit_target_pct": 25,
            "max_loss_multiple": 1.5,
            "roll_dte_trigger": 14,
            "unknown_field": "ignored",
        }
    )
    assert rules.profit_target_pct == 25
    assert rules.max_loss_multiple == 1.5
    assert rules.roll_dte_trigger == 14


def test_strategy_filters_from_dict():
    filters = StrategyFilters.from_dict(
        {
            "iv_rank_min": 30,
            "dte_range": [30, 45],
            "delta_range": [0.14, 0.18],
            "underlyings": ["SPY", "IWM"],
        }
    )
    assert filters.iv_rank_min == 30
    assert filters.dte_range == (30, 45)
    assert filters.delta_range == (0.14, 0.18)
    assert filters.underlyings == ["SPY", "IWM"]


def test_strategy_allocation_from_dict():
    alloc = StrategyAllocation.from_dict(
        {
            "max_bpr_pct": 40,
            "max_positions": 10,
        }
    )
    assert alloc.max_bpr_pct == 40
    assert alloc.max_positions == 10
    assert alloc.max_per_position_pct == 10.0  # default


def test_strategy_from_dict_full():
    data = {
        "id": "core_strangle",
        "name": "Core Short Strangle",
        "structure": "short_strangle",
        "description": "Sell 16-delta strangles on major ETFs",
        "filters": {
            "iv_rank_min": 30,
            "dte_range": [30, 45],
            "delta_range": [0.14, 0.18],
            "underlyings": ["SPY", "IWM", "QQQ"],
        },
        "management": {
            "profit_target_pct": 50,
            "max_loss_multiple": 2.0,
            "roll_dte_trigger": 21,
        },
        "allocation": {
            "max_bpr_pct": 30,
            "max_per_position_pct": 15,
            "max_positions": 5,
        },
        "source_traders": ["Mike Butler", "Sarah Chen"],
        "backtest_approved": False,
        "dry_run_passed": False,
    }

    strat = Strategy.from_dict(data)
    assert strat.id == "core_strangle"
    assert strat.structure == StrategyType.SHORT_STRANGLE
    assert strat.filters.iv_rank_min == 30
    assert strat.filters.dte_range == (30, 45)
    assert strat.management.profit_target_pct == 50
    assert strat.allocation.max_bpr_pct == 30
    assert strat.source_traders == ["Mike Butler", "Sarah Chen"]


def test_strategy_from_dict_unknown_structure():
    data = {
        "id": "weird",
        "name": "Weird Strategy",
        "structure": "banana_spread",
    }
    strat = Strategy.from_dict(data)
    assert strat.structure == StrategyType.CUSTOM


def test_strategy_to_dict_round_trip():
    data = {
        "id": "test",
        "name": "Test",
        "structure": "short_put",
        "filters": {"dte_range": [30, 45], "delta_range": [0.18, 0.22]},
        "management": {"profit_target_pct": 50},
        "allocation": {"max_bpr_pct": 20},
    }
    strat = Strategy.from_dict(data)
    d = strat.to_dict()
    assert d["structure"] == "short_put"
    assert d["filters"]["dte_range"] == [30, 45]
    assert d["management"]["profit_target_pct"] == 50


def test_extracted_candidate_summary():
    c = ExtractedStrategyCandidate(
        source_file="test.txt",
        trader_name="Tom",
        show_name="Bootstrappers",
        strategy_name="Core Strangle",
        structure="short_strangle",
        description="A short strangle strategy on SPY with 16 delta wings",
    )
    s = c.summary()
    assert "Tom" in s
    assert "Core Strangle" in s
    assert "short_strangle" in s
