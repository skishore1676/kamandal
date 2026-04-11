from __future__ import annotations

from vol_crush.core.models import Greeks, OptionLeg, PendingOrder, TradeAction
from vol_crush.integrations.public_broker import PublicBrokerAdapter


class FakePublicClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.cache: dict[str, dict] = {}

    def _read_json(self, path) -> dict | None:
        return self.cache.get(str(path))

    def _write_json(self, path, payload: dict) -> None:
        self.cache[str(path)] = payload

    def get(self, endpoint: str, *, params=None) -> dict:
        if endpoint == "/userapigateway/trading/account":
            return {"accounts": [{"accountId": "acct_123"}]}
        raise AssertionError(f"unexpected GET endpoint: {endpoint}")

    def post(self, endpoint: str, *, json_data=None) -> dict:
        self.posts.append((endpoint, json_data))
        if endpoint.endswith("/preflight/multi-leg"):
            return {
                "strategyName": "Iron Condor",
                "buyingPowerRequirement": "250.00",
                "estimatedCost": "10.00",
            }
        if endpoint.endswith("/order/multileg"):
            return {"orderId": "ord_live_123"}
        raise AssertionError(f"unexpected POST endpoint: {endpoint}")

    def delete(self, endpoint: str) -> dict:
        raise AssertionError(f"unexpected DELETE endpoint: {endpoint}")


def _config(mode: str) -> dict:
    return {
        "execution": {
            "mode": mode,
            "time_in_force": "DAY",
            "submit_to_broker": True,
        },
        "broker": {
            "active": "public",
            "public": {
                "secret_token": "test-secret",
                "account_id": "",
                "session_file": "data/cache/test_public_session.json",
                "account_cache_file": "data/cache/test_public_account.json",
                "require_preflight": True,
            },
        },
    }


def _multi_leg_order() -> PendingOrder:
    return PendingOrder(
        pending_order_id="pending_1",
        plan_id="plan_1",
        created_at="2026-04-03T12:00:00Z",
        action=TradeAction.OPEN,
        status="pending",
        underlying="AAPL",
        strategy_id="iron_condor",
        quantity=1,
        target_price=1.25,
        estimated_credit=125.0,
        estimated_bpr=250.0,
        greeks_impact=Greeks(delta=0.1, gamma=-0.01, theta=0.05, vega=-0.02),
        legs=[
            OptionLeg("AAPL", "2026-05-15", 180.0, "put", "buy"),
            OptionLeg("AAPL", "2026-05-15", 185.0, "put", "sell"),
            OptionLeg("AAPL", "2026-05-15", 205.0, "call", "sell"),
            OptionLeg("AAPL", "2026-05-15", 210.0, "call", "buy"),
        ],
    )


def test_public_broker_preflights_multileg_orders_in_dry_run() -> None:
    adapter = PublicBrokerAdapter(_config("dry_run"), client=FakePublicClient())

    submitted = adapter.submit_pending_orders([_multi_leg_order()])

    assert len(submitted) == 1
    order = submitted[0]
    assert order.status == "dry_run"
    assert order.broker == "public"
    assert order.broker_status == "PREFLIGHT_OK"
    assert order.broker_response["strategyName"] == "Iron Condor"
    assert order.broker_payload["orderType"] == "LIMIT"
    assert len(order.broker_payload["legs"]) == 4
    assert (
        order.broker_payload["legs"][0]["instrument"]["symbol"] == "AAPL260515P00180000"
    )


def test_public_broker_places_multileg_orders_in_live_mode() -> None:
    client = FakePublicClient()
    adapter = PublicBrokerAdapter(_config("live"), client=client)

    submitted = adapter.submit_pending_orders([_multi_leg_order()])

    order = submitted[0]
    assert order.status == "pending"
    # Client-supplied orderId is the durable group anchor; Public echoes it back and
    # our submitter should never overwrite it with the fake response id.
    assert order.broker_order_id
    assert order.broker_order_id != "ord_live_123"
    assert order.broker_payload["orderId"] == order.broker_order_id
    assert order.broker_payload["type"] == "LIMIT"
    assert any(
        endpoint.endswith("/preflight/multi-leg") for endpoint, _ in client.posts
    )
    assert any(endpoint.endswith("/order/multileg") for endpoint, _ in client.posts)


def test_public_broker_stamps_anchor_before_dry_run_preflight() -> None:
    adapter = PublicBrokerAdapter(_config("dry_run"), client=FakePublicClient())

    submitted = adapter.submit_pending_orders([_multi_leg_order()])
    order = submitted[0]

    # Even in dry_run, the group anchor must be stamped so the PendingOrder
    # can later be matched to raw broker legs if the order is later resubmitted live.
    assert order.broker_order_id
    # Preflight payloads never include orderId — Public rejects that field at the
    # preflight endpoint — so the payload on disk won't carry it, but the anchor
    # must still live on the PendingOrder itself.
    assert "orderId" not in order.broker_payload
