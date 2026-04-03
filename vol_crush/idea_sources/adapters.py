"""Source adapter implementations for raw idea-content capture."""

from __future__ import annotations

import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from vol_crush.core.models import RawContentStatus, RawSourceDocument, SourceType
from vol_crush.idea_sources.utils import clean_text, html_to_text, make_fingerprint, safe_fetch_url

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
    fingerprint = make_fingerprint(source_type.value, source_name, title, url, text[:5000])
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
        result.notes.append(f"loaded {len(result.documents)} local transcript documents")
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
        match = re.search(r"<title>(.*?)</title>", body, flags=re.IGNORECASE | re.DOTALL)
        return clean_text(match.group(1)) if match else ""


class RssFeedAdapter:
    """Fetch RSS/Atom feeds and materialize feed items into raw documents."""

    def fetch(self, feed_url: str, limit: int = 5, source_name: str = "rss") -> SourceFetchResult:
        xml_body = safe_fetch_url(feed_url)
        result = SourceFetchResult()
        if not xml_body:
            result.notes.append(f"failed to fetch feed {feed_url}")
            return result
        root = ET.fromstring(xml_body)
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in items[:limit]:
            title = self._find_text(item, "title")
            link = self._find_text(item, "link")
            description = self._find_text(item, "description") or self._find_text(item, "summary")
            published_at = self._find_text(item, "pubDate") or self._find_text(item, "published")
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
        result.notes.append(f"fetched {len(result.documents)} feed items from {feed_url}")
        return result

    @staticmethod
    def _find_text(item: ET.Element, local_name: str) -> str:
        for child in item.iter():
            if child.tag.split("}")[-1] == local_name:
                return clean_text(child.text or child.get("href", ""))
        return ""


class YouTubeChannelAdapter:
    """Fetch recent YouTube channel videos and extract transcripts when available."""

    def fetch(self, channel_id: str, limit: int = 5) -> SourceFetchResult:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        xml_body = safe_fetch_url(feed_url)
        result = SourceFetchResult()
        if not xml_body:
            result.notes.append(f"failed to fetch YouTube feed for {channel_id}")
            return result
        root = ET.fromstring(xml_body)
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        entries = root.findall("atom:entry", ns)
        for entry in entries[:limit]:
            video_id = self._find_text(entry, "videoId")
            title = self._find_text(entry, "title")
            published_at = self._find_text(entry, "published")
            author = self._find_text(entry, "name")
            url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
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
        result.notes.append(f"fetched {len(result.documents)} YouTube documents from channel {channel_id}")
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
        body = safe_fetch_url(url)
        if not body:
            return ""
        caption_url = self._extract_caption_url(body)
        if not caption_url:
            return ""
        transcript_xml = safe_fetch_url(caption_url)
        if not transcript_xml:
            return ""
        try:
            root = ET.fromstring(transcript_xml)
        except ET.ParseError:
            return ""
        pieces = [clean_text(node.text or "") for node in root.findall(".//text")]
        return "\n".join(piece for piece in pieces if piece)

    def _extract_caption_url(self, body: str) -> str:
        candidates = re.findall(r'"baseUrl":"(https:[^"]+?)"', body)
        for candidate in candidates:
            if "timedtext" not in candidate:
                continue
            decoded = self._decode_js_string(candidate)
            if "fmt=" not in decoded:
                decoded += "&fmt=srv3"
            return decoded
        return ""

    @staticmethod
    def _decode_js_string(value: str) -> str:
        try:
            return json.loads(f'"{value}"')
        except json.JSONDecodeError:
            return unquote(value.replace("\\u0026", "&").replace("\\/", "/"))

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
