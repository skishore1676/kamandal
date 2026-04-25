"""Tests for source adapters and source-driven idea extraction."""

from unittest.mock import MagicMock

from vol_crush.core.models import RawSourceDocument, SourceType
from vol_crush.idea_scraper.scraper import (
    dedupe_trade_ideas,
    extract_ideas_from_raw_documents,
)
from vol_crush.idea_sources.adapters import (
    TranscriptDirectoryAdapter,
    YouTubeChannelAdapter,
)
from vol_crush.idea_sources.fetcher import _dedupe_documents, run_source_fetch
from vol_crush.integrations.storage import LocalStore


def test_transcript_directory_adapter_reads_documents(tmp_path):
    transcript = tmp_path / "sample.txt"
    transcript.write_text("Trader: Sell the SPY put spread.", encoding="utf-8")

    result = TranscriptDirectoryAdapter().fetch(tmp_path)

    assert len(result.documents) == 1
    assert result.documents[0].source_type == SourceType.TRANSCRIPT.value
    assert "SPY put spread" in result.documents[0].text


def test_dedupe_documents_queues_stored_unextracted_duplicate():
    existing = RawSourceDocument(
        document_id="doc_existing",
        source_type=SourceType.YOUTUBE.value,
        source_name="youtube:ch",
        title="SPY idea",
        text="Sell the SPY put spread",
        fingerprint="fp1",
    )
    incoming = RawSourceDocument(
        document_id="doc_incoming",
        source_type=SourceType.YOUTUBE.value,
        source_name="youtube:ch",
        title="SPY idea",
        text="Sell the SPY put spread",
        fingerprint="fp1",
    )

    kept, duplicates, unextracted_existing = _dedupe_documents([existing], [incoming])

    assert kept == []
    assert duplicates == 1
    assert unextracted_existing == [existing]


def _build_feed_xml(entries):
    """Tiny helper — assemble a multi-entry Atom feed for tests."""
    body = "".join(f"""
        <entry>
          <yt:videoId>{e['video_id']}</yt:videoId>
          <title>{e['title']}</title>
          <published>{e.get('published', '2026-04-02T14:00:00+00:00')}</published>
          <author><name>{e.get('author', 'Trader A')}</name></author>
        </entry>
        """ for e in entries)
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:yt="http://www.youtube.com/xml/schemas/2015">' + body + "</feed>"
    )


class _FakeSnippet:
    def __init__(self, text):
        self.text = text


def _install_fake_transcript_api(monkeypatch, mapping):
    """Replace YouTubeTranscriptApi.fetch with a dict-driven stub."""
    import youtube_transcript_api

    class _FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self, video_id, languages=None):
            if video_id in mapping:
                return [_FakeSnippet(t) for t in mapping[video_id]]
            raise RuntimeError(f"no transcript for {video_id}")

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", _FakeApi)


def test_youtube_adapter_extracts_transcript(monkeypatch):
    feed_xml = _build_feed_xml([{"video_id": "abc123", "title": "SPY short put idea"}])

    def fake_fetch(url, timeout=15, max_attempts=3):
        if "feeds/videos.xml" in url:
            return feed_xml
        return '<html><body>"shortDescription":"Selling the SPY put here"</body></html>'

    monkeypatch.setattr("vol_crush.idea_sources.adapters.safe_fetch_url", fake_fetch)
    _install_fake_transcript_api(
        monkeypatch, {"abc123": ["Sell the SPY put spread today"]}
    )

    result = YouTubeChannelAdapter().fetch("test-channel", limit=1)

    assert len(result.documents) == 1
    assert result.documents[0].metadata["has_transcript"] is True
    assert "Sell the SPY put spread today" in result.documents[0].text


def test_youtube_adapter_title_filter_include(monkeypatch):
    feed_xml = _build_feed_xml(
        [
            {"video_id": "v1", "title": "SPY earnings strangle setup"},
            {"video_id": "v2", "title": "Harry Dent doom macro interview"},
        ]
    )
    monkeypatch.setattr(
        "vol_crush.idea_sources.adapters.safe_fetch_url",
        lambda url, timeout=15, max_attempts=3: (
            feed_xml if "feeds/videos.xml" in url else ""
        ),
    )
    _install_fake_transcript_api(monkeypatch, {"v1": ["Sell strangle"], "v2": ["Doom"]})

    result = YouTubeChannelAdapter().fetch(
        "ch", limit=5, title_include_keywords=["earnings", "strangle"]
    )

    assert [doc.metadata["video_id"] for doc in result.documents] == ["v1"]
    assert any("skipped 1 videos" in note for note in result.notes)


def test_youtube_adapter_title_filter_exclude(monkeypatch):
    feed_xml = _build_feed_xml(
        [
            {"video_id": "v1", "title": "Harry Dent interview macro"},
            {"video_id": "v2", "title": "SPY strangle 45 DTE"},
        ]
    )
    monkeypatch.setattr(
        "vol_crush.idea_sources.adapters.safe_fetch_url",
        lambda url, timeout=15, max_attempts=3: (
            feed_xml if "feeds/videos.xml" in url else ""
        ),
    )
    _install_fake_transcript_api(monkeypatch, {"v1": ["Interview"], "v2": ["Sell"]})

    result = YouTubeChannelAdapter().fetch(
        "ch", limit=5, title_exclude_keywords=["interview"]
    )

    assert [doc.metadata["video_id"] for doc in result.documents] == ["v2"]


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


def test_extract_ideas_skips_missing_underlying():
    llm = MagicMock()
    llm.chat_json.return_value = {
        "ideas": [
            {
                "trader_name": "Trader A",
                "show_name": "Show",
                "underlying": "",
                "strategy_type": "short_strangle",
                "description": "Sell earnings strangles on overhyped names",
                "expiration": "45 DTE",
                "credit_target": "",
                "rationale": "Post-earnings vol crush",
                "confidence": "low",
                "timestamp_approx": "04:20",
            },
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
            },
        ]
    }
    docs = [
        RawSourceDocument(
            document_id="doc1",
            source_type=SourceType.WEB.value,
            source_name="web",
            title="Ideas",
            url="https://example.com/1",
            text="ideas",
        )
    ]

    ideas = extract_ideas_from_raw_documents(llm, docs)

    assert len(ideas) == 1
    assert ideas[0].underlying == "SPY"


def test_transcript_archive_write_and_purge(tmp_path):
    from datetime import datetime, timedelta

    from vol_crush.idea_sources.transcript_archive import (
        purge_older_than,
        write_transcript,
    )

    doc = RawSourceDocument(
        document_id="doc1",
        source_type=SourceType.YOUTUBE.value,
        source_name="youtube:ch",
        title="SPY idea",
        text="Sell the SPY put",
        url="https://youtu.be/abc",
        metadata={"video_id": "abc"},
    )
    path = write_transcript(tmp_path, doc)
    assert path is not None
    assert path.read_text(encoding="utf-8") == "Sell the SPY put"
    meta = path.with_suffix(".meta.json")
    assert meta.exists()

    # Fast-forward: backdate the file so the purge sees it as stale.
    stale_time = (datetime.now() - timedelta(days=30)).timestamp()
    import os

    os.utime(path, (stale_time, stale_time))
    os.utime(meta, (stale_time, stale_time))

    removed = purge_older_than(tmp_path, retention_days=14)
    assert removed == 1
    assert not path.exists()
    assert not meta.exists()


def test_summary_archive_writes_markdown(tmp_path):
    from vol_crush.idea_scraper.summary_archive import (
        read_recent_summary_records,
        write_summary,
    )

    doc = RawSourceDocument(
        document_id="doc1",
        source_type=SourceType.YOUTUBE.value,
        source_name="youtube:ch",
        title="IVR matters today",
        author="tastylive",
        url="https://youtu.be/abc",
        metadata={"video_id": "abc"},
    )
    summary = {
        "headline": "Short premium opportunistic",
        "macro_view": "Range-bound SPX",
        "vol_view": "IV rank elevated",
        "tickers": [
            {"ticker": "SPY", "bias": "neutral", "notes": "IV rank 45"},
        ],
        "strategies_discussed": ["short strangles"],
        "notable_quotes": ["Sell premium when IVR is high"],
        "risks": "",
        "actionable_ideas_present": True,
    }
    path = write_summary(tmp_path, doc, summary, model="openrouter:fake-model")
    assert path.exists()
    rendered = path.read_text(encoding="utf-8")
    assert "# IVR matters today" in rendered
    assert "**SPY**" in rendered
    assert "short strangles" in rendered
    assert "Sell premium" in rendered

    sidecar = path.with_suffix(".json")
    assert sidecar.exists()
    records = read_recent_summary_records(tmp_path, lookback_days=7)
    assert len(records) == 1
    assert records[0].digest_id == "abc"
    assert records[0].category == "trade_setup"
    assert records[0].actionable_ideas_present is True
    assert "Short premium opportunistic" in records[0].summary


def test_fetch_url_retries_then_succeeds(monkeypatch):
    from vol_crush.idea_sources import utils

    calls = []
    sleeps = []

    class _FakeResponse:
        def __init__(self, body):
            self.body = body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

    def fake_urlopen(request, timeout, context):
        calls.append(request.full_url)
        if len(calls) < 3:
            from urllib.error import HTTPError

            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return _FakeResponse("ok")

    monkeypatch.setattr(utils, "urlopen", fake_urlopen)

    text = utils.fetch_url(
        "https://example.com/f", sleep=lambda d: sleeps.append(d), base_delay=0.01
    )
    assert text == "ok"
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert sleeps[1] >= sleeps[0]  # exponential


def test_fetch_url_raises_on_non_retryable(monkeypatch):
    from urllib.error import HTTPError

    from vol_crush.idea_sources import utils

    def fake_urlopen(request, timeout, context):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(utils, "urlopen", fake_urlopen)

    try:
        utils.fetch_url("https://example.com/f", sleep=lambda d: None)
    except HTTPError as exc:
        assert exc.code == 403
    else:
        raise AssertionError("expected HTTPError to propagate")


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
    observations = store.list_source_observations()
    scorecards = store.list_source_intelligence()
    assert len(observations) == 1
    assert observations[0].source_name == "local_transcripts"
    assert observations[0].lane_assignment == ["noise"]
    assert len(scorecards) == 1
    assert scorecards[0].source_name == "local_transcripts"
    assert scorecards[0].sample_size == 1
    assert any("fetched" in note for note in notes)


def test_record_intake_artifacts_promotes_candidates_and_playbook(tmp_path):
    from vol_crush.core.models import TradeIdea
    from vol_crush.intelligence.service import record_intake_artifacts

    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
    )
    doc = RawSourceDocument(
        document_id="doc1",
        source_type=SourceType.YOUTUBE.value,
        source_name="youtube:ch",
        title="SPY idea",
        url="https://example.com/video",
        text="Sell the SPY put. Prefer defined-risk spreads in low IV.",
    )
    idea = TradeIdea(
        id="idea_1",
        date="2026-04-25",
        trader_name="Trader",
        show_name="Show",
        underlying="SPY",
        strategy_type="short_put",
        description="Sell a SPY short put",
        rationale="IV elevated",
        confidence="high",
        source_url="https://example.com/video",
    )
    summary = {
        "headline": "Short premium remains attractive.",
        "vol_view": "IV rank elevated.",
        "strategies_discussed": ["short_put"],
        "actionable_ideas_present": True,
    }

    observations, candidates, insights = record_intake_artifacts(
        store,
        [doc],
        [idea],
        summaries_by_document_id={"doc1": summary},
    )

    assert len(observations) == 1
    assert observations[0].lane_assignment == ["trade_idea", "operator_digest"]
    assert observations[0].idea_count == 1
    assert len(candidates) == 1
    assert candidates[0].promotable is True
    assert candidates[0].promoted_to_idea_review is True
    assert insights == []
    assert store.list_source_intelligence()[0].idea_rate == 1.0


def test_record_intake_artifacts_captures_playbook_without_trade_idea(tmp_path):
    from vol_crush.intelligence.service import record_intake_artifacts

    store = LocalStore(
        sqlite_path=tmp_path / "vol_crush.db", audit_dir=tmp_path / "audit"
    )
    doc = RawSourceDocument(
        document_id="doc_playbook",
        source_type=SourceType.YOUTUBE.value,
        source_name="youtube:education",
        title="When to use put spreads",
        url="https://example.com/playbook",
        text="Use defined risk when implied volatility is low.",
    )
    summary = {
        "headline": "Defined risk is preferable when premium is thin.",
        "vol_view": "Low IV makes undefined-risk premium less attractive.",
        "strategies_discussed": ["put_spread", "iron_condor"],
        "actionable_ideas_present": False,
    }

    observations, candidates, insights = record_intake_artifacts(
        store,
        [doc],
        [],
        summaries_by_document_id={"doc_playbook": summary},
    )

    assert candidates == []
    assert len(insights) == 1
    assert "Defined risk" in insights[0].lesson
    assert observations[0].lane_assignment == ["operator_digest", "playbook"]
    scorecard = store.list_source_intelligence()[0]
    assert scorecard.playbook_rate == 1.0
    assert scorecard.current_intake_priority == "high"
