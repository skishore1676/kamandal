"""Tests for local storage and fixture import/provider behavior."""

import json
import sqlite3

from vol_crush.core.models import Greeks, PortfolioSnapshot, ReplayTrade, TradeIdea
from vol_crush.integrations.fixtures import (
    FixtureMarketDataProvider,
    build_fixture_payload,
    write_fixture_artifacts,
)
from vol_crush.integrations.storage import LocalStore


def test_local_store_round_trip(tmp_path):
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db",
        audit_dir=tmp_path / "audit",
    )
    idea = TradeIdea(
        id="idea_1",
        date="2026-04-02",
        trader_name="Tom",
        show_name="Market Measures",
        underlying="SPY",
        strategy_type="short_put",
        description="Sell the put",
    )
    snapshot = PortfolioSnapshot(
        timestamp="2026-04-02T14:00:00+00:00",
        net_liquidation_value=100000.0,
        greeks=Greeks(delta=1.2, gamma=0.1, theta=120.0, vega=8.0),
        beta_weighted_delta=1.2,
        bpr_used=12000.0,
        bpr_used_pct=12.0,
        theta_as_pct_nlv=0.12,
        gamma_theta_ratio=0.0008,
        position_count=0,
    )

    store.save_trade_ideas([idea])
    store.save_portfolio_snapshot(snapshot)

    loaded_ideas = store.list_trade_ideas()
    loaded_snapshot = store.get_latest_portfolio_snapshot()

    assert len(loaded_ideas) == 1
    assert loaded_ideas[0].underlying == "SPY"
    assert loaded_snapshot is not None
    assert loaded_snapshot.net_liquidation_value == 100000.0
    assert (tmp_path / "audit" / "trade_ideas.json").exists()


def test_fixture_builder_imports_gds_and_replay(tmp_path):
    db_path = tmp_path / "gds_history.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE market_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            stock_price REAL,
            morning_price REAL,
            call_symbol TEXT,
            call_strike REAL,
            call_bid REAL,
            call_ask REAL,
            call_last REAL,
            call_delta REAL,
            call_gamma REAL,
            call_theta REAL,
            call_vega REAL,
            call_iv REAL,
            call_gds REAL,
            put_symbol TEXT,
            put_strike REAL,
            put_bid REAL,
            put_ask REAL,
            put_last REAL,
            put_delta REAL,
            put_gamma REAL,
            put_theta REAL,
            put_vega REAL,
            put_iv REAL,
            put_gds REAL
        );
        """)
    conn.execute(
        """
        INSERT INTO market_observations (
            timestamp, symbol, stock_price, call_symbol, call_strike, call_bid, call_ask, call_last,
            call_delta, call_gamma, call_theta, call_vega, call_iv, call_gds, put_symbol, put_strike,
            put_bid, put_ask, put_last, put_delta, put_gamma, put_theta, put_vega, put_iv, put_gds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-04-01T14:00:00+00:00",
            "SPY",
            520.0,
            "SPY260516C00525000",
            525.0,
            2.1,
            2.3,
            2.2,
            0.32,
            0.05,
            0.08,
            0.12,
            0.24,
            0.1,
            "SPY260516P00515000",
            515.0,
            1.9,
            2.1,
            2.0,
            -0.28,
            0.04,
            0.09,
            0.11,
            0.26,
            0.08,
        ),
    )
    conn.commit()
    conn.close()

    analysis_path = tmp_path / "replay.json"
    analysis_path.write_text(
        json.dumps(
            [
                {
                    "trade_id": "trade_1",
                    "symbol": "SPY250908P00648000",
                    "profit_pct": 12.5,
                    "is_winner": True,
                    "entry_price": 1.1,
                    "exit_price": 0.9,
                    "entry_greeks": {
                        "delta": -0.25,
                        "gamma": 0.04,
                        "theta": 0.08,
                        "vega": 0.1,
                    },
                    "terminal_greeks": {
                        "delta": -0.2,
                        "gamma": 0.03,
                        "theta": 0.06,
                        "vega": 0.08,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    config = {
        "data_sources": {
            "fixtures": {
                "import_gds_history_db": str(db_path),
                "import_gds_analysis_json": str(analysis_path),
                "bundle_path": str(tmp_path / "fixtures" / "fixture_bundle.json"),
                "replay_path": str(tmp_path / "fixtures" / "replay_trades.json"),
                "enable_public_seed_fetch": False,
            }
        }
    }

    payload, replay_trades = build_fixture_payload(config)
    bundle_path, replay_path = write_fixture_artifacts(config, payload, replay_trades)
    provider = FixtureMarketDataProvider(bundle_path)

    assert payload["market_snapshots"]
    assert replay_trades and isinstance(replay_trades[0], ReplayTrade)
    snapshot = provider.get_market_snapshot("SPY")
    assert snapshot is not None
    assert snapshot.underlying_price == 520.0
    assert snapshot.option_snapshots[0].expiration == "2026-05-16"
    assert snapshot.option_snapshots[1].expiration == "2026-05-16"
    assert bundle_path.exists()
    assert replay_path.exists()
