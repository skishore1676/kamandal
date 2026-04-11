"""
Module 0: Strategy Miner — Entry Point

Usage:
    python -m vol_crush.strategy_miner [--transcripts-dir PATH] [--skip-review] [--extract-only]

Workflow:
    1. Load all transcripts from data/transcripts/
    2. Extract strategy candidates from each transcript via LLM
    3. Save raw candidates to data/extracted_candidates.json
    4. Distill candidates into 3-5 canonical strategies via LLM
    5. Present to human for interactive review
    6. Save approved strategies to config/strategies.yaml
"""

import argparse
import json
import sys
from pathlib import Path

from vol_crush.core.config import (
    get_project_root,
    get_transcripts_dir,
    load_config,
    save_strategies,
)
from vol_crush.core.logging import setup_logging
from vol_crush.integrations.llm import LLMClient
from vol_crush.strategy_miner.distiller import distill_strategies
from vol_crush.strategy_miner.extractor import (
    extract_all,
    save_candidates,
)
from vol_crush.strategy_miner.review import (
    interactive_review,
    print_candidates_summary,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Vol Crush — Module 0: Strategy Miner",
    )
    parser.add_argument(
        "--transcripts-dir",
        type=Path,
        default=None,
        help="Path to transcripts directory (default: data/transcripts/)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="Only extract candidates, don't distill or review",
    )
    parser.add_argument(
        "--skip-review",
        action="store_true",
        help="Skip interactive review (auto-approve all strategies)",
    )
    parser.add_argument(
        "--from-candidates",
        type=Path,
        default=None,
        help="Skip extraction, load candidates from JSON file and go straight to distillation",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Load config
    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    logger.info("Vol Crush — Strategy Miner starting")

    # Validate OpenAI key
    openai_key = config.get("openai", {}).get("api_key", "")
    if not openai_key:
        logger.error(
            "OpenAI API key not configured. Set it in config/config.yaml "
            "or via VOL_CRUSH_OPENAI_API_KEY environment variable."
        )
        sys.exit(1)

    model = config.get("openai", {}).get("model", "gpt-4o")
    llm = LLMClient(api_key=openai_key, model=model)

    project_root = get_project_root()
    transcripts_dir = args.transcripts_dir or get_transcripts_dir()
    candidates_path = project_root / "data" / "extracted_candidates.json"

    # ── Step 1: Extract (or load from file) ──────────────────────────
    if args.from_candidates:
        logger.info("Loading candidates from: %s", args.from_candidates)
        with open(args.from_candidates) as f:
            candidates_data = json.load(f)
        # Convert dicts back to candidate objects for distillation
        from vol_crush.core.models import ExtractedStrategyCandidate

        candidates = [ExtractedStrategyCandidate(**c) for c in candidates_data]
        logger.info("Loaded %d candidates from file", len(candidates))
    else:
        logger.info("Extracting strategies from transcripts in: %s", transcripts_dir)
        candidates = extract_all(llm, transcripts_dir)

        if not candidates:
            logger.error("No candidates extracted. Check transcripts directory.")
            sys.exit(1)

        # Save raw candidates
        save_candidates(candidates, candidates_path)
        logger.info("Raw candidates saved to: %s", candidates_path)

    # Print summary
    from dataclasses import asdict

    candidates_dicts = [asdict(c) for c in candidates]
    print_candidates_summary(candidates_dicts)

    if args.extract_only:
        logger.info("Extract-only mode. Candidates saved. Exiting.")
        return

    # ── Step 2: Distill ──────────────────────────────────────────────
    logger.info(
        "Distilling %d candidates into canonical strategies...", len(candidates)
    )
    strategies_raw, portfolio_guidelines = distill_strategies(llm, candidates)

    if not strategies_raw:
        logger.error("Distillation produced no strategies.")
        sys.exit(1)

    # Save distillation output for reference
    distill_path = project_root / "data" / "distilled_strategies.json"
    with open(distill_path, "w") as f:
        json.dump(
            {
                "strategies": strategies_raw,
                "portfolio_guidelines": portfolio_guidelines,
            },
            f,
            indent=2,
        )
    logger.info("Distilled output saved to: %s", distill_path)

    # ── Step 3: Human Review ─────────────────────────────────────────
    if args.skip_review:
        logger.info("Skipping review (--skip-review). Auto-approving all strategies.")
        approved_raw = strategies_raw
        approved_guidelines = portfolio_guidelines
    else:
        approved_raw, approved_guidelines = interactive_review(
            strategies_raw,
            portfolio_guidelines,
        )

    if not approved_raw:
        logger.warning("No strategies approved. Nothing to save.")
        return

    # ── Step 4: Save to config/strategies.yaml ───────────────────────
    # Mark all as not yet backtested/dry-run
    for strat in approved_raw:
        strat.setdefault("backtest_approved", False)
        strat.setdefault("dry_run_passed", False)

    strategies_path = save_strategies(approved_raw)
    logger.info(
        "Saved %d approved strategies to: %s", len(approved_raw), strategies_path
    )

    # Also update portfolio constraints in config if guidelines were produced
    if approved_guidelines:
        guidelines_path = project_root / "data" / "portfolio_guidelines.json"
        with open(guidelines_path, "w") as f:
            json.dump(approved_guidelines, f, indent=2)
        logger.info("Portfolio guidelines saved to: %s", guidelines_path)
        logger.info(
            "NOTE: Review portfolio_guidelines.json and manually merge "
            "relevant values into config/config.yaml under portfolio.constraints"
        )

    # ── Done ─────────────────────────────────────────────────────────
    logger.info("Strategy Miner complete.")
    logger.info("Next steps:")
    logger.info("  1. Review config/strategies.yaml")
    logger.info("  2. Review data/portfolio_guidelines.json")
    logger.info("  3. Run backtester to validate strategies")
    logger.info("  4. Run dry run for each approved strategy")


if __name__ == "__main__":
    main()
