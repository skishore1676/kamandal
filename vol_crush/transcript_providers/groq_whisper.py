"""Transcript provider backed by Groq's Whisper-turbo API.

Flow:
  1. Download audio with yt-dlp at low bitrate (configurable).
  2. If the resulting file exceeds Groq's upload limit, split into N-minute
     chunks with ffmpeg.
  3. POST each chunk to ``/openai/v1/audio/transcriptions``.
  4. Concatenate the texts.

Opt-in: requires ``GROQ_API_KEY`` and an explicit ``enabled: true`` in config.
yt-dlp and ffmpeg are imported/invoked lazily so missing tools do not break
import.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from vol_crush.transcript_providers.base import TranscriptFetch

logger = logging.getLogger("vol_crush.transcript_providers.groq_whisper")

_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "whisper-large-v3-turbo"
_DEFAULT_UPLOAD_LIMIT_MB = 24  # Groq free tier caps at 25MB; give ourselves headroom
_DEFAULT_CHUNK_SECONDS = 600  # 10 minutes per chunk


class GroqWhisperProvider:
    """Transcribe audio using Groq's OpenAI-compatible Whisper endpoint.

    Parameters are read from the provider config dict; see
    :func:`vol_crush.transcript_providers.registry.build_chain`.
    """

    name = "groq_whisper"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        audio_bitrate_kbps: int = 64,
        upload_limit_mb: int = _DEFAULT_UPLOAD_LIMIT_MB,
        chunk_seconds: int = _DEFAULT_CHUNK_SECONDS,
        max_audio_minutes: int = 240,
        base_url: str = _GROQ_BASE_URL,
        temp_dir: Path | str | None = None,
    ):
        if not api_key:
            raise ValueError("GroqWhisperProvider requires an api_key")
        self.api_key = api_key
        self.model = model
        self.audio_bitrate_kbps = audio_bitrate_kbps
        self.upload_limit_mb = upload_limit_mb
        self.chunk_seconds = chunk_seconds
        self.max_audio_minutes = max_audio_minutes
        self.base_url = base_url
        self.temp_dir = Path(temp_dir) if temp_dir else None

    def supports(self, url: str, metadata: Mapping[str, Any]) -> bool:
        return bool(url)

    def fetch(
        self, url: str, metadata: Mapping[str, Any] | None = None
    ) -> TranscriptFetch:
        if not url:
            return TranscriptFetch.empty(provider=self.name)

        with tempfile.TemporaryDirectory(
            prefix="groq_whisper_", dir=str(self.temp_dir) if self.temp_dir else None
        ) as tmp:
            tmp_path = Path(tmp)
            try:
                audio_path = self._download_audio(url, tmp_path)
            except RuntimeError as exc:
                return TranscriptFetch.failure(provider=self.name, error=str(exc))

            try:
                duration = _probe_duration_seconds(audio_path)
            except RuntimeError as exc:
                logger.warning("groq_whisper: ffprobe failed: %s", exc)
                duration = None

            if duration and duration > self.max_audio_minutes * 60:
                return TranscriptFetch.failure(
                    provider=self.name,
                    error=(
                        f"audio exceeds max_audio_minutes={self.max_audio_minutes} "
                        f"(duration={duration:.0f}s)"
                    ),
                )

            try:
                chunks = self._split_if_needed(audio_path, tmp_path)
            except RuntimeError as exc:
                return TranscriptFetch.failure(provider=self.name, error=str(exc))

            texts: list[str] = []
            total_bytes = 0
            for chunk in chunks:
                try:
                    texts.append(self._transcribe(chunk))
                    total_bytes += chunk.stat().st_size
                except RuntimeError as exc:
                    return TranscriptFetch.failure(
                        provider=self.name,
                        error=f"transcription failed for chunk {chunk.name}: {exc}",
                    )

            text = "\n".join(piece.strip() for piece in texts if piece.strip())
            if not text:
                return TranscriptFetch.failure(
                    provider=self.name, error="transcription returned empty text"
                )

            cost = _estimate_cost_usd(duration_seconds=duration or 0)
            return TranscriptFetch(
                provider=self.name,
                text=text,
                duration_seconds=duration,
                cost_estimate_usd=cost,
                metadata={"audio_bytes": total_bytes, "chunks": len(chunks)},
            )

    # ── Internals ────────────────────────────────────────────────────

    def _download_audio(self, url: str, tmp_path: Path) -> Path:
        try:
            import yt_dlp  # type: ignore
        except ImportError as exc:
            raise RuntimeError("yt-dlp not installed (pip install yt-dlp)") from exc

        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH (brew install ffmpeg)")

        output_template = str(tmp_path / "audio.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(self.audio_bitrate_kbps),
                }
            ],
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as exc:  # yt-dlp raises many types
            raise RuntimeError(f"yt-dlp download failed: {exc}") from exc

        candidates = list(tmp_path.glob("audio.*"))
        mp3s = [p for p in candidates if p.suffix == ".mp3"]
        if not mp3s:
            raise RuntimeError(f"no mp3 produced in {tmp_path}; got {candidates}")
        return mp3s[0]

    def _split_if_needed(self, audio_path: Path, tmp_path: Path) -> list[Path]:
        size_mb = audio_path.stat().st_size / (1024 * 1024)
        if size_mb <= self.upload_limit_mb:
            return [audio_path]

        if not shutil.which("ffmpeg"):
            raise RuntimeError(
                f"audio is {size_mb:.1f}MB (limit {self.upload_limit_mb}MB) and "
                "ffmpeg is not installed — cannot chunk"
            )

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir(exist_ok=True)
        pattern = str(chunks_dir / "chunk_%03d.mp3")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(audio_path),
            "-f",
            "segment",
            "-segment_time",
            str(self.chunk_seconds),
            "-c",
            "copy",
            pattern,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg segment failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        chunks = sorted(chunks_dir.glob("chunk_*.mp3"))
        if not chunks:
            raise RuntimeError("ffmpeg produced zero chunks")
        logger.info(
            "groq_whisper: split %s (%.1fMB) into %d chunk(s)",
            audio_path.name,
            size_mb,
            len(chunks),
        )
        return chunks

    def _transcribe(self, audio_path: Path) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai sdk not installed") from exc

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        with audio_path.open("rb") as handle:
            response = client.audio.transcriptions.create(
                model=self.model,
                file=handle,
                response_format="text",
            )
        if isinstance(response, str):
            return response
        text = getattr(response, "text", None)
        return text or str(response)


def _probe_duration_seconds(audio_path: Path) -> float:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not installed")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return float(result.stdout.strip() or 0)


def _estimate_cost_usd(duration_seconds: float) -> float:
    # Groq pricing as of 2025: whisper-large-v3-turbo ≈ $0.04/hour (paid tier).
    # Free-tier requests are effectively $0 but rate-limited. Keep a conservative
    # upper-bound estimate so callers see a non-trivial number.
    return round((duration_seconds / 3600.0) * 0.04, 4)


def from_config(provider_cfg: Mapping[str, Any]) -> "GroqWhisperProvider":
    """Build a provider from a config dict, reading GROQ_API_KEY from env when unset."""
    api_key = provider_cfg.get("api_key") or os.environ.get(
        provider_cfg.get("api_key_env", "GROQ_API_KEY")
    )
    if not api_key:
        raise ValueError(
            "groq_whisper: api_key missing — set GROQ_API_KEY in .env or "
            "providers[].api_key in config.yaml"
        )
    return GroqWhisperProvider(
        api_key=api_key,
        model=provider_cfg.get("model", _DEFAULT_MODEL),
        audio_bitrate_kbps=int(provider_cfg.get("audio_bitrate_kbps", 64)),
        upload_limit_mb=int(
            provider_cfg.get("upload_limit_mb", _DEFAULT_UPLOAD_LIMIT_MB)
        ),
        chunk_seconds=int(provider_cfg.get("chunk_seconds", _DEFAULT_CHUNK_SECONDS)),
        max_audio_minutes=int(provider_cfg.get("max_audio_minutes", 240)),
        base_url=provider_cfg.get("base_url", _GROQ_BASE_URL),
        temp_dir=provider_cfg.get("temp_dir"),
    )
