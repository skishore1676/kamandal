"""Sync live broker portfolio state into the local Kamandal store."""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vol_crush.core.config import load_config
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import Greeks, OptionLeg, PortfolioSnapshot, Position
from vol_crush.integrations.public_broker import PublicBrokerAdapter, parse_occ_symbol
from vol_crush.integrations.storage import LocalStore, build_local_store

logger = logging.getLogger("vol_crush.portfolio_sync")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int_quantity(value: Any) -> int:
    quantity = _safe_float(value, 0.0)
    rounded = int(round(abs(quantity)))
    return max(rounded, 1) if quantity else 0


def _days_to_expiration(expiration: str) -> int:
    try:
        expiry = datetime.fromisoformat(expiration).date()
    except ValueError:
        return 0
    today = datetime.now(UTC).date()
    return max((expiry - today).days, 0)


def _equity_total(portfolio: dict[str, Any]) -> float:
    entries = portfolio.get("equity", []) or []
    total = sum(_safe_float(item.get("value")) for item in entries)
    if total:
        return total
    buying_power = portfolio.get("buyingPower", {}) or {}
    return _safe_float(buying_power.get("buyingPower"))


def _position_from_public(raw_position: dict[str, Any], greeks_by_symbol: dict[str, dict[str, Any]]) -> Position | None:
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
    greeks_payload = greeks_by_symbol.get(symbol, {})
    sign = 1.0 if quantity_signed > 0 else -1.0
    multiplier = abs(quantity_signed)

    current_value_total = abs(_safe_float(raw_position.get("currentValue")))
    cost_basis = raw_position.get("costBasis", {}) or {}
    total_cost = abs(_safe_float(cost_basis.get("totalCost"), current_value_total))
    unit_cost = abs(_safe_float(cost_basis.get("unitCost"), total_cost))
    price_divisor = max(abs(quantity_signed) * 100.0, 100.0)
    current_value = current_value_total / price_divisor
    open_credit = unit_cost / 100.0 if unit_cost else total_cost / price_divisor

    return Position(
        position_id=f"public:{symbol}",
        underlying=parsed["underlying"],
        strategy_id="public_imported_option",
        legs=[
            OptionLeg(
                underlying=parsed["underlying"],
                expiration=parsed["expiration"],
                strike=parsed["strike"],
                option_type=parsed["option_type"],
                side=side,
                quantity=quantity,
            )
        ],
        open_date="",
        open_credit=round(open_credit, 4),
        current_value=round(current_value, 4),
        greeks=Greeks(
            delta=_safe_float(greeks_payload.get("delta")) * sign * multiplier,
            gamma=_safe_float(greeks_payload.get("gamma")) * sign * multiplier,
            theta=_safe_float(greeks_payload.get("theta")) * sign * multiplier,
            vega=_safe_float(greeks_payload.get("vega")) * sign * multiplier,
        ),
        dte_remaining=_days_to_expiration(parsed["expiration"]),
        pnl_pct=_safe_float(cost_basis.get("gainPercentage")),
        bpr=round(max(current_value_total, total_cost), 2),
        status="open",
    )


def _snapshot_from_positions(portfolio: dict[str, Any], positions: list[Position]) -> PortfolioSnapshot:
    net_liquidation_value = _equity_total(portfolio)
    greeks = Greeks()
    bpr_used = 0.0
    for position in positions:
        greeks = greeks + position.greeks
        bpr_used += position.bpr
    theta_pct = (greeks.theta * 100.0 / net_liquidation_value) if net_liquidation_value else 0.0
    gamma_theta_ratio = abs(greeks.gamma / greeks.theta) if greeks.theta else 0.0
    return PortfolioSnapshot(
        timestamp=datetime.now(UTC).isoformat(),
        net_liquidation_value=round(net_liquidation_value, 4),
        greeks=greeks,
        beta_weighted_delta=greeks.delta,
        bpr_used=round(bpr_used, 4),
        bpr_used_pct=round((bpr_used / net_liquidation_value) * 100.0, 4) if net_liquidation_value else 0.0,
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

    portfolio = adapter.get_portfolio()
    raw_positions = portfolio.get("positions", []) or []
    option_symbols = [
        str((item.get("instrument", {}) or {}).get("symbol", ""))
        for item in raw_positions
        if str((item.get("instrument", {}) or {}).get("type", "")).upper() == "OPTION"
    ]
    greeks_by_symbol = adapter.get_option_greeks_batch(option_symbols)
    positions = [
        position
        for position in (
            _position_from_public(raw_position, greeks_by_symbol)
            for raw_position in raw_positions
        )
        if position is not None
    ]
    snapshot = _snapshot_from_positions(portfolio, positions)
    store.save_positions(positions)
    store.save_portfolio_snapshot(snapshot)
    logger.info("Synced %d Public option positions into local store", len(positions))
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Public portfolio into Kamandal store")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    snapshot = sync_public_portfolio(config)
    logger.info(
        "Portfolio sync complete: NLV=%.2f, positions=%d, delta=%.4f, theta=%.4f",
        snapshot.net_liquidation_value,
        snapshot.position_count,
        snapshot.greeks.delta,
        snapshot.greeks.theta,
    )
