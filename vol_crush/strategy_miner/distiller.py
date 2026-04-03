"""
Module 0: Strategy Miner — Distillation

Takes all extracted candidates and uses the LLM to synthesize them into
3-5 canonical strategies with precise, machine-executable parameters.
"""

import json
import logging
from typing import Any

from vol_crush.core.models import ExtractedStrategyCandidate, Strategy
from vol_crush.integrations.llm import LLMClient
from vol_crush.strategy_miner.extractor import candidates_to_json
from vol_crush.strategy_miner.prompts import (
    DISTILLATION_SYSTEM_PROMPT,
    DISTILLATION_USER_PROMPT,
)

logger = logging.getLogger("vol_crush.strategy_miner.distiller")


def distill_strategies(
    llm: LLMClient,
    candidates: list[ExtractedStrategyCandidate],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Distill extracted candidates into canonical strategies.

    Returns:
        (strategies_list, portfolio_guidelines_dict)
    """
    logger.info("Distilling %d candidates into canonical strategies", len(candidates))

    candidates_json = candidates_to_json(candidates)

    user_prompt = DISTILLATION_USER_PROMPT.format(
        candidates_json=candidates_json,
    )

    response = llm.chat_json(
        system_prompt=DISTILLATION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.1,
        max_tokens=8192,
    )

    strategies_raw = response.get("strategies", [])
    portfolio_guidelines = response.get("portfolio_guidelines", {})

    logger.info(
        "Distillation produced %d canonical strategies",
        len(strategies_raw),
    )

    for strat in strategies_raw:
        logger.info(
            "  -> [%s] %s (%s) — sources: %s",
            strat.get("id"),
            strat.get("name"),
            strat.get("structure"),
            ", ".join(strat.get("source_traders", [])),
        )

    if portfolio_guidelines:
        logger.info("Portfolio guidelines extracted:")
        for key, val in portfolio_guidelines.items():
            if key != "notes":
                logger.info("  %s: %s", key, val)

    return strategies_raw, portfolio_guidelines


def build_strategy_objects(strategies_raw: list[dict[str, Any]]) -> list[Strategy]:
    """Convert raw distilled strategy dicts into Strategy model objects."""
    strategies = []
    for raw in strategies_raw:
        try:
            strategy = Strategy.from_dict(raw)
            strategies.append(strategy)
        except Exception as e:
            logger.error("Failed to parse strategy %s: %s", raw.get("id", "?"), e)
            continue
    return strategies

