"""Pydantic row models for the kamandal_control Google Sheet.

Each model maps one sheet tab. They share three traits:

1. ``HEADER`` — ordered list of column names as rendered in the sheet.
2. ``from_row`` — forgiving parser that tolerates missing / extra columns,
   column aliases, and common boolean spellings (TRUE/true/yes/1).
3. ``to_row`` — dict → ordered list matching ``HEADER``.

Every model is designed to round-trip cleanly: parse → modify → render →
parse produces the same object. That property is what makes the sheet the
source of truth without us writing a diff engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from vol_crush.integrations.google_sheets import as_bool_cell, as_list_cell, coerce_bool


class AuthorizationMode(str, Enum):
    SHADOW = "shadow"
    LIVE = "live"


class IdeaApproval(str, Enum):
    PENDING = ""         # default when the row is first written
    APPROVED = "approve"
    REJECTED = "reject"
    HOLD = "hold"


# Column alias map: sheet header → canonical field. Supports minor operator
# drift (e.g. renaming "approved by" to "reviewer") without breaking ingest.
_STRATEGY_ALIASES = {
    "id": "row_id",
    "row_id": "row_id",
    "enabled": "enabled",
    "template": "template_id",
    "template_id": "template_id",
    "profile": "profile_id",
    "profile_id": "profile_id",
    "mode": "authorization_mode",
    "authorization_mode": "authorization_mode",
    "backtest_approved": "backtest_approved",
    "dry_run_passed": "dry_run_passed",
    "max_bpr_pct_override": "max_bpr_pct_override",
    "max_positions_override": "max_positions_override",
    "approved_by": "approved_by",
    "reviewer": "approved_by",
    "approval_reason": "approval_reason",
    "reason": "approval_reason",
    "approved_at": "approved_at",
    "notes": "notes",
}


@dataclass
class StrategyApprovalRow:
    """One approved (template × profile) combo."""

    HEADER: Sequence[str] = field(
        default=(
            "row_id",
            "enabled",
            "template_id",
            "profile_id",
            "authorization_mode",
            "backtest_approved",
            "dry_run_passed",
            "max_bpr_pct_override",
            "max_positions_override",
            "approved_by",
            "approval_reason",
            "approved_at",
            "notes",
        ),
        repr=False,
    )

    row_id: str = ""
    enabled: bool = False
    template_id: str = ""
    profile_id: str = ""
    authorization_mode: AuthorizationMode = AuthorizationMode.SHADOW
    backtest_approved: bool = False
    dry_run_passed: bool = False
    max_bpr_pct_override: float | None = None
    max_positions_override: int | None = None
    approved_by: str = ""
    approval_reason: str = ""
    approved_at: str = ""
    notes: str = ""

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> "StrategyApprovalRow":
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            canonical = _STRATEGY_ALIASES.get(str(key).strip().lower())
            if canonical:
                normalized[canonical] = value
        mode_raw = str(normalized.get("authorization_mode", "")).strip().lower()
        if mode_raw in ("", "paper", "shadow"):
            auth_mode = AuthorizationMode.SHADOW
        elif mode_raw == "live":
            auth_mode = AuthorizationMode.LIVE
        else:
            auth_mode = AuthorizationMode.SHADOW
        return cls(
            row_id=str(normalized.get("row_id") or "").strip(),
            enabled=coerce_bool(normalized.get("enabled"), default=False),
            template_id=str(normalized.get("template_id") or "").strip(),
            profile_id=str(normalized.get("profile_id") or "").strip(),
            authorization_mode=auth_mode,
            backtest_approved=coerce_bool(
                normalized.get("backtest_approved"), default=False
            ),
            dry_run_passed=coerce_bool(
                normalized.get("dry_run_passed"), default=False
            ),
            max_bpr_pct_override=_parse_optional_float(
                normalized.get("max_bpr_pct_override")
            ),
            max_positions_override=_parse_optional_int(
                normalized.get("max_positions_override")
            ),
            approved_by=str(normalized.get("approved_by") or "").strip(),
            approval_reason=str(normalized.get("approval_reason") or "").strip(),
            approved_at=str(normalized.get("approved_at") or "").strip(),
            notes=str(normalized.get("notes") or "").strip(),
        )

    def to_row(self) -> list[Any]:
        return [
            self.row_id,
            as_bool_cell(self.enabled),
            self.template_id,
            self.profile_id,
            self.authorization_mode.value,
            as_bool_cell(self.backtest_approved),
            as_bool_cell(self.dry_run_passed),
            "" if self.max_bpr_pct_override is None else self.max_bpr_pct_override,
            "" if self.max_positions_override is None else self.max_positions_override,
            self.approved_by,
            self.approval_reason,
            self.approved_at,
            self.notes,
        ]

    def is_live_eligible(self) -> bool:
        """Row-level live gate: all three must hold."""
        return (
            self.enabled
            and self.backtest_approved
            and self.dry_run_passed
            and self.authorization_mode == AuthorizationMode.LIVE
        )

    def identity_key(self) -> str:
        """Stable identity used for change detection + merging."""
        if self.row_id:
            return self.row_id.strip()
        return f"{self.template_id}::{self.profile_id}"


_IDEA_ALIASES = {
    "idea_id": "idea_id",
    "date": "date",
    "underlying": "underlying",
    "strategy_type": "strategy_type",
    "strategy": "strategy_type",
    "description": "description",
    "strikes": "strikes",
    "expiration": "expiration",
    "confidence": "confidence",
    "host": "host",
    "trader_name": "host",
    "video_id": "video_id",
    "source_url": "source_url",
    "url": "source_url",
    "approval": "approval",
    "status": "approval",
    "operator_notes": "operator_notes",
    "reviewer_notes": "operator_notes",
    "reviewed_by": "reviewed_by",
    "reviewed_at": "reviewed_at",
}


@dataclass
class IdeaReviewRow:
    """One LLM-extracted trade idea awaiting human approval."""

    HEADER: Sequence[str] = field(
        default=(
            "idea_id",
            "date",
            "underlying",
            "strategy_type",
            "description",
            "strikes",
            "expiration",
            "confidence",
            "host",
            "video_id",
            "source_url",
            "approval",
            "operator_notes",
            "reviewed_by",
            "reviewed_at",
        ),
        repr=False,
    )

    idea_id: str = ""
    date: str = ""
    underlying: str = ""
    strategy_type: str = ""
    description: str = ""
    strikes: list[float] = field(default_factory=list)
    expiration: str = ""
    confidence: str = ""
    host: str = ""
    video_id: str = ""
    source_url: str = ""
    approval: IdeaApproval = IdeaApproval.PENDING
    operator_notes: str = ""
    reviewed_by: str = ""
    reviewed_at: str = ""

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> "IdeaReviewRow":
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            canonical = _IDEA_ALIASES.get(str(key).strip().lower())
            if canonical:
                normalized[canonical] = value
        approval_raw = str(normalized.get("approval", "")).strip().lower()
        try:
            approval = IdeaApproval(approval_raw)
        except ValueError:
            approval = IdeaApproval.PENDING
        return cls(
            idea_id=str(normalized.get("idea_id") or "").strip(),
            date=str(normalized.get("date") or "").strip(),
            underlying=str(normalized.get("underlying") or "").strip().upper(),
            strategy_type=str(normalized.get("strategy_type") or "").strip(),
            description=str(normalized.get("description") or "").strip(),
            strikes=_parse_strikes(normalized.get("strikes")),
            expiration=str(normalized.get("expiration") or "").strip(),
            confidence=str(normalized.get("confidence") or "").strip(),
            host=str(normalized.get("host") or "").strip(),
            video_id=str(normalized.get("video_id") or "").strip(),
            source_url=str(normalized.get("source_url") or "").strip(),
            approval=approval,
            operator_notes=str(normalized.get("operator_notes") or "").strip(),
            reviewed_by=str(normalized.get("reviewed_by") or "").strip(),
            reviewed_at=str(normalized.get("reviewed_at") or "").strip(),
        )

    def to_row(self) -> list[Any]:
        return [
            self.idea_id,
            self.date,
            self.underlying,
            self.strategy_type,
            self.description,
            as_list_cell(self.strikes),
            self.expiration,
            self.confidence,
            self.host,
            self.video_id,
            self.source_url,
            self.approval.value,
            self.operator_notes,
            self.reviewed_by,
            self.reviewed_at,
        ]


@dataclass
class DailyPlanRow:
    """One line on today's optimizer output."""

    HEADER: Sequence[str] = field(
        default=(
            "plan_id",
            "date",
            "decision",
            "combo_description",
            "underlying",
            "strategy",
            "strikes",
            "expiration",
            "quantity",
            "est_credit",
            "est_bpr",
            "est_max_loss",
            "regime",
            "risk_flags",
            "execution_mode",
            "created_at",
        ),
        repr=False,
    )

    plan_id: str = ""
    date: str = ""
    decision: str = ""
    combo_description: str = ""
    underlying: str = ""
    strategy: str = ""
    strikes: list[float] = field(default_factory=list)
    expiration: str = ""
    quantity: int = 0
    est_credit: float = 0.0
    est_bpr: float = 0.0
    est_max_loss: float = 0.0
    regime: str = ""
    risk_flags: list[str] = field(default_factory=list)
    execution_mode: str = ""
    created_at: str = ""

    def to_row(self) -> list[Any]:
        return [
            self.plan_id,
            self.date,
            self.decision,
            self.combo_description,
            self.underlying,
            self.strategy,
            as_list_cell(self.strikes),
            self.expiration,
            self.quantity,
            self.est_credit,
            self.est_bpr,
            self.est_max_loss,
            self.regime,
            as_list_cell(self.risk_flags),
            self.execution_mode,
            self.created_at,
        ]


@dataclass
class PositionRow:
    """Grouped Position with health flags."""

    HEADER: Sequence[str] = field(
        default=(
            "group_id",
            "strategy_type",
            "underlying",
            "legs_summary",
            "expiration",
            "quantity",
            "net_delta",
            "net_theta",
            "bpr_used",
            "pnl_unrealized",
            "management_status",
            "source",
            "opened_at",
            "days_open",
        ),
        repr=False,
    )

    group_id: str = ""
    strategy_type: str = ""
    underlying: str = ""
    legs_summary: str = ""
    expiration: str = ""
    quantity: int = 0
    net_delta: float = 0.0
    net_theta: float = 0.0
    bpr_used: float = 0.0
    pnl_unrealized: float = 0.0
    management_status: str = ""
    source: str = ""
    opened_at: str = ""
    days_open: int = 0

    def to_row(self) -> list[Any]:
        return [
            self.group_id,
            self.strategy_type,
            self.underlying,
            self.legs_summary,
            self.expiration,
            self.quantity,
            self.net_delta,
            self.net_theta,
            self.bpr_used,
            self.pnl_unrealized,
            self.management_status,
            self.source,
            self.opened_at,
            self.days_open,
        ]


# ── Parsing helpers ─────────────────────────────────────────────────


def _parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _parse_strikes(raw: Any) -> list[float]:
    if raw in (None, ""):
        return []
    if isinstance(raw, (int, float)):
        return [float(raw)]
    if isinstance(raw, list):
        out: list[float] = []
        for item in raw:
            try:
                out.append(float(item))
            except (ValueError, TypeError):
                continue
        return out
    text = str(raw).replace(",", " ").replace("/", " ").replace("|", " ")
    out = []
    for token in text.split():
        try:
            out.append(float(token.lstrip("$").rstrip(".")))
        except ValueError:
            continue
    return out
