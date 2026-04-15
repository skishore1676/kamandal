"""Tests for retry_transcripts service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vol_crush.core.models import RawSourceDocument, SourceType
from vol_crush.idea_sources.retry_transcripts import retry_missing_transcripts
from vol_crush.integrations.storage import LocalStore
from vol_crush.transcript_providers import ProviderChain, TranscriptFetch


class _FakeLLM:
    """Minimal LLM stub — same shape as LLMClient."""

    def __init__(self):
        self.provider = "test"
        self.model = "fake"

    def chat_json(self, system_prompt, user_prompt, **_):
        if "Summarize this transcript" in user_prompt:
            return {
                "headline": "short",
                "macro_view": "",
                "vol_view": "",
                "tickers": [],
                "strategies_discussed": [],
                "notable_quotes": [],
                "risks": "",
                "actionable_ideas_present": False,
            }
        return {
            "ideas": [
                {
                    "trader_name": "T",
                    "host": "H",
                    "show_name": "S",
                    "underlying": "SPY",
                    "strategy_type": "short_put",
                    "description": "Sell the SPY 45 DTE put",
                    "expiration": "2026-05-15",
                    "strikes": [450],
                    "credit_target": "1.00",
                    "rationale": "IV",
                    "confidence": "medium",
                    "timestamp_approx": "",
                }
            ]
        }


class _StaticProvider:
    def __init__(self, name, result):
        self.name = name
        self._result = result

    def supports(self, url, metadata):
        return True

    def fetch(self, url, metadata=None):
        return self._result


def _config(tmp_path):
    return {
        "storage": {
            "local": {
                "sqlite_path": str(tmp_path / "vol_crush.db"),
                "audit_dir": str(tmp_path / "audit"),
            }
        },
        "idea_sources": {
            "transcripts_archive": {"path": str(tmp_path / "archive"), "retention_days": 14},
            "summaries_archive": {"path": str(tmp_path / "ideas")},
            "transcripts": {"retry": {"min_age_hours": 20, "max_age_hours": 168}},
        },
    }


def _seed_doc(
    tmp_path,
    *,
    video_id: str,
    has_transcript: bool,
    published_at: str,
    text: str = "",
) -> RawSourceDocument:
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
    )
    doc = RawSourceDocument(
        document_id=f"doc_{video_id}",
        source_type=SourceType.YOUTUBE.value,
        source_name="youtube:ch",
        title=f"title-{video_id}",
        author="tastylive",
        published_at=published_at,
        url=f"https://www.youtube.com/watch?v={video_id}",
        text=text,
        metadata={"video_id": video_id, "has_transcript": has_transcript},
    )
    store.save_raw_documents([doc])
    return doc


def test_retry_skips_doc_still_in_cooldown_window(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _seed_doc(
        tmp_path,
        video_id="fresh",
        has_transcript=False,
        published_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    monkeypatch.setattr(
        "vol_crush.idea_sources.retry_transcripts.build_llm_client",
        lambda cfg: _FakeLLM(),
    )
    chain = ProviderChain(
        [_StaticProvider("captions", TranscriptFetch(provider="captions", text="t"))]
    )
    report = retry_missing_transcripts(config, chain=chain)
    assert report.considered == 1
    assert report.skipped_too_young == 1
    assert report.recovered_documents == []


def test_retry_recovers_doc_and_triggers_summary_and_ideas(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _seed_doc(
        tmp_path,
        video_id="ready",
        has_transcript=False,
        published_at=(datetime.now(UTC) - timedelta(hours=30)).isoformat(),
    )
    monkeypatch.setattr(
        "vol_crush.idea_sources.retry_transcripts.build_llm_client",
        lambda cfg: _FakeLLM(),
    )
    recovered_text = "this is the transcript body"
    chain = ProviderChain(
        [
            _StaticProvider(
                "captions",
                TranscriptFetch(
                    provider="captions", text=recovered_text, language="en"
                ),
            )
        ]
    )

    report = retry_missing_transcripts(config, chain=chain)

    assert report.considered == 1
    assert len(report.recovered_documents) == 1
    assert report.new_ideas == 1

    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
    )
    updated = store.list_raw_documents()[0]
    assert updated.metadata["has_transcript"] is True
    assert updated.metadata["transcript_provider"] == "captions"
    assert updated.text == recovered_text

    ideas = store.list_trade_ideas()
    assert len(ideas) == 1
    assert ideas[0].underlying == "SPY"

    archive_file = next((tmp_path / "archive").rglob("ready.txt"), None)
    assert archive_file is not None
    assert archive_file.read_text(encoding="utf-8") == recovered_text

    summaries = list((tmp_path / "ideas").rglob("ready_summary.md"))
    assert summaries


def test_retry_dry_run_does_not_mutate_db(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _seed_doc(
        tmp_path,
        video_id="ready",
        has_transcript=False,
        published_at=(datetime.now(UTC) - timedelta(hours=30)).isoformat(),
    )
    monkeypatch.setattr(
        "vol_crush.idea_sources.retry_transcripts.build_llm_client",
        lambda cfg: _FakeLLM(),
    )
    chain = ProviderChain(
        [_StaticProvider("captions", TranscriptFetch(provider="captions", text="x"))]
    )
    report = retry_missing_transcripts(config, chain=chain, dry_run=True)
    assert len(report.recovered_documents) == 1

    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
    )
    doc = store.list_raw_documents()[0]
    assert doc.metadata["has_transcript"] is False  # unchanged
    assert store.list_trade_ideas() == []


def test_retry_skips_docs_that_already_have_transcript(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _seed_doc(
        tmp_path,
        video_id="done",
        has_transcript=True,
        published_at=(datetime.now(UTC) - timedelta(hours=30)).isoformat(),
        text="already transcribed",
    )
    monkeypatch.setattr(
        "vol_crush.idea_sources.retry_transcripts.build_llm_client",
        lambda cfg: _FakeLLM(),
    )
    # Chain that would succeed, but the doc shouldn't be handed to it.
    chain = ProviderChain(
        [_StaticProvider("captions", TranscriptFetch(provider="captions", text="x"))]
    )
    report = retry_missing_transcripts(config, chain=chain)
    assert report.skipped_already_has_transcript == 1
    assert report.considered == 0
