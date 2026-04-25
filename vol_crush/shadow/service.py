"""Shadow portfolio projection for broker-preflighted paper execution."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Iterable

from vol_crush.core.config import shadow_net_liquidation_value
from vol_crush.core.models import (
    Greeks,
    GroupConfidence,
    ManagementStatus,
    OrderStatus,
    PendingOrder,
    PortfolioSnapshot,
    Position,
    PositionSource,
    PositionStatus,
    ShadowFill,
    TradeAction,
)
from vol_crush.integrations.storage import LocalStore

logger = logging.getLogger("vol_crush.shadow")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _shadow_starting_nlv(config: dict[str, Any]) -> float:
    configured = shadow_net_liquidation_value(config)
    if configured is not None:
        return float(configured)
    return 100000.0


def _preflight_bpr(order: PendingOrder) -> float:
    for key in ("buyingPowerRequirement", "buying_power_requirement", "bpr"):
        raw = (order.broker_response or {}).get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return float(order.estimated_bpr or 0.0)


def _fill_id_for_order(order: PendingOrder) -> str:
    stable = order.pending_order_id or uuid.uuid4().hex
    return f"shadow_fill_{stable}"


def shadow_fill_from_order(
    order: PendingOrder, *, filled_at: str | None = None
) -> ShadowFill:
    """Create a deterministic shadow fill from a successful preflighted order."""
    return ShadowFill(
        fill_id=_fill_id_for_order(order),
        pending_order_id=order.pending_order_id,
        plan_id=order.plan_id,
        idea_id=order.idea_id,
        filled_at=filled_at or _utc_now(),
        action=(
            order.action.value if hasattr(order.action, "value") else str(order.action)
        ),
        underlying=order.underlying,
        strategy_id=order.strategy_id,
        strategy_type=order.strategy_type,
        quantity=max(int(order.quantity or 1), 1),
        fill_price=float(order.target_price or 0.0),
        gross_credit=float(order.estimated_credit or 0.0),
        estimated_bpr=_preflight_bpr(order),
        greeks_impact=Greeks.from_dict(order.greeks_impact.to_dict()),
        broker=order.broker,
        broker_order_id=order.broker_order_id,
        broker_status=order.broker_status,
        preflight_response=dict(order.broker_response or {}),
        legs=list(order.legs),
    )


def _is_shadow_fillable(order: PendingOrder) -> bool:
    action = order.action.value if hasattr(order.action, "value") else str(order.action)
    return (
        str(order.execution_mode or "").lower() == "shadow"
        and action == TradeAction.OPEN.value
        and order.broker_status == "PREFLIGHT_OK"
        and order.status == OrderStatus.PENDING.value
    )


def record_shadow_fills_for_orders(
    store: LocalStore,
    orders: Iterable[PendingOrder],
    config: dict[str, Any],
) -> list[ShadowFill]:
    """Persist fills for newly preflighted shadow orders and refresh the account."""
    existing_order_ids = {fill.pending_order_id for fill in store.list_shadow_fills()}
    fills: list[ShadowFill] = []
    for order in orders:
        if not _is_shadow_fillable(order):
            continue
        if order.pending_order_id in existing_order_ids:
            continue
        fill = shadow_fill_from_order(order)
        fills.append(fill)
        order.status = OrderStatus.FILLED.value
        order.notes = (
            f"{order.notes} Shadow fill assumed after successful broker preflight."
        ).strip()

    if not fills:
        return []

    store.save_shadow_fills(fills)
    build_shadow_portfolio_snapshot(store, config)
    logger.info("Recorded %d shadow fills.", len(fills))
    return fills


def _position_from_open_fill(fill: ShadowFill) -> Position:
    return Position(
        position_id=f"shadow_pos_{fill.fill_id}",
        underlying=fill.underlying,
        strategy_id=fill.strategy_id,
        legs=list(fill.legs),
        open_date=fill.filled_at[:10],
        open_credit=round(fill.gross_credit, 4),
        current_value=round(fill.gross_credit, 4),
        greeks=Greeks.from_dict(fill.greeks_impact.to_dict()),
        pnl_pct=0.0,
        status=PositionStatus.OPEN.value,
        bpr=round(fill.estimated_bpr, 2),
        group_id=fill.fill_id,
        source=PositionSource.SHADOW.value,
        strategy_type=fill.strategy_type,
        expirations=sorted({leg.expiration for leg in fill.legs if leg.expiration}),
        quantity=max(int(fill.quantity or 1), 1),
        net_credit=round(fill.gross_credit, 4),
        max_profit=max(round(fill.gross_credit, 4), 0.0),
        max_loss=round(fill.estimated_bpr, 2),
        confidence=GroupConfidence.HIGH.value,
        management_status=ManagementStatus.AUTO.value,
        broker=fill.broker,
        broker_order_id=fill.broker_order_id,
    )


def project_shadow_positions(fills: Iterable[ShadowFill]) -> list[Position]:
    """Project open shadow fills into the current shadow position book.

    Phase 1 supports open fills. Close/roll/adjust lifecycle simulation will layer
    on this ledger without changing the execution-core contract.
    """
    positions = [
        _position_from_open_fill(fill)
        for fill in fills
        if fill.action == TradeAction.OPEN.value
    ]
    positions.sort(key=lambda position: position.position_id)
    return positions


def _snapshot_from_positions(
    positions: list[Position],
    *,
    starting_nlv: float,
) -> PortfolioSnapshot:
    greeks = Greeks()
    bpr_used = 0.0
    for position in positions:
        greeks = greeks + position.greeks
        bpr_used += position.bpr
    theta_pct = (greeks.theta * 100.0 / starting_nlv) if starting_nlv else 0.0
    gamma_theta_ratio = abs(greeks.gamma / greeks.theta) if greeks.theta else 0.0
    return PortfolioSnapshot(
        timestamp=_utc_now(),
        net_liquidation_value=round(starting_nlv, 4),
        greeks=greeks,
        beta_weighted_delta=greeks.delta,
        bpr_used=round(bpr_used, 4),
        bpr_used_pct=(
            round((bpr_used / starting_nlv) * 100.0, 4) if starting_nlv else 0.0
        ),
        theta_as_pct_nlv=round(theta_pct, 6),
        gamma_theta_ratio=round(gamma_theta_ratio, 6),
        position_count=len(positions),
        positions=positions,
    )


def build_shadow_portfolio_snapshot(
    store: LocalStore,
    config: dict[str, Any],
) -> PortfolioSnapshot:
    """Rebuild and persist the shadow account from the fill ledger."""
    positions = project_shadow_positions(store.list_shadow_fills())
    store.save_shadow_positions(positions)
    snapshot = _snapshot_from_positions(
        positions,
        starting_nlv=_shadow_starting_nlv(config),
    )
    store.save_shadow_portfolio_snapshot(snapshot)
    return snapshot
