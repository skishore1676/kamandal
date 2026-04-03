"""Local-first persistence backend for Vol Crush."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from vol_crush.core.config import get_data_dir, get_project_root
from vol_crush.core.interfaces import StorageBackend
from vol_crush.core.models import (
    BacktestResult,
    PendingOrder,
    PortfolioSnapshot,
    Position,
    RawSourceDocument,
    ReplayTrade,
    TradeIdea,
    TradePlan,
    serialize_value,
)


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return get_project_root() / path


class LocalStore(StorageBackend):
    """SQLite-backed operational store with JSON audit mirrors."""

    def __init__(self, sqlite_path: str | Path | None = None, audit_dir: str | Path | None = None):
        data_dir = get_data_dir()
        self.sqlite_path = _resolve_path(str(sqlite_path or data_dir / "vol_crush.db"))
        self.audit_dir = _resolve_path(str(audit_dir or data_dir / "audit"))
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trade_ideas (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_documents (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_plans (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_orders (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_trades (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fixtures (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                """
            )

    def _upsert_many(self, table: str, rows: list[tuple[str, str, str]]) -> None:
        with self._connect() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)",
                rows,
            )

    def _write_audit(self, name: str, payload: Any) -> None:
        path = self.audit_dir / f"{name}.json"
        path.write_text(json.dumps(serialize_value(payload), indent=2), encoding="utf-8")

    def save_trade_ideas(self, ideas: list[TradeIdea]) -> None:
        rows = [(idea.id, idea.status, json.dumps(idea.to_dict())) for idea in ideas]
        self._upsert_many("trade_ideas", rows)
        self._write_audit("trade_ideas", [idea.to_dict() for idea in self.list_trade_ideas()])

    def list_trade_ideas(self, status: str | None = None) -> list[TradeIdea]:
        query = "SELECT payload FROM trade_ideas"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [TradeIdea.from_dict(json.loads(row["payload"])) for row in rows]

    def save_raw_documents(self, documents: list[RawSourceDocument]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO raw_documents VALUES (?, ?, ?, ?)",
                [
                    (
                        document.document_id,
                        document.source_type,
                        document.status,
                        json.dumps(document.to_dict()),
                    )
                    for document in documents
                ],
            )
        self._write_audit(
            "raw_documents",
            [document.to_dict() for document in self.list_raw_documents()],
        )

    def list_raw_documents(
        self,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[RawSourceDocument]:
        query = "SELECT payload FROM raw_documents"
        conditions = []
        params: list[Any] = []
        if source_type:
            conditions.append("source_type = ?")
            params.append(source_type)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [RawSourceDocument.from_dict(json.loads(row["payload"])) for row in rows]

    def save_positions(self, positions: list[Position]) -> None:
        rows = [(position.position_id, position.status, json.dumps(position.to_dict())) for position in positions]
        self._upsert_many("positions", rows)
        self._write_audit("positions", [position.to_dict() for position in self.list_positions()])

    def list_positions(self, status: str | None = None) -> list[Position]:
        query = "SELECT payload FROM positions"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Position.from_dict(json.loads(row["payload"])) for row in rows]

    def save_portfolio_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        payload = json.dumps(snapshot.to_dict())
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO portfolio_snapshots VALUES (?, ?)",
                (snapshot.timestamp or "latest", payload),
            )
        items = [item.to_dict() for item in self.list_portfolio_snapshots()]
        self._write_audit("portfolio_snapshots", items)

    def list_portfolio_snapshots(self) -> list[PortfolioSnapshot]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM portfolio_snapshots ORDER BY id").fetchall()
        return [PortfolioSnapshot.from_dict(json.loads(row["payload"])) for row in rows]

    def get_latest_portfolio_snapshot(self) -> PortfolioSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return PortfolioSnapshot.from_dict(json.loads(row["payload"]))

    def save_trade_plan(self, plan: TradePlan) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO trade_plans VALUES (?, ?, ?)",
                (plan.plan_id, plan.status, json.dumps(plan.to_dict())),
            )
        self._write_audit("trade_plans", [item.to_dict() for item in self.list_trade_plans()])

    def list_trade_plans(self) -> list[TradePlan]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM trade_plans ORDER BY id").fetchall()
        return [TradePlan.from_dict(json.loads(row["payload"])) for row in rows]

    def save_pending_orders(self, orders: list[PendingOrder]) -> None:
        rows = [
            (order.pending_order_id, order.status, json.dumps(order.to_dict()))
            for order in orders
        ]
        self._upsert_many("pending_orders", rows)
        self._write_audit("pending_orders", [item.to_dict() for item in self.list_pending_orders()])

    def list_pending_orders(self, status: str | None = None) -> list[PendingOrder]:
        query = "SELECT payload FROM pending_orders"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [PendingOrder.from_dict(json.loads(row["payload"])) for row in rows]

    def save_backtest_result(self, result: BacktestResult) -> None:
        payload = result.to_dict()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO backtest_results VALUES (?, ?, ?)",
                (result.strategy_id, "approved" if result.approved else "pending", json.dumps(payload)),
            )
        self._write_audit("backtest_results", [item.to_dict() for item in self.list_backtest_results()])

    def list_backtest_results(self) -> list[BacktestResult]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM backtest_results ORDER BY id").fetchall()
        return [BacktestResult(**json.loads(row["payload"])) for row in rows]

    def save_fixture_payload(self, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fixtures VALUES (?, ?)",
                ("current", json.dumps(payload)),
            )
        self._write_audit("fixtures", payload)

    def load_fixture_payload(self) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM fixtures WHERE id = ?", ("current",)).fetchone()
        if row is None:
            return {}
        return json.loads(row["payload"])

    def save_replay_trades(self, trades: list[ReplayTrade]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO replay_trades VALUES (?, ?)",
                [(trade.trade_id, json.dumps(trade.to_dict())) for trade in trades],
            )
        self._write_audit("replay_trades", [trade.to_dict() for trade in self.list_replay_trades()])

    def list_replay_trades(self) -> list[ReplayTrade]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM replay_trades ORDER BY id").fetchall()
        return [ReplayTrade.from_dict(json.loads(row["payload"])) for row in rows]


def build_local_store(config: dict[str, Any]) -> LocalStore:
    """Construct the default local storage backend from config."""
    local = config.get("storage", {}).get("local", {})
    return LocalStore(
        sqlite_path=local.get("sqlite_path"),
        audit_dir=local.get("audit_dir"),
    )
