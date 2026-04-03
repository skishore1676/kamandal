"""Position management checks for open dry-run positions."""

from __future__ import annotations

import argparse
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vol_crush.core.config import load_config, load_strategies
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import Greeks, OrderStatus, PendingOrder, PositionStatus, Strategy, TradeAction
from vol_crush.integrations.storage import build_local_store

logger = logging.getLogger("vol_crush.position_manager")


def _strategy_map() -> dict[str, Strategy]:
    return {item["id"]: Strategy.from_dict(item) for item in load_strategies()}


def evaluate_positions(config: dict) -> list[PendingOrder]:
    store = build_local_store(config)
    strategies = _strategy_map()
    positions = [position for position in store.list_positions() if position.status == PositionStatus.OPEN.value]
    actions: list[PendingOrder] = []
    timestamp = datetime.now(timezone.utc).isoformat()

    for position in positions:
        strategy = strategies.get(position.strategy_id)
        if strategy is None:
            continue
        action = None
        note = ""
        if position.pnl_pct >= strategy.management.profit_target_pct:
            action = TradeAction.CLOSE
            note = "Profit target reached."
        elif position.current_value >= (position.open_credit * strategy.management.max_loss_multiple):
            action = TradeAction.CLOSE
            note = "Max-loss multiple exceeded."
        elif position.dte_remaining and position.dte_remaining <= strategy.management.roll_dte_trigger:
            action = TradeAction.ROLL
            note = "Roll trigger reached."

        if action is None:
            continue
        actions.append(
            PendingOrder(
                pending_order_id=f"pm_{uuid.uuid4().hex[:10]}",
                plan_id="position_manager",
                created_at=timestamp,
                action=action,
                status=OrderStatus.PENDING.value,
                underlying=position.underlying,
                strategy_id=position.strategy_id,
                quantity=1,
                target_price=position.current_value,
                estimated_credit=position.current_value,
                estimated_bpr=position.bpr,
                greeks_impact=Greeks(
                    delta=-position.greeks.delta,
                    gamma=-position.greeks.gamma,
                    theta=-position.greeks.theta,
                    vega=-position.greeks.vega,
                ),
                notes=note,
                legs=position.legs,
            )
        )
    if actions:
        store.save_pending_orders(actions)
    return actions


def main() -> None:
    parser = argparse.ArgumentParser(description="Vol Crush position manager")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    actions = evaluate_positions(config)
    logger.info("Generated %d position-management actions", len(actions))


if __name__ == "__main__":
    main()
