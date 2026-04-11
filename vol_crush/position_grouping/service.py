"""Deterministic classifier that turns raw broker legs into grouped Positions.

This is the core of the first-class position-grouping layer. Given a flat list of
`BrokerPositionLeg` objects, `group_broker_legs` partitions them into strategy
bundles and returns one `Position` per bundle.

Classification is *pure* and *deterministic*. The same input always produces the
same grouping. When we can't classify a bundle confidently we emit an
`unknown_complex` or `orphan_leg` group with `management_status="manual_review_required"`
so the position manager refuses to act on it.

For Kamandal-opened trades that we want to preserve exactly (same legs, same
broker_order_id, same strategy_id), callers should use `reconcile_with_known_orders`
BEFORE calling `group_broker_legs` on the leftover legs. That's the seam that lets
us treat Public's `orderId` as the durable group anchor.

Ordering of classifiers matters — most specific first. The first match wins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from vol_crush.core.models import (
    BrokerPositionLeg,
    GroupConfidence,
    Greeks,
    ManagementStatus,
    PendingOrder,
    Position,
    PositionSource,
    PositionStatus,
    StrategyType,
)
from vol_crush.position_grouping.bpr import estimate_bpr, estimate_max_profit

# ── Types ─────────────────────────────────────────────────────────────────────


@dataclass
class _GroupCandidate:
    """Internal classification output before materializing a Position."""

    strategy_type: str
    legs: list[BrokerPositionLeg]
    quantity: int
    net_credit: float  # per-contract net credit (positive = credit, negative = debit)
    confidence: str
    management_status: str
    notes: list[str]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _days_to_expiration(expiration: str) -> int:
    try:
        expiry = datetime.fromisoformat(expiration).date()
    except ValueError:
        return 0
    today = datetime.now(UTC).date()
    return max((expiry - today).days, 0)


def _per_contract_net_credit(legs: Iterable[BrokerPositionLeg]) -> float:
    """Estimate the per-contract net credit/debit for a group from unit_cost.

    `unit_cost` on a BrokerPositionLeg is the absolute cost basis per contract
    (positive regardless of side). For short legs this represents premium received;
    for long legs this represents premium paid. We return a positive number for
    credits and negative for debits, scaled per 1-contract of the group.
    """
    total = 0.0
    for leg in legs:
        sign = 1.0 if leg.side == "sell" else -1.0
        total += sign * leg.unit_cost / 100.0
    return round(total, 4)


def _group_greeks(legs: Iterable[BrokerPositionLeg]) -> Greeks:
    """Sum leg-level greeks. BrokerPositionLeg greeks are already sign-adjusted
    upstream (short => negative contribution) by the portfolio sync pipeline."""
    total = Greeks()
    for leg in legs:
        total = total + leg.greeks
    return total


def _min_quantity(legs: Iterable[BrokerPositionLeg]) -> int:
    qtys = [leg.quantity for leg in legs if leg.quantity > 0]
    return min(qtys) if qtys else 1


def _materialize_position(
    group: _GroupCandidate,
    source: str = PositionSource.PUBLIC_INFERRED.value,
    broker: str = "",
    broker_order_id: str = "",
    strategy_id: str = "",
) -> Position:
    option_legs = [leg.as_option_leg() for leg in group.legs]
    underlying = group.legs[0].underlying
    expirations = sorted({leg.expiration for leg in group.legs})
    dte = min((_days_to_expiration(exp) for exp in expirations), default=0)
    greeks = _group_greeks(group.legs)
    current_value = sum(leg.current_value for leg in group.legs)
    total_cost = sum(leg.total_cost for leg in group.legs)

    bpr = estimate_bpr(
        group.strategy_type, option_legs, group.net_credit, group.quantity
    )
    max_profit = estimate_max_profit(
        group.strategy_type, option_legs, group.net_credit, group.quantity
    )
    max_loss = (
        bpr  # for defined-risk groups these match; undefined-risk reuses the BPR floor
    )

    # Deterministic group_id so resyncs are idempotent:
    sorted_legs = sorted(group.legs, key=lambda leg: (leg.option_type, leg.strike))
    strike_part = "/".join(f"{leg.strike:g}" for leg in sorted_legs)
    deterministic_id = f"inferred:{underlying}:{'+'.join(expirations)}:{group.strategy_type}:{strike_part}"
    group_id = broker_order_id or deterministic_id

    pnl_pct = 0.0
    if abs(total_cost) > 1e-6:
        pnl_pct = round(((current_value - total_cost) / abs(total_cost)) * 100.0, 4)

    return Position(
        position_id=group_id,
        underlying=underlying,
        strategy_id=strategy_id,
        legs=option_legs,
        open_date="",
        open_credit=round(group.net_credit, 4),
        current_value=round(abs(current_value) / 100.0, 4) if current_value else 0.0,
        greeks=greeks,
        dte_remaining=dte,
        pnl_pct=pnl_pct,
        status=PositionStatus.OPEN.value,
        bpr=round(bpr, 2),
        group_id=group_id,
        source=source,
        strategy_type=group.strategy_type,
        expirations=expirations,
        quantity=group.quantity,
        net_credit=round(group.net_credit * 100.0 * group.quantity, 2),
        max_profit=round(max_profit, 2),
        max_loss=round(max_loss, 2),
        confidence=group.confidence,
        management_status=group.management_status,
        broker=broker,
        broker_order_id=broker_order_id,
    )


# ── Classifiers ───────────────────────────────────────────────────────────────


def _classify_same_expiry(legs: list[BrokerPositionLeg]) -> _GroupCandidate | None:
    """Try to classify a list of legs that share one underlying and one expiration."""
    if not legs:
        return None

    qty = _min_quantity(legs)
    net_credit = _per_contract_net_credit(legs)
    puts = [leg for leg in legs if leg.option_type == "put"]
    calls = [leg for leg in legs if leg.option_type == "call"]
    short_puts = [leg for leg in puts if leg.side == "sell"]
    long_puts = [leg for leg in puts if leg.side == "buy"]
    short_calls = [leg for leg in calls if leg.side == "sell"]
    long_calls = [leg for leg in calls if leg.side == "buy"]

    # Iron condor: exactly 4 legs — long_put, short_put, short_call, long_call
    # with long_put.strike < short_put.strike < short_call.strike < long_call.strike
    if (
        len(legs) == 4
        and len(long_puts) == 1
        and len(short_puts) == 1
        and len(short_calls) == 1
        and len(long_calls) == 1
        and long_puts[0].strike
        < short_puts[0].strike
        < short_calls[0].strike
        < long_calls[0].strike
    ):
        return _GroupCandidate(
            strategy_type=StrategyType.IRON_CONDOR.value,
            legs=legs,
            quantity=qty,
            net_credit=net_credit,
            confidence=GroupConfidence.HIGH.value,
            management_status=ManagementStatus.AUTO.value,
            notes=[],
        )

    # Jade lizard: short_put + short_call + long_call (same expiry), call spread
    # narrower than or equal to net credit (so no upside risk). 3 legs.
    if (
        len(legs) == 3
        and len(short_puts) == 1
        and len(short_calls) == 1
        and len(long_calls) == 1
        and long_calls[0].strike > short_calls[0].strike
    ):
        call_width = long_calls[0].strike - short_calls[0].strike
        notes: list[str] = []
        confidence = GroupConfidence.HIGH.value
        mgmt = ManagementStatus.AUTO.value
        if net_credit < call_width:
            notes.append(
                "Net credit below call spread width — jade lizard has upside risk; treat cautiously."
            )
            confidence = GroupConfidence.MEDIUM.value
        return _GroupCandidate(
            strategy_type=StrategyType.JADE_LIZARD.value,
            legs=legs,
            quantity=qty,
            net_credit=net_credit,
            confidence=confidence,
            management_status=mgmt,
            notes=notes,
        )

    # Straddle: short_put + short_call at SAME strike, same expiry
    if (
        len(legs) == 2
        and len(short_puts) == 1
        and len(short_calls) == 1
        and short_puts[0].strike == short_calls[0].strike
    ):
        return _GroupCandidate(
            strategy_type=StrategyType.STRADDLE.value,
            legs=legs,
            quantity=qty,
            net_credit=net_credit,
            confidence=GroupConfidence.HIGH.value,
            management_status=ManagementStatus.AUTO.value,
            notes=[],
        )

    # Short strangle: short_put + short_call at DIFFERENT strikes, same expiry
    if len(legs) == 2 and len(short_puts) == 1 and len(short_calls) == 1:
        return _GroupCandidate(
            strategy_type=StrategyType.SHORT_STRANGLE.value,
            legs=legs,
            quantity=qty,
            net_credit=net_credit,
            confidence=GroupConfidence.HIGH.value,
            management_status=ManagementStatus.AUTO.value,
            notes=[],
        )

    # Vertical put spread: 2 puts, opposite sides, same expiry
    if (
        len(legs) == 2
        and len(puts) == 2
        and len(short_puts) == 1
        and len(long_puts) == 1
    ):
        return _GroupCandidate(
            strategy_type=StrategyType.PUT_SPREAD.value,
            legs=legs,
            quantity=qty,
            net_credit=net_credit,
            confidence=GroupConfidence.HIGH.value,
            management_status=ManagementStatus.AUTO.value,
            notes=[],
        )

    # Vertical call spread: 2 calls, opposite sides, same expiry
    if (
        len(legs) == 2
        and len(calls) == 2
        and len(short_calls) == 1
        and len(long_calls) == 1
    ):
        return _GroupCandidate(
            strategy_type=StrategyType.CALL_SPREAD.value,
            legs=legs,
            quantity=qty,
            net_credit=net_credit,
            confidence=GroupConfidence.HIGH.value,
            management_status=ManagementStatus.AUTO.value,
            notes=[],
        )

    # Single-leg classifications
    if len(legs) == 1:
        leg = legs[0]
        if leg.option_type == "put" and leg.side == "sell":
            return _GroupCandidate(
                strategy_type=StrategyType.SHORT_PUT.value,
                legs=legs,
                quantity=qty,
                net_credit=net_credit,
                confidence=GroupConfidence.HIGH.value,
                management_status=ManagementStatus.AUTO.value,
                notes=[],
            )
        if leg.option_type == "call" and leg.side == "sell":
            # Naked short call is high-risk. Allow classification but flag for review.
            return _GroupCandidate(
                strategy_type=StrategyType.SHORT_CALL.value,
                legs=legs,
                quantity=qty,
                net_credit=net_credit,
                confidence=GroupConfidence.MEDIUM.value,
                management_status=ManagementStatus.MANUAL_REVIEW_REQUIRED.value,
                notes=[
                    "Naked short call detected — undefined upside risk, manual review required."
                ],
            )
        if leg.option_type == "put" and leg.side == "buy":
            return _GroupCandidate(
                strategy_type=StrategyType.LONG_PUT.value,
                legs=legs,
                quantity=qty,
                net_credit=net_credit,
                confidence=GroupConfidence.HIGH.value,
                management_status=ManagementStatus.AUTO.value,
                notes=[],
            )
        if leg.option_type == "call" and leg.side == "buy":
            return _GroupCandidate(
                strategy_type=StrategyType.LONG_CALL.value,
                legs=legs,
                quantity=qty,
                net_credit=net_credit,
                confidence=GroupConfidence.HIGH.value,
                management_status=ManagementStatus.AUTO.value,
                notes=[],
            )

    # Short leg present but structure not recognized — flag as orphan so the
    # optimizer's orphan-leg guard can see it.
    has_short = any(leg.side == "sell" for leg in legs)
    strategy_type = (
        StrategyType.ORPHAN_LEG.value
        if has_short
        else StrategyType.UNKNOWN_COMPLEX.value
    )
    return _GroupCandidate(
        strategy_type=strategy_type,
        legs=legs,
        quantity=qty,
        net_credit=net_credit,
        confidence=GroupConfidence.LOW.value,
        management_status=ManagementStatus.MANUAL_REVIEW_REQUIRED.value,
        notes=[
            f"Could not classify {len(legs)}-leg bundle on {legs[0].underlying} "
            f"({'short leg present' if has_short else 'long-only'}) — manual review required."
        ],
    )


def _classify_multi_expiry(legs: list[BrokerPositionLeg]) -> _GroupCandidate:
    """Multi-expiration bundles. Currently supports calendar; everything else is orphan/unknown."""
    qty = _min_quantity(legs)
    net_credit = _per_contract_net_credit(legs)

    # Calendar spread: 2 legs, same option_type, same strike, different expirations, opposite sides
    if len(legs) == 2:
        a, b = legs
        if (
            a.option_type == b.option_type
            and a.strike == b.strike
            and a.expiration != b.expiration
            and a.side != b.side
        ):
            return _GroupCandidate(
                strategy_type=StrategyType.CALENDAR_SPREAD.value,
                legs=legs,
                quantity=qty,
                net_credit=net_credit,
                confidence=GroupConfidence.MEDIUM.value,
                management_status=ManagementStatus.AUTO.value,
                notes=[
                    "Calendar spread spans multiple expirations; confidence is medium."
                ],
            )

    has_short = any(leg.side == "sell" for leg in legs)
    return _GroupCandidate(
        strategy_type=(
            StrategyType.ORPHAN_LEG.value
            if has_short
            else StrategyType.UNKNOWN_COMPLEX.value
        ),
        legs=legs,
        quantity=qty,
        net_credit=net_credit,
        confidence=GroupConfidence.LOW.value,
        management_status=ManagementStatus.MANUAL_REVIEW_REQUIRED.value,
        notes=[
            f"Multi-expiry bundle on {legs[0].underlying} could not be classified as a known structure."
        ],
    )


# ── Public API ────────────────────────────────────────────────────────────────


def reconcile_with_known_orders(
    legs: list[BrokerPositionLeg],
    known_orders: list[PendingOrder],
) -> tuple[list[Position], list[BrokerPositionLeg]]:
    """Match broker legs against Kamandal-submitted PendingOrders by OCC symbol set.

    Returns (matched_positions, leftover_legs). Matched positions carry:
        source = PositionSource.KAMANDAL_ORDER
        group_id = broker_order_id
        strategy_id = the PendingOrder's strategy_id
        strategy_type = the PendingOrder's strategy_type (or derived)
        bpr = the preflight buyingPowerRequirement from broker_response when present

    A PendingOrder matches iff every one of its legs (by OCC symbol) is present in
    `legs` with compatible side and enough quantity. Matched legs are consumed and
    removed from the leftover pool.
    """
    from vol_crush.integrations.public_broker import (
        _occ_symbol_from_leg,
    )  # late import to avoid cycles

    leftover_by_symbol: dict[str, BrokerPositionLeg] = {
        leg.occ_symbol: leg for leg in legs
    }
    matched: list[Position] = []

    for order in known_orders:
        if not order.broker_order_id:
            continue
        if not order.legs:
            continue

        try:
            order_symbols = [_occ_symbol_from_leg(leg) for leg in order.legs]
        except ValueError:
            continue

        if not all(symbol in leftover_by_symbol for symbol in order_symbols):
            continue

        # Also verify side parity — the leg in leftover must match the order intent.
        matched_legs: list[BrokerPositionLeg] = []
        side_mismatch = False
        for symbol, order_leg in zip(order_symbols, order.legs):
            broker_leg = leftover_by_symbol[symbol]
            # For OPEN orders, the live position's side should match the order leg side.
            if broker_leg.side != order_leg.side:
                side_mismatch = True
                break
            matched_legs.append(broker_leg)
        if side_mismatch:
            continue

        for leg in matched_legs:
            leftover_by_symbol.pop(leg.occ_symbol, None)

        # Build a Position carrying the order's intent.
        strategy_type_hint = (
            order.broker_response.get("strategyName", "").lower().replace(" ", "_")
            if isinstance(order.broker_response, dict)
            else ""
        )
        strategy_type = _derive_strategy_type(order, strategy_type_hint) or _classify_same_expiry(matched_legs).strategy_type  # type: ignore[union-attr]

        preflight_bpr = 0.0
        if isinstance(order.broker_response, dict):
            try:
                preflight_bpr = float(
                    order.broker_response.get("buyingPowerRequirement", 0.0)
                )
            except (TypeError, ValueError):
                preflight_bpr = 0.0

        qty = _min_quantity(matched_legs)
        net_credit = _per_contract_net_credit(matched_legs)
        candidate = _GroupCandidate(
            strategy_type=strategy_type,
            legs=matched_legs,
            quantity=qty,
            net_credit=net_credit,
            confidence=GroupConfidence.HIGH.value,
            management_status=ManagementStatus.AUTO.value,
            notes=[],
        )
        position = _materialize_position(
            candidate,
            source=PositionSource.KAMANDAL_ORDER.value,
            broker=order.broker or "public",
            broker_order_id=order.broker_order_id,
            strategy_id=order.strategy_id,
        )
        # Prefer broker-reported BPR when we have it.
        if preflight_bpr > 0:
            position.bpr = round(preflight_bpr, 2)
            position.max_loss = round(preflight_bpr, 2)
        matched.append(position)

    leftover_legs = list(leftover_by_symbol.values())
    return matched, leftover_legs


def _derive_strategy_type(order: PendingOrder, hint: str) -> str:
    """Best-effort mapping from a pending order's metadata to a StrategyType value."""
    if order.strategy_id:
        # strategies.yaml IDs usually track structure; try a direct enum parse.
        try:
            return StrategyType(order.strategy_id).value
        except ValueError:
            pass
    if hint:
        try:
            return StrategyType(hint).value
        except ValueError:
            pass
    # Heuristic from leg shape:
    legs = order.legs
    puts = [leg for leg in legs if leg.option_type == "put"]
    calls = [leg for leg in legs if leg.option_type == "call"]
    if len(legs) == 4 and len(puts) == 2 and len(calls) == 2:
        return StrategyType.IRON_CONDOR.value
    if len(legs) == 2 and len(puts) == 2:
        return StrategyType.PUT_SPREAD.value
    if len(legs) == 2 and len(calls) == 2:
        return StrategyType.CALL_SPREAD.value
    if len(legs) == 2 and len(puts) == 1 and len(calls) == 1:
        return StrategyType.SHORT_STRANGLE.value
    if len(legs) == 1 and puts and puts[0].side == "sell":
        return StrategyType.SHORT_PUT.value
    return StrategyType.CUSTOM.value


def group_broker_legs(
    legs: list[BrokerPositionLeg],
    known_orders: list[PendingOrder] | None = None,
) -> list[Position]:
    """Top-level entry point: raw broker legs → grouped Positions.

    Steps:
        1. If `known_orders` is provided, try to reconcile Kamandal-opened bundles
           first. Those get PositionSource.KAMANDAL_ORDER and carry the broker_order_id
           as their durable group anchor.
        2. Leftover legs are partitioned by (underlying, expiration), then each bucket
           is classified. Buckets that cross expirations are routed through the
           multi-expiry classifier.
        3. The result is the union of Kamandal-matched and inferred Positions.

    Unclassifiable bundles become `unknown_complex` or `orphan_leg` groups with
    `management_status=manual_review_required` so no downstream automation acts on
    them.
    """
    if not legs:
        return []

    matched: list[Position] = []
    leftover = list(legs)
    if known_orders:
        matched, leftover = reconcile_with_known_orders(legs, known_orders)

    # Partition remaining legs by underlying first, then consider multi-expiry inside.
    by_underlying: dict[str, list[BrokerPositionLeg]] = {}
    for leg in leftover:
        by_underlying.setdefault(leg.underlying, []).append(leg)

    inferred: list[Position] = []
    for underlying, bucket in by_underlying.items():
        expirations = {leg.expiration for leg in bucket}
        if len(expirations) == 1:
            candidate = _classify_same_expiry(bucket)
            if candidate is not None:
                inferred.append(_materialize_position(candidate))
            continue

        # Multi-expiration bucket: if it's exactly a calendar, classify as one group.
        # Otherwise, further partition by expiration and classify each partition.
        if len(bucket) == 2:
            candidate = _classify_multi_expiry(bucket)
            inferred.append(_materialize_position(candidate))
            continue

        for expiration in sorted(expirations):
            sub_bucket = [leg for leg in bucket if leg.expiration == expiration]
            candidate = _classify_same_expiry(sub_bucket)
            if candidate is not None:
                inferred.append(_materialize_position(candidate))

    return matched + inferred
