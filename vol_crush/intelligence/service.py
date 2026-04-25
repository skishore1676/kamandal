"""Deterministic intelligence artifact creation for source intake."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Mapping

from vol_crush.core.models import (
    IdeaCandidate,
    PlaybookInsight,
    RawSourceDocument,
    SourceIntelligence,
    SourceObservation,
    TradeIdea,
)
from vol_crush.integrations.storage import LocalStore


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "|".join(str(part or "") for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _summary_for_document(
    document: RawSourceDocument,
    summaries_by_document_id: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[str, Any]:
    if not summaries_by_document_id:
        return {}
    return summaries_by_document_id.get(document.document_id, {}) or {}


def _ideas_for_documents(
    documents: list[RawSourceDocument],
    ideas: list[TradeIdea],
) -> dict[str, list[TradeIdea]]:
    by_doc_id: dict[str, list[TradeIdea]] = {
        document.document_id: [] for document in documents
    }
    url_to_doc = {
        document.url: document.document_id for document in documents if document.url
    }
    video_to_doc = {
        str((document.metadata or {}).get("video_id")): document.document_id
        for document in documents
        if (document.metadata or {}).get("video_id")
    }
    for idea in ideas:
        doc_id = ""
        if idea.source_url and idea.source_url in url_to_doc:
            doc_id = url_to_doc[idea.source_url]
        elif idea.video_id and idea.video_id in video_to_doc:
            doc_id = video_to_doc[idea.video_id]
        if doc_id:
            by_doc_id.setdefault(doc_id, []).append(idea)
    return by_doc_id


def _content_type(document: RawSourceDocument, summary: Mapping[str, Any]) -> str:
    if summary.get("category"):
        return str(summary["category"])
    title = (document.title or "").lower()
    if any(token in title for token in ("basics", "explained", "beginner")):
        return "basics"
    if summary.get("macro_view") or summary.get("vol_view"):
        return "macro"
    return "commentary"


def _lane_assignment(
    *,
    idea_count: int,
    summary: Mapping[str, Any],
    playbook_value: bool,
    operator_value: bool,
) -> list[str]:
    lanes: list[str] = []
    if idea_count or bool(summary.get("actionable_ideas_present")):
        lanes.append("trade_idea")
    if operator_value:
        lanes.append("operator_digest")
    if playbook_value:
        lanes.append("playbook")
    if not lanes:
        lanes.append("noise")
    return lanes


def _summary_operator_value(summary: Mapping[str, Any]) -> bool:
    return any(
        str(summary.get(key) or "").strip()
        for key in ("headline", "macro_view", "vol_view", "risks")
    )


def _summary_playbook_value(summary: Mapping[str, Any]) -> bool:
    strategies = summary.get("strategies_discussed") or []
    return bool(strategies) and not bool(summary.get("actionable_ideas_present"))


def _evidence_snippets(
    document: RawSourceDocument,
    doc_ideas: list[TradeIdea],
    summary: Mapping[str, Any],
) -> list[str]:
    snippets: list[str] = []
    headline = str(summary.get("headline") or "").strip()
    if headline:
        snippets.append(headline[:240])
    for idea in doc_ideas[:2]:
        if idea.description:
            snippets.append(idea.description[:240])
    if not snippets and document.summary:
        snippets.append(document.summary[:240])
    if not snippets and document.text:
        snippets.append(" ".join(document.text.split())[:240])
    return snippets


def _observation_for_document(
    document: RawSourceDocument,
    doc_ideas: list[TradeIdea],
    summary: Mapping[str, Any],
    *,
    observed_at: str,
) -> SourceObservation:
    playbook_value = _summary_playbook_value(summary)
    operator_value = _summary_operator_value(summary)
    actionable = bool(doc_ideas) or bool(summary.get("actionable_ideas_present"))
    lanes = _lane_assignment(
        idea_count=len(doc_ideas),
        summary=summary,
        playbook_value=playbook_value,
        operator_value=operator_value,
    )
    return SourceObservation(
        observation_id=_stable_id("srcobs", document.document_id),
        source_id=document.document_id,
        observed_at=observed_at,
        source_type=document.source_type,
        source_name=document.source_name,
        title=document.title,
        url=document.url,
        published_at=document.published_at,
        content_type=_content_type(document, summary),
        lane_assignment=lanes,
        actionable_ideas_present=actionable,
        idea_count=len(doc_ideas),
        playbook_value=playbook_value,
        operator_value=operator_value,
        confidence="high" if document.text else "medium",
        evidence_snippets=_evidence_snippets(document, doc_ideas, summary),
    )


def _candidate_for_idea(
    idea: TradeIdea,
    *,
    source_id: str,
    observed_at: str,
) -> IdeaCandidate:
    confidence = str(idea.confidence or "medium").lower()
    has_mapping = bool(
        idea.underlying and idea.strategy_type and idea.strategy_type != "other"
    )
    promotable = has_mapping and confidence in {"high", "medium"}
    rejection_reason = ""
    if not has_mapping:
        rejection_reason = "missing_executable_mapping"
    elif not promotable:
        rejection_reason = "low_confidence"
    return IdeaCandidate(
        candidate_id=_stable_id("ideacand", idea.id),
        idea_id=idea.id,
        source_id=source_id,
        observed_at=observed_at,
        underlying=idea.underlying,
        strategy_type=idea.strategy_type,
        description=idea.description,
        rationale=idea.rationale,
        confidence=idea.confidence,
        evidence_snippets=[item for item in (idea.description, idea.rationale) if item][
            :2
        ],
        promotable=promotable,
        rejection_reason=rejection_reason,
        duplicate_cluster_id=_stable_id(
            "dup",
            idea.date,
            idea.underlying,
            idea.strategy_type,
            idea.expiration,
            idea.description[:120],
        ),
        promoted_to_idea_review=promotable,
    )


def _playbook_insight_for_summary(
    document: RawSourceDocument,
    summary: Mapping[str, Any],
    *,
    observed_at: str,
) -> PlaybookInsight | None:
    strategies = [str(item) for item in summary.get("strategies_discussed") or []]
    if not strategies or bool(summary.get("actionable_ideas_present")):
        return None
    headline = str(summary.get("headline") or "").strip()
    vol_view = str(summary.get("vol_view") or "").strip()
    macro_view = str(summary.get("macro_view") or "").strip()
    lesson_parts = [part for part in (headline, vol_view, macro_view) if part]
    if not lesson_parts:
        return None
    lesson = " ".join(lesson_parts)[:500]
    return PlaybookInsight(
        insight_id=_stable_id("playbook", document.document_id, lesson),
        source_id=document.document_id,
        observed_at=observed_at,
        lesson=lesson,
        applies_to_strategies=strategies,
        evidence_snippets=[lesson[:240]],
        confidence="medium",
        proposal_candidate=False,
    )


def rebuild_source_intelligence(store: LocalStore) -> list[SourceIntelligence]:
    observations = store.list_source_observations()
    by_source: dict[str, list[SourceObservation]] = defaultdict(list)
    for observation in observations:
        by_source[observation.source_name].append(observation)

    updated_at = _utc_now()
    scorecards: list[SourceIntelligence] = []
    for source_name, items in sorted(by_source.items()):
        sample_size = len(items)
        if not sample_size:
            continue
        idea_items = sum(1 for item in items if item.actionable_ideas_present)
        digest_items = sum(1 for item in items if item.operator_value)
        playbook_items = sum(1 for item in items if item.playbook_value)
        low_conf_ideas = sum(
            1
            for item in items
            if item.actionable_ideas_present and item.confidence == "low"
        )
        idea_rate = idea_items / sample_size
        digest_rate = digest_items / sample_size
        playbook_rate = playbook_items / sample_size
        false_positive_rate = low_conf_ideas / idea_items if idea_items else 0.0
        if false_positive_rate > 0.5:
            priority = "low"
        elif idea_rate >= 0.4 or playbook_rate >= 0.4:
            priority = "high"
        else:
            priority = "normal"
        scorecards.append(
            SourceIntelligence(
                source_name=source_name,
                updated_at=updated_at,
                sample_size=sample_size,
                idea_rate=round(idea_rate, 4),
                digest_rate=round(digest_rate, 4),
                playbook_rate=round(playbook_rate, 4),
                false_positive_rate=round(false_positive_rate, 4),
                current_intake_priority=priority,
            )
        )
    if scorecards:
        store.save_source_intelligence(scorecards)
    return scorecards


def record_intake_artifacts(
    store: LocalStore,
    documents: list[RawSourceDocument],
    ideas: list[TradeIdea],
    *,
    summaries_by_document_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[SourceObservation], list[IdeaCandidate], list[PlaybookInsight]]:
    """Persist deterministic intake artifacts for documents and extracted ideas."""
    observed_at = _utc_now()
    ideas_by_doc = _ideas_for_documents(documents, ideas)
    observations: list[SourceObservation] = []
    candidates: list[IdeaCandidate] = []
    insights: list[PlaybookInsight] = []

    for document in documents:
        doc_ideas = ideas_by_doc.get(document.document_id, [])
        summary = _summary_for_document(document, summaries_by_document_id)
        observations.append(
            _observation_for_document(
                document,
                doc_ideas,
                summary,
                observed_at=observed_at,
            )
        )
        for idea in doc_ideas:
            candidates.append(
                _candidate_for_idea(
                    idea,
                    source_id=document.document_id,
                    observed_at=observed_at,
                )
            )
        insight = _playbook_insight_for_summary(
            document,
            summary,
            observed_at=observed_at,
        )
        if insight is not None:
            insights.append(insight)

    if observations:
        store.save_source_observations(observations)
    if candidates:
        store.save_idea_candidates(candidates)
    if insights:
        store.save_playbook_insights(insights)
    rebuild_source_intelligence(store)
    return observations, candidates, insights
