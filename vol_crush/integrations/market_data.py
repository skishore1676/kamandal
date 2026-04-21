"""Market-data provider selection for planning and shadow execution."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from vol_crush.core.interfaces import MarketDataProvider
from vol_crush.core.models import Greeks, MarketSnapshot, OptionSnapshot
from vol_crush.integrations.fixtures import FixtureMarketDataProvider
from vol_crush.integrations.public_broker import PublicBrokerAdapter, parse_occ_symbol

logger = logging.getLogger("vol_crush.integrations.market_data")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _nearest_fridays(start_dte: int = 21, end_dte: int = 49) -> list[str]:
    today = date.today()
    expirations: list[str] = []
    for days_ahead in range(start_dte, end_dte + 1):
        candidate = today + timedelta(days=days_ahead)
        if candidate.weekday() == 4:
            expirations.append(candidate.isoformat())
    return expirations[:3]


class PublicFallbackMarketDataProvider(MarketDataProvider):
    """Use Public option chains for planning, with fixture fallback."""

    def __init__(
        self,
        *,
        fallback: FixtureMarketDataProvider,
        config: dict[str, Any],
        adapter: PublicBrokerAdapter | None = None,
        expiration_dates: Sequence[str] | None = None,
    ):
        self.fallback = fallback
        self.adapter = adapter or PublicBrokerAdapter(config)
        self.expiration_dates = list(expiration_dates or _nearest_fridays())
        self._cache: dict[str, MarketSnapshot | None] = {}

    def list_market_snapshots(self) -> list[MarketSnapshot]:
        return self.fallback.list_market_snapshots()

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot | None:
        symbol = symbol.upper()
        if symbol in self._cache:
            return self._cache[symbol]

        base = self.fallback.get_market_snapshot(symbol)
        try:
            live = self._fetch_live_snapshot(symbol, base)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Public market data failed for %s; using fixture fallback: %s",
                symbol,
                exc,
            )
            live = None

        self._cache[symbol] = live or base
        return self._cache[symbol]

    def _fetch_live_snapshot(
        self, symbol: str, base: MarketSnapshot | None
    ) -> MarketSnapshot | None:
        quote_payload = self.adapter.get_quotes([{"symbol": symbol, "type": "EQUITY"}])
        quotes = quote_payload.get("quotes", []) or []
        quote = next(
            (
                item
                for item in quotes
                if str((item.get("instrument") or {}).get("symbol", "")).upper() == symbol
            ),
            None,
        )
        if not quote:
            return None

        price = _as_float(quote.get("last"))
        if price <= 0:
            bid = _as_float(quote.get("bid"))
            ask = _as_float(quote.get("ask"))
            if bid > 0 and ask > 0:
                price = (bid + ask) / 2.0
            else:
                price = base.underlying_price if base else 0.0

        option_snapshots: list[OptionSnapshot] = []
        seen_symbols: set[str] = set()
        for expiration in self.expiration_dates:
            try:
                chain = self.adapter.get_option_chain(symbol, expiration)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Skipping Public option chain for %s %s: %s",
                    symbol,
                    expiration,
                    exc,
                )
                continue
            option_snapshots.extend(
                self._parse_chain_items(chain.get("calls", []), seen_symbols)
            )
            option_snapshots.extend(
                self._parse_chain_items(chain.get("puts", []), seen_symbols)
            )

        if not option_snapshots:
            return None

        timestamp = (
            quote.get("lastTimestamp")
            or quote.get("bidTimestamp")
            or quote.get("askTimestamp")
            or datetime.now(UTC).isoformat()
        )
        return MarketSnapshot(
            symbol=symbol,
            timestamp=str(timestamp),
            underlying_price=price,
            iv_rank=base.iv_rank if base else 0.0,
            realized_volatility=base.realized_volatility if base else 0.0,
            beta_to_spy=base.beta_to_spy if base else 1.0,
            sector=base.sector if base else "unknown",
            event_risk=base.event_risk if base else False,
            source="public_marketdata",
            option_snapshots=option_snapshots,
            notes=(base.notes[:] if base else []) + ["option chain refreshed from Public"],
        )

    def _parse_chain_items(
        self, items: Sequence[dict[str, Any]], seen_symbols: set[str]
    ) -> list[OptionSnapshot]:
        parsed: list[OptionSnapshot] = []
        for item in items:
            if str(item.get("outcome", "")).upper() != "SUCCESS":
                continue
            instrument = item.get("instrument") or {}
            option_symbol = str(instrument.get("symbol", "")).upper()
            if not option_symbol or option_symbol in seen_symbols:
                continue
            seen_symbols.add(option_symbol)
            details = item.get("optionDetails") or {}
            greeks = (details.get("greeks") or {}) if isinstance(details, dict) else {}
            parsed_symbol = parse_occ_symbol(option_symbol)
            timestamp = (
                item.get("lastTimestamp")
                or item.get("bidTimestamp")
                or item.get("askTimestamp")
                or datetime.now(UTC).isoformat()
            )
            parsed.append(
                OptionSnapshot(
                    underlying=parsed_symbol["underlying"],
                    timestamp=str(timestamp),
                    option_type=parsed_symbol["option_type"],
                    strike=_as_float(details.get("strikePrice"), parsed_symbol["strike"]),
                    expiration=parsed_symbol["expiration"],
                    bid=_as_float(item.get("bid")),
                    ask=_as_float(item.get("ask")),
                    last=_as_float(item.get("last")),
                    greeks=Greeks(
                        delta=_as_float(greeks.get("delta")),
                        gamma=_as_float(greeks.get("gamma")),
                        theta=_as_float(greeks.get("theta")),
                        vega=_as_float(greeks.get("vega")),
                    ),
                    implied_volatility=_as_float(greeks.get("impliedVolatility")),
                    source="public_marketdata",
                )
            )
        return parsed


def build_market_data_provider(
    config: dict[str, Any], bundle_path: str | Path
) -> MarketDataProvider:
    fallback = FixtureMarketDataProvider(bundle_path)
    broker_cfg = config.get("broker", {}) or {}
    if str(broker_cfg.get("active", "")).lower() != "public":
        return fallback
    public_cfg = broker_cfg.get("public", {}) or {}
    if not str(public_cfg.get("secret_token", "")).strip():
        return fallback
    return PublicFallbackMarketDataProvider(fallback=fallback, config=config)
