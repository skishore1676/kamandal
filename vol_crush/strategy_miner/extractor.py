"""
Module 0: Strategy Miner — Transcript Extraction

Reads transcript files from data/transcripts/, sends each to the LLM
for strategy extraction, and collects all candidates.
"""

import json
import logging
from pathlib import Path

from vol_crush.core.models import ExtractedStrategyCandidate
from vol_crush.integrations.llm import LLMClient
from vol_crush.strategy_miner.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_PROMPT,
)

logger = logging.getLogger("vol_crush.strategy_miner.extractor")


def load_transcripts(transcripts_dir: Path) -> list[tuple[str, str]]:
    """Load all .txt transcript files from a directory.

    Returns list of (filename, content) tuples, sorted by name.
    """
    transcripts = []
    for path in sorted(transcripts_dir.glob("*.txt")):
        content = path.read_text(encoding="utf-8").strip()
        if content:
            transcripts.append((path.name, content))
            logger.info("Loaded transcript: %s (%d chars)", path.name, len(content))

    logger.info("Total transcripts loaded: %d", len(transcripts))
    return transcripts


def extract_from_transcript(
    llm: LLMClient,
    source_file: str,
    transcript: str,
) -> list[ExtractedStrategyCandidate]:
    """Extract strategy candidates from a single transcript using the LLM.

    Returns a list of ExtractedStrategyCandidate objects.
    """
    logger.info("Extracting strategies from: %s", source_file)

    user_prompt = EXTRACTION_USER_PROMPT.format(
        source_file=source_file,
        transcript=transcript,
    )

    response = llm.chat_json(
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.1,
        max_tokens=4096,
    )

    raw_strategies = response.get("strategies", [])
    logger.info("Extracted %d strategies from %s", len(raw_strategies), source_file)

    candidates = []
    for raw in raw_strategies:
        candidate = ExtractedStrategyCandidate(
            source_file=source_file,
            trader_name=raw.get("trader_name", "Unknown"),
            show_name=raw.get("show_name", "Unknown"),
            strategy_name=raw.get("strategy_name", "Unnamed"),
            structure=raw.get("structure", "custom"),
            description=raw.get("description", ""),
            underlyings=raw.get("underlyings", []),
            iv_rank_filter=raw.get("iv_rank_filter", ""),
            dte_preference=raw.get("dte_preference", ""),
            delta_targets=raw.get("delta_targets", ""),
            spread_width=raw.get("spread_width", ""),
            profit_target=raw.get("profit_target", ""),
            loss_management=raw.get("loss_management", ""),
            roll_rules=raw.get("roll_rules", ""),
            position_sizing=raw.get("position_sizing", ""),
            allocation_notes=raw.get("allocation_notes", ""),
            win_rate_claimed=raw.get("win_rate_claimed", ""),
            annual_return_claimed=raw.get("annual_return_claimed", ""),
            portfolio_greek_notes=raw.get("portfolio_greek_notes", ""),
            key_quotes=raw.get("key_quotes", []),
            confidence=raw.get("confidence", "medium"),
        )
        candidates.append(candidate)
        logger.debug("  -> %s", candidate.summary())

    return candidates


def extract_all(
    llm: LLMClient,
    transcripts_dir: Path,
) -> list[ExtractedStrategyCandidate]:
    """Extract strategies from ALL transcripts in the directory.

    Returns the full list of candidates across all transcripts.
    """
    transcripts = load_transcripts(transcripts_dir)

    if not transcripts:
        logger.warning("No transcripts found in %s", transcripts_dir)
        return []

    all_candidates: list[ExtractedStrategyCandidate] = []

    for source_file, content in transcripts:
        try:
            candidates = extract_from_transcript(llm, source_file, content)
            all_candidates.extend(candidates)
        except Exception as e:
            logger.error("Failed to extract from %s: %s", source_file, e)
            continue

    logger.info(
        "Total candidates extracted: %d from %d transcripts",
        len(all_candidates),
        len(transcripts),
    )
    return all_candidates


def candidates_to_json(candidates: list[ExtractedStrategyCandidate]) -> str:
    """Serialize candidates to JSON string for the distillation prompt."""
    from dataclasses import asdict

    data = [asdict(c) for c in candidates]
    return json.dumps(data, indent=2)


def save_candidates(
    candidates: list[ExtractedStrategyCandidate],
    output_path: Path,
) -> None:
    """Save raw candidates to a JSON file for inspection."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict

    data = [asdict(c) for c in candidates]
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Saved %d candidates to %s", len(candidates), output_path)
