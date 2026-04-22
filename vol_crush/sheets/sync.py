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
from vol_crush.core.config import load_strategy_templates
from vol_crush.core.config import load_underlying_profiles
from vol_crush.core.models import MarketRegime
from vol_crush.core.strategy_aliases import operator_strategy_label
from vol_crush.integrations.google_sheets import (
    GoogleSheetClient,
    WorksheetHandle,
)
from vol_crush.sheets.schemas import (
    AuthorizationMode,
    DailyPlanRow,
    IdeaApproval,
    IdeaReviewRow,
    OperatorDigestRow,
    PositionRow,
    ProfileConfigRow,
    RegimeControlRow,
    StrategyApprovalRow,
    TemplateLibraryRow,
    UniverseMemberRow,
)

logger = logging.getLogger("vol_crush.sheets.sync")

_DEFAULT_TABS = {
    "strategies": "strategies",
    "template_library": "template_library",
    "regime_control": "regime_control",
    "profiles": "profiles",
    "universe": "universe",
    "operator_digest": "operator_digest",
    "idea_review": "idea_review",
    "daily_plan": "daily_plan",
    "positions": "positions",
}

# Example rows used by bootstrap so a fresh sheet shows operators the shape.
_STRATEGY_EXAMPLE = StrategyApprovalRow(
    row_id="put_vertical::index_etf",
    enabled=False,
    strategy_id="put_vertical",
    stock_profile="index_etf",
    authorization_mode=AuthorizationMode.SHADOW,
)


def _profile_seed_rows() -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in load_underlying_profiles():
        if not item:
            continue
        row = ProfileConfigRow.from_row(item)
        if row.stock_profile:
            rows.append(row.to_row())
    return rows


def _template_seed_rows() -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in load_strategy_templates():
        if not item:
            continue
        filters = item.get("filters") or {}
        management = item.get("management") or {}
        structure = str(item.get("structure") or "").strip()
        rows.append(
            TemplateLibraryRow(
                template_id=str(item.get("id") or "").strip(),
                strategy_id=operator_strategy_label(structure),
                structure=structure,
                name=str(item.get("name") or "").strip(),
                allowed_regimes=[
                    str(value).strip() for value in item.get("allowed_regimes", []) or []
                ],
                iv_rank_min=filters.get("iv_rank_min"),
                iv_rank_max=filters.get("iv_rank_max"),
                dte_min=_range_min(filters.get("dte_range")),
                dte_max=_range_max(filters.get("dte_range")),
                delta_min=_range_min(filters.get("delta_range")),
                delta_max=_range_max(filters.get("delta_range")),
                spread_width=filters.get("spread_width"),
                min_credit_to_width_ratio=filters.get("min_credit_to_width_ratio"),
                profit_target_pct=management.get("profit_target_pct"),
                max_loss_multiple=management.get("max_loss_multiple"),
                roll_dte_trigger=management.get("roll_dte_trigger"),
                roll_for_credit=management.get("roll_for_credit"),
                close_before_expiration=management.get("close_before_expiration"),
                avoid_earnings=item.get("avoid_earnings"),
            ).to_row()
        )
    return rows


def _universe_seed_rows() -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in load_underlying_profiles():
        profile = ProfileConfigRow.from_row(item)
        stock_profile = profile.stock_profile
        for symbol in item.get("symbols", []) or []:
            rows.append(
                UniverseMemberRow(
                    symbol=str(symbol).upper(),
                    stock_profile=stock_profile,
                    enabled=True,
                ).to_row()
            )
    return rows


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
    template_library: SheetPullResult | None = None
    regime_control: SheetPullResult | None = None
    profiles: SheetPullResult | None = None
    universe: SheetPullResult | None = None
    idea_review: SheetPullResult | None = None
    daily_plan: SheetPullResult | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategies": asdict(self.strategies) if self.strategies else None,
            "template_library": (
                asdict(self.template_library) if self.template_library else None
            ),
            "regime_control": (
                asdict(self.regime_control) if self.regime_control else None
            ),
            "profiles": asdict(self.profiles) if self.profiles else None,
            "universe": asdict(self.universe) if self.universe else None,
            "idea_review": asdict(self.idea_review) if self.idea_review else None,
            "daily_plan": asdict(self.daily_plan) if self.daily_plan else None,
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
    _ensure_model_tab(
        strategies,
        row_cls=StrategyApprovalRow,
        example_rows=[_STRATEGY_EXAMPLE.to_row()],
    )
    strategies.set_enum_validation(
        "authorization_mode", [m.value for m in AuthorizationMode]
    )
    strategies.set_enum_validation("enabled", ["TRUE", "FALSE"])
    strategies.set_enum_validation("avoid_earnings", ["TRUE", "FALSE"])
    notes.append("strategies: header + example row set, dropdowns applied")

    template_library = client.get_worksheet(tabs["template_library"])
    _ensure_model_tab(
        template_library,
        row_cls=TemplateLibraryRow,
        example_rows=_template_seed_rows(),
    )
    template_library.set_enum_validation("roll_for_credit", ["TRUE", "FALSE"])
    template_library.set_enum_validation(
        "close_before_expiration", ["TRUE", "FALSE"]
    )
    template_library.set_enum_validation("avoid_earnings", ["TRUE", "FALSE"])
    notes.append("template_library: header + seeded rows set")

    regime_control = client.get_worksheet(tabs["regime_control"])
    _ensure_model_tab(regime_control, row_cls=RegimeControlRow)
    regime_control.set_enum_validation(
        "regime", [regime.value for regime in MarketRegime]
    )
    regime_control.set_enum_validation("override_enabled", ["TRUE", "FALSE"])
    notes.append("regime_control: header set")

    profiles = client.get_worksheet(tabs["profiles"])
    _ensure_model_tab(
        profiles,
        row_cls=ProfileConfigRow,
        example_rows=_profile_seed_rows(),
    )
    profiles.set_enum_validation("earnings_sensitive", ["TRUE", "FALSE"])
    notes.append("profiles: header + seeded rows set")

    universe = client.get_worksheet(tabs["universe"])
    _ensure_model_tab(
        universe,
        row_cls=UniverseMemberRow,
        example_rows=_universe_seed_rows(),
    )
    universe.set_enum_validation("enabled", ["TRUE", "FALSE"])
    notes.append("universe: header + seeded rows set")

    operator_digest = client.get_worksheet(tabs["operator_digest"])
    _ensure_model_tab(operator_digest, row_cls=OperatorDigestRow)
    operator_digest.set_enum_validation("actionable_ideas_present", ["TRUE", "FALSE"])
    notes.append("operator_digest: header set")

    # idea_review — headers + dropdown on approval.
    idea_review = client.get_worksheet(tabs["idea_review"])
    _ensure_model_tab(idea_review, row_cls=IdeaReviewRow)
    idea_review.set_enum_validation(
        "approval", [v.value for v in IdeaApproval if v.value]
    )
    notes.append("idea_review: header + approval dropdown set")

    # daily_plan — proposed orders. Operator may approve rows in a later run.
    daily_plan = client.get_worksheet(tabs["daily_plan"])
    _ensure_headered_tab(daily_plan, header=list(DailyPlanRow.HEADER))
    notes.append("daily_plan: header set (write target)")

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


def _ensure_model_tab(
    handle: WorksheetHandle,
    *,
    row_cls,
    example_rows: Sequence[Sequence[Any]] | None = None,
) -> None:
    header = list(row_cls.HEADER)
    if handle.header() == header:
        return
    existing = []
    if handle.header():
        for raw in handle.data_rows():
            try:
                row = row_cls.from_row(raw)
            except Exception:  # noqa: BLE001
                continue
            existing.append(row.to_row())
    if existing:
        handle.replace_contents(header, existing)
        return
    _ensure_headered_tab(handle, header=header, example_rows=example_rows)


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
        report.template_library = _pull_template_library(
            client, tabs["template_library"], cache_dir
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("template_library pull failed")
        report.errors.append(f"template_library: {type(exc).__name__}: {exc}")

    try:
        report.regime_control = _pull_regime_control(
            client, tabs["regime_control"], cache_dir
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("regime_control pull failed")
        report.errors.append(f"regime_control: {type(exc).__name__}: {exc}")

    try:
        report.profiles = _pull_profiles(client, tabs["profiles"], cache_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("profiles pull failed")
        report.errors.append(f"profiles: {type(exc).__name__}: {exc}")

    try:
        report.universe = _pull_universe(client, tabs["universe"], cache_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("universe pull failed")
        report.errors.append(f"universe: {type(exc).__name__}: {exc}")

    try:
        report.idea_review = _pull_idea_review(client, tabs["idea_review"], cache_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("idea_review pull failed")
        report.errors.append(f"idea_review: {type(exc).__name__}: {exc}")

    try:
        report.daily_plan = _pull_daily_plan(client, tabs["daily_plan"], cache_dir)
    except Exception as exc:  # noqa: BLE001
        logger.exception("daily_plan pull failed")
        report.errors.append(f"daily_plan: {type(exc).__name__}: {exc}")

    return report


def _pull_strategies(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [StrategyApprovalRow.from_row(row) for row in raw_rows]
    parsed = [row for row in parsed if row.strategy_id and row.stock_profile]

    cache_path = cache_dir / "strategies.json"
    prior = _load_json(cache_path) or {}

    stamped = 0

    payload = {
        "fetched_at": _utc_now_stamp(),
        "tab": title,
        "rows": [
            {
                **asdict(row),
                "template_id": row.template_id,
                "profile_id": row.profile_id,
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


def _pull_profiles(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [ProfileConfigRow.from_row(row) for row in raw_rows]
    parsed = [row for row in parsed if row.stock_profile]

    cache_path = cache_dir / "profiles.json"
    prior = _load_json(cache_path) or {}
    payload = {
        "fetched_at": _utc_now_stamp(),
        "tab": title,
        "rows": [{**asdict(row), "identity_key": row.identity_key()} for row in parsed],
    }
    digest = _hash_payload(payload["rows"])
    payload["hash"] = digest
    changed = digest != (prior.get("hash") or "")
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("profiles pull: rows=%d changed=%s", len(parsed), changed)
    return SheetPullResult(
        tab=title,
        rows_fetched=len(parsed),
        cache_path=cache_path,
        changed=changed,
        hash=digest,
        stamped_rows=0,
    )


def _pull_template_library(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [TemplateLibraryRow.from_row(row) for row in raw_rows]
    parsed = [row for row in parsed if row.template_id]

    cache_path = cache_dir / "template_library.json"
    prior = _load_json(cache_path) or {}
    payload = {
        "fetched_at": _utc_now_stamp(),
        "tab": title,
        "rows": [{**asdict(row), "identity_key": row.identity_key()} for row in parsed],
    }
    digest = _hash_payload(payload["rows"])
    payload["hash"] = digest
    changed = digest != (prior.get("hash") or "")
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("template_library pull: rows=%d changed=%s", len(parsed), changed)
    return SheetPullResult(
        tab=title,
        rows_fetched=len(parsed),
        cache_path=cache_path,
        changed=changed,
        hash=digest,
        stamped_rows=0,
    )


def _pull_regime_control(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [RegimeControlRow.from_row(row) for row in raw_rows]
    parsed = [row for row in parsed if row.date]

    cache_path = cache_dir / "regime_control.json"
    prior = _load_json(cache_path) or {}
    payload = {
        "fetched_at": _utc_now_stamp(),
        "tab": title,
        "rows": [{**asdict(row), "identity_key": row.identity_key()} for row in parsed],
    }
    digest = _hash_payload(payload["rows"])
    payload["hash"] = digest
    changed = digest != (prior.get("hash") or "")
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        "regime_control pull: rows=%d enabled=%d changed=%s",
        len(parsed),
        sum(1 for row in parsed if row.override_enabled),
        changed,
    )
    return SheetPullResult(
        tab=title,
        rows_fetched=len(parsed),
        cache_path=cache_path,
        changed=changed,
        hash=digest,
        stamped_rows=0,
    )


def _pull_universe(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [UniverseMemberRow.from_row(row) for row in raw_rows]
    parsed = [row for row in parsed if row.symbol and row.stock_profile]

    cache_path = cache_dir / "universe.json"
    prior = _load_json(cache_path) or {}
    payload = {
        "fetched_at": _utc_now_stamp(),
        "tab": title,
        "rows": [asdict(row) for row in parsed],
    }
    digest = _hash_payload(payload["rows"])
    payload["hash"] = digest
    changed = digest != (prior.get("hash") or "")
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        "universe pull: rows=%d enabled=%d changed=%s",
        len(parsed),
        sum(1 for row in parsed if row.enabled),
        changed,
    )
    return SheetPullResult(
        tab=title,
        rows_fetched=len(parsed),
        cache_path=cache_path,
        changed=changed,
        hash=digest,
        stamped_rows=0,
    )


def _pull_idea_review(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [IdeaReviewRow.from_row(row) for row in raw_rows]
    metadata = _load_idea_review_metadata(cache_dir)
    parsed = [
        _enrich_idea_review_row(row, _idea_review_metadata_for_row(metadata, row))
        for row in parsed
    ]
    parsed = [row for row in parsed if row.idea_id]

    cache_path = cache_dir / "idea_review.json"
    prior = _load_json(cache_path) or {}
    stamped = 0

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


def _pull_daily_plan(
    client: GoogleSheetClient, title: str, cache_dir: Path
) -> SheetPullResult:
    handle = client.get_worksheet(title)
    raw_rows = handle.data_rows()
    parsed = [DailyPlanRow.from_row(row) for row in raw_rows]
    parsed = [row for row in parsed if row.plan_id]

    cache_path = cache_dir / "daily_plan.json"
    prior = _load_json(cache_path) or {}
    payload = {
        "fetched_at": _utc_now_stamp(),
        "tab": title,
        "rows": [asdict(row) for row in parsed],
    }
    digest = _hash_payload(payload["rows"])
    payload["hash"] = digest
    changed = digest != (prior.get("hash") or "")
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    logger.info(
        "daily_plan pull: rows=%d approved=%d changed=%s",
        len(parsed),
        sum(1 for r in parsed if r.approval == "approve"),
        changed,
    )
    return SheetPullResult(
        tab=title,
        rows_fetched=len(parsed),
        cache_path=cache_path,
        changed=changed,
        hash=digest,
        stamped_rows=0,
    )


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _idea_review_metadata_path(cache_dir: Path) -> Path:
    return cache_dir / "idea_review_metadata.json"


def _load_idea_review_metadata(cache_dir: Path) -> dict[str, dict[str, Any]]:
    data = _load_json(_idea_review_metadata_path(cache_dir)) or {}
    rows = data.get("rows") or {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): dict(value or {}) for key, value in rows.items()}


def _write_idea_review_metadata(
    cache_dir: Path, rows: Mapping[str, Mapping[str, Any]]
) -> None:
    payload = {
        "updated_at": _utc_now_stamp(),
        "rows": {str(key): dict(value or {}) for key, value in rows.items()},
    }
    _idea_review_metadata_path(cache_dir).write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _enrich_idea_review_row(
    row: IdeaReviewRow, metadata: Mapping[str, Any] | None
) -> IdeaReviewRow:
    if not metadata:
        return row
    if metadata.get("idea_id"):
        row.idea_id = str(metadata["idea_id"]).strip()
    if metadata.get("description") and not row.description:
        row.description = str(metadata["description"]).strip()
    if metadata.get("rationale") and not row.rationale:
        row.rationale = str(metadata["rationale"]).strip()
    if metadata.get("confidence") and not row.confidence:
        row.confidence = str(metadata["confidence"]).strip()
    if metadata.get("host") and not row.host:
        row.host = str(metadata["host"]).strip()
    if metadata.get("video_id") and not row.video_id:
        row.video_id = str(metadata["video_id"]).strip()
    if metadata.get("source_url") and not row.source_url:
        row.source_url = str(metadata["source_url"]).strip()
    if metadata.get("source_timestamp") and not row.source_timestamp:
        row.source_timestamp = str(metadata["source_timestamp"]).strip()
    return row


def _idea_review_metadata_for_row(
    metadata_rows: Mapping[str, Mapping[str, Any]], row: IdeaReviewRow
) -> Mapping[str, Any] | None:
    return metadata_rows.get(row.identity_key()) or metadata_rows.get(
        row.legacy_identity_key()
    )


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
    existing_rows = [IdeaReviewRow.from_row(row) for row in existing_raw]
    existing: dict[str, IdeaReviewRow] = {}
    for row in existing_rows:
        existing[row.identity_key()] = row
        existing[row.legacy_identity_key()] = row
    cache_dir = _cache_dir(config)
    existing_metadata = _load_idea_review_metadata(cache_dir)
    merged: dict[str, IdeaReviewRow] = {}
    for row in rows:
        prior = existing.pop(row.identity_key(), None)
        if prior is None:
            prior = existing.pop(row.legacy_identity_key(), None)
        if prior is not None:
            # App-controlled fields refresh; operator-controlled fields stay.
            row.approval = prior.approval
            row.operator_notes = prior.operator_notes or row.operator_notes
            row.reviewed_by = prior.reviewed_by or row.reviewed_by
            row.reviewed_at = prior.reviewed_at or row.reviewed_at
            existing.pop(prior.identity_key(), None)
            existing.pop(prior.legacy_identity_key(), None)
        merged[row.identity_key()] = row
    # Preserve operator-only rows that kamandal hasn't seen (edge case: hand-
    # entered by operator). We keep them at the bottom.
    trailing_map: dict[str, IdeaReviewRow] = {}
    for candidate in existing.values():
        trailing_map[candidate.identity_key()] = candidate
    trailing = list(trailing_map.values())
    final = list(merged.values()) + trailing
    handle.replace_contents(IdeaReviewRow.HEADER, [row.to_row() for row in final])
    metadata_rows: dict[str, dict[str, Any]] = {}
    for row in final:
        key = row.identity_key()
        prior_meta = _idea_review_metadata_for_row(existing_metadata, row) or {}
        payload = {
            "idea_id": row.idea_id or prior_meta.get("idea_id", ""),
            "description": row.description or prior_meta.get("description", ""),
            "rationale": row.rationale or prior_meta.get("rationale", ""),
            "confidence": row.confidence or prior_meta.get("confidence", ""),
            "host": row.host or prior_meta.get("host", ""),
            "video_id": row.video_id or prior_meta.get("video_id", ""),
            "source_url": row.source_url or prior_meta.get("source_url", ""),
            "source_timestamp": row.source_timestamp
            or prior_meta.get("source_timestamp", ""),
        }
        if any(payload.values()):
            metadata_rows[key] = payload
    _write_idea_review_metadata(cache_dir, metadata_rows)
    logger.info(
        "pushed idea_review: %d new/updated, %d untouched operator rows",
        len(merged),
        len(trailing),
    )


def push_operator_digest(
    config: Mapping[str, Any], rows: Sequence[OperatorDigestRow]
) -> None:
    """Publish the operator digest while preserving operator-authored notes."""
    tabs = _tabs(config)
    client = GoogleSheetClient.from_config(config)
    handle = client.get_worksheet(tabs["operator_digest"])
    existing_raw = handle.data_rows()
    existing_rows = [OperatorDigestRow.from_row(row) for row in existing_raw]
    existing_by_key = {row.identity_key(): row for row in existing_rows}

    merged: list[OperatorDigestRow] = []
    seen: set[str] = set()
    for row in rows:
        key = row.identity_key()
        prior = existing_by_key.get(key)
        if prior is not None:
            row.operator_notes = prior.operator_notes or row.operator_notes
        merged.append(row)
        seen.add(key)

    trailing = [
        row
        for row in existing_rows
        if row.identity_key() not in seen and row.operator_notes
    ]
    final = merged + trailing
    handle.replace_contents(OperatorDigestRow.HEADER, [row.to_row() for row in final])
    logger.info(
        "pushed operator_digest: %d refreshed rows, %d preserved noted rows",
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


def read_profile_configs_cache(
    config: Mapping[str, Any],
) -> list[ProfileConfigRow]:
    cache_path = _cache_dir(config) / "profiles.json"
    data = _load_json(cache_path) or {}
    rows: list[ProfileConfigRow] = []
    for item in data.get("rows", []):
        try:
            rows.append(ProfileConfigRow.from_row(item))
        except Exception:  # noqa: BLE001
            logger.warning("failed to parse cached profile row: %r", item)
    return rows


def read_regime_control_cache(
    config: Mapping[str, Any],
) -> list[RegimeControlRow]:
    cache_path = _cache_dir(config) / "regime_control.json"
    data = _load_json(cache_path) or {}
    rows: list[RegimeControlRow] = []
    for item in data.get("rows", []):
        try:
            rows.append(RegimeControlRow.from_row(item))
        except Exception:  # noqa: BLE001
            logger.warning("failed to parse cached regime row: %r", item)
    return rows


def read_template_library_cache(
    config: Mapping[str, Any],
) -> list[TemplateLibraryRow]:
    cache_path = _cache_dir(config) / "template_library.json"
    data = _load_json(cache_path) or {}
    rows: list[TemplateLibraryRow] = []
    for item in data.get("rows", []):
        try:
            rows.append(TemplateLibraryRow.from_row(item))
        except Exception:  # noqa: BLE001
            logger.warning("failed to parse cached template row: %r", item)
    return rows


def read_universe_cache(
    config: Mapping[str, Any],
) -> list[UniverseMemberRow]:
    cache_path = _cache_dir(config) / "universe.json"
    data = _load_json(cache_path) or {}
    rows: list[UniverseMemberRow] = []
    for item in data.get("rows", []):
        try:
            rows.append(UniverseMemberRow.from_row(item))
        except Exception:  # noqa: BLE001
            logger.warning("failed to parse cached universe row: %r", item)
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


def read_daily_plan_cache(config: Mapping[str, Any]) -> list[DailyPlanRow]:
    cache_path = _cache_dir(config) / "daily_plan.json"
    data = _load_json(cache_path) or {}
    rows: list[DailyPlanRow] = []
    for item in data.get("rows", []):
        try:
            rows.append(DailyPlanRow.from_row(item))
        except Exception:  # noqa: BLE001
            logger.warning("failed to parse cached daily_plan row: %r", item)
    return rows


def _range_min(values: Any) -> float | int | None:
    if not isinstance(values, (list, tuple)) or not values:
        return None
    return values[0]


def _range_max(values: Any) -> float | int | None:
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        return None
    return values[1]
