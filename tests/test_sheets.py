"""Tests for the Google Sheet control-plane module.

The gspread client is never exercised against a live sheet — sync functions
take an injected client or read/write the local JSON cache.
"""

from __future__ import annotations

import json
from pathlib import Path
from vol_crush.sheets.schemas import (
    AuthorizationMode,
    IdeaApproval,
    IdeaReviewRow,
    StrategyApprovalRow,
)


# ── Schema parsing / round-trip ──────────────────────────────────────


def test_strategy_row_parses_aliases_and_booleans():
    raw = {
        "row_id": "spy::index_etf",
        "enabled": "TRUE",
        "template": "short_put_spread",  # alias for template_id
        "profile_id": "index_etf",
        "mode": "live",  # alias for authorization_mode
        "backtest_approved": "yes",
        "dry_run_passed": "1",
        "max_bpr_pct_override": "15",
        "max_positions_override": "3",
        "reason": "hand-approved after 5 shadow days",  # alias for approval_reason
        "approved_by": "sunny",
    }
    row = StrategyApprovalRow.from_row(raw)
    assert row.enabled is True
    assert row.template_id == "short_put_spread"
    assert row.profile_id == "index_etf"
    assert row.authorization_mode == AuthorizationMode.LIVE
    assert row.backtest_approved is True
    assert row.dry_run_passed is True
    assert row.max_bpr_pct_override == 15.0
    assert row.max_positions_override == 3
    assert row.approval_reason.startswith("hand-approved")
    assert row.is_live_eligible() is True


def test_strategy_row_not_live_eligible_unless_all_conditions_hold():
    base = {
        "enabled": "TRUE",
        "template_id": "t",
        "profile_id": "p",
        "authorization_mode": "live",
        "backtest_approved": "TRUE",
        "dry_run_passed": "TRUE",
    }
    assert StrategyApprovalRow.from_row(base).is_live_eligible()
    assert not StrategyApprovalRow.from_row({**base, "enabled": "FALSE"}).is_live_eligible()
    assert not StrategyApprovalRow.from_row(
        {**base, "authorization_mode": "shadow"}
    ).is_live_eligible()
    assert not StrategyApprovalRow.from_row(
        {**base, "backtest_approved": "FALSE"}
    ).is_live_eligible()
    assert not StrategyApprovalRow.from_row(
        {**base, "dry_run_passed": "FALSE"}
    ).is_live_eligible()


def test_strategy_row_round_trip_to_row_and_back():
    original = StrategyApprovalRow(
        row_id="r",
        enabled=True,
        template_id="short_put",
        profile_id="bond_etf",
        authorization_mode=AuthorizationMode.LIVE,
        backtest_approved=True,
        dry_run_passed=True,
        max_bpr_pct_override=20.0,
        max_positions_override=2,
        approved_by="sunny",
        approval_reason="reason",
        notes="note",
    )
    cells = original.to_row()
    header = list(StrategyApprovalRow.HEADER)
    recovered = StrategyApprovalRow.from_row(dict(zip(header, cells)))
    assert recovered.enabled is True
    assert recovered.authorization_mode == AuthorizationMode.LIVE
    assert recovered.max_bpr_pct_override == 20.0
    assert recovered.max_positions_override == 2
    assert recovered.template_id == "short_put"


def test_idea_review_row_parses_strikes_from_string():
    raw = {
        "idea_id": "idea_123",
        "underlying": "spy",
        "strategy_type": "short_put",
        "strikes": "450 / 440",
        "approval": "approve",
    }
    row = IdeaReviewRow.from_row(raw)
    assert row.underlying == "SPY"
    assert row.strikes == [450.0, 440.0]
    assert row.approval == IdeaApproval.APPROVED


def test_idea_review_row_unknown_approval_falls_back_to_pending():
    raw = {"idea_id": "x", "approval": "banana"}
    assert IdeaReviewRow.from_row(raw).approval == IdeaApproval.PENDING


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


def test_bootstrap_populates_all_four_tabs(tmp_path, monkeypatch):
    spreadsheet = _FakeSpreadsheet("kamandal_control")
    _install_fake_client(monkeypatch, spreadsheet)

    from vol_crush.sheets.sync import bootstrap_sheet

    notes = bootstrap_sheet(_fake_client_config(tmp_path))

    titles = [ws.title for ws in spreadsheet.worksheets()]
    for expected in ("strategies", "idea_review", "daily_plan", "positions"):
        assert expected in titles
    assert "Sheet1" not in titles  # removed once other tabs exist

    strategies_rows = spreadsheet.worksheet("strategies").get_all_values()
    assert strategies_rows[0] == list(StrategyApprovalRow.HEADER)
    assert strategies_rows[1][2] == "short_put_spread"  # example template_id
    assert any("strategies:" in n for n in notes)


def test_pull_stamps_approval_metadata_and_caches(tmp_path, monkeypatch):
    spreadsheet = _FakeSpreadsheet("kamandal_control")
    _install_fake_client(monkeypatch, spreadsheet)

    # Pre-populate the strategies tab with one fully-approved live row.
    strategies = spreadsheet.add_worksheet(title="strategies")
    strategies.update(
        range_name="A1",
        values=[
            list(StrategyApprovalRow.HEADER),
            [
                "spy::index_etf",
                "TRUE",
                "short_put_spread",
                "index_etf",
                "live",
                "TRUE",
                "TRUE",
                "",
                "",
                "",
                "5 shadow days clean",
                "",
                "",
            ],
        ],
        value_input_option="USER_ENTERED",
    )

    # And the idea_review tab with one pending row.
    idea_review = spreadsheet.add_worksheet(title="idea_review")
    idea_review.update(
        range_name="A1",
        values=[
            list(IdeaReviewRow.HEADER),
            [
                "idea_1",
                "2026-04-15",
                "SPY",
                "short_put",
                "sell SPY 45dte put",
                "450",
                "2026-05-30",
                "high",
                "Tom",
                "vidX",
                "https://example.com",
                "approve",  # operator newly approved
                "looks good",
                "",
                "",  # reviewed_at missing → should get stamped
            ],
        ],
        value_input_option="USER_ENTERED",
    )

    from vol_crush.sheets.sync import pull_sheet

    config = _fake_client_config(tmp_path)
    report = pull_sheet(config)

    assert report.strategies.rows_fetched == 1
    assert report.strategies.stamped_rows == 1  # new live-eligible → stamped
    # approved_at now on the sheet
    new_rows = spreadsheet.worksheet("strategies").get_all_values()
    approved_at_col = StrategyApprovalRow.HEADER.index("approved_at")
    assert new_rows[1][approved_at_col]  # non-empty

    assert report.idea_review.rows_fetched == 1
    assert report.idea_review.stamped_rows == 1
    review_rows = spreadsheet.worksheet("idea_review").get_all_values()
    reviewed_at_col = IdeaReviewRow.HEADER.index("reviewed_at")
    assert review_rows[1][reviewed_at_col]

    # JSON cache has hash + parsed rows
    strategies_cache = json.loads(
        (Path(tmp_path) / "sheet_cache" / "strategies.json").read_text()
    )
    assert strategies_cache["rows"][0]["is_live_eligible"] is True
    assert strategies_cache["hash"]
    assert strategies_cache["rows"][0]["approved_by"]  # auto-filled

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
            [
                "idea_1",
                "2026-04-15",
                "SPY",
                "short_put",
                "sell SPY put",
                "450",
                "2026-05-30",
                "high",
                "Tom",
                "vidX",
                "http://u",
                "approve",
                "op-note",
                "sunny",
                "2026-04-15T10:00:00Z",
            ],
        ],
        value_input_option="USER_ENTERED",
    )

    from vol_crush.sheets.sync import push_idea_review

    new_rows = [
        IdeaReviewRow(
            idea_id="idea_1",
            date="2026-04-15",
            underlying="SPY",
            strategy_type="short_put",
            description="FRESHER DESCRIPTION",  # app overwrites
            strikes=[450.0],
            expiration="2026-05-30",
            confidence="high",
            host="Tom",
            video_id="vidX",
            source_url="http://u",
            approval=IdeaApproval.PENDING,  # app pushes as pending
            operator_notes="",
            reviewed_by="",
            reviewed_at="",
        ),
        IdeaReviewRow(
            idea_id="idea_2",
            date="2026-04-15",
            underlying="QQQ",
            strategy_type="short_put",
            description="new idea",
            approval=IdeaApproval.PENDING,
        ),
    ]
    push_idea_review(_fake_client_config(tmp_path), new_rows)

    all_rows = spreadsheet.worksheet("idea_review").get_all_values()
    # Header + 2 data rows
    assert len(all_rows) == 3
    # Row 1: app description overwrote, but approval/notes/reviewer stayed
    assert all_rows[1][4] == "FRESHER DESCRIPTION"
    assert all_rows[1][11] == "approve"
    assert all_rows[1][12] == "op-note"
    assert all_rows[1][13] == "sunny"
    # Row 2 is the new idea
    assert all_rows[2][0] == "idea_2"


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
                        "row_id": "spy::idx",
                        "enabled": True,
                        "template_id": "short_put_spread",
                        "profile_id": "index_etf",
                        "authorization_mode": "live",
                        "backtest_approved": True,
                        "dry_run_passed": True,
                        "max_bpr_pct_override": None,
                        "max_positions_override": 3,
                        "approved_by": "sunny",
                        "approval_reason": "5 shadow days",
                        "approved_at": "2026-04-15T00:00:00Z",
                        "notes": "",
                        "is_live_eligible": True,
                        "identity_key": "short_put_spread::index_etf",
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
    # Overlay flipped approval flags on the first one.
    assert s_ok.backtest_approved is True
    assert s_ok.dry_run_passed is True
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
                        "template_id": "short_put_spread",
                        "profile_id": "index_etf",
                        "enabled": True,
                        "authorization_mode": "shadow",  # row-level downgrade
                        "backtest_approved": True,
                        "dry_run_passed": True,
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


def test_idea_review_gate_permissive_in_shadow_mode(tmp_path):
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
    assert [i.id for i in kept] == ["x"]
    assert notes == []
