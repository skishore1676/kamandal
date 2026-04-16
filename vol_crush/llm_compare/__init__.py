"""LLM comparison harness for running multiple models against the same transcript."""

from vol_crush.llm_compare.service import ComparisonResult, run_comparison

__all__ = ["ComparisonResult", "run_comparison"]
