"""
Module 0: Strategy Miner (One-Time Setup)

Extracts trading strategies from historical tastytrade transcripts.
Presents candidates for human review. Approved strategies are written
to config/strategies.yaml.

This module runs once at project setup and rarely again.

Usage:
    python -m vol_crush.strategy_miner
"""

from vol_crush.strategy_miner.extractor import extract_all, extract_from_transcript
from vol_crush.strategy_miner.distiller import distill_strategies
from vol_crush.strategy_miner.review import interactive_review

__all__ = [
    "extract_all",
    "extract_from_transcript",
    "distill_strategies",
    "interactive_review",
]

