"""CLI orchestration for source-adapter based idea acquisition."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from vol_crush.core.config import get_transcripts_dir, load_config
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import (
    IdeaStatus,
    RawContentStatus,
    RawSourceDocument,
    TradeIdea,
)
from vol_crush.idea_scraper.scraper import (
    dedupe_trade_ideas,
    extract_ideas_from_raw_documents,
)
from vol_crush.idea_sources.adapters import (
    GenericWebAdapter,
    RssFeedAdapter,
    TranscriptDirectoryAdapter,
    YouTubeChannelAdapter,
)
from vol_crush.integrations.llm import LLMClient
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
) -> tuple[list[RawSourceDocument], list[TradeIdea], list[str]]:
    store = build_local_store(config)
    existing = store.list_raw_documents()
    fetched: list[RawSourceDocument] = []
    notes: list[str] = []

    if source == "youtube":
        adapter = YouTubeChannelAdapter()
        channel_ids = channel_ids or config.get("idea_sources", {}).get(
            "youtube", {}
        ).get("channel_ids", [])
        if not limit:
            limit = config.get("idea_sources", {}).get("youtube", {}).get("limit", 5)
        for channel_id in channel_ids:
            result = adapter.fetch(channel_id=channel_id, limit=limit)
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

    kept, duplicates = _dedupe_documents(existing, fetched)
    store.save_raw_documents(fetched)
    ideas: list[TradeIdea] = []
    notes.append(
        f"fetched {len(fetched)} raw documents ({len(kept)} new, {duplicates} duplicates)"
    )

    if not extract_ideas or not kept:
        return kept, ideas, notes

    openai_key = config.get("openai", {}).get("api_key", "")
    if not openai_key:
        notes.append(
            "OpenAI API key not configured; raw documents saved but idea extraction skipped."
        )
        return kept, ideas, notes

    llm = LLMClient(
        api_key=openai_key, model=config.get("openai", {}).get("model", "gpt-4o")
    )
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
