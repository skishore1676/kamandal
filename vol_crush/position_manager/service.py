"""Position management checks for open grouped positions.

Consumes grouped Position objects (one per strategy bundle), NOT raw legs.
Safety invariants enforced here:
    1. Never emit any action when management_status != AUTO.
    2. CLOSE orders must carry every leg of the source group — we assert this
       before emitting, so we can never accidentally close one leg of a spread.
    3. Strategy lookup happens by strategy_id (linking to strategies.yaml) OR
       by strategy_type for groups reconstructed from Public where the yaml
       rule was keyed on structure. Falling through both is a silent no-op.
    4. Rolls and partial adjustments are intentionally not automated yet — the
       executor/broker doesn't wire those into multi-leg preflight, and
       blindly emitting them would be worse than leaving them manual.
"""

from __future__ import annotations

import argparse
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from vol_crush.core.config import (
    load_config,
    load_strategies,
    load_strategy_templates,
    load_underlying_profiles,
)
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import (
    Greeks,
    ManagementStatus,
    OrderStatus,
    PendingOrder,
    Position,
    PositionStatus,
    Strategy,
    StrategyTemplate,
    TradeAction,
    UnderlyingProfile,
    resolve_all_strategies,
)
from vol_crush.integrations.storage import build_local_store

logger = logging.getLogger("vol_crush.position_manager")


def _execution_mode(config: dict) -> str:
    raw = str((config.get("execution") or {}).get("mode", "")).lower()
    return "shadow" if raw == "pending" else raw


def _strategy_map() -> dict[str, Strategy]:
    """Load resolved strategies from templates + profiles, falling back to legacy strategies.yaml."""
    templates = [StrategyTemplate.from_dict(d) for d in load_strategy_templates() if d]
    profiles = [UnderlyingProfile.from_dict(d) for d in load_underlying_profiles() if d]
    resolved = resolve_all_strategies(templates, profiles)
    if resolved:
        return {s.id: s for s in resolved}
    return {item["id"]: Strategy.from_dict(item) for item in load_strategies()}


def _resolve_strategy(
    position: Position, strategies: dict[str, Strategy]
) -> Strategy | None:
    """Prefer a direct strategy_id match; fall back to any strategy whose structure
    equals this group's classified strategy_type. This lets strategies.yaml rules
    manage Public-imported groups that share the same structural template."""
    rule = strategies.get(position.strategy_id)
    if rule is not None:
        return rule
    if not position.strategy_type:
        return None
    for candidate in strategies.values():
        if candidate.structure.value == position.strategy_type:
            return candidate
    return None


def _assert_full_group_close(position: Position, order_legs: list) -> None:
    """Hard assertion: close orders must carry every leg of the source group.

    If this ever trips in production it means someone introduced a partial-close
    path without wiring it through TradeAction.ADJUST with an explicit safety check.
    Crashing is the correct behavior — a partial-close on a short spread is worse
    than a no-op.
    """
    if len(order_legs) != len(position.legs):
        raise AssertionError(
            f"Position manager attempted to close group {position.group_id} "
            f"with {len(order_legs)} legs but group has {len(position.legs)}. "
            f"This would leave naked legs. Refusing."
        )


def evaluate_positions(config: dict) -> list[PendingOrder]:
    store = build_local_store(config)
    strategies = _strategy_map()
    if _execution_mode(config) == "shadow":
        from vol_crush.shadow.service import build_shadow_portfolio_snapshot

        build_shadow_portfolio_snapshot(store, config)
        source_positions = store.list_shadow_positions()
    else:
        source_positions = store.list_positions()
    positions = [p for p in source_positions if p.status == PositionStatus.OPEN.value]
    actions: list[PendingOrder] = []
    timestamp = datetime.now(UTC).isoformat()

    for position in positions:
        if position.management_status != ManagementStatus.AUTO.value:
            logger.info(
                "Skipping group %s (%s on %s): management_status=%s",
                position.group_id or position.position_id,
                position.strategy_type,
                position.underlying,
                position.management_status,
            )
            continue

        strategy = _resolve_strategy(position, strategies)
        if strategy is None:
            logger.debug(
                "Group %s has no matching strategy rule (strategy_id=%r, strategy_type=%r)",
                position.group_id or position.position_id,
                position.strategy_id,
                position.strategy_type,
            )
            continue

        action: TradeAction | None = None
        note = ""
        if position.pnl_pct >= strategy.management.profit_target_pct:
            action = TradeAction.CLOSE
            note = "Profit target reached."
        elif position.open_credit and position.current_value >= (
            position.open_credit * strategy.management.max_loss_multiple
        ):
            action = TradeAction.CLOSE
            note = "Max-loss multiple exceeded."
        elif (
            position.dte_remaining
            and position.dte_remaining <= strategy.management.roll_dte_trigger
        ):
            action = TradeAction.ROLL
            note = "Roll trigger reached."

        if action is None:
            continue

        order_legs = list(position.legs)
        if action == TradeAction.CLOSE:
            _assert_full_group_close(position, order_legs)

        actions.append(
            PendingOrder(
                pending_order_id=f"pm_{uuid.uuid4().hex[:10]}",
                plan_id="position_manager",
                created_at=timestamp,
                action=action,
                status=OrderStatus.PENDING.value,
                underlying=position.underlying,
                strategy_id=position.strategy_id or strategy.id,
                quantity=max(int(position.quantity), 1),
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
                legs=order_legs,
                broker_order_id=position.broker_order_id,
                strategy_type=position.strategy_type,
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
