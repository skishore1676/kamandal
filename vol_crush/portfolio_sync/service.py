"""Sync live broker portfolio state into the local Kamandal store.

Flow (as of the first-class position grouping layer):
    1. Pull raw positions from Public (portfolio/v2).
    2. Fetch option greeks in one batch call per sync.
    3. Persist raw legs verbatim to the `broker_position_legs` audit floor.
    4. Group legs into strategy bundles:
         a. First reconcile against Kamandal-submitted PendingOrders that carry
            a client-supplied broker_order_id. Those become source=kamandal_order
            Positions and inherit strategy_id + preflight BPR.
         b. Everything left is run through the deterministic classifier
            (vol_crush.position_grouping). Unclassifiable bundles become
            unknown_complex / orphan_leg groups with management_status=
            manual_review_required so downstream automation refuses to touch them.
    5. Aggregate the grouped Positions into a PortfolioSnapshot. position_count
       now reflects group count — NOT leg count — which is the number the
       optimizer's diversification and max_positions constraints reason about.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vol_crush.core.config import load_config
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import (
    BrokerPositionLeg,
    Greeks,
    ManagementStatus,
    OrderStatus,
    PendingOrder,
    PortfolioSnapshot,
    Position,
    TradeAction,
)
from vol_crush.integrations.public_broker import PublicBrokerAdapter, parse_occ_symbol
from vol_crush.integrations.storage import LocalStore, build_local_store
from vol_crush.position_grouping import group_broker_legs

logger = logging.getLogger("vol_crush.portfolio_sync")

_RECONCILABLE_ORDER_STATUSES = {
    OrderStatus.PENDING.value,
    OrderStatus.WORKING.value,
    OrderStatus.FILLED.value,
}
_RECONCILABLE_BROKER_STATUSES = {
    "ACCEPTED",
    "FILLED",
    "OPEN",
    "PARTIALLY_FILLED",
    "PENDING",
    "QUEUED",
    "SUBMITTED",
    "WORKING",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int_quantity(value: Any) -> int:
    quantity = _safe_float(value, 0.0)
    rounded = int(round(abs(quantity)))
    return max(rounded, 1) if quantity else 0


def _equity_total(portfolio: dict[str, Any]) -> float:
    entries = portfolio.get("equity", []) or []
    total = sum(_safe_float(item.get("value")) for item in entries)
    if total:
        return total
    buying_power = portfolio.get("buyingPower", {}) or {}
    return _safe_float(buying_power.get("buyingPower"))


def _broker_leg_from_public(
    raw_position: dict[str, Any],
    greeks_by_symbol: dict[str, dict[str, Any]],
    account_id: str,
    retrieved_at: str,
) -> BrokerPositionLeg | None:
    """Translate one raw Public option position into a BrokerPositionLeg.

    Returns None if the row isn't an option (stock legs are irrelevant to the
    options grouping layer; covered_strangle detection is a later concern).
    """
    instrument = raw_position.get("instrument", {}) or {}
    symbol = str(instrument.get("symbol", ""))
    instrument_type = str(instrument.get("type", "")).upper()
    quantity_signed = _safe_float(raw_position.get("quantity"))

    if not symbol or not quantity_signed:
        return None
    if instrument_type != "OPTION":
        return None

    parsed = parse_occ_symbol(symbol)
    quantity = _safe_int_quantity(quantity_signed)
    side = "buy" if quantity_signed > 0 else "sell"
    sign = 1.0 if quantity_signed > 0 else -1.0
    multiplier = abs(quantity_signed)

    greeks_payload = greeks_by_symbol.get(symbol, {})
    leg_greeks = Greeks(
        delta=_safe_float(greeks_payload.get("delta")) * sign * multiplier,
        gamma=_safe_float(greeks_payload.get("gamma")) * sign * multiplier,
        theta=_safe_float(greeks_payload.get("theta")) * sign * multiplier,
        vega=_safe_float(greeks_payload.get("vega")) * sign * multiplier,
    )

    cost_basis = raw_position.get("costBasis", {}) or {}
    current_value = _safe_float(raw_position.get("currentValue"))
    total_cost = _safe_float(cost_basis.get("totalCost"), current_value)
    unit_cost = _safe_float(cost_basis.get("unitCost"), total_cost)
    pnl_pct = _safe_float(cost_basis.get("gainPercentage"))

    return BrokerPositionLeg(
        leg_id=f"public:{account_id}:{symbol}",
        broker="public",
        account_id=account_id,
        occ_symbol=symbol,
        underlying=parsed["underlying"],
        expiration=parsed["expiration"],
        strike=parsed["strike"],
        option_type=parsed["option_type"],
        side=side,
        quantity=quantity,
        signed_quantity=quantity_signed,
        current_value=round(current_value, 4),
        total_cost=round(total_cost, 4),
        unit_cost=round(abs(unit_cost), 4),
        pnl_pct=pnl_pct,
        greeks=leg_greeks,
        retrieved_at=retrieved_at,
        raw_payload=dict(raw_position),
    )


def _load_known_orders(store: LocalStore) -> list[PendingOrder]:
    """Return PendingOrders that could have opened live legs on the broker.

    Kamandal-opened groups are reconciled against these. We skip close/roll/adjust
    orders (they would remove legs, not add them), dry-run/preflight-only rows, and
    orders without a group anchor.
    """
    orders: list[PendingOrder] = []
    for order in store.list_pending_orders():
        if not order.broker_order_id:
            continue
        if order.action != TradeAction.OPEN:
            continue
        if order.status not in _RECONCILABLE_ORDER_STATUSES:
            continue
        broker_status = (order.broker_status or "").upper()
        if broker_status not in _RECONCILABLE_BROKER_STATUSES:
            continue
        orders.append(order)
    return orders


def _snapshot_from_positions(
    portfolio: dict[str, Any], positions: list[Position]
) -> PortfolioSnapshot:
    """Aggregate grouped Positions into a PortfolioSnapshot.

    Greeks and BPR include ALL groups (even manual_review_required), because the
    optimizer must see true exposure even when it refuses to manage a bundle.
    The `position_count` is ALWAYS the group count — never leg count.
    """
    net_liquidation_value = _equity_total(portfolio)
    greeks = Greeks()
    bpr_used = 0.0
    for position in positions:
        greeks = greeks + position.greeks
        bpr_used += position.bpr
    theta_pct = (
        (greeks.theta * 100.0 / net_liquidation_value) if net_liquidation_value else 0.0
    )
    gamma_theta_ratio = abs(greeks.gamma / greeks.theta) if greeks.theta else 0.0
    return PortfolioSnapshot(
        timestamp=datetime.now(UTC).isoformat(),
        net_liquidation_value=round(net_liquidation_value, 4),
        greeks=greeks,
        beta_weighted_delta=greeks.delta,
        bpr_used=round(bpr_used, 4),
        bpr_used_pct=(
            round((bpr_used / net_liquidation_value) * 100.0, 4)
            if net_liquidation_value
            else 0.0
        ),
        theta_as_pct_nlv=round(theta_pct, 6),
        gamma_theta_ratio=round(gamma_theta_ratio, 6),
        position_count=len(positions),
        positions=positions,
    )


def sync_public_portfolio(
    config: dict[str, Any],
    store: LocalStore | None = None,
    adapter: PublicBrokerAdapter | None = None,
) -> PortfolioSnapshot:
    store = store or build_local_store(config)
    adapter = adapter or PublicBrokerAdapter(config)

    retrieved_at = datetime.now(UTC).isoformat()
    portfolio = adapter.get_portfolio()
    raw_positions = portfolio.get("positions", []) or []

    account_id = str(portfolio.get("accountId") or "")
    if not account_id and hasattr(adapter, "get_primary_account_id"):
        try:
            account_id = adapter.get_primary_account_id()
        except Exception:
            account_id = ""

    option_symbols = [
        str((item.get("instrument", {}) or {}).get("symbol", ""))
        for item in raw_positions
        if str((item.get("instrument", {}) or {}).get("type", "")).upper() == "OPTION"
    ]
    greeks_by_symbol = adapter.get_option_greeks_batch(option_symbols)

    # Step 1: build the raw-leg audit floor.
    broker_legs: list[BrokerPositionLeg] = []
    for raw_position in raw_positions:
        leg = _broker_leg_from_public(
            raw_position, greeks_by_symbol, account_id, retrieved_at
        )
        if leg is not None:
            broker_legs.append(leg)

    # Replace all Public legs atomically (portfolio pulls are snapshots).
    store.replace_broker_legs("public", broker_legs)

    # Step 2: group legs into strategy bundles, preferring Kamandal-submitted orders.
    known_orders = _load_known_orders(store)
    positions = group_broker_legs(broker_legs, known_orders=known_orders)

    # Step 3: surface any manual_review_required groups in the logs for operator visibility.
    for position in positions:
        if position.management_status == ManagementStatus.MANUAL_REVIEW_REQUIRED.value:
            logger.warning(
                "Group %s (%s on %s) flagged for manual review: confidence=%s, legs=%d",
                position.group_id,
                position.strategy_type,
                position.underlying,
                position.confidence,
                len(position.legs),
            )

    snapshot = _snapshot_from_positions(portfolio, positions)
    store.save_positions(positions)
    store.save_portfolio_snapshot(snapshot)
    logger.info(
        "Public portfolio sync: %d raw legs → %d groups (kamandal=%d, inferred=%d, manual_review=%d)",
        len(broker_legs),
        len(positions),
        sum(1 for p in positions if p.source == "kamandal_order"),
        sum(1 for p in positions if p.source == "public_inferred"),
        sum(
            1
            for p in positions
            if p.management_status == ManagementStatus.MANUAL_REVIEW_REQUIRED.value
        ),
    )
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Public portfolio into Kamandal store"
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--broker",
        default="public",
        choices=["public"],
        help="Which broker to sync (currently only Public is supported)",
    )
    parser.add_argument(
        "--show-groups",
        action="store_true",
        help="Print the grouped view after syncing: one line per strategy bundle.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    snapshot = sync_public_portfolio(config)
    logger.info(
        "Portfolio sync complete: NLV=%.2f, groups=%d, delta=%.4f, theta=%.4f",
        snapshot.net_liquidation_value,
        snapshot.position_count,
        snapshot.greeks.delta,
        snapshot.greeks.theta,
    )
    if args.show_groups:
        _print_groups(snapshot)


def _print_groups(snapshot: PortfolioSnapshot) -> None:
    """Pretty-print the grouped view for CLI inspection."""
    if not snapshot.positions:
        print("(no grouped positions)")
        return
    print(
        f"{'group_id':42} {'type':16} {'undl':6} {'qty':>4} {'credit':>9} {'max_loss':>10} {'bpr':>10} {'conf':>8} {'mgmt':22}"
    )
    for position in snapshot.positions:
        group_id = position.group_id or position.position_id
        print(
            f"{group_id[:42]:42} "
            f"{position.strategy_type[:16]:16} "
            f"{position.underlying[:6]:6} "
            f"{position.quantity:>4} "
            f"{position.net_credit:>9.2f} "
            f"{position.max_loss:>10.2f} "
            f"{position.bpr:>10.2f} "
            f"{position.confidence:>8} "
            f"{position.management_status:22}"
        )
