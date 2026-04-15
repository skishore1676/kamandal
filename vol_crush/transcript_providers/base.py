"""Provider protocol + result dataclass for transcript acquisition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass
class TranscriptFetch:
    """Outcome of a single provider attempt.

    - ``text`` empty string signals "nothing to use" — either the provider
      declined (e.g. caption-only provider asked to handle a non-YouTube URL)
      or a downstream error occurred. Either way the chain will try the next
      provider.
    - ``error`` is populated when an attempt was made but failed. The chain
      logs these but does not raise, so callers see the best successful
      answer or an empty result.
    - ``cost_estimate_usd`` lets callers surface spend (useful when paid
      providers are in the chain).
    """

    provider: str = ""
    text: str = ""
    language: str = ""
    duration_seconds: float | None = None
    cost_estimate_usd: float = 0.0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls, provider: str = "") -> "TranscriptFetch":
        return cls(provider=provider)

    @classmethod
    def failure(cls, provider: str, error: str) -> "TranscriptFetch":
        return cls(provider=provider, error=error)


@runtime_checkable
class TranscriptProvider(Protocol):
    """Produce transcript text for a given media URL.

    Providers are free to reject URLs they do not understand by returning an
    empty ``TranscriptFetch`` (``text == ""`` and ``error == ""``). The chain
    treats that as "pass, try the next provider". Genuine failures should set
    ``error`` so they surface in logs and reports.
    """

    name: str

    def supports(self, url: str, metadata: Mapping[str, Any]) -> bool:
        """True if this provider is willing to attempt the given URL."""
        ...

    def fetch(
        self, url: str, metadata: Mapping[str, Any] | None = None
    ) -> TranscriptFetch:
        """Attempt to fetch a transcript for this URL."""
        ...
