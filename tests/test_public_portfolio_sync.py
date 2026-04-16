from __future__ import annotations

from vol_crush.core.models import (
    Greeks,
    ManagementStatus,
    OptionLeg,
    PendingOrder,
    PositionSource,
    StrategyType,
    TradeAction,
)
from vol_crush.integrations.public_broker import parse_occ_symbol
from vol_crush.integrations.storage import LocalStore
from vol_crush.portfolio_sync.service import sync_public_portfolio


class FakePublicSyncAdapter:
    """Reports a bull call debit spread on AAPL: long 200C, short 210C, same expiry."""

    def get_portfolio(self) -> dict:
        return {
            "accountId": "acct_123",
            "buyingPower": {
                "cashOnlyBuyingPower": "5000.00",
                "buyingPower": "10000.00",
                "optionsBuyingPower": "10000.00",
            },
            "equity": [
                {"type": "CASH", "value": "4000.00", "percentageOfPortfolio": "40.0"},
                {
                    "type": "OPTIONS_LONG",
                    "value": "900.00",
                    "percentageOfPortfolio": "9.0",
                },
                {
                    "type": "OPTIONS_SHORT",
                    "value": "-200.00",
                    "percentageOfPortfolio": "-2.0",
                },
            ],
            "positions": [
                {
                    "instrument": {
                        "symbol": "AAPL260515C00200000",
                        "type": "OPTION",
                    },
                    "quantity": "1.0",
                    "currentValue": "900.00",
                    "costBasis": {
                        "totalCost": "850.00",
                        "unitCost": "850.00",
                        "gainPercentage": "5.88",
                    },
                },
                {
                    "instrument": {
                        "symbol": "AAPL260515C00210000",
                        "type": "OPTION",
                    },
                    "quantity": "-1.0",
                    "currentValue": "-200.00",
                    "costBasis": {
                        "totalCost": "-150.00",
                        "unitCost": "-150.00",
                        "gainPercentage": "-33.33",
                    },
                },
            ],
        }

    def get_primary_account_id(self) -> str:
        return "acct_123"

    def get_option_greeks_batch(self, option_symbols: list[str]) -> dict[str, dict]:
        assert sorted(option_symbols) == [
            "AAPL260515C00200000",
            "AAPL260515C00210000",
        ]
        return {
            "AAPL260515C00200000": {
                "delta": "0.40",
                "gamma": "0.03",
                "theta": "-0.05",
                "vega": "0.08",
            },
            "AAPL260515C00210000": {
                "delta": "0.20",
                "gamma": "0.01",
                "theta": "-0.02",
                "vega": "0.03",
            },
        }


def test_parse_occ_symbol() -> None:
    parsed = parse_occ_symbol("AAPL260515C00200000")
    assert parsed["underlying"] == "AAPL"
    assert parsed["expiration"] == "2026-05-15"
    assert parsed["option_type"] == "call"
    assert parsed["strike"] == 200.0


def test_sync_public_portfolio_groups_debit_call_spread(tmp_path) -> None:
    """A long 200 call + short 210 call should become ONE call_spread group, not two positions."""
    config = {
        "storage": {
            "local": {
                "sqlite_path": str(tmp_path / "kamandal.db"),
                "audit_dir": str(tmp_path / "audit"),
            }
        }
    }
    store = LocalStore(
        sqlite_path=tmp_path / "kamandal.db", audit_dir=tmp_path / "audit"
    )

    snapshot = sync_public_portfolio(
        config, store=store, adapter=FakePublicSyncAdapter()
    )

    # Grouped view
    positions = store.list_positions()
    assert len(positions) == 1, "Two call legs must group into exactly one call spread"
    group = positions[0]
    assert group.strategy_type == StrategyType.CALL_SPREAD.value
    assert group.source == PositionSource.PUBLIC_INFERRED.value
    assert group.management_status == ManagementStatus.AUTO.value
    assert group.underlying == "AAPL"
    assert len(group.legs) == 2

    # Position count in the snapshot now reflects groups, not legs
    assert snapshot.position_count == 1
    assert snapshot.net_liquidation_value == 4700.0

    # Aggregate greeks still sum correctly across the two legs
    assert round(snapshot.greeks.delta, 4) == 0.2
    assert round(snapshot.greeks.gamma, 4) == 0.02
    assert round(snapshot.greeks.theta, 4) == -0.03
    assert round(snapshot.greeks.vega, 4) == 0.05

    # Raw legs are preserved in the audit floor
    raw_legs = store.list_broker_legs(broker="public")
    assert len(raw_legs) == 2
    assert {leg.occ_symbol for leg in raw_legs} == {
        "AAPL260515C00200000",
        "AAPL260515C00210000",
    }


def test_sync_public_portfolio_rehydrates_kamandal_opened_group(tmp_path) -> None:
    """A pending order with a stamped broker_order_id matching the live legs should
    cause the sync to produce a source=kamandal_order Position carrying the anchor."""
    config = {
        "storage": {
            "local": {
                "sqlite_path": str(tmp_path / "kamandal.db"),
                "audit_dir": str(tmp_path / "audit"),
            }
        }
    }
    store = LocalStore(
        sqlite_path=tmp_path / "kamandal.db", audit_dir=tmp_path / "audit"
    )

    # Seed a pending order whose legs match the fake adapter's debit call spread.
    known = PendingOrder(
        pending_order_id="pending_live_1",
        plan_id="plan_1",
        created_at="2026-04-03T12:00:00Z",
        action=TradeAction.OPEN,
        status="pending",
        underlying="AAPL",
        strategy_id="aapl_debit_spread",
        quantity=1,
        target_price=7.00,
        estimated_credit=-700.0,
        estimated_bpr=700.0,
        greeks_impact=Greeks(),
        legs=[
            OptionLeg("AAPL", "2026-05-15", 200.0, "call", "buy"),
            OptionLeg("AAPL", "2026-05-15", 210.0, "call", "sell"),
        ],
        broker="public",
        broker_order_id="kamandal-anchor-42",
        broker_status="SUBMITTED",
        broker_response={"buyingPowerRequirement": "700.00"},
    )
    store.save_pending_orders([known])

    sync_public_portfolio(config, store=store, adapter=FakePublicSyncAdapter())

    positions = store.list_positions()
    assert len(positions) == 1
    group = positions[0]
    assert group.source == PositionSource.KAMANDAL_ORDER.value
    assert group.broker_order_id == "kamandal-anchor-42"
    assert group.strategy_id == "aapl_debit_spread"
    # Preflight BPR from the known order should override the inference formula.
    assert group.bpr == 700.0


def test_sync_public_portfolio_ignores_close_order_group_anchors(tmp_path) -> None:
    """A close order may reuse the open order anchor, but it must not rehydrate open legs."""
    config = {
        "storage": {
            "local": {
                "sqlite_path": str(tmp_path / "kamandal.db"),
                "audit_dir": str(tmp_path / "audit"),
            }
        }
    }
    store = LocalStore(
        sqlite_path=tmp_path / "kamandal.db", audit_dir=tmp_path / "audit"
    )

    close_order = PendingOrder(
        pending_order_id="pm_close_1",
        plan_id="position_manager",
        created_at="2026-04-03T12:00:00Z",
        action=TradeAction.CLOSE,
        status="pending",
        underlying="AAPL",
        strategy_id="should_not_be_inherited",
        quantity=1,
        target_price=7.00,
        estimated_credit=700.0,
        estimated_bpr=999.0,
        greeks_impact=Greeks(),
        legs=[
            OptionLeg("AAPL", "2026-05-15", 200.0, "call", "buy"),
            OptionLeg("AAPL", "2026-05-15", 210.0, "call", "sell"),
        ],
        broker="public",
        broker_order_id="kamandal-anchor-42",
        broker_status="SUBMITTED",
        broker_response={"buyingPowerRequirement": "999.00"},
    )
    store.save_pending_orders([close_order])

    sync_public_portfolio(config, store=store, adapter=FakePublicSyncAdapter())

    positions = store.list_positions()
    assert len(positions) == 1
    group = positions[0]
    assert group.source == PositionSource.PUBLIC_INFERRED.value
    assert group.strategy_id == ""
    assert group.broker_order_id == ""
    assert group.bpr != 999.0


def test_sync_public_portfolio_ignores_preflight_only_open_orders(tmp_path) -> None:
    """Dry-run/pending preflight anchors should not claim matching live broker legs."""
    config = {
        "storage": {
            "local": {
                "sqlite_path": str(tmp_path / "kamandal.db"),
                "audit_dir": str(tmp_path / "audit"),
            }
        }
    }
    store = LocalStore(
        sqlite_path=tmp_path / "kamandal.db", audit_dir=tmp_path / "audit"
    )

    preflight_order = PendingOrder(
        pending_order_id="dry_run_1",
        plan_id="plan_1",
        created_at="2026-04-03T12:00:00Z",
        action=TradeAction.OPEN,
        status="dry_run",
        underlying="AAPL",
        strategy_id="should_not_be_inherited",
        quantity=1,
        target_price=7.00,
        estimated_credit=-700.0,
        estimated_bpr=999.0,
        greeks_impact=Greeks(),
        legs=[
            OptionLeg("AAPL", "2026-05-15", 200.0, "call", "buy"),
            OptionLeg("AAPL", "2026-05-15", 210.0, "call", "sell"),
        ],
        broker="public",
        broker_order_id="dry-run-anchor",
        broker_status="PREFLIGHT_OK",
        broker_response={"buyingPowerRequirement": "999.00"},
    )
    store.save_pending_orders([preflight_order])

    sync_public_portfolio(config, store=store, adapter=FakePublicSyncAdapter())

    group = store.list_positions()[0]
    assert group.source == PositionSource.PUBLIC_INFERRED.value
    assert group.strategy_id == ""
    assert group.broker_order_id == ""
