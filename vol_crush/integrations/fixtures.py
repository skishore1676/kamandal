"""Fixture building and local market-data provider utilities."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from vol_crush.core.config import get_project_root, load_config
from vol_crush.core.interfaces import MarketDataProvider
from vol_crush.core.logging import setup_logging
from vol_crush.core.models import Greeks, MarketSnapshot, OptionSnapshot, ReplayTrade
from vol_crush.integrations.storage import build_local_store

logger = logging.getLogger("vol_crush.integrations.fixtures")

SECTOR_MAP = {
    "SPY": "broad_market",
    "QQQ": "technology",
    "IWM": "small_caps",
    "TLT": "rates",
    "SMH": "technology",
    "GLD": "metals",
    "AAPL": "technology",
    "AMD": "technology",
    "GE": "industrial",
    "HOOD": "financials",
    "INTC": "technology",
    "NVDA": "technology",
    "RBLX": "communication",
    "TSLA": "consumer",
}


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return get_project_root() / path


def _extract_underlying(symbol: str) -> str:
    match = re.match(r"([A-Za-z]+)", symbol or "")
    if not match:
        return symbol.upper()
    return match.group(1).upper()


def _extract_expiration_from_option_symbol(symbol: str, fallback: str) -> str:
    match = re.search(r"(\d{6})[CP]\d+$", symbol or "")
    if not match:
        return fallback
    try:
        return datetime.strptime(match.group(1), "%y%m%d").date().isoformat()
    except ValueError:
        return fallback


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sheet_universe_symbols(config: dict[str, Any]) -> list[str]:
    """Return enabled universe symbols from the local Google Sheet cache."""
    if not (config.get("google_sheets") or {}).get("enabled", False):
        return []
    try:
        from vol_crush.sheets.sync import read_universe_cache
    except ImportError:
        return []

    seen: set[str] = set()
    symbols: list[str] = []
    for row in read_universe_cache(config):
        symbol = row.symbol.upper()
        if not row.enabled or not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _fixture_seed_symbols(
    config: dict[str, Any], fixture_cfg: dict[str, Any]
) -> list[str]:
    """Resolve public seed symbols with Google Sheet universe as primary input."""
    sheet_symbols = _sheet_universe_symbols(config)
    raw_fallback = fixture_cfg.get("public_seed_symbols", []) or []
    fallback_symbols = [str(symbol).upper() for symbol in raw_fallback if symbol]
    source_symbols = sheet_symbols or fallback_symbols

    seen: set[str] = set()
    result: list[str] = []
    for symbol in source_symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    if sheet_symbols:
        logger.info(
            "Fixture seed universe loaded from Google Sheet: %d symbols", len(result)
        )
    else:
        logger.info(
            "Fixture seed universe loaded from config fallback: %d symbols", len(result)
        )
    return result


def _build_option_snapshots(row: sqlite3.Row) -> list[OptionSnapshot]:
    timestamp = row["timestamp"]
    symbol = row["symbol"]
    underlying_price = _safe_float(row["stock_price"])
    fallback_expiration = (
        datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date().isoformat()
    )
    call_expiration = _extract_expiration_from_option_symbol(
        row["call_symbol"], fallback_expiration
    )
    put_expiration = _extract_expiration_from_option_symbol(
        row["put_symbol"], fallback_expiration
    )

    call = OptionSnapshot(
        underlying=symbol,
        timestamp=timestamp,
        option_type="call",
        strike=_safe_float(row["call_strike"], underlying_price),
        expiration=call_expiration,
        bid=_safe_float(row["call_bid"]),
        ask=_safe_float(row["call_ask"]),
        last=_safe_float(row["call_last"]),
        greeks=Greeks(
            delta=_safe_float(row["call_delta"]),
            gamma=_safe_float(row["call_gamma"]),
            theta=_safe_float(row["call_theta"]),
            vega=_safe_float(row["call_vega"]),
        ),
        implied_volatility=_safe_float(row["call_iv"]) * 100.0,
        gds_score=_safe_float(row["call_gds"]),
        source="gds_history.db",
    )
    put = OptionSnapshot(
        underlying=symbol,
        timestamp=timestamp,
        option_type="put",
        strike=_safe_float(row["put_strike"], underlying_price),
        expiration=put_expiration,
        bid=_safe_float(row["put_bid"]),
        ask=_safe_float(row["put_ask"]),
        last=_safe_float(row["put_last"]),
        greeks=Greeks(
            delta=_safe_float(row["put_delta"]),
            gamma=_safe_float(row["put_gamma"]),
            theta=_safe_float(row["put_theta"]),
            vega=_safe_float(row["put_vega"]),
        ),
        implied_volatility=_safe_float(row["put_iv"]) * 100.0,
        gds_score=_safe_float(row["put_gds"]),
        source="gds_history.db",
    )
    return [call, put]


def fetch_public_market_seed(symbol: str) -> dict[str, Any]:
    """Fetch public underlying context from Yahoo Finance when available."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    try:
        request = Request(url, headers={"User-Agent": "vol-crush-fixture-builder/1.0"})
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        close = next(
            (float(item) for item in reversed(closes) if item is not None), 0.0
        )
        return {"symbol": symbol, "underlying_price": close, "source": "yahoo_public"}
    except (HTTPError, URLError, KeyError, IndexError, ValueError, TypeError) as exc:
        logger.warning("Public seed fetch failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "underlying_price": 0.0, "source": "unavailable"}


def _synthetic_option_snapshots(
    symbol: str,
    *,
    timestamp: str,
    underlying_price: float,
    iv_rank: float,
) -> list[OptionSnapshot]:
    """Build a small deterministic option surface for replay smoke tests.

    Public quote seeds do not provide option chains. These synthetic snapshots
    are deliberately simple and tagged as synthetic so optimizer plumbing can be
    exercised without mistaking them for tradable quotes.
    """
    if underlying_price <= 0:
        return []
    expiry = (datetime.now(timezone.utc).date()).toordinal()
    expiration_35 = datetime.fromordinal(expiry + 35).date().isoformat()
    expiration_56 = datetime.fromordinal(expiry + 56).date().isoformat()
    iv = max(iv_rank / 100.0, 0.12)
    specs = [
        ("call", underlying_price * 1.18, 0.18, expiration_35),
        ("put", underlying_price * 0.82, -0.18, expiration_35),
        ("call", underlying_price * 1.02, 0.52, expiration_56),
        ("put", underlying_price * 0.98, -0.48, expiration_56),
    ]
    snapshots: list[OptionSnapshot] = []
    for option_type, raw_strike, delta, expiration in specs:
        strike = round(raw_strike / 5.0) * 5.0
        intrinsic = (
            max(underlying_price - strike, 0.0)
            if option_type == "call"
            else max(strike - underlying_price, 0.0)
        )
        extrinsic = max(underlying_price * iv * 0.035, 0.25)
        mid = round(intrinsic + extrinsic, 2)
        bid = round(max(mid - 0.05, 0.01), 2)
        ask = round(mid + 0.05, 2)
        theta = -round(max(mid * 0.015, 0.01), 4)
        snapshots.append(
            OptionSnapshot(
                underlying=symbol,
                timestamp=timestamp,
                option_type=option_type,
                strike=strike,
                expiration=expiration,
                bid=bid,
                ask=ask,
                last=mid,
                greeks=Greeks(
                    delta=delta,
                    gamma=0.004,
                    theta=theta,
                    vega=round(max(underlying_price * 0.0015, 0.05), 4),
                ),
                implied_volatility=round(iv * 100.0, 2),
                source="synthetic_public_seed",
            )
        )
    return snapshots


def build_fixture_payload(
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[ReplayTrade]]:
    """Build a normalized fixture bundle from sibling repos and public data."""
    fixture_cfg = config.get("data_sources", {}).get("fixtures", {})
    db_path = _resolve_path(fixture_cfg.get("import_gds_history_db", ""))
    analysis_path = _resolve_path(fixture_cfg.get("import_gds_analysis_json", ""))

    snapshots: dict[str, MarketSnapshot] = {}
    provenance: list[dict[str, Any]] = []

    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT mo.*
                FROM market_observations mo
                JOIN (
                    SELECT symbol, MAX(timestamp) AS max_timestamp
                    FROM market_observations
                    GROUP BY symbol
                ) latest
                  ON latest.symbol = mo.symbol
                 AND latest.max_timestamp = mo.timestamp
                ORDER BY mo.symbol
                """).fetchall()
        for row in rows:
            symbol = row["symbol"].upper()
            option_snapshots = _build_option_snapshots(row)
            avg_iv = sum(item.implied_volatility for item in option_snapshots) / max(
                len(option_snapshots), 1
            )
            snapshots[symbol] = MarketSnapshot(
                symbol=symbol,
                timestamp=row["timestamp"],
                underlying_price=_safe_float(row["stock_price"]),
                iv_rank=min(round(avg_iv, 2), 100.0),
                realized_volatility=round(avg_iv * 0.6, 2),
                beta_to_spy=1.0 if symbol == "SPY" else 0.9,
                sector=SECTOR_MAP.get(symbol, "unknown"),
                event_risk=False,
                source="gds_history.db",
                option_snapshots=option_snapshots,
            )
        provenance.append({"source": str(db_path), "records": len(rows)})

    seed_symbols = _fixture_seed_symbols(config, fixture_cfg)
    if fixture_cfg.get("enable_public_seed_fetch", True):
        for symbol in seed_symbols:
            symbol = symbol.upper()
            seed = fetch_public_market_seed(symbol)
            if symbol not in snapshots:
                timestamp = datetime.now(timezone.utc).isoformat()
                underlying_price = _safe_float(seed.get("underlying_price"), 100.0)
                snapshots[symbol] = MarketSnapshot(
                    symbol=symbol,
                    timestamp=timestamp,
                    underlying_price=underlying_price,
                    iv_rank=20.0,
                    realized_volatility=12.0,
                    beta_to_spy=1.0 if symbol == "SPY" else 0.9,
                    sector=SECTOR_MAP.get(symbol, "unknown"),
                    event_risk=False,
                    source=seed.get("source", "public_seed"),
                    option_snapshots=_synthetic_option_snapshots(
                        symbol,
                        timestamp=timestamp,
                        underlying_price=underlying_price,
                        iv_rank=20.0,
                    ),
                )
            elif seed.get("underlying_price"):
                snapshots[symbol].underlying_price = _safe_float(
                    seed["underlying_price"]
                )
                if not snapshots[symbol].option_snapshots:
                    snapshots[symbol].option_snapshots = _synthetic_option_snapshots(
                        symbol,
                        timestamp=snapshots[symbol].timestamp,
                        underlying_price=snapshots[symbol].underlying_price,
                        iv_rank=snapshots[symbol].iv_rank,
                    )
                snapshots[symbol].notes.append(
                    f"underlying refreshed from {seed['source']}"
                )
            provenance.append(seed)

    replay_trades: list[ReplayTrade] = []
    if analysis_path.exists():
        raw = json.loads(analysis_path.read_text(encoding="utf-8"))
        for item in raw:
            entry_greeks = Greeks.from_dict(item.get("entry_greeks", {}))
            terminal_greeks = Greeks.from_dict(item.get("terminal_greeks", {}))
            theta_capture = _safe_float(item.get("profit_pct")) / max(
                abs(entry_greeks.theta), 0.01
            )
            replay_trades.append(
                ReplayTrade(
                    trade_id=item.get("trade_id", ""),
                    underlying=_extract_underlying(item.get("symbol", "")),
                    symbol=item.get("symbol", ""),
                    profit_pct=_safe_float(item.get("profit_pct")),
                    is_winner=bool(item.get("is_winner", False)),
                    entry_price=_safe_float(item.get("entry_price")),
                    exit_price=_safe_float(item.get("exit_price")),
                    entry_greeks=entry_greeks,
                    terminal_greeks=terminal_greeks,
                    theta_capture_proxy=theta_capture,
                )
            )
        provenance.append({"source": str(analysis_path), "records": len(replay_trades)})

    if not snapshots:
        for symbol, price, iv_rank in (
            ("SPY", 520.0, 28.0),
            ("IWM", 205.0, 34.0),
            ("QQQ", 442.0, 22.0),
        ):
            snapshots[symbol] = MarketSnapshot(
                symbol=symbol,
                timestamp=datetime.now(timezone.utc).isoformat(),
                underlying_price=price,
                iv_rank=iv_rank,
                realized_volatility=iv_rank * 0.65,
                beta_to_spy=1.0 if symbol == "SPY" else 0.9,
                sector=SECTOR_MAP.get(symbol, "unknown"),
                source="fallback_static",
            )
        provenance.append({"source": "fallback_static", "records": len(snapshots)})

    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance,
        "market_snapshots": [snapshot.to_dict() for snapshot in snapshots.values()],
    }
    return payload, replay_trades


def write_fixture_artifacts(
    config: dict[str, Any], payload: dict[str, Any], replay_trades: list[ReplayTrade]
) -> tuple[Path, Path]:
    fixture_cfg = config.get("data_sources", {}).get("fixtures", {})
    bundle_path = _resolve_path(
        fixture_cfg.get("bundle_path", "data/fixtures/fixture_bundle.json")
    )
    replay_path = _resolve_path(
        fixture_cfg.get("replay_path", "data/fixtures/replay_trades.json")
    )
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    replay_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    replay_path.write_text(
        json.dumps([trade.to_dict() for trade in replay_trades], indent=2),
        encoding="utf-8",
    )
    return bundle_path, replay_path


class FixtureMarketDataProvider(MarketDataProvider):
    """Runtime provider backed by the Vol Crush fixture bundle."""

    def __init__(self, bundle_path: str | Path):
        self.bundle_path = _resolve_path(str(bundle_path))
        self._cache = self._load_bundle()

    def _load_bundle(self) -> dict[str, Any]:
        if not self.bundle_path.exists():
            return {"market_snapshots": []}
        return json.loads(self.bundle_path.read_text(encoding="utf-8"))

    def refresh(self) -> None:
        self._cache = self._load_bundle()

    def list_market_snapshots(self) -> list[MarketSnapshot]:
        return [
            MarketSnapshot.from_dict(item)
            for item in self._cache.get("market_snapshots", [])
        ]

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot | None:
        symbol = symbol.upper()
        for snapshot in self.list_market_snapshots():
            if snapshot.symbol == symbol:
                return snapshot
        return None


def load_replay_trades(config: dict[str, Any]) -> list[ReplayTrade]:
    fixture_cfg = config.get("data_sources", {}).get("fixtures", {})
    replay_path = _resolve_path(
        fixture_cfg.get("replay_path", "data/fixtures/replay_trades.json")
    )
    if not replay_path.exists():
        return []
    data = json.loads(replay_path.read_text(encoding="utf-8"))
    return [ReplayTrade.from_dict(item) for item in data]


def main() -> None:
    parser = argparse.ArgumentParser(description="Vol Crush fixture builder")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.get("app", {}).get("log_level", "INFO"))
    store = build_local_store(config)

    payload, replay_trades = build_fixture_payload(config)
    bundle_path, replay_path = write_fixture_artifacts(config, payload, replay_trades)
    store.save_fixture_payload(payload)
    store.save_replay_trades(replay_trades)

    logger.info("Fixture bundle written to %s", bundle_path)
    logger.info("Replay trades written to %s", replay_path)
    logger.info(
        "Stored %d market snapshots and %d replay trades",
        len(payload["market_snapshots"]),
        len(replay_trades),
    )


if __name__ == "__main__":
    main()
