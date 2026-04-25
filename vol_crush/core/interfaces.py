"""Shared runtime interfaces for Vol Crush services."""

from __future__ import annotations

from typing import Protocol

from vol_crush.core.models import (
    BacktestResult,
    BrokerPositionLeg,
    IdeaCandidate,
    MarketRegime,
    MarketSnapshot,
    PendingOrder,
    PlaybookInsight,
    PolicyProposal,
    PortfolioSnapshot,
    Position,
    RawSourceDocument,
    ReflectionSummary,
    RegimePolicy,
    ReplayTrade,
    ShadowFill,
    SourceIntelligence,
    SourceObservation,
    TradeIdea,
    TradePlan,
)


class StorageBackend(Protocol):
    """Persistence boundary for Vol Crush operational state."""

    def save_trade_ideas(self, ideas: list[TradeIdea]) -> None: ...

    def list_trade_ideas(self, status: str | None = None) -> list[TradeIdea]: ...

    def save_raw_documents(self, documents: list[RawSourceDocument]) -> None: ...

    def list_raw_documents(
        self,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[RawSourceDocument]: ...

    def save_positions(self, positions: list[Position]) -> None: ...

    def list_positions(self, status: str | None = None) -> list[Position]: ...

    def save_shadow_positions(self, positions: list[Position]) -> None: ...

    def list_shadow_positions(self, status: str | None = None) -> list[Position]: ...

    def save_shadow_fills(self, fills: list[ShadowFill]) -> None: ...

    def list_shadow_fills(self) -> list[ShadowFill]: ...

    def save_shadow_portfolio_snapshot(self, snapshot: PortfolioSnapshot) -> None: ...

    def get_latest_shadow_portfolio_snapshot(self) -> PortfolioSnapshot | None: ...

    def save_source_observations(
        self, observations: list[SourceObservation]
    ) -> None: ...

    def list_source_observations(self) -> list[SourceObservation]: ...

    def save_idea_candidates(self, candidates: list[IdeaCandidate]) -> None: ...

    def list_idea_candidates(self) -> list[IdeaCandidate]: ...

    def save_playbook_insights(self, insights: list[PlaybookInsight]) -> None: ...

    def list_playbook_insights(self) -> list[PlaybookInsight]: ...

    def save_source_intelligence(self, items: list[SourceIntelligence]) -> None: ...

    def list_source_intelligence(self) -> list[SourceIntelligence]: ...

    def save_policy_proposals(self, proposals: list[PolicyProposal]) -> None: ...

    def list_policy_proposals(self) -> list[PolicyProposal]: ...

    def save_reflection_summaries(self, summaries: list[ReflectionSummary]) -> None: ...

    def list_reflection_summaries(self) -> list[ReflectionSummary]: ...

    def replace_broker_legs(self, broker: str, legs: list[BrokerPositionLeg]) -> None:
        """Wipe all legs for the given broker and write the new set.

        Broker portfolio pulls are complete snapshots, so we don't upsert — we replace.
        Otherwise closed positions would linger in the raw-leg audit floor.
        """
        ...

    def list_broker_legs(
        self, broker: str | None = None
    ) -> list[BrokerPositionLeg]: ...

    def save_portfolio_snapshot(self, snapshot: PortfolioSnapshot) -> None: ...

    def get_latest_portfolio_snapshot(self) -> PortfolioSnapshot | None: ...

    def save_trade_plan(self, plan: TradePlan) -> None: ...

    def list_trade_plans(self) -> list[TradePlan]: ...

    def save_pending_orders(self, orders: list[PendingOrder]) -> None: ...

    def list_pending_orders(self, status: str | None = None) -> list[PendingOrder]: ...

    def save_backtest_result(self, result: BacktestResult) -> None: ...

    def list_backtest_results(self) -> list[BacktestResult]: ...

    def save_fixture_payload(self, payload: dict) -> None: ...

    def load_fixture_payload(self) -> dict: ...

    def save_replay_trades(self, trades: list[ReplayTrade]) -> None: ...

    def list_replay_trades(self) -> list[ReplayTrade]: ...


class MarketDataProvider(Protocol):
    """Market data boundary used by optimizer and replay."""

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot | None: ...

    def list_market_snapshots(self) -> list[MarketSnapshot]: ...


class BrokerAdapter(Protocol):
    """Placeholder broker boundary for future live execution."""

    def execution_mode(self) -> str: ...

    def submit_pending_orders(
        self, orders: list[PendingOrder]
    ) -> list[PendingOrder]: ...


class RegimeEvaluator(Protocol):
    """Policy component that maps current market state into a regime."""

    def determine_regime(self, snapshots: list[MarketSnapshot]) -> MarketRegime: ...

    def get_policy(self, regime: MarketRegime) -> RegimePolicy: ...
