"""Name → factory registry for transcript providers + config-driven chain builder.

To plug a new provider in from another codebase:

    from vol_crush.transcript_providers import register_provider

    def build_my_provider(cfg):
        return MyProvider(**cfg)

    register_provider("my_provider", build_my_provider)

Then reference it in config:

    idea_sources:
      transcripts:
        providers:
          - type: my_provider
            enabled: true
            foo: bar
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from vol_crush.transcript_providers.base import TranscriptProvider
from vol_crush.transcript_providers.chain import ProviderChain

logger = logging.getLogger("vol_crush.transcript_providers.registry")

ProviderFactory = Callable[[Mapping[str, Any]], TranscriptProvider]


def _build_youtube_captions(cfg: Mapping[str, Any]) -> TranscriptProvider:
    from vol_crush.transcript_providers.youtube_captions import (
        YouTubeCaptionProvider,
    )

    languages = cfg.get("languages") or ["en", "en-US", "en-GB"]
    return YouTubeCaptionProvider(languages=list(languages))


def _build_groq_whisper(cfg: Mapping[str, Any]) -> TranscriptProvider:
    from vol_crush.transcript_providers.groq_whisper import from_config

    return from_config(cfg)


PROVIDER_REGISTRY: dict[str, ProviderFactory] = {
    "youtube_captions": _build_youtube_captions,
    "groq_whisper": _build_groq_whisper,
}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register a third-party provider factory."""
    PROVIDER_REGISTRY[name] = factory


def build_chain(transcripts_config: Mapping[str, Any] | None) -> ProviderChain:
    """Construct a ProviderChain from ``idea_sources.transcripts.providers``.

    If the config is empty or has no providers list, a default chain with just
    ``youtube_captions`` is returned — keeps backward compatibility.
    """
    cfg = transcripts_config or {}
    providers_cfg = cfg.get("providers")
    if not providers_cfg:
        return ProviderChain([PROVIDER_REGISTRY["youtube_captions"]({})])

    built: list[TranscriptProvider] = []
    for entry in providers_cfg:
        if not entry:
            continue
        if not entry.get("enabled", True):
            continue
        provider_type = entry.get("type")
        if not provider_type:
            logger.warning("skipping providers[] entry with no 'type' field: %s", entry)
            continue
        factory = PROVIDER_REGISTRY.get(provider_type)
        if factory is None:
            logger.warning(
                "unknown transcript provider %r; known=%s",
                provider_type,
                sorted(PROVIDER_REGISTRY),
            )
            continue
        try:
            built.append(factory(entry))
        except Exception as exc:  # noqa: BLE001 — bad config should not break the pipeline
            logger.warning(
                "failed to build provider %s: %s", provider_type, exc
            )
    if not built:
        logger.info(
            "no transcript providers configured; falling back to youtube_captions only"
        )
        built.append(PROVIDER_REGISTRY["youtube_captions"]({}))
    return ProviderChain(built)
