"""Utilities for source acquisition and normalization."""

from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TextExtractor(HTMLParser):
    """Very small HTML-to-text extractor for generic web pages."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = " ".join(data.split())
        if cleaned:
            self._chunks.append(cleaned)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


def fetch_url(url: str, timeout: int = 15) -> str:
    request = Request(url, headers={"User-Agent": "vol-crush-source-fetcher/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def safe_fetch_url(url: str, timeout: int = 15) -> str:
    try:
        return fetch_url(url, timeout=timeout)
    except (HTTPError, URLError, TimeoutError):
        return ""


def html_to_text(body: str) -> str:
    parser = TextExtractor()
    parser.feed(body)
    text = parser.get_text()
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    return re.sub(r"\s+", " ", text).strip()


def make_fingerprint(*parts: str) -> str:
    normalized = "||".join(clean_text(part).lower() for part in parts if part)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
