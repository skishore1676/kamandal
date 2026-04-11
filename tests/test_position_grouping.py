"""Tests for the deterministic position-grouping classifier.

Covers every strategy type we classify, plus the escape hatches: unknown_complex,
orphan_leg, and manual_review flagging. Each test builds raw BrokerPositionLeg
objects (the audit floor) and asserts the grouped Position result.
"""

from __future__ import annotations

from vol_crush.core.models import (
    BrokerPositionLeg,
    GroupConfidence,
    Greeks,
    ManagementStatus,
    PendingOrder,
    PositionSource,
    StrategyType,
    TradeAction,
    OptionLeg,
)
from vol_crush.position_grouping import (
    estimate_bpr,
    estimate_max_profit,
    group_broker_legs,
)
from vol_crush.position_grouping.service import reconcile_with_known_orders


def _leg(
    *,
    symbol: str,
    underlying: str,
    expiration: str,
    strike: float,
    option_type: str,
    side: str,
    quantity: int = 1,
    unit_cost: float = 100.0,
    greeks: Greeks | None = None,
) -> BrokerPositionLeg:
    signed = float(quantity) if side == "buy" else -float(quantity)
    return BrokerPositionLeg(
        leg_id=f"public:acct:{symbol}",
        broker="public",
        account_id="acct",
        occ_symbol=symbol,
        underlying=underlying,
        expiration=expiration,
        strike=strike,
        option_type=option_type,
        side=side,
        quantity=quantity,
        signed_quantity=signed,
        current_value=unit_cost * (1 if side == "buy" else -1),
        total_cost=unit_cost * (1 if side == "buy" else -1),
        unit_cost=unit_cost,
        greeks=greeks or Greeks(),
    )


# ── Iron condor ──────────────────────────────────────────────────────────────


def test_iron_condor_classification() -> None:
    legs = [
        _leg(
            symbol="AAPL260515P00180000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=180.0,
            option_type="put",
            side="buy",
            unit_cost=50.0,
        ),
        _leg(
            symbol="AAPL260515P00185000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=185.0,
            option_type="put",
            side="sell",
            unit_cost=120.0,
        ),
        _leg(
            symbol="AAPL260515C00205000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=205.0,
            option_type="call",
            side="sell",
            unit_cost=110.0,
        ),
        _leg(
            symbol="AAPL260515C00210000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=210.0,
            option_type="call",
            side="buy",
            unit_cost=45.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    group = groups[0]
    assert group.strategy_type == StrategyType.IRON_CONDOR.value
    assert group.confidence == GroupConfidence.HIGH.value
    assert group.management_status == ManagementStatus.AUTO.value
    assert group.underlying == "AAPL"
    assert group.expirations == ["2026-05-15"]
    assert len(group.legs) == 4
    # Wider wing = 5, credit = sum of (shorts - longs) per-contract = (1.20 + 1.10 - 0.50 - 0.45) = 1.35
    # max_loss = 5*100 - 1.35*100 = 365
    assert abs(group.max_loss - 365.0) < 0.01
    # max_profit = credit * 100 = 135
    assert abs(group.max_profit - 135.0) < 0.01
    # group_id should be derived from legs and deterministic
    assert "AAPL" in group.position_id
    assert group.source == PositionSource.PUBLIC_INFERRED.value


# ── Put spread (credit) ──────────────────────────────────────────────────────


def test_credit_put_spread_classification() -> None:
    # Bull put spread: sell higher-strike put, buy lower-strike put. Net credit.
    legs = [
        _leg(
            symbol="SPY260515P00510000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=510.0,
            option_type="put",
            side="buy",
            unit_cost=90.0,
        ),
        _leg(
            symbol="SPY260515P00515000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=515.0,
            option_type="put",
            side="sell",
            unit_cost=150.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    group = groups[0]
    assert group.strategy_type == StrategyType.PUT_SPREAD.value
    assert group.confidence == GroupConfidence.HIGH.value
    # Net credit per contract = 1.50 - 0.90 = 0.60
    assert abs(group.open_credit - 0.60) < 0.01
    # max_loss = width (5) * 100 - credit (0.60) * 100 = 440
    assert abs(group.max_loss - 440.0) < 0.01
    # max_profit = credit * 100 = 60
    assert abs(group.max_profit - 60.0) < 0.01


# ── Call spread (debit) ──────────────────────────────────────────────────────


def test_debit_call_spread_max_loss_is_debit() -> None:
    # Bull call debit spread: buy lower-strike call, sell higher-strike call. Net debit.
    legs = [
        _leg(
            symbol="AAPL260515C00200000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=200.0,
            option_type="call",
            side="buy",
            unit_cost=850.0,
        ),
        _leg(
            symbol="AAPL260515C00210000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=210.0,
            option_type="call",
            side="sell",
            unit_cost=150.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    group = groups[0]
    assert group.strategy_type == StrategyType.CALL_SPREAD.value
    # Net per-contract: 1.50 (credit from short) - 8.50 (debit for long) = -7.00
    assert group.open_credit < 0
    # Debit spread: max_loss is the debit paid = $700
    assert abs(group.max_loss - 700.0) < 0.01
    # max_profit = width*100 - debit*100 = 1000 - 700 = 300
    assert abs(group.max_profit - 300.0) < 0.01


# ── Short strangle ──────────────────────────────────────────────────────────


def test_short_strangle_classification() -> None:
    legs = [
        _leg(
            symbol="SPY260515P00515000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=515.0,
            option_type="put",
            side="sell",
            unit_cost=200.0,
        ),
        _leg(
            symbol="SPY260515C00525000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=525.0,
            option_type="call",
            side="sell",
            unit_cost=210.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    group = groups[0]
    assert group.strategy_type == StrategyType.SHORT_STRANGLE.value
    assert group.confidence == GroupConfidence.HIGH.value
    assert group.management_status == ManagementStatus.AUTO.value
    # Undefined-risk fallback BPR: 20% of avg short strike * 100 * qty = 0.20 * 520 * 100 = 10400
    assert group.bpr > 0
    # Max profit = credit received = 4.10 * 100 = 410
    assert abs(group.max_profit - 410.0) < 0.01


def test_straddle_classification() -> None:
    legs = [
        _leg(
            symbol="SPY260515P00520000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=520.0,
            option_type="put",
            side="sell",
            unit_cost=280.0,
        ),
        _leg(
            symbol="SPY260515C00520000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=520.0,
            option_type="call",
            side="sell",
            unit_cost=290.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    assert groups[0].strategy_type == StrategyType.STRADDLE.value


# ── Jade lizard ─────────────────────────────────────────────────────────────


def test_jade_lizard_with_credit_above_width_is_high_confidence() -> None:
    # short put + short call + long call; net credit > call spread width => no upside risk
    legs = [
        _leg(
            symbol="AAPL260515P00185000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=185.0,
            option_type="put",
            side="sell",
            unit_cost=300.0,
        ),
        _leg(
            symbol="AAPL260515C00205000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=205.0,
            option_type="call",
            side="sell",
            unit_cost=250.0,
        ),
        _leg(
            symbol="AAPL260515C00210000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=210.0,
            option_type="call",
            side="buy",
            unit_cost=50.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    assert groups[0].strategy_type == StrategyType.JADE_LIZARD.value
    # net credit = 3.00 + 2.50 - 0.50 = 5.00 > call width 5 (tied is treated as >= width)
    assert groups[0].open_credit >= 5.0
    assert groups[0].confidence == GroupConfidence.HIGH.value


def test_jade_lizard_with_credit_below_width_is_medium_confidence() -> None:
    legs = [
        _leg(
            symbol="AAPL260515P00185000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=185.0,
            option_type="put",
            side="sell",
            unit_cost=100.0,
        ),
        _leg(
            symbol="AAPL260515C00205000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=205.0,
            option_type="call",
            side="sell",
            unit_cost=100.0,
        ),
        _leg(
            symbol="AAPL260515C00210000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=210.0,
            option_type="call",
            side="buy",
            unit_cost=50.0,
        ),
    ]
    groups = group_broker_legs(legs)

    group = groups[0]
    assert group.strategy_type == StrategyType.JADE_LIZARD.value
    # Net credit = 1 + 1 - 0.5 = 1.50 which is below call spread width (5)
    assert group.confidence == GroupConfidence.MEDIUM.value


# ── Single-leg positions ────────────────────────────────────────────────────


def test_naked_short_put_is_auto_managed() -> None:
    legs = [
        _leg(
            symbol="SPY260515P00515000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=515.0,
            option_type="put",
            side="sell",
            unit_cost=200.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    assert groups[0].strategy_type == StrategyType.SHORT_PUT.value
    assert groups[0].confidence == GroupConfidence.HIGH.value
    assert groups[0].management_status == ManagementStatus.AUTO.value


def test_naked_short_call_requires_manual_review() -> None:
    """Naked short calls have undefined upside risk. Classify but refuse to auto-manage."""
    legs = [
        _leg(
            symbol="SPY260515C00525000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=525.0,
            option_type="call",
            side="sell",
            unit_cost=210.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    group = groups[0]
    assert group.strategy_type == StrategyType.SHORT_CALL.value
    assert group.management_status == ManagementStatus.MANUAL_REVIEW_REQUIRED.value
    assert group.confidence == GroupConfidence.MEDIUM.value


def test_long_put_classification() -> None:
    legs = [
        _leg(
            symbol="SPY260515P00510000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=510.0,
            option_type="put",
            side="buy",
            unit_cost=90.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    assert groups[0].strategy_type == StrategyType.LONG_PUT.value


# ── Calendar ────────────────────────────────────────────────────────────────


def test_calendar_spread_classification() -> None:
    legs = [
        _leg(
            symbol="SPY260417P00520000",
            underlying="SPY",
            expiration="2026-04-17",
            strike=520.0,
            option_type="put",
            side="sell",
            unit_cost=100.0,
        ),
        _leg(
            symbol="SPY260515P00520000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=520.0,
            option_type="put",
            side="buy",
            unit_cost=150.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    group = groups[0]
    assert group.strategy_type == StrategyType.CALENDAR_SPREAD.value
    assert group.confidence == GroupConfidence.MEDIUM.value
    assert sorted(group.expirations) == ["2026-04-17", "2026-05-15"]


# ── Orphan / unknown_complex ────────────────────────────────────────────────


def test_unrecognizable_bundle_is_flagged_manual_review() -> None:
    # 3 legs that don't match any known structure (e.g. random butterfly-ish mix)
    legs = [
        _leg(
            symbol="IWM260515P00200000",
            underlying="IWM",
            expiration="2026-05-15",
            strike=200.0,
            option_type="put",
            side="sell",
            unit_cost=100.0,
        ),
        _leg(
            symbol="IWM260515P00195000",
            underlying="IWM",
            expiration="2026-05-15",
            strike=195.0,
            option_type="put",
            side="sell",
            unit_cost=80.0,
        ),
        _leg(
            symbol="IWM260515C00210000",
            underlying="IWM",
            expiration="2026-05-15",
            strike=210.0,
            option_type="call",
            side="buy",
            unit_cost=50.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    group = groups[0]
    assert group.strategy_type in (
        StrategyType.ORPHAN_LEG.value,
        StrategyType.UNKNOWN_COMPLEX.value,
    )
    assert group.management_status == ManagementStatus.MANUAL_REVIEW_REQUIRED.value
    assert group.confidence == GroupConfidence.LOW.value


def test_mixed_expirations_partition_when_no_calendar_match() -> None:
    """Two legs on different expirations that aren't a calendar should still be
    routed through the multi-expiry classifier (where they fall to orphan/unknown)."""
    legs = [
        _leg(
            symbol="IWM260417P00200000",
            underlying="IWM",
            expiration="2026-04-17",
            strike=200.0,
            option_type="put",
            side="sell",
            unit_cost=80.0,
        ),
        _leg(
            symbol="IWM260515C00210000",
            underlying="IWM",
            expiration="2026-05-15",
            strike=210.0,
            option_type="call",
            side="sell",
            unit_cost=70.0,
        ),
    ]
    groups = group_broker_legs(legs)

    assert len(groups) == 1
    assert groups[0].management_status == ManagementStatus.MANUAL_REVIEW_REQUIRED.value


# ── Kamandal-opened round trip ──────────────────────────────────────────────


def test_reconcile_with_known_orders_uses_broker_order_id_as_anchor() -> None:
    legs = [
        _leg(
            symbol="AAPL260515P00180000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=180.0,
            option_type="put",
            side="buy",
            unit_cost=50.0,
        ),
        _leg(
            symbol="AAPL260515P00185000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=185.0,
            option_type="put",
            side="sell",
            unit_cost=120.0,
        ),
        _leg(
            symbol="AAPL260515C00205000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=205.0,
            option_type="call",
            side="sell",
            unit_cost=110.0,
        ),
        _leg(
            symbol="AAPL260515C00210000",
            underlying="AAPL",
            expiration="2026-05-15",
            strike=210.0,
            option_type="call",
            side="buy",
            unit_cost=45.0,
        ),
    ]
    known_order = PendingOrder(
        pending_order_id="pending_xyz",
        plan_id="plan_1",
        created_at="2026-04-03T12:00:00Z",
        action=TradeAction.OPEN,
        status="pending",
        underlying="AAPL",
        strategy_id="iron_condor_core",
        quantity=1,
        target_price=1.35,
        estimated_credit=135.0,
        estimated_bpr=365.0,
        greeks_impact=Greeks(),
        legs=[
            OptionLeg("AAPL", "2026-05-15", 180.0, "put", "buy"),
            OptionLeg("AAPL", "2026-05-15", 185.0, "put", "sell"),
            OptionLeg("AAPL", "2026-05-15", 205.0, "call", "sell"),
            OptionLeg("AAPL", "2026-05-15", 210.0, "call", "buy"),
        ],
        broker="public",
        broker_order_id="anchor-uuid-abc",
        broker_response={
            "strategyName": "Iron Condor",
            "buyingPowerRequirement": "365.00",
        },
    )

    matched, leftover = reconcile_with_known_orders(legs, [known_order])

    assert len(matched) == 1
    assert not leftover
    group = matched[0]
    assert group.source == PositionSource.KAMANDAL_ORDER.value
    assert group.broker_order_id == "anchor-uuid-abc"
    assert group.group_id == "anchor-uuid-abc"
    assert group.strategy_id == "iron_condor_core"
    # Preflight BPR from the order's broker_response should take over
    assert group.bpr == 365.0
    assert group.max_loss == 365.0
    assert group.management_status == ManagementStatus.AUTO.value


def test_group_broker_legs_prefers_known_orders_over_inference() -> None:
    """Same legs, but with a known order anchor — should get source=kamandal_order."""
    legs = [
        _leg(
            symbol="SPY260515P00510000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=510.0,
            option_type="put",
            side="buy",
            unit_cost=90.0,
        ),
        _leg(
            symbol="SPY260515P00515000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=515.0,
            option_type="put",
            side="sell",
            unit_cost=150.0,
        ),
    ]
    known_order = PendingOrder(
        pending_order_id="pending_1",
        plan_id="plan_1",
        created_at="2026-04-03T12:00:00Z",
        action=TradeAction.OPEN,
        status="pending",
        underlying="SPY",
        strategy_id="spy_put_spread",
        quantity=1,
        target_price=0.60,
        estimated_credit=60.0,
        estimated_bpr=440.0,
        greeks_impact=Greeks(),
        legs=[
            OptionLeg("SPY", "2026-05-15", 510.0, "put", "buy"),
            OptionLeg("SPY", "2026-05-15", 515.0, "put", "sell"),
        ],
        broker="public",
        broker_order_id="spread-anchor-1",
        broker_response={"buyingPowerRequirement": "440.00"},
    )

    groups = group_broker_legs(legs, known_orders=[known_order])

    assert len(groups) == 1
    assert groups[0].source == PositionSource.KAMANDAL_ORDER.value
    assert groups[0].broker_order_id == "spread-anchor-1"


def test_group_broker_legs_falls_back_to_inference_when_no_anchor_match() -> None:
    """When the known-order legs don't match the live legs, inference takes over."""
    live_legs = [
        _leg(
            symbol="SPY260515P00510000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=510.0,
            option_type="put",
            side="buy",
            unit_cost=90.0,
        ),
        _leg(
            symbol="SPY260515P00515000",
            underlying="SPY",
            expiration="2026-05-15",
            strike=515.0,
            option_type="put",
            side="sell",
            unit_cost=150.0,
        ),
    ]
    unrelated_order = PendingOrder(
        pending_order_id="pending_99",
        plan_id="plan_99",
        created_at="2026-04-03T12:00:00Z",
        action=TradeAction.OPEN,
        status="pending",
        underlying="SPY",
        strategy_id="spy_put_spread",
        quantity=1,
        target_price=0.60,
        estimated_credit=60.0,
        estimated_bpr=440.0,
        greeks_impact=Greeks(),
        legs=[
            OptionLeg("SPY", "2026-06-19", 510.0, "put", "buy"),  # different expiry
            OptionLeg("SPY", "2026-06-19", 515.0, "put", "sell"),
        ],
        broker="public",
        broker_order_id="different-anchor",
    )

    groups = group_broker_legs(live_legs, known_orders=[unrelated_order])

    assert len(groups) == 1
    assert groups[0].source == PositionSource.PUBLIC_INFERRED.value


# ── BPR edge cases ──────────────────────────────────────────────────────────


def test_estimate_bpr_unknown_short_strategy_uses_conservative_floor() -> None:
    legs = [
        OptionLeg("IWM", "2026-05-15", 200.0, "put", "sell"),
        OptionLeg("IWM", "2026-05-15", 210.0, "call", "buy"),
    ]
    assert estimate_bpr("banana_spread", legs, 0.5, 1) == 20000.0


def test_estimate_max_profit_for_iron_condor() -> None:
    legs = [
        OptionLeg("AAPL", "2026-05-15", 180.0, "put", "buy"),
        OptionLeg("AAPL", "2026-05-15", 185.0, "put", "sell"),
        OptionLeg("AAPL", "2026-05-15", 205.0, "call", "sell"),
        OptionLeg("AAPL", "2026-05-15", 210.0, "call", "buy"),
    ]
    # credit 1.35 per contract, 1 contract
    assert (
        abs(estimate_max_profit(StrategyType.IRON_CONDOR.value, legs, 1.35, 1) - 135.0)
        < 1e-6
    )
