"""
Module 1: Live Idea Scraper — Entry Point

Usage:
    python -m vol_crush.idea_scraper --mode live --duration 300
    python -m vol_crush.idea_scraper --mode record --file data/audio/segment.wav
    python -m vol_crush.idea_scraper --mode transcript --file data/transcripts/file.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vol_crush.core.config import load_config, get_project_root
from vol_crush.core.logging import setup_logging
from vol_crush.integrations.llm import build_llm_client
from vol_crush.integrations.storage import build_local_store
from vol_crush.idea_scraper.scraper import (
    capture_from_audio_file,
    capture_from_transcript_file,
    record_audio,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vol Crush — Module 1: Idea Scraper")
    parser.add_argument(
        "--mode",
        choices=["live", "record", "transcript"],
        required=True,
        help="live=capture mic audio; record=process audio file; transcript=process text file",
    )
    parser.add_argument("--file", type=Path, help="Audio or transcript file path")
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Recording duration in seconds (live mode)",
    )
    parser.add_argument("--source", default="YouTube", help="Content source label")
    parser.add_argument("--config", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    logger.info("Vol Crush — Idea Scraper starting (mode=%s)", args.mode)

    try:
        llm = build_llm_client(config)
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if args.mode in ("live", "record") and llm.provider != "openai":
        logger.error(
            "Audio transcription requires Whisper (provider=openai). "
            "Current provider=%s. Use --mode transcript or switch llm.provider.",
            llm.provider,
        )
        sys.exit(1)
    store = build_local_store(config)

    if args.mode == "live":
        audio_cfg = config.get("audio", {})
        audio_path = record_audio(
            duration_seconds=args.duration,
            sample_rate=audio_cfg.get("sample_rate", 16000),
            channels=audio_cfg.get("channels", 1),
        )
        ideas = capture_from_audio_file(llm, audio_path, source=args.source)

    elif args.mode == "record":
        if not args.file or not args.file.exists():
            logger.error("--file required for record mode")
            sys.exit(1)
        ideas = capture_from_audio_file(llm, args.file, source=args.source)

    elif args.mode == "transcript":
        if not args.file or not args.file.exists():
            logger.error("--file required for transcript mode")
            sys.exit(1)
        ideas = capture_from_transcript_file(llm, args.file, source=args.source)

    else:
        logger.error("Unknown mode: %s", args.mode)
        sys.exit(1)

    # Output results
    logger.info("Captured %d trade ideas:", len(ideas))
    for idea in ideas:
        logger.info(
            "  [%s] %s %s on %s — %s",
            idea.confidence,
            idea.trader_name,
            idea.strategy_type,
            idea.underlying,
            idea.description[:60],
        )

    store.save_trade_ideas(ideas)
    logger.info(
        "Ideas saved to local store at %s",
        get_project_root()
        / config.get("storage", {})
        .get("local", {})
        .get("sqlite_path", "data/vol_crush.db"),
    )


if __name__ == "__main__":
    main()
