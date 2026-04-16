"""Bootstrap / pull / push services for the kamandal_control Google Sheet."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from vol_crush.core.config import get_data_dir
from vol_crush.integrations.google_sheets import (
    GoogleSheetClient,
    WorksheetHandle,
)
from vol_crush.sheets.schemas import (
    AuthorizationMode,
    DailyPlanRow,
    IdeaApproval,
    IdeaReviewRow,
    PositionRow,
    StrategyApprovalRow,
)

logger = logging.getLogger("vol_crush.sheets.sync")

_DEFAULT_TABS = {
    "strategies": "strategies",
    "idea_review": "idea_review",
    "daily_plan": "daily_plan",
    "positions": "positions",
}

# Example rows used by bootstrap so a fresh sheet shows operators the shape.
_STRATEGY_EXAMPLE = StrategyApprovalRow(
    row_id="spy_put_spread::index_etf",
    enabled=False,
    template_id="short_put_spread",
    profile_id="index_etf",
    authorization_mode=AuthorizationMode.SHADOW,
    backtest_approved=False,
    dry_run_passed=False,
    approved_by="",
    approval_reason="example row — edit or delete",
    notes="flip enabled=TRUE and both approvals to let live mode place orders",
)


@dataclass
class SheetPullResult:
    tab: str
    rows_fetched: int
    cache_path: Path
    changed: bool = False
    hash: str = ""
    stamped_rows: int = 0


@dataclass
class SheetsPullReport:
    strategies: SheetPullResult | None = None
    idea_review: SheetPullResult | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategies": asdict(self.strategies) if self.strategies else None,
            "idea_review": asdict(self.idea_review) if self.idea_review else None,
            "errors": list(self.errors),
        }


def _tabs(config: Mapping[str, Any]) -> dict[str, str]:
    cfg = (config.get("google_sheets") or {}).get("tabs") or {}
    merged = dict(_DEFAULT_TABS)
    merged.update({k: str(v) for k, v in cfg.items() if v})
    return merged


def _cache_dir(config: Mapping[str, Any]) -> Path:
    raw = (config.get("google_sheets") or {}).get("cache_dir") or str(
        get_data_dir() / "sheet_cache"
    )
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _utc_now_stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _operator_identity() -> str:
    return os.environ.get("KAMANDAL_OPERATOR") or os.environ.get("USER") or "unknown"


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


# ── Bootstrap ───────────────────────────────────────────────────────


def bootstrap_sheet(config: Mapping[str, Any]) -> list[str]:
    """Create all v1 tabs with headers, dropdowns, and example rows.

    Idempotent: if a tab already exists with data, its header is ensured but
    existing rows are preserved. Returns a list of human-readable notes.
    """
    tabs = _tabs(config)
    client = GoogleSheetClient.from_config(config)
    notes: list[str] = []

    # strategies — headers + 1 example row.
    strategies = client.get_worksheet(tabs["strategies"])
    _ensure_headered_tab(
        strategies,
        header=list(StrategyApprovalRow.HEADER),
        example_rows=[_STRATEGY_EXAMPLE.to_row()],
    )
    strategies.set_enum_validation(
        "authorization_mode", [m.value for m in AuthorizationMode]
    )
    for col in ("enabled", "backtest_approved", "dry_run_passed"):
        strategies.set_enum_validation(col, ["TRUE", "FALSE"])
    notes.append("strategies: header + example row set, dropdowns applied")

    # idea_review — headers + dropdown on approval.
    idea_review = client.get_worksheet(tabs["idea_review"])
    _ensure_headered_tab(idea_review, header=list(IdeaReviewRow.HEADER))
    idea_review.set_enum_validation(
        "approval", [v.value for v in IdeaApproval if v.value]
    )
    notes.append("idea_review: header + approval dropdown set")

    # daily_plan / positions — write-only; bootstrap just lays down headers.
    daily_plan = client.get_worksheet(tabs["daily_plan"])
    _ensure_headered_tab(daily_plan, header=list(DailyPlanRow.HEADER))
    notes.append("daily_plan: header set (write target)")

    positions = client.get_worksheet(tabs["positions"])
    _ensure_headered_tab(positions, header=list(PositionRow.HEADER))
    notes.append("positions: header set (write target)")

    client.ensure_no_default_sheet1()
    return notes


def _ensure_headered_tab(
    handle: WorksheetHandle,
    *,
    header: Sequence[str],
    example_rows: Sequence[Sequence[Any]] | None = None,
) -> None:
    current = handle.header()
    if current == list(header):
        return
    if not current and example_rows:
        handle.replace_contents(header, example_rows)
        return
    handle.ensure_header(header)


# ── Pull ────────────────────────────────────────────────────────────


def pull_sheet(config: Mapping[str, Any]) -> SheetsPullReport:
    """Fetch read-side tabs, stamp approval changes, write local JSON cache."""
    tabs = _tabs(config)
    cache_dir = _cache_dir(config)
    client = GoogleSheetClient.from_config(config)
    report = SheetsPullReport()

    try:
        report.strategies = _pull_strategies(client, tabs["strategies"], cache_dir)
    except Exception as exc:  # noqa: BLE001 — report and continue
        logger.exception("strategies pull failed")
        report.errors.append(f"strategies: {type(exc).__name__}: {exc}")

    try:
        report.idea_review = _pull_idea_review(client, tabs["idea_review"], cache_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("idea_review pull failed")
        report.errors.append(f"idea_review: {type(exc).__name__}: {exc}")

    return report


def _pull_strategies(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [StrategyApprovalRow.from_row(row) for row in raw_rows]
    parsed = [row for row in parsed if row.template_id and row.profile_id]

    cache_path = cache_dir / "strategies.json"
    prior = _load_json(cache_path) or {}
    prior_by_id = {item.get("identity_key"): item for item in prior.get("rows", [])}

    # Stamp approved_at when a row is newly eligible for live and has no stamp.
    stamped = 0
    for row in parsed:
        key = row.identity_key()
        was_eligible = bool(prior_by_id.get(key, {}).get("is_live_eligible"))
        if row.is_live_eligible() and (not was_eligible or not row.approved_at):
            row.approved_at = _utc_now_stamp()
            if not row.approved_by:
                row.approved_by = _operator_identity()
            stamped += 1
    if stamped:
        # Write stamps back to the sheet so the audit trail is visible there too.
        handle.replace_contents(StrategyApprovalRow.HEADER, [r.to_row() for r in parsed])

    payload = {
        "fetched_at": _utc_now_stamp(),
        "tab": title,
        "rows": [
            {
                **asdict(row),
                "authorization_mode": row.authorization_mode.value,
                "identity_key": row.identity_key(),
                "is_live_eligible": row.is_live_eligible(),
            }
            for row in parsed
        ],
    }
    digest = _hash_payload(payload["rows"])
    payload["hash"] = digest
    changed = digest != (prior.get("hash") or "")
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    logger.info(
        "strategies pull: rows=%d live_eligible=%d changed=%s stamped=%d",
        len(parsed),
        sum(1 for r in parsed if r.is_live_eligible()),
        changed,
        stamped,
    )
    return SheetPullResult(
        tab=title,
        rows_fetched=len(parsed),
        cache_path=cache_path,
        changed=changed,
        hash=digest,
        stamped_rows=stamped,
    )


def _pull_idea_review(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [IdeaReviewRow.from_row(row) for row in raw_rows]
    parsed = [row for row in parsed if row.idea_id]

    cache_path = cache_dir / "idea_review.json"
    prior = _load_json(cache_path) or {}
    prior_by_id = {row.get("idea_id"): row for row in prior.get("rows", [])}

    stamped = 0
    for row in parsed:
        prior_row = prior_by_id.get(row.idea_id) or {}
        prior_approval = str(prior_row.get("approval") or "")
        if row.approval != IdeaApproval.PENDING and (
            prior_approval != row.approval.value or not row.reviewed_at
        ):
            row.reviewed_at = _utc_now_stamp()
            if not row.reviewed_by:
                row.reviewed_by = _operator_identity()
            stamped += 1
    if stamped:
        handle.replace_contents(IdeaReviewRow.HEADER, [r.to_row() for r in parsed])

    payload = {
        "fetched_at": _utc_now_stamp(),
        "tab": title,
        "rows": [
            {**asdict(row), "approval": row.approval.value} for row in parsed
        ],
    }
    digest = _hash_payload(payload["rows"])
    payload["hash"] = digest
    changed = digest != (prior.get("hash") or "")
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    logger.info(
        "idea_review pull: rows=%d approved=%d rejected=%d changed=%s stamped=%d",
        len(parsed),
        sum(1 for r in parsed if r.approval == IdeaApproval.APPROVED),
        sum(1 for r in parsed if r.approval == IdeaApproval.REJECTED),
        changed,
        stamped,
    )
    return SheetPullResult(
        tab=title,
        rows_fetched=len(parsed),
        cache_path=cache_path,
        changed=changed,
        hash=digest,
        stamped_rows=stamped,
    )


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ── Push ────────────────────────────────────────────────────────────


def push_daily_plan(
    config: Mapping[str, Any], rows: Sequence[DailyPlanRow]
) -> None:
    tabs = _tabs(config)
    client = GoogleSheetClient.from_config(config)
    handle = client.get_worksheet(tabs["daily_plan"])
    handle.replace_contents(
        DailyPlanRow.HEADER, [row.to_row() for row in rows]
    )
    logger.info("pushed %d daily_plan rows", len(rows))


def push_positions(
    config: Mapping[str, Any], rows: Sequence[PositionRow]
) -> None:
    tabs = _tabs(config)
    client = GoogleSheetClient.from_config(config)
    handle = client.get_worksheet(tabs["positions"])
    handle.replace_contents(PositionRow.HEADER, [row.to_row() for row in rows])
    logger.info("pushed %d positions rows", len(rows))


def push_idea_review(
    config: Mapping[str, Any], rows: Sequence[IdeaReviewRow]
) -> None:
    """Publish newly-extracted ideas into the bidirectional review tab.

    Preserves existing `approval`, `operator_notes`, `reviewed_by`, `reviewed_at`
    on any row with a matching ``idea_id``. New rows appended at the bottom.
    """
    tabs = _tabs(config)
    client = GoogleSheetClient.from_config(config)
    handle = client.get_worksheet(tabs["idea_review"])
    existing_raw = handle.data_rows()
    existing = {
        row.get("idea_id"): IdeaReviewRow.from_row(row)
        for row in existing_raw
        if row.get("idea_id")
    }
    merged: dict[str, IdeaReviewRow] = {}
    for row in rows:
        prior = existing.pop(row.idea_id, None)
        if prior is not None:
            # App-controlled fields refresh; operator-controlled fields stay.
            row.approval = prior.approval
            row.operator_notes = prior.operator_notes or row.operator_notes
            row.reviewed_by = prior.reviewed_by or row.reviewed_by
            row.reviewed_at = prior.reviewed_at or row.reviewed_at
        merged[row.idea_id] = row
    # Preserve operator-only rows that kamandal hasn't seen (edge case: hand-
    # entered by operator). We keep them at the bottom.
    trailing = list(existing.values())
    final = list(merged.values()) + trailing
    handle.replace_contents(IdeaReviewRow.HEADER, [row.to_row() for row in final])
    logger.info(
        "pushed idea_review: %d new/updated, %d untouched operator rows",
        len(merged),
        len(trailing),
    )


# ── Cache readers (optimizer consumes these) ─────────────────────────


def read_approvals_cache(
    config: Mapping[str, Any],
) -> list[StrategyApprovalRow]:
    cache_path = _cache_dir(config) / "strategies.json"
    data = _load_json(cache_path) or {}
    rows: list[StrategyApprovalRow] = []
    for item in data.get("rows", []):
        try:
            rows.append(StrategyApprovalRow.from_row(item))
        except Exception:  # noqa: BLE001
            logger.warning("failed to parse cached strategy row: %r", item)
    return rows


def read_idea_approvals_cache(
    config: Mapping[str, Any],
) -> list[IdeaReviewRow]:
    cache_path = _cache_dir(config) / "idea_review.json"
    data = _load_json(cache_path) or {}
    rows: list[IdeaReviewRow] = []
    for item in data.get("rows", []):
        try:
            rows.append(IdeaReviewRow.from_row(item))
        except Exception:  # noqa: BLE001
            logger.warning("failed to parse cached idea_review row: %r", item)
    return rows
