"""Deterministic portfolio optimizer for Vol Crush dry-run workflows."""

from __future__ import annotations

import argparse
import itertools
import logging
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timezone
from pathlib import Path
from typing import Any

from vol_crush.core.config import (
    get_data_dir,
    load_config,
    load_strategies,
    load_strategy_templates,
    load_underlying_profiles,
    shadow_net_liquidation_value,
)
from vol_crush.core.interfaces import (
    MarketDataProvider,
    RegimeEvaluator,
    StorageBackend,
)
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import (
    CandidatePosition,
    ComboScore,
    ConstraintCheck,
    Greeks,
    IdeaStatus,
    ManagementStatus,
    MarketRegime,
    MarketSnapshot,
    OptionLeg,
    PlanDecision,
    PortfolioSnapshot,
    Position,
    RegimePolicy,
    Strategy,
    StrategyTemplate,
    StrategyType,
    TradeIdea,
    TradePlan,
    UnderlyingProfile,
    resolve_strategy,
    resolve_all_strategies,
)
from vol_crush.core.strategy_aliases import (
    canonical_strategy_type,
    strategy_profile_key,
)
from vol_crush.integrations.market_data import build_market_data_provider
from vol_crush.integrations.storage import build_local_store

logger = logging.getLogger("vol_crush.optimizer")


def _coerce_strategy_type(value: str) -> StrategyType:
    canonical = canonical_strategy_type(value)
    try:
        return StrategyType(canonical)
    except ValueError:
        return StrategyType.CUSTOM


def _sheet_template_overrides(
    config: dict, base_templates: list[StrategyTemplate]
) -> list[StrategyTemplate]:
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return base_templates
    raw_cache_dir = (config.get("google_sheets") or {}).get("cache_dir")
    cache_dir = Path(raw_cache_dir) if raw_cache_dir else get_data_dir() / "sheet_cache"
    cache_exists = (cache_dir / "template_library.json").exists()
    try:
        from vol_crush.sheets.sync import read_template_library_cache
    except ImportError:
        return [] if cache_exists else base_templates

    rows = read_template_library_cache(config)
    if not rows:
        return [] if cache_exists else base_templates

    base_by_id = {template.id: template for template in base_templates}
    merged: list[StrategyTemplate] = []
    for row in rows:
        if not row.template_id:
            continue
        base = None if cache_exists else base_by_id.get(row.template_id)
        if base is None:
            template = StrategyTemplate(
                id=row.template_id,
                name=row.name or row.template_id,
                structure=_coerce_strategy_type(row.structure or row.strategy_id),
            )
        else:
            template = replace(base)
        template.id = row.template_id
        template.name = row.name or template.name
        if row.structure or row.strategy_id:
            template.structure = _coerce_strategy_type(row.structure or row.strategy_id)
        if row.allowed_regimes:
            template.allowed_regimes = list(row.allowed_regimes)
        if row.iv_rank_min is not None:
            template.filters.iv_rank_min = row.iv_rank_min
        if row.iv_rank_max is not None:
            template.filters.iv_rank_max = row.iv_rank_max
        if row.dte_min is not None or row.dte_max is not None:
            template.filters.dte_range = (
                (
                    row.dte_min
                    if row.dte_min is not None
                    else template.filters.dte_range[0]
                ),
                (
                    row.dte_max
                    if row.dte_max is not None
                    else template.filters.dte_range[1]
                ),
            )
        if row.delta_min is not None or row.delta_max is not None:
            template.filters.delta_range = (
                (
                    row.delta_min
                    if row.delta_min is not None
                    else template.filters.delta_range[0]
                ),
                (
                    row.delta_max
                    if row.delta_max is not None
                    else template.filters.delta_range[1]
                ),
            )
        if row.spread_width is not None:
            template.filters.spread_width = row.spread_width
        if row.min_credit_to_width_ratio is not None:
            template.filters.min_credit_to_width_ratio = row.min_credit_to_width_ratio
        if row.profit_target_pct is not None:
            template.management.profit_target_pct = row.profit_target_pct
        if row.max_loss_multiple is not None:
            template.management.max_loss_multiple = row.max_loss_multiple
        if row.roll_dte_trigger is not None:
            template.management.roll_dte_trigger = row.roll_dte_trigger
        if row.roll_for_credit is not None:
            template.management.roll_for_credit = row.roll_for_credit
        if row.close_before_expiration is not None:
            template.management.close_before_expiration = row.close_before_expiration
        if row.avoid_earnings is not None:
            template.avoid_earnings = row.avoid_earnings
        merged.append(template)

    return merged


def _sheet_profile_overrides(
    config: dict, base_profiles: list[UnderlyingProfile]
) -> list[UnderlyingProfile]:
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return base_profiles
    try:
        from vol_crush.sheets.sync import (
            read_profile_configs_cache,
            read_universe_cache,
        )
    except ImportError:
        return base_profiles

    profile_rows = read_profile_configs_cache(config)
    universe_rows = read_universe_cache(config)
    if not profile_rows and not universe_rows:
        return base_profiles

    base_by_id = {profile.profile_id: profile for profile in base_profiles}
    row_by_id = {row.stock_profile: row for row in profile_rows if row.stock_profile}
    enabled_symbols: dict[str, list[str]] = {}
    for row in universe_rows:
        if not row.enabled or not row.stock_profile or not row.symbol:
            continue
        enabled_symbols.setdefault(row.stock_profile, []).append(row.symbol)

    profile_ids = set(base_by_id) | set(row_by_id) | set(enabled_symbols)
    merged: list[UnderlyingProfile] = []
    for profile_id in sorted(profile_ids):
        base = base_by_id.get(profile_id, UnderlyingProfile(profile_id=profile_id))
        row = row_by_id.get(profile_id)
        symbols = enabled_symbols.get(profile_id, list(base.symbols))
        merged.append(
            UnderlyingProfile(
                profile_id=profile_id,
                name=base.name or profile_id,
                symbols=list(symbols),
                allowed_structures=list(base.allowed_structures),
                max_bpr_pct=(
                    row.max_bpr_pct
                    if row is not None and row.max_bpr_pct is not None
                    else base.max_bpr_pct
                ),
                max_per_position_pct=(
                    row.max_per_position_pct
                    if row is not None and row.max_per_position_pct is not None
                    else base.max_per_position_pct
                ),
                max_positions=(
                    row.max_positions
                    if row is not None and row.max_positions is not None
                    else base.max_positions
                ),
                earnings_sensitive=(
                    row.earnings_sensitive
                    if row is not None and row.earnings_sensitive is not None
                    else base.earnings_sensitive
                ),
                min_option_volume=base.min_option_volume,
                min_open_interest=base.min_open_interest,
                notes=(row.notes if row is not None and row.notes else base.notes),
            )
        )
    return merged


def _find_template_for_sheet_strategy(
    strategy_id: str, templates: list[StrategyTemplate]
) -> StrategyTemplate | None:
    key = canonical_strategy_type(strategy_id)
    for template in templates:
        if canonical_strategy_type(template.id) == key:
            return template
    for template in templates:
        if canonical_strategy_type(template.structure.value) == key:
            return template
    return None


def _sheet_strategy_cache_exists(config: dict) -> bool:
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return False
    raw_cache_dir = (config.get("google_sheets") or {}).get("cache_dir")
    cache_dir = Path(raw_cache_dir) if raw_cache_dir else get_data_dir() / "sheet_cache"
    return (cache_dir / "strategies.json").exists()


def _sheet_strategy_objects(
    config: dict,
    templates: list[StrategyTemplate],
    profiles: list[UnderlyingProfile],
) -> tuple[bool, list[Strategy]]:
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return False, []
    cache_exists = _sheet_strategy_cache_exists(config)
    try:
        from vol_crush.sheets.sync import read_approvals_cache
    except ImportError:
        return cache_exists, []

    rows = [
        row
        for row in read_approvals_cache(config)
        if row.strategy_id and row.stock_profile
    ]
    if not rows:
        return cache_exists, []

    profiles_by_id = {profile.profile_id: profile for profile in profiles}
    resolved: dict[str, Strategy] = {}
    for row in rows:
        profile = profiles_by_id.get(row.stock_profile)
        template = _find_template_for_sheet_strategy(row.strategy_id, templates)
        if profile is None or template is None:
            continue
        strategy = resolve_strategy(template, profile)
        resolved[strategy.id] = strategy
    return cache_exists, list(resolved.values())


def load_strategy_objects(
    config_path: dict | str | Path | None = None,
) -> list[Strategy]:
    """Resolve strategy templates + underlying profiles into runtime Strategy objects.

    Falls back to the legacy strategies.yaml if the new config files don't exist or
    produce no results (backward compatibility for tests and existing deployments).
    """
    config = config_path if isinstance(config_path, Mapping) else None
    templates = [StrategyTemplate.from_dict(d) for d in load_strategy_templates() if d]
    profiles = [UnderlyingProfile.from_dict(d) for d in load_underlying_profiles() if d]
    if config is not None:
        templates = _sheet_template_overrides(dict(config), templates)
        profiles = _sheet_profile_overrides(dict(config), profiles)
        sheet_cache_exists, sheet_resolved = _sheet_strategy_objects(
            dict(config), templates, profiles
        )
        if sheet_cache_exists:
            return sheet_resolved
    resolved = resolve_all_strategies(templates, profiles)
    if resolved:
        return resolved
    return [Strategy.from_dict(item) for item in load_strategies() if item]


def _execution_mode(config: dict) -> str:
    """Return the canonical execution mode.

    Accepts the deprecated ``"pending"`` value and normalizes it to
    ``"shadow"`` — both mean "write PendingOrder with full preflight, do not
    submit to broker." See :class:`vol_crush.core.models.ExecutionMode`.
    """
    raw = str(config.get("execution", {}).get("mode", "")).lower()
    if raw == "pending":
        return "shadow"
    return raw


def _apply_shadow_nlv_override(
    snapshot: PortfolioSnapshot, config: Mapping[str, Any]
) -> PortfolioSnapshot:
    """Use a configured paper NLV when the synced broker snapshot is too small.

    This is only active outside live mode. It keeps shadow-mode planning useful
    on brand-new or lightly funded accounts where the broker snapshot can come
    back with a placeholder balance that is much smaller than the intended
    shadow bankroll.
    """
    if _execution_mode(dict(config)) == "live":
        return snapshot
    override = shadow_net_liquidation_value(config)
    if override is None or snapshot.net_liquidation_value >= override:
        return snapshot
    adjusted = PortfolioSnapshot.from_dict(snapshot.to_dict())
    adjusted.net_liquidation_value = override
    adjusted.bpr_used_pct = (adjusted.bpr_used / override) * 100.0 if override else 0.0
    adjusted.theta_as_pct_nlv = (
        (adjusted.greeks.theta * 100.0 / override) if override else 0.0
    )
    return adjusted


def _normalize_trade_idea(idea: TradeIdea) -> TradeIdea:
    """Normalize legacy stored idea fields before validation/matching."""
    return replace(
        idea,
        underlying=str(idea.underlying or "").upper(),
        strategy_type=canonical_strategy_type(idea.strategy_type),
    )


def _load_runtime_strategies(config: dict) -> list[Strategy]:
    """Compatibility wrapper for tests that monkeypatch zero-arg loaders."""
    try:
        return load_strategy_objects(config)
    except TypeError:
        return load_strategy_objects()


def _split_strategy_id(strategy_id: str) -> tuple[str, str]:
    """Strategy.id is built as ``f'{template_id}:{profile_id}'``."""
    if ":" in strategy_id:
        template_id, _, profile_id = strategy_id.partition(":")
        return template_id, profile_id
    return strategy_id, ""


def _load_approval_overlay(config: dict) -> dict[tuple[str, str], Any]:
    """Load sheet-synced approval rows keyed by (strategy_type, stock_profile).

    Returns an empty dict when sheet sync is disabled or the cache is missing.
    Importing lazily so optimizer tests do not need gspread installed.
    """
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return {}
    try:
        from vol_crush.sheets.sync import read_approvals_cache
    except ImportError:
        return {}
    overlay: dict[tuple[str, str], Any] = {}
    for row in read_approvals_cache(config):
        if not row.strategy_id or not row.stock_profile:
            continue
        overlay[(canonical_strategy_type(row.strategy_id), row.stock_profile)] = row
    return overlay


def _approval_row_for_strategy(strategy: Strategy, overlay: dict[tuple[str, str], Any]):
    template_id, profile_id = strategy_profile_key(strategy.id)
    keys = [
        (canonical_strategy_type(strategy.structure.value), profile_id),
        (canonical_strategy_type(template_id), profile_id),
        (template_id, profile_id),
    ]
    for key in keys:
        row = overlay.get(key)
        if row is not None:
            return row
    return None


def _filter_strategies_for_execution(
    strategies: list[Strategy], config: dict
) -> tuple[list[Strategy], list[str]]:
    """Apply approval gates that depend on execution mode.

    When the simplified strategies sheet has rows, shadow/live modes only consider
    enabled rows. If the sheet cache is empty, shadow/dry-run stay permissive so
    local tests and first-time setup can still run.

    Live mode is strict at the operator-control layer: a strategy needs an
    enabled sheet row with ``authorization_mode=live``. Rows set to ``shadow``
    downgrade that strategy even if the account-level mode is live.

    If no matching sheet row exists, the strategy is treated as not approved.
    """
    mode = _execution_mode(config)
    overlay = _load_approval_overlay(config)

    # Merge sheet overrides onto every strategy (in place copy). This lets the
    # rest of the optimizer see a single source of truth.
    for strategy in strategies:
        row = _approval_row_for_strategy(strategy, overlay)
        if row is None:
            continue
        if row.allowed_regimes:
            strategy.allowed_regimes = list(row.allowed_regimes)
        if row.iv_rank_min is not None:
            strategy.filters.iv_rank_min = row.iv_rank_min
        if row.iv_rank_max is not None:
            strategy.filters.iv_rank_max = row.iv_rank_max
        if row.avoid_earnings is not None:
            strategy.avoid_earnings = row.avoid_earnings
        if row.backtest_approved:
            strategy.backtest_approved = True
        if row.dry_run_passed:
            strategy.dry_run_passed = True
        if row.max_bpr_pct_override is not None:
            strategy.allocation.max_bpr_pct = row.max_bpr_pct_override
        if row.max_positions_override is not None:
            strategy.allocation.max_positions = row.max_positions_override

    if mode != "live":
        if overlay:
            eligible = [
                strategy
                for strategy in strategies
                if (row := _approval_row_for_strategy(strategy, overlay)) is not None
                and row.enabled
            ]
            notes = [
                f"{strategy.id}: blocked — no enabled strategies row"
                for strategy in strategies
                if _approval_row_for_strategy(strategy, overlay) is None
                or not _approval_row_for_strategy(strategy, overlay).enabled
            ]
            return eligible, notes
        return strategies, []

    eligible: list[Strategy] = []
    notes: list[str] = []
    for strategy in strategies:
        row = _approval_row_for_strategy(strategy, overlay)
        if row is None:
            notes.append(
                f"{strategy.id}: blocked in live mode — no sheet approval row "
                f"found in the strategies tab"
            )
            continue
        if not row.enabled:
            notes.append(f"{strategy.id}: blocked — sheet row has enabled=FALSE")
            continue
        if row.authorization_mode.value != "live":
            notes.append(
                f"{strategy.id}: blocked — sheet authorization_mode=shadow "
                f"downgrades this row from account-level live"
            )
            continue
        eligible.append(strategy)
    return eligible, notes


def _load_idea_approval_overlay(config: dict) -> dict[str, Any]:
    """Load sheet-synced idea_review rows keyed by idea_id."""
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return {}
    try:
        from vol_crush.sheets.sync import read_idea_approvals_cache
    except ImportError:
        return {}
    return {
        row.idea_id: row for row in read_idea_approvals_cache(config) if row.idea_id
    }


def _load_sheet_trade_ideas(config: dict) -> list[TradeIdea]:
    """Treat approved rows in idea_review as operator-entered trade ideas."""
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return []
    try:
        from vol_crush.sheets.schemas import IdeaApproval
        from vol_crush.sheets.sync import read_idea_approvals_cache
    except ImportError:
        return []

    ideas: list[TradeIdea] = []
    for row in read_idea_approvals_cache(config):
        if row.approval != IdeaApproval.APPROVED:
            continue
        if not row.underlying or not (row.proposed_strategy or row.strategy_type):
            continue
        strategy_type = canonical_strategy_type(
            row.proposed_strategy or row.strategy_type
        )
        note = row.note or row.description or row.operator_notes
        expectation = f"{row.expectation} expectation. " if row.expectation else ""
        rationale = row.rationale or note
        ideas.append(
            TradeIdea(
                id=row.idea_id,
                date=row.date,
                trader_name=row.reviewed_by or "operator",
                show_name="idea_review",
                underlying=row.underlying,
                strategy_type=strategy_type,
                description=note,
                expiration=row.expiration,
                credit_target=0.0,
                rationale=f"{expectation}{rationale}".strip(),
                confidence=row.confidence or "operator",
                source_url=row.source_url,
                source_timestamp=row.source_timestamp,
                video_id=row.video_id,
                host=row.host,
                strikes=list(row.strikes or []),
                extracted_at="",
                status=IdeaStatus.APPROVED.value,
            )
        )
    return ideas


def _filter_ideas_for_execution(ideas, config: dict):
    """Gate LLM-extracted ideas through the idea_review sheet when enabled.

    When Sheets are disabled, local DB ideas are allowed through for standalone
    testing. When Sheets are enabled, ideas must be approved in idea_review.
    This keeps the operator review loop intact in shadow mode too.
    """
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return list(ideas), []

    overlay = _load_idea_approval_overlay(config)
    auto_approve = bool(
        (config.get("execution") or {}).get("auto_approve_ideas", False)
    )
    kept: list = []
    notes: list[str] = []
    for idea in ideas:
        row = overlay.get(idea.id)
        if row is None:
            if auto_approve:
                kept.append(idea)
                continue
            notes.append(f"idea {idea.id}: blocked — no idea_review row")
            continue
        if row.approval.value == "approve":
            kept.append(idea)
            continue
        if auto_approve and row.approval.value not in {"reject", "hold"}:
            kept.append(idea)
            continue
        notes.append(
            f"idea {idea.id}: blocked by idea_review "
            f"(approval={row.approval.value or 'pending'})"
        )
    return kept, notes


class ConfigRegimeEvaluator(RegimeEvaluator):
    """Simple regime selection from current fixture state."""

    def __init__(self, config: dict):
        self.config = config
        regime_cfg = config.get("portfolio", {}).get("regimes", {})
        self.policies = {}
        for key, value in regime_cfg.items():
            payload = dict(value)
            payload["regime"] = key
            self.policies[key] = RegimePolicy.from_dict(payload)

    def determine_regime(self, snapshots: list[MarketSnapshot]) -> MarketRegime:
        if not snapshots:
            return MarketRegime.UNKNOWN
        if any(snapshot.event_risk for snapshot in snapshots):
            return MarketRegime.EVENT_RISK
        avg_iv_rank = sum(snapshot.iv_rank for snapshot in snapshots) / len(snapshots)
        if avg_iv_rank >= 35:
            return MarketRegime.HIGH_IV
        if avg_iv_rank <= 17:
            return MarketRegime.LOW_IV
        return MarketRegime.NORMAL_IV

    def get_policy(self, regime: MarketRegime) -> RegimePolicy:
        key = regime.value if isinstance(regime, MarketRegime) else str(regime)
        if key in self.policies:
            return self.policies[key]
        unknown = self.policies.get(MarketRegime.NORMAL_IV.value)
        if unknown:
            return unknown
        return RegimePolicy(regime=MarketRegime.UNKNOWN)


def _strategy_lookup(strategies: list[Strategy]) -> dict[str, list[Strategy]]:
    """Build a structure → list[Strategy] index so multiple resolved strategies
    (e.g. put_spread on SPY via index_etf AND put_spread on TLT via bond_etf)
    coexist without overwriting each other."""
    result: dict[str, list[Strategy]] = {}
    for strategy in strategies:
        result.setdefault(strategy.structure.value, []).append(strategy)
    return result


def _find_strategy_for_idea(
    idea: TradeIdea,
    strategy_map: dict[str, list[Strategy]],
    policy: RegimePolicy,
) -> Strategy | None:
    """Find the first matching resolved Strategy for a given idea.

    Matches on structure (idea.strategy_type) AND underlying (idea.underlying must
    be in the strategy's underlyings list, or the list is empty = any).
    """
    candidates = strategy_map.get(idea.strategy_type, [])
    for strategy in candidates:
        if (
            strategy.filters.underlyings
            and idea.underlying not in strategy.filters.underlyings
        ):
            continue
        return strategy
    return None


def _option_dte(option) -> int:
    expiry = _option_expiry_date(option)
    if expiry is None:
        return 9999
    return (expiry - datetime.now(timezone.utc).date()).days


def _pick_option(
    snapshot: MarketSnapshot,
    option_type: str,
    *,
    delta_range: tuple[float, float],
    dte_range: tuple[int, int],
    expiration: str | None = None,
):
    options = [
        item
        for item in _active_option_snapshots(snapshot)
        if item.option_type == option_type
    ]
    if expiration:
        matching_expiration = [
            item for item in options if item.expiration == expiration
        ]
        if matching_expiration:
            options = matching_expiration
    if not options:
        return None

    target_delta = (delta_range[0] + delta_range[1]) / 2.0
    target_dte = (dte_range[0] + dte_range[1]) / 2.0

    def score(item) -> tuple[float, ...]:
        abs_delta = abs(item.greeks.delta)
        dte = _option_dte(item)
        delta_penalty = (
            0.0
            if delta_range[0] <= abs_delta <= delta_range[1]
            else min(abs(abs_delta - delta_range[0]), abs(abs_delta - delta_range[1]))
            + 1.0
        )
        dte_penalty = (
            0.0
            if dte_range[0] <= dte <= dte_range[1]
            else min(abs(dte - dte_range[0]), abs(dte - dte_range[1])) + 1.0
        )
        return (
            delta_penalty,
            dte_penalty,
            abs(abs_delta - target_delta),
            abs(dte - target_dte),
            abs(item.strike - snapshot.underlying_price),
        )

    return min(options, key=score)


def _default_expiration(snapshot: MarketSnapshot) -> str:
    active = _active_option_snapshots(snapshot)
    if active:
        return active[0].expiration
    return datetime.now(timezone.utc).date().isoformat()


def _option_expiry_date(option) -> date | None:
    try:
        return datetime.fromisoformat(option.expiration).date()
    except (TypeError, ValueError):
        return None


def _active_option_snapshots(snapshot: MarketSnapshot) -> list:
    today = datetime.now(timezone.utc).date()
    active: list = []
    for item in snapshot.option_snapshots:
        expiry = _option_expiry_date(item)
        if expiry is None or expiry >= today:
            active.append(item)
    return active


def _approximate_candidate(
    idea: TradeIdea, strategy: Strategy, snapshot: MarketSnapshot
) -> CandidatePosition | None:
    call = _pick_option(
        snapshot,
        "call",
        delta_range=strategy.filters.delta_range,
        dte_range=strategy.filters.dte_range,
    )
    put = _pick_option(
        snapshot,
        "put",
        delta_range=strategy.filters.delta_range,
        dte_range=strategy.filters.dte_range,
    )
    strategy_type = strategy.structure.value
    credit = idea.credit_target
    bpr = snapshot.underlying_price * 100 * 0.18
    greeks = Greeks()
    legs: list[OptionLeg] = []

    if strategy_type == StrategyType.SHORT_PUT.value:
        if not put:
            return None
        credit = credit or put.mid
        greeks = replace(put.greeks)
        bpr = max(snapshot.underlying_price * 100 * 0.12, credit * 100 * 5)
        legs = [
            OptionLeg(
                underlying=idea.underlying,
                expiration=idea.expiration or put.expiration,
                strike=put.strike,
                option_type="put",
                side="sell",
            )
        ]
    elif strategy_type == StrategyType.SHORT_CALL.value:
        if not call:
            return None
        credit = credit or call.mid
        greeks = replace(call.greeks)
        bpr = max(snapshot.underlying_price * 100 * 0.12, credit * 100 * 5)
        legs = [
            OptionLeg(
                underlying=idea.underlying,
                expiration=idea.expiration or call.expiration,
                strike=call.strike,
                option_type="call",
                side="sell",
            )
        ]
    elif strategy_type in (StrategyType.LONG_CALL.value, StrategyType.LONG_PUT.value):
        base = call if strategy_type == StrategyType.LONG_CALL.value else put
        if not base:
            return None
        debit = idea.credit_target or base.mid
        credit = -abs(debit)
        greeks = replace(base.greeks)
        bpr = abs(debit) * 100
        legs = [
            OptionLeg(
                underlying=idea.underlying,
                expiration=idea.expiration or base.expiration,
                strike=base.strike,
                option_type=base.option_type,
                side="buy",
            )
        ]
    elif strategy_type == StrategyType.SHORT_STRANGLE.value:
        if not put:
            return None
        call = _pick_option(
            snapshot,
            "call",
            delta_range=strategy.filters.delta_range,
            dte_range=strategy.filters.dte_range,
            expiration=put.expiration,
        )
        if not call:
            return None
        credit = credit or (call.mid + put.mid)
        greeks = Greeks(
            delta=call.greeks.delta + put.greeks.delta,
            gamma=call.greeks.gamma + put.greeks.gamma,
            theta=call.greeks.theta + put.greeks.theta,
            vega=call.greeks.vega + put.greeks.vega,
        )
        bpr = max(snapshot.underlying_price * 100 * 0.2, credit * 100 * 6)
        legs = [
            OptionLeg(
                idea.underlying,
                idea.expiration or put.expiration,
                put.strike,
                "put",
                "sell",
            ),
            OptionLeg(
                idea.underlying,
                idea.expiration or call.expiration,
                call.strike,
                "call",
                "sell",
            ),
        ]
    elif strategy_type in (
        StrategyType.PUT_SPREAD.value,
        StrategyType.CALL_SPREAD.value,
        StrategyType.IRON_CONDOR.value,
    ):
        base = (
            put
            if "put" in strategy_type or strategy_type == StrategyType.IRON_CONDOR.value
            else call
        )
        other_type = "call" if strategy_type == StrategyType.IRON_CONDOR.value else None
        credit = credit or (base.mid * 0.5 if base else 1.0)
        source_greeks = (
            base.greeks
            if base
            else Greeks(delta=-0.18, gamma=0.04, theta=0.08, vega=0.05)
        )
        greeks = source_greeks * 0.6
        bpr = max((strategy.filters.spread_width or 5.0) * 100, credit * 100 * 2)
        if not base:
            return None
        width = strategy.filters.spread_width or 5.0
        buy_strike = (
            base.strike - width if base.option_type == "put" else base.strike + width
        )
        legs = [
            OptionLeg(
                idea.underlying,
                idea.expiration or base.expiration,
                base.strike,
                base.option_type,
                "sell",
            ),
            OptionLeg(
                idea.underlying,
                idea.expiration or base.expiration,
                buy_strike,
                base.option_type,
                "buy",
            ),
        ]
        if other_type:
            call = _pick_option(
                snapshot,
                "call",
                delta_range=strategy.filters.delta_range,
                dte_range=strategy.filters.dte_range,
                expiration=base.expiration,
            )
            if not call:
                return None
            width = strategy.filters.spread_width or 5.0
            legs.extend(
                [
                    OptionLeg(
                        idea.underlying,
                        idea.expiration or call.expiration,
                        call.strike,
                        "call",
                        "sell",
                    ),
                    OptionLeg(
                        idea.underlying,
                        idea.expiration or call.expiration,
                        call.strike + width,
                        "call",
                        "buy",
                    ),
                ]
            )
    elif strategy_type == StrategyType.JADE_LIZARD.value:
        if not put:
            return None
        call = _pick_option(
            snapshot,
            "call",
            delta_range=strategy.filters.delta_range,
            dte_range=strategy.filters.dte_range,
            expiration=put.expiration,
        )
        if not call:
            return None
        width = strategy.filters.spread_width or 5.0
        credit = credit or (put.mid + call.mid * 0.5)
        greeks = Greeks(
            delta=put.greeks.delta + call.greeks.delta * 0.5,
            gamma=put.greeks.gamma + call.greeks.gamma * 0.5,
            theta=put.greeks.theta + call.greeks.theta * 0.5,
            vega=put.greeks.vega + call.greeks.vega * 0.5,
        )
        bpr = max(put.strike * 100 * 0.2, width * 100)
        legs = [
            OptionLeg(
                idea.underlying,
                idea.expiration or put.expiration,
                put.strike,
                "put",
                "sell",
            ),
            OptionLeg(
                idea.underlying,
                idea.expiration or call.expiration,
                call.strike,
                "call",
                "sell",
            ),
            OptionLeg(
                idea.underlying,
                idea.expiration or call.expiration,
                call.strike + width,
                "call",
                "buy",
            ),
        ]
    else:
        credit = credit or 1.0
        greeks = Greeks(delta=0.0, gamma=0.05, theta=0.06, vega=0.04)
        bpr = max(snapshot.underlying_price * 100 * 0.1, 250.0)

    candidate_expiration = idea.expiration or (
        legs[0].expiration if legs else _default_expiration(snapshot)
    )

    return CandidatePosition(
        idea_id=idea.id,
        strategy_id=strategy.id,
        underlying=idea.underlying,
        strategy_type=strategy_type,
        expiration=candidate_expiration,
        estimated_credit=round(credit, 4),
        estimated_bpr=round(bpr, 2),
        estimated_greeks=greeks,
        iv_rank=snapshot.iv_rank,
        sector=snapshot.sector,
        event_risk=snapshot.event_risk,
        rationale=idea.rationale or idea.description,
        legs=legs,
    )


def validate_trade_ideas(
    ideas: list[TradeIdea],
    strategies: list[Strategy],
    provider: MarketDataProvider,
    policy: RegimePolicy,
    regime: MarketRegime | None = None,
) -> tuple[list[CandidatePosition], list[str]]:
    """Validate ideas against resolved strategies, regime, and fixture data.

    Checks enforced (in order):
        1. Structure + underlying match via resolved strategy lookup.
        2. Template-level regime gate: current regime must be in strategy.allowed_regimes.
        3. Template-level earnings gate: strategy.avoid_earnings + snapshot.event_risk.
        4. Template-level IV rank bounds: strategy.filters.iv_rank_min/max.
        5. Regime policy: reject_event_risk, min/max_iv_rank, avoid_structures.
        6. Fixture availability.
    """
    notes: list[str] = []
    strategy_map = _strategy_lookup(strategies)
    candidates: list[CandidatePosition] = []
    current_regime = regime.value if regime is not None else None

    for idea in ideas:
        strategy = _find_strategy_for_idea(idea, strategy_map, policy)
        if strategy is None:
            notes.append(
                f"{idea.id}: no matching template+profile for "
                f"{idea.strategy_type} on {idea.underlying}"
            )
            continue

        # Template-level regime gate
        if (
            strategy.allowed_regimes
            and current_regime
            and current_regime not in strategy.allowed_regimes
        ):
            notes.append(
                f"{idea.id}: {strategy.id} not eligible in {current_regime} regime "
                f"(allowed: {strategy.allowed_regimes})"
            )
            continue

        snapshot = provider.get_market_snapshot(idea.underlying)
        if snapshot is None:
            notes.append(f"{idea.id}: missing fixture data for {idea.underlying}")
            continue

        # Template-level earnings gate
        if strategy.avoid_earnings and snapshot.event_risk:
            notes.append(
                f"{idea.id}: {strategy.id} avoids earnings/event risk on {idea.underlying}"
            )
            continue

        # Template-level IV rank bounds (strategy filters, not just regime policy)
        if (
            strategy.filters.iv_rank_min is not None
            and snapshot.iv_rank < strategy.filters.iv_rank_min
        ):
            notes.append(
                f"{idea.id}: IV rank {snapshot.iv_rank} below strategy minimum "
                f"{strategy.filters.iv_rank_min} for {strategy.id}"
            )
            continue
        if (
            strategy.filters.iv_rank_max is not None
            and snapshot.iv_rank > strategy.filters.iv_rank_max
        ):
            notes.append(
                f"{idea.id}: IV rank {snapshot.iv_rank} above strategy maximum "
                f"{strategy.filters.iv_rank_max} for {strategy.id}"
            )
            continue

        # Regime policy checks (broader market-level gates)
        if policy.reject_event_risk and snapshot.event_risk:
            notes.append(f"{idea.id}: rejected for event risk on {idea.underlying}")
            continue
        if policy.min_iv_rank is not None and snapshot.iv_rank < policy.min_iv_rank:
            notes.append(f"{idea.id}: IV rank {snapshot.iv_rank} below regime floor")
            continue
        if policy.max_iv_rank is not None and snapshot.iv_rank > policy.max_iv_rank:
            notes.append(f"{idea.id}: IV rank {snapshot.iv_rank} above regime ceiling")
            continue
        if strategy.structure.value in policy.avoid_structures:
            notes.append(
                f"{idea.id}: {strategy.structure.value} down-ranked by regime policy"
            )
        candidate = _approximate_candidate(idea, strategy, snapshot)
        if candidate is None:
            notes.append(
                f"{idea.id}: no active option snapshots available for {strategy.id}"
            )
            continue
        candidates.append(candidate)

    return candidates, notes


def build_portfolio_snapshot(store: StorageBackend) -> PortfolioSnapshot:
    snapshot = store.get_latest_portfolio_snapshot()
    if snapshot is not None:
        return snapshot
    positions = store.list_positions(status=None)
    greeks = Greeks()
    bpr_used = 0.0
    for position in positions:
        greeks = greeks + position.greeks
        bpr_used += position.bpr
    nlv = 100000.0
    theta_pct = (greeks.theta * 100.0 / nlv) if nlv else 0.0
    ratio = abs(greeks.gamma / greeks.theta) if greeks.theta else 0.0
    snapshot = PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc).isoformat(),
        net_liquidation_value=nlv,
        greeks=greeks,
        beta_weighted_delta=greeks.delta,
        bpr_used=bpr_used,
        bpr_used_pct=(bpr_used / nlv) * 100.0 if nlv else 0.0,
        theta_as_pct_nlv=theta_pct,
        gamma_theta_ratio=ratio,
        position_count=len(positions),
        positions=positions,
    )
    store.save_portfolio_snapshot(snapshot)
    return snapshot


def _auto_managed_positions(snapshot: PortfolioSnapshot) -> list[Position]:
    """Return positions the optimizer is allowed to count for diversification.

    Groups flagged for manual review (inferred short calls, unknown_complex,
    orphan_leg, etc.) still contribute Greeks and BPR to the aggregate — we see
    the real exposure — but they do NOT count toward position_count or
    diversification bonuses, because we cannot reason about their management.
    """
    return [
        p
        for p in snapshot.positions
        if p.management_status == ManagementStatus.AUTO.value
    ]


def _orphan_leg_count(snapshot: PortfolioSnapshot) -> int:
    return sum(
        1
        for p in snapshot.positions
        if p.strategy_type
        in (StrategyType.ORPHAN_LEG.value, StrategyType.UNKNOWN_COMPLEX.value)
        or p.management_status == ManagementStatus.MANUAL_REVIEW_REQUIRED.value
    )


def _project_portfolio(
    base: PortfolioSnapshot, candidates: list[CandidatePosition]
) -> PortfolioSnapshot:
    projected = PortfolioSnapshot.from_dict(base.to_dict())
    total_bpr = base.bpr_used
    greeks = Greeks.from_dict(base.greeks.to_dict())
    for candidate in candidates:
        greeks = greeks + candidate.estimated_greeks
        total_bpr += candidate.estimated_bpr
    projected.timestamp = datetime.now(timezone.utc).isoformat()
    projected.greeks = greeks
    projected.beta_weighted_delta = greeks.delta
    projected.bpr_used = total_bpr
    projected.bpr_used_pct = (
        (total_bpr / projected.net_liquidation_value) * 100.0
        if projected.net_liquidation_value
        else 0.0
    )
    projected.theta_as_pct_nlv = (
        (greeks.theta * 100.0 / projected.net_liquidation_value)
        if projected.net_liquidation_value
        else 0.0
    )
    projected.gamma_theta_ratio = (
        abs(greeks.gamma / greeks.theta) if greeks.theta else 0.0
    )
    # position_count is the number of *auto-managed* groups plus the new candidates.
    # Manual-review groups still live in the portfolio (so their Greeks and BPR
    # appear in the aggregate), but they don't count toward max_positions.
    auto_baseline = len(_auto_managed_positions(base))
    projected.position_count = auto_baseline + len(candidates)
    return projected


def evaluate_constraints(
    projected: PortfolioSnapshot,
    candidates: list[CandidatePosition],
    config: dict,
    base: PortfolioSnapshot | None = None,
) -> list[ConstraintCheck]:
    constraints = config.get("portfolio", {}).get("constraints", {})
    max_orphan_legs = int(constraints.get("max_orphan_legs", 0))
    orphan_count = _orphan_leg_count(base) if base is not None else 0
    checks = [
        ConstraintCheck(
            name="max_orphan_legs",
            passed=orphan_count <= max_orphan_legs,
            actual=float(orphan_count),
            max_value=float(max_orphan_legs),
            message=(
                "Portfolio contains ungrouped short / unknown-complex legs above "
                "the configured threshold. Refuse new opens until they are classified "
                "or manually resolved."
            ),
        ),
        ConstraintCheck(
            name="beta_weighted_delta_pct",
            passed=constraints["beta_weighted_delta_pct"][0]
            <= projected.beta_weighted_delta
            <= constraints["beta_weighted_delta_pct"][1],
            actual=projected.beta_weighted_delta,
            min_value=constraints["beta_weighted_delta_pct"][0],
            max_value=constraints["beta_weighted_delta_pct"][1],
            message="Portfolio delta must stay in configured beta-weighted bounds.",
        ),
        ConstraintCheck(
            name="daily_theta_pct",
            passed=constraints["daily_theta_pct"][0]
            <= projected.theta_as_pct_nlv
            <= constraints["daily_theta_pct"][1],
            actual=projected.theta_as_pct_nlv,
            min_value=constraints["daily_theta_pct"][0],
            max_value=constraints["daily_theta_pct"][1],
            message="Daily theta as a percent of NLV must stay within target range.",
        ),
        ConstraintCheck(
            name="max_gamma_ratio",
            passed=projected.gamma_theta_ratio <= constraints["max_gamma_ratio"],
            actual=projected.gamma_theta_ratio,
            max_value=constraints["max_gamma_ratio"],
            message="Gamma/theta ratio exceeded.",
        ),
        ConstraintCheck(
            name="max_bpr_utilization_pct",
            passed=projected.bpr_used_pct <= constraints["max_bpr_utilization_pct"],
            actual=projected.bpr_used_pct,
            max_value=constraints["max_bpr_utilization_pct"],
            message="BPR utilization exceeded target cap.",
        ),
        ConstraintCheck(
            name="max_positions",
            passed=projected.position_count <= constraints["max_positions"],
            actual=float(projected.position_count),
            max_value=float(constraints["max_positions"]),
            message="Position count exceeded configured maximum.",
        ),
    ]
    by_underlying = {}
    if base is not None:
        for position in base.positions:
            by_underlying[position.underlying] = (
                by_underlying.get(position.underlying, 0.0) + position.bpr
            )
    for candidate in candidates:
        by_underlying[candidate.underlying] = (
            by_underlying.get(candidate.underlying, 0.0) + candidate.estimated_bpr
        )
    for underlying, bpr in by_underlying.items():
        pct = (bpr / projected.bpr_used) * 100.0 if projected.bpr_used else 0.0
        checks.append(
            ConstraintCheck(
                name=f"max_single_underlying_pct:{underlying}",
                passed=pct <= constraints["max_single_underlying_pct"],
                actual=pct,
                max_value=constraints["max_single_underlying_pct"],
                message=f"{underlying} concentration exceeds per-underlying cap.",
            )
        )
    return checks


def _score_combo(
    base: PortfolioSnapshot,
    projected: PortfolioSnapshot,
    candidates: list[CandidatePosition],
    checks: list[ConstraintCheck],
    config: dict,
    policy: RegimePolicy,
) -> ComboScore:
    weights = config.get("portfolio", {}).get("optimizer_weights", {})
    target_delta = policy.target_delta_bias
    before_distance = abs(base.beta_weighted_delta - target_delta)
    after_distance = abs(projected.beta_weighted_delta - target_delta)
    delta_score = max(before_distance - after_distance, 0.0)
    gamma_score = max(1.5 - projected.gamma_theta_ratio, 0.0)
    theta_score = max(projected.theta_as_pct_nlv - base.theta_as_pct_nlv, 0.0)
    unique_underlyings = len({candidate.underlying for candidate in candidates})
    unique_sectors = len({candidate.sector for candidate in candidates})
    diversification_score = unique_underlyings + (0.5 * unique_sectors)
    preferred_hits = sum(
        1
        for candidate in candidates
        if candidate.strategy_type in policy.prefer_structures
    )
    avoided_hits = sum(
        1
        for candidate in candidates
        if candidate.strategy_type in policy.avoid_structures
    )
    regime_fit = (
        preferred_hits
        - avoided_hits
        - sum(1 for candidate in candidates if candidate.event_risk)
    )
    total_score = (
        delta_score * weights.get("delta_improvement", 0.25)
        + gamma_score * weights.get("gamma_profile", 0.20)
        + theta_score * weights.get("theta_improvement", 0.35)
        + diversification_score * weights.get("diversification", 0.20)
        + regime_fit * 0.10
    )
    notes = []
    if avoided_hits:
        notes.append(
            "One or more structures were down-ranked by the current regime policy."
        )
    if not all(check.passed for check in checks):
        notes.append("Constraint failures prevent this combo from being tradable.")
    return ComboScore(
        combo_ids=[candidate.idea_id for candidate in candidates],
        candidate_positions=candidates,
        total_score=round(total_score, 4),
        component_scores={
            "delta_improvement": round(delta_score, 4),
            "gamma_profile": round(gamma_score, 4),
            "theta_improvement": round(theta_score, 4),
            "diversification": round(diversification_score, 4),
            "regime_fit": round(regime_fit, 4),
        },
        projected_portfolio=projected,
        constraint_checks=checks,
        regime=policy.regime.value,
        notes=notes,
    )


def rank_candidate_combos(
    base_snapshot: PortfolioSnapshot,
    candidates: list[CandidatePosition],
    config: dict,
    policy: RegimePolicy,
) -> list[ComboScore]:
    combos: list[ComboScore] = []
    for size in (1, 2, 3):
        if len(candidates) < size:
            break
        for group in itertools.combinations(candidates, min(size, len(candidates))):
            projected = _project_portfolio(base_snapshot, list(group))
            checks = evaluate_constraints(
                projected, list(group), config, base=base_snapshot
            )
            combos.append(
                _score_combo(
                    base_snapshot, projected, list(group), checks, config, policy
                )
            )
    combos.sort(key=lambda item: item.total_score, reverse=True)
    return combos


def _resolve_regime(config: dict, snapshots: list) -> tuple[MarketRegime, RegimePolicy]:
    """Determine the current market regime and its policy.

    Uses today's optional `regime_control` sheet override first. When no
    override is enabled for today, falls back to the local config-driven
    evaluator based on fixture state.
    """
    evaluator = ConfigRegimeEvaluator(config)
    override_row = _todays_regime_override(config)
    if override_row is not None:
        regime = MarketRegime(override_row.regime)
        policy = evaluator.get_policy(regime)
        logger.info(
            "Using regime_control override for %s: regime=%s%s",
            override_row.date,
            regime.value,
            f" note={override_row.note}" if override_row.note else "",
        )
        return regime, policy

    logger.info("No regime_control override for today; using local evaluator")
    regime = evaluator.determine_regime(snapshots)
    policy = evaluator.get_policy(regime)
    return regime, policy


def _todays_regime_override(config: dict):
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return None
    try:
        from vol_crush.sheets.sync import read_regime_control_cache
    except ImportError:
        return None

    today = datetime.now(UTC).date()
    valid = {_regime.value for _regime in MarketRegime}
    for row in read_regime_control_cache(config):
        if not row.override_enabled or row.regime not in valid:
            continue
        try:
            row_date = date.fromisoformat(str(row.date).strip())
        except ValueError:
            continue
        if row_date == today:
            return row
    return None


def build_trade_plan(
    store: StorageBackend,
    config: dict,
    provider: MarketDataProvider,
) -> TradePlan:
    strategies, approval_notes = _filter_strategies_for_execution(
        _load_runtime_strategies(config), config
    )
    raw_ideas = [
        _normalize_trade_idea(idea)
        for idea in store.list_trade_ideas()
        if idea.status in (IdeaStatus.NEW.value, IdeaStatus.APPROVED.value)
    ]
    sheet_ideas = _load_sheet_trade_ideas(config)
    if sheet_ideas:
        existing_ids = {idea.id for idea in raw_ideas}
        raw_ideas.extend(idea for idea in sheet_ideas if idea.id not in existing_ids)
    ideas, idea_approval_notes = _filter_ideas_for_execution(raw_ideas, config)
    approval_notes = approval_notes + idea_approval_notes
    snapshots = provider.list_market_snapshots()
    regime, policy = _resolve_regime(config, snapshots)
    if _execution_mode(dict(config)) == "shadow":
        from vol_crush.intelligence.candidates import generate_agent_trade_ideas

        agent_ideas = generate_agent_trade_ideas(
            config,
            strategies=strategies,
            provider=provider,
            regime=regime,
            policy=policy,
        )
        if agent_ideas:
            existing_ids = {idea.id for idea in ideas}
            ideas.extend(idea for idea in agent_ideas if idea.id not in existing_ids)
            approval_notes.append(
                f"agent candidates added in shadow mode: {len(agent_ideas)}"
            )
    candidates, notes = validate_trade_ideas(
        ideas, strategies, provider, policy, regime=regime
    )
    notes = approval_notes + notes
    if _execution_mode(dict(config)) == "shadow":
        from vol_crush.shadow.service import build_shadow_portfolio_snapshot

        base_snapshot = build_shadow_portfolio_snapshot(store, config)
    else:
        base_snapshot = build_portfolio_snapshot(store)
    base_snapshot = _apply_shadow_nlv_override(base_snapshot, config)

    plan_id = f"plan_{uuid.uuid4().hex[:10]}"
    if not candidates:
        return TradePlan(
            plan_id=plan_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            decision=PlanDecision.NO_TRADE,
            regime=regime.value,
            reasoning="No ideas passed validation against strategies, fixtures, and regime policy.",
            risk_flags=notes,
            status="pending",
        )

    ranked = rank_candidate_combos(base_snapshot, candidates, config, policy)
    viable = [combo for combo in ranked if combo.passes_constraints]
    if not viable or viable[0].total_score <= 0:
        return TradePlan(
            plan_id=plan_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            decision=PlanDecision.NO_TRADE,
            regime=regime.value,
            ranked_combos=ranked[:3],
            candidate_positions=candidates,
            reasoning="Candidates were evaluated, but no combo improved the portfolio enough within constraints.",
            risk_flags=notes
            + [
                item.message
                for combo in ranked[:3]
                for item in combo.constraint_checks
                if not item.passed
            ],
            status="pending",
        )

    best = viable[0]
    return TradePlan(
        plan_id=plan_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        decision=PlanDecision.EXECUTE,
        regime=regime.value,
        selected_combo_ids=best.combo_ids,
        ranked_combos=ranked[:3],
        candidate_positions=best.candidate_positions,
        reasoning="Selected the top-ranked combo that improved target Greeks while passing all hard constraints.",
        risk_flags=notes,
        status="pending",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Vol Crush optimizer")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    store = build_local_store(config)
    bundle_path = (
        config.get("data_sources", {})
        .get("fixtures", {})
        .get("bundle_path", "data/fixtures/fixture_bundle.json")
    )
    provider = build_market_data_provider(config, bundle_path)
    plan = build_trade_plan(store, config, provider)
    store.save_trade_plan(plan)
    logger.info(
        "Generated trade plan %s with decision=%s", plan.plan_id, plan.decision.value
    )
    if plan.selected_combo_ids:
        logger.info("Selected combo ids: %s", ", ".join(plan.selected_combo_ids))
    else:
        logger.info("No trade selected. Reason: %s", plan.reasoning)


if __name__ == "__main__":
    main()
