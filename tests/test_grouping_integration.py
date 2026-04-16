"""Integration tests that stitch the position grouping layer across consumers.

Covers:
    - position_manager refuses to act on management_status=manual_review_required groups
    - position_manager's close-all-legs assertion prevents partial-leg closes
    - optimizer's orphan_leg guard blocks new opens when unmanageable groups exist
    - optimizer's auto-managed position_count excludes manual-review groups
"""

from __future__ import annotations

from vol_crush.core.models import (
    CandidatePosition,
    Greeks,
    GroupConfidence,
    ManagementStatus,
    OptionLeg,
    PlanDecision,
    PortfolioSnapshot,
    Position,
    PositionSource,
    Strategy,
    StrategyType,
    TradeAction,
    TradeIdea,
    IdeaStatus,
)
from vol_crush.integrations.fixtures import FixtureMarketDataProvider
from vol_crush.integrations.storage import LocalStore
from vol_crush.optimizer.service import (
    _auto_managed_positions,
    _orphan_leg_count,
    build_trade_plan,
    evaluate_constraints,
)
from vol_crush.position_manager import service as position_manager_service

# ── Position manager safety gate ────────────────────────────────────────────


def test_position_manager_skips_manual_review_group(tmp_path, monkeypatch) -> None:
    store = LocalStore(sqlite_path=tmp_path / "km.db", audit_dir=tmp_path / "audit")
    # A group that would otherwise hit profit target, but is flagged for manual review.
    store.save_positions(
        [
            Position(
                position_id="inferred:IWM:..",
                underlying="IWM",
                strategy_id="short_call",
                legs=[OptionLeg("IWM", "2026-05-15", 220.0, "call", "sell")],
                open_credit=2.0,
                current_value=0.4,
                greeks=Greeks(delta=-0.1),
                dte_remaining=25,
                pnl_pct=80.0,
                bpr=4400.0,
                group_id="inferred:IWM:..",
                source=PositionSource.PUBLIC_INFERRED.value,
                strategy_type=StrategyType.SHORT_CALL.value,
                quantity=1,
                confidence=GroupConfidence.MEDIUM.value,
                management_status=ManagementStatus.MANUAL_REVIEW_REQUIRED.value,
            )
        ]
    )
    monkeypatch.setattr(
        "vol_crush.position_manager.service.build_local_store", lambda _: store
    )
    monkeypatch.setattr(
        position_manager_service,
        "_strategy_map",
        lambda: {
            "short_call": Strategy.from_dict(
                {
                    "id": "short_call",
                    "name": "Manual review fallback",
                    "structure": "short_call",
                    "management": {
                        "profit_target_pct": 50,
                        "max_loss_multiple": 2.0,
                        "roll_dte_trigger": 21,
                    },
                }
            )
        },
    )

    actions = position_manager_service.evaluate_positions({})
    assert actions == []


def test_position_manager_closes_full_group_not_single_leg(
    tmp_path, monkeypatch
) -> None:
    """A profit-target close on an iron condor must carry all 4 legs."""
    store = LocalStore(sqlite_path=tmp_path / "km.db", audit_dir=tmp_path / "audit")
    condor_legs = [
        OptionLeg("AAPL", "2026-05-15", 180.0, "put", "buy"),
        OptionLeg("AAPL", "2026-05-15", 185.0, "put", "sell"),
        OptionLeg("AAPL", "2026-05-15", 205.0, "call", "sell"),
        OptionLeg("AAPL", "2026-05-15", 210.0, "call", "buy"),
    ]
    store.save_positions(
        [
            Position(
                position_id="inferred:AAPL:ic",
                underlying="AAPL",
                strategy_id="core_iron_condor",
                legs=condor_legs,
                open_credit=1.35,
                current_value=0.50,
                greeks=Greeks(delta=0.01, theta=0.10),
                dte_remaining=25,
                pnl_pct=62.0,
                bpr=365.0,
                group_id="inferred:AAPL:ic",
                source=PositionSource.PUBLIC_INFERRED.value,
                strategy_type=StrategyType.IRON_CONDOR.value,
                quantity=1,
                confidence=GroupConfidence.HIGH.value,
                management_status=ManagementStatus.AUTO.value,
            )
        ]
    )
    monkeypatch.setattr(
        "vol_crush.position_manager.service.build_local_store", lambda _: store
    )
    monkeypatch.setattr(
        position_manager_service,
        "_strategy_map",
        lambda: {
            "core_iron_condor": Strategy.from_dict(
                {
                    "id": "core_iron_condor",
                    "name": "Core iron condor",
                    "structure": "iron_condor",
                    "management": {
                        "profit_target_pct": 50,
                        "max_loss_multiple": 2.0,
                        "roll_dte_trigger": 21,
                    },
                }
            )
        },
    )

    actions = position_manager_service.evaluate_positions({})
    assert len(actions) == 1
    action = actions[0]
    assert action.action == TradeAction.CLOSE
    assert (
        len(action.legs) == 4
    ), "Close must include every leg of the iron condor group"


def test_position_manager_uses_structural_strategy_lookup(
    tmp_path, monkeypatch
) -> None:
    """A Public-inferred short_strangle with empty strategy_id should match a
    strategies.yaml rule whose structure is short_strangle."""
    store = LocalStore(sqlite_path=tmp_path / "km.db", audit_dir=tmp_path / "audit")
    store.save_positions(
        [
            Position(
                position_id="inferred:SPY:ss",
                underlying="SPY",
                strategy_id="",  # no direct rule binding
                legs=[
                    OptionLeg("SPY", "2026-05-15", 510.0, "put", "sell"),
                    OptionLeg("SPY", "2026-05-15", 530.0, "call", "sell"),
                ],
                open_credit=4.0,
                current_value=1.0,
                greeks=Greeks(theta=0.20),
                dte_remaining=28,
                pnl_pct=75.0,
                bpr=10400.0,
                source=PositionSource.PUBLIC_INFERRED.value,
                strategy_type=StrategyType.SHORT_STRANGLE.value,
                confidence=GroupConfidence.HIGH.value,
                management_status=ManagementStatus.AUTO.value,
            )
        ]
    )
    monkeypatch.setattr(
        "vol_crush.position_manager.service.build_local_store", lambda _: store
    )
    monkeypatch.setattr(
        position_manager_service,
        "_strategy_map",
        lambda: {
            "spy_strangle": Strategy.from_dict(
                {
                    "id": "spy_strangle",
                    "name": "SPY short strangle",
                    "structure": "short_strangle",
                    "management": {
                        "profit_target_pct": 50,
                        "max_loss_multiple": 2.0,
                        "roll_dte_trigger": 21,
                    },
                }
            )
        },
    )

    actions = position_manager_service.evaluate_positions({})
    assert len(actions) == 1
    assert actions[0].action == TradeAction.CLOSE


# ── Optimizer group-aware constraints ───────────────────────────────────────


def _auto_condor(underlying: str) -> Position:
    return Position(
        position_id=f"{underlying}_ic",
        underlying=underlying,
        strategy_id="",
        legs=[
            OptionLeg(underlying, "2026-05-15", 180.0, "put", "buy"),
            OptionLeg(underlying, "2026-05-15", 185.0, "put", "sell"),
            OptionLeg(underlying, "2026-05-15", 205.0, "call", "sell"),
            OptionLeg(underlying, "2026-05-15", 210.0, "call", "buy"),
        ],
        greeks=Greeks(theta=0.10),
        bpr=500.0,
        source=PositionSource.PUBLIC_INFERRED.value,
        strategy_type=StrategyType.IRON_CONDOR.value,
        confidence=GroupConfidence.HIGH.value,
        management_status=ManagementStatus.AUTO.value,
    )


def _orphan_short_call(underlying: str) -> Position:
    return Position(
        position_id=f"{underlying}_orphan",
        underlying=underlying,
        strategy_id="",
        legs=[OptionLeg(underlying, "2026-05-15", 220.0, "call", "sell")],
        greeks=Greeks(delta=-0.15),
        bpr=4400.0,
        source=PositionSource.PUBLIC_INFERRED.value,
        strategy_type=StrategyType.SHORT_CALL.value,
        confidence=GroupConfidence.MEDIUM.value,
        management_status=ManagementStatus.MANUAL_REVIEW_REQUIRED.value,
    )


def test_auto_managed_positions_excludes_manual_review() -> None:
    snap = PortfolioSnapshot(
        net_liquidation_value=100_000.0,
        positions=[
            _auto_condor("AAPL"),
            _orphan_short_call("IWM"),
            _auto_condor("SPY"),
        ],
    )
    auto = _auto_managed_positions(snap)
    assert len(auto) == 2
    assert {p.underlying for p in auto} == {"AAPL", "SPY"}


def test_orphan_leg_count_sees_manual_review_and_orphans() -> None:
    snap = PortfolioSnapshot(
        positions=[_auto_condor("AAPL"), _orphan_short_call("IWM")],
    )
    assert _orphan_leg_count(snap) == 1


def test_optimizer_orphan_leg_constraint_blocks_new_opens() -> None:
    """When max_orphan_legs=0 and the portfolio has an orphan group, constraints must fail."""
    base = PortfolioSnapshot(
        net_liquidation_value=100_000.0,
        positions=[_orphan_short_call("IWM")],
        greeks=Greeks(),
        bpr_used=4400.0,
        bpr_used_pct=4.4,
        theta_as_pct_nlv=0.0,
        gamma_theta_ratio=0.0,
        position_count=0,  # orphan doesn't count as auto-managed
    )
    projected = PortfolioSnapshot.from_dict(base.to_dict())
    projected.position_count = 1
    config = {
        "portfolio": {
            "constraints": {
                "beta_weighted_delta_pct": [-5.0, 5.0],
                "daily_theta_pct": [0.0, 0.5],
                "max_gamma_ratio": 1.5,
                "max_bpr_utilization_pct": 50.0,
                "max_single_underlying_pct": 50.0,
                "max_positions": 15,
                "max_orphan_legs": 0,
            }
        }
    }
    candidate = CandidatePosition(
        idea_id="idea_1",
        strategy_id="spy_put",
        underlying="SPY",
        strategy_type="short_put",
        expiration="2026-05-15",
        estimated_credit=2.0,
        estimated_bpr=1000.0,
        estimated_greeks=Greeks(theta=0.08),
    )

    checks = evaluate_constraints(projected, [candidate], config, base=base)

    orphan_check = next(check for check in checks if check.name == "max_orphan_legs")
    assert not orphan_check.passed
    assert orphan_check.actual == 1.0


def test_single_underlying_constraint_includes_existing_exposure() -> None:
    base = PortfolioSnapshot(
        net_liquidation_value=100_000.0,
        positions=[
            Position(
                position_id="spy_existing",
                underlying="SPY",
                strategy_id="",
                bpr=8000.0,
                management_status=ManagementStatus.AUTO.value,
            ),
            Position(
                position_id="iwm_existing",
                underlying="IWM",
                strategy_id="",
                bpr=1000.0,
                management_status=ManagementStatus.AUTO.value,
            ),
        ],
        greeks=Greeks(),
        bpr_used=9000.0,
        bpr_used_pct=9.0,
        theta_as_pct_nlv=0.0,
        gamma_theta_ratio=0.0,
        position_count=2,
    )
    projected = PortfolioSnapshot.from_dict(base.to_dict())
    projected.bpr_used = 11000.0
    projected.bpr_used_pct = 11.0
    projected.position_count = 3
    config = {
        "portfolio": {
            "constraints": {
                "beta_weighted_delta_pct": [-5.0, 5.0],
                "daily_theta_pct": [0.0, 0.5],
                "max_gamma_ratio": 1.5,
                "max_bpr_utilization_pct": 50.0,
                "max_single_underlying_pct": 85.0,
                "max_positions": 15,
                "max_orphan_legs": 0,
            }
        }
    }
    candidate = CandidatePosition(
        idea_id="idea_1",
        strategy_id="spy_put",
        underlying="SPY",
        strategy_type="short_put",
        expiration="2026-05-15",
        estimated_credit=2.0,
        estimated_bpr=2000.0,
        estimated_greeks=Greeks(theta=0.08),
    )

    checks = evaluate_constraints(projected, [candidate], config, base=base)

    spy_check = next(
        check for check in checks if check.name == "max_single_underlying_pct:SPY"
    )
    assert not spy_check.passed
    assert round(spy_check.actual, 2) == 90.91


def test_optimizer_no_trade_when_orphan_leg_present_and_threshold_zero(
    tmp_path, monkeypatch
) -> None:
    """End-to-end: a fresh idea + orphan position should produce no_trade even when the idea would otherwise pass."""
    store = LocalStore(sqlite_path=tmp_path / "km.db", audit_dir=tmp_path / "audit")
    store.save_positions([_orphan_short_call("IWM")])
    store.save_portfolio_snapshot(
        PortfolioSnapshot(
            timestamp="2026-04-02T14:00:00+00:00",
            net_liquidation_value=100000.0,
            greeks=Greeks(theta=0.0),
            beta_weighted_delta=0.0,
            bpr_used=4400.0,
            bpr_used_pct=4.4,
            theta_as_pct_nlv=0.0,
            gamma_theta_ratio=0.0,
            position_count=0,
            positions=[_orphan_short_call("IWM")],
        )
    )
    store.save_trade_ideas(
        [
            TradeIdea(
                id="idea_spy_put",
                date="2026-04-02",
                trader_name="Tom",
                show_name="Bootstrappers",
                underlying="SPY",
                strategy_type="short_put",
                description="Sell SPY put",
                status=IdeaStatus.NEW.value,
            )
        ]
    )

    # Minimal bundle with one SPY snapshot that'd normally let the candidate through.
    import json

    bundle_path = tmp_path / "fixture_bundle.json"
    bundle_path.write_text(
        json.dumps(
            {
                "market_snapshots": [
                    {
                        "symbol": "SPY",
                        "timestamp": "2026-04-02T14:00:00+00:00",
                        "underlying_price": 520.0,
                        "iv_rank": 25.0,
                        "realized_volatility": 16.0,
                        "beta_to_spy": 1.0,
                        "sector": "broad_market",
                        "event_risk": False,
                        "source": "test",
                        "option_snapshots": [
                            {
                                "underlying": "SPY",
                                "timestamp": "2026-04-02T14:00:00+00:00",
                                "option_type": "put",
                                "strike": 515.0,
                                "expiration": "2026-05-15",
                                "bid": 1.8,
                                "ask": 2.0,
                                "last": 1.9,
                                "greeks": {
                                    "delta": -0.16,
                                    "gamma": 0.025,
                                    "theta": 0.09,
                                    "vega": 0.1,
                                },
                                "implied_volatility": 26.0,
                                "gds_score": 0.08,
                                "source": "test",
                                "quality_flags": [],
                            }
                        ],
                        "notes": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provider = FixtureMarketDataProvider(bundle_path)
    monkeypatch.setattr(
        "vol_crush.optimizer.service.load_strategy_objects",
        lambda: [
            Strategy.from_dict(
                {
                    "id": "spy_put",
                    "name": "SPY Short Put",
                    "structure": "short_put",
                    "filters": {
                        "underlyings": ["SPY"],
                        "dte_range": [30, 45],
                        "delta_range": [0.14, 0.2],
                    },
                    "management": {"profit_target_pct": 50},
                    "allocation": {
                        "max_bpr_pct": 30,
                        "max_per_position_pct": 10,
                        "max_positions": 5,
                    },
                }
            )
        ],
    )

    config = {
        "portfolio": {
            "constraints": {
                "beta_weighted_delta_pct": [-5.0, 5.0],
                "daily_theta_pct": [0.0, 0.5],
                "max_gamma_ratio": 1.5,
                "max_bpr_utilization_pct": 50.0,
                "max_single_underlying_pct": 50.0,
                "max_positions": 15,
                "max_orphan_legs": 0,
            },
            "optimizer_weights": {
                "delta_improvement": 0.25,
                "gamma_profile": 0.20,
                "theta_improvement": 0.35,
                "diversification": 0.20,
            },
            "regimes": {
                "normal_iv": {
                    "prefer_structures": ["short_put"],
                    "avoid_structures": [],
                    "allow_undefined_risk": True,
                    "min_iv_rank": 10,
                    "max_iv_rank": 40,
                    "target_delta_bias": 0.0,
                    "reject_event_risk": True,
                }
            },
        }
    }

    plan = build_trade_plan(store, config, provider)
    assert plan.decision == PlanDecision.NO_TRADE
