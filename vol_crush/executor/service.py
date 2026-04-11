"""Pending/dry-run executor for Vol Crush."""

from __future__ import annotations

import argparse
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vol_crush.core.config import load_config
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import (
    OrderStatus,
    PendingOrder,
    PortfolioSnapshot,
    TradeAction,
    TradePlan,
)
from vol_crush.integrations.public_broker import PublicBrokerAdapter
from vol_crush.integrations.storage import build_local_store
from vol_crush.portfolio_sync.service import sync_public_portfolio

logger = logging.getLogger("vol_crush.executor")


def _sized_quantity(candidate, portfolio: PortfolioSnapshot, config: dict) -> int:
    constraints = config.get("portfolio", {}).get("constraints", {})
    max_single_pct = constraints.get("max_single_underlying_pct", 15.0) / 100.0
    max_bpr_for_single = portfolio.net_liquidation_value * max_single_pct
    if candidate.estimated_bpr <= 0:
        return 1
    return max(1, int(max_bpr_for_single // candidate.estimated_bpr))


def create_pending_orders(
    plan: TradePlan, portfolio: PortfolioSnapshot, config: dict
) -> list[PendingOrder]:
    if plan.decision.value != "execute":
        return []
    created_at = datetime.now(timezone.utc).isoformat()
    orders = []
    for candidate in plan.candidate_positions:
        quantity = _sized_quantity(candidate, portfolio, config)
        orders.append(
            PendingOrder(
                pending_order_id=f"pending_{uuid.uuid4().hex[:10]}",
                plan_id=plan.plan_id,
                created_at=created_at,
                action=TradeAction.OPEN,
                status=OrderStatus.PENDING.value,
                underlying=candidate.underlying,
                strategy_id=candidate.strategy_id,
                quantity=quantity,
                target_price=round(candidate.estimated_credit, 4),
                estimated_credit=round(candidate.estimated_credit * quantity, 4),
                estimated_bpr=round(candidate.estimated_bpr * quantity, 2),
                greeks_impact=candidate.estimated_greeks * quantity,
                notes="Pending order generated from deterministic optimizer plan.",
                legs=candidate.legs,
            )
        )
    return orders


def execute_latest_plan(config: dict) -> list[PendingOrder]:
    store = build_local_store(config)
    plans = store.list_trade_plans()
    if not plans:
        logger.info("No trade plans found.")
        return []
    latest = plans[-1]
    snapshot = store.get_latest_portfolio_snapshot() or PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc).isoformat(),
        net_liquidation_value=100000.0,
    )
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
