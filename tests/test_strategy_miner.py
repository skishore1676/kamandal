"""Tests for the Strategy Miner pipeline (Module 0).

Uses mock LLM responses to test the full extract -> distill -> save flow
without requiring a real OpenAI API key.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import asdict

import pytest

from vol_crush.core.models import ExtractedStrategyCandidate
from vol_crush.integrations.llm import LLMClient
from vol_crush.strategy_miner.extractor import (
    load_transcripts,
    extract_from_transcript,
    extract_all,
    candidates_to_json,
    save_candidates,
)
from vol_crush.strategy_miner.distiller import (
    distill_strategies,
    build_strategy_objects,
)

# ── Fixtures ──────────────────────────────────────────────────────────

MOCK_EXTRACTION_RESPONSE = {
    "strategies": [
        {
            "trader_name": "Mike Butler",
            "show_name": "Bootstrappers",
            "strategy_name": "Core Short Strangle",
            "structure": "short_strangle",
            "description": "Sell 16-delta strangles on SPY and IWM, 45 DTE",
            "underlyings": ["SPY", "IWM"],
            "iv_rank_filter": "above 20, ideally above 30",
            "dte_preference": "45 DTE",
            "delta_targets": "16 delta each side",
            "spread_width": "",
            "profit_target": "50% of max profit",
            "loss_management": "2x credit received",
            "roll_rules": "21 DTE, roll for credit",
            "position_sizing": "max 25% BPR per underlying, 50% total",
            "allocation_notes": "",
            "win_rate_claimed": "80% at 50% management",
            "annual_return_claimed": "18%",
            "portfolio_greek_notes": "",
            "key_quotes": [
                "I close at 50% of max profit. That's non-negotiable.",
                "I never use more than 50% of my buying power on strangles total.",
            ],
            "confidence": "high",
        }
    ]
}

MOCK_DISTILLATION_RESPONSE = {
    "strategies": [
        {
            "id": "core_strangle",
            "name": "Core Short Strangle",
            "structure": "short_strangle",
            "description": "Sell 16-delta strangles on major ETFs, 45 DTE, manage at 50%",
            "filters": {
                "iv_rank_min": 25,
                "iv_rank_max": None,
                "dte_range": [30, 45],
                "delta_range": [0.14, 0.18],
                "spread_width": None,
                "min_credit_to_width_ratio": None,
                "underlyings": ["SPY", "IWM", "QQQ"],
            },
            "management": {
                "profit_target_pct": 50,
                "max_loss_multiple": 2.0,
                "roll_dte_trigger": 21,
                "roll_for_credit": True,
                "close_before_expiration": True,
            },
            "allocation": {
                "max_bpr_pct": 40,
                "max_per_position_pct": 15,
                "max_positions": 5,
            },
            "source_traders": ["Mike Butler", "Sarah Chen"],
            "consensus_notes": "Strong consensus on 16-delta, 45 DTE, 50% management.",
        },
        {
            "id": "etf_short_put",
            "name": "ETF Short Put",
            "structure": "short_put",
            "description": "Sell 20-delta puts on ETFs for bullish exposure with theta",
            "filters": {
                "iv_rank_min": 20,
                "iv_rank_max": None,
                "dte_range": [30, 45],
                "delta_range": [0.18, 0.22],
                "spread_width": None,
                "min_credit_to_width_ratio": None,
                "underlyings": ["SPY", "QQQ", "IWM", "TLT", "GLD"],
            },
            "management": {
                "profit_target_pct": 50,
                "max_loss_multiple": 2.0,
                "roll_dte_trigger": 21,
                "roll_for_credit": True,
                "close_before_expiration": True,
            },
            "allocation": {
                "max_bpr_pct": 35,
                "max_per_position_pct": 10,
                "max_positions": 6,
            },
            "source_traders": ["Sarah Chen", "James Okafor"],
            "consensus_notes": "Multiple traders use short puts as base layer.",
        },
    ],
    "portfolio_guidelines": {
        "beta_weighted_delta_pct": [-5.0, 5.0],
        "daily_theta_pct": [0.10, 0.25],
        "max_gamma_ratio": 1.5,
        "max_vega_pct": 2.0,
        "max_bpr_utilization_pct": 50.0,
        "hard_bpr_cap_pct": 60.0,
        "max_single_underlying_pct": 15.0,
        "notes": "Strong consensus from Jim Schultz and Sarah Chen on these levels.",
    },
}


@pytest.fixture
def mock_llm():
    """Create a mock LLM client."""
    llm = MagicMock(spec=LLMClient)
    return llm


@pytest.fixture
def sample_transcripts_dir(tmp_path):
    """Create a temp dir with sample transcripts."""
    t1 = tmp_path / "2025-01-01_test-show_trader-a.txt"
    t1.write_text("Tom: Welcome to the show.\nTrader A: I sell strangles on SPY.\n")

    t2 = tmp_path / "2025-01-02_test-show_trader-b.txt"
    t2.write_text("Tom: Tell us about your strategy.\nTrader B: I sell puts on QQQ.\n")

    # Non-txt file should be ignored
    (tmp_path / "notes.md").write_text("ignore me")

    # Empty file should be ignored
    (tmp_path / "empty.txt").write_text("")

    return tmp_path


# ── Tests: Extractor ─────────────────────────────────────────────────


def test_load_transcripts(sample_transcripts_dir):
    transcripts = load_transcripts(sample_transcripts_dir)
    assert len(transcripts) == 2
    assert transcripts[0][0] == "2025-01-01_test-show_trader-a.txt"
    assert transcripts[1][0] == "2025-01-02_test-show_trader-b.txt"
    assert "strangles" in transcripts[0][1]


def test_load_transcripts_empty_dir(tmp_path):
    transcripts = load_transcripts(tmp_path)
    assert transcripts == []


def test_extract_from_transcript(mock_llm):
    mock_llm.chat_json.return_value = MOCK_EXTRACTION_RESPONSE

    candidates = extract_from_transcript(mock_llm, "test.txt", "Some transcript text")

    assert len(candidates) == 1
    assert candidates[0].trader_name == "Mike Butler"
    assert candidates[0].strategy_name == "Core Short Strangle"
    assert candidates[0].structure == "short_strangle"
    assert candidates[0].confidence == "high"
    assert len(candidates[0].key_quotes) == 2

    mock_llm.chat_json.assert_called_once()


def test_extract_from_transcript_empty_response(mock_llm):
    mock_llm.chat_json.return_value = {"strategies": []}
    candidates = extract_from_transcript(mock_llm, "test.txt", "text")
    assert candidates == []


def test_extract_all(mock_llm, sample_transcripts_dir):
    mock_llm.chat_json.return_value = MOCK_EXTRACTION_RESPONSE
    candidates = extract_all(mock_llm, sample_transcripts_dir)
    # 2 transcripts * 1 strategy each
    assert len(candidates) == 2
    assert mock_llm.chat_json.call_count == 2


def test_extract_all_handles_llm_error(mock_llm, sample_transcripts_dir):
    """If one transcript fails, the others should still be processed."""
    mock_llm.chat_json.side_effect = [
        Exception("API error"),
        MOCK_EXTRACTION_RESPONSE,
    ]
    candidates = extract_all(mock_llm, sample_transcripts_dir)
    assert len(candidates) == 1  # only second transcript succeeded


def test_candidates_to_json():
    candidate = ExtractedStrategyCandidate(
        source_file="test.txt",
        trader_name="Tom",
        show_name="Show",
        strategy_name="Strat",
        structure="short_strangle",
        description="A strategy",
    )
    result = candidates_to_json([candidate])
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["trader_name"] == "Tom"


def test_save_candidates(tmp_path):
    candidate = ExtractedStrategyCandidate(
        source_file="test.txt",
        trader_name="Tom",
        show_name="Show",
        strategy_name="Strat",
        structure="short_strangle",
        description="A strategy",
    )
    out = tmp_path / "candidates.json"
    save_candidates([candidate], out)
    assert out.exists()
    data = json.loads(out.read_text())
    assert len(data) == 1
    assert data[0]["trader_name"] == "Tom"


# ── Tests: Distiller ─────────────────────────────────────────────────


def test_distill_strategies(mock_llm):
    mock_llm.chat_json.return_value = MOCK_DISTILLATION_RESPONSE

    candidates = [
        ExtractedStrategyCandidate(
            source_file="test.txt",
            trader_name="Mike",
            show_name="Show",
            strategy_name="Strangle",
            structure="short_strangle",
            description="test",
        )
    ]

    strategies_raw, guidelines = distill_strategies(mock_llm, candidates)

    assert len(strategies_raw) == 2
    assert strategies_raw[0]["id"] == "core_strangle"
    assert strategies_raw[1]["id"] == "etf_short_put"
    assert guidelines["max_gamma_ratio"] == 1.5
    assert guidelines["max_bpr_utilization_pct"] == 50.0

    mock_llm.chat_json.assert_called_once()


def test_build_strategy_objects():
    strategies_raw = MOCK_DISTILLATION_RESPONSE["strategies"]
    strategies = build_strategy_objects(strategies_raw)

    assert len(strategies) == 2
    assert strategies[0].id == "core_strangle"
    assert strategies[0].structure.value == "short_strangle"
    assert strategies[0].filters.iv_rank_min == 25
    assert strategies[0].filters.dte_range == (30, 45)
    assert strategies[0].management.profit_target_pct == 50
    assert strategies[0].allocation.max_bpr_pct == 40

    assert strategies[1].id == "etf_short_put"
    assert strategies[1].structure.value == "short_put"


# ── Tests: Full Pipeline ─────────────────────────────────────────────


def test_full_pipeline_extract_distill_save(mock_llm, sample_transcripts_dir, tmp_path):
    """End-to-end: extract from transcripts, distill, save to YAML."""
    # Mock extraction
    mock_llm.chat_json.side_effect = [
        MOCK_EXTRACTION_RESPONSE,  # transcript 1
        MOCK_EXTRACTION_RESPONSE,  # transcript 2
        MOCK_DISTILLATION_RESPONSE,  # distillation
    ]

    # Extract
    candidates = extract_all(mock_llm, sample_transcripts_dir)
    assert len(candidates) == 2

    # Save candidates
    candidates_path = tmp_path / "candidates.json"
    save_candidates(candidates, candidates_path)
    assert candidates_path.exists()

    # Distill
    strategies_raw, guidelines = distill_strategies(mock_llm, candidates)
    assert len(strategies_raw) == 2

    # Save strategies
    from vol_crush.core.config import save_strategies, load_strategies

    strat_path = tmp_path / "strategies.yaml"
    for s in strategies_raw:
        s["backtest_approved"] = False
        s["dry_run_passed"] = False
    save_strategies(strategies_raw, strat_path)

    # Reload and verify
    loaded = load_strategies(strat_path)
    assert len(loaded) == 2
    assert loaded[0]["id"] == "core_strangle"
    assert loaded[1]["id"] == "etf_short_put"
    assert loaded[0]["backtest_approved"] is False

    # Verify we can build Strategy objects from saved YAML
    from vol_crush.core.models import Strategy

    strat_objects = [Strategy.from_dict(s) for s in loaded]
    assert strat_objects[0].structure.value == "short_strangle"
    assert strat_objects[1].filters.delta_range == (0.18, 0.22)
