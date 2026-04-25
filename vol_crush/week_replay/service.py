"""Controlled Monday-Friday replay for source intake, intelligence, and planning."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from vol_crush.core.config import get_data_dir, load_config
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import OrderStatus
from vol_crush.executor.service import create_pending_orders
from vol_crush.idea_sources.fetcher import run_source_fetch
from vol_crush.integrations.fixtures import (
    build_fixture_payload,
    write_fixture_artifacts,
)
from vol_crush.integrations.market_data import build_market_data_provider
from vol_crush.integrations.storage import build_local_store
from vol_crush.optimizer.service import _apply_shadow_nlv_override, build_trade_plan
from vol_crush.position_manager.service import evaluate_positions
from vol_crush.reflection.service import run_reflection
from vol_crush.shadow.service import (
    build_shadow_portfolio_snapshot,
    shadow_fill_from_order,
)

logger = logging.getLogger("vol_crush.week_replay")


@dataclass
class ReplayDayReport:
    replay_date: str
    documents: int
    ideas: int
    observations: int
    candidates: int
    playbook_insights: int
    decision: str
    plan_id: str
    selected_ideas: list[str]
    orders: int
    shadow_fills: int
    position_actions: int
    notes: list[str]


@dataclass
class WeekReplayReport:
    run_id: str
    start_date: str
    end_date: str
    sqlite_path: str
    audit_dir: str
    source: str
    limit: int
    simulate_preflight: bool
    days: list[ReplayDayReport]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["days"] = [asdict(day) for day in self.days]
        return data


def _default_replay_window(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    days_since_friday = (today.weekday() - 4) % 7
    if days_since_friday == 0 and today.weekday() < 5:
        days_since_friday = 7
    friday = today - timedelta(days=days_since_friday)
    monday = friday - timedelta(days=4)
    return monday, friday


def _day_start(day: date) -> str:
    return datetime.combine(day, time.min, tzinfo=UTC).isoformat()


def _day_end(day: date) -> str:
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC).isoformat()


def _day_decision_time(day: date) -> str:
    return datetime.combine(day, time(21, 0), tzinfo=UTC).isoformat()


def _replay_config(
    base_config: dict[str, Any],
    *,
    run_id: str,
    replay_root: Path | None,
) -> dict[str, Any]:
    config = copy.deepcopy(base_config)
    root = replay_root or (get_data_dir() / "replays" / run_id)
    root.mkdir(parents=True, exist_ok=True)
    config.setdefault("storage", {}).setdefault("local", {})
    config["storage"]["local"]["sqlite_path"] = str(root / "kamandal.db")
    config["storage"]["local"]["audit_dir"] = str(root / "audit")
    config.setdefault("data_sources", {}).setdefault("fixtures", {})
    config["data_sources"]["fixtures"]["bundle_path"] = str(
        root / "fixtures" / "fixture_bundle.json"
    )
    config["data_sources"]["fixtures"]["replay_path"] = str(
        root / "fixtures" / "replay_trades.json"
    )
    config.setdefault("google_sheets", {})["enabled"] = False
    config.setdefault("execution", {})["mode"] = "shadow"
    config["execution"]["bypass_daily_plan_approval"] = True
    config["execution"]["auto_approve_ideas"] = True
    config.setdefault("broker", {})["active"] = "replay"
    return config


def _simulate_replay_orders(
    config: dict[str, Any],
    *,
    day: date,
    plan,
) -> tuple[int, int]:
    if plan.decision.value != "execute":
        return 0, 0
    store = build_local_store(config)
    snapshot = build_shadow_portfolio_snapshot(store, config)
    snapshot = _apply_shadow_nlv_override(snapshot, config)
    orders = create_pending_orders(plan, snapshot, config)
    if not orders:
        return 0, 0

    timestamp = _day_decision_time(day)
    for order in orders:
        order.created_at = timestamp
        order.submitted_at = timestamp
        order.broker = "replay"
        order.execution_mode = "shadow"
        order.broker_order_id = f"replay-{day.isoformat()}-{uuid.uuid4().hex[:8]}"
        order.broker_status = "PREFLIGHT_OK"
        order.broker_response = {
            "status": "PREFLIGHT_OK",
            "replay": True,
            "as_of_date": day.isoformat(),
            "buyingPowerRequirement": order.estimated_bpr,
        }
    store.save_pending_orders(orders)

    fills = [shadow_fill_from_order(order, filled_at=timestamp) for order in orders]
    for order in orders:
        order.status = OrderStatus.FILLED.value
        order.notes = (
            f"{order.notes} Replay shadow fill assumed after simulated preflight."
        ).strip()
    store.save_shadow_fills(fills)
    store.save_pending_orders(orders)
    build_shadow_portfolio_snapshot(store, config)
    return len(orders), len(fills)


def _record_would_preflight_orders(
    config: dict[str, Any],
    *,
    day: date,
    plan,
) -> tuple[int, int]:
    if plan.decision.value != "execute":
        return 0, 0
    store = build_local_store(config)
    snapshot = build_shadow_portfolio_snapshot(store, config)
    snapshot = _apply_shadow_nlv_override(snapshot, config)
    orders = create_pending_orders(plan, snapshot, config)
    timestamp = _day_decision_time(day)
    for order in orders:
        order.created_at = timestamp
        order.submitted_at = timestamp
        order.broker = "replay"
        order.execution_mode = "shadow"
        order.broker_status = "WOULD_PREFLIGHT"
        order.notes = (
            f"{order.notes} Replay recorded without broker preflight simulation."
        ).strip()
    if orders:
        store.save_pending_orders(orders)
    return len(orders), 0


def _run_replay_day(
    config: dict[str, Any],
    *,
    day: date,
    source: str,
    limit: int,
    simulate_preflight: bool,
    generate_summaries: bool,
) -> ReplayDayReport:
    store = build_local_store(config)
    window_start = _day_start(day)
    window_end = _day_end(day)
    documents, ideas, notes = run_source_fetch(
        config,
        source,
        limit=limit,
        extract_ideas=True,
        generate_summaries=generate_summaries,
        published_start=window_start,
        published_end=window_end,
        observed_at=window_start,
    )
    payload, replay_trades = build_fixture_payload(config)
    bundle_path, _ = write_fixture_artifacts(config, payload, replay_trades)
    store.save_fixture_payload(payload)
    store.save_replay_trades(replay_trades)
    provider = build_market_data_provider(config, bundle_path)

    plan = build_trade_plan(store, config, provider)
    plan.created_at = _day_decision_time(day)
    plan.plan_id = f"replay_{day.isoformat()}_{uuid.uuid4().hex[:8]}"
    store.save_trade_plan(plan)

    if simulate_preflight:
        orders, fills = _simulate_replay_orders(config, day=day, plan=plan)
    else:
        orders, fills = _record_would_preflight_orders(config, day=day, plan=plan)
    position_actions = evaluate_positions(config)
    reflection = run_reflection(
        config,
        store=store,
        window_start=window_start,
        window_end=window_end,
    )
    logger.info(
        "Replay %s: docs=%d ideas=%d decision=%s orders=%d fills=%d",
        day.isoformat(),
        len(documents),
        len(ideas),
        plan.decision.value,
        orders,
        fills,
    )
    return ReplayDayReport(
        replay_date=day.isoformat(),
        documents=len(documents),
        ideas=len(ideas),
        observations=reflection.source_observation_count,
        candidates=reflection.idea_candidate_count,
        playbook_insights=reflection.playbook_insight_count,
        decision=plan.decision.value,
        plan_id=plan.plan_id,
        selected_ideas=list(plan.selected_combo_ids),
        orders=orders,
        shadow_fills=fills,
        position_actions=len(position_actions),
        notes=list(notes) + list(reflection.notes),
    )


def run_week_replay(
    config: dict[str, Any],
    *,
    start_date: date,
    end_date: date,
    source: str = "youtube",
    limit: int = 12,
    run_id: str | None = None,
    replay_root: Path | None = None,
    simulate_preflight: bool = True,
    generate_summaries: bool = True,
) -> WeekReplayReport:
    """Run source/intelligence/optimizer/shadow replay one date at a time."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    run_id = run_id or f"week_replay_{start_date}_{end_date}_{uuid.uuid4().hex[:6]}"
    replay_config = _replay_config(config, run_id=run_id, replay_root=replay_root)
    store = build_local_store(replay_config)
    days: list[ReplayDayReport] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            days.append(
                _run_replay_day(
                    replay_config,
                    day=current,
                    source=source,
                    limit=limit,
                    simulate_preflight=simulate_preflight,
                    generate_summaries=generate_summaries,
                )
            )
        current += timedelta(days=1)

    local = replay_config["storage"]["local"]
    report = WeekReplayReport(
        run_id=run_id,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        sqlite_path=local["sqlite_path"],
        audit_dir=local["audit_dir"],
        source=source,
        limit=limit,
        simulate_preflight=simulate_preflight,
        days=days,
    )
    report_path = Path(local["audit_dir"]) / "week_replay_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    # Touch the store so empty replays still have a initialized DB for inspection.
    store.list_trade_plans()
    return report


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_args() -> argparse.Namespace:
    default_start, default_end = _default_replay_window()
    parser = argparse.ArgumentParser(
        description="Replay a Monday-Friday source/intelligence/planning week."
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--start-date", type=_parse_date, default=default_start)
    parser.add_argument("--end-date", type=_parse_date, default=default_end)
    parser.add_argument("--source", choices=["youtube"], default="youtube")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--replay-root", type=Path, default=None)
    parser.add_argument(
        "--no-simulated-preflight",
        action="store_true",
        help="Record would-preflight orders but do not create simulated shadow fills.",
    )
    parser.add_argument(
        "--no-summaries",
        action="store_true",
        help="Skip LLM summary pass; idea extraction still runs.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    setup_logging(config.get("app", {}).get("log_level", "INFO"))
    report = run_week_replay(
        config,
        start_date=args.start_date,
        end_date=args.end_date,
        source=args.source,
        limit=args.limit,
        run_id=args.run_id,
        replay_root=args.replay_root,
        simulate_preflight=not args.no_simulated_preflight,
        generate_summaries=not args.no_summaries,
    )
    logger.info(
        "Week replay %s complete: %s → %s, db=%s",
        report.run_id,
        report.start_date,
        report.end_date,
        report.sqlite_path,
    )


if __name__ == "__main__":
    main()
