"""Unit tests for the pluggable transcript provider framework."""

from __future__ import annotations

from vol_crush.transcript_providers import (
    ProviderChain,
    TranscriptFetch,
    TranscriptProvider,
    YouTubeCaptionProvider,
    build_chain,
    register_provider,
)
from vol_crush.transcript_providers.registry import PROVIDER_REGISTRY


class _FakeSnippet:
    def __init__(self, text):
        self.text = text


def _install_fake_transcript_api(monkeypatch, mapping, error=None):
    import youtube_transcript_api

    class _FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self, video_id, languages=None):
            if error is not None:
                raise error
            if video_id in mapping:
                return [_FakeSnippet(t) for t in mapping[video_id]]
            raise RuntimeError(f"no transcript for {video_id}")

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", _FakeApi)


# ── YouTubeCaptionProvider ────────────────────────────────────────────


def test_youtube_captions_success(monkeypatch):
    _install_fake_transcript_api(monkeypatch, {"abc123": ["hello world", "part two"]})
    provider = YouTubeCaptionProvider()
    result = provider.fetch(
        "https://www.youtube.com/watch?v=abc123", {"video_id": "abc123"}
    )
    assert result.provider == "youtube_captions"
    assert "hello world" in result.text
    assert "part two" in result.text
    assert result.error == ""


def test_youtube_captions_rejects_non_youtube(monkeypatch):
    provider = YouTubeCaptionProvider()
    assert not provider.supports("https://example.com/podcast", {})
    result = provider.fetch("https://example.com/podcast", {})
    assert result.text == ""
    assert result.error == ""


def test_youtube_captions_failure_has_error(monkeypatch):
    _install_fake_transcript_api(
        monkeypatch, {}, error=RuntimeError("captions disabled")
    )
    provider = YouTubeCaptionProvider()
    result = provider.fetch(
        "https://www.youtube.com/watch?v=nope", {"video_id": "nope"}
    )
    assert result.text == ""
    assert "captions disabled" in result.error


# ── ProviderChain ─────────────────────────────────────────────────────


class _StaticProvider:
    """Test double: always returns the same response."""

    def __init__(self, name: str, response: TranscriptFetch, supports_url: bool = True):
        self.name = name
        self._response = response
        self._supports = supports_url
        self.calls = 0

    def supports(self, url, metadata):
        return self._supports

    def fetch(self, url, metadata=None):
        self.calls += 1
        return self._response


def test_chain_uses_first_successful_provider():
    first = _StaticProvider(
        "first", TranscriptFetch(provider="first", text="", error="no captions")
    )
    second = _StaticProvider(
        "second", TranscriptFetch(provider="second", text="actual transcript")
    )
    third = _StaticProvider(
        "third", TranscriptFetch(provider="third", text="should not run")
    )
    chain = ProviderChain([first, second, third])
    result = chain.fetch("https://example.com/vid")
    assert result.provider == "second"
    assert result.text == "actual transcript"
    assert first.calls == 1
    assert second.calls == 1
    assert third.calls == 0
    assert result.metadata["failed_providers"][0]["provider"] == "first"


def test_chain_skips_providers_that_do_not_support_url():
    first = _StaticProvider(
        "first",
        TranscriptFetch(provider="first", text="won't run"),
        supports_url=False,
    )
    second = _StaticProvider(
        "second", TranscriptFetch(provider="second", text="ran")
    )
    chain = ProviderChain([first, second])
    result = chain.fetch("https://example.com/vid")
    assert first.calls == 0
    assert second.calls == 1
    assert result.text == "ran"


def test_chain_returns_empty_when_all_fail():
    providers = [
        _StaticProvider(
            f"p{i}",
            TranscriptFetch(provider=f"p{i}", text="", error=f"err {i}"),
        )
        for i in range(3)
    ]
    chain = ProviderChain(providers)
    result = chain.fetch("https://example.com/vid")
    assert result.text == ""
    assert len(result.metadata["failed_providers"]) == 3


def test_chain_tolerates_provider_exceptions():
    class _Exploder:
        name = "exploder"

        def supports(self, url, metadata):
            return True

        def fetch(self, url, metadata=None):
            raise RuntimeError("boom")

    second = _StaticProvider(
        "second", TranscriptFetch(provider="second", text="fallback ok")
    )
    chain = ProviderChain([_Exploder(), second])
    result = chain.fetch("https://example.com/vid")
    assert result.text == "fallback ok"
    assert second.calls == 1


# ── Registry / build_chain ────────────────────────────────────────────


def test_build_chain_defaults_to_youtube_only():
    chain = build_chain({})
    assert len(chain.providers) == 1
    assert chain.providers[0].name == "youtube_captions"


def test_build_chain_skips_disabled_entries():
    cfg = {
        "providers": [
            {"type": "youtube_captions", "enabled": True},
            {"type": "groq_whisper", "enabled": False},
        ]
    }
    chain = build_chain(cfg)
    assert [p.name for p in chain.providers] == ["youtube_captions"]


def test_build_chain_warns_on_unknown_type_and_falls_back():
    cfg = {"providers": [{"type": "does_not_exist", "enabled": True}]}
    chain = build_chain(cfg)
    # Unknown + nothing else → falls back to default youtube_captions
    assert [p.name for p in chain.providers] == ["youtube_captions"]


def test_register_provider_allows_third_party_types():
    try:
        sentinel = _StaticProvider(
            "custom", TranscriptFetch(provider="custom", text="hello")
        )
        register_provider("custom_test", lambda cfg: sentinel)
        cfg = {"providers": [{"type": "custom_test", "enabled": True}]}
        chain = build_chain(cfg)
        assert [p.name for p in chain.providers] == ["custom"]
        # Ensure the TranscriptProvider Protocol is satisfied at runtime.
        assert isinstance(sentinel, TranscriptProvider)
    finally:
        PROVIDER_REGISTRY.pop("custom_test", None)
