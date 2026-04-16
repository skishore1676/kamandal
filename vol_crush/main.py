"""Daily dry-run orchestration entrypoint for Vol Crush."""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, date, datetime, timedelta
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
        for idea in store.list_trade_ideas():
            if idea.date and idea.date < cutoff:
                continue
            rows.append(
                IdeaReviewRow(
                    idea_id=idea.id,
                    date=idea.date,
                    underlying=idea.underlying,
                    strategy_type=idea.strategy_type,
                    description=idea.description,
                    strikes=list(idea.strikes or []),
                    expiration=idea.expiration,
                    confidence=idea.confidence,
                    host=idea.host or idea.trader_name,
                    video_id=idea.video_id,
                    source_url=idea.source_url,
                )
            )
        push_idea_review(config, rows)
        logger.info("Sheet push: idea_review %d ideas from last %dd", len(rows), lookback_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Sheet push idea_review failed (%s: %s); continuing",
            type(exc).__name__,
            exc,
        )


def _push_plan_and_positions(config: dict, store, plan, logger: logging.Logger) -> None:
    try:
        from vol_crush.sheets.schemas import DailyPlanRow, PositionRow
        from vol_crush.sheets.sync import push_daily_plan, push_positions

        created_at = plan.created_at if plan.created_at else datetime.now(UTC).isoformat()
        plan_rows: list[DailyPlanRow] = []
        if not plan.combos:
            plan_rows.append(
                DailyPlanRow(
                    plan_id=plan.plan_id,
                    date=date.today().isoformat(),
                    decision=plan.decision.value,
                    combo_description=plan.reasoning or "",
                    regime=plan.regime,
                    risk_flags=list(plan.risk_flags or []),
                    execution_mode=str((config.get("execution") or {}).get("mode", "")),
                    created_at=created_at,
                )
            )
        else:
            for combo in plan.combos:
                descr = getattr(combo, "description", "") or getattr(
                    combo, "reasoning", ""
                )
                plan_rows.append(
                    DailyPlanRow(
                        plan_id=plan.plan_id,
                        date=date.today().isoformat(),
                        decision=plan.decision.value,
                        combo_description=descr,
                        underlying=getattr(combo, "underlying", ""),
                        strategy=getattr(combo, "strategy_id", ""),
                        strikes=list(getattr(combo, "strikes", []) or []),
                        expiration=getattr(combo, "expiration", ""),
                        quantity=int(getattr(combo, "quantity", 0) or 0),
                        est_credit=float(getattr(combo, "estimated_credit", 0.0) or 0.0),
                        est_bpr=float(getattr(combo, "estimated_bpr", 0.0) or 0.0),
                        est_max_loss=float(
                            getattr(combo, "estimated_max_loss", 0.0) or 0.0
                        ),
                        regime=plan.regime,
                        risk_flags=list(plan.risk_flags or []),
                        execution_mode=str(
                            (config.get("execution") or {}).get("mode", "")
                        ),
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

    try:
        from vol_crush.sheets.schemas import PositionRow
        from vol_crush.sheets.sync import push_positions

        position_rows: list[PositionRow] = []
        for position in store.list_positions():
            legs_summary = ", ".join(
                f"{leg.action} {leg.quantity} {leg.strike}{leg.option_type[0].upper()}"
                for leg in (getattr(position, "legs", None) or [])
            )
            position_rows.append(
                PositionRow(
                    group_id=getattr(position, "position_id", ""),
                    strategy_type=getattr(position, "strategy_type", ""),
                    underlying=getattr(position, "underlying", ""),
                    legs_summary=legs_summary,
                    expiration=getattr(position, "expiration", ""),
                    quantity=int(getattr(position, "quantity", 0) or 0),
                    net_delta=float(getattr(position, "net_delta", 0.0) or 0.0),
                    net_theta=float(getattr(position, "net_theta", 0.0) or 0.0),
                    bpr_used=float(getattr(position, "bpr_used", 0.0) or 0.0),
                    pnl_unrealized=float(
                        getattr(position, "pnl_unrealized", 0.0) or 0.0
                    ),
                    management_status=str(
                        getattr(position, "management_status", "")
                    ),
                    source=str(getattr(position, "source", "")),
                    opened_at=str(getattr(position, "opened_at", "") or ""),
                    days_open=int(getattr(position, "days_open", 0) or 0),
                )
            )
        push_positions(config, position_rows)
        logger.info("Sheet push: positions %d rows", len(position_rows))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Sheet push positions failed (%s: %s); continuing",
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

    if sheets_enabled:
        # Pull first so the optimizer + idea-review gate see the latest operator
        # decisions; push the idea_review tab after so freshly-extracted ideas
        # appear for review (existing approvals preserved by merge).
        _try_sheet_pull(config, logger)
        _push_recent_ideas_to_sheet(config, store, logger)

    payload, replay_trades = build_fixture_payload(config)
    bundle_path, replay_path = write_fixture_artifacts(config, payload, replay_trades)
    store.save_fixture_payload(payload)
    store.save_replay_trades(replay_trades)
    provider = FixtureMarketDataProvider(bundle_path)
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
