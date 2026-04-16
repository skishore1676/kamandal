"""Transcript provider backed by youtube-transcript-api (free, delayed)."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from vol_crush.idea_sources.adapters import extract_video_id_from_url
from vol_crush.idea_sources.utils import clean_text
from vol_crush.transcript_providers.base import TranscriptFetch

logger = logging.getLogger("vol_crush.transcript_providers.youtube_captions")


class YouTubeCaptionProvider:
    """Pulls published captions for a YouTube video.

    Returns empty on non-YouTube URLs so the chain moves on. For live streams,
    YouTube typically generates auto-captions within 6–24h of the stream
    ending — running the pipeline on a delayed cron picks those up for free.
    """

    name = "youtube_captions"

    def __init__(self, languages: list[str] | None = None):
        self.languages = languages or ["en", "en-US", "en-GB"]

    def supports(self, url: str, metadata: Mapping[str, Any]) -> bool:
        if not url:
            return False
        return "youtube.com" in url or "youtu.be" in url

    def fetch(
        self, url: str, metadata: Mapping[str, Any] | None = None
    ) -> TranscriptFetch:
        if not self.supports(url, metadata or {}):
            return TranscriptFetch.empty(provider=self.name)

        video_id = (metadata or {}).get("video_id") or extract_video_id_from_url(url)
        if not video_id:
            return TranscriptFetch.failure(
                provider=self.name,
                error=f"could not extract video_id from {url!r}",
            )

        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            return TranscriptFetch.failure(
                provider=self.name,
                error="youtube-transcript-api is not installed",
            )

        try:
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id, languages=self.languages)
        except Exception as exc:  # noqa: BLE001 — many failure modes, all non-fatal
            logger.info(
                "youtube_captions: transcript unavailable for %s (%s: %s)",
                video_id,
                type(exc).__name__,
                exc,
            )
            return TranscriptFetch.failure(
                provider=self.name,
                error=f"{type(exc).__name__}: {exc}",
            )

        pieces = [
            clean_text(getattr(snippet, "text", "") or "") for snippet in fetched
        ]
        text = "\n".join(piece for piece in pieces if piece)
        if not text:
            return TranscriptFetch.failure(
                provider=self.name, error="empty transcript"
            )

        return TranscriptFetch(
            provider=self.name,
            text=text,
            language=self.languages[0],
            metadata={"video_id": video_id},
        )
