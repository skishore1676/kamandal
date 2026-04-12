"""Deterministic portfolio optimizer for Vol Crush dry-run workflows."""

from __future__ import annotations

import argparse
import itertools
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from vol_crush.core.config import (
    load_config,
    load_strategies,
    load_strategy_templates,
    load_underlying_profiles,
)
from vol_crush.core.interfaces import RegimeEvaluator, StorageBackend
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import (
    CandidatePosition,
    ComboScore,
    ConstraintCheck,
    Greeks,
    IdeaStatus,
    ManagementStatus,
    MarketRegime,
    MarketSnapshot,
    OptionLeg,
    PlanDecision,
    PortfolioSnapshot,
    Position,
    RegimePolicy,
    Strategy,
    StrategyTemplate,
    StrategyType,
    TradeIdea,
    TradePlan,
    UnderlyingProfile,
    resolve_all_strategies,
)
from vol_crush.integrations.fixtures import FixtureMarketDataProvider
from vol_crush.integrations.storage import build_local_store

logger = logging.getLogger("vol_crush.optimizer")


def load_strategy_objects(config_path: str | Path | None = None) -> list[Strategy]:
    """Resolve strategy templates + underlying profiles into runtime Strategy objects.

    Falls back to the legacy strategies.yaml if the new config files don't exist or
    produce no results (backward compatibility for tests and existing deployments).
    """
    templates = [StrategyTemplate.from_dict(d) for d in load_strategy_templates() if d]
    profiles = [UnderlyingProfile.from_dict(d) for d in load_underlying_profiles() if d]
    resolved = resolve_all_strategies(templates, profiles)
    if resolved:
        return resolved
    return [Strategy.from_dict(item) for item in load_strategies() if item]


class ConfigRegimeEvaluator(RegimeEvaluator):
    """Simple regime selection from current fixture state."""

    def __init__(self, config: dict):
        self.config = config
        regime_cfg = config.get("portfolio", {}).get("regimes", {})
        self.policies = {}
        for key, value in regime_cfg.items():
            payload = dict(value)
            payload["regime"] = key
            self.policies[key] = RegimePolicy.from_dict(payload)

    def determine_regime(self, snapshots: list[MarketSnapshot]) -> MarketRegime:
        if not snapshots:
            return MarketRegime.UNKNOWN
        if any(snapshot.event_risk for snapshot in snapshots):
            return MarketRegime.EVENT_RISK
        avg_iv_rank = sum(snapshot.iv_rank for snapshot in snapshots) / len(snapshots)
        if avg_iv_rank >= 35:
            return MarketRegime.HIGH_IV
        if avg_iv_rank <= 17:
            return MarketRegime.LOW_IV
        return MarketRegime.NORMAL_IV

    def get_policy(self, regime: MarketRegime) -> RegimePolicy:
        key = regime.value if isinstance(regime, MarketRegime) else str(regime)
        if key in self.policies:
            return self.policies[key]
        unknown = self.policies.get(MarketRegime.NORMAL_IV.value)
        if unknown:
            return unknown
        return RegimePolicy(regime=MarketRegime.UNKNOWN)


def _strategy_lookup(strategies: list[Strategy]) -> dict[str, list[Strategy]]:
    """Build a structure → list[Strategy] index so multiple resolved strategies
    (e.g. put_spread on SPY via index_etf AND put_spread on TLT via bond_etf)
    coexist without overwriting each other."""
    result: dict[str, list[Strategy]] = {}
    for strategy in strategies:
        result.setdefault(strategy.structure.value, []).append(strategy)
    return result


def _find_strategy_for_idea(
    idea: TradeIdea,
    strategy_map: dict[str, list[Strategy]],
    policy: RegimePolicy,
) -> Strategy | None:
    """Find the first matching resolved Strategy for a given idea.

    Matches on structure (idea.strategy_type) AND underlying (idea.underlying must
    be in the strategy's underlyings list, or the list is empty = any).
    """
    candidates = strategy_map.get(idea.strategy_type, [])
    for strategy in candidates:
        if (
            strategy.filters.underlyings
            and idea.underlying not in strategy.filters.underlyings
        ):
            continue
        return strategy
    return None


def _pick_option(snapshot: MarketSnapshot, option_type: str):
    for item in snapshot.option_snapshots:
        if item.option_type == option_type:
            return item
    return None


def _default_expiration(snapshot: MarketSnapshot) -> str:
    if snapshot.option_snapshots:
        return snapshot.option_snapshots[0].expiration
    return datetime.now(timezone.utc).date().isoformat()


def _approximate_candidate(
    idea: TradeIdea, strategy: Strategy, snapshot: MarketSnapshot
) -> CandidatePosition:
    call = _pick_option(snapshot, "call")
    put = _pick_option(snapshot, "put")
    strategy_type = strategy.structure.value
    credit = idea.credit_target
    bpr = snapshot.underlying_price * 100 * 0.18
    greeks = Greeks()
    legs: list[OptionLeg] = []

    if strategy_type == StrategyType.SHORT_PUT.value and put:
        credit = credit or put.mid
        greeks = replace(put.greeks)
        bpr = max(snapshot.underlying_price * 100 * 0.12, credit * 100 * 5)
        legs = [
            OptionLeg(
                underlying=idea.underlying,
                expiration=idea.expiration or put.expiration,
                strike=put.strike,
                option_type="put",
                side="sell",
            )
        ]
    elif strategy_type == StrategyType.SHORT_STRANGLE.value and call and put:
        credit = credit or (call.mid + put.mid)
        greeks = Greeks(
            delta=call.greeks.delta + put.greeks.delta,
            gamma=call.greeks.gamma + put.greeks.gamma,
            theta=call.greeks.theta + put.greeks.theta,
            vega=call.greeks.vega + put.greeks.vega,
        )
        bpr = max(snapshot.underlying_price * 100 * 0.2, credit * 100 * 6)
        legs = [
            OptionLeg(
                idea.underlying,
                idea.expiration or put.expiration,
                put.strike,
                "put",
                "sell",
            ),
            OptionLeg(
                idea.underlying,
                idea.expiration or call.expiration,
                call.strike,
                "call",
                "sell",
            ),
        ]
    elif strategy_type in (
        StrategyType.PUT_SPREAD.value,
        StrategyType.CALL_SPREAD.value,
        StrategyType.IRON_CONDOR.value,
    ):
        base = (
            put
            if "put" in strategy_type or strategy_type == StrategyType.IRON_CONDOR.value
            else call
        )
        other_type = "call" if strategy_type == StrategyType.IRON_CONDOR.value else None
        credit = credit or (base.mid * 0.5 if base else 1.0)
        source_greeks = (
            base.greeks
            if base
            else Greeks(delta=-0.18, gamma=0.04, theta=0.08, vega=0.05)
        )
        greeks = source_greeks * 0.6
        bpr = max((strategy.filters.spread_width or 5.0) * 100, credit * 100 * 2)
        if base:
            width = strategy.filters.spread_width or 5.0
            buy_strike = (
                base.strike - width
                if base.option_type == "put"
                else base.strike + width
            )
            legs = [
                OptionLeg(
                    idea.underlying,
                    idea.expiration or base.expiration,
                    base.strike,
                    base.option_type,
                    "sell",
                ),
                OptionLeg(
                    idea.underlying,
                    idea.expiration or base.expiration,
                    buy_strike,
                    base.option_type,
                    "buy",
                ),
            ]
            if other_type and call:
                width = strategy.filters.spread_width or 5.0
                legs.extend(
                    [
                        OptionLeg(
                            idea.underlying,
                            idea.expiration or call.expiration,
                            call.strike,
                            "call",
                            "sell",
                        ),
                        OptionLeg(
                            idea.underlying,
                            idea.expiration or call.expiration,
                            call.strike + width,
                            "call",
                            "buy",
                        ),
                    ]
                )
    else:
        credit = credit or 1.0
        greeks = Greeks(delta=0.0, gamma=0.05, theta=0.06, vega=0.04)
        bpr = max(snapshot.underlying_price * 100 * 0.1, 250.0)

    return CandidatePosition(
        idea_id=idea.id,
        strategy_id=strategy.id,
        underlying=idea.underlying,
        strategy_type=strategy_type,
        expiration=idea.expiration or _default_expiration(snapshot),
        estimated_credit=round(credit, 4),
        estimated_bpr=round(bpr, 2),
        estimated_greeks=greeks,
        iv_rank=snapshot.iv_rank,
        sector=snapshot.sector,
        event_risk=snapshot.event_risk,
        rationale=idea.rationale or idea.description,
        legs=legs,
    )


def validate_trade_ideas(
    ideas: list[TradeIdea],
    strategies: list[Strategy],
    provider: FixtureMarketDataProvider,
    policy: RegimePolicy,
    regime: MarketRegime | None = None,
) -> tuple[list[CandidatePosition], list[str]]:
    """Validate ideas against resolved strategies, regime, and fixture data.

    Checks enforced (in order):
        1. Structure + underlying match via resolved strategy lookup.
        2. Template-level regime gate: current regime must be in strategy.allowed_regimes.
        3. Template-level earnings gate: strategy.avoid_earnings + snapshot.event_risk.
        4. Template-level IV rank bounds: strategy.filters.iv_rank_min/max.
        5. Regime policy: reject_event_risk, min/max_iv_rank, avoid_structures.
        6. Fixture availability.
    """
    notes: list[str] = []
    strategy_map = _strategy_lookup(strategies)
    candidates: list[CandidatePosition] = []
    current_regime = regime.value if regime is not None else None

    for idea in ideas:
        strategy = _find_strategy_for_idea(idea, strategy_map, policy)
        if strategy is None:
            notes.append(
                f"{idea.id}: no matching template+profile for "
                f"{idea.strategy_type} on {idea.underlying}"
            )
            continue

        # Template-level regime gate
        if (
            strategy.allowed_regimes
            and current_regime
            and current_regime not in strategy.allowed_regimes
        ):
            notes.append(
                f"{idea.id}: {strategy.id} not eligible in {current_regime} regime "
                f"(allowed: {strategy.allowed_regimes})"
            )
            continue

        snapshot = provider.get_market_snapshot(idea.underlying)
        if snapshot is None:
            notes.append(f"{idea.id}: missing fixture data for {idea.underlying}")
            continue

        # Template-level earnings gate
        if strategy.avoid_earnings and snapshot.event_risk:
            notes.append(
                f"{idea.id}: {strategy.id} avoids earnings/event risk on {idea.underlying}"
            )
            continue

        # Template-level IV rank bounds (strategy filters, not just regime policy)
        if (
            strategy.filters.iv_rank_min is not None
            and snapshot.iv_rank < strategy.filters.iv_rank_min
        ):
            notes.append(
                f"{idea.id}: IV rank {snapshot.iv_rank} below strategy minimum "
                f"{strategy.filters.iv_rank_min} for {strategy.id}"
            )
            continue
        if (
            strategy.filters.iv_rank_max is not None
            and snapshot.iv_rank > strategy.filters.iv_rank_max
        ):
            notes.append(
                f"{idea.id}: IV rank {snapshot.iv_rank} above strategy maximum "
                f"{strategy.filters.iv_rank_max} for {strategy.id}"
            )
            continue

        # Regime policy checks (broader market-level gates)
        if policy.reject_event_risk and snapshot.event_risk:
            notes.append(f"{idea.id}: rejected for event risk on {idea.underlying}")
            continue
        if policy.min_iv_rank is not None and snapshot.iv_rank < policy.min_iv_rank:
            notes.append(f"{idea.id}: IV rank {snapshot.iv_rank} below regime floor")
            continue
        if policy.max_iv_rank is not None and snapshot.iv_rank > policy.max_iv_rank:
            notes.append(f"{idea.id}: IV rank {snapshot.iv_rank} above regime ceiling")
            continue
        if strategy.structure.value in policy.avoid_structures:
            notes.append(
                f"{idea.id}: {strategy.structure.value} down-ranked by regime policy"
            )
        candidates.append(_approximate_candidate(idea, strategy, snapshot))

    return candidates, notes


def build_portfolio_snapshot(store: StorageBackend) -> PortfolioSnapshot:
    snapshot = store.get_latest_portfolio_snapshot()
    if snapshot is not None:
        return snapshot
    positions = store.list_positions(status=None)
    greeks = Greeks()
    bpr_used = 0.0
    for position in positions:
        greeks = greeks + position.greeks
        bpr_used += position.bpr
    nlv = 100000.0
    theta_pct = (greeks.theta * 100.0 / nlv) if nlv else 0.0
    ratio = abs(greeks.gamma / greeks.theta) if greeks.theta else 0.0
    snapshot = PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc).isoformat(),
        net_liquidation_value=nlv,
        greeks=greeks,
        beta_weighted_delta=greeks.delta,
        bpr_used=bpr_used,
        bpr_used_pct=(bpr_used / nlv) * 100.0 if nlv else 0.0,
        theta_as_pct_nlv=theta_pct,
        gamma_theta_ratio=ratio,
        position_count=len(positions),
        positions=positions,
    )
    store.save_portfolio_snapshot(snapshot)
    return snapshot


def _auto_managed_positions(snapshot: PortfolioSnapshot) -> list[Position]:
    """Return positions the optimizer is allowed to count for diversification.

    Groups flagged for manual review (inferred short calls, unknown_complex,
    orphan_leg, etc.) still contribute Greeks and BPR to the aggregate — we see
    the real exposure — but they do NOT count toward position_count or
    diversification bonuses, because we cannot reason about their management.
    """
    return [
        p
        for p in snapshot.positions
        if p.management_status == ManagementStatus.AUTO.value
    ]


def _orphan_leg_count(snapshot: PortfolioSnapshot) -> int:
    return sum(
        1
        for p in snapshot.positions
        if p.strategy_type
        in (StrategyType.ORPHAN_LEG.value, StrategyType.UNKNOWN_COMPLEX.value)
        or p.management_status == ManagementStatus.MANUAL_REVIEW_REQUIRED.value
    )


def _project_portfolio(
    base: PortfolioSnapshot, candidates: list[CandidatePosition]
) -> PortfolioSnapshot:
    projected = PortfolioSnapshot.from_dict(base.to_dict())
    total_bpr = base.bpr_used
    greeks = Greeks.from_dict(base.greeks.to_dict())
    for candidate in candidates:
        greeks = greeks + candidate.estimated_greeks
        total_bpr += candidate.estimated_bpr
    projected.timestamp = datetime.now(timezone.utc).isoformat()
    projected.greeks = greeks
    projected.beta_weighted_delta = greeks.delta
    projected.bpr_used = total_bpr
    projected.bpr_used_pct = (
        (total_bpr / projected.net_liquidation_value) * 100.0
        if projected.net_liquidation_value
        else 0.0
    )
    projected.theta_as_pct_nlv = (
        (greeks.theta * 100.0 / projected.net_liquidation_value)
        if projected.net_liquidation_value
        else 0.0
    )
    projected.gamma_theta_ratio = (
        abs(greeks.gamma / greeks.theta) if greeks.theta else 0.0
    )
    # position_count is the number of *auto-managed* groups plus the new candidates.
    # Manual-review groups still live in the portfolio (so their Greeks and BPR
    # appear in the aggregate), but they don't count toward max_positions.
    auto_baseline = len(_auto_managed_positions(base))
    projected.position_count = auto_baseline + len(candidates)
    return projected


def evaluate_constraints(
    projected: PortfolioSnapshot,
    candidates: list[CandidatePosition],
    config: dict,
    base: PortfolioSnapshot | None = None,
) -> list[ConstraintCheck]:
    constraints = config.get("portfolio", {}).get("constraints", {})
    max_orphan_legs = int(constraints.get("max_orphan_legs", 0))
    orphan_count = _orphan_leg_count(base) if base is not None else 0
    checks = [
        ConstraintCheck(
            name="max_orphan_legs",
            passed=orphan_count <= max_orphan_legs,
            actual=float(orphan_count),
            max_value=float(max_orphan_legs),
            message=(
                "Portfolio contains ungrouped short / unknown-complex legs above "
                "the configured threshold. Refuse new opens until they are classified "
                "or manually resolved."
            ),
        ),
        ConstraintCheck(
            name="beta_weighted_delta_pct",
            passed=constraints["beta_weighted_delta_pct"][0]
            <= projected.beta_weighted_delta
            <= constraints["beta_weighted_delta_pct"][1],
            actual=projected.beta_weighted_delta,
            min_value=constraints["beta_weighted_delta_pct"][0],
            max_value=constraints["beta_weighted_delta_pct"][1],
            message="Portfolio delta must stay in configured beta-weighted bounds.",
        ),
        ConstraintCheck(
            name="daily_theta_pct",
            passed=constraints["daily_theta_pct"][0]
            <= projected.theta_as_pct_nlv
            <= constraints["daily_theta_pct"][1],
            actual=projected.theta_as_pct_nlv,
            min_value=constraints["daily_theta_pct"][0],
            max_value=constraints["daily_theta_pct"][1],
            message="Daily theta as a percent of NLV must stay within target range.",
        ),
        ConstraintCheck(
            name="max_gamma_ratio",
            passed=projected.gamma_theta_ratio <= constraints["max_gamma_ratio"],
            actual=projected.gamma_theta_ratio,
            max_value=constraints["max_gamma_ratio"],
            message="Gamma/theta ratio exceeded.",
        ),
        ConstraintCheck(
            name="max_bpr_utilization_pct",
            passed=projected.bpr_used_pct <= constraints["max_bpr_utilization_pct"],
            actual=projected.bpr_used_pct,
            max_value=constraints["max_bpr_utilization_pct"],
            message="BPR utilization exceeded target cap.",
        ),
        ConstraintCheck(
            name="max_positions",
            passed=projected.position_count <= constraints["max_positions"],
            actual=float(projected.position_count),
            max_value=float(constraints["max_positions"]),
            message="Position count exceeded configured maximum.",
        ),
    ]
    by_underlying = {}
    for candidate in candidates:
        by_underlying[candidate.underlying] = (
            by_underlying.get(candidate.underlying, 0.0) + candidate.estimated_bpr
        )
    for underlying, bpr in by_underlying.items():
        pct = (bpr / projected.bpr_used) * 100.0 if projected.bpr_used else 0.0
        checks.append(
            ConstraintCheck(
                name=f"max_single_underlying_pct:{underlying}",
                passed=pct <= constraints["max_single_underlying_pct"],
                actual=pct,
                max_value=constraints["max_single_underlying_pct"],
                message=f"{underlying} concentration exceeds per-underlying cap.",
            )
        )
    return checks


def _score_combo(
    base: PortfolioSnapshot,
    projected: PortfolioSnapshot,
    candidates: list[CandidatePosition],
    checks: list[ConstraintCheck],
    config: dict,
    policy: RegimePolicy,
) -> ComboScore:
    weights = config.get("portfolio", {}).get("optimizer_weights", {})
    target_delta = policy.target_delta_bias
    before_distance = abs(base.beta_weighted_delta - target_delta)
    after_distance = abs(projected.beta_weighted_delta - target_delta)
    delta_score = max(before_distance - after_distance, 0.0)
    gamma_score = max(1.5 - projected.gamma_theta_ratio, 0.0)
    theta_score = max(projected.theta_as_pct_nlv - base.theta_as_pct_nlv, 0.0)
    unique_underlyings = len({candidate.underlying for candidate in candidates})
    unique_sectors = len({candidate.sector for candidate in candidates})
    diversification_score = unique_underlyings + (0.5 * unique_sectors)
    preferred_hits = sum(
        1
        for candidate in candidates
        if candidate.strategy_type in policy.prefer_structures
    )
    avoided_hits = sum(
        1
        for candidate in candidates
        if candidate.strategy_type in policy.avoid_structures
    )
    regime_fit = (
        preferred_hits
        - avoided_hits
        - sum(1 for candidate in candidates if candidate.event_risk)
    )
    total_score = (
        delta_score * weights.get("delta_improvement", 0.25)
        + gamma_score * weights.get("gamma_profile", 0.20)
        + theta_score * weights.get("theta_improvement", 0.35)
        + diversification_score * weights.get("diversification", 0.20)
        + regime_fit * 0.10
    )
    notes = []
    if avoided_hits:
        notes.append(
            "One or more structures were down-ranked by the current regime policy."
        )
    if not all(check.passed for check in checks):
        notes.append("Constraint failures prevent this combo from being tradable.")
    return ComboScore(
        combo_ids=[candidate.idea_id for candidate in candidates],
        candidate_positions=candidates,
        total_score=round(total_score, 4),
        component_scores={
            "delta_improvement": round(delta_score, 4),
            "gamma_profile": round(gamma_score, 4),
            "theta_improvement": round(theta_score, 4),
            "diversification": round(diversification_score, 4),
            "regime_fit": round(regime_fit, 4),
        },
        projected_portfolio=projected,
        constraint_checks=checks,
        regime=policy.regime.value,
        notes=notes,
    )


def rank_candidate_combos(
    base_snapshot: PortfolioSnapshot,
    candidates: list[CandidatePosition],
    config: dict,
    policy: RegimePolicy,
) -> list[ComboScore]:
    combos: list[ComboScore] = []
    for size in (1, 2, 3):
        if len(candidates) < size:
            break
        for group in itertools.combinations(candidates, min(size, len(candidates))):
            projected = _project_portfolio(base_snapshot, list(group))
            checks = evaluate_constraints(
                projected, list(group), config, base=base_snapshot
            )
            combos.append(
                _score_combo(
                    base_snapshot, projected, list(group), checks, config, policy
                )
            )
    combos.sort(key=lambda item: item.total_score, reverse=True)
    return combos


def _resolve_regime(config: dict, snapshots: list) -> tuple[MarketRegime, RegimePolicy]:
    """Determine the current market regime and its policy.

    Tries the trade_lab_bridge sheet first (live mala_v1 signal), falls back
    to the config-driven evaluator (fixture IV rank) if the bridge is
    unconfigured, unreachable, or stale.
    """
    from vol_crush.integrations.regime_bridge import (
        BridgeRegimeEvaluator,
        fetch_regime_from_sheet,
    )

    bridge_cfg = config.get("regime_bridge", {})
    creds = bridge_cfg.get("credentials_path", "")
    sheet_id = bridge_cfg.get("sheet_id", "")
    sheet_name = bridge_cfg.get("sheet_name", "trade_lab_bridge")

    snapshot = None
    if creds and sheet_id:
        snapshot = fetch_regime_from_sheet(creds, sheet_id, sheet_name)

    if snapshot is not None:
        evaluator = BridgeRegimeEvaluator(config, snapshot=snapshot)
        regime = evaluator.determine_regime()
        policy = evaluator.get_policy(regime)
        logger.info("Using regime from trade_lab_bridge: %s", snapshot.summary())
        return regime, policy

    logger.info("Regime bridge unavailable; falling back to config-driven evaluator")
    evaluator = ConfigRegimeEvaluator(config)
    regime = evaluator.determine_regime(snapshots)
    policy = evaluator.get_policy(regime)
    return regime, policy


def build_trade_plan(
    store: StorageBackend,
    config: dict,
    provider: FixtureMarketDataProvider,
) -> TradePlan:
    strategies = load_strategy_objects()
    ideas = [
        idea
        for idea in store.list_trade_ideas()
        if idea.status in (IdeaStatus.NEW.value, IdeaStatus.APPROVED.value)
    ]
    snapshots = provider.list_market_snapshots()
    regime, policy = _resolve_regime(config, snapshots)
    candidates, notes = validate_trade_ideas(
        ideas, strategies, provider, policy, regime=regime
    )
    base_snapshot = build_portfolio_snapshot(store)

    plan_id = f"plan_{uuid.uuid4().hex[:10]}"
    if not candidates:
        return TradePlan(
            plan_id=plan_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            decision=PlanDecision.NO_TRADE,
            regime=regime.value,
            reasoning="No ideas passed validation against strategies, fixtures, and regime policy.",
            risk_flags=notes,
            status="pending",
        )

    ranked = rank_candidate_combos(base_snapshot, candidates, config, policy)
    viable = [combo for combo in ranked if combo.passes_constraints]
    if not viable or viable[0].total_score <= 0:
        return TradePlan(
            plan_id=plan_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            decision=PlanDecision.NO_TRADE,
            regime=regime.value,
            ranked_combos=ranked[:3],
            candidate_positions=candidates,
            reasoning="Candidates were evaluated, but no combo improved the portfolio enough within constraints.",
            risk_flags=notes
            + [
                item.message
                for combo in ranked[:3]
                for item in combo.constraint_checks
                if not item.passed
            ],
            status="pending",
        )

    best = viable[0]
    return TradePlan(
        plan_id=plan_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        decision=PlanDecision.EXECUTE,
        regime=regime.value,
        selected_combo_ids=best.combo_ids,
        ranked_combos=ranked[:3],
        candidate_positions=best.candidate_positions,
        reasoning="Selected the top-ranked combo that improved target Greeks while passing all hard constraints.",
        risk_flags=notes,
        status="pending",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Vol Crush optimizer")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    store = build_local_store(config)
    bundle_path = (
        config.get("data_sources", {})
        .get("fixtures", {})
        .get("bundle_path", "data/fixtures/fixture_bundle.json")
    )
    provider = FixtureMarketDataProvider(bundle_path)
    plan = build_trade_plan(store, config, provider)
    store.save_trade_plan(plan)
    logger.info(
        "Generated trade plan %s with decision=%s", plan.plan_id, plan.decision.value
    )
    if plan.selected_combo_ids:
        logger.info("Selected combo ids: %s", ", ".join(plan.selected_combo_ids))
    else:
        logger.info("No trade selected. Reason: %s", plan.reasoning)


if __name__ == "__main__":
    main()
