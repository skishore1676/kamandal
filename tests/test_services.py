"""Tests for optimizer, executor, position manager, and replay services."""

from datetime import UTC, datetime

from vol_crush.backtester.service import evaluate_strategy
from vol_crush.core.models import (
    CandidatePosition,
    Greeks,
    IdeaStatus,
    MarketRegime,
    PendingOrder,
    PlanDecision,
    PortfolioSnapshot,
    Position,
    ReplayTrade,
    Strategy,
    TradeAction,
    TradeIdea,
    TradePlan,
)
from vol_crush.executor.service import (
    _latest_trade_plan,
    _sheet_plan_approved,
    create_pending_orders,
)
from vol_crush.integrations.fixtures import FixtureMarketDataProvider
from vol_crush.integrations.storage import LocalStore
from vol_crush.optimizer.service import build_trade_plan
from vol_crush.position_manager import service as position_manager_service


def _sample_config(tmp_path):
    return {
        "storage": {
            "local": {
                "sqlite_path": str(tmp_path / "vol_crush.db"),
                "audit_dir": str(tmp_path / "audit"),
            }
        },
        "data_sources": {
            "fixtures": {"bundle_path": str(tmp_path / "fixture_bundle.json")}
        },
        "portfolio": {
            "constraints": {
                "beta_weighted_delta_pct": [-5.0, 5.0],
                "daily_theta_pct": [0.0, 0.5],
                "max_gamma_ratio": 1.5,
                "max_bpr_utilization_pct": 50.0,
                "max_single_underlying_pct": 50.0,
                "max_positions": 15,
            },
            "optimizer_weights": {
                "delta_improvement": 0.25,
                "gamma_profile": 0.20,
                "theta_improvement": 0.35,
                "diversification": 0.20,
            },
            "regimes": {
                "normal_iv": {
                    "prefer_structures": ["short_put", "short_strangle"],
                    "avoid_structures": [],
                    "allow_undefined_risk": True,
                    "min_iv_rank": 10,
                    "max_iv_rank": 40,
                    "target_delta_bias": 0.0,
                    "reject_event_risk": True,
                }
            },
        },
        "backtesting": {
            "approval_thresholds": {
                "min_win_rate": 0.5,
                "max_drawdown_pct": 50.0,
            }
        },
    }


def test_shadow_nlv_override_applies_to_zero_snapshot(tmp_path):
    from vol_crush.optimizer.service import _apply_shadow_nlv_override

    config = _sample_config(tmp_path)
    config["execution"] = {"mode": "shadow", "shadow_net_liquidation_value": 100000.0}
    snapshot = PortfolioSnapshot(
        timestamp="2026-04-02T14:00:00+00:00",
        net_liquidation_value=0.0,
        greeks=Greeks(theta=120.0, gamma=6.0),
        bpr_used=15000.0,
    )

    adjusted = _apply_shadow_nlv_override(snapshot, config)

    assert adjusted.net_liquidation_value == 100000.0
    assert adjusted.bpr_used_pct == 15.0
    assert adjusted.theta_as_pct_nlv == 0.12
    assert adjusted.gamma_theta_ratio == 0.0


def test_shadow_nlv_override_applies_to_small_shadow_snapshot(tmp_path):
    from vol_crush.optimizer.service import _apply_shadow_nlv_override

    config = _sample_config(tmp_path)
    config["execution"] = {"mode": "shadow", "shadow_net_liquidation_value": 100000.0}
    snapshot = PortfolioSnapshot(
        timestamp="2026-04-02T14:00:00+00:00",
        net_liquidation_value=2000.0,
        greeks=Greeks(theta=120.0, gamma=6.0),
        bpr_used=15000.0,
    )

    adjusted = _apply_shadow_nlv_override(snapshot, config)

    assert adjusted.net_liquidation_value == 100000.0
    assert adjusted.bpr_used_pct == 15.0
    assert adjusted.theta_as_pct_nlv == 0.12


def test_resolve_regime_uses_sheet_override(tmp_path):
    from vol_crush.optimizer.service import _resolve_regime

    today = datetime.now(UTC).date().isoformat()
    cache_dir = tmp_path / "sheet_cache"
    cache_dir.mkdir()
    (cache_dir / "regime_control.json").write_text(
        __import__("json").dumps(
            {
                "rows": [
                    {
                        "date": today,
                        "regime": "high_iv",
                        "override_enabled": True,
                        "note": "manual override",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = _sample_config(tmp_path)
    config["google_sheets"] = {"enabled": True, "cache_dir": str(cache_dir)}

    regime, policy = _resolve_regime(config, [])

    assert regime == MarketRegime.HIGH_IV
    assert "short_strangle" in policy.prefer_structures


def test_resolve_regime_falls_back_to_local_evaluator(tmp_path):
    from vol_crush.optimizer.service import _resolve_regime

    config = _sample_config(tmp_path)
    snapshots = [
        __import__("types").SimpleNamespace(iv_rank=40.0, event_risk=False),
        __import__("types").SimpleNamespace(iv_rank=38.0, event_risk=False),
    ]

    regime, policy = _resolve_regime(config, snapshots)

    assert regime == MarketRegime.HIGH_IV
    assert "short_strangle" in policy.prefer_structures


def _sample_bundle(tmp_path):
    payload = {
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
                        "option_type": "call",
                        "strike": 525.0,
                        "expiration": "2026-05-15",
                        "bid": 2.0,
                        "ask": 2.2,
                        "last": 2.1,
                        "greeks": {
                            "delta": 0.18,
                            "gamma": 0.03,
                            "theta": 0.08,
                            "vega": 0.11,
                        },
                        "implied_volatility": 24.0,
                        "gds_score": 0.1,
                        "source": "test",
                        "quality_flags": [],
                    },
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
                    },
                ],
                "notes": [],
            }
        ]
    }
    path = tmp_path / "fixture_bundle.json"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    return path


def test_optimizer_builds_executable_trade_plan(tmp_path, monkeypatch):
    config = _sample_config(tmp_path)
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
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
                description="Short SPY put",
                status=IdeaStatus.NEW.value,
            )
        ]
    )
    store.save_portfolio_snapshot(
        PortfolioSnapshot(
            timestamp="2026-04-02T14:00:00+00:00",
            net_liquidation_value=100000.0,
            greeks=Greeks(delta=0.1, gamma=0.01, theta=30.0, vega=5.0),
            beta_weighted_delta=0.1,
            bpr_used=10000.0,
            bpr_used_pct=10.0,
            theta_as_pct_nlv=0.03,
            gamma_theta_ratio=0.0003,
            position_count=1,
        )
    )
    bundle_path = _sample_bundle(tmp_path)
    provider = FixtureMarketDataProvider(bundle_path)
    strategies = [
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
    ]
    monkeypatch.setattr(
        "vol_crush.optimizer.service.load_strategy_objects", lambda: strategies
    )

    plan = build_trade_plan(store, config, provider)

    assert plan.decision == PlanDecision.EXECUTE
    assert plan.selected_combo_ids == ["idea_spy_put"]
    assert plan.ranked_combos


def test_optimizer_prefers_option_near_target_delta(tmp_path, monkeypatch):
    config = _sample_config(tmp_path)
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
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
                description="Short SPY put",
                status=IdeaStatus.NEW.value,
            )
        ]
    )
    store.save_portfolio_snapshot(
        PortfolioSnapshot(
            timestamp="2026-04-02T14:00:00+00:00",
            net_liquidation_value=100000.0,
            greeks=Greeks(delta=0.1, gamma=0.01, theta=30.0, vega=5.0),
            beta_weighted_delta=0.1,
            bpr_used=10000.0,
            bpr_used_pct=10.0,
            theta_as_pct_nlv=0.03,
            gamma_theta_ratio=0.0003,
            position_count=1,
        )
    )
    payload = __import__("json").loads(_sample_bundle(tmp_path).read_text())
    payload["market_snapshots"][0]["option_snapshots"] = [
        {
            "underlying": "SPY",
            "timestamp": "2026-04-02T14:00:00+00:00",
            "option_type": "put",
            "strike": 505.0,
            "expiration": "2026-05-15",
            "bid": 5.0,
            "ask": 5.2,
            "last": 5.1,
            "greeks": {"delta": -0.35, "gamma": 0.03, "theta": 0.10, "vega": 0.12},
            "implied_volatility": 26.0,
            "gds_score": 0.08,
            "source": "test",
            "quality_flags": [],
        },
        {
            "underlying": "SPY",
            "timestamp": "2026-04-02T14:00:00+00:00",
            "option_type": "put",
            "strike": 500.0,
            "expiration": "2026-05-15",
            "bid": 2.0,
            "ask": 2.2,
            "last": 2.1,
            "greeks": {"delta": -0.16, "gamma": 0.025, "theta": 0.09, "vega": 0.10},
            "implied_volatility": 26.0,
            "gds_score": 0.08,
            "source": "test",
            "quality_flags": [],
        },
        {
            "underlying": "SPY",
            "timestamp": "2026-04-02T14:00:00+00:00",
            "option_type": "call",
            "strike": 540.0,
            "expiration": "2026-05-15",
            "bid": 1.9,
            "ask": 2.1,
            "last": 2.0,
            "greeks": {"delta": 0.16, "gamma": 0.02, "theta": 0.08, "vega": 0.11},
            "implied_volatility": 24.0,
            "gds_score": 0.1,
            "source": "test",
            "quality_flags": [],
        },
    ]
    bundle_path = tmp_path / "delta_target_bundle.json"
    bundle_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    provider = FixtureMarketDataProvider(bundle_path)
    strategies = [
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
    ]
    monkeypatch.setattr(
        "vol_crush.optimizer.service.load_strategy_objects", lambda: strategies
    )

    plan = build_trade_plan(store, config, provider)

    assert plan.decision == PlanDecision.EXECUTE
    assert plan.candidate_positions[0].legs[0].strike == 500.0


def test_optimizer_rejects_expired_option_snapshots(tmp_path, monkeypatch):
    config = _sample_config(tmp_path)
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
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
                description="Short SPY put",
                status=IdeaStatus.NEW.value,
            )
        ]
    )
    store.save_portfolio_snapshot(
        PortfolioSnapshot(
            timestamp="2026-04-02T14:00:00+00:00",
            net_liquidation_value=100000.0,
            greeks=Greeks(delta=0.1, gamma=0.01, theta=30.0, vega=5.0),
            beta_weighted_delta=0.1,
            bpr_used=10000.0,
            bpr_used_pct=10.0,
            theta_as_pct_nlv=0.03,
            gamma_theta_ratio=0.0003,
            position_count=1,
        )
    )
    payload = __import__("json").loads(_sample_bundle(tmp_path).read_text())
    payload["market_snapshots"][0]["option_snapshots"][0]["expiration"] = "2020-01-17"
    payload["market_snapshots"][0]["option_snapshots"][1]["expiration"] = "2020-01-17"
    bundle_path = tmp_path / "expired_fixture_bundle.json"
    bundle_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    provider = FixtureMarketDataProvider(bundle_path)
    strategies = [
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
    ]
    monkeypatch.setattr(
        "vol_crush.optimizer.service.load_strategy_objects", lambda: strategies
    )

    plan = build_trade_plan(store, config, provider)

    assert plan.decision == PlanDecision.NO_TRADE
    assert "no active option snapshots available" in " ".join(plan.risk_flags)


def test_optimizer_returns_no_trade_without_matching_strategy(tmp_path, monkeypatch):
    config = _sample_config(tmp_path)
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
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
                description="Short SPY put",
                status=IdeaStatus.NEW.value,
            )
        ]
    )
    bundle_path = _sample_bundle(tmp_path)
    provider = FixtureMarketDataProvider(bundle_path)
    monkeypatch.setattr("vol_crush.optimizer.service.load_strategy_objects", lambda: [])

    plan = build_trade_plan(store, config, provider)

    assert plan.decision == PlanDecision.NO_TRADE


def test_optimizer_returns_no_trade_without_ideas(tmp_path, monkeypatch):
    config = _sample_config(tmp_path)
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
    )
    provider = FixtureMarketDataProvider(_sample_bundle(tmp_path))
    monkeypatch.setattr("vol_crush.optimizer.service.load_strategy_objects", lambda: [])

    plan = build_trade_plan(store, config, provider)

    assert plan.decision == PlanDecision.NO_TRADE
    assert "No ideas passed validation" in plan.reasoning


def test_execution_mode_pending_normalizes_to_shadow():
    """The deprecated 'pending' value must be treated identically to 'shadow'.

    The optimizer's live-mode gate only triggers when mode == 'live'. Both
    'pending' and 'shadow' should fall through to the permissive (non-live)
    branch with no behavioral difference.
    """
    from vol_crush.optimizer.service import _execution_mode

    assert _execution_mode({"execution": {"mode": "pending"}}) == "shadow"
    assert _execution_mode({"execution": {"mode": "shadow"}}) == "shadow"
    assert _execution_mode({"execution": {"mode": "PENDING"}}) == "shadow"
    assert _execution_mode({"execution": {"mode": "live"}}) == "live"
    assert _execution_mode({"execution": {"mode": "dry_run"}}) == "dry_run"
    assert _execution_mode({}) == ""


def test_optimizer_blocks_unapproved_strategies_in_live_mode(tmp_path, monkeypatch):
    config = _sample_config(tmp_path)
    config["execution"] = {"mode": "live"}
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
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
                description="Short SPY put",
                status=IdeaStatus.NEW.value,
            )
        ]
    )
    provider = FixtureMarketDataProvider(_sample_bundle(tmp_path))
    monkeypatch.setattr(
        "vol_crush.optimizer.service.load_strategy_objects",
        lambda: [
            Strategy.from_dict(
                {
                    "id": "spy_put",
                    "name": "SPY Short Put",
                    "structure": "short_put",
                    "filters": {"underlyings": ["SPY"]},
                    "backtest_approved": False,
                    "dry_run_passed": False,
                }
            )
        ],
    )

    plan = build_trade_plan(store, config, provider)

    assert plan.decision == PlanDecision.NO_TRADE
    assert any("blocked in live mode" in flag for flag in plan.risk_flags)


def test_optimizer_rejects_event_risk_candidate(tmp_path, monkeypatch):
    config = _sample_config(tmp_path)
    config["portfolio"]["regimes"]["event_risk"] = {
        "prefer_structures": [],
        "avoid_structures": ["short_put"],
        "allow_undefined_risk": False,
        "target_delta_bias": 0.0,
        "reject_event_risk": True,
    }
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
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
                description="Short SPY put",
                status=IdeaStatus.NEW.value,
            )
        ]
    )
    bundle_path = _sample_bundle(tmp_path)
    payload = __import__("json").loads(bundle_path.read_text(encoding="utf-8"))
    payload["market_snapshots"][0]["event_risk"] = True
    bundle_path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    provider = FixtureMarketDataProvider(bundle_path)
    monkeypatch.setattr(
        "vol_crush.optimizer.service.load_strategy_objects",
        lambda: [
            Strategy.from_dict(
                {"id": "spy_put", "name": "SPY Short Put", "structure": "short_put"}
            )
        ],
    )

    plan = build_trade_plan(store, config, provider)

    assert plan.decision == PlanDecision.NO_TRADE
    assert any("event risk" in flag.lower() for flag in plan.risk_flags)


def test_pending_executor_sizes_order(tmp_path):
    plan = TradePlan(
        plan_id="plan_1",
        created_at="2026-04-02T14:00:00+00:00",
        decision=PlanDecision.EXECUTE,
        regime=MarketRegime.NORMAL_IV.value,
        candidate_positions=[
            CandidatePosition(
                idea_id="idea_1",
                strategy_id="spy_put",
                underlying="SPY",
                strategy_type="short_put",
                expiration="2026-05-15",
                estimated_credit=1.95,
                estimated_bpr=850.0,
                estimated_greeks=Greeks(delta=-0.16, gamma=0.025, theta=0.09, vega=0.1),
            )
        ],
    )
    snapshot = PortfolioSnapshot(
        timestamp="2026-04-02T14:00:00+00:00",
        net_liquidation_value=100000.0,
        bpr_used=10000.0,
        bpr_used_pct=10.0,
    )
    config = _sample_config(tmp_path)

    orders = create_pending_orders(plan, snapshot, config)

    assert len(orders) == 1
    assert isinstance(orders[0], PendingOrder)
    assert orders[0].action == TradeAction.OPEN
    assert orders[0].idea_id == "idea_1"
    assert orders[0].estimated_bpr >= 850.0


def test_pending_executor_rechecks_constraints_after_sizing(tmp_path):
    plan = TradePlan(
        plan_id="plan_1",
        created_at="2026-04-02T14:00:00+00:00",
        decision=PlanDecision.EXECUTE,
        regime=MarketRegime.NORMAL_IV.value,
        candidate_positions=[
            CandidatePosition(
                idea_id="idea_1",
                strategy_id="spy_put",
                underlying="SPY",
                strategy_type="short_put",
                expiration="2026-05-15",
                estimated_credit=1.0,
                estimated_bpr=1000.0,
                estimated_greeks=Greeks(delta=2.0, gamma=0.0, theta=0.0, vega=0.0),
            )
        ],
    )
    snapshot = PortfolioSnapshot(
        timestamp="2026-04-02T14:00:00+00:00",
        net_liquidation_value=100000.0,
        bpr_used=10000.0,
        bpr_used_pct=10.0,
    )
    config = _sample_config(tmp_path)
    config["execution"] = {"mode": "pending", "max_contracts_per_order": 10}
    config["portfolio"]["constraints"]["max_single_underlying_pct"] = 100.0

    orders = create_pending_orders(plan, snapshot, config)

    assert len(orders) == 1
    assert orders[0].quantity == 2
    assert orders[0].estimated_bpr == 2000.0
    assert orders[0].greeks_impact.delta == 4.0


def test_pending_executor_caps_live_quantity_to_one_by_default(tmp_path):
    plan = TradePlan(
        plan_id="plan_1",
        created_at="2026-04-02T14:00:00+00:00",
        decision=PlanDecision.EXECUTE,
        regime=MarketRegime.NORMAL_IV.value,
        candidate_positions=[
            CandidatePosition(
                idea_id="idea_1",
                strategy_id="spy_put",
                underlying="SPY",
                strategy_type="short_put",
                expiration="2026-05-15",
                estimated_credit=1.95,
                estimated_bpr=850.0,
                estimated_greeks=Greeks(delta=-0.16, gamma=0.025, theta=0.09, vega=0.1),
            )
        ],
    )
    snapshot = PortfolioSnapshot(
        timestamp="2026-04-02T14:00:00+00:00",
        net_liquidation_value=100000.0,
        bpr_used=10000.0,
        bpr_used_pct=10.0,
    )
    config = _sample_config(tmp_path)
    config["execution"] = {"mode": "live"}

    orders = create_pending_orders(plan, snapshot, config)

    assert orders[0].quantity == 1
    assert orders[0].estimated_bpr == 850.0


def test_latest_trade_plan_uses_created_at_not_random_id() -> None:
    old_plan = TradePlan(
        plan_id="plan_zzzz",
        created_at="2026-04-02T14:00:00+00:00",
        decision=PlanDecision.EXECUTE,
        regime=MarketRegime.NORMAL_IV.value,
    )
    new_plan = TradePlan(
        plan_id="plan_aaaa",
        created_at="2026-04-03T14:00:00+00:00",
        decision=PlanDecision.NO_TRADE,
        regime=MarketRegime.NORMAL_IV.value,
    )

    assert _latest_trade_plan([new_plan, old_plan]) is new_plan


def test_sheet_plan_approval_requires_all_plan_rows(tmp_path) -> None:
    config = _sample_config(tmp_path)
    cache_dir = tmp_path / "sheet_cache"
    cache_dir.mkdir()
    config["google_sheets"] = {"enabled": True, "cache_dir": str(cache_dir)}
    plan = TradePlan(
        plan_id="plan_1",
        created_at="2026-04-02T14:00:00+00:00",
        decision=PlanDecision.EXECUTE,
        regime=MarketRegime.NORMAL_IV.value,
    )

    approved, note = _sheet_plan_approved(config, plan)
    assert approved is False
    assert "no daily_plan" in note

    (cache_dir / "daily_plan.json").write_text(
        __import__("json").dumps(
            {
                "rows": [
                    {"plan_id": "plan_1", "approval": "approve"},
                    {"plan_id": "plan_1", "approval": ""},
                ]
            }
        ),
        encoding="utf-8",
    )
    approved, note = _sheet_plan_approved(config, plan)
    assert approved is False
    assert "not approved" in note

    (cache_dir / "daily_plan.json").write_text(
        __import__("json").dumps(
            {"rows": [{"plan_id": "plan_1", "approval": "approve"}]}
        ),
        encoding="utf-8",
    )
    approved, note = _sheet_plan_approved(config, plan)
    assert approved is True
    assert "approved" in note


def test_sheet_plan_approval_can_be_bypassed(tmp_path) -> None:
    config = _sample_config(tmp_path)
    config["google_sheets"] = {
        "enabled": True,
        "cache_dir": str(tmp_path / "missing_cache"),
    }
    config["execution"] = {"bypass_daily_plan_approval": True}
    plan = TradePlan(
        plan_id="plan_1",
        created_at="2026-04-02T14:00:00+00:00",
        decision=PlanDecision.EXECUTE,
        regime=MarketRegime.NORMAL_IV.value,
    )

    approved, note = _sheet_plan_approved(config, plan)

    assert approved is True
    assert "bypass enabled" in note


def test_position_manager_emits_close_signal(tmp_path, monkeypatch):
    config = _sample_config(tmp_path)
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
    )
    store.save_positions(
        [
            Position(
                position_id="pos_1",
                underlying="SPY",
                strategy_id="spy_put",
                open_credit=2.0,
                current_value=0.8,
                greeks=Greeks(delta=-0.15, gamma=0.02, theta=0.1, vega=0.1),
                dte_remaining=25,
                pnl_pct=60.0,
                bpr=900.0,
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
            "spy_put": Strategy.from_dict(
                {
                    "id": "spy_put",
                    "name": "SPY Short Put",
                    "structure": "short_put",
                    "management": {
                        "profit_target_pct": 50,
                        "max_loss_multiple": 2.0,
                        "roll_dte_trigger": 21,
                    },
                }
            )
        },
    )

    actions = position_manager_service.evaluate_positions(config)

    assert len(actions) == 1
    assert actions[0].action == TradeAction.CLOSE


def test_backtester_evaluates_strategy_gate(tmp_path):
    config = _sample_config(tmp_path)
    strategy = Strategy.from_dict(
        {
            "id": "spy_put",
            "name": "SPY Short Put",
            "structure": "short_put",
            "filters": {"underlyings": ["SPY"]},
        }
    )
    trades = [
        ReplayTrade(
            "t1",
            "SPY",
            "SPY2501P",
            12.0,
            True,
            theta_capture_proxy=3.0,
            days_in_trade=18.0,
        ),
        ReplayTrade(
            "t2",
            "SPY",
            "SPY2502P",
            -4.0,
            False,
            theta_capture_proxy=-1.0,
            days_in_trade=22.0,
        ),
        ReplayTrade(
            "t3",
            "SPY",
            "SPY2503P",
            8.0,
            True,
            theta_capture_proxy=2.0,
            days_in_trade=24.0,
        ),
    ]

    result = evaluate_strategy(strategy, trades, config)

    assert result.total_trades == 3
    assert result.winning_trades == 2
    assert result.approved is True
