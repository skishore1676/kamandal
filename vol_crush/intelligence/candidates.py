"""Guarded agent-style candidate generation for shadow-mode exploration."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

from vol_crush.core.interfaces import MarketDataProvider
from vol_crush.core.models import MarketRegime, RegimePolicy, Strategy, TradeIdea

AGENT_SOURCE_URL = "kamandal://agent/opportunity"
AGENT_SHOW_NAME = "agent_generated"


def _execution_mode(config: dict[str, Any]) -> str:
    raw = str((config.get("execution") or {}).get("mode", "")).lower()
    return "shadow" if raw == "pending" else raw


def agent_candidates_enabled(config: dict[str, Any]) -> bool:
    cfg = (config.get("intelligence") or {}).get("agent_candidates") or {}
    return _execution_mode(config) == "shadow" and bool(cfg.get("enabled", False))


def _stable_idea_id(*parts: str) -> str:
    raw = "|".join(str(part or "") for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"agent_{digest}"


def _allowed_structures(config: dict[str, Any], policy: RegimePolicy) -> set[str]:
    cfg = (config.get("intelligence") or {}).get("agent_candidates") or {}
    configured = {str(item) for item in cfg.get("allowed_structures", []) or []}
    if configured:
        return configured
    preferred = {str(item) for item in policy.prefer_structures}
    if preferred:
        return preferred
    return set()


def _strategy_matches_symbol(strategy: Strategy, symbol: str) -> bool:
    underlyings = strategy.filters.underlyings or []
    return not underlyings or symbol in underlyings


def _strategy_passes_regime(
    strategy: Strategy,
    regime: MarketRegime,
    policy: RegimePolicy,
) -> bool:
    if strategy.allowed_regimes and regime.value not in strategy.allowed_regimes:
        return False
    structure = strategy.structure.value
    if policy.prefer_structures and structure not in policy.prefer_structures:
        return False
    if structure in policy.avoid_structures:
        return False
    return True


def _snapshot_passes_strategy(
    snapshot, strategy: Strategy, policy: RegimePolicy
) -> bool:
    if strategy.avoid_earnings and snapshot.event_risk:
        return False
    if policy.reject_event_risk and snapshot.event_risk:
        return False
    iv_rank_min = strategy.filters.iv_rank_min
    iv_rank_max = strategy.filters.iv_rank_max
    if iv_rank_min is not None and snapshot.iv_rank < iv_rank_min:
        return False
    if iv_rank_max is not None and snapshot.iv_rank > iv_rank_max:
        return False
    if policy.min_iv_rank is not None and snapshot.iv_rank < policy.min_iv_rank:
        return False
    if policy.max_iv_rank is not None and snapshot.iv_rank > policy.max_iv_rank:
        return False
    if not snapshot.option_snapshots:
        return False
    return True


def generate_agent_trade_ideas(
    config: dict[str, Any],
    *,
    strategies: list[Strategy],
    provider: MarketDataProvider,
    regime: MarketRegime,
    policy: RegimePolicy,
) -> list[TradeIdea]:
    """Generate shadow-only internal candidates from regime + configured playbooks.

    This is intentionally deterministic. It gives the optimizer a small set of
    playbook/universe candidates without letting agent prose mutate execution.
    """
    if not agent_candidates_enabled(config):
        return []

    cfg = (config.get("intelligence") or {}).get("agent_candidates") or {}
    max_candidates = max(int(cfg.get("max_candidates", 3) or 3), 1)
    allowed_structures = _allowed_structures(config, policy)
    generated: list[TradeIdea] = []
    snapshots = sorted(
        provider.list_market_snapshots(),
        key=lambda item: (item.event_risk, -float(item.iv_rank or 0.0), item.symbol),
    )

    for snapshot in snapshots:
        symbol = str(snapshot.symbol or "").upper()
        if not symbol:
            continue
        matching_strategies = [
            strategy
            for strategy in strategies
            if _strategy_matches_symbol(strategy, symbol)
            and _strategy_passes_regime(strategy, regime, policy)
            and (
                not allowed_structures or strategy.structure.value in allowed_structures
            )
            and _snapshot_passes_strategy(snapshot, strategy, policy)
        ]
        matching_strategies.sort(key=lambda strategy: strategy.id)
        for strategy in matching_strategies:
            generated.append(
                TradeIdea(
                    id=_stable_idea_id(
                        date.today().isoformat(),
                        regime.value,
                        symbol,
                        strategy.id,
                    ),
                    date=date.today().isoformat(),
                    trader_name="kamandal_agent",
                    show_name=AGENT_SHOW_NAME,
                    underlying=symbol,
                    strategy_type=strategy.structure.value,
                    description=(
                        f"Agent-generated {strategy.structure.value} candidate "
                        f"for {symbol} in {regime.value} regime."
                    ),
                    expiration="",
                    credit_target=0.0,
                    rationale=(
                        "Shadow-only internal candidate from current regime, "
                        f"strategy template {strategy.id}, IV rank {snapshot.iv_rank}, "
                        f"generated_at {datetime.now(UTC).isoformat()}."
                    ),
                    confidence="agent",
                    source_url=AGENT_SOURCE_URL,
                    host="kamandal_agent",
                    status="new",
                )
            )
            if len(generated) >= max_candidates:
                return generated
    return generated
