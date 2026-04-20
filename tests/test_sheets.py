"""Tests for the Google Sheet control-plane module.

The gspread client is never exercised against a live sheet — sync functions
take an injected client or read/write the local JSON cache.
"""

from __future__ import annotations

import json
from pathlib import Path
from vol_crush.sheets.schemas import (
    AuthorizationMode,
    DailyPlanRow,
    IdeaApproval,
    IdeaReviewRow,
    ProfileConfigRow,
    RegimeControlRow,
    StrategyApprovalRow,
    TemplateLibraryRow,
    UniverseMemberRow,
)


# ── Schema parsing / round-trip ──────────────────────────────────────


def test_strategy_row_parses_aliases_and_booleans():
    raw = {
        "row_id": "put_vertical::index_etf",
        "enabled": "TRUE",
        "strategy_id": "put_vertical",
        "stock_profile": "index_etf",
        "allowed_regimes": "high_iv, normal_iv",
        "iv_rank_min": "25",
        "avoid_earnings": "TRUE",
        "mode": "live",  # alias for authorization_mode
        "max_bpr_pct_override": "15",
        "max_positions_override": "3",
    }
    row = StrategyApprovalRow.from_row(raw)
    assert row.enabled is True
    assert row.strategy_id == "put_vertical"
    assert row.stock_profile == "index_etf"
    assert row.authorization_mode == AuthorizationMode.LIVE
    assert row.allowed_regimes == ["high_iv", "normal_iv"]
    assert row.iv_rank_min == 25.0
    assert row.avoid_earnings is True
    assert row.max_bpr_pct_override == 15.0
    assert row.max_positions_override == 3
    assert row.is_live_eligible() is True


def test_strategy_row_not_live_eligible_unless_all_conditions_hold():
    base = {
        "enabled": "TRUE",
        "strategy_id": "t",
        "stock_profile": "p",
        "authorization_mode": "live",
    }
    assert StrategyApprovalRow.from_row(base).is_live_eligible()
    assert not StrategyApprovalRow.from_row({**base, "enabled": "FALSE"}).is_live_eligible()
    assert not StrategyApprovalRow.from_row(
        {**base, "authorization_mode": "shadow"}
    ).is_live_eligible()


def test_strategy_row_round_trip_to_row_and_back():
    original = StrategyApprovalRow(
        row_id="r",
        enabled=True,
        strategy_id="short_put",
        stock_profile="bond_etf",
        authorization_mode=AuthorizationMode.LIVE,
        max_bpr_pct_override=20.0,
        max_positions_override=2,
    )
    cells = original.to_row()
    header = list(StrategyApprovalRow.HEADER)
    recovered = StrategyApprovalRow.from_row(dict(zip(header, cells)))
    assert recovered.enabled is True
    assert recovered.authorization_mode == AuthorizationMode.LIVE
    assert recovered.strategy_id == "short_put"
    assert recovered.stock_profile == "bond_etf"
    assert recovered.identity_key() == "r"


def test_idea_review_row_parses_strikes_from_string():
    raw = {"underlying": "spy", "proposed_strategy": "put_vertical", "approval": "approve"}
    row = IdeaReviewRow.from_row(raw)
    assert row.underlying == "SPY"
    assert row.proposed_strategy == "put_vertical"
    assert row.strategy_type == "put_spread"
    assert row.approval == IdeaApproval.APPROVED


def test_idea_review_row_unknown_approval_falls_back_to_pending():
    raw = {"idea_id": "x", "approval": "banana"}
    assert IdeaReviewRow.from_row(raw).approval == IdeaApproval.PENDING


def test_strategy_row_emits_visible_strategy_key():
    row = StrategyApprovalRow(
        strategy_id="put_vertical",
        enabled=True,
        stock_profile="index_etf",
    )
    cells = row.to_row()
    assert cells[-1] == "put_vertical::index_etf"


def test_idea_review_row_emits_visible_debug_columns():
    row = IdeaReviewRow(
        idea_id="idea_123",
        date="2026-04-19",
        underlying="NVDA",
        expectation="bearish",
        proposed_strategy="call_vertical",
        note="Bear call spread",
        source_url="https://youtube.test/watch?v=abc",
        source_timestamp="12:34",
    )
    cells = row.to_row()
    assert cells[6:] == ["idea_123", "https://youtube.test/watch?v=abc", "12:34"]
    assert row.identity_key() == "idea_123"


def test_daily_plan_row_emits_visible_idea_id():
    row = DailyPlanRow(
        plan_id="plan_1",
        date="2026-04-19",
        underlying="NVDA",
        strategy="call_spread",
        approval="",
        note="candidate",
        idea_id="idea_123",
    )
    cells = row.to_row()
    assert cells[-1] == "idea_123"
    recovered = DailyPlanRow.from_row(dict(zip(DailyPlanRow.HEADER, cells)))
    assert recovered.idea_id == "idea_123"


def test_profile_and_universe_rows_round_trip():
    profile = ProfileConfigRow.from_row(
        {
            "stock_profile": "index_etf",
            "max_bpr_pct": "20",
            "max_per_position_pct": "10",
            "max_positions": "4",
            "earnings_sensitive": "FALSE",
        }
    )
    assert profile.stock_profile == "index_etf"
    assert profile.max_bpr_pct == 20.0
    assert profile.earnings_sensitive is False

    universe = UniverseMemberRow.from_row(
        {"symbol": "spy", "stock_profile": "index_etf", "enabled": "TRUE"}
    )
    assert universe.symbol == "SPY"
    assert universe.stock_profile == "index_etf"
    assert universe.enabled is True


def test_template_library_row_round_trip():
    row = TemplateLibraryRow.from_row(
        {
            "template_id": "put_spread_standard",
            "strategy_id": "put_vertical",
            "structure": "put_spread",
            "name": "Bull put spread",
            "allowed_regimes": "normal_iv, high_iv",
            "iv_rank_min": "22",
            "dte_min": "30",
            "dte_max": "45",
            "delta_min": "0.14",
            "delta_max": "0.18",
            "spread_width": "5",
            "profit_target_pct": "50",
            "roll_for_credit": "TRUE",
            "close_before_expiration": "TRUE",
            "avoid_earnings": "FALSE",
        }
    )
    assert row.template_id == "put_spread_standard"
    assert row.strategy_id == "put_vertical"
    assert row.structure == "put_spread"
    assert row.allowed_regimes == ["normal_iv", "high_iv"]
    assert row.iv_rank_min == 22.0
    assert row.roll_for_credit is True
    assert row.avoid_earnings is False
    assert row.to_row()[-1] == "put_spread_standard"


def test_regime_control_row_round_trip():
    row = RegimeControlRow.from_row(
        {
            "date": "2026-04-19",
            "regime": "normal_iv",
            "override_enabled": "TRUE",
            "note": "Operator override",
        }
    )
    assert row.date == "2026-04-19"
    assert row.regime == "normal_iv"
    assert row.override_enabled is True
    assert row.note == "Operator override"
    assert row.to_row() == ["2026-04-19", "normal_iv", "TRUE", "Operator override"]


# ── Bootstrap / pull / push with mocked sheet client ─────────────────


class _FakeWorksheet:
    """Records header + rows written; serves them back on read."""

    _next_id = 1

    def __init__(self, title: str, parent):
        self.title = title
        self._rows: list[list[str]] = []
        self.spreadsheet = parent
        self.id = _FakeWorksheet._next_id
        _FakeWorksheet._next_id += 1

    # gspread surface used by our client
    def get_all_values(self):
        return [list(row) for row in self._rows]

    def clear(self):
        self._rows = []

    def update(self, *, range_name, values, value_input_option=None):
        self._rows = [list(row) for row in values]


class _FakeSpreadsheet:
    def __init__(self, title: str):
        self.title = title
        self._worksheets: dict[str, _FakeWorksheet] = {}
        self._worksheets["Sheet1"] = _FakeWorksheet("Sheet1", self)
        self.batch_updates: list[dict] = []

    def worksheet(self, title: str):
        if title not in self._worksheets:
            raise RuntimeError(f"worksheet not found: {title}")
        return self._worksheets[title]

    def add_worksheet(self, *, title: str, rows: int = 200, cols: int = 26):
        ws = _FakeWorksheet(title, self)
        self._worksheets[title] = ws
        return ws

    def worksheets(self):
        return list(self._worksheets.values())

    def del_worksheet(self, worksheet):
        self._worksheets = {k: v for k, v in self._worksheets.items() if v is not worksheet}

    def batch_update(self, body):
        self.batch_updates.append(body)


def _fake_client_config(tmp_path: Path) -> dict:
    return {
        "google_sheets": {
            "credentials_file": "",
            "spreadsheet_id": "fake-id",
            "enabled": True,
            "cache_dir": str(tmp_path / "sheet_cache"),
            "tabs": {
                "strategies": "strategies",
                "template_library": "template_library",
                "regime_control": "regime_control",
                "profiles": "profiles",
                "universe": "universe",
                "idea_review": "idea_review",
                "daily_plan": "daily_plan",
                "positions": "positions",
            },
        }
    }


def _install_fake_client(monkeypatch, spreadsheet):
    from vol_crush.integrations import google_sheets as gs_module

    # Short-circuit the real GoogleSheetClient (which would import gspread +
    # Credentials in its __init__) by replacing the class on the sync module.
    class _FakeGSClient:
        def __init__(self, **_):
            self._spreadsheet = spreadsheet
            self.spreadsheet_id = "fake-id"
            self.title = spreadsheet.title

        @classmethod
        def from_config(cls, config):
            return cls()

        def get_worksheet(self, title, rows=200, cols=26):
            try:
                ws = self._spreadsheet.worksheet(title)
            except Exception:
                ws = self._spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
            return gs_module.WorksheetHandle(worksheet=ws, title=title)

        def worksheet_titles(self):
            return [ws.title for ws in self._spreadsheet.worksheets()]

        def delete_worksheet(self, title):
            try:
                ws = self._spreadsheet.worksheet(title)
            except Exception:
                return
            self._spreadsheet.del_worksheet(ws)

        def ensure_no_default_sheet1(self):
            titles = self.worksheet_titles()
            if "Sheet1" in titles and len(titles) > 1:
                self.delete_worksheet("Sheet1")

    from vol_crush.sheets import sync as sync_module

    monkeypatch.setattr(sync_module, "GoogleSheetClient", _FakeGSClient)
    return _FakeGSClient


def test_bootstrap_populates_all_control_tabs(tmp_path, monkeypatch):
    spreadsheet = _FakeSpreadsheet("kamandal_control")
    _install_fake_client(monkeypatch, spreadsheet)

    from vol_crush.sheets.sync import bootstrap_sheet

    notes = bootstrap_sheet(_fake_client_config(tmp_path))

    titles = [ws.title for ws in spreadsheet.worksheets()]
    for expected in (
        "strategies",
        "template_library",
        "regime_control",
        "profiles",
        "universe",
        "idea_review",
        "daily_plan",
    ):
        assert expected in titles
    assert "Sheet1" not in titles  # removed once other tabs exist

    strategies_rows = spreadsheet.worksheet("strategies").get_all_values()
    assert strategies_rows[0] == list(StrategyApprovalRow.HEADER)
    assert strategies_rows[1][0] == "put_vertical"
    assert any("strategies:" in n for n in notes)

    profiles_rows = spreadsheet.worksheet("profiles").get_all_values()
    regime_rows = spreadsheet.worksheet("regime_control").get_all_values()
    template_rows = spreadsheet.worksheet("template_library").get_all_values()
    universe_rows = spreadsheet.worksheet("universe").get_all_values()
    assert regime_rows[0] == list(RegimeControlRow.HEADER)
    assert template_rows[0] == list(TemplateLibraryRow.HEADER)
    assert profiles_rows[0] == list(ProfileConfigRow.HEADER)
    assert universe_rows[0] == list(UniverseMemberRow.HEADER)


def test_pull_stamps_approval_metadata_and_caches(tmp_path, monkeypatch):
    spreadsheet = _FakeSpreadsheet("kamandal_control")
    _install_fake_client(monkeypatch, spreadsheet)

    # Pre-populate the strategies tab with one fully-approved live row.
    strategies = spreadsheet.add_worksheet(title="strategies")
    strategies.update(
        range_name="A1",
        values=[
            list(StrategyApprovalRow.HEADER),
            ["put_vertical", "TRUE", "live", "index_etf"],
        ],
        value_input_option="USER_ENTERED",
    )

    # And the idea_review tab with one pending row.
    idea_review = spreadsheet.add_worksheet(title="idea_review")
    idea_review.update(
        range_name="A1",
        values=[
            list(IdeaReviewRow.HEADER),
            ["2026-04-15", "approve", "SPY", "bullish", "short_put", "sell SPY 45dte put"],
        ],
        value_input_option="USER_ENTERED",
    )

    from vol_crush.sheets.sync import pull_sheet

    config = _fake_client_config(tmp_path)
    report = pull_sheet(config)

    assert report.strategies.rows_fetched == 1
    assert report.strategies.stamped_rows == 0
    assert report.template_library.rows_fetched >= 0
    assert report.regime_control.rows_fetched == 0

    assert report.idea_review.rows_fetched == 1
    assert report.idea_review.stamped_rows == 0

    # JSON cache has hash + parsed rows
    strategies_cache = json.loads(
        (Path(tmp_path) / "sheet_cache" / "strategies.json").read_text()
    )
    assert strategies_cache["rows"][0]["is_live_eligible"] is True
    assert strategies_cache["hash"]
    assert strategies_cache["rows"][0]["strategy_id"] == "put_vertical"

    # Second pull with no changes → changed=False
    report2 = pull_sheet(config)
    assert report2.strategies.changed is False


def test_push_idea_review_preserves_operator_fields(tmp_path, monkeypatch):
    spreadsheet = _FakeSpreadsheet("kamandal_control")
    _install_fake_client(monkeypatch, spreadsheet)

    idea_review = spreadsheet.add_worksheet(title="idea_review")
    idea_review.update(
        range_name="A1",
        values=[
            list(IdeaReviewRow.HEADER),
            ["2026-04-15", "approve", "SPY", "neutral", "short_put", "sell SPY put"],
        ],
        value_input_option="USER_ENTERED",
    )

    from vol_crush.sheets.sync import push_idea_review

    new_rows = [
        IdeaReviewRow(
                idea_id="idea_1",
                date="2026-04-15",
                underlying="SPY",
                proposed_strategy="short_put",
                note="FRESHER DESCRIPTION",  # app overwrites
                approval=IdeaApproval.PENDING,  # app pushes as pending
            ),
            IdeaReviewRow(
                idea_id="idea_2",
                date="2026-04-15",
                underlying="QQQ",
                proposed_strategy="short_put",
                note="new idea",
                approval=IdeaApproval.PENDING,
            ),
    ]
    push_idea_review(_fake_client_config(tmp_path), new_rows)

    all_rows = spreadsheet.worksheet("idea_review").get_all_values()
    # Header + 2 data rows
    assert len(all_rows) == 3
    # Row 1: app description overwrote, but approval/notes/reviewer stayed
    assert all_rows[1][5] == "FRESHER DESCRIPTION"
    assert all_rows[1][1] == "approve"
    # Row 2 is the new idea
    assert all_rows[2][2] == "QQQ"


def test_push_idea_review_matches_legacy_row_without_visible_idea_id(tmp_path, monkeypatch):
    spreadsheet = _FakeSpreadsheet("kamandal_control")
    _install_fake_client(monkeypatch, spreadsheet)

    idea_review = spreadsheet.add_worksheet(title="idea_review")
    idea_review.update(
        range_name="A1",
        values=[
            ["date", "approval", "underlying", "expectation", "proposed_strategy", "note"],
            ["2026-04-15", "approve", "SPY", "neutral", "short_put", "sell SPY put"],
        ],
        value_input_option="USER_ENTERED",
    )

    from vol_crush.sheets.sync import push_idea_review

    push_idea_review(
        _fake_client_config(tmp_path),
        [
            IdeaReviewRow(
                idea_id="idea_legacy_match",
                date="2026-04-15",
                underlying="SPY",
                expectation="neutral",
                proposed_strategy="short_put",
                note="sell SPY put",
                approval=IdeaApproval.PENDING,
            )
        ],
    )

    all_rows = spreadsheet.worksheet("idea_review").get_all_values()
    assert len(all_rows) == 2
    assert all_rows[1][1] == "approve"
    assert all_rows[1][6] == "idea_legacy_match"


def test_pull_idea_review_restores_hidden_metadata(tmp_path, monkeypatch):
    spreadsheet = _FakeSpreadsheet("kamandal_control")
    _install_fake_client(monkeypatch, spreadsheet)

    idea_review = spreadsheet.add_worksheet(title="idea_review")
    idea_review.update(
        range_name="A1",
        values=[
            list(IdeaReviewRow.HEADER),
            ["2026-04-19", "", "NVDA", "bearish", "call_vertical", "Bear call spread"],
        ],
        value_input_option="USER_ENTERED",
    )

    cache_dir = tmp_path / "sheet_cache"
    cache_dir.mkdir()
    row_key = IdeaReviewRow.from_row(
        {
            "date": "2026-04-19",
            "underlying": "NVDA",
            "expectation": "bearish",
            "proposed_strategy": "call_vertical",
            "note": "Bear call spread",
        }
    ).identity_key()
    (cache_dir / "idea_review_metadata.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-04-19T00:00:00Z",
                "rows": {
                    row_key: {
                        "idea_id": "idea_123",
                        "source_url": "https://youtube.test/watch?v=abc",
                        "video_id": "abc",
                        "source_timestamp": "12:34",
                        "rationale": "Assignment-risk example",
                    }
                },
            }
        )
    )

    from vol_crush.sheets.sync import pull_sheet, read_idea_approvals_cache

    pull_sheet(_fake_client_config(tmp_path))
    rows = read_idea_approvals_cache(_fake_client_config(tmp_path))

    assert rows[0].idea_id == "idea_123"
    assert rows[0].source_url == "https://youtube.test/watch?v=abc"
    assert rows[0].video_id == "abc"
    assert rows[0].source_timestamp == "12:34"
    assert rows[0].rationale == "Assignment-risk example"


def test_load_strategy_objects_prefers_sheet_runtime_controls(tmp_path):
    from vol_crush.optimizer.service import load_strategy_objects

    cache_dir = tmp_path / "sheet_cache"
    cache_dir.mkdir()
    (cache_dir / "strategies.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "strategy_id": "short_put",
                        "enabled": True,
                        "authorization_mode": "shadow",
                        "stock_profile": "index_etf",
                    }
                ]
            }
        )
    )
    (cache_dir / "profiles.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "stock_profile": "index_etf",
                        "max_bpr_pct": 22.0,
                        "max_per_position_pct": 11.0,
                        "max_positions": 6,
                        "earnings_sensitive": False,
                    }
                ]
            }
        )
    )
    (cache_dir / "template_library.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "template_id": "short_put_conservative",
                        "strategy_id": "short_put",
                        "structure": "short_put",
                        "allowed_regimes": ["normal_iv"],
                        "iv_rank_min": 19.0,
                        "dte_min": 28,
                        "dte_max": 40,
                        "delta_min": 0.18,
                        "delta_max": 0.22,
                        "profit_target_pct": 55.0,
                        "avoid_earnings": False,
                    }
                ]
            }
        )
    )
    (cache_dir / "universe.json").write_text(
        json.dumps(
            {
                "rows": [
                    {"symbol": "SPY", "stock_profile": "index_etf", "enabled": True},
                    {"symbol": "QQQ", "stock_profile": "index_etf", "enabled": True},
                ]
            }
        )
    )
    config = {
        "google_sheets": {
            "enabled": True,
            "cache_dir": str(cache_dir),
        }
    }

    strategies = load_strategy_objects(config)

    short_put_idx = [
        strategy for strategy in strategies if strategy.id == "short_put_conservative:index_etf"
    ]
    assert len(short_put_idx) == 1
    assert short_put_idx[0].filters.underlyings == ["SPY", "QQQ"]
    assert short_put_idx[0].allowed_regimes == ["normal_iv"]
    assert short_put_idx[0].filters.iv_rank_min == 19.0
    assert short_put_idx[0].filters.dte_range == (28, 40)
    assert short_put_idx[0].management.profit_target_pct == 55.0
    assert short_put_idx[0].avoid_earnings is False
    assert short_put_idx[0].allocation.max_bpr_pct == 22.0
    assert short_put_idx[0].allocation.max_positions == 6


# ── Optimizer gate integration ───────────────────────────────────────


def test_load_approval_overlay_merges_into_optimizer(tmp_path):
    from vol_crush.core.models import Strategy, StrategyType
    from vol_crush.optimizer.service import _filter_strategies_for_execution

    cache_dir = tmp_path / "sheet_cache"
    cache_dir.mkdir()
    (cache_dir / "strategies.json").write_text(
        json.dumps(
            {
                "fetched_at": "2026-04-15T00:00:00Z",
                "hash": "xxx",
                "rows": [
                    {
                        "row_id": "put_vertical::index_etf",
                        "enabled": True,
                        "strategy_id": "put_vertical",
                        "stock_profile": "index_etf",
                        "authorization_mode": "live",
                        "max_bpr_pct_override": None,
                        "max_positions_override": 3,
                        "notes": "",
                        "is_live_eligible": True,
                        "identity_key": "put_spread::index_etf",
                    }
                ],
            }
        )
    )

    config = {
        "execution": {"mode": "live"},
        "google_sheets": {
            "enabled": True,
            "cache_dir": str(cache_dir),
            "credentials_file": "",
            "spreadsheet_id": "fake",
        },
    }
    s_ok = Strategy(
        id="short_put_spread:index_etf",
        name="x",
        structure=StrategyType.PUT_SPREAD,
    )
    s_missing = Strategy(
        id="short_call:bond_etf",
        name="x",
        structure=StrategyType.SHORT_CALL,
    )

    eligible, notes = _filter_strategies_for_execution([s_ok, s_missing], config)
    assert [s.id for s in eligible] == ["short_put_spread:index_etf"]
    assert s_ok.allocation.max_positions == 3
    assert any("no sheet approval row" in n for n in notes)


def test_row_level_shadow_downgrades_account_live(tmp_path):
    from vol_crush.core.models import Strategy, StrategyType
    from vol_crush.optimizer.service import _filter_strategies_for_execution

    cache_dir = tmp_path / "sheet_cache"
    cache_dir.mkdir()
    (cache_dir / "strategies.json").write_text(
        json.dumps(
            {
                "hash": "yy",
                "rows": [
                    {
                        "strategy_id": "put_vertical",
                        "stock_profile": "index_etf",
                        "enabled": True,
                        "authorization_mode": "shadow",  # row-level downgrade
                    }
                ],
            }
        )
    )
    config = {
        "execution": {"mode": "live"},
        "google_sheets": {
            "enabled": True,
            "cache_dir": str(cache_dir),
        },
    }
    s = Strategy(
        id="short_put_spread:index_etf",
        name="x",
        structure=StrategyType.PUT_SPREAD,
    )
    eligible, notes = _filter_strategies_for_execution([s], config)
    assert eligible == []
    assert any("authorization_mode=shadow" in n for n in notes)


def test_idea_review_gate_blocks_unapproved_in_live_mode(tmp_path):
    from vol_crush.core.models import IdeaStatus, TradeIdea
    from vol_crush.optimizer.service import _filter_ideas_for_execution

    cache_dir = tmp_path / "sheet_cache"
    cache_dir.mkdir()
    (cache_dir / "idea_review.json").write_text(
        json.dumps(
            {
                "rows": [
                    {"idea_id": "good", "approval": "approve"},
                    {"idea_id": "rejected", "approval": "reject"},
                ]
            }
        )
    )
    config = {
        "execution": {"mode": "live"},
        "google_sheets": {"enabled": True, "cache_dir": str(cache_dir)},
    }
    ideas = [
        TradeIdea(
            id=idea_id,
            date="2026-04-15",
            trader_name="",
            show_name="",
            underlying="SPY",
            strategy_type="short_put",
            description="",
            status=IdeaStatus.NEW.value,
        )
        for idea_id in ("good", "rejected", "missing")
    ]
    kept, notes = _filter_ideas_for_execution(ideas, config)
    assert [i.id for i in kept] == ["good"]
    assert any("rejected" in n for n in notes)
    assert any("missing" in n for n in notes)


def test_idea_review_gate_applies_in_shadow_mode(tmp_path):
    from vol_crush.core.models import IdeaStatus, TradeIdea
    from vol_crush.optimizer.service import _filter_ideas_for_execution

    config = {"execution": {"mode": "shadow"}, "google_sheets": {"enabled": True}}
    ideas = [
        TradeIdea(
            id="x",
            date="2026-04-15",
            trader_name="",
            show_name="",
            underlying="SPY",
            strategy_type="short_put",
            description="",
            status=IdeaStatus.NEW.value,
        )
    ]
    kept, notes = _filter_ideas_for_execution(ideas, config)
    assert kept == []
    assert any("no idea_review row" in n for n in notes)


def test_idea_review_gate_can_auto_approve_pending_and_missing_rows(tmp_path):
    from vol_crush.core.models import IdeaStatus, TradeIdea
    from vol_crush.optimizer.service import _filter_ideas_for_execution

    cache_dir = tmp_path / "sheet_cache"
    cache_dir.mkdir()
    (cache_dir / "idea_review.json").write_text(
        json.dumps({"rows": [{"idea_id": "pending", "approval": ""}]}),
        encoding="utf-8",
    )
    config = {
        "execution": {"mode": "shadow", "auto_approve_ideas": True},
        "google_sheets": {"enabled": True, "cache_dir": str(cache_dir)},
    }
    ideas = [
        TradeIdea(
            id="pending",
            date="2026-04-15",
            trader_name="",
            show_name="",
            underlying="SPY",
            strategy_type="short_put",
            description="",
            status=IdeaStatus.NEW.value,
        ),
        TradeIdea(
            id="missing",
            date="2026-04-15",
            trader_name="",
            show_name="",
            underlying="QQQ",
            strategy_type="short_put",
            description="",
            status=IdeaStatus.NEW.value,
        ),
    ]

    kept, notes = _filter_ideas_for_execution(ideas, config)
    assert [idea.id for idea in kept] == ["pending", "missing"]
    assert notes == []


def test_idea_review_gate_disabled_without_sheets(tmp_path):
    from vol_crush.core.models import IdeaStatus, TradeIdea
    from vol_crush.optimizer.service import _filter_ideas_for_execution

    config = {"execution": {"mode": "shadow"}, "google_sheets": {"enabled": False}}
    ideas = [
        TradeIdea(
            id="x",
            date="2026-04-15",
            trader_name="",
            show_name="",
            underlying="SPY",
            strategy_type="short_put",
            description="",
            status=IdeaStatus.NEW.value,
        )
    ]
    kept, notes = _filter_ideas_for_execution(ideas, config)
    assert [i.id for i in kept] == ["x"]
    assert notes == []
