"""Operator-facing strategy names and canonical internal strategy types."""

from __future__ import annotations


_ALIASES = {
    "put_vertical": "put_spread",
    "put_pertical": "put_spread",
    "put_spread": "put_spread",
    "short_put_spread": "put_spread",
    "bull_put_spread": "put_spread",
    "call_vertical": "call_spread",
    "call_pertical": "call_spread",
    "call_spread": "call_spread",
    "short_call_spread": "call_spread",
    "bear_call_spread": "call_spread",
    "strangle": "short_strangle",
    "short_strangle": "short_strangle",
    "straddle": "straddle",
    "short_straddle": "straddle",
    "put_calendar": "calendar_spread",
    "call_calendar": "calendar_spread",
    "calendar": "calendar_spread",
    "calendar_spread": "calendar_spread",
    "iron_condor": "iron_condor",
    "jade_lizard": "jade_lizard",
    "short_put": "short_put",
    "cash_secured_put": "short_put",
    "short_call": "short_call",
    "long_put": "long_put",
    "long_call": "long_call",
}

_DISPLAY = {
    "put_spread": "put_vertical",
    "call_spread": "call_vertical",
    "short_strangle": "strangle",
    "calendar_spread": "put_calendar",
}


def normalize_key(value: str) -> str:
    return "_".join(str(value or "").strip().lower().replace("-", "_").split())


def canonical_strategy_type(value: str) -> str:
    """Map sheet/LLM vocabulary to StrategyType values where possible."""
    key = normalize_key(value)
    return _ALIASES.get(key, key)


def operator_strategy_label(value: str) -> str:
    """Prefer the shorter sheet vocabulary for canonical strategy types."""
    canonical = canonical_strategy_type(value)
    return _DISPLAY.get(canonical, normalize_key(value))


def infer_expectation(value: str) -> str:
    """Best-effort directional label for operator-facing review sheets."""
    canonical = canonical_strategy_type(value)
    if canonical in {"short_strangle", "straddle", "iron_condor"}:
        return "neutral"
    if canonical in {"short_put", "put_spread", "jade_lizard", "long_call"}:
        return "bullish"
    if canonical in {"short_call", "call_spread", "long_put"}:
        return "bearish"
    return ""


def strategy_profile_key(strategy_id: str) -> tuple[str, str]:
    """Return (template_or_structure, stock_profile) from a runtime strategy id."""
    if ":" not in strategy_id:
        return strategy_id, ""
    left, _, right = strategy_id.partition(":")
    return left, right
