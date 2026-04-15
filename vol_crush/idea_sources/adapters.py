"""Source adapter implementations for raw idea-content capture."""

from __future__ import annotations

import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from vol_crush.core.models import RawContentStatus, RawSourceDocument, SourceType
from vol_crush.idea_sources.utils import (
    clean_text,
    html_to_text,
    make_fingerprint,
    safe_fetch_url,
)

logger = logging.getLogger("vol_crush.idea_sources")


@dataclass
class SourceFetchResult:
    """Result bundle from an adapter fetch run."""

    documents: list[RawSourceDocument] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _new_document(
    source_type: SourceType,
    source_name: str,
    title: str,
    text: str,
    *,
    author: str = "",
    published_at: str = "",
    url: str = "",
    summary: str = "",
    metadata: dict | None = None,
) -> RawSourceDocument:
    fingerprint = make_fingerprint(
        source_type.value, source_name, title, url, text[:5000]
    )
    return RawSourceDocument(
        document_id=f"doc_{uuid.uuid4().hex[:10]}",
        source_type=source_type.value,
        source_name=source_name,
        title=clean_text(title) or "Untitled",
        author=clean_text(author),
        published_at=published_at,
        url=url,
        text=text.strip(),
        summary=clean_text(summary),
        fingerprint=fingerprint,
        status=RawContentStatus.NEW.value,
        metadata=metadata or {},
    )


class TranscriptDirectoryAdapter:
    """Read local transcript files into raw source documents."""

    def fetch(self, transcripts_dir: Path) -> SourceFetchResult:
        result = SourceFetchResult()
        for path in sorted(transcripts_dir.glob("*.txt")):
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue
            result.documents.append(
                _new_document(
                    SourceType.TRANSCRIPT,
                    "local_transcripts",
                    path.stem,
                    text,
                    url=str(path),
                    metadata={"path": str(path)},
                )
            )
        result.notes.append(
            f"loaded {len(result.documents)} local transcript documents"
        )
        return result


class GenericWebAdapter:
    """Fetch arbitrary webpages and normalize their visible text."""

    def fetch(self, urls: list[str], source_name: str = "web") -> SourceFetchResult:
        result = SourceFetchResult()
        for url in urls:
            body = safe_fetch_url(url)
            if not body:
                result.notes.append(f"failed to fetch {url}")
                continue
            text = html_to_text(body)
            title = self._extract_title(body) or url
            result.documents.append(
                _new_document(
                    SourceType.WEB,
                    source_name,
                    title,
                    text,
                    url=url,
                    summary=text[:280],
                )
            )
        result.notes.append(f"fetched {len(result.documents)} web documents")
        return result

    @staticmethod
    def _extract_title(body: str) -> str:
        match = re.search(
            r"<title>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL
        )
        return clean_text(match.group(1)) if match else ""


class RssFeedAdapter:
    """Fetch RSS/Atom feeds and materialize feed items into raw documents."""

    def fetch(
        self, feed_url: str, limit: int = 5, source_name: str = "rss"
    ) -> SourceFetchResult:
        xml_body = safe_fetch_url(feed_url)
        result = SourceFetchResult()
        if not xml_body:
            result.notes.append(f"failed to fetch feed {feed_url}")
            return result
        root = ET.fromstring(xml_body)
        items = root.findall(".//item") or root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        )
        for item in items[:limit]:
            title = self._find_text(item, "title")
            link = self._find_text(item, "link")
            description = self._find_text(item, "description") or self._find_text(
                item, "summary"
            )
            published_at = self._find_text(item, "pubDate") or self._find_text(
                item, "published"
            )
            author = self._find_text(item, "author")
            text = clean_text(description)
            if link:
                body = safe_fetch_url(link)
                if body:
                    text = html_to_text(body) or text
            result.documents.append(
                _new_document(
                    SourceType.RSS,
                    source_name,
                    title or link or "RSS item",
                    text,
                    author=author,
                    published_at=published_at,
                    url=link,
                    summary=description,
                    metadata={"feed_url": feed_url},
                )
            )
        result.notes.append(
            f"fetched {len(result.documents)} feed items from {feed_url}"
        )
        return result

    @staticmethod
    def _find_text(item: ET.Element, local_name: str) -> str:
        for child in item.iter():
            if child.tag.split("}")[-1] == local_name:
                return clean_text(child.text or child.get("href", ""))
        return ""


class YouTubeChannelAdapter:
    """Fetch recent YouTube channel videos and extract transcripts when available."""

    def fetch(
        self,
        channel_id: str,
        limit: int = 5,
        *,
        title_include_keywords: list[str] | None = None,
        title_exclude_keywords: list[str] | None = None,
    ) -> SourceFetchResult:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        xml_body = safe_fetch_url(feed_url)
        result = SourceFetchResult()
        if not xml_body:
            result.notes.append(f"failed to fetch YouTube feed for {channel_id}")
            return result
        root = ET.fromstring(xml_body)
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
        }
        include_patterns = _compile_keyword_patterns(title_include_keywords)
        exclude_patterns = _compile_keyword_patterns(title_exclude_keywords)
        entries = root.findall("atom:entry", ns)
        skipped_titles = 0
        considered = 0
        for entry in entries:
            if considered >= limit:
                break
            video_id = self._find_text(entry, "videoId")
            title = self._find_text(entry, "title")
            published_at = self._find_text(entry, "published")
            author = self._find_text(entry, "name")
            url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

            if not _title_passes_filter(title, include_patterns, exclude_patterns):
                skipped_titles += 1
                logger.debug(
                    "skipping youtube video on title filter: %r (%s)", title, url
                )
                continue
            considered += 1

            transcript = self._fetch_transcript(url) if url else ""
            summary = self._fetch_description(url) if url else ""
            text = transcript or summary
            if not text:
                result.notes.append(f"no transcript/summary available for {url}")
                continue
            result.documents.append(
                _new_document(
                    SourceType.YOUTUBE,
                    f"youtube:{channel_id}",
                    title or video_id or "YouTube video",
                    text,
                    author=author,
                    published_at=published_at,
                    url=url,
                    summary=summary[:500],
                    metadata={
                        "channel_id": channel_id,
                        "video_id": video_id,
                        "has_transcript": bool(transcript),
                    },
                )
            )
        if skipped_titles:
            result.notes.append(
                f"skipped {skipped_titles} videos from {channel_id} on title filter"
            )
        result.notes.append(
            f"fetched {len(result.documents)} YouTube documents from channel {channel_id}"
        )
        return result

    def _fetch_description(self, url: str) -> str:
        body = safe_fetch_url(url)
        if not body:
            return ""
        match = re.search(r'"shortDescription":"(.*?)"', body)
        if not match:
            return ""
        try:
            return clean_text(json.loads(f'"{match.group(1)}"'))
        except json.JSONDecodeError:
            return clean_text(match.group(1))

    def _fetch_transcript(self, url: str) -> str:
        video_id = extract_video_id_from_url(url)
        if not video_id:
            return ""
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            logger.warning(
                "youtube-transcript-api not installed; transcript fetch disabled"
            )
            return ""
        try:
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        except Exception as exc:
            logger.info(
                "transcript unavailable for %s (%s: %s)",
                video_id,
                type(exc).__name__,
                exc,
            )
            return ""
        pieces = [
            clean_text(getattr(snippet, "text", "") or "") for snippet in fetched
        ]
        return "\n".join(piece for piece in pieces if piece)

    @staticmethod
    def _find_text(entry: ET.Element, local_name: str) -> str:
        for child in entry.iter():
            if child.tag.split("}")[-1] == local_name:
                return clean_text(child.text or "")
        return ""


def extract_video_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname and "youtube.com" in parsed.hostname:
        return parse_qs(parsed.query).get("v", [""])[0]
    if parsed.hostname and "youtu.be" in parsed.hostname:
        return parsed.path.strip("/")
    return ""


def _compile_keyword_patterns(
    keywords: list[str] | None,
) -> list[re.Pattern[str]]:
    """Compile a list of include/exclude keywords into case-insensitive regexes.

    Each keyword is treated as a literal substring unless it contains regex
    metacharacters (detected via re.escape round-trip); this lets users drop in
    either simple words ("earnings") or real patterns ("iron\\s*condor").
    """
    if not keywords:
        return []
    patterns: list[re.Pattern[str]] = []
    for kw in keywords:
        if not kw:
            continue
        text = str(kw).strip()
        if not text:
            continue
        # If the user didn't use any regex-ish character, escape it as literal.
        looks_like_regex = any(ch in text for ch in r".*+?^$()[]{}|\\")
        pattern = text if looks_like_regex else re.escape(text)
        try:
            patterns.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            patterns.append(re.compile(re.escape(text), re.IGNORECASE))
    return patterns


def _title_passes_filter(
    title: str,
    include_patterns: list[re.Pattern[str]],
    exclude_patterns: list[re.Pattern[str]],
) -> bool:
    if not title:
        return not include_patterns  # no title → accept only when nothing required
    for pattern in exclude_patterns:
        if pattern.search(title):
            return False
    if not include_patterns:
        return True
    return any(pattern.search(title) for pattern in include_patterns)
