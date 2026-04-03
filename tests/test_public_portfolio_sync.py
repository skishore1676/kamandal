from __future__ import annotations

from vol_crush.integrations.public_broker import parse_occ_symbol
from vol_crush.integrations.storage import LocalStore
from vol_crush.portfolio_sync.service import sync_public_portfolio


class FakePublicSyncAdapter:
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
                {"type": "OPTIONS_LONG", "value": "900.00", "percentageOfPortfolio": "9.0"},
                {"type": "OPTIONS_SHORT", "value": "-200.00", "percentageOfPortfolio": "-2.0"},
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


def test_sync_public_portfolio_persists_positions_and_snapshot(tmp_path) -> None:
    config = {
        "storage": {
            "local": {
                "sqlite_path": str(tmp_path / "kamandal.db"),
                "audit_dir": str(tmp_path / "audit"),
            }
        }
    }
    store = LocalStore(sqlite_path=tmp_path / "kamandal.db", audit_dir=tmp_path / "audit")

    snapshot = sync_public_portfolio(config, store=store, adapter=FakePublicSyncAdapter())

    positions = store.list_positions()
    assert len(positions) == 2
    assert positions[0].strategy_id == "public_imported_option"
    assert snapshot.net_liquidation_value == 4700.0
    assert snapshot.position_count == 2
    assert round(snapshot.greeks.delta, 4) == 0.2
    assert round(snapshot.greeks.gamma, 4) == 0.02
    assert round(snapshot.greeks.theta, 4) == -0.03
    assert round(snapshot.greeks.vega, 4) == 0.05
