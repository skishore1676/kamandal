"""CLI entrypoint for Kamandal reflection summaries."""

from __future__ import annotations

import argparse
from pathlib import Path

from vol_crush.core.config import load_config
from vol_crush.core.logging import setup_logging
from vol_crush.reflection.service import run_reflection


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Kamandal reflection summary")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--window-start", default="", help="ISO lower bound")
    parser.add_argument("--window-end", default="", help="ISO upper bound")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    summary = run_reflection(
        config,
        window_start=args.window_start,
        window_end=args.window_end,
    )
    logger.info(
        "Reflection complete: %s observations=%d candidates=%d fills=%d",
        summary.summary_id,
        summary.source_observation_count,
        summary.idea_candidate_count,
        summary.shadow_fill_count,
    )


if __name__ == "__main__":
    main()
