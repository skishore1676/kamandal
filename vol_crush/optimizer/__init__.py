"""
Module 2: Portfolio Optimizer (The Edge)

2A: Deterministic optimization engine — enumerates idea combinations,
    scores against portfolio Greek targets, enforces hard constraints.
    Pure code, no LLM.

2B: LLM refinement loop — reviews optimizer output with market context,
    can reject or modify but never add. The LLM is the refinement layer;
    the optimizer is the gatekeeper.
"""

