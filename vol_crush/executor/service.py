"""Pending/dry-run executor for Vol Crush."""

from __future__ import annotations

import argparse
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from vol_crush.core.config import load_config
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import (
    CandidatePosition,
    OrderStatus,
    PendingOrder,
    PortfolioSnapshot,
    TradeAction,
    TradePlan,
)
from vol_crush.integrations.public_broker import PublicBrokerAdapter
from vol_crush.integrations.storage import build_local_store
from vol_crush.optimizer.service import (
    _apply_shadow_nlv_override,
    _project_portfolio,
    evaluate_constraints,
)
from vol_crush.portfolio_sync.service import sync_public_portfolio

logger = logging.getLogger("vol_crush.executor")


def _sized_quantity(candidate, portfolio: PortfolioSnapshot, config: dict) -> int:
    constraints = config.get("portfolio", {}).get("constraints", {})
    execution = config.get("execution", {})
    max_single_pct = constraints.get("max_single_underlying_pct", 15.0) / 100.0
    max_bpr_for_single = portfolio.net_liquidation_value * max_single_pct
    if candidate.estimated_bpr <= 0:
        quantity = 1
    else:
        quantity = max(1, int(max_bpr_for_single // candidate.estimated_bpr))

    cap = execution.get("max_contracts_per_order")
    if cap is None and str(execution.get("mode", "")).lower() == "live":
        cap = 1
    if cap is not None:
        quantity = min(quantity, max(1, int(cap)))
    return quantity


def _scale_candidate(candidate: CandidatePosition, quantity: int) -> CandidatePosition:
    return replace(
        candidate,
        estimated_credit=round(candidate.estimated_credit * quantity, 4),
        estimated_bpr=round(candidate.estimated_bpr * quantity, 2),
        estimated_greeks=candidate.estimated_greeks * quantity,
    )


def _largest_quantity_within_constraints(
    candidate: CandidatePosition,
    desired_quantity: int,
    accepted_candidates: list[CandidatePosition],
    portfolio: PortfolioSnapshot,
    config: dict,
) -> int:
    """Walk quantity down until the scaled order still passes hard constraints."""
    for quantity in range(max(desired_quantity, 1), 0, -1):
        scaled = _scale_candidate(candidate, quantity)
        projected_candidates = accepted_candidates + [scaled]
        projected = _project_portfolio(portfolio, projected_candidates)
        checks = evaluate_constraints(
            projected, projected_candidates, config, base=portfolio
        )
        if all(check.passed for check in checks):
            return quantity
    return 0


def _latest_trade_plan(plans: list[TradePlan]) -> TradePlan | None:
    if not plans:
        return None

    def _created_at_key(plan: TradePlan) -> tuple[datetime, str]:
        raw = (plan.created_at or "").replace("Z", "+00:00")
        try:
            created_at = datetime.fromisoformat(raw)
        except ValueError:
            created_at = datetime.min.replace(tzinfo=timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.astimezone(timezone.utc), plan.plan_id

    return max(plans, key=_created_at_key)


def _sheet_plan_approved(config: dict, plan: TradePlan) -> tuple[bool, str]:
    """Require operator approval from daily_plan when sheet sync is enabled."""
    if bool((config.get("execution") or {}).get("bypass_daily_plan_approval", False)):
        return True, "daily_plan approval bypass enabled"
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return True, "sheet approval disabled"
    try:
        from vol_crush.sheets.sync import read_daily_plan_cache
    except ImportError:
        return False, "daily_plan cache reader unavailable"

    rows = [row for row in read_daily_plan_cache(config) if row.plan_id == plan.plan_id]
    if not rows:
        return False, f"no daily_plan approval rows found for {plan.plan_id}"
    unapproved = [row for row in rows if row.approval != "approve"]
    if unapproved:
        return False, f"{len(unapproved)} daily_plan rows are not approved"
    return True, "daily_plan approved"


def create_pending_orders(
    plan: TradePlan, portfolio: PortfolioSnapshot, config: dict
) -> list[PendingOrder]:
    if plan.decision.value != "execute":
        return []
    created_at = datetime.now(timezone.utc).isoformat()
    orders = []
    accepted_candidates: list[CandidatePosition] = []
    for candidate in plan.candidate_positions:
        desired_quantity = _sized_quantity(candidate, portfolio, config)
        quantity = _largest_quantity_within_constraints(
            candidate,
            desired_quantity,
            accepted_candidates,
            portfolio,
            config,
        )
        if quantity < 1:
            logger.warning(
                "Skipping candidate %s: no quantity passes post-sizing constraints.",
                candidate.idea_id,
            )
            continue
        scaled_candidate = _scale_candidate(candidate, quantity)
        accepted_candidates.append(scaled_candidate)
        orders.append(
            PendingOrder(
                pending_order_id=f"pending_{uuid.uuid4().hex[:10]}",
                plan_id=plan.plan_id,
                idea_id=candidate.idea_id,
                created_at=created_at,
                action=TradeAction.OPEN,
                status=OrderStatus.PENDING.value,
                underlying=candidate.underlying,
                strategy_id=candidate.strategy_id,
                quantity=quantity,
                target_price=round(candidate.estimated_credit, 4),
                estimated_credit=scaled_candidate.estimated_credit,
                estimated_bpr=scaled_candidate.estimated_bpr,
                greeks_impact=scaled_candidate.estimated_greeks,
                notes="Pending order generated from deterministic optimizer plan.",
                legs=candidate.legs,
            )
        )
    return orders


def execute_latest_plan(config: dict) -> list[PendingOrder]:
    store = build_local_store(config)
    plans = store.list_trade_plans()
    latest = _latest_trade_plan(plans)
    if latest is None:
        logger.info("No trade plans found.")
        return []
    approved, approval_note = _sheet_plan_approved(config, latest)
    if not approved:
        logger.info("Skipping executor for %s: %s", latest.plan_id, approval_note)
        return []
    snapshot = store.get_latest_portfolio_snapshot() or PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc).isoformat(),
        net_liquidation_value=100000.0,
    )
    snapshot = _apply_shadow_nlv_override(snapshot, config)
    orders = create_pending_orders(latest, snapshot, config)
    if orders:
        store.save_pending_orders(orders)
        if config.get("broker", {}).get("active") == "public":
            adapter = PublicBrokerAdapter(config)
            orders = adapter.submit_pending_orders(orders)
            store.save_pending_orders(orders)
            if (
                config.get("broker", {})
                .get("public", {})
                .get("sync_portfolio_after_submission", True)
            ):
                try:
                    sync_public_portfolio(config, store=store, adapter=adapter)
                except Exception as exc:
                    logger.warning(
                        "Post-submission Public portfolio sync failed: %s", exc
                    )
    return orders


def main() -> None:
    parser = argparse.ArgumentParser(description="Vol Crush pending executor")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    orders = execute_latest_plan(config)
    logger.info("Generated %d pending orders", len(orders))


if __name__ == "__main__":
    main()
