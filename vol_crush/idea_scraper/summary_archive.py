"""Persist transcript-level LLM summaries as human-scannable markdown."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping

from vol_crush.core.models import RawSourceDocument

logger = logging.getLogger("vol_crush.idea_scraper.summary_archive")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(value: str, fallback: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", (value or "").strip())
    return cleaned or fallback


def _base_filename(document: RawSourceDocument) -> str:
    video_id = (document.metadata or {}).get("video_id") or ""
    return _safe(video_id or document.document_id, "doc")


def summary_path(
    summary_root: Path,
    document: RawSourceDocument,
    *,
    idea_date: date | None = None,
) -> Path:
    day = (idea_date or date.today()).isoformat()
    return summary_root / day / f"{_base_filename(document)}_summary.md"


def summary_json_path(
    summary_root: Path,
    document: RawSourceDocument,
    *,
    idea_date: date | None = None,
) -> Path:
    day = (idea_date or date.today()).isoformat()
    return summary_root / day / f"{_base_filename(document)}_summary.json"


@dataclass
class SummaryArchiveRecord:
    digest_id: str
    date: str
    source_type: str
    source_name: str
    title: str
    author: str
    url: str
    published_at: str
    model: str
    extracted_at: str
    actionable_ideas_present: bool
    category: str
    summary: str
    headline: str
    macro_view: str
    vol_view: str


def _infer_digest_category(
    document: RawSourceDocument,
    summary: Mapping[str, Any],
) -> str:
    title = (document.title or "").lower()
    headline = str(summary.get("headline") or "").lower()
    strategies = " ".join(
        str(item).lower() for item in (summary.get("strategies_discussed") or [])
    )
    corpus = " ".join(
        [
            title,
            headline,
            strategies,
            str(summary.get("macro_view") or "").lower(),
            str(summary.get("vol_view") or "").lower(),
        ]
    )
    if bool(summary.get("actionable_ideas_present")):
        return "trade_setup"
    if any(
        token in corpus
        for token in ("beginner", "basics", "explained", "how call options work")
    ):
        return "basics"
    if any(
        token in corpus
        for token in ("bot", "paper trade", "day trading", "account", "pdt", "journal")
    ):
        return "bot_journal"
    if (summary.get("macro_view") or "").strip() or (summary.get("vol_view") or "").strip():
        return "macro"
    return "commentary"


def _brief_summary(summary: Mapping[str, Any]) -> str:
    parts: list[str] = []
    headline = str(summary.get("headline") or "").strip()
    macro = str(summary.get("macro_view") or "").strip()
    vol = str(summary.get("vol_view") or "").strip()
    if headline:
        parts.append(headline)
    if macro and macro not in parts:
        parts.append(f"Macro: {macro}")
    if vol and vol not in parts:
        parts.append(f"Vol: {vol}")
    return " ".join(parts[:2]).strip()


def build_summary_record(
    document: RawSourceDocument,
    summary: Mapping[str, Any],
    *,
    model: str = "",
    extracted_at: str | None = None,
) -> SummaryArchiveRecord:
    extracted_at = (
        extracted_at or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    digest_id = _base_filename(document)
    category = _infer_digest_category(document, summary)
    return SummaryArchiveRecord(
        digest_id=digest_id,
        date=(document.published_at or extracted_at)[:10],
        source_type=document.source_type,
        source_name=document.source_name,
        title=document.title or digest_id,
        author=document.author or "",
        url=document.url or "",
        published_at=document.published_at or "",
        model=model or "",
        extracted_at=extracted_at,
        actionable_ideas_present=bool(summary.get("actionable_ideas_present")),
        category=category,
        summary=_brief_summary(summary),
        headline=str(summary.get("headline") or "").strip(),
        macro_view=str(summary.get("macro_view") or "").strip(),
        vol_view=str(summary.get("vol_view") or "").strip(),
    )


def read_recent_summary_records(
    summary_root: Path,
    *,
    lookback_days: int = 7,
) -> list[SummaryArchiveRecord]:
    cutoff = date.today().toordinal() - lookback_days
    records: list[SummaryArchiveRecord] = []
    for json_path in sorted(summary_root.glob("*/*_summary.json")):
        try:
            day = date.fromisoformat(json_path.parent.name)
        except ValueError:
            continue
        if day.toordinal() < cutoff:
            continue
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        try:
            records.append(SummaryArchiveRecord(**payload))
        except TypeError:
            continue
    records.sort(key=lambda record: (record.date, record.extracted_at, record.digest_id), reverse=True)
    return records


def render_summary_markdown(
    document: RawSourceDocument,
    summary: Mapping[str, Any],
    *,
    model: str = "",
    extracted_at: str | None = None,
) -> str:
    """Render the summary JSON as a compact markdown page for human scan."""
    meta = document.metadata or {}
    extracted_at = (
        extracted_at or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )

    def _section(title: str, body: str) -> str:
        body = (body or "").strip()
        if not body:
            return ""
        return f"## {title}\n\n{body}\n"

    headline = (summary.get("headline") or "").strip()
    macro = (summary.get("macro_view") or "").strip()
    vol = (summary.get("vol_view") or "").strip()
    risks = (summary.get("risks") or "").strip()
    strategies = summary.get("strategies_discussed") or []
    quotes = summary.get("notable_quotes") or []
    tickers = summary.get("tickers") or []
    actionable = summary.get("actionable_ideas_present")

    header = [
        f"# {document.title or meta.get('video_id', 'Untitled')}",
        "",
        f"- **Source**: {document.source_type}:{document.source_name}",
        f"- **URL**: {document.url or 'n/a'}",
        f"- **Author/Channel**: {document.author or 'unknown'}",
        f"- **Published**: {document.published_at or 'unknown'}",
        f"- **Summary model**: {model or 'unknown'}",
        f"- **Summarized at**: {extracted_at}",
        f"- **Actionable ideas present**: {actionable}",
        "",
    ]
    if headline:
        header += [f"> {headline}", ""]

    sections = [
        "\n".join(header),
        _section("Macro view", macro),
        _section("Vol / IV view", vol),
    ]

    if tickers:
        ticker_lines = ["## Tickers", ""]
        for item in tickers:
            if not isinstance(item, Mapping):
                continue
            sym = (item.get("ticker") or "").upper()
            bias = item.get("bias") or ""
            note = (item.get("notes") or "").strip()
            ticker_lines.append(f"- **{sym}** — _{bias}_ — {note}")
        sections.append("\n".join(ticker_lines) + "\n")

    if strategies:
        sections.append(
            "## Strategies discussed\n\n"
            + "\n".join(f"- {s}" for s in strategies if isinstance(s, str))
            + "\n"
        )

    if quotes:
        sections.append(
            "## Notable quotes\n\n"
            + "\n".join(f"> {q}" for q in quotes if isinstance(q, str))
            + "\n"
        )

    sections.append(_section("Risks", risks))
    sections.append(
        "## Raw JSON\n\n```json\n" + json.dumps(summary, indent=2, ensure_ascii=False) + "\n```\n"
    )

    return "\n".join(section for section in sections if section).rstrip() + "\n"


def write_summary(
    summary_root: Path,
    document: RawSourceDocument,
    summary: Mapping[str, Any],
    *,
    model: str = "",
    idea_date: date | None = None,
) -> Path:
    path = summary_path(summary_root, document, idea_date=idea_date)
    json_path = summary_json_path(summary_root, document, idea_date=idea_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    extracted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    path.write_text(
        render_summary_markdown(
            document, summary, model=model, extracted_at=extracted_at
        ),
        encoding="utf-8",
    )
    record = build_summary_record(
        document,
        summary,
        model=model,
        extracted_at=extracted_at,
    )
    json_path.write_text(
        json.dumps(record.__dict__, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("wrote summary %s", path)
    return path
