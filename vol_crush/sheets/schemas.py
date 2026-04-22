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

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from vol_crush.core.models import MarketRegime
from vol_crush.core.strategy_aliases import canonical_strategy_type, normalize_key
from vol_crush.integrations.google_sheets import as_bool_cell, coerce_bool


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
    "strategy_key": "row_id",
    "strategy": "strategy_id",
    "strategy_id": "strategy_id",
    "enabled": "enabled",
    "template": "template_id",
    "template_id": "template_id",
    "profile": "profile_id",
    "profile_id": "profile_id",
    "stock_profile": "stock_profile",
    "allowed_regimes": "allowed_regimes",
    "iv_rank_min": "iv_rank_min",
    "iv_rank_max": "iv_rank_max",
    "avoid_earnings": "avoid_earnings",
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
    """One operator-approved (strategy × stock_profile) combo."""

    HEADER: Sequence[str] = field(
        default=(
            "strategy_id",
            "enabled",
            "authorization_mode",
            "stock_profile",
            "allowed_regimes",
            "iv_rank_min",
            "iv_rank_max",
            "avoid_earnings",
            "max_bpr_pct_override",
            "max_positions_override",
            "strategy_key",
        ),
        repr=False,
    )

    row_id: str = ""
    strategy_id: str = ""
    enabled: bool = False
    authorization_mode: AuthorizationMode = AuthorizationMode.SHADOW
    stock_profile: str = ""
    allowed_regimes: list[str] = field(default_factory=list)
    iv_rank_min: float | None = None
    iv_rank_max: float | None = None
    avoid_earnings: bool | None = None
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
            strategy_id=normalize_key(
                str(
                    normalized.get("strategy_id")
                    or normalized.get("template_id")
                    or ""
                )
            ),
            enabled=coerce_bool(normalized.get("enabled"), default=False),
            authorization_mode=auth_mode,
            stock_profile=normalize_key(
                str(
                    normalized.get("stock_profile")
                    or normalized.get("profile_id")
                    or ""
                )
            ),
            allowed_regimes=_parse_text_list(normalized.get("allowed_regimes")),
            iv_rank_min=_parse_optional_float(normalized.get("iv_rank_min")),
            iv_rank_max=_parse_optional_float(normalized.get("iv_rank_max")),
            avoid_earnings=_parse_optional_bool(normalized.get("avoid_earnings")),
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
            self.strategy_id,
            as_bool_cell(self.enabled),
            self.authorization_mode.value,
            self.stock_profile,
            _as_list_cell(self.allowed_regimes),
            "" if self.iv_rank_min is None else self.iv_rank_min,
            "" if self.iv_rank_max is None else self.iv_rank_max,
            "" if self.avoid_earnings is None else as_bool_cell(self.avoid_earnings),
            "" if self.max_bpr_pct_override is None else self.max_bpr_pct_override,
            "" if self.max_positions_override is None else self.max_positions_override,
            self.identity_key(),
        ]

    def is_live_eligible(self) -> bool:
        """Row-level live gate."""
        return self.enabled and self.authorization_mode == AuthorizationMode.LIVE

    def identity_key(self) -> str:
        """Stable identity used for change detection + merging."""
        if self.row_id:
            return self.row_id.strip()
        return f"{self.strategy_id}::{self.stock_profile}"

    @property
    def template_id(self) -> str:
        """Backward-compatible alias for old cache readers."""
        return self.strategy_id

    @property
    def profile_id(self) -> str:
        """Backward-compatible alias for old cache readers."""
        return self.stock_profile


_IDEA_ALIASES = {
    "idea_id": "idea_id",
    "date": "date",
    "underlying": "underlying",
    "expectation": "expectation",
    "bias": "expectation",
    "direction": "expectation",
    "proposed_strategy": "proposed_strategy",
    "proposed_startegy": "proposed_strategy",
    "strategy_id": "proposed_strategy",
    "strategy_type": "strategy_type",
    "strategy": "strategy_type",
    "note": "note",
    "thesis": "note",
    "description": "description",
    "strikes": "strikes",
    "expiration": "expiration",
    "confidence": "confidence",
    "host": "host",
    "trader_name": "host",
    "video_id": "video_id",
    "source_url": "source_url",
    "url": "source_url",
    "source_timestamp": "source_timestamp",
    "timestamp": "source_timestamp",
    "rationale": "rationale",
    "approval": "approval",
    "status": "approval",
    "operator_notes": "operator_notes",
    "reviewer_notes": "operator_notes",
    "reviewed_by": "reviewed_by",
    "reviewed_at": "reviewed_at",
}


_OPERATOR_DIGEST_ALIASES = {
    "date": "date",
    "category": "category",
    "title": "title",
    "source": "source",
    "summary": "summary",
    "actionable_ideas_present": "actionable_ideas_present",
    "source_url": "source_url",
    "url": "source_url",
    "operator_notes": "operator_notes",
    "digest_id": "digest_id",
}


@dataclass
class IdeaReviewRow:
    """One human or LLM-entered trade idea awaiting approval."""

    HEADER: Sequence[str] = field(
        default=(
            "date",
            "approval",
            "underlying",
            "expectation",
            "proposed_strategy",
            "note",
            "idea_id",
            "source_url",
            "source_timestamp",
        ),
        repr=False,
    )

    idea_id: str = ""
    date: str = ""
    underlying: str = ""
    expectation: str = ""
    proposed_strategy: str = ""
    note: str = ""
    strategy_type: str = ""
    description: str = ""
    strikes: list[float] = field(default_factory=list)
    expiration: str = ""
    confidence: str = ""
    host: str = ""
    video_id: str = ""
    source_url: str = ""
    source_timestamp: str = ""
    rationale: str = ""
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
        date = str(normalized.get("date") or "").strip()
        underlying = str(normalized.get("underlying") or "").strip().upper()
        proposed_strategy = normalize_key(
            str(
                normalized.get("proposed_strategy")
                or normalized.get("strategy_type")
                or ""
            )
        )
        note = str(normalized.get("note") or normalized.get("description") or "").strip()
        idea_id = str(normalized.get("idea_id") or "").strip()
        if not idea_id:
            idea_id = _derived_idea_id(date, underlying, proposed_strategy, note)
        return cls(
            idea_id=idea_id,
            date=date,
            underlying=underlying,
            expectation=str(normalized.get("expectation") or "").strip().lower(),
            proposed_strategy=proposed_strategy,
            note=note,
            strategy_type=canonical_strategy_type(proposed_strategy),
            description=note,
            strikes=_parse_strikes(normalized.get("strikes")),
            expiration=str(normalized.get("expiration") or "").strip(),
            confidence=str(normalized.get("confidence") or "").strip(),
            host=str(normalized.get("host") or "").strip(),
            video_id=str(normalized.get("video_id") or "").strip(),
            source_url=str(normalized.get("source_url") or "").strip(),
            source_timestamp=str(normalized.get("source_timestamp") or "").strip(),
            rationale=str(normalized.get("rationale") or "").strip(),
            approval=approval,
            operator_notes=str(normalized.get("operator_notes") or "").strip(),
            reviewed_by=str(normalized.get("reviewed_by") or "").strip(),
            reviewed_at=str(normalized.get("reviewed_at") or "").strip(),
        )

    def to_row(self) -> list[Any]:
        return [
            self.date,
            self.approval.value,
            self.underlying,
            self.expectation,
            self.proposed_strategy or self.strategy_type,
            self.note or self.description,
            self.idea_id,
            self.source_url,
            self.source_timestamp,
        ]

    def identity_key(self) -> str:
        if self.idea_id:
            return self.idea_id.strip()
        return self.legacy_identity_key()

    def legacy_identity_key(self) -> str:
        return _derived_idea_id(
            self.date,
            self.underlying,
            self.proposed_strategy or self.strategy_type,
            self.note or self.description,
        )


@dataclass
class OperatorDigestRow:
    """Brief operator-facing context row, kept separate from execution ideas."""

    HEADER: Sequence[str] = field(
        default=(
            "date",
            "category",
            "title",
            "source",
            "summary",
            "actionable_ideas_present",
            "source_url",
            "operator_notes",
            "digest_id",
        ),
        repr=False,
    )

    digest_id: str = ""
    date: str = ""
    category: str = ""
    title: str = ""
    source: str = ""
    summary: str = ""
    actionable_ideas_present: bool = False
    source_url: str = ""
    operator_notes: str = ""

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> "OperatorDigestRow":
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            canonical = _OPERATOR_DIGEST_ALIASES.get(str(key).strip().lower())
            if canonical:
                normalized[canonical] = value
        row = cls(
            digest_id=str(normalized.get("digest_id") or "").strip(),
            date=str(normalized.get("date") or "").strip(),
            category=str(normalized.get("category") or "").strip().lower(),
            title=str(normalized.get("title") or "").strip(),
            source=str(normalized.get("source") or "").strip(),
            summary=str(normalized.get("summary") or "").strip(),
            actionable_ideas_present=coerce_bool(
                normalized.get("actionable_ideas_present"), default=False
            ),
            source_url=str(normalized.get("source_url") or "").strip(),
            operator_notes=str(normalized.get("operator_notes") or "").strip(),
        )
        if not row.digest_id:
            row.digest_id = row.identity_key()
        return row

    def to_row(self) -> list[Any]:
        return [
            self.date,
            self.category,
            self.title,
            self.source,
            self.summary,
            as_bool_cell(self.actionable_ideas_present),
            self.source_url,
            self.operator_notes,
            self.digest_id or self.identity_key(),
        ]

    def identity_key(self) -> str:
        if self.digest_id:
            return self.digest_id
        basis = f"{self.date}|{self.category}|{self.title}|{self.source_url}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass
class DailyPlanRow:
    """One proposed execution-plan line for operator approval."""

    HEADER: Sequence[str] = field(
        default=(
            "plan_id",
            "date",
            "underlying",
            "strategy",
            "approval",
            "note",
            "idea_id",
        ),
        repr=False,
    )

    plan_id: str = ""
    date: str = ""
    underlying: str = ""
    strategy: str = ""
    approval: str = ""
    note: str = ""
    idea_id: str = ""
    decision: str = ""
    combo_description: str = ""
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

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> "DailyPlanRow":
        normalized = {str(k).strip().lower(): v for k, v in raw.items()}
        return cls(
            plan_id=str(normalized.get("plan_id") or "").strip(),
            date=str(normalized.get("date") or "").strip(),
            underlying=str(normalized.get("underlying") or "").strip().upper(),
            strategy=canonical_strategy_type(
                str(
                    normalized.get("strategy")
                    or normalized.get("proposed_strategy")
                    or ""
                )
            ),
            approval=str(normalized.get("approval") or "").strip().lower(),
            note=str(normalized.get("note") or "").strip(),
            idea_id=str(normalized.get("idea_id") or "").strip(),
        )

    def to_row(self) -> list[Any]:
        return [
            self.plan_id,
            self.date,
            self.underlying,
            self.strategy,
            self.approval,
            self.note or self.combo_description or self.decision,
            self.idea_id,
        ]


_PROFILE_ALIASES = {
    "stock_profile": "stock_profile",
    "profile": "stock_profile",
    "profile_id": "stock_profile",
    "max_bpr_pct": "max_bpr_pct",
    "max_per_position_pct": "max_per_position_pct",
    "max_positions": "max_positions",
    "earnings_sensitive": "earnings_sensitive",
    "notes": "notes",
    "profile_key": "profile_key",
}


_TEMPLATE_ALIASES = {
    "template_id": "template_id",
    "id": "template_id",
    "template_key": "template_key",
    "strategy_id": "strategy_id",
    "strategy": "strategy_id",
    "structure": "structure",
    "name": "name",
    "allowed_regimes": "allowed_regimes",
    "iv_rank_min": "iv_rank_min",
    "iv_rank_max": "iv_rank_max",
    "dte_min": "dte_min",
    "dte_max": "dte_max",
    "delta_min": "delta_min",
    "delta_max": "delta_max",
    "spread_width": "spread_width",
    "min_credit_to_width_ratio": "min_credit_to_width_ratio",
    "profit_target_pct": "profit_target_pct",
    "max_loss_multiple": "max_loss_multiple",
    "roll_dte_trigger": "roll_dte_trigger",
    "roll_for_credit": "roll_for_credit",
    "close_before_expiration": "close_before_expiration",
    "avoid_earnings": "avoid_earnings",
    "notes": "notes",
}


_REGIME_ALIASES = {
    "date": "date",
    "trading_date": "date",
    "regime": "regime",
    "override_enabled": "override_enabled",
    "enabled": "override_enabled",
    "note": "note",
    "notes": "note",
}


@dataclass
class RegimeControlRow:
    """Optional day-level regime override managed from Sheets."""

    HEADER: Sequence[str] = field(
        default=("date", "regime", "override_enabled", "note"),
        repr=False,
    )

    date: str = ""
    regime: str = ""
    override_enabled: bool = False
    note: str = ""

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> "RegimeControlRow":
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            canonical = _REGIME_ALIASES.get(str(key).strip().lower())
            if canonical:
                normalized[canonical] = value
        regime_raw = normalize_key(str(normalized.get("regime") or ""))
        valid = {regime.value for regime in MarketRegime}
        return cls(
            date=str(normalized.get("date") or "").strip(),
            regime=regime_raw if regime_raw in valid else MarketRegime.UNKNOWN.value,
            override_enabled=coerce_bool(
                normalized.get("override_enabled"), default=False
            ),
            note=str(normalized.get("note") or "").strip(),
        )

    def to_row(self) -> list[Any]:
        return [
            self.date,
            self.regime,
            as_bool_cell(self.override_enabled),
            self.note,
        ]

    def identity_key(self) -> str:
        return self.date


@dataclass
class TemplateLibraryRow:
    """Structure-level defaults and gating knobs managed from Sheets."""

    HEADER: Sequence[str] = field(
        default=(
            "template_id",
            "strategy_id",
            "structure",
            "name",
            "allowed_regimes",
            "iv_rank_min",
            "iv_rank_max",
            "dte_min",
            "dte_max",
            "delta_min",
            "delta_max",
            "spread_width",
            "min_credit_to_width_ratio",
            "profit_target_pct",
            "max_loss_multiple",
            "roll_dte_trigger",
            "roll_for_credit",
            "close_before_expiration",
            "avoid_earnings",
            "template_key",
        ),
        repr=False,
    )

    template_id: str = ""
    strategy_id: str = ""
    structure: str = ""
    name: str = ""
    allowed_regimes: list[str] = field(default_factory=list)
    iv_rank_min: float | None = None
    iv_rank_max: float | None = None
    dte_min: int | None = None
    dte_max: int | None = None
    delta_min: float | None = None
    delta_max: float | None = None
    spread_width: float | None = None
    min_credit_to_width_ratio: float | None = None
    profit_target_pct: float | None = None
    max_loss_multiple: float | None = None
    roll_dte_trigger: int | None = None
    roll_for_credit: bool | None = None
    close_before_expiration: bool | None = None
    avoid_earnings: bool | None = None
    notes: str = ""

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> "TemplateLibraryRow":
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            canonical = _TEMPLATE_ALIASES.get(str(key).strip().lower())
            if canonical:
                normalized[canonical] = value
        strategy_id = normalize_key(str(normalized.get("strategy_id") or ""))
        structure = canonical_strategy_type(
            str(normalized.get("structure") or strategy_id)
        )
        return cls(
            template_id=normalize_key(str(normalized.get("template_id") or "")),
            strategy_id=strategy_id,
            structure=structure,
            name=str(normalized.get("name") or "").strip(),
            allowed_regimes=_parse_text_list(normalized.get("allowed_regimes")),
            iv_rank_min=_parse_optional_float(normalized.get("iv_rank_min")),
            iv_rank_max=_parse_optional_float(normalized.get("iv_rank_max")),
            dte_min=_parse_optional_int(normalized.get("dte_min")),
            dte_max=_parse_optional_int(normalized.get("dte_max")),
            delta_min=_parse_optional_float(normalized.get("delta_min")),
            delta_max=_parse_optional_float(normalized.get("delta_max")),
            spread_width=_parse_optional_float(normalized.get("spread_width")),
            min_credit_to_width_ratio=_parse_optional_float(
                normalized.get("min_credit_to_width_ratio")
            ),
            profit_target_pct=_parse_optional_float(
                normalized.get("profit_target_pct")
            ),
            max_loss_multiple=_parse_optional_float(
                normalized.get("max_loss_multiple")
            ),
            roll_dte_trigger=_parse_optional_int(normalized.get("roll_dte_trigger")),
            roll_for_credit=_parse_optional_bool(normalized.get("roll_for_credit")),
            close_before_expiration=_parse_optional_bool(
                normalized.get("close_before_expiration")
            ),
            avoid_earnings=_parse_optional_bool(normalized.get("avoid_earnings")),
            notes=str(normalized.get("notes") or "").strip(),
        )

    def to_row(self) -> list[Any]:
        return [
            self.template_id,
            self.strategy_id,
            self.structure,
            self.name,
            _as_list_cell(self.allowed_regimes),
            "" if self.iv_rank_min is None else self.iv_rank_min,
            "" if self.iv_rank_max is None else self.iv_rank_max,
            "" if self.dte_min is None else self.dte_min,
            "" if self.dte_max is None else self.dte_max,
            "" if self.delta_min is None else self.delta_min,
            "" if self.delta_max is None else self.delta_max,
            "" if self.spread_width is None else self.spread_width,
            ""
            if self.min_credit_to_width_ratio is None
            else self.min_credit_to_width_ratio,
            "" if self.profit_target_pct is None else self.profit_target_pct,
            "" if self.max_loss_multiple is None else self.max_loss_multiple,
            "" if self.roll_dte_trigger is None else self.roll_dte_trigger,
            "" if self.roll_for_credit is None else as_bool_cell(self.roll_for_credit),
            ""
            if self.close_before_expiration is None
            else as_bool_cell(self.close_before_expiration),
            "" if self.avoid_earnings is None else as_bool_cell(self.avoid_earnings),
            self.identity_key(),
        ]

    def identity_key(self) -> str:
        return self.template_id


@dataclass
class ProfileConfigRow:
    """Profile-level risk caps and rules, managed from Sheets."""

    HEADER: Sequence[str] = field(
        default=(
            "stock_profile",
            "max_bpr_pct",
            "max_per_position_pct",
            "max_positions",
            "earnings_sensitive",
            "profile_key",
        ),
        repr=False,
    )

    stock_profile: str = ""
    max_bpr_pct: float | None = None
    max_per_position_pct: float | None = None
    max_positions: int | None = None
    earnings_sensitive: bool | None = None
    notes: str = ""

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> "ProfileConfigRow":
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            canonical = _PROFILE_ALIASES.get(str(key).strip().lower())
            if canonical:
                normalized[canonical] = value
        return cls(
            stock_profile=normalize_key(str(normalized.get("stock_profile") or "")),
            max_bpr_pct=_parse_optional_float(normalized.get("max_bpr_pct")),
            max_per_position_pct=_parse_optional_float(
                normalized.get("max_per_position_pct")
            ),
            max_positions=_parse_optional_int(normalized.get("max_positions")),
            earnings_sensitive=_parse_optional_bool(
                normalized.get("earnings_sensitive")
            ),
            notes=str(normalized.get("notes") or "").strip(),
        )

    def to_row(self) -> list[Any]:
        return [
            self.stock_profile,
            "" if self.max_bpr_pct is None else self.max_bpr_pct,
            "" if self.max_per_position_pct is None else self.max_per_position_pct,
            "" if self.max_positions is None else self.max_positions,
            "" if self.earnings_sensitive is None else as_bool_cell(self.earnings_sensitive),
            self.identity_key(),
        ]

    def identity_key(self) -> str:
        return self.stock_profile


_UNIVERSE_ALIASES = {
    "symbol": "symbol",
    "ticker": "symbol",
    "underlying": "symbol",
    "stock_profile": "stock_profile",
    "profile": "stock_profile",
    "profile_id": "stock_profile",
    "enabled": "enabled",
    "notes": "notes",
}


@dataclass
class UniverseMemberRow:
    """One symbol membership row for the operator-maintained universe."""

    HEADER: Sequence[str] = field(
        default=("symbol", "stock_profile", "enabled"),
        repr=False,
    )

    symbol: str = ""
    stock_profile: str = ""
    enabled: bool = True
    notes: str = ""

    @classmethod
    def from_row(cls, raw: Mapping[str, Any]) -> "UniverseMemberRow":
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            canonical = _UNIVERSE_ALIASES.get(str(key).strip().lower())
            if canonical:
                normalized[canonical] = value
        return cls(
            symbol=str(normalized.get("symbol") or "").strip().upper(),
            stock_profile=normalize_key(str(normalized.get("stock_profile") or "")),
            enabled=coerce_bool(normalized.get("enabled"), default=True),
            notes=str(normalized.get("notes") or "").strip(),
        )

    def to_row(self) -> list[Any]:
        return [self.symbol, self.stock_profile, as_bool_cell(self.enabled)]


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


def _parse_optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    return coerce_bool(value, default=False)


def _parse_text_list(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        items = raw
    else:
        text = str(raw).replace("\n", ",").replace("|", ",")
        items = [part.strip() for part in text.split(",")]
    return [normalize_key(item) for item in items if str(item).strip()]


def _as_list_cell(values: Sequence[str]) -> str:
    return ", ".join(str(value).strip() for value in values if str(value).strip())


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


def _derived_idea_id(date: str, underlying: str, strategy: str, note: str) -> str:
    raw = "|".join(
        [
            str(date or "").strip(),
            str(underlying or "").strip().upper(),
            canonical_strategy_type(strategy),
        ]
    )
    return "sheetidea_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
