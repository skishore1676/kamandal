"""
Module 1: Live Idea Scraper

Two modes:
  - live:   capture system audio, stream to Whisper, extract ideas in real-time
  - record: record audio to file, then batch transcribe + extract

Output: TradeIdea objects written to Google Sheets (daily_ideas tab).
"""

from __future__ import annotations

import logging
import tempfile
import uuid
import wave
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from vol_crush.core.models import IdeaStatus, RawSourceDocument, TradeIdea
from vol_crush.integrations.llm import LLMClient
from vol_crush.idea_scraper.prompts import (
    IDEA_EXTRACTION_SYSTEM_PROMPT,
    IDEA_EXTRACTION_USER_PROMPT,
)

logger = logging.getLogger("vol_crush.idea_scraper")


# ── Transcription ─────────────────────────────────────────────────────


def transcribe_audio_file(llm_client: LLMClient, audio_path: Path) -> str:
    """Transcribe an audio file using OpenAI Whisper API."""
    logger.info("Transcribing audio: %s", audio_path)
    with open(audio_path, "rb") as f:
        response = llm_client.client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
        )
    text = str(response).strip()
    logger.info("Transcription complete: %d chars", len(text))
    return text


def transcribe_text(text: str) -> str:
    """Pass-through for already-transcribed text (e.g. from data/transcripts/)."""
    return text


# ── Idea Extraction ──────────────────────────────────────────────────


def extract_ideas_from_transcript(
    llm: LLMClient,
    transcript: str,
    source: str = "YouTube",
    idea_date: str | None = None,
    source_url: str = "",
    source_title: str = "",
) -> list[TradeIdea]:
    """Extract actionable trade ideas from a transcript."""
    if idea_date is None:
        idea_date = date.today().isoformat()

    logger.info("Extracting ideas from transcript (%d chars)", len(transcript))

    user_prompt = IDEA_EXTRACTION_USER_PROMPT.format(
        date=idea_date,
        source=source,
        title=source_title or source,
        source_url=source_url or "unknown",
        transcript=transcript,
    )

    response = llm.chat_json(
        system_prompt=IDEA_EXTRACTION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.1,
        max_tokens=4096,
    )

    raw_ideas = response.get("ideas", [])
    logger.info("Extracted %d raw ideas", len(raw_ideas))

    ideas = []
    for raw in raw_ideas:
        idea = TradeIdea(
            id=f"idea_{uuid.uuid4().hex[:8]}",
            date=idea_date,
            trader_name=raw.get("trader_name", "Unknown"),
            show_name=raw.get("show_name", source),
            underlying=raw.get("underlying", ""),
            strategy_type=raw.get("strategy_type", "other"),
            description=raw.get("description", ""),
            expiration=raw.get("expiration", ""),
            credit_target=_parse_credit(raw.get("credit_target", "")),
            rationale=raw.get("rationale", ""),
            confidence=raw.get("confidence", "medium"),
            source_url=source_url,
            source_timestamp=raw.get("timestamp_approx", ""),
            status=IdeaStatus.NEW.value,
        )
        ideas.append(idea)
        logger.debug(
            "  -> %s: %s on %s", idea.trader_name, idea.strategy_type, idea.underlying
        )

    return ideas


def _parse_credit(credit_str: str) -> float:
    """Try to parse a credit string like '$3.50' into a float."""
    if not credit_str:
        return 0.0
    try:
        return float(credit_str.replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


# ── Audio Recording ──────────────────────────────────────────────────


def record_audio(
    duration_seconds: int,
    sample_rate: int = 16000,
    channels: int = 1,
    output_path: Path | None = None,
) -> Path:
    """Record audio from the default input device.

    Returns path to the saved WAV file.
    """
    try:
        import sounddevice as sd
    except ImportError:
        raise RuntimeError("sounddevice not installed. Run: pip install sounddevice")

    logger.info("Recording audio for %d seconds...", duration_seconds)
    audio_data = sd.rec(
        int(duration_seconds * sample_rate),
        samplerate=sample_rate,
        channels=channels,
        dtype="int16",
    )
    sd.wait()
    logger.info("Recording complete.")

    if output_path is None:
        output_path = Path(tempfile.mktemp(suffix=".wav", dir="data/audio"))

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data.tobytes())

    logger.info("Audio saved to: %s", output_path)
    return output_path


# ── Pipeline: Record → Transcribe → Extract ──────────────────────────


def capture_from_audio_file(
    llm: LLMClient,
    audio_path: Path,
    source: str = "YouTube",
) -> list[TradeIdea]:
    """Full pipeline: transcribe audio file → extract ideas."""
    transcript = transcribe_audio_file(llm, audio_path)
    return extract_ideas_from_transcript(
        llm, transcript, source=source, source_url=str(audio_path)
    )


def capture_from_transcript_file(
    llm: LLMClient,
    transcript_path: Path,
    source: str = "YouTube",
) -> list[TradeIdea]:
    """Full pipeline: read transcript file → extract ideas."""
    transcript = transcript_path.read_text(encoding="utf-8").strip()
    return extract_ideas_from_transcript(
        llm,
        transcript,
        source=source,
        source_url=str(transcript_path),
        source_title=transcript_path.name,
    )


def extract_ideas_from_raw_documents(
    llm: LLMClient,
    documents: Iterable[RawSourceDocument],
) -> list[TradeIdea]:
    """Extract ideas from normalized raw source documents."""
    ideas: list[TradeIdea] = []
    for document in documents:
        text = document.text.strip()
        if not text:
            continue
        ideas.extend(
            extract_ideas_from_transcript(
                llm,
                text,
                source=f"{document.source_type}:{document.source_name}",
                idea_date=document.published_at[:10] if document.published_at else None,
                source_url=document.url,
                source_title=document.title,
            )
        )
    return ideas


def dedupe_trade_ideas(ideas: Iterable[TradeIdea]) -> list[TradeIdea]:
    """Collapse duplicate ideas that share the same core trade identity."""
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[TradeIdea] = []
    for idea in ideas:
        key = (
            idea.date,
            idea.underlying.upper(),
            idea.strategy_type,
            idea.expiration,
            " ".join(idea.description.lower().split())[:120],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(idea)
    return unique
