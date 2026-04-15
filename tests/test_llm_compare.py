"""Tests for the LLM comparison harness."""

import json

import pytest

from vol_crush.core.models import RawSourceDocument, SourceType
from vol_crush.idea_sources.transcript_archive import write_transcript
from vol_crush.llm_compare.service import run_comparison


class _FakeLLM:
    """Minimal LLMClient stand-in for the comparison tests."""

    def __init__(self, provider, model, **_kwargs):
        self.provider = provider
        self.model = model
        self.fallback_model = None

    def chat_json(self, system_prompt, user_prompt, **_):
        if "Summarize this transcript" in user_prompt:
            return {
                "headline": f"summary from {self.model}",
                "macro_view": "range-bound",
                "vol_view": "IVR elevated",
                "tickers": [{"ticker": "SPY", "bias": "neutral", "notes": ""}],
                "strategies_discussed": ["short strangles"],
                "notable_quotes": [],
                "risks": "",
                "actionable_ideas_present": True,
            }
        return {
            "ideas": [
                {
                    "trader_name": "Trader",
                    "host": "Host",
                    "show_name": "Test",
                    "underlying": "SPY",
                    "strategy_type": "short_put",
                    "description": f"{self.model} — sell SPY 45dte put",
                    "expiration": "2026-05-15",
                    "strikes": [450],
                    "credit_target": "1.50",
                    "rationale": "IVR high",
                    "confidence": "medium",
                    "timestamp_approx": "10:00",
                }
            ]
        }


def test_run_comparison_writes_report(tmp_path, monkeypatch):
    archive_root = tmp_path / "archive"
    output_root = tmp_path / "out"

    doc = RawSourceDocument(
        document_id="doc1",
        source_type=SourceType.YOUTUBE.value,
        source_name="youtube:ch",
        title="SPY premium selling",
        author="tastylive",
        url="https://youtu.be/abc",
        text="We should sell 45 DTE SPY puts today because IV rank is 60.",
        metadata={"video_id": "abc"},
    )
    write_transcript(archive_root, doc)

    monkeypatch.setattr("vol_crush.llm_compare.service.LLMClient", _FakeLLM)

    md_path = run_comparison(
        video_id="abc",
        models=["alpha/model-1", "beta/model-2"],
        api_key="fake",
        provider="openrouter",
        archive_root=archive_root,
        output_root=output_root,
    )

    assert md_path.exists()
    text = md_path.read_text(encoding="utf-8")
    assert "alpha/model-1" in text
    assert "beta/model-2" in text
    assert "summary from alpha/model-1" in text
    assert "sell SPY 45dte put" in text

    json_path = md_path.with_suffix(".json")
    payload = json.loads(json_path.read_text())
    assert len(payload["results"]) == 2
    assert payload["results"][0]["ideas"]
    assert payload["results"][0]["summary"]["headline"] == "summary from alpha/model-1"


def test_run_comparison_missing_archive_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("vol_crush.llm_compare.service.LLMClient", _FakeLLM)
    with pytest.raises(FileNotFoundError):
        run_comparison(
            video_id="missing",
            models=["foo/bar"],
            api_key="fake",
            provider="openrouter",
            archive_root=tmp_path / "does-not-exist",
            output_root=tmp_path / "out",
        )
