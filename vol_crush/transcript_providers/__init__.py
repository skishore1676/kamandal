"""Pluggable transcript providers.

Turns a media URL (YouTube, podcast, local file, …) into transcript text via a
chain of providers. Designed to be usable outside of this project — the module
has no dependency on ``idea_sources`` and no knowledge of trade ideas.

Typical usage:

    from vol_crush.transcript_providers import build_chain
    chain = build_chain(config.get("idea_sources", {}).get("transcripts", {}))
    result = chain.fetch("https://youtu.be/abc123")
    if result.text:
        print(result.provider, result.text[:200])
"""

from vol_crush.transcript_providers.base import (
    TranscriptFetch,
    TranscriptProvider,
)
from vol_crush.transcript_providers.chain import ProviderChain
from vol_crush.transcript_providers.groq_whisper import GroqWhisperProvider
from vol_crush.transcript_providers.registry import (
    PROVIDER_REGISTRY,
    build_chain,
    register_provider,
)
from vol_crush.transcript_providers.youtube_captions import (
    YouTubeCaptionProvider,
)

__all__ = [
    "GroqWhisperProvider",
    "PROVIDER_REGISTRY",
    "ProviderChain",
    "TranscriptFetch",
    "TranscriptProvider",
    "YouTubeCaptionProvider",
    "build_chain",
    "register_provider",
]
