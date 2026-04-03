"""Daily dry-run orchestration entrypoint for Vol Crush."""

from __future__ import annotations

import argparse
from pathlib import Path

from vol_crush.backtester.service import run_backtests
from vol_crush.core.config import load_config
from vol_crush.core.logging import setup_logging
from vol_crush.executor.service import execute_latest_plan
from vol_crush.idea_sources.fetcher import run_source_fetch
from vol_crush.integrations.fixtures import (
    FixtureMarketDataProvider,
    build_fixture_payload,
    write_fixture_artifacts,
)
from vol_crush.integrations.storage import build_local_store
from vol_crush.optimizer.service import build_trade_plan
from vol_crush.portfolio_sync.service import sync_public_portfolio
from vol_crush.position_manager.service import evaluate_positions


def main() -> None:
    parser = argparse.ArgumentParser(description="Vol Crush daily dry-run pipeline")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--skip-backtest", action="store_true", help="Skip replay gate step")
    parser.add_argument(
        "--fetch-sources",
        nargs="*",
        choices=["youtube", "rss", "web", "transcripts"],
        default=[],
        help="Optionally fetch fresh source content before optimization",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    store = build_local_store(config)

    for source in args.fetch_sources:
        documents, ideas, notes = run_source_fetch(config, source, extract_ideas=True)
        logger.info(
            "Source fetch [%s]: %d documents, %d ideas",
            source,
            len(documents),
            len(ideas),
        )
        for note in notes:
            logger.info(note)

    payload, replay_trades = build_fixture_payload(config)
    bundle_path, replay_path = write_fixture_artifacts(config, payload, replay_trades)
    store.save_fixture_payload(payload)
    store.save_replay_trades(replay_trades)
    provider = FixtureMarketDataProvider(bundle_path)
    logger.info("Fixture refresh complete: %s and %s", bundle_path, replay_path)

    if not args.skip_backtest:
        results = run_backtests(config)
        logger.info("Backtest gate refreshed for %d strategies", len(results))

    if (
        config.get("broker", {}).get("active") == "public"
        and config.get("broker", {}).get("public", {}).get("sync_portfolio_before_optimizer", True)
    ):
        try:
            snapshot = sync_public_portfolio(config, store=store)
            logger.info(
                "Public portfolio sync complete: positions=%d nlv=%.2f",
                snapshot.position_count,
                snapshot.net_liquidation_value,
            )
        except Exception as exc:
            logger.warning("Public portfolio sync failed; continuing with local state: %s", exc)

    plan = build_trade_plan(store, config, provider)
    store.save_trade_plan(plan)
    logger.info("Optimizer decision=%s for plan %s", plan.decision.value, plan.plan_id)

    orders = execute_latest_plan(config)
    logger.info("Pending executor emitted %d orders", len(orders))

    position_actions = evaluate_positions(config)
    logger.info("Position manager emitted %d actions", len(position_actions))


if __name__ == "__main__":
    main()
