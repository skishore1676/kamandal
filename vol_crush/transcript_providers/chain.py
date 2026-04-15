"""Ordered chain of TranscriptProviders."""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from vol_crush.transcript_providers.base import (
    TranscriptFetch,
    TranscriptProvider,
)

logger = logging.getLogger("vol_crush.transcript_providers.chain")


class ProviderChain:
    """Try each provider in order; return the first successful transcript.

    A "successful" fetch is one where ``text`` is non-empty. Empty-without-error
    returns (e.g. a provider that does not support the URL) are treated as
    "skip silently"; returns with ``error`` are logged but not fatal. The
    chain never raises — callers receive the best result available or a
    ``TranscriptFetch`` whose ``text`` is empty and whose ``metadata`` lists
    each provider's error.
    """

    def __init__(self, providers: Iterable[TranscriptProvider]):
        self.providers = list(providers)

    def fetch(
        self, url: str, metadata: Mapping[str, Any] | None = None
    ) -> TranscriptFetch:
        metadata = metadata or {}
        attempts: list[dict[str, str]] = []
        for provider in self.providers:
            try:
                if not provider.supports(url, metadata):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "provider %s.supports raised %s; skipping",
                    getattr(provider, "name", provider),
                    exc,
                )
                continue
            try:
                result = provider.fetch(url, metadata)
            except Exception as exc:  # noqa: BLE001 — never raise past the chain
                logger.warning(
                    "provider %s.fetch raised %s",
                    getattr(provider, "name", provider),
                    exc,
                )
                attempts.append(
                    {
                        "provider": getattr(provider, "name", ""),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if result.text:
                logger.info(
                    "transcript for %s obtained via %s (%d chars)",
                    url,
                    result.provider,
                    len(result.text),
                )
                if attempts:
                    result.metadata.setdefault("failed_providers", attempts)
                return result
            if result.error:
                attempts.append({"provider": result.provider, "error": result.error})

        logger.info("no provider produced a transcript for %s", url)
        return TranscriptFetch(
            provider="chain",
            text="",
            metadata={"failed_providers": attempts} if attempts else {},
        )
