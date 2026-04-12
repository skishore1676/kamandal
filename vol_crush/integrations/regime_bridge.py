"""Read today's market regime from the shared trade_lab_bridge Google Sheet.

mala_v1 publishes a daily regime snapshot (VIX band, SPY trend, session type)
to the `trade_lab_bridge` tab of the shared Google Sheet. This module reads
that row and maps it to Kamandal's MarketRegime enum so the optimizer uses
a live market signal instead of stale fixture data.

Read-only — Kamandal never writes to the sheet.

Fallback: if the sheet is unreachable, today's row is missing, or the
trading_date is stale, callers get None and should fall back to the
existing ConfigRegimeEvaluator.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from vol_crush.core.models import MarketRegime, RegimePolicy

logger = logging.getLogger("vol_crush.integrations.regime_bridge")


@dataclass
class RegimeSnapshot:
    """Parsed regime from the trade_lab_bridge sheet."""

    trading_date: date
    vix_band: str  # "low" | "mid" | "high"
    spy_trend_20d: str  # "up" | "flat" | "down"
    session_type: str  # "normal" | "opex" | "post_fed" | "earnings_heavy"
    vix_close: float = 0.0
    spy_close: float = 0.0
    spy_sma20: float = 0.0
    spy_trend_slope_pct: float = 0.0
    kamandal_regime: MarketRegime = MarketRegime.UNKNOWN

    def summary(self) -> str:
        return (
            f"regime={self.kamandal_regime.value} "
            f"vix_band={self.vix_band} spy_trend={self.spy_trend_20d} "
            f"session={self.session_type} vix={self.vix_close:.2f}"
        )


def map_to_kamandal_regime(
    vix_band: str,
    spy_trend_20d: str,
    session_type: str,
) -> MarketRegime:
    """Map mala_v1's three-dimensional regime tuple to Kamandal's enum.

    Mapping:
        event_risk  ← session is opex/post_fed/earnings_heavy,
                       OR vix is high + spy trend is down (active selloff)
        high_iv     ← vix_band == "high" (but not event_risk)
        low_iv      ← vix_band == "low"
        normal_iv   ← everything else (vix_band == "mid")
    """
    special_sessions = {"opex", "post_fed", "earnings_heavy"}
    if session_type in special_sessions:
        return MarketRegime.EVENT_RISK
    if vix_band == "high" and spy_trend_20d == "down":
        return MarketRegime.EVENT_RISK
    if vix_band == "high":
        return MarketRegime.HIGH_IV
    if vix_band == "low":
        return MarketRegime.LOW_IV
    return MarketRegime.NORMAL_IV


def _parse_regime_payload(payload_json: str) -> RegimeSnapshot | None:
    """Parse the JSON string stored in the payload_json column."""
    try:
        data = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return None

    trading_date_str = data.get("trading_date", "")
    try:
        trading_date = date.fromisoformat(trading_date_str)
    except ValueError:
        return None

    vix_band = str(data.get("vix_band", "mid"))
    spy_trend = str(data.get("spy_trend_20d", "flat"))
    session = str(data.get("session_type", "normal"))

    return RegimeSnapshot(
        trading_date=trading_date,
        vix_band=vix_band,
        spy_trend_20d=spy_trend,
        session_type=session,
        vix_close=float(data.get("vix_close", 0.0) or 0.0),
        spy_close=float(data.get("spy_close", 0.0) or 0.0),
        spy_sma20=float(data.get("spy_sma20", 0.0) or 0.0),
        spy_trend_slope_pct=float(data.get("spy_trend_slope_pct", 0.0) or 0.0),
        kamandal_regime=map_to_kamandal_regime(vix_band, spy_trend, session),
    )


def _find_todays_regime_row(
    rows: list[dict[str, Any]],
    target_date: date | None = None,
) -> dict[str, Any] | None:
    """Find the row whose filename matches regime-YYYY-MM-DD.json for today."""
    today = target_date or datetime.now(UTC).date()
    target_filename = f"regime-{today.isoformat()}.json"
    for row in rows:
        if row.get("filename", "").strip() == target_filename:
            return row
    return None


def fetch_regime_from_sheet(
    credentials_path: str | Path,
    spreadsheet_id: str,
    sheet_name: str = "trade_lab_bridge",
    target_date: date | None = None,
) -> RegimeSnapshot | None:
    """Read today's regime from the trade_lab_bridge sheet tab.

    Returns None (triggering fallback) on any failure: missing credentials,
    network error, missing row, stale date, parse error. Never raises.
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.warning("gspread or google-auth not installed; regime bridge disabled")
        return None

    creds_path = Path(credentials_path).expanduser().resolve()
    if not creds_path.exists():
        logger.warning(
            "Google credentials not found at %s; regime bridge disabled", creds_path
        )
        return None

    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet(sheet_name)
        all_records = worksheet.get_all_records()
    except Exception as exc:
        logger.warning("Failed to read trade_lab_bridge sheet: %s", exc)
        return None

    today = target_date or datetime.now(UTC).date()
    row = _find_todays_regime_row(
        [dict(r) for r in all_records],
        target_date=today,
    )
    if row is None:
        logger.warning(
            "No regime row found for %s in trade_lab_bridge", today.isoformat()
        )
        return None

    payload_json = row.get("payload_json", "")
    snapshot = _parse_regime_payload(payload_json)
    if snapshot is None:
        logger.warning("Failed to parse regime payload for %s", today.isoformat())
        return None

    if snapshot.trading_date != today:
        logger.warning(
            "Regime trading_date %s does not match target %s; treating as stale",
            snapshot.trading_date,
            today,
        )
        return None

    logger.info("Regime bridge: %s", snapshot.summary())
    return snapshot


class BridgeRegimeEvaluator:
    """RegimeEvaluator that reads from the trade_lab_bridge sheet.

    Falls back to the config-based regime policies for get_policy().
    The bridge only replaces HOW we determine the current regime, not
    the policy rules applied once the regime is known.
    """

    def __init__(
        self,
        config: dict[str, Any],
        snapshot: RegimeSnapshot | None = None,
    ):
        self.config = config
        self.snapshot = snapshot

        regime_cfg = config.get("portfolio", {}).get("regimes", {})
        self.policies: dict[str, RegimePolicy] = {}
        for key, value in regime_cfg.items():
            payload = dict(value)
            payload["regime"] = key
            self.policies[key] = RegimePolicy.from_dict(payload)

    def determine_regime(self, snapshots: list | None = None) -> MarketRegime:
        if self.snapshot is not None:
            return self.snapshot.kamandal_regime
        return MarketRegime.UNKNOWN

    def get_policy(self, regime: MarketRegime) -> RegimePolicy:
        key = regime.value if isinstance(regime, MarketRegime) else str(regime)
        if key in self.policies:
            return self.policies[key]
        fallback = self.policies.get(MarketRegime.NORMAL_IV.value)
        if fallback:
            return fallback
        return RegimePolicy(regime=MarketRegime.UNKNOWN)
