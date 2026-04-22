"""Daily dry-run orchestration entrypoint for Vol Crush."""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from vol_crush.backtester.service import run_backtests
from vol_crush.core.config import load_config
from vol_crush.core.logging import setup_logging
from vol_crush.core.strategy_aliases import infer_expectation, operator_strategy_label
from vol_crush.executor.service import execute_latest_plan
from vol_crush.idea_scraper.summary_archive import (
    read_recent_summary_records,
)
from vol_crush.idea_sources.fetcher import run_source_fetch
from vol_crush.integrations.fixtures import (
    build_fixture_payload,
    write_fixture_artifacts,
)
from vol_crush.integrations.market_data import build_market_data_provider
from vol_crush.integrations.storage import build_local_store
from vol_crush.optimizer.service import build_trade_plan
from vol_crush.portfolio_sync.service import sync_public_portfolio
from vol_crush.position_manager.service import evaluate_positions


def _sheets_enabled(config: dict, cli_override: bool | None) -> bool:
    """Respect CLI override first, then config/env."""
    if cli_override is False:
        return False
    return bool((config.get("google_sheets") or {}).get("enabled", False))


def _try_sheet_pull(config: dict, logger: logging.Logger) -> None:
    try:
        from vol_crush.sheets.sync import pull_sheet

        report = pull_sheet(config)
        if report.strategies:
            logger.info(
                "Sheet pull: strategies rows=%d changed=%s stamped=%d",
                report.strategies.rows_fetched,
                report.strategies.changed,
                report.strategies.stamped_rows,
            )
        if report.idea_review:
            logger.info(
                "Sheet pull: idea_review rows=%d changed=%s stamped=%d",
                report.idea_review.rows_fetched,
                report.idea_review.changed,
                report.idea_review.stamped_rows,
            )
        if report.daily_plan:
            logger.info(
                "Sheet pull: daily_plan rows=%d changed=%s",
                report.daily_plan.rows_fetched,
                report.daily_plan.changed,
            )
        for err in report.errors:
            logger.warning("Sheet pull error: %s", err)
    except Exception as exc:  # noqa: BLE001 — sheet is best-effort
        logger.warning("Sheet pull failed (%s: %s); continuing", type(exc).__name__, exc)


def _push_recent_ideas_to_sheet(
    config: dict, store, logger: logging.Logger, *, lookback_days: int = 7
) -> None:
    try:
        from vol_crush.sheets.schemas import IdeaReviewRow
        from vol_crush.sheets.sync import push_idea_review

        cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
        rows: list[IdeaReviewRow] = []
        skipped_missing_underlying = 0
        for idea in store.list_trade_ideas():
            if idea.date and idea.date < cutoff:
                continue
            if not str(idea.underlying or "").strip():
                skipped_missing_underlying += 1
                continue
            rows.append(
                IdeaReviewRow(
                    date=idea.date,
                    underlying=idea.underlying,
                    expectation=infer_expectation(idea.strategy_type),
                    proposed_strategy=operator_strategy_label(idea.strategy_type),
                    note=idea.description or idea.rationale,
                    idea_id=idea.id,
                    description=idea.description,
                    rationale=idea.rationale,
                    expiration=idea.expiration,
                    confidence=idea.confidence,
                    host=idea.host or idea.trader_name,
                    video_id=idea.video_id,
                    source_url=idea.source_url,
                    source_timestamp=idea.source_timestamp,
                )
            )
        push_idea_review(config, rows)
        logger.info("Sheet push: idea_review %d ideas from last %dd", len(rows), lookback_days)
        if skipped_missing_underlying:
            logger.info(
                "Sheet push: skipped %d ideas with missing underlying",
                skipped_missing_underlying,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Sheet push idea_review failed (%s: %s); continuing",
            type(exc).__name__,
            exc,
        )


def _push_plan_and_positions(config: dict, store, plan, logger: logging.Logger) -> None:
    try:
        from vol_crush.sheets.schemas import DailyPlanRow
        from vol_crush.sheets.sync import push_daily_plan

        created_at = plan.created_at if plan.created_at else datetime.now(UTC).isoformat()
        plan_rows: list[DailyPlanRow] = []
        if not plan.candidate_positions:
            plan_rows.append(
                DailyPlanRow(
                    plan_id=plan.plan_id,
                    date=date.today().isoformat(),
                    decision=plan.decision.value,
                    note=plan.reasoning or "",
                    created_at=created_at,
                )
            )
        else:
            for candidate in plan.candidate_positions:
                note = (
                    f"{plan.decision.value}; est_credit={candidate.estimated_credit}; "
                    f"est_bpr={candidate.estimated_bpr}; regime={plan.regime}"
                )
                if candidate.rationale:
                    note = f"{note}; {candidate.rationale}"
                plan_rows.append(
                    DailyPlanRow(
                        plan_id=plan.plan_id,
                        date=date.today().isoformat(),
                        decision=plan.decision.value,
                        underlying=candidate.underlying,
                        strategy=operator_strategy_label(candidate.strategy_type),
                        note=note,
                        idea_id=candidate.idea_id,
                        created_at=created_at,
                    )
                )
        push_daily_plan(config, plan_rows)
        logger.info("Sheet push: daily_plan %d rows", len(plan_rows))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Sheet push daily_plan failed (%s: %s); continuing",
            type(exc).__name__,
            exc,
        )


def _push_operator_digest(
    config: dict,
    logger: logging.Logger,
    *,
    lookback_days: int = 7,
) -> None:
    try:
        from vol_crush.idea_sources.fetcher import _resolve_archive_roots
        from vol_crush.sheets.schemas import OperatorDigestRow
        from vol_crush.sheets.sync import push_operator_digest

        _, summaries_root, _ = _resolve_archive_roots(config)
        records = read_recent_summary_records(
            summaries_root, lookback_days=lookback_days
        )
        rows = [
            OperatorDigestRow(
                digest_id=record.digest_id,
                date=record.date,
                category=record.category,
                title=record.title,
                source=record.author or record.source_name,
                summary=record.summary or record.headline,
                actionable_ideas_present=record.actionable_ideas_present,
                source_url=record.url,
            )
            for record in records
        ]
        push_operator_digest(config, rows)
        logger.info(
            "Sheet push: operator_digest %d rows from last %dd",
            len(rows),
            lookback_days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Sheet push operator_digest failed (%s: %s); continuing",
            type(exc).__name__,
            exc,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Vol Crush daily dry-run pipeline")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument(
        "--skip-backtest", action="store_true", help="Skip replay gate step"
    )
    parser.add_argument(
        "--fetch-sources",
        nargs="*",
        choices=["youtube", "rss", "web", "transcripts"],
        default=[],
        help="Optionally fetch fresh source content before optimization",
    )
    parser.add_argument(
        "--source-limit",
        type=int,
        default=None,
        help="Max items per configured source feed/channel when fetching sources",
    )
    parser.add_argument(
        "--no-sheet-sync",
        action="store_true",
        help="Skip Google Sheet pull/push even if enabled in config",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    store = build_local_store(config)

    sheets_enabled = _sheets_enabled(
        config, cli_override=False if args.no_sheet_sync else None
    )
    if not sheets_enabled:
        config.setdefault("google_sheets", {})["enabled"] = False

    for source in args.fetch_sources:
        documents, ideas, notes = run_source_fetch(
            config, source, limit=args.source_limit, extract_ideas=True
        )
        logger.info(
            "Source fetch [%s]: %d documents, %d ideas",
            source,
            len(documents),
            len(ideas),
        )
        for note in notes:
            logger.info(note)

    if sheets_enabled:
        # Pull first so the optimizer + idea-review gate see the latest operator
        # decisions; push the idea_review tab after so freshly-extracted ideas
        # appear for review (existing approvals preserved by merge).
        _try_sheet_pull(config, logger)
        _push_recent_ideas_to_sheet(config, store, logger)
        _push_operator_digest(config, logger)

    payload, replay_trades = build_fixture_payload(config)
    bundle_path, replay_path = write_fixture_artifacts(config, payload, replay_trades)
    store.save_fixture_payload(payload)
    store.save_replay_trades(replay_trades)
    provider = build_market_data_provider(config, bundle_path)
    logger.info("Fixture refresh complete: %s and %s", bundle_path, replay_path)

    if not args.skip_backtest:
        results = run_backtests(config)
        logger.info("Backtest gate refreshed for %d strategies", len(results))

    if config.get("broker", {}).get("active") == "public" and config.get(
        "broker", {}
    ).get("public", {}).get("sync_portfolio_before_optimizer", True):
        try:
            snapshot = sync_public_portfolio(config, store=store)
            logger.info(
                "Public portfolio sync complete: positions=%d nlv=%.2f",
                snapshot.position_count,
                snapshot.net_liquidation_value,
            )
        except Exception as exc:
            logger.warning(
                "Public portfolio sync failed; continuing with local state: %s", exc
            )

    plan = build_trade_plan(store, config, provider)
    store.save_trade_plan(plan)
    logger.info("Optimizer decision=%s for plan %s", plan.decision.value, plan.plan_id)

    orders = execute_latest_plan(config)
    logger.info("Pending executor emitted %d orders", len(orders))

    position_actions = evaluate_positions(config)
    logger.info("Position manager emitted %d actions", len(position_actions))

    if sheets_enabled:
        _push_plan_and_positions(config, store, plan, logger)


if __name__ == "__main__":
    main()
