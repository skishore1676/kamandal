"""CLI for the LLM comparison harness: run N models against an archived transcript."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from vol_crush.core.config import get_data_dir, load_config
from vol_crush.core.logging import setup_logging
from vol_crush.idea_sources.fetcher import _resolve_archive_roots
from vol_crush.llm_compare.service import run_comparison

logger = logging.getLogger("vol_crush.llm_compare.cli")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run multiple LLMs against a previously archived transcript and "
            "produce a side-by-side JSON + markdown report."
        )
    )
    p.add_argument("--config", type=Path, default=None)
    p.add_argument(
        "--video-id",
        required=True,
        help="YouTube video id (matches <video_id>.txt in the transcript archive)",
    )
    p.add_argument(
        "--models",
        required=True,
        help="Comma-separated model ids (provider-prefixed on OpenRouter)",
    )
    p.add_argument(
        "--provider",
        default=None,
        help="Override provider (default: llm.provider from config)",
    )
    p.add_argument(
        "--fallback-model",
        default=None,
        help="Fallback for each model when it errors (default: llm.fallback_model)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Where to write the comparison report (default: "
            "<data_dir>/llm_comparisons)"
        ),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config.get("app", {}).get("log_level", "INFO"))

    llm_cfg = config.get("llm") or {}
    api_key = llm_cfg.get("api_key") or ""
    if not api_key:
        logger.error(
            "LLM api_key missing — set VOL_CRUSH_LLM_API_KEY in .env before running"
        )
        sys.exit(1)

    provider = args.provider or (llm_cfg.get("provider") or "openrouter").lower()
    fallback = args.fallback_model if args.fallback_model is not None else (
        llm_cfg.get("fallback_model") or None
    )
    base_url = llm_cfg.get("base_url") or None
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        logger.error("--models produced no entries after splitting on ','")
        sys.exit(1)

    transcripts_archive_root, _, _ = _resolve_archive_roots(config)
    output_root = args.output_dir or (get_data_dir() / "llm_comparisons")

    md_path = run_comparison(
        video_id=args.video_id,
        models=models,
        api_key=api_key,
        provider=provider,
        fallback_model=fallback,
        base_url=base_url,
        archive_root=transcripts_archive_root,
        output_root=output_root,
    )
    logger.info("comparison report: %s", md_path)
    print(md_path)


if __name__ == "__main__":
    main()
