"""Public broker integration for Kamandal execution workflows."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from vol_crush.core.config import get_project_root
from vol_crush.core.models import OptionLeg, OrderStatus, PendingOrder, TradeAction

logger = logging.getLogger("vol_crush.integrations.public_broker")


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return get_project_root() / path


def _format_price(value: float) -> str:
    return f"{abs(float(value)):.2f}"


def _occ_symbol_from_leg(leg: OptionLeg) -> str:
    expiration = leg.expiration.replace("-", "")
    if len(expiration) != 8:
        raise ValueError(f"Invalid expiration for leg: {leg.expiration}")
    yy_mm_dd = expiration[2:]
    option_flag = "C" if leg.option_type.lower() == "call" else "P"
    strike_int = int(round(float(leg.strike) * 1000))
    return f"{leg.underlying.upper()}{yy_mm_dd}{option_flag}{strike_int:08d}"


def parse_occ_symbol(option_symbol: str) -> dict[str, Any]:
    """Parse a normalized OCC/OSI option symbol into components."""
    match = re.match(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$", option_symbol.upper())
    if not match:
        raise ValueError(f"Unsupported OCC option symbol: {option_symbol}")
    root, yymmdd, option_flag, strike_raw = match.groups()
    expiration = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return {
        "underlying": root,
        "expiration": expiration,
        "option_type": "call" if option_flag == "C" else "put",
        "strike": int(strike_raw) / 1000.0,
    }


def _invert_side(side: str) -> str:
    return "BUY" if side.lower() == "sell" else "SELL"


def _public_side(action: TradeAction, leg: OptionLeg) -> str:
    if action == TradeAction.OPEN:
        return leg.side.upper()
    return _invert_side(leg.side)


def _open_close_indicator(action: TradeAction) -> str:
    return "OPEN" if action == TradeAction.OPEN else "CLOSE"


@dataclass
class PublicBrokerSettings:
    secret_token: str = ""
    api_base_url: str = "https://api.public.com"
    auth_endpoint: str = "https://api.public.com/userapiauthservice/personal/access-tokens"
    account_id: str = ""
    session_file: str = "data/cache/public_session.json"
    account_cache_file: str = "data/cache/public_account.json"
    token_validity_minutes: int = 60
    api_requests_per_second: float = 5.0
    api_burst_limit: int = 5
    require_preflight: bool = True
    sync_portfolio_after_submission: bool = True

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "PublicBrokerSettings":
        payload = config.get("broker", {}).get("public", {})
        return cls(
            secret_token=str(payload.get("secret_token", "")),
            api_base_url=str(payload.get("api_base_url", cls.api_base_url)),
            auth_endpoint=str(payload.get("auth_endpoint", cls.auth_endpoint)),
            account_id=str(payload.get("account_id", "")),
            session_file=str(payload.get("session_file", cls.session_file)),
            account_cache_file=str(payload.get("account_cache_file", cls.account_cache_file)),
            token_validity_minutes=int(payload.get("token_validity_minutes", cls.token_validity_minutes)),
            api_requests_per_second=float(payload.get("api_requests_per_second", cls.api_requests_per_second)),
            api_burst_limit=int(payload.get("api_burst_limit", cls.api_burst_limit)),
            require_preflight=bool(payload.get("require_preflight", True)),
            sync_portfolio_after_submission=bool(
                payload.get("sync_portfolio_after_submission", True)
            ),
        )

    def validate_credentials(self) -> None:
        if not self.secret_token:
            raise ValueError("PUBLIC_SECRET_TOKEN is not configured")


class PublicRateLimiter:
    """Small token bucket limiter for the Public API."""

    def __init__(self, requests_per_second: float, burst_limit: int) -> None:
        self.requests_per_second = max(requests_per_second, 0.1)
        self.burst_limit = max(burst_limit, 1)
        self.tokens = float(self.burst_limit)
        self.last_update = time.time()

    def acquire(self) -> None:
        now = time.time()
        elapsed = now - self.last_update
        self.tokens = min(self.burst_limit, self.tokens + elapsed * self.requests_per_second)
        self.last_update = now
        if self.tokens < 1.0:
            wait_time = (1.0 - self.tokens) / self.requests_per_second
            time.sleep(wait_time)
            now = time.time()
            elapsed = now - self.last_update
            self.tokens = min(self.burst_limit, self.tokens + elapsed * self.requests_per_second)
            self.last_update = now
        self.tokens = max(self.tokens - 1.0, 0.0)


class PublicApiClient:
    """Sync HTTP client for Public authenticated requests."""

    def __init__(self, settings: PublicBrokerSettings):
        self.settings = settings
        self.session = requests.Session()
        self.rate_limiter = PublicRateLimiter(
            requests_per_second=settings.api_requests_per_second,
            burst_limit=settings.api_burst_limit,
        )

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _get_access_token(self) -> str:
        self.settings.validate_credentials()
        session_path = _resolve_path(self.settings.session_file)
        cached = self._read_json(session_path) or {}
        expires_at = float(cached.get("expiration_timestamp", 0))
        if cached.get("access_token") and expires_at > time.time() + 300:
            return str(cached["access_token"])

        response = self.session.post(
            self.settings.auth_endpoint,
            json={
                "secret": self.settings.secret_token,
                "validityInMinutes": self.settings.token_validity_minutes,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("accessToken")
        if not token:
            raise ValueError("Public auth response did not contain accessToken")
        expires_in = int(payload.get("expiresIn", self.settings.token_validity_minutes * 60))
        self._write_json(
            session_path,
            {
                "access_token": token,
                "expiration_timestamp": int(time.time()) + expires_in,
            },
        )
        return str(token)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.rate_limiter.acquire()
        response = self.session.request(
            method=method,
            url=f"{self.settings.api_base_url.rstrip('/')}{endpoint}",
            headers={
                "Authorization": f"Bearer {self._get_access_token()}",
                "Content-Type": "application/json",
            },
            params=params,
            json=json_data,
            timeout=30,
        )
        if response.status_code == 429:
            time.sleep(1.0)
            self.rate_limiter.acquire()
            response = self.session.request(
                method=method,
                url=f"{self.settings.api_base_url.rstrip('/')}{endpoint}",
                headers={
                    "Authorization": f"Bearer {self._get_access_token()}",
                    "Content-Type": "application/json",
                },
                params=params,
                json=json_data,
                timeout=30,
            )
        response.raise_for_status()
        if response.status_code == 204 or not response.text:
            return {}
        return response.json()

    def get(self, endpoint: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", endpoint, params=params)

    def post(self, endpoint: str, *, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", endpoint, json_data=json_data)

    def delete(self, endpoint: str) -> dict[str, Any]:
        return self._request("DELETE", endpoint)


class PublicBrokerAdapter:
    """Broker adapter for Public single-leg and multi-leg options execution."""

    def __init__(self, config: dict[str, Any], client: PublicApiClient | None = None):
        self.config = config
        self.execution_cfg = config.get("execution", {})
        self.settings = PublicBrokerSettings.from_config(config)
        self.client = client or PublicApiClient(self.settings)

    def execution_mode(self) -> str:
        return str(self.execution_cfg.get("mode", "pending"))

    def get_primary_account_id(self) -> str:
        if self.settings.account_id:
            return self.settings.account_id
        cache_path = _resolve_path(self.settings.account_cache_file)
        cached = self.client._read_json(cache_path) or {}
        if cached.get("accountId"):
            return str(cached["accountId"])
        payload = self.client.get("/userapigateway/trading/account")
        accounts = payload.get("accounts", [])
        if not accounts:
            raise ValueError("No Public trading accounts returned by API")
        account_id = accounts[0].get("accountId")
        if not account_id:
            raise ValueError("Primary Public account missing accountId")
        self.client._write_json(cache_path, {"accountId": account_id})
        self.settings.account_id = str(account_id)
        return str(account_id)

    def get_portfolio(self) -> dict[str, Any]:
        account_id = self.get_primary_account_id()
        return self.client.get(f"/userapigateway/trading/{account_id}/portfolio/v2")

    def get_quotes(self, instruments: list[dict[str, str]]) -> dict[str, Any]:
        account_id = self.get_primary_account_id()
        return self.client.post(
            f"/userapigateway/marketdata/{account_id}/quotes",
            json_data={"instruments": instruments},
        )

    def get_option_chain(self, symbol: str, expiration_date: str) -> dict[str, Any]:
        account_id = self.get_primary_account_id()
        return self.client.post(
            f"/userapigateway/marketdata/{account_id}/option-chain",
            json_data={
                "instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
                "expirationDate": expiration_date,
            },
        )

    def get_option_greeks(self, option_symbol: str) -> dict[str, Any]:
        account_id = self.get_primary_account_id()
        payload = self.client.get(
            f"/userapigateway/option-details/{account_id}/greeks",
            params=[("osiSymbols", option_symbol)],
        )
        items = payload.get("greeks", []) or []
        if items:
            return items[0].get("greeks", {})
        return {}

    def get_option_greeks_batch(self, option_symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not option_symbols:
            return {}
        account_id = self.get_primary_account_id()
        payload = self.client.get(
            f"/userapigateway/option-details/{account_id}/greeks",
            params=[("osiSymbols", symbol) for symbol in option_symbols],
        )
        result: dict[str, dict[str, Any]] = {}
        for item in payload.get("greeks", []) or []:
            symbol = item.get("symbol")
            greeks = item.get("greeks", {})
            if symbol:
                result[str(symbol)] = greeks
        return result

    def get_order(self, order_id: str) -> dict[str, Any]:
        account_id = self.get_primary_account_id()
        return self.client.get(f"/userapigateway/trading/{account_id}/order/{order_id}")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        account_id = self.get_primary_account_id()
        return self.client.delete(f"/userapigateway/trading/{account_id}/order/{order_id}")

    def _order_payload(self, order: PendingOrder, *, preflight: bool) -> dict[str, Any]:
        tif = str(self.execution_cfg.get("time_in_force", "DAY")).upper()
        quantity = str(int(order.quantity))
        limit_price = _format_price(order.target_price)

        if len(order.legs) == 1:
            leg = order.legs[0]
            payload: dict[str, Any] = {
                "expiration": {"timeInForce": tif},
                "quantity": quantity,
                "limitPrice": limit_price,
                "instrument": {
                    "symbol": _occ_symbol_from_leg(leg),
                    "type": "OPTION",
                },
                "orderSide": _public_side(order.action, leg),
                "openCloseIndicator": _open_close_indicator(order.action),
            }
            payload["orderType" if preflight else "orderType"] = "LIMIT"
            if not preflight:
                payload["orderId"] = str(uuid.uuid4())
            return payload

        legs = [
            {
                "instrument": {
                    "symbol": _occ_symbol_from_leg(leg),
                    "type": "OPTION",
                },
                "side": _public_side(order.action, leg),
                "openCloseIndicator": _open_close_indicator(order.action),
                "ratioQuantity": int(leg.quantity or 1),
            }
            for leg in order.legs
        ]
        payload = {
            "quantity": quantity,
            "limitPrice": limit_price,
            "expiration": {"timeInForce": tif},
            "legs": legs,
        }
        payload["orderType" if preflight else "type"] = "LIMIT"
        if not preflight:
            payload["orderId"] = str(uuid.uuid4())
        return payload

    def _preflight_order(self, order: PendingOrder) -> tuple[dict[str, Any], dict[str, Any]]:
        account_id = self.get_primary_account_id()
        payload = self._order_payload(order, preflight=True)
        if len(order.legs) == 1:
            response = self.client.post(
                f"/userapigateway/trading/{account_id}/preflight/single-leg",
                json_data=payload,
            )
        else:
            response = self.client.post(
                f"/userapigateway/trading/{account_id}/preflight/multi-leg",
                json_data=payload,
            )
        return payload, response

    def _place_order(self, order: PendingOrder) -> tuple[dict[str, Any], dict[str, Any]]:
        account_id = self.get_primary_account_id()
        payload = self._order_payload(order, preflight=False)
        if len(order.legs) == 1:
            response = self.client.post(
                f"/userapigateway/trading/{account_id}/order",
                json_data=payload,
            )
        else:
            response = self.client.post(
                f"/userapigateway/trading/{account_id}/order/multileg",
                json_data=payload,
            )
        return payload, response

    def submit_pending_orders(self, orders: list[PendingOrder]) -> list[PendingOrder]:
        mode = self.execution_mode()
        submit_to_broker = bool(self.execution_cfg.get("submit_to_broker", True))
        updated: list[PendingOrder] = []

        for order in orders:
            order.broker = "public"
            order.execution_mode = mode

            if order.action in (TradeAction.ROLL, TradeAction.ADJUST):
                order.notes = (
                    f"{order.notes} Public broker integration does not yet automate "
                    "roll/adjust workflows."
                ).strip()
                updated.append(order)
                continue

            if not submit_to_broker:
                updated.append(order)
                continue

            try:
                if mode == "live":
                    if self.settings.require_preflight:
                        preflight_payload, preflight_response = self._preflight_order(order)
                        order.broker_payload = preflight_payload
                        order.broker_response = preflight_response
                        order.broker_status = "PREFLIGHT_OK"
                    payload, response = self._place_order(order)
                    order.broker_payload = payload
                    order.broker_response = response
                    order.broker_order_id = str(response.get("orderId", ""))
                    order.broker_status = str(response.get("status", "SUBMITTED"))
                    order.status = OrderStatus.PENDING.value
                    order.submitted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                else:
                    payload, response = self._preflight_order(order)
                    order.broker_payload = payload
                    order.broker_response = response
                    order.broker_status = "PREFLIGHT_OK"
                    order.status = (
                        OrderStatus.DRY_RUN.value if mode == "dry_run" else OrderStatus.PENDING.value
                    )
                    order.submitted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    order.notes = (
                        f"{order.notes} Public preflight completed successfully."
                    ).strip()
            except Exception as exc:
                logger.exception("Public broker submission failed for %s", order.pending_order_id)
                order.broker_status = "ERROR"
                order.notes = f"{order.notes} Public broker submission failed: {exc}".strip()

            updated.append(order)
        return updated
