"""CLI entrypoint for sheet sync: `python -m vol_crush.sheets_sync {bootstrap|pull|push}`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from vol_crush.core.config import load_config
from vol_crush.core.logging import setup_logging
from vol_crush.sheets.sync import bootstrap_sheet, pull_sheet

logger = logging.getLogger("vol_crush.sheets.cli")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the kamandal_control Google Sheet."
    )
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "bootstrap",
        help="Create/ensure tabs, headers, and data-validation dropdowns.",
    )
    sub.add_parser(
        "pull",
        help="Fetch strategies + idea_review → data/sheet_cache/*.json.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    setup_logging(config.get("app", {}).get("log_level", "INFO"))

    if args.command == "bootstrap":
        notes = bootstrap_sheet(config)
        for note in notes:
            logger.info(note)
        return

    if args.command == "pull":
        report = pull_sheet(config)
        if report.strategies:
            logger.info(
                "strategies: %d rows, changed=%s, stamped=%d → %s",
                report.strategies.rows_fetched,
                report.strategies.changed,
                report.strategies.stamped_rows,
                report.strategies.cache_path,
            )
        if report.template_library:
            logger.info(
                "template_library: %d rows, changed=%s → %s",
                report.template_library.rows_fetched,
                report.template_library.changed,
                report.template_library.cache_path,
            )
        if report.regime_control:
            logger.info(
                "regime_control: %d rows, changed=%s → %s",
                report.regime_control.rows_fetched,
                report.regime_control.changed,
                report.regime_control.cache_path,
            )
        if report.profiles:
            logger.info(
                "profiles: %d rows, changed=%s → %s",
                report.profiles.rows_fetched,
                report.profiles.changed,
                report.profiles.cache_path,
            )
        if report.universe:
            logger.info(
                "universe: %d rows, changed=%s → %s",
                report.universe.rows_fetched,
                report.universe.changed,
                report.universe.cache_path,
            )
        if report.idea_review:
            logger.info(
                "idea_review: %d rows, changed=%s, stamped=%d → %s",
                report.idea_review.rows_fetched,
                report.idea_review.changed,
                report.idea_review.stamped_rows,
                report.idea_review.cache_path,
            )
        if report.daily_plan:
            logger.info(
                "daily_plan: %d rows, changed=%s → %s",
                report.daily_plan.rows_fetched,
                report.daily_plan.changed,
                report.daily_plan.cache_path,
            )
        for err in report.errors:
            logger.warning(err)
        if report.errors:
            sys.exit(1)
        return


if __name__ == "__main__":
    main()
