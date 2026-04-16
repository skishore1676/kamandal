"""Retry transcript acquisition for previously failed raw documents.

Use case: YouTube live streams disable captions during broadcast but expose
auto-generated captions 6–24h after the stream ends. Running this periodically
picks those up without paying for audio transcription.

Also handles paid audio fallback when a GroqWhisper (or similar) provider is
enabled in config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vol_crush.core.models import (
    IdeaStatus,
    RawContentStatus,
    RawSourceDocument,
    SourceType,
    TradeIdea,
)
from vol_crush.idea_scraper.scraper import (
    dedupe_trade_ideas,
    extract_ideas_from_transcript,
    summarize_transcript,
)
from vol_crush.idea_scraper.summary_archive import write_summary
from vol_crush.idea_sources.fetcher import _resolve_archive_roots
from vol_crush.idea_sources.transcript_archive import write_transcript
from vol_crush.integrations.llm import build_llm_client
from vol_crush.integrations.storage import build_local_store
from vol_crush.transcript_providers import ProviderChain, build_chain

logger = logging.getLogger("vol_crush.idea_sources.retry_transcripts")


@dataclass
class RetryReport:
    considered: int = 0
    skipped_too_young: int = 0
    skipped_too_old: int = 0
    skipped_wrong_source: int = 0
    skipped_already_has_transcript: int = 0
    recovered_documents: list[dict[str, str]] = field(default_factory=list)
    still_missing: list[dict[str, str]] = field(default_factory=list)
    new_ideas: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "skipped_too_young": self.skipped_too_young,
            "skipped_too_old": self.skipped_too_old,
            "skipped_wrong_source": self.skipped_wrong_source,
            "skipped_already_has_transcript": self.skipped_already_has_transcript,
            "recovered_documents": self.recovered_documents,
            "still_missing": self.still_missing,
            "new_ideas": self.new_ideas,
            "errors": self.errors,
        }


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        # fromisoformat handles "2026-04-14T21:32:50+00:00"; strip a trailing Z.
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def retry_missing_transcripts(
    config: dict[str, Any],
    *,
    min_age_hours: float | None = None,
    max_age_hours: float | None = None,
    source_type: str = SourceType.YOUTUBE.value,
    dry_run: bool = False,
    chain: ProviderChain | None = None,
    now: datetime | None = None,
) -> RetryReport:
    """Re-attempt transcript fetch for raw documents still marked has_transcript=false.

    Age window is (published_at + min_age_hours) ≤ now ≤ (published_at + max_age_hours).
    Defaults come from ``idea_sources.transcripts.retry`` config:
        min_age_hours: 20
        max_age_hours: 168   # 7 days

    Returns a :class:`RetryReport` summarizing actions taken.
    """
    retry_cfg = (
        (config.get("idea_sources") or {}).get("transcripts") or {}
    ).get("retry") or {}
    min_age_hours = (
        min_age_hours
        if min_age_hours is not None
        else float(retry_cfg.get("min_age_hours", 20))
    )
    max_age_hours = (
        max_age_hours
        if max_age_hours is not None
        else float(retry_cfg.get("max_age_hours", 168))
    )
    now = now or datetime.now(UTC)
    min_age = timedelta(hours=min_age_hours)
    max_age = timedelta(hours=max_age_hours)

    store = build_local_store(config)
    documents = store.list_raw_documents(source_type=source_type)
    transcripts_archive_root, summaries_root, _ = _resolve_archive_roots(config)

    report = RetryReport()

    # Lazily build the chain + LLM client — callers can pass a custom chain
    # (useful for tests). LLM is only needed if we actually recover a doc.
    if chain is None:
        chain = build_chain(
            (config.get("idea_sources") or {}).get("transcripts") or {}
        )

    llm = None
    collected_new_ideas: list[TradeIdea] = []

    for document in documents:
        meta = document.metadata or {}
        if source_type and document.source_type != source_type:
            report.skipped_wrong_source += 1
            continue
        if meta.get("has_transcript"):
            report.skipped_already_has_transcript += 1
            continue

        report.considered += 1

        published_at = _parse_iso(document.published_at)
        if published_at is not None:
            age = now - published_at
            if age < min_age:
                report.skipped_too_young += 1
                continue
            if age > max_age:
                report.skipped_too_old += 1
                continue

        url = document.url
        video_id = meta.get("video_id", "")
        result = chain.fetch(url, {"video_id": video_id})

        doc_ref = {
            "document_id": document.document_id,
            "video_id": video_id,
            "title": document.title,
            "url": url,
        }
        if not result.text:
            doc_ref["failed_providers"] = str(
                result.metadata.get("failed_providers") or []
            )
            report.still_missing.append(doc_ref)
            continue

        doc_ref["provider"] = result.provider
        doc_ref["chars"] = str(len(result.text))
        if result.cost_estimate_usd:
            doc_ref["cost_estimate_usd"] = f"{result.cost_estimate_usd:.4f}"
        report.recovered_documents.append(doc_ref)

        if dry_run:
            continue

        # Mutate the document in place, persist back, archive the transcript.
        document.text = result.text
        meta["has_transcript"] = True
        if result.provider:
            meta["transcript_provider"] = result.provider
        document.metadata = meta
        document.status = RawContentStatus.NEW.value

        try:
            write_transcript(transcripts_archive_root, document)
        except OSError as exc:
            report.errors.append(
                f"archive write failed for {document.document_id}: {exc}"
            )

        store.save_raw_documents([document])

        # LLM-driven summary + extraction — reuse the live pipeline code paths.
        if llm is None:
            try:
                llm = build_llm_client(config)
            except RuntimeError as exc:
                report.errors.append(str(exc))
                break

        _summarize_and_write(llm, document, summaries_root, report)
        ideas = _extract_ideas(llm, document)
        if ideas:
            collected_new_ideas.extend(ideas)
            document.status = RawContentStatus.EXTRACTED.value
            store.save_raw_documents([document])

    if collected_new_ideas and not dry_run:
        existing_keys = _idea_keyset(store.list_trade_ideas())
        unique = []
        for idea in dedupe_trade_ideas(collected_new_ideas):
            key = _idea_key(idea)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            idea.status = IdeaStatus.NEW.value
            unique.append(idea)
        if unique:
            store.save_trade_ideas(unique)
        report.new_ideas = len(unique)

    return report


def _summarize_and_write(
    llm, document: RawSourceDocument, summaries_root: Path, report: RetryReport
) -> None:
    try:
        summary = summarize_transcript(
            llm,
            document.text,
            source=f"{document.source_type}:{document.source_name}",
            idea_date=(
                document.published_at[:10] if document.published_at else None
            ),
            source_url=document.url,
            source_title=document.title,
            author=document.author,
        )
    except Exception as exc:  # noqa: BLE001
        report.errors.append(
            f"summary failed for {document.document_id}: {type(exc).__name__}: {exc}"
        )
        return
    try:
        write_summary(
            summaries_root,
            document,
            summary,
            model=f"{llm.provider}:{llm.model}",
        )
    except OSError as exc:
        report.errors.append(
            f"summary write failed for {document.document_id}: {exc}"
        )


def _extract_ideas(llm, document: RawSourceDocument) -> list[TradeIdea]:
    meta = document.metadata or {}
    try:
        return extract_ideas_from_transcript(
            llm,
            document.text,
            source=f"{document.source_type}:{document.source_name}",
            idea_date=(
                document.published_at[:10] if document.published_at else None
            ),
            source_url=document.url,
            source_title=document.title,
            author=document.author,
            video_id=meta.get("video_id", ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "extraction failed for %s: %s: %s",
            document.document_id,
            type(exc).__name__,
            exc,
        )
        return []


def _idea_key(idea: TradeIdea) -> tuple:
    return (
        idea.date,
        idea.underlying.upper(),
        idea.strategy_type,
        idea.expiration,
        " ".join(idea.description.lower().split())[:120],
    )


def _idea_keyset(ideas: list[TradeIdea]) -> set[tuple]:
    return {_idea_key(idea) for idea in ideas}


# ── CLI ─────────────────────────────────────────────────────────────

def _parse_args() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Retry transcript fetch for raw documents currently missing a "
            "transcript. Picks up YouTube auto-captions once they become "
            "available after a live stream, and (if enabled) falls back to "
            "paid audio transcription providers."
        )
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=None,
        help="Only retry docs older than this (default: config.idea_sources.transcripts.retry.min_age_hours, 20)",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=None,
        help="Skip docs older than this (default: 168 = 7 days)",
    )
    parser.add_argument(
        "--source-type",
        default=SourceType.YOUTUBE.value,
        help="Restrict to this source_type (default: youtube)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be retried but do not mutate DB or write files",
    )
    return parser.parse_args()


def main() -> None:
    from vol_crush.core.config import load_config
    from vol_crush.core.logging import setup_logging

    args = _parse_args()
    config = load_config(args.config)
    setup_logging(config.get("app", {}).get("log_level", "INFO"))

    report = retry_missing_transcripts(
        config,
        min_age_hours=args.min_age_hours,
        max_age_hours=args.max_age_hours,
        source_type=args.source_type,
        dry_run=args.dry_run,
    )

    logger.info("considered=%d", report.considered)
    logger.info(
        "skipped: too_young=%d too_old=%d wrong_source=%d already_has_transcript=%d",
        report.skipped_too_young,
        report.skipped_too_old,
        report.skipped_wrong_source,
        report.skipped_already_has_transcript,
    )
    logger.info(
        "recovered=%d still_missing=%d new_ideas=%d errors=%d",
        len(report.recovered_documents),
        len(report.still_missing),
        report.new_ideas,
        len(report.errors),
    )
    for doc in report.recovered_documents:
        logger.info(
            "  + recovered %s (%s chars via %s)%s",
            doc.get("video_id") or doc.get("document_id"),
            doc.get("chars", "?"),
            doc.get("provider", "?"),
            f" ${doc['cost_estimate_usd']}"
            if doc.get("cost_estimate_usd") and float(doc["cost_estimate_usd"]) > 0
            else "",
        )
    for doc in report.still_missing:
        logger.info(
            "  - still missing %s: %s",
            doc.get("video_id") or doc.get("document_id"),
            doc.get("failed_providers", "no providers attempted"),
        )
    for err in report.errors:
        logger.warning("  ! error: %s", err)


if __name__ == "__main__":
    main()
