"""On-disk archive of fetched transcripts with time-based retention.

Stores each transcript as a plain-text file alongside a JSON metadata sidecar,
organized by fetch date under the archive root. Purges files older than the
configured retention window.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from vol_crush.core.models import RawSourceDocument

logger = logging.getLogger("vol_crush.idea_sources.transcript_archive")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", (value or "").strip())
    return cleaned or fallback


def archive_path(
    archive_root: Path,
    document: RawSourceDocument,
    *,
    fetch_date: date | None = None,
) -> Path:
    """Return the directory where a transcript + metadata will be stored."""
    day = (fetch_date or date.today()).isoformat()
    source = _safe_component(document.source_type or "unknown", "unknown")
    return archive_root / source / day


def _base_filename(document: RawSourceDocument) -> str:
    video_id = (document.metadata or {}).get("video_id") or ""
    return _safe_component(video_id or document.document_id, "doc")


def write_transcript(
    archive_root: Path,
    document: RawSourceDocument,
    *,
    fetch_date: date | None = None,
) -> Path | None:
    """Persist a transcript + metadata sidecar to disk. Returns transcript path.

    Returns None when the document has no text to archive. Existing files are
    overwritten so re-fetches keep the archive in sync with the DB.
    """
    if not document.text:
        return None

    directory = archive_path(archive_root, document, fetch_date=fetch_date)
    directory.mkdir(parents=True, exist_ok=True)
    base = _base_filename(document)
    transcript_file = directory / f"{base}.txt"
    metadata_file = directory / f"{base}.meta.json"

    transcript_file.write_text(document.text, encoding="utf-8")
    metadata_file.write_text(
        json.dumps(
            {
                "document_id": document.document_id,
                "source_type": document.source_type,
                "source_name": document.source_name,
                "title": document.title,
                "author": document.author,
                "published_at": document.published_at,
                "url": document.url,
                "summary": document.summary,
                "metadata": document.metadata or {},
                "archived_at": datetime.now(UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.debug("archived transcript %s", transcript_file)
    return transcript_file


def purge_older_than(
    archive_root: Path,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Delete transcript files (and their sidecars) older than retention_days.

    Also removes empty day-directories afterwards. Returns the count of
    transcript files removed.
    """
    if retention_days <= 0 or not archive_root.exists():
        return 0

    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()
    removed = 0

    for transcript in archive_root.rglob("*.txt"):
        try:
            if transcript.stat().st_mtime >= cutoff_ts:
                continue
        except FileNotFoundError:
            continue
        sidecar = transcript.with_suffix(".meta.json")
        try:
            transcript.unlink()
        except FileNotFoundError:
            pass
        else:
            removed += 1
        if sidecar.exists():
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass

    # Remove empty leaf directories (best-effort; ignore non-empty ones).
    for directory in sorted(
        (p for p in archive_root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass

    if removed:
        logger.info(
            "purged %d transcript file(s) older than %d days from %s",
            removed,
            retention_days,
            archive_root,
        )
    return removed
