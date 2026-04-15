"""Utilities for source acquisition and normalization."""

from __future__ import annotations

import hashlib
import html
import logging
import re
import ssl
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("vol_crush.idea_sources.utils")

# YouTube RSS returns 404 intermittently; treat it as retryable rather than permanent.
_RETRYABLE_STATUS = {404, 408, 425, 429, 500, 502, 503, 504}


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CONTEXT = _build_ssl_context()


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


_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def fetch_url(
    url: str,
    timeout: int = 15,
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    sleep: callable = time.sleep,
) -> str:
    """Fetch a URL with exponential-backoff retry on transient errors.

    YouTube RSS endpoints return 404/500 intermittently even for valid channels;
    the retry handles this without the caller having to care. Raises the last
    exception when all attempts fail.
    """
    request = Request(
        url,
        headers={
            "User-Agent": _DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(request, timeout=timeout, context=_SSL_CONTEXT) as response:
                return response.read().decode("utf-8", errors="ignore")
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in _RETRYABLE_STATUS or attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.info(
                "fetch_url %s returned %d (attempt %d/%d); retrying in %.1fs",
                url,
                exc.code,
                attempt,
                max_attempts,
                delay,
            )
            sleep(delay)
        except (URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.info(
                "fetch_url %s failed (%s: %s) (attempt %d/%d); retrying in %.1fs",
                url,
                type(exc).__name__,
                exc,
                attempt,
                max_attempts,
                delay,
            )
            sleep(delay)
    assert last_exc is not None  # pragma: no cover — loop always raised or returned
    raise last_exc


def safe_fetch_url(url: str, timeout: int = 15, *, max_attempts: int = 3) -> str:
    try:
        return fetch_url(url, timeout=timeout, max_attempts=max_attempts)
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
