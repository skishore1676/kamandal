from datetime import date

from vol_crush.week_replay.service import _default_replay_window, _replay_config


def test_default_replay_window_uses_most_recent_monday_friday_week():
    start, end = _default_replay_window(date(2026, 4, 25))

    assert start == date(2026, 4, 20)
    assert end == date(2026, 4, 24)


def test_replay_config_uses_isolated_shadow_store(tmp_path):
    config = {
        "storage": {"local": {"sqlite_path": "data/kamandal.db"}},
        "google_sheets": {"enabled": True},
        "execution": {"mode": "live"},
        "broker": {"active": "public"},
    }

    replay = _replay_config(config, run_id="replay_test", replay_root=tmp_path)

    assert replay["storage"]["local"]["sqlite_path"] == str(tmp_path / "kamandal.db")
    assert replay["storage"]["local"]["audit_dir"] == str(tmp_path / "audit")
    assert replay["data_sources"]["fixtures"]["bundle_path"] == str(
        tmp_path / "fixtures" / "fixture_bundle.json"
    )
    assert replay["data_sources"]["fixtures"]["replay_path"] == str(
        tmp_path / "fixtures" / "replay_trades.json"
    )
    assert replay["google_sheets"]["enabled"] is False
    assert replay["execution"]["mode"] == "shadow"
    assert replay["execution"]["bypass_daily_plan_approval"] is True
    assert replay["execution"]["auto_approve_ideas"] is True
    assert replay["broker"]["active"] == "replay"
    assert config["execution"]["mode"] == "live"
