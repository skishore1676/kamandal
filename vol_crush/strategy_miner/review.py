"""
Module 0: Strategy Miner — Interactive Review CLI

Presents extracted and distilled strategies to the human for review.
The human approves/rejects/edits strategies before they're written to config.
"""

import json
import logging
import sys
from typing import Any

logger = logging.getLogger("vol_crush.strategy_miner.review")


def print_separator(char: str = "=", width: int = 80) -> None:
    print(char * width)


def print_candidates_summary(candidates: list[dict]) -> None:
    """Print a summary table of all extracted candidates."""
    print_separator()
    print(f"  EXTRACTED CANDIDATES: {len(candidates)} strategies from transcripts")
    print_separator()

    for i, c in enumerate(candidates, 1):
        conf_icon = {"high": "+", "medium": "~", "low": "-"}.get(
            c.get("confidence", ""), "?"
        )
        print(
            f"  {i:2d}. [{conf_icon}] {c.get('trader_name', '?'):20s} | "
            f"{c.get('strategy_name', '?'):30s} | "
            f"{c.get('structure', '?')}"
        )
        if c.get("description"):
            print(f"       {c['description'][:75]}")

    print_separator("-")
    print("  [+] = high confidence   [~] = medium   [-] = low")
    print()


def print_distilled_strategy(idx: int, strat: dict[str, Any]) -> None:
    """Pretty-print a single distilled canonical strategy."""
    print_separator("=")
    print(f"  STRATEGY {idx}: {strat.get('name', '?')}")
    print(f"  ID: {strat.get('id', '?')}")
    print(f"  Structure: {strat.get('structure', '?')}")
    print_separator("-")

    if strat.get("description"):
        print(f"  Description: {strat['description']}")
        print()

    # Filters
    filters = strat.get("filters", {})
    if filters:
        print("  ENTRY FILTERS:")
        if filters.get("iv_rank_min") is not None:
            iv_max = filters.get("iv_rank_max")
            iv_str = f">= {filters['iv_rank_min']}"
            if iv_max is not None:
                iv_str += f", <= {iv_max}"
            print(f"    IV Rank:       {iv_str}")
        if filters.get("dte_range"):
            print(f"    DTE:           {filters['dte_range'][0]} - {filters['dte_range'][1]}")
        if filters.get("delta_range"):
            print(f"    Delta:         {filters['delta_range'][0]} - {filters['delta_range'][1]}")
        if filters.get("spread_width"):
            print(f"    Spread Width:  ${filters['spread_width']}")
        if filters.get("min_credit_to_width_ratio"):
            print(f"    Min Credit/Width: {filters['min_credit_to_width_ratio']:.0%}")
        if filters.get("underlyings"):
            print(f"    Underlyings:   {', '.join(filters['underlyings'])}")
        print()

    # Management
    mgmt = strat.get("management", {})
    if mgmt:
        print("  MANAGEMENT RULES:")
        if mgmt.get("profit_target_pct") is not None:
            print(f"    Profit Target: {mgmt['profit_target_pct']}% of max profit")
        if mgmt.get("max_loss_multiple") is not None:
            print(f"    Max Loss:      {mgmt['max_loss_multiple']}x credit received")
        if mgmt.get("roll_dte_trigger") is not None:
            print(f"    Roll Trigger:  {mgmt['roll_dte_trigger']} DTE")
        if mgmt.get("roll_for_credit") is not None:
            print(f"    Roll for Credit: {'Yes' if mgmt['roll_for_credit'] else 'No'}")
        print()

    # Allocation
    alloc = strat.get("allocation", {})
    if alloc:
        print("  ALLOCATION:")
        if alloc.get("max_bpr_pct") is not None:
            print(f"    Max BPR:           {alloc['max_bpr_pct']}% of portfolio")
        if alloc.get("max_per_position_pct") is not None:
            print(f"    Max Per Position:  {alloc['max_per_position_pct']}% of portfolio")
        if alloc.get("max_positions") is not None:
            print(f"    Max Positions:     {alloc['max_positions']}")
        print()

    # Sources
    sources = strat.get("source_traders", [])
    if sources:
        print(f"  SOURCES: {', '.join(sources)}")

    notes = strat.get("consensus_notes", "")
    if notes:
        print(f"  CONSENSUS: {notes}")

    print()


def print_portfolio_guidelines(guidelines: dict[str, Any]) -> None:
    """Pretty-print portfolio-level guidelines."""
    print_separator("=")
    print("  PORTFOLIO GUIDELINES (synthesized from all sources)")
    print_separator("-")

    key_labels = {
        "beta_weighted_delta_pct": "Beta-Wtd Delta",
        "daily_theta_pct": "Daily Theta",
        "max_gamma_ratio": "Max Gamma/Theta Ratio",
        "max_vega_pct": "Max Vega",
        "max_bpr_utilization_pct": "Max BPR Utilization",
        "hard_bpr_cap_pct": "Hard BPR Cap (kill switch)",
        "max_single_underlying_pct": "Max Single Underlying",
    }

    for key, label in key_labels.items():
        val = guidelines.get(key)
        if val is not None:
            if isinstance(val, list):
                print(f"    {label:30s} {val[0]}% to {val[1]}% of NLV")
            else:
                print(f"    {label:30s} {val}%")

    notes = guidelines.get("notes", "")
    if notes:
        print()
        print(f"  Notes: {notes}")
    print()


def interactive_review(
    strategies_raw: list[dict[str, Any]],
    portfolio_guidelines: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Interactive CLI review of distilled strategies.

    Returns (approved_strategies, portfolio_guidelines) — potentially modified.
    """
    print()
    print_separator("*")
    print("  VOL CRUSH — STRATEGY MINER — HUMAN REVIEW")
    print_separator("*")
    print()

    # Show all distilled strategies
    for i, strat in enumerate(strategies_raw, 1):
        print_distilled_strategy(i, strat)

    # Show portfolio guidelines
    print_portfolio_guidelines(portfolio_guidelines)

    # Review loop
    print_separator("=")
    print("  REVIEW OPTIONS:")
    print("    [a]  Approve ALL strategies as-is")
    print("    [r]  Review one-by-one (approve/reject each)")
    print("    [e]  Export to JSON for manual editing, then re-import")
    print("    [q]  Quit without saving")
    print_separator("-")

    while True:
        choice = input("\n  Your choice [a/r/e/q]: ").strip().lower()

        if choice == "a":
            print("\n  All strategies approved.")
            return strategies_raw, portfolio_guidelines

        elif choice == "r":
            return _review_one_by_one(strategies_raw, portfolio_guidelines)

        elif choice == "e":
            return _export_and_reimport(strategies_raw, portfolio_guidelines)

        elif choice == "q":
            print("\n  Exiting without saving.")
            sys.exit(0)

        else:
            print("  Invalid choice. Please enter a, r, e, or q.")


def _review_one_by_one(
    strategies_raw: list[dict[str, Any]],
    portfolio_guidelines: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Review each strategy individually."""
    approved = []

    for i, strat in enumerate(strategies_raw, 1):
        print()
        print_distilled_strategy(i, strat)

        while True:
            choice = input(f"  Strategy {i} — [a]pprove / [s]kip / [e]dit JSON? ").strip().lower()

            if choice == "a":
                approved.append(strat)
                print(f"    -> Approved: {strat.get('name')}")
                break
            elif choice == "s":
                print(f"    -> Skipped: {strat.get('name')}")
                break
            elif choice == "e":
                strat = _edit_strategy_json(strat)
                approved.append(strat)
                print(f"    -> Approved (edited): {strat.get('name')}")
                break
            else:
                print("    Invalid. Enter a, s, or e.")

    print(f"\n  Approved {len(approved)} of {len(strategies_raw)} strategies.")
    return approved, portfolio_guidelines


def _edit_strategy_json(strat: dict[str, Any]) -> dict[str, Any]:
    """Let user edit a strategy's JSON inline."""
    print("\n  Current JSON (edit and paste back, end with an empty line):")
    print(json.dumps(strat, indent=2))
    print("\n  Paste edited JSON below (empty line to finish):")

    lines = []
    while True:
        line = input()
        if line.strip() == "":
            break
        lines.append(line)

    if lines:
        try:
            edited = json.loads("\n".join(lines))
            print("    -> JSON parsed successfully.")
            return edited
        except json.JSONDecodeError as e:
            print(f"    -> JSON parse error: {e}. Keeping original.")

    return strat


def _export_and_reimport(
    strategies_raw: list[dict[str, Any]],
    portfolio_guidelines: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Export to a temp JSON file for editing, then re-import."""
    from vol_crush.core.config import get_project_root

    export_path = get_project_root() / "data" / "review_export.json"
    export_data = {
        "strategies": strategies_raw,
        "portfolio_guidelines": portfolio_guidelines,
    }

    with open(export_path, "w") as f:
        json.dump(export_data, f, indent=2)

    print(f"\n  Exported to: {export_path}")
    print("  Edit the file, save it, then press Enter to re-import.")
    input("  Press Enter when ready...")

    with open(export_path) as f:
        imported = json.load(f)

    strategies = imported.get("strategies", [])
    guidelines = imported.get("portfolio_guidelines", portfolio_guidelines)

    print(f"  Re-imported {len(strategies)} strategies.")
    return strategies, guidelines

