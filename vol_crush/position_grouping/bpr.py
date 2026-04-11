"""Conservative buying-power / max-loss formulas per strategy structure.

All functions return dollar amounts (not per-share, not per-contract premium).
Conventions:
    - `width` is the difference between strike prices (dollars, not cents).
    - `net_credit` is the net dollar credit received opening the structure at
      quantity=1 (negative if a debit).
    - `quantity` is the number of contracts (1 contract = 100 multiplier).
    - Formulas are intentionally conservative fallbacks. When Public preflight
      returns a broker-computed `buyingPowerRequirement`, that value is always
      preferred over these formulas.
"""

from __future__ import annotations

from vol_crush.core.models import OptionLeg, StrategyType

CONTRACT_MULTIPLIER = 100.0


def _width(strikes: list[float]) -> float:
    if len(strikes) < 2:
        return 0.0
    return abs(max(strikes) - min(strikes))


def _put_strikes(legs: list[OptionLeg]) -> list[float]:
    return [leg.strike for leg in legs if leg.option_type == "put"]


def _call_strikes(legs: list[OptionLeg]) -> list[float]:
    return [leg.strike for leg in legs if leg.option_type == "call"]


def estimate_bpr(
    strategy_type: str,
    legs: list[OptionLeg],
    net_credit: float,
    quantity: int = 1,
) -> float:
    """Return a conservative BPR / max-loss estimate for a strategy group.

    The returned value is the dollar buying-power reduction. For defined-risk
    credit structures this equals max_loss. For undefined-risk structures we use
    a generous heuristic floor — callers should prefer Public preflight when
    available.
    """
    qty = max(int(quantity), 1)

    if strategy_type == StrategyType.PUT_SPREAD.value:
        width = _width([leg.strike for leg in legs if leg.option_type == "put"])
        if net_credit >= 0:
            # credit put spread (bull put spread)
            return max(
                width * CONTRACT_MULTIPLIER * qty
                - net_credit * CONTRACT_MULTIPLIER * qty,
                0.0,
            )
        # debit put spread (bear put spread): max loss is the debit paid
        return abs(net_credit) * CONTRACT_MULTIPLIER * qty

    if strategy_type == StrategyType.CALL_SPREAD.value:
        width = _width([leg.strike for leg in legs if leg.option_type == "call"])
        if net_credit >= 0:
            # credit call spread (bear call spread)
            return max(
                width * CONTRACT_MULTIPLIER * qty
                - net_credit * CONTRACT_MULTIPLIER * qty,
                0.0,
            )
        # debit call spread (bull call spread): max loss is the debit paid
        return abs(net_credit) * CONTRACT_MULTIPLIER * qty

    if strategy_type == StrategyType.IRON_CONDOR.value:
        put_width = _width(_put_strikes(legs))
        call_width = _width(_call_strikes(legs))
        wider = max(put_width, call_width)
        return max(
            wider * CONTRACT_MULTIPLIER * qty - net_credit * CONTRACT_MULTIPLIER * qty,
            0.0,
        )

    if strategy_type == StrategyType.JADE_LIZARD.value:
        # Short put risk is max(strike * 100, naked_put_fallback). Call spread risk
        # should be fully covered by premium collected by construction — validated
        # at classification time. Conservative fallback: naked short put risk.
        short_put_strikes = [
            leg.strike
            for leg in legs
            if leg.option_type == "put" and leg.side == "sell"
        ]
        if not short_put_strikes:
            return 0.0
        return (
            short_put_strikes[0] * CONTRACT_MULTIPLIER * qty * 0.2
        )  # tastytrade-style 20% rule of thumb

    if (
        strategy_type == StrategyType.SHORT_STRANGLE.value
        or strategy_type == StrategyType.STRADDLE.value
    ):
        # Undefined risk. tastytrade 20%-of-underlying rule of thumb.
        short_strikes = [leg.strike for leg in legs if leg.side == "sell"]
        if not short_strikes:
            return 0.0
        anchor = sum(short_strikes) / len(short_strikes)
        return anchor * CONTRACT_MULTIPLIER * qty * 0.20

    if strategy_type == StrategyType.SHORT_PUT.value:
        short_puts = [
            leg for leg in legs if leg.option_type == "put" and leg.side == "sell"
        ]
        if not short_puts:
            return 0.0
        return short_puts[0].strike * CONTRACT_MULTIPLIER * qty * 0.20

    if strategy_type == StrategyType.SHORT_CALL.value:
        short_calls = [
            leg for leg in legs if leg.option_type == "call" and leg.side == "sell"
        ]
        if not short_calls:
            return 0.0
        return short_calls[0].strike * CONTRACT_MULTIPLIER * qty * 0.20

    if strategy_type in (StrategyType.LONG_PUT.value, StrategyType.LONG_CALL.value):
        # Long options: risk is the debit paid.
        return abs(net_credit) * CONTRACT_MULTIPLIER * qty

    if strategy_type == StrategyType.CALENDAR_SPREAD.value:
        # Calendar risk is the net debit paid.
        return abs(net_credit) * CONTRACT_MULTIPLIER * qty

    if strategy_type == StrategyType.COVERED_STRANGLE.value:
        # Stock-backed; the short call is covered, the short put is effectively cash-secured.
        short_puts = [
            leg for leg in legs if leg.option_type == "put" and leg.side == "sell"
        ]
        if not short_puts:
            return 0.0
        return short_puts[0].strike * CONTRACT_MULTIPLIER * qty

    # ORPHAN_LEG / UNKNOWN_COMPLEX / CUSTOM fall through:
    # We don't know the structure well enough to compute risk. Use a deliberately
    # conservative floor so aggregate BPR still reflects that this unknown bundle
    # consumes risk budget. The optimizer is also gated by max_orphan_legs, but this
    # protects users who intentionally allow a small number of manual-review groups.
    short_strikes = [leg.strike for leg in legs if leg.side == "sell"]
    if short_strikes:
        anchor = max(short_strikes)
        return anchor * CONTRACT_MULTIPLIER * qty
    long_cost = abs(net_credit) * CONTRACT_MULTIPLIER * qty
    return long_cost if long_cost else CONTRACT_MULTIPLIER * qty


def estimate_max_profit(
    strategy_type: str,
    legs: list[OptionLeg],
    net_credit: float,
    quantity: int = 1,
) -> float:
    """Return max profit in dollars for a strategy group, or 0.0 if undefined/unbounded."""
    qty = max(int(quantity), 1)

    if strategy_type in (StrategyType.PUT_SPREAD.value, StrategyType.CALL_SPREAD.value):
        # Credit spread: max profit = credit received. Debit spread: max profit = width - debit.
        if net_credit >= 0:
            return net_credit * CONTRACT_MULTIPLIER * qty
        strikes = (
            _put_strikes(legs)
            if strategy_type == StrategyType.PUT_SPREAD.value
            else _call_strikes(legs)
        )
        width = _width(strikes)
        return max(
            width * CONTRACT_MULTIPLIER * qty
            - abs(net_credit) * CONTRACT_MULTIPLIER * qty,
            0.0,
        )

    if strategy_type == StrategyType.IRON_CONDOR.value:
        return max(net_credit * CONTRACT_MULTIPLIER * qty, 0.0)

    if strategy_type in (
        StrategyType.SHORT_STRANGLE.value,
        StrategyType.STRADDLE.value,
        StrategyType.SHORT_PUT.value,
        StrategyType.SHORT_CALL.value,
        StrategyType.JADE_LIZARD.value,
    ):
        return max(net_credit * CONTRACT_MULTIPLIER * qty, 0.0)

    if strategy_type in (StrategyType.LONG_PUT.value, StrategyType.LONG_CALL.value):
        # Unbounded on the upside (for a call) or capped by strike*100 (for a put).
        # Treat as undefined for max_profit reporting.
        return 0.0

    return 0.0
