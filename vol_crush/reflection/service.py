"""Deterministic reflection loop over intake, plans, orders, and shadow fills."""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import UTC, datetime

from vol_crush.core.models import (
    IdeaCandidate,
    PlanDecision,
    ReflectionSummary,
    SourceIntelligence,
)
from vol_crush.integrations.storage import LocalStore, build_local_store

logger = logging.getLogger("vol_crush.reflection")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _stable_summary_id(window_start: str, window_end: str, generated_at: str) -> str:
    raw = f"{window_start}|{window_end}|{generated_at[:10]}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"reflection_{digest}"


def _in_window(value: str, *, window_start: str = "", window_end: str = "") -> bool:
    if not value:
        return True
    if window_start and value < window_start:
        return False
    if window_end and value > window_end:
        return False
    return True


def _selected_idea_ids(plans) -> set[str]:
    result: set[str] = set()
    for plan in plans:
        result.update(plan.selected_combo_ids or [])
        for candidate in plan.candidate_positions:
            if candidate.idea_id in (plan.selected_combo_ids or []):
                result.add(candidate.idea_id)
    return {item for item in result if item}


def _candidate_source_map(candidates: list[IdeaCandidate]) -> dict[str, str]:
    return {
        candidate.idea_id: candidate.source_id
        for candidate in candidates
        if candidate.idea_id and candidate.source_id
    }


def _source_name_by_id(observations) -> dict[str, str]:
    return {
        observation.source_id: observation.source_name
        for observation in observations
        if observation.source_id
    }


def _conversion_counts_by_source(
    *,
    candidates: list[IdeaCandidate],
    observations,
    selected_ids: set[str],
    ordered_ids: set[str],
) -> dict[str, dict[str, int]]:
    source_by_id = _source_name_by_id(observations)
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"candidates": 0, "selected": 0, "ordered": 0}
    )
    for candidate in candidates:
        source_name = source_by_id.get(candidate.source_id)
        if not source_name:
            continue
        counts[source_name]["candidates"] += 1
        if candidate.idea_id in selected_ids:
            counts[source_name]["selected"] += 1
        if candidate.idea_id in ordered_ids:
            counts[source_name]["ordered"] += 1
    return counts


def _update_source_intelligence_conversions(
    store: LocalStore,
    *,
    candidates: list[IdeaCandidate],
    observations,
    selected_ids: set[str],
    ordered_ids: set[str],
) -> list[SourceIntelligence]:
    existing = {item.source_name: item for item in store.list_source_intelligence()}
    counts = _conversion_counts_by_source(
        candidates=candidates,
        observations=observations,
        selected_ids=selected_ids,
        ordered_ids=ordered_ids,
    )
    updated_at = _utc_now()
    updated: list[SourceIntelligence] = []
    for source_name, item in existing.items():
        source_counts = counts.get(source_name, {})
        candidate_count = source_counts.get("candidates", 0)
        selected_count = source_counts.get("selected", 0)
        ordered_count = source_counts.get("ordered", 0)
        item.updated_at = updated_at
        item.plan_conversion_rate = (
            round(selected_count / candidate_count, 4) if candidate_count else 0.0
        )
        item.order_conversion_rate = (
            round(ordered_count / candidate_count, 4) if candidate_count else 0.0
        )
        updated.append(item)
    if updated:
        store.save_source_intelligence(updated)
    return updated


def run_reflection(
    config: dict,
    *,
    store: LocalStore | None = None,
    window_start: str = "",
    window_end: str = "",
) -> ReflectionSummary:
    """Create and persist one deterministic reflection summary."""
    store = store or build_local_store(config)
    generated_at = _utc_now()

    observations = [
        item
        for item in store.list_source_observations()
        if _in_window(
            item.observed_at, window_start=window_start, window_end=window_end
        )
    ]
    candidates = [
        item
        for item in store.list_idea_candidates()
        if _in_window(
            item.observed_at, window_start=window_start, window_end=window_end
        )
    ]
    insights = [
        item
        for item in store.list_playbook_insights()
        if _in_window(
            item.observed_at, window_start=window_start, window_end=window_end
        )
    ]
    plans = [
        item
        for item in store.list_trade_plans()
        if _in_window(item.created_at, window_start=window_start, window_end=window_end)
    ]
    orders = [
        item
        for item in store.list_pending_orders()
        if _in_window(item.created_at, window_start=window_start, window_end=window_end)
    ]
    fills = [
        item
        for item in store.list_shadow_fills()
        if _in_window(item.filled_at, window_start=window_start, window_end=window_end)
    ]

    selected_ids = _selected_idea_ids(plans)
    ordered_ids = {order.idea_id for order in orders if order.idea_id}
    filled_ids = {fill.idea_id for fill in fills if fill.idea_id}
    preflight_ok = sum(1 for order in orders if order.broker_status == "PREFLIGHT_OK")

    _update_source_intelligence_conversions(
        store,
        candidates=candidates,
        observations=observations,
        selected_ids=selected_ids,
        ordered_ids=ordered_ids,
    )

    scorecards = store.list_source_intelligence()
    noisy_sources = [
        item.source_name
        for item in scorecards
        if item.sample_size >= 3
        and item.idea_rate == 0
        and item.playbook_rate == 0
        and item.digest_rate < 0.25
    ]
    high_value_sources = [
        item.source_name
        for item in scorecards
        if item.sample_size >= 1
        and (item.idea_rate >= 0.4 or item.playbook_rate >= 0.4)
    ]
    notes: list[str] = []
    if candidates and not selected_ids:
        notes.append(
            "Idea candidates existed, but no candidate was selected by a plan."
        )
    if selected_ids and not ordered_ids:
        notes.append("Plans selected ideas, but no pending orders were emitted.")
    if orders and not fills:
        notes.append(
            "Orders existed, but no shadow fills were recorded in this window."
        )
    if fills:
        notes.append(
            "Shadow fills recorded; outcome review can use shadow portfolio state."
        )

    summary = ReflectionSummary(
        summary_id=_stable_summary_id(window_start, window_end, generated_at),
        generated_at=generated_at,
        window_start=window_start,
        window_end=window_end,
        source_observation_count=len(observations),
        idea_candidate_count=len(candidates),
        promotable_candidate_count=sum(1 for item in candidates if item.promotable),
        playbook_insight_count=len(insights),
        trade_plan_count=len(plans),
        execute_plan_count=sum(
            1 for plan in plans if plan.decision == PlanDecision.EXECUTE
        ),
        pending_order_count=len(orders),
        preflight_ok_count=preflight_ok,
        shadow_fill_count=len(fills),
        selected_idea_ids=sorted(selected_ids),
        ordered_idea_ids=sorted(ordered_ids),
        shadow_filled_idea_ids=sorted(filled_ids),
        noisy_sources=sorted(noisy_sources),
        high_value_sources=sorted(high_value_sources),
        notes=notes,
    )
    store.save_reflection_summaries([summary])
    logger.info(
        "Reflection summary %s: observations=%d candidates=%d plans=%d orders=%d fills=%d",
        summary.summary_id,
        summary.source_observation_count,
        summary.idea_candidate_count,
        summary.trade_plan_count,
        summary.pending_order_count,
        summary.shadow_fill_count,
    )
    return summary
