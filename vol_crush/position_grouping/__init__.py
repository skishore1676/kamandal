"""Deterministic grouping of raw broker legs into strategy-level Positions.

This module is the bridge between what the broker reports (individual legs) and what
Kamandal's trading brain reasons about (strategy bundles). It is pure logic — no
storage, no network, no config mutation — and is safe to import from anywhere.

Public surface:
    group_broker_legs(legs, ...) -> list[Position]
    estimate_bpr(strategy_type, legs, net_credit, quantity) -> float

Design notes live in docs/PROJECT_DOCUMENT.md §Position Grouping.
"""

from vol_crush.position_grouping.bpr import estimate_bpr, estimate_max_profit
from vol_crush.position_grouping.service import group_broker_legs

__all__ = ["group_broker_legs", "estimate_bpr", "estimate_max_profit"]
