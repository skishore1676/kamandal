"""Tests for the regime bridge integration (trade_lab_bridge → MarketRegime)."""

from __future__ import annotations

from datetime import date

from vol_crush.core.models import MarketRegime, RegimePolicy
from vol_crush.integrations.regime_bridge import (
    BridgeRegimeEvaluator,
    RegimeSnapshot,
    _find_todays_regime_row,
    _parse_regime_payload,
    map_to_kamandal_regime,
)

# ── Mapping logic ───────────────────────────────────────────────────────────


def test_high_vix_maps_to_high_iv() -> None:
    assert map_to_kamandal_regime("high", "up", "normal") == MarketRegime.HIGH_IV


def test_high_vix_with_downtrend_maps_to_event_risk() -> None:
    assert map_to_kamandal_regime("high", "down", "normal") == MarketRegime.EVENT_RISK


def test_mid_vix_maps_to_normal_iv() -> None:
    assert map_to_kamandal_regime("mid", "flat", "normal") == MarketRegime.NORMAL_IV


def test_low_vix_maps_to_low_iv() -> None:
    assert map_to_kamandal_regime("low", "up", "normal") == MarketRegime.LOW_IV


def test_opex_session_maps_to_event_risk() -> None:
    assert map_to_kamandal_regime("mid", "up", "opex") == MarketRegime.EVENT_RISK


def test_post_fed_session_maps_to_event_risk() -> None:
    assert map_to_kamandal_regime("low", "flat", "post_fed") == MarketRegime.EVENT_RISK


def test_earnings_heavy_session_maps_to_event_risk() -> None:
    assert (
        map_to_kamandal_regime("mid", "up", "earnings_heavy") == MarketRegime.EVENT_RISK
    )


def test_high_vix_flat_trend_is_high_iv_not_event_risk() -> None:
    assert map_to_kamandal_regime("high", "flat", "normal") == MarketRegime.HIGH_IV


# ── Payload parsing ─────────────────────────────────────────────────────────


def test_parse_valid_payload() -> None:
    payload = (
        '{"trading_date": "2026-04-13", "vix_band": "mid", '
        '"spy_trend_20d": "up", "session_type": "normal", '
        '"vix_close": 17.5, "spy_close": 535.0, "spy_sma20": 530.0, '
        '"spy_trend_slope_pct": 0.1}'
    )
    snap = _parse_regime_payload(payload)
    assert snap is not None
    assert snap.trading_date == date(2026, 4, 13)
    assert snap.vix_band == "mid"
    assert snap.kamandal_regime == MarketRegime.NORMAL_IV
    assert snap.vix_close == 17.5


def test_parse_invalid_json_returns_none() -> None:
    assert _parse_regime_payload("not json") is None


def test_parse_missing_date_returns_none() -> None:
    assert _parse_regime_payload('{"vix_band": "mid"}') is None


def test_parse_empty_string_returns_none() -> None:
    assert _parse_regime_payload("") is None


# ── Row finding ─────────────────────────────────────────────────────────────


def test_find_todays_row_matches_correct_date() -> None:
    rows = [
        {"filename": "regime-2026-04-12.json", "payload_json": "{}"},
        {"filename": "regime-2026-04-13.json", "payload_json": '{"good": true}'},
        {"filename": "catalog_regime_performance.json", "payload_json": "{}"},
    ]
    row = _find_todays_regime_row(rows, target_date=date(2026, 4, 13))
    assert row is not None
    assert row["filename"] == "regime-2026-04-13.json"


def test_find_todays_row_returns_none_when_missing() -> None:
    rows = [{"filename": "regime-2026-04-12.json", "payload_json": "{}"}]
    assert _find_todays_regime_row(rows, target_date=date(2026, 4, 13)) is None


# ── BridgeRegimeEvaluator ──────────────────────────────────────────────────


def test_bridge_evaluator_uses_snapshot_regime() -> None:
    snapshot = RegimeSnapshot(
        trading_date=date(2026, 4, 13),
        vix_band="high",
        spy_trend_20d="up",
        session_type="normal",
        kamandal_regime=MarketRegime.HIGH_IV,
    )
    config = {
        "portfolio": {
            "regimes": {
                "high_iv": {
                    "prefer_structures": ["short_strangle"],
                    "avoid_structures": [],
                    "allow_undefined_risk": True,
                    "reject_event_risk": True,
                },
                "normal_iv": {
                    "prefer_structures": ["short_put"],
                    "avoid_structures": [],
                    "allow_undefined_risk": True,
                    "reject_event_risk": True,
                },
            }
        }
    }
    evaluator = BridgeRegimeEvaluator(config, snapshot=snapshot)

    regime = evaluator.determine_regime()
    assert regime == MarketRegime.HIGH_IV

    policy = evaluator.get_policy(regime)
    assert isinstance(policy, RegimePolicy)
    assert "short_strangle" in policy.prefer_structures


def test_bridge_evaluator_returns_unknown_without_snapshot() -> None:
    evaluator = BridgeRegimeEvaluator({"portfolio": {"regimes": {}}}, snapshot=None)
    assert evaluator.determine_regime() == MarketRegime.UNKNOWN
