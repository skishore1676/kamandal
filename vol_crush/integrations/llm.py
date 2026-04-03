"""
OpenAI LLM client wrapper.

Provides a simple interface for chat completions and JSON-mode responses.
Used by Strategy Miner (Module 0), Idea Scraper (Module 1), and
LLM Refinement (Module 2B).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("vol_crush.integrations.llm")


class LLMClient:
    """Wrapper around OpenAI chat completions."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request, return the response text."""
        logger.debug("LLM request: model=%s, temp=%.1f", self.model, temperature)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )

        text = response.choices[0].message.content or ""
        logger.debug("LLM response: %d chars", len(text))
        return text

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> dict[str, Any] | list[Any]:
        """Send a chat request expecting JSON response. Parses and returns the object."""
        logger.debug("LLM JSON request: model=%s", self.model)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

        text = response.choices[0].message.content or "{}"
        logger.debug("LLM JSON response: %d chars", len(text))

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse LLM JSON response: %s", e)
            logger.error("Raw response: %s", text[:500])
            raise
