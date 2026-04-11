"""Tests for source adapters and source-driven idea extraction."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from vol_crush.core.models import RawSourceDocument, SourceType, TradeIdea
from vol_crush.idea_scraper.scraper import (
    dedupe_trade_ideas,
    extract_ideas_from_raw_documents,
)
from vol_crush.idea_sources.adapters import (
    TranscriptDirectoryAdapter,
    YouTubeChannelAdapter,
)
from vol_crush.idea_sources.fetcher import run_source_fetch
from vol_crush.integrations.storage import LocalStore


def test_transcript_directory_adapter_reads_documents(tmp_path):
    transcript = tmp_path / "sample.txt"
    transcript.write_text("Trader: Sell the SPY put spread.", encoding="utf-8")

    result = TranscriptDirectoryAdapter().fetch(tmp_path)

    assert len(result.documents) == 1
    assert result.documents[0].source_type == SourceType.TRANSCRIPT.value
    assert "SPY put spread" in result.documents[0].text


def test_youtube_adapter_extracts_transcript(monkeypatch):
    feed_xml = """
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <entry>
        <yt:videoId>abc123</yt:videoId>
        <title>SPY short put idea</title>
        <published>2026-04-02T14:00:00+00:00</published>
        <author><name>Trader A</name></author>
      </entry>
    </feed>
    """
    watch_html = """
    <html><head><title>SPY short put idea</title></head>
    <body>"shortDescription":"Selling the SPY put here"
    "baseUrl":"https:\\/\\/www.youtube.com\\/api\\/timedtext?v=abc123\\u0026lang=en"
    </body></html>
    """
    transcript_xml = (
        "<transcript><text>Sell the SPY put spread today</text></transcript>"
    )

    def fake_fetch(url, timeout=15):
        if "feeds/videos.xml" in url:
            return feed_xml
        if "timedtext" in url:
            return transcript_xml
        return watch_html

    monkeypatch.setattr("vol_crush.idea_sources.adapters.safe_fetch_url", fake_fetch)

    result = YouTubeChannelAdapter().fetch("test-channel", limit=1)

    assert len(result.documents) == 1
    assert result.documents[0].metadata["has_transcript"] is True
    assert "Sell the SPY put spread today" in result.documents[0].text


def test_extract_ideas_from_raw_documents_and_dedupe():
    llm = MagicMock()
    llm.chat_json.return_value = {
        "ideas": [
            {
                "trader_name": "Trader A",
                "show_name": "Show",
                "underlying": "SPY",
                "strategy_type": "short_put",
                "description": "Sell the SPY 45 DTE put",
                "expiration": "2026-05-15",
                "credit_target": "$2.10",
                "rationale": "IV elevated",
                "confidence": "high",
                "timestamp_approx": "12:00",
            }
        ]
    }
    docs = [
        RawSourceDocument(
            document_id="doc1",
            source_type=SourceType.WEB.value,
            source_name="web",
            title="SPY idea",
            url="https://example.com/1",
            text="Sell the SPY put",
        ),
        RawSourceDocument(
            document_id="doc2",
            source_type=SourceType.WEB.value,
            source_name="web",
            title="SPY idea duplicate",
            url="https://example.com/2",
            text="Sell the SPY put",
        ),
    ]

    ideas = extract_ideas_from_raw_documents(llm, docs)
    deduped = dedupe_trade_ideas(ideas)

    assert len(ideas) == 2
    assert len(deduped) == 1
    assert deduped[0].source_url == "https://example.com/1"


def test_run_source_fetch_transcripts_saves_raw_documents(tmp_path, monkeypatch):
    transcript = tmp_path / "sample.txt"
    transcript.write_text("Trader: Sell the SPY put spread.", encoding="utf-8")
    config = {
        "storage": {
            "local": {
                "sqlite_path": str(tmp_path / "vol_crush.db"),
                "audit_dir": str(tmp_path / "audit"),
            }
        },
        "idea_sources": {"transcripts": {"path": str(tmp_path)}},
    }

    documents, ideas, notes = run_source_fetch(
        config, "transcripts", extract_ideas=False
    )
    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
    )

    assert len(documents) == 1
    assert ideas == []
    assert len(store.list_raw_documents()) == 1
    assert any("fetched" in note for note in notes)
