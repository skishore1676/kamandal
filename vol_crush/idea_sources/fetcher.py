"""CLI orchestration for source-adapter based idea acquisition."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from vol_crush.core.config import (
    get_data_dir,
    get_transcripts_dir,
    load_config,
)
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import (
    IdeaStatus,
    RawContentStatus,
    RawSourceDocument,
    SourceType,
    TradeIdea,
)
from vol_crush.idea_scraper.scraper import (
    dedupe_trade_ideas,
    extract_ideas_from_raw_documents,
    summarize_transcript,
)
from vol_crush.idea_scraper.summary_archive import write_summary
from vol_crush.idea_sources.adapters import (
    GenericWebAdapter,
    RssFeedAdapter,
    TranscriptDirectoryAdapter,
    YouTubeChannelAdapter,
)
from vol_crush.idea_sources.transcript_archive import (
    purge_older_than,
    write_transcript,
)
from vol_crush.integrations.llm import build_llm_client
from vol_crush.integrations.storage import build_local_store

logger = logging.getLogger("vol_crush.idea_sources.fetcher")


def _dedupe_documents(
    existing: list[RawSourceDocument], incoming: list[RawSourceDocument]
) -> tuple[list[RawSourceDocument], int]:
    fingerprints = {document.fingerprint for document in existing}
    kept = []
    duplicates = 0
    for document in incoming:
        if document.fingerprint in fingerprints:
            document.status = RawContentStatus.DUPLICATE.value
            duplicates += 1
        else:
            fingerprints.add(document.fingerprint)
            kept.append(document)
    return kept, duplicates


def _new_unique_ideas(
    existing: list[TradeIdea], incoming: list[TradeIdea]
) -> list[TradeIdea]:
    existing_keys = {
        (
            idea.date,
            idea.underlying.upper(),
            idea.strategy_type,
            idea.expiration,
            " ".join(idea.description.lower().split())[:120],
        )
        for idea in existing
    }
    unique = []
    for idea in dedupe_trade_ideas(incoming):
        key = (
            idea.date,
            idea.underlying.upper(),
            idea.strategy_type,
            idea.expiration,
            " ".join(idea.description.lower().split())[:120],
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)
        unique.append(idea)
    return unique


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vol Crush source fetcher")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--source", choices=["youtube", "rss", "web", "transcripts"], required=True
    )
    parser.add_argument("--channel-id", action="append", default=[])
    parser.add_argument("--feed-url", action="append", default=[])
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--transcripts-dir", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--extract-ideas", action="store_true")
    return parser.parse_args()


def _resolve_archive_roots(config: dict) -> tuple[Path, Path, int]:
    """Return (transcripts_archive_root, summaries_root, retention_days)."""
    data_dir = get_data_dir()
    sources_cfg = config.get("idea_sources") or {}
    archive_cfg = sources_cfg.get("transcripts_archive") or {}
    summaries_cfg = sources_cfg.get("summaries_archive") or {}

    transcripts_root = Path(
        archive_cfg.get("path") or (data_dir / "transcripts" / "archive")
    )
    summaries_root = Path(summaries_cfg.get("path") or (data_dir / "ideas"))
    retention_days = int(archive_cfg.get("retention_days") or 14)
    return transcripts_root, summaries_root, retention_days


def _youtube_title_filters(config: dict) -> tuple[list[str], list[str]]:
    yt_cfg = (config.get("idea_sources") or {}).get("youtube") or {}
    include = list(yt_cfg.get("title_include_keywords") or [])
    exclude = list(yt_cfg.get("title_exclude_keywords") or [])
    return include, exclude


def run_source_fetch(
    config: dict,
    source: str,
    *,
    channel_ids: list[str] | None = None,
    feed_urls: list[str] | None = None,
    urls: list[str] | None = None,
    transcripts_dir: Path | None = None,
    limit: int = 5,
    extract_ideas: bool = False,
    generate_summaries: bool = True,
) -> tuple[list[RawSourceDocument], list[TradeIdea], list[str]]:
    store = build_local_store(config)
    existing = store.list_raw_documents()
    fetched: list[RawSourceDocument] = []
    notes: list[str] = []

    transcripts_archive_root, summaries_root, retention_days = _resolve_archive_roots(
        config
    )
    # Opportunistic cleanup — cheap and keeps the archive bounded.
    purge_older_than(transcripts_archive_root, retention_days=retention_days)

    if source == "youtube":
        adapter = YouTubeChannelAdapter()
        channel_ids = channel_ids or config.get("idea_sources", {}).get(
            "youtube", {}
        ).get("channel_ids", [])
        if not limit:
            limit = config.get("idea_sources", {}).get("youtube", {}).get("limit", 5)
        include_keywords, exclude_keywords = _youtube_title_filters(config)
        for channel_id in channel_ids:
            result = adapter.fetch(
                channel_id=channel_id,
                limit=limit,
                title_include_keywords=include_keywords,
                title_exclude_keywords=exclude_keywords,
            )
            fetched.extend(result.documents)
            notes.extend(result.notes)
    elif source == "rss":
        adapter = RssFeedAdapter()
        feed_urls = feed_urls or config.get("idea_sources", {}).get("rss", {}).get(
            "feed_urls", []
        )
        if not limit:
            limit = config.get("idea_sources", {}).get("rss", {}).get("limit", 5)
        for feed_url in feed_urls:
            result = adapter.fetch(feed_url=feed_url, limit=limit, source_name="rss")
            fetched.extend(result.documents)
            notes.extend(result.notes)
    elif source == "web":
        urls = urls or config.get("idea_sources", {}).get("web", {}).get("urls", [])
        result = GenericWebAdapter().fetch(urls=urls, source_name="web")
        fetched.extend(result.documents)
        notes.extend(result.notes)
    elif source == "transcripts":
        transcripts_dir = transcripts_dir or Path(
            config.get("idea_sources", {})
            .get("transcripts", {})
            .get("path", get_transcripts_dir())
        )
        result = TranscriptDirectoryAdapter().fetch(transcripts_dir)
        fetched.extend(result.documents)
        notes.extend(result.notes)

    # Mirror freshly fetched transcripts to disk (audit/retention window).
    for document in fetched:
        if document.source_type == SourceType.YOUTUBE.value:
            try:
                write_transcript(transcripts_archive_root, document)
            except OSError as exc:
                logger.warning(
                    "failed to archive transcript for %s: %s",
                    document.document_id,
                    exc,
                )

    kept, duplicates = _dedupe_documents(existing, fetched)
    store.save_raw_documents(fetched)
    ideas: list[TradeIdea] = []
    notes.append(
        f"fetched {len(fetched)} raw documents ({len(kept)} new, {duplicates} duplicates)"
    )

    if not extract_ideas or not kept:
        return kept, ideas, notes

    try:
        llm = build_llm_client(config)
    except RuntimeError as exc:
        notes.append(f"{exc}; raw documents saved but idea extraction skipped.")
        return kept, ideas, notes

    # Summaries first — one LLM call per new transcript, saved to disk for
    # human review regardless of whether actionable ideas get extracted.
    if generate_summaries:
        _run_summary_pass(llm, kept, summaries_root, notes)

    ideas = extract_ideas_from_raw_documents(llm, kept)
    unique_new_ideas = _new_unique_ideas(store.list_trade_ideas(), ideas)
    for document in kept:
        document.status = RawContentStatus.EXTRACTED.value
    store.save_raw_documents(kept)
    if unique_new_ideas:
        for idea in unique_new_ideas:
            idea.status = IdeaStatus.NEW.value
        store.save_trade_ideas(unique_new_ideas)
    logger.info(
        "Extracted %d ideas from %d new documents (%d unique after dedupe)",
        len(ideas),
        len(kept),
        len(unique_new_ideas),
    )
    return kept, unique_new_ideas, notes


def _run_summary_pass(
    llm,
    documents: list[RawSourceDocument],
    summaries_root: Path,
    notes: list[str],
) -> None:
    for document in documents:
        text = (document.text or "").strip()
        if not text:
            continue
        try:
            summary = summarize_transcript(
                llm,
                text,
                source=f"{document.source_type}:{document.source_name}",
                idea_date=(
                    document.published_at[:10] if document.published_at else None
                ),
                source_url=document.url,
                source_title=document.title,
                author=document.author,
            )
        except Exception as exc:  # noqa: BLE001 — any model/network failure is non-fatal here
            notes.append(
                f"summary failed for {document.document_id}: {type(exc).__name__}: {exc}"
            )
            logger.warning(
                "summary generation failed for %s: %s", document.document_id, exc
            )
            continue
        try:
            write_summary(
                summaries_root,
                document,
                summary,
                model=f"{llm.provider}:{llm.model}",
            )
        except OSError as exc:
            notes.append(
                f"summary write failed for {document.document_id}: {exc}"
            )


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    _, _, notes = run_source_fetch(
        config,
        args.source,
        channel_ids=args.channel_id,
        feed_urls=args.feed_url,
        urls=args.url,
        transcripts_dir=args.transcripts_dir,
        limit=args.limit,
        extract_ideas=args.extract_ideas,
    )
    for note in notes:
        logger.info(note)


if __name__ == "__main__":
    main()
