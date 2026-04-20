"""
Domain models for Vol Crush.

Pydantic models for strategies, trade ideas, positions, and portfolio state.
These are the shared data structures used across all modules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any


class StrategyType(str, Enum):
    """Supported option strategy structures."""

    SHORT_STRANGLE = "short_strangle"
    SHORT_PUT = "short_put"
    SHORT_CALL = "short_call"
    LONG_PUT = "long_put"
    LONG_CALL = "long_call"
    IRON_CONDOR = "iron_condor"
    PUT_SPREAD = "put_spread"
    CALL_SPREAD = "call_spread"
    JADE_LIZARD = "jade_lizard"
    BIG_LIZARD = "big_lizard"
    CALENDAR_SPREAD = "calendar_spread"
    COVERED_STRANGLE = "covered_strangle"
    STRADDLE = "straddle"
    UNKNOWN_COMPLEX = "unknown_complex"
    ORPHAN_LEG = "orphan_leg"
    CUSTOM = "custom"


class PositionSource(str, Enum):
    """How a Position (group) came to exist in Kamandal's view of the book."""

    KAMANDAL_ORDER = (
        "kamandal_order"  # opened by our executor, tied to a broker orderId we issued
    )
    PUBLIC_ORDER = "public_order"  # rehydrated from GET /order/{id} on the broker side
    PUBLIC_INFERRED = (
        "public_inferred"  # grouped deterministically from raw broker legs
    )
    MANUAL = "manual"  # human-entered or test-only


class GroupConfidence(str, Enum):
    """How sure Kamandal is that a group's classification is correct."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ManagementStatus(str, Enum):
    """Whether the position manager is allowed to emit automatic actions for a group."""

    AUTO = "auto"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    BLOCKED = "blocked"


class IdeaStatus(str, Enum):
    NEW = "new"
    EVALUATED = "evaluated"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ROLLED = "rolled"
    ADJUSTED = "adjusted"


class BacktestStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ExecutionMode(str, Enum):
    # SHADOW = write PendingOrder + full preflight, do not submit to broker.
    # Canonical; matches the bhiksha authorization_mode vocabulary and the
    # cross-project "shadow → live graduation" language in /Users/sunny/Documents/CLAUDE.md.
    SHADOW = "shadow"
    # PENDING is a deprecated alias for SHADOW, kept so existing configs keep
    # working. Treat it the same as SHADOW in all gate checks.
    PENDING = "pending"
    DRY_RUN = "dry_run"
    LIVE = "live"


class MarketRegime(str, Enum):
    HIGH_IV = "high_iv"
    NORMAL_IV = "normal_iv"
    LOW_IV = "low_iv"
    EVENT_RISK = "event_risk"
    UNKNOWN = "unknown"


class SourceType(str, Enum):
    YOUTUBE = "youtube"
    RSS = "rss"
    WEB = "web"
    TRANSCRIPT = "transcript"
    AUDIO = "audio"


class RawContentStatus(str, Enum):
    NEW = "new"
    EXTRACTED = "extracted"
    DUPLICATE = "duplicate"
    FAILED = "failed"


@dataclass
class ManagementRules:
    """Position management rules for a strategy."""

    profit_target_pct: float = 50.0  # close at X% of max profit
    max_loss_multiple: float = 2.0  # close at Nx credit received
    roll_dte_trigger: int = 21  # roll when DTE <= this
    roll_for_credit: bool = True  # only roll if net credit
    close_before_expiration: bool = True  # never hold to expiry

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ManagementRules:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class StrategyFilters:
    """Entry filters for a strategy."""

    iv_rank_min: float | None = None  # minimum IV rank to enter
    iv_rank_max: float | None = None
    dte_range: tuple[int, int] = (30, 45)  # DTE range for entry
    delta_range: tuple[float, float] = (0.14, 0.18)  # delta per leg
    spread_width: float | None = None  # for defined-risk strategies
    min_credit_to_width_ratio: float | None = None  # e.g. 0.33 for put spreads
    underlyings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyFilters:
        data = {}
        for k, v in d.items():
            if k in cls.__dataclass_fields__:
                # Convert lists to tuples for range fields
                if k in ("dte_range", "delta_range") and isinstance(v, list):
                    data[k] = tuple(v)
                else:
                    data[k] = v
        return cls(**data)


@dataclass
class StrategyAllocation:
    """Position sizing / allocation rules."""

    max_bpr_pct: float = 30.0  # max % of BPR for this strategy
    max_per_position_pct: float = 10.0  # max BPR per single position
    max_positions: int = 5  # max concurrent positions of this type

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyAllocation:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Strategy:
    """A fully defined, approved trading strategy."""

    id: str
    name: str
    structure: StrategyType
    description: str = ""
    filters: StrategyFilters = field(default_factory=StrategyFilters)
    management: ManagementRules = field(default_factory=ManagementRules)
    allocation: StrategyAllocation = field(default_factory=StrategyAllocation)
    backtest_approved: bool = False
    dry_run_passed: bool = False
    source_traders: list[str] = field(default_factory=list)
    consensus_notes: str = ""
    allowed_regimes: list[str] = field(default_factory=list)
    avoid_earnings: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Strategy:
        """Create a Strategy from a dictionary (e.g. from YAML)."""
        data = {
            k: v
            for k, v in d.items()
            if k not in ("filters", "management", "allocation", "structure")
            and k in cls.__dataclass_fields__
        }

        # Parse structure enum
        structure_val = d.get("structure", "custom")
        try:
            data["structure"] = StrategyType(structure_val)
        except ValueError:
            data["structure"] = StrategyType.CUSTOM

        # Parse nested objects
        if "filters" in d and isinstance(d["filters"], dict):
            data["filters"] = StrategyFilters.from_dict(d["filters"])
        if "management" in d and isinstance(d["management"], dict):
            data["management"] = ManagementRules.from_dict(d["management"])
        if "allocation" in d and isinstance(d["allocation"], dict):
            data["allocation"] = StrategyAllocation.from_dict(d["allocation"])

        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary suitable for YAML output."""
        from dataclasses import asdict

        d = asdict(self)
        d["structure"] = self.structure.value
        # Convert tuples back to lists for YAML
        if "filters" in d:
            for k in ("dte_range", "delta_range"):
                if k in d["filters"] and isinstance(d["filters"][k], tuple):
                    d["filters"][k] = list(d["filters"][k])
        return d


@dataclass
class StrategyTemplate:
    """Structure-level strategy definition from strategy_templates.yaml.

    A template describes HOW to build a particular options structure — management
    rules, entry filters, regime eligibility. It does NOT say which tickers to use
    (that comes from UnderlyingProfile) or how much capital to allocate (also from
    the profile). At resolution time, template + profile → Strategy.
    """

    id: str
    name: str
    structure: StrategyType
    description: str = ""
    filters: StrategyFilters = field(default_factory=StrategyFilters)
    management: ManagementRules = field(default_factory=ManagementRules)
    allowed_regimes: list[str] = field(default_factory=list)
    avoid_earnings: bool = True
    backtest_approved: bool = False
    dry_run_passed: bool = False
    source_traders: list[str] = field(default_factory=list)
    consensus_notes: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyTemplate:
        data = {
            k: v
            for k, v in d.items()
            if k not in ("filters", "management", "structure", "allowed_regimes")
            and k in cls.__dataclass_fields__
        }
        structure_val = d.get("structure", "custom")
        try:
            data["structure"] = StrategyType(structure_val)
        except ValueError:
            data["structure"] = StrategyType.CUSTOM
        if "filters" in d and isinstance(d["filters"], dict):
            data["filters"] = StrategyFilters.from_dict(d["filters"])
        if "management" in d and isinstance(d["management"], dict):
            data["management"] = ManagementRules.from_dict(d["management"])
        data["allowed_regimes"] = list(d.get("allowed_regimes", []))
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["structure"] = self.structure.value
        if "filters" in d:
            for k in ("dte_range", "delta_range"):
                if k in d["filters"] and isinstance(d["filters"][k], tuple):
                    d["filters"][k] = list(d["filters"][k])
        return d


@dataclass
class UnderlyingProfile:
    """Universe grouping from underlying_profiles.yaml.

    Describes a category of underlyings (index ETFs, bond ETFs, etc.) and the
    risk/allocation constraints that apply to all symbols in the group. The
    optimizer uses profiles to decide which structures are eligible for a ticker
    and how much capital to allocate.
    """

    profile_id: str
    name: str = ""
    symbols: list[str] = field(default_factory=list)
    allowed_structures: list[str] = field(default_factory=list)
    max_bpr_pct: float = 15.0
    max_per_position_pct: float = 10.0
    max_positions: int = 3
    earnings_sensitive: bool = True
    min_option_volume: int = 0
    min_open_interest: int = 0
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UnderlyingProfile:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_strategy(
    template: StrategyTemplate, profile: UnderlyingProfile
) -> Strategy:
    """Merge a template with a profile to produce a runtime Strategy.

    Template provides: structure, entry filters, management rules, regime eligibility.
    Profile provides: underlyings list, allocation caps.
    The resulting Strategy is the object the optimizer and position_manager consume.
    """
    filters = StrategyFilters(
        iv_rank_min=template.filters.iv_rank_min,
        iv_rank_max=template.filters.iv_rank_max,
        dte_range=template.filters.dte_range,
        delta_range=template.filters.delta_range,
        spread_width=template.filters.spread_width,
        min_credit_to_width_ratio=template.filters.min_credit_to_width_ratio,
        underlyings=list(profile.symbols),
    )
    allocation = StrategyAllocation(
        max_bpr_pct=profile.max_bpr_pct,
        max_per_position_pct=profile.max_per_position_pct,
        max_positions=profile.max_positions,
    )
    return Strategy(
        id=f"{template.id}:{profile.profile_id}",
        name=template.name,
        structure=template.structure,
        description=template.description,
        filters=filters,
        management=template.management,
        allocation=allocation,
        backtest_approved=template.backtest_approved,
        dry_run_passed=template.dry_run_passed,
        source_traders=list(template.source_traders),
        consensus_notes=template.consensus_notes,
        allowed_regimes=list(template.allowed_regimes),
        avoid_earnings=template.avoid_earnings,
    )


def resolve_all_strategies(
    templates: list[StrategyTemplate],
    profiles: list[UnderlyingProfile],
) -> list[Strategy]:
    """Produce one resolved Strategy per (template, eligible profile) pair.

    A template is eligible for a profile if the template's structure is in the
    profile's allowed_structures. This is the cross-product that feeds the optimizer.
    """
    resolved: list[Strategy] = []
    for template in templates:
        for profile in profiles:
            if template.structure.value in profile.allowed_structures:
                resolved.append(resolve_strategy(template, profile))
    return resolved


@dataclass
class ExtractedStrategyCandidate:
    """A raw strategy candidate extracted from a transcript by the LLM.

    This is NOT yet an approved Strategy. It needs human review first.
    """

    source_file: str
    trader_name: str
    show_name: str
    strategy_name: str
    structure: str  # raw string from LLM, mapped to StrategyType later
    description: str
    underlyings: list[str] = field(default_factory=list)
    iv_rank_filter: str = ""  # e.g. "above 30"
    dte_preference: str = ""  # e.g. "30-45 DTE"
    delta_targets: str = ""  # e.g. "16 delta each side"
    spread_width: str = ""  # e.g. "$5 wide"
    profit_target: str = ""  # e.g. "50% of max profit"
    loss_management: str = ""  # e.g. "2x credit received"
    roll_rules: str = ""  # e.g. "21 DTE, roll for credit"
    position_sizing: str = ""  # e.g. "25% of BPR max"
    allocation_notes: str = ""
    win_rate_claimed: str = ""
    annual_return_claimed: str = ""
    portfolio_greek_notes: str = ""  # any portfolio-level Greek guidance
    key_quotes: list[str] = field(default_factory=list)
    confidence: str = ""  # how specific/confident was the source

    def summary(self) -> str:
        """One-line summary for display."""
        return (
            f"[{self.source_file}] {self.trader_name}: {self.strategy_name} "
            f"({self.structure}) — {self.description[:80]}"
        )


# ── Models for Modules 1-4 and Backtester ────────────────────────────


class TradeAction(str, Enum):
    """Actions that can be taken on a position."""

    OPEN = "open"
    CLOSE = "close"
    ROLL = "roll"
    ADJUST = "adjust"


class OrderStatus(str, Enum):
    PENDING = "pending"
    WORKING = "working"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    DRY_RUN = "dry_run"


class PlanDecision(str, Enum):
    EXECUTE = "execute"
    NO_TRADE = "no_trade"
    ADJUST_ONLY = "adjust_only"


@dataclass
class Greeks:
    """Option Greeks for a single leg or aggregated portfolio."""

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0

    def __add__(self, other: Greeks) -> Greeks:
        return Greeks(
            delta=self.delta + other.delta,
            gamma=self.gamma + other.gamma,
            theta=self.theta + other.theta,
            vega=self.vega + other.vega,
        )

    def __mul__(self, scalar: float) -> Greeks:
        return Greeks(
            delta=self.delta * scalar,
            gamma=self.gamma * scalar,
            theta=self.theta * scalar,
            vega=self.vega * scalar,
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Greeks:
        return cls(
            delta=float(d.get("delta", 0)),
            gamma=float(d.get("gamma", 0)),
            theta=float(d.get("theta", 0)),
            vega=float(d.get("vega", 0)),
        )


@dataclass
class OptionLeg:
    """A single option leg in a position or order."""

    underlying: str
    expiration: str  # ISO date string
    strike: float
    option_type: str  # "call" or "put"
    side: str  # "sell" or "buy"
    quantity: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "expiration": self.expiration,
            "strike": self.strike,
            "option_type": self.option_type,
            "side": self.side,
            "quantity": self.quantity,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OptionLeg:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class BrokerPositionLeg:
    """A raw broker-reported option leg, persisted verbatim for audit and reconciliation.

    This is the floor of truth. It is NEVER read by the optimizer or position manager —
    those consume grouped Position objects. BrokerPositionLeg exists so that, if the
    grouping layer ever drifts or misclassifies, we can always reconstruct exactly what
    the broker thought we owned at sync time.
    """

    leg_id: str  # stable key; e.g. "public:acct_123:AAPL260515P00185000"
    broker: str  # "public", "tastytrade", ...
    account_id: str
    occ_symbol: str
    underlying: str
    expiration: str  # ISO date
    strike: float
    option_type: str  # "call" | "put"
    side: str  # "buy" (long) | "sell" (short) — derived from signed qty
    quantity: int = 1  # absolute contract count
    signed_quantity: float = (
        0.0  # raw value from broker (positive = long, negative = short)
    )
    current_value: float = 0.0  # total dollar value reported by broker
    total_cost: float = 0.0
    unit_cost: float = 0.0
    pnl_pct: float = 0.0
    greeks: Greeks = field(default_factory=Greeks)
    retrieved_at: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_id": self.leg_id,
            "broker": self.broker,
            "account_id": self.account_id,
            "occ_symbol": self.occ_symbol,
            "underlying": self.underlying,
            "expiration": self.expiration,
            "strike": self.strike,
            "option_type": self.option_type,
            "side": self.side,
            "quantity": self.quantity,
            "signed_quantity": self.signed_quantity,
            "current_value": self.current_value,
            "total_cost": self.total_cost,
            "unit_cost": self.unit_cost,
            "pnl_pct": self.pnl_pct,
            "greeks": self.greeks.to_dict(),
            "retrieved_at": self.retrieved_at,
            "raw_payload": self.raw_payload,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BrokerPositionLeg:
        greeks = Greeks.from_dict(d.get("greeks", {}))
        data = {
            k: v
            for k, v in d.items()
            if k not in ("greeks",) and k in cls.__dataclass_fields__
        }
        return cls(greeks=greeks, **data)

    def as_option_leg(self) -> OptionLeg:
        """Project this raw leg as an OptionLeg suitable for grouping logic."""
        return OptionLeg(
            underlying=self.underlying,
            expiration=self.expiration,
            strike=self.strike,
            option_type=self.option_type,
            side=self.side,
            quantity=self.quantity,
        )


@dataclass
class OptionSnapshot:
    """A normalized option-market snapshot used for fixtures and scoring."""

    underlying: str
    timestamp: str
    option_type: str
    strike: float
    expiration: str
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    greeks: Greeks = field(default_factory=Greeks)
    implied_volatility: float = 0.0
    gds_score: float = 0.0
    source: str = "fixture"
    quality_flags: list[str] = field(default_factory=list)

    @property
    def mid(self) -> float:
        if self.bid and self.ask:
            return round((self.bid + self.ask) / 2.0, 4)
        return self.last

    def to_dict(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "timestamp": self.timestamp,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiration": self.expiration,
            "bid": self.bid,
            "ask": self.ask,
            "last": self.last,
            "greeks": self.greeks.to_dict(),
            "implied_volatility": self.implied_volatility,
            "gds_score": self.gds_score,
            "source": self.source,
            "quality_flags": self.quality_flags,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OptionSnapshot":
        data = {
            k: v
            for k, v in d.items()
            if k != "greeks" and k in cls.__dataclass_fields__
        }
        return cls(greeks=Greeks.from_dict(d.get("greeks", {})), **data)


@dataclass
class MarketSnapshot:
    """Underlying market state plus representative option quotes."""

    symbol: str
    timestamp: str
    underlying_price: float
    iv_rank: float = 0.0
    realized_volatility: float = 0.0
    beta_to_spy: float = 1.0
    sector: str = "unknown"
    event_risk: bool = False
    source: str = "fixture"
    option_snapshots: list[OptionSnapshot] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "underlying_price": self.underlying_price,
            "iv_rank": self.iv_rank,
            "realized_volatility": self.realized_volatility,
            "beta_to_spy": self.beta_to_spy,
            "sector": self.sector,
            "event_risk": self.event_risk,
            "source": self.source,
            "option_snapshots": [item.to_dict() for item in self.option_snapshots],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MarketSnapshot":
        items = [
            OptionSnapshot.from_dict(item) for item in d.get("option_snapshots", [])
        ]
        data = {
            k: v
            for k, v in d.items()
            if k != "option_snapshots" and k in cls.__dataclass_fields__
        }
        return cls(option_snapshots=items, **data)


@dataclass
class TradeIdea:
    """A specific trade idea captured from daily content (Module 1 output)."""

    id: str
    date: str  # ISO date
    trader_name: str
    show_name: str
    underlying: str
    strategy_type: str  # maps to StrategyType
    description: str
    legs: list[OptionLeg] = field(default_factory=list)
    expiration: str = ""
    credit_target: float = 0.0
    rationale: str = ""
    confidence: str = "medium"  # high / medium / low
    source_url: str = ""
    source_timestamp: str = ""
    # Richer YouTube-era fields. trader_name stays for back-compat; host is the
    # on-camera person when distinguishable (e.g. "Tom Sosnoff").
    video_id: str = ""
    host: str = ""
    strikes: list[float] = field(default_factory=list)
    extracted_at: str = ""  # ISO-8601 UTC timestamp when the LLM produced the idea
    status: str = (
        IdeaStatus.NEW.value
    )  # new / evaluated / approved / rejected / executed

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "date": self.date,
            "trader_name": self.trader_name,
            "show_name": self.show_name,
            "underlying": self.underlying,
            "strategy_type": self.strategy_type,
            "description": self.description,
            "legs": [l.to_dict() for l in self.legs],
            "expiration": self.expiration,
            "credit_target": self.credit_target,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "source_url": self.source_url,
            "source_timestamp": self.source_timestamp,
            "video_id": self.video_id,
            "host": self.host,
            "strikes": list(self.strikes),
            "extracted_at": self.extracted_at,
            "status": self.status,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TradeIdea:
        legs = [OptionLeg.from_dict(l) for l in d.get("legs", [])]
        data = {
            k: v for k, v in d.items() if k != "legs" and k in cls.__dataclass_fields__
        }
        return cls(legs=legs, **data)


@dataclass
class Position:
    """An open options position in the portfolio.

    A Position represents a **trading unit** (the strategy bundle), not a single broker
    leg. A vertical spread, iron condor, strangle, or naked short put is ONE Position
    with a list of legs. This is the object the optimizer and position manager reason
    about — all risk, diversification, and management decisions happen at this grain.

    Raw broker legs live in a separate `broker_position_legs` table and are never read
    by the trading brain.
    """

    position_id: str
    underlying: str
    strategy_id: str  # links to Strategy.id in strategies.yaml (may be empty for inferred groups)
    legs: list[OptionLeg] = field(default_factory=list)
    open_date: str = ""
    open_credit: float = 0.0
    current_value: float = 0.0
    greeks: Greeks = field(default_factory=Greeks)
    dte_remaining: int = 0
    pnl_pct: float = 0.0
    status: str = PositionStatus.OPEN.value  # open / closed / rolled
    bpr: float = 0.0  # buying power reduction

    # ── Group metadata (added 2026-04: first-class position grouping layer) ──
    group_id: str = (
        ""  # durable anchor: Public orderId for Kamandal trades, deterministic id for inferred groups
    )
    source: str = PositionSource.MANUAL.value
    strategy_type: str = (
        ""  # structural classification (StrategyType.value), distinct from strategy_id
    )
    expirations: list[str] = field(default_factory=list)
    quantity: int = 1  # multiplier — e.g. 2 iron condors share 4 legs at ratio 2
    net_credit: float = 0.0  # net credit/debit received (dollars)
    max_profit: float = 0.0  # 0.0 means undefined / not applicable
    max_loss: float = 0.0  # may exceed bpr for undefined-risk structures
    confidence: str = GroupConfidence.HIGH.value
    management_status: str = ManagementStatus.AUTO.value
    broker: str = ""
    broker_order_id: str = (
        ""  # Public orderId (UUID) for Kamandal-opened multi-leg orders
    )

    @property
    def pnl_dollar(self) -> float:
        return self.open_credit - self.current_value

    @property
    def is_auto_managed(self) -> bool:
        return self.management_status == ManagementStatus.AUTO.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "underlying": self.underlying,
            "strategy_id": self.strategy_id,
            "legs": [l.to_dict() for l in self.legs],
            "open_date": self.open_date,
            "open_credit": self.open_credit,
            "current_value": self.current_value,
            "greeks": self.greeks.to_dict(),
            "dte_remaining": self.dte_remaining,
            "pnl_pct": self.pnl_pct,
            "status": self.status,
            "bpr": self.bpr,
            "group_id": self.group_id,
            "source": self.source,
            "strategy_type": self.strategy_type,
            "expirations": list(self.expirations),
            "quantity": self.quantity,
            "net_credit": self.net_credit,
            "max_profit": self.max_profit,
            "max_loss": self.max_loss,
            "confidence": self.confidence,
            "management_status": self.management_status,
            "broker": self.broker,
            "broker_order_id": self.broker_order_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        legs = [OptionLeg.from_dict(l) for l in d.get("legs", [])]
        greeks = Greeks.from_dict(d.get("greeks", {}))
        data = {
            k: v
            for k, v in d.items()
            if k not in ("legs", "greeks") and k in cls.__dataclass_fields__
        }
        return cls(legs=legs, greeks=greeks, **data)


@dataclass
class PortfolioSnapshot:
    """Point-in-time snapshot of portfolio state."""

    timestamp: str = ""
    net_liquidation_value: float = 0.0
    greeks: Greeks = field(default_factory=Greeks)
    beta_weighted_delta: float = 0.0  # SPY-equivalent
    bpr_used: float = 0.0
    bpr_used_pct: float = 0.0
    theta_as_pct_nlv: float = 0.0
    gamma_theta_ratio: float = 0.0
    position_count: int = 0
    positions: list[Position] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "nlv": self.net_liquidation_value,
            "greeks": self.greeks.to_dict(),
            "beta_weighted_delta": self.beta_weighted_delta,
            "bpr_used": self.bpr_used,
            "bpr_used_pct": self.bpr_used_pct,
            "theta_as_pct_nlv": self.theta_as_pct_nlv,
            "gamma_theta_ratio": self.gamma_theta_ratio,
            "position_count": self.position_count,
            "positions": [position.to_dict() for position in self.positions],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PortfolioSnapshot":
        positions = [Position.from_dict(item) for item in d.get("positions", [])]
        data = {
            "timestamp": d.get("timestamp", ""),
            "net_liquidation_value": float(
                d.get("net_liquidation_value", d.get("nlv", 0.0))
            ),
            "greeks": Greeks.from_dict(d.get("greeks", {})),
            "beta_weighted_delta": float(d.get("beta_weighted_delta", 0.0)),
            "bpr_used": float(d.get("bpr_used", 0.0)),
            "bpr_used_pct": float(d.get("bpr_used_pct", 0.0)),
            "theta_as_pct_nlv": float(d.get("theta_as_pct_nlv", 0.0)),
            "gamma_theta_ratio": float(d.get("gamma_theta_ratio", 0.0)),
            "position_count": int(d.get("position_count", len(positions))),
            "positions": positions,
        }
        return cls(**data)


@dataclass
class Order:
    """A trade order to be submitted or logged."""

    order_id: str
    action: TradeAction
    underlying: str
    strategy_id: str
    legs: list[OptionLeg] = field(default_factory=list)
    quantity: int = 1
    limit_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float = 0.0
    commission: float = 0.0
    filled_at: str = ""
    dry_run: bool = False
    optimizer_score: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "action": self.action.value,
            "underlying": self.underlying,
            "strategy_id": self.strategy_id,
            "legs": [l.to_dict() for l in self.legs],
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "status": self.status.value,
            "fill_price": self.fill_price,
            "commission": self.commission,
            "filled_at": self.filled_at,
            "dry_run": self.dry_run,
            "optimizer_score": self.optimizer_score,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Order":
        return cls(
            order_id=d["order_id"],
            action=TradeAction(d.get("action", TradeAction.OPEN.value)),
            underlying=d.get("underlying", ""),
            strategy_id=d.get("strategy_id", ""),
            legs=[OptionLeg.from_dict(item) for item in d.get("legs", [])],
            quantity=int(d.get("quantity", 1)),
            limit_price=float(d.get("limit_price", 0.0)),
            status=OrderStatus(d.get("status", OrderStatus.PENDING.value)),
            fill_price=float(d.get("fill_price", 0.0)),
            commission=float(d.get("commission", 0.0)),
            filled_at=d.get("filled_at", ""),
            dry_run=bool(d.get("dry_run", False)),
            optimizer_score=float(d.get("optimizer_score", 0.0)),
            notes=d.get("notes", ""),
        )


@dataclass
class OptimizerResult:
    """Output of the portfolio optimizer for a single candidate combination."""

    combo_ids: list[str]  # TradeIdea IDs in this combo
    score: float = 0.0
    delta_before: float = 0.0
    delta_after: float = 0.0
    gamma_before: float = 0.0
    gamma_after: float = 0.0
    theta_before: float = 0.0
    theta_after: float = 0.0
    diversification_score: float = 0.0
    passes_constraints: bool = True
    constraint_violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "combo_ids": self.combo_ids,
            "score": self.score,
            "delta_before": self.delta_before,
            "delta_after": self.delta_after,
            "gamma_before": self.gamma_before,
            "gamma_after": self.gamma_after,
            "theta_before": self.theta_before,
            "theta_after": self.theta_after,
            "diversification_score": self.diversification_score,
            "passes_constraints": self.passes_constraints,
            "constraint_violations": self.constraint_violations,
        }


@dataclass
class BacktestResult:
    """Result of backtesting a single strategy."""

    strategy_id: str
    test_date: str
    period_start: str
    period_end: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_pnl_per_trade: float = 0.0
    total_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    theta_efficiency: float = 0.0  # actual PnL / theoretical theta
    approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RegimePolicy:
    """Policy controls applied to candidate selection by market regime."""

    regime: MarketRegime
    prefer_structures: list[str] = field(default_factory=list)
    avoid_structures: list[str] = field(default_factory=list)
    allow_undefined_risk: bool = True
    min_iv_rank: float | None = None
    max_iv_rank: float | None = None
    target_delta_bias: float = 0.0
    reject_event_risk: bool = True
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegimePolicy":
        regime = d.get("regime", MarketRegime.UNKNOWN.value)
        return cls(
            regime=MarketRegime(regime),
            prefer_structures=list(d.get("prefer_structures", [])),
            avoid_structures=list(d.get("avoid_structures", [])),
            allow_undefined_risk=bool(d.get("allow_undefined_risk", True)),
            min_iv_rank=d.get("min_iv_rank"),
            max_iv_rank=d.get("max_iv_rank"),
            target_delta_bias=float(d.get("target_delta_bias", 0.0)),
            reject_event_risk=bool(d.get("reject_event_risk", True)),
            notes=list(d.get("notes", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["regime"] = self.regime.value
        return data


@dataclass
class CandidatePosition:
    """A scored, strategy-aligned position candidate derived from a trade idea."""

    idea_id: str
    strategy_id: str
    underlying: str
    strategy_type: str
    expiration: str
    estimated_credit: float
    estimated_bpr: float
    estimated_greeks: Greeks
    iv_rank: float = 0.0
    sector: str = "unknown"
    event_risk: bool = False
    rationale: str = ""
    legs: list[OptionLeg] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "idea_id": self.idea_id,
            "strategy_id": self.strategy_id,
            "underlying": self.underlying,
            "strategy_type": self.strategy_type,
            "expiration": self.expiration,
            "estimated_credit": self.estimated_credit,
            "estimated_bpr": self.estimated_bpr,
            "estimated_greeks": self.estimated_greeks.to_dict(),
            "iv_rank": self.iv_rank,
            "sector": self.sector,
            "event_risk": self.event_risk,
            "rationale": self.rationale,
            "legs": [leg.to_dict() for leg in self.legs],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CandidatePosition":
        return cls(
            idea_id=d.get("idea_id", ""),
            strategy_id=d.get("strategy_id", ""),
            underlying=d.get("underlying", ""),
            strategy_type=d.get("strategy_type", ""),
            expiration=d.get("expiration", ""),
            estimated_credit=float(d.get("estimated_credit", 0.0)),
            estimated_bpr=float(d.get("estimated_bpr", 0.0)),
            estimated_greeks=Greeks.from_dict(d.get("estimated_greeks", {})),
            iv_rank=float(d.get("iv_rank", 0.0)),
            sector=d.get("sector", "unknown"),
            event_risk=bool(d.get("event_risk", False)),
            rationale=d.get("rationale", ""),
            legs=[OptionLeg.from_dict(item) for item in d.get("legs", [])],
        )


@dataclass
class ConstraintCheck:
    """Single hard-constraint evaluation result."""

    name: str
    passed: bool
    actual: float
    min_value: float | None = None
    max_value: float | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComboScore:
    """Composite scoring record for a candidate combination."""

    combo_ids: list[str]
    candidate_positions: list[CandidatePosition]
    total_score: float
    component_scores: dict[str, float] = field(default_factory=dict)
    projected_portfolio: PortfolioSnapshot = field(default_factory=PortfolioSnapshot)
    constraint_checks: list[ConstraintCheck] = field(default_factory=list)
    regime: str = MarketRegime.UNKNOWN.value
    notes: list[str] = field(default_factory=list)

    @property
    def passes_constraints(self) -> bool:
        return all(check.passed for check in self.constraint_checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "combo_ids": self.combo_ids,
            "candidate_positions": [
                item.to_dict() for item in self.candidate_positions
            ],
            "total_score": self.total_score,
            "component_scores": self.component_scores,
            "projected_portfolio": self.projected_portfolio.to_dict(),
            "constraint_checks": [item.to_dict() for item in self.constraint_checks],
            "regime": self.regime,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ComboScore":
        return cls(
            combo_ids=list(d.get("combo_ids", [])),
            candidate_positions=[
                CandidatePosition.from_dict(item)
                for item in d.get("candidate_positions", [])
            ],
            total_score=float(d.get("total_score", 0.0)),
            component_scores=dict(d.get("component_scores", {})),
            projected_portfolio=PortfolioSnapshot.from_dict(
                d.get("projected_portfolio", {})
            ),
            constraint_checks=[
                ConstraintCheck(**item) for item in d.get("constraint_checks", [])
            ],
            regime=d.get("regime", MarketRegime.UNKNOWN.value),
            notes=list(d.get("notes", [])),
        )


@dataclass
class TradePlan:
    """Approved or rejected optimizer decision package."""

    plan_id: str
    created_at: str
    decision: PlanDecision
    regime: str
    selected_combo_ids: list[str] = field(default_factory=list)
    ranked_combos: list[ComboScore] = field(default_factory=list)
    candidate_positions: list[CandidatePosition] = field(default_factory=list)
    reasoning: str = ""
    risk_flags: list[str] = field(default_factory=list)
    status: str = OrderStatus.PENDING.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "decision": self.decision.value,
            "regime": self.regime,
            "selected_combo_ids": self.selected_combo_ids,
            "ranked_combos": [item.to_dict() for item in self.ranked_combos],
            "candidate_positions": [
                item.to_dict() for item in self.candidate_positions
            ],
            "reasoning": self.reasoning,
            "risk_flags": self.risk_flags,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TradePlan":
        decision = d.get("decision", PlanDecision.NO_TRADE.value)
        return cls(
            plan_id=d.get("plan_id", ""),
            created_at=d.get("created_at", ""),
            decision=(
                decision
                if isinstance(decision, PlanDecision)
                else PlanDecision(decision)
            ),
            regime=d.get("regime", MarketRegime.UNKNOWN.value),
            selected_combo_ids=list(d.get("selected_combo_ids", [])),
            ranked_combos=[
                ComboScore.from_dict(item) for item in d.get("ranked_combos", [])
            ],
            candidate_positions=[
                CandidatePosition.from_dict(item)
                for item in d.get("candidate_positions", [])
            ],
            reasoning=d.get("reasoning", ""),
            risk_flags=list(d.get("risk_flags", [])),
            status=d.get("status", OrderStatus.PENDING.value),
        )


@dataclass
class PendingOrder:
    """Dry-run or pending execution artifact emitted by the executor."""

    pending_order_id: str
    plan_id: str
    created_at: str
    action: TradeAction
    status: str
    underlying: str
    strategy_id: str
    quantity: int
    target_price: float
    estimated_credit: float
    estimated_bpr: float
    greeks_impact: Greeks
    idea_id: str = ""
    notes: str = ""
    legs: list[OptionLeg] = field(default_factory=list)
    broker: str = ""
    execution_mode: str = ""
    submitted_at: str = ""
    broker_order_id: str = ""
    broker_status: str = ""
    broker_payload: dict[str, Any] = field(default_factory=dict)
    broker_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_order_id": self.pending_order_id,
            "plan_id": self.plan_id,
            "idea_id": self.idea_id,
            "created_at": self.created_at,
            "action": self.action.value,
            "status": self.status,
            "underlying": self.underlying,
            "strategy_id": self.strategy_id,
            "quantity": self.quantity,
            "target_price": self.target_price,
            "estimated_credit": self.estimated_credit,
            "estimated_bpr": self.estimated_bpr,
            "greeks_impact": self.greeks_impact.to_dict(),
            "notes": self.notes,
            "legs": [leg.to_dict() for leg in self.legs],
            "broker": self.broker,
            "execution_mode": self.execution_mode,
            "submitted_at": self.submitted_at,
            "broker_order_id": self.broker_order_id,
            "broker_status": self.broker_status,
            "broker_payload": self.broker_payload,
            "broker_response": self.broker_response,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PendingOrder":
        action = d.get("action", TradeAction.OPEN.value)
        return cls(
            pending_order_id=d.get("pending_order_id", ""),
            plan_id=d.get("plan_id", ""),
            idea_id=d.get("idea_id", ""),
            created_at=d.get("created_at", ""),
            action=action if isinstance(action, TradeAction) else TradeAction(action),
            status=d.get("status", OrderStatus.PENDING.value),
            underlying=d.get("underlying", ""),
            strategy_id=d.get("strategy_id", ""),
            quantity=int(d.get("quantity", 1)),
            target_price=float(d.get("target_price", 0.0)),
            estimated_credit=float(d.get("estimated_credit", 0.0)),
            estimated_bpr=float(d.get("estimated_bpr", 0.0)),
            greeks_impact=Greeks.from_dict(d.get("greeks_impact", {})),
            notes=d.get("notes", ""),
            legs=[OptionLeg.from_dict(item) for item in d.get("legs", [])],
            broker=d.get("broker", ""),
            execution_mode=d.get("execution_mode", ""),
            submitted_at=d.get("submitted_at", ""),
            broker_order_id=d.get("broker_order_id", ""),
            broker_status=d.get("broker_status", ""),
            broker_payload=dict(d.get("broker_payload", {})),
            broker_response=dict(d.get("broker_response", {})),
        )


@dataclass
class ReplayTrade:
    """Normalized historical replay trade imported from fixtures."""

    trade_id: str
    underlying: str
    symbol: str
    profit_pct: float
    is_winner: bool
    entry_price: float = 0.0
    exit_price: float = 0.0
    entry_greeks: Greeks = field(default_factory=Greeks)
    terminal_greeks: Greeks = field(default_factory=Greeks)
    theta_capture_proxy: float = 0.0
    days_in_trade: float = 21.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "underlying": self.underlying,
            "symbol": self.symbol,
            "profit_pct": self.profit_pct,
            "is_winner": self.is_winner,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "entry_greeks": self.entry_greeks.to_dict(),
            "terminal_greeks": self.terminal_greeks.to_dict(),
            "theta_capture_proxy": self.theta_capture_proxy,
            "days_in_trade": self.days_in_trade,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReplayTrade":
        return cls(
            trade_id=d.get("trade_id", ""),
            underlying=d.get("underlying", d.get("symbol", "")[:3].upper()),
            symbol=d.get("symbol", ""),
            profit_pct=float(d.get("profit_pct", 0.0)),
            is_winner=bool(d.get("is_winner", False)),
            entry_price=float(d.get("entry_price", 0.0)),
            exit_price=float(d.get("exit_price", 0.0)),
            entry_greeks=Greeks.from_dict(d.get("entry_greeks", {})),
            terminal_greeks=Greeks.from_dict(d.get("terminal_greeks", {})),
            theta_capture_proxy=float(d.get("theta_capture_proxy", 0.0)),
            days_in_trade=float(d.get("days_in_trade", 21.0)),
        )


@dataclass
class ReplayResult:
    """Replay/backtest summary for gating strategy readiness."""

    strategy_id: str
    evaluated_at: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_pnl_per_trade: float
    total_pnl: float
    max_drawdown_pct: float
    avg_days_in_trade: float
    theta_capture_proxy: float
    status: BacktestStatus = BacktestStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class RawSourceDocument:
    """Normalized raw source content captured before idea extraction."""

    document_id: str
    source_type: str
    source_name: str
    title: str
    author: str = ""
    published_at: str = ""
    url: str = ""
    text: str = ""
    summary: str = ""
    fingerprint: str = ""
    status: str = RawContentStatus.NEW.value
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RawSourceDocument":
        data = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        if "metadata" not in data or data["metadata"] is None:
            data["metadata"] = {}
        return cls(**data)


def serialize_value(value: Any) -> Any:
    """Recursively serialize enums and dataclasses for storage and JSON output."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: serialize_value(val) for key, val in asdict(value).items()}
    if isinstance(value, list):
        return [serialize_value(item) for item in value]
    if isinstance(value, tuple):
        return [serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_value(val) for key, val in value.items()}
    return value
