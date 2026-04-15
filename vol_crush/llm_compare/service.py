"""Run the same transcript through multiple LLMs and persist a comparison report.

Pairs with the summary + extraction prompts so you can eyeball which model
produces the most useful structured output before committing to one as the
primary in your `.env`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

from vol_crush.core.models import RawSourceDocument
from vol_crush.idea_scraper.scraper import (
    extract_ideas_from_transcript,
    summarize_transcript,
)
from vol_crush.integrations.llm import LLMClient

logger = logging.getLogger("vol_crush.llm_compare")


@dataclass
class ComparisonResult:
    """One model's output for a single transcript."""

    model: str
    provider: str
    summary: dict[str, Any] | None = None
    summary_error: str = ""
    summary_duration_s: float = 0.0
    ideas: list[dict[str, Any]] = field(default_factory=list)
    ideas_error: str = ""
    ideas_duration_s: float = 0.0


def _load_transcript(video_id: str, archive_root: Path) -> tuple[str, RawSourceDocument]:
    """Locate the most recent archived transcript for a video_id."""
    if not archive_root.exists():
        raise FileNotFoundError(
            f"transcript archive does not exist: {archive_root} — run idea_sources first"
        )
    matches = sorted(archive_root.rglob(f"{video_id}.txt"), reverse=True)
    if not matches:
        raise FileNotFoundError(
            f"no archived transcript for video_id={video_id!r} under {archive_root}"
        )
    transcript_path = matches[0]
    meta_path = transcript_path.with_suffix(".meta.json")
    metadata = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    document = RawSourceDocument(
        document_id=metadata.get("document_id", video_id),
        source_type=metadata.get("source_type", "youtube"),
        source_name=metadata.get("source_name", "youtube"),
        title=metadata.get("title", video_id),
        author=metadata.get("author", ""),
        published_at=metadata.get("published_at", ""),
        url=metadata.get("url", ""),
        text=transcript_path.read_text(encoding="utf-8"),
        summary=metadata.get("summary", ""),
        metadata=metadata.get("metadata") or {"video_id": video_id},
    )
    return document.text, document


def _run_one_model(
    document: RawSourceDocument,
    transcript: str,
    *,
    api_key: str,
    provider: str,
    model: str,
    fallback_model: str | None,
    base_url: str | None,
) -> ComparisonResult:
    client = LLMClient(
        api_key=api_key,
        model=model,
        provider=provider,
        base_url=base_url,
        fallback_model=fallback_model,
    )
    meta = document.metadata or {}
    result = ComparisonResult(model=model, provider=provider)

    start = time.perf_counter()
    try:
        result.summary = summarize_transcript(
            client,
            transcript,
            source=f"{document.source_type}:{document.source_name}",
            source_url=document.url,
            source_title=document.title,
            author=document.author,
        )
    except Exception as exc:  # noqa: BLE001 — capture for report
        result.summary_error = f"{type(exc).__name__}: {exc}"
        logger.warning("summary with %s failed: %s", model, exc)
    result.summary_duration_s = round(time.perf_counter() - start, 2)

    start = time.perf_counter()
    try:
        ideas = extract_ideas_from_transcript(
            client,
            transcript,
            source=f"{document.source_type}:{document.source_name}",
            source_url=document.url,
            source_title=document.title,
            author=document.author,
            video_id=meta.get("video_id", ""),
        )
        result.ideas = [idea.to_dict() for idea in ideas]
    except Exception as exc:  # noqa: BLE001
        result.ideas_error = f"{type(exc).__name__}: {exc}"
        logger.warning("extraction with %s failed: %s", model, exc)
    result.ideas_duration_s = round(time.perf_counter() - start, 2)
    return result


def run_comparison(
    *,
    video_id: str,
    models: Iterable[str],
    api_key: str,
    provider: str = "openrouter",
    fallback_model: str | None = None,
    base_url: str | None = None,
    archive_root: Path,
    output_root: Path,
    run_date: date | None = None,
) -> Path:
    """Execute the comparison and persist both a JSON payload and a markdown report.

    Returns the markdown report path.
    """
    transcript, document = _load_transcript(video_id, archive_root)
    results: list[ComparisonResult] = []
    for model in models:
        logger.info("running model %s against %s", model, video_id)
        results.append(
            _run_one_model(
                document,
                transcript,
                api_key=api_key,
                provider=provider,
                model=model,
                fallback_model=fallback_model,
                base_url=base_url,
            )
        )

    run_date = run_date or date.today()
    output_dir = output_root / run_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    base = f"{video_id}_compare"
    json_path = output_dir / f"{base}.json"
    md_path = output_dir / f"{base}.md"

    payload = {
        "video_id": video_id,
        "title": document.title,
        "url": document.url,
        "run_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "provider": provider,
        "results": [_result_to_dict(r) for r in results],
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    md_path.write_text(_render_report(document, results), encoding="utf-8")
    logger.info("wrote comparison report %s", md_path)
    return md_path


def _result_to_dict(result: ComparisonResult) -> dict[str, Any]:
    return {
        "model": result.model,
        "provider": result.provider,
        "summary_duration_s": result.summary_duration_s,
        "ideas_duration_s": result.ideas_duration_s,
        "summary_error": result.summary_error,
        "ideas_error": result.ideas_error,
        "summary": result.summary,
        "ideas": result.ideas,
    }


def _render_report(
    document: RawSourceDocument, results: list[ComparisonResult]
) -> str:
    lines = [
        f"# LLM comparison — {document.title or document.document_id}",
        "",
        f"- **Source URL**: {document.url or 'n/a'}",
        f"- **Video ID**: {(document.metadata or {}).get('video_id', 'n/a')}",
        f"- **Transcript length**: {len(document.text)} chars",
        "",
        "## Timing + errors",
        "",
        "| model | summary (s) | ideas (s) | summary error | ideas error |",
        "|---|---:|---:|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r.model}` | {r.summary_duration_s} | {r.ideas_duration_s} | "
            f"{r.summary_error or '—'} | {r.ideas_error or '—'} |"
        )
    for r in results:
        lines += [
            "",
            f"## `{r.model}`",
            "",
            "### Summary",
            "",
            "```json",
            json.dumps(r.summary, indent=2, ensure_ascii=False) if r.summary else "null",
            "```",
            "",
            f"### Trade ideas ({len(r.ideas)})",
            "",
            "```json",
            json.dumps(r.ideas, indent=2, ensure_ascii=False) if r.ideas else "[]",
            "```",
        ]
    return "\n".join(lines) + "\n"
