from __future__ import annotations

import json

from vol_crush.integrations.fixtures import FixtureMarketDataProvider
from vol_crush.integrations.market_data import PublicFallbackMarketDataProvider


class FakePublicAdapter:
    def __init__(self, *, quote_payload: dict, chain_payloads: dict[str, dict]) -> None:
        self.quote_payload = quote_payload
        self.chain_payloads = chain_payloads

    def get_quotes(self, instruments: list[dict[str, str]]) -> dict:
        return self.quote_payload

    def get_option_chain(self, symbol: str, expiration_date: str) -> dict:
        return self.chain_payloads.get(expiration_date, {"calls": [], "puts": []})


def _bundle_path(tmp_path):
    path = tmp_path / "fixture_bundle.json"
    path.write_text(
        json.dumps(
            {
                "market_snapshots": [
                    {
                        "symbol": "QQQ",
                        "timestamp": "2026-04-21T12:00:00Z",
                        "underlying_price": 640.0,
                        "iv_rank": 18.75,
                        "realized_volatility": 12.0,
                        "beta_to_spy": 1.1,
                        "sector": "technology",
                        "event_risk": False,
                        "source": "fixture",
                        "option_snapshots": [
                            {
                                "underlying": "QQQ",
                                "timestamp": "2026-04-16T19:59:02Z",
                                "option_type": "put",
                                "strike": 633.0,
                                "expiration": "2026-04-17",
                                "bid": 0.45,
                                "ask": 0.55,
                                "last": 0.5,
                                "greeks": {
                                    "delta": -0.16,
                                    "gamma": 0.03,
                                    "theta": -0.2,
                                    "vega": 0.07,
                                },
                                "implied_volatility": 20.0,
                                "source": "fixture",
                                "quality_flags": [],
                            }
                        ],
                        "notes": ["fixture context"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_public_market_data_provider_merges_live_chain_with_fixture_context(tmp_path):
    fallback = FixtureMarketDataProvider(_bundle_path(tmp_path))
    adapter = FakePublicAdapter(
        quote_payload={
            "quotes": [
                {
                    "instrument": {"symbol": "QQQ", "type": "EQUITY"},
                    "outcome": "SUCCESS",
                    "last": "648.61",
                    "lastTimestamp": "2026-04-21T12:57:41Z",
                    "bid": "649.07",
                    "ask": "649.14",
                }
            ]
        },
        chain_payloads={
            "2026-05-15": {
                "calls": [
                    {
                        "instrument": {"symbol": "QQQ260515C00660000", "type": "OPTION"},
                        "outcome": "SUCCESS",
                        "last": "1.22",
                        "lastTimestamp": "2026-04-21T12:57:41Z",
                        "bid": "1.20",
                        "ask": "1.24",
                        "optionDetails": {
                            "greeks": {
                                "delta": "0.17",
                                "gamma": "0.02",
                                "theta": "-0.11",
                                "vega": "0.08",
                                "impliedVolatility": "0.22",
                            },
                            "strikePrice": "660",
                        },
                    }
                ],
                "puts": [
                    {
                        "instrument": {"symbol": "QQQ260515P00630000", "type": "OPTION"},
                        "outcome": "SUCCESS",
                        "last": "1.18",
                        "lastTimestamp": "2026-04-21T12:57:41Z",
                        "bid": "1.15",
                        "ask": "1.21",
                        "optionDetails": {
                            "greeks": {
                                "delta": "-0.16",
                                "gamma": "0.02",
                                "theta": "-0.10",
                                "vega": "0.07",
                                "impliedVolatility": "0.24",
                            },
                            "strikePrice": "630",
                        },
                    }
                ],
            }
        },
    )

    provider = PublicFallbackMarketDataProvider(
        fallback=fallback,
        config={},
        adapter=adapter,
        expiration_dates=["2026-05-15"],
    )

    snapshot = provider.get_market_snapshot("QQQ")

    assert snapshot is not None
    assert snapshot.source == "public_marketdata"
    assert snapshot.underlying_price == 648.61
    assert snapshot.iv_rank == 18.75
    assert snapshot.sector == "technology"
    assert len(snapshot.option_snapshots) == 2
    assert {item.expiration for item in snapshot.option_snapshots} == {"2026-05-15"}


def test_public_market_data_provider_falls_back_to_fixture_when_chain_missing(tmp_path):
    fallback = FixtureMarketDataProvider(_bundle_path(tmp_path))
    adapter = FakePublicAdapter(
        quote_payload={"quotes": []},
        chain_payloads={},
    )

    provider = PublicFallbackMarketDataProvider(
        fallback=fallback,
        config={},
        adapter=adapter,
        expiration_dates=["2026-05-15"],
    )

    snapshot = provider.get_market_snapshot("QQQ")

    assert snapshot is not None
    assert snapshot.source == "fixture"
    assert snapshot.option_snapshots[0].expiration == "2026-04-17"
