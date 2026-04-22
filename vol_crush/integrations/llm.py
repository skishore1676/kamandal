"""
LLM client wrapper.

Provides a provider-agnostic chat + JSON interface. Supports OpenAI directly,
OpenRouter (OpenAI-compatible API at a different base_url), and Codex CLI
shell-outs. Used by Strategy Miner (Module 0), Idea Scraper (Module 1), and
LLM Refinement (Module 2B).

Audio transcription (Whisper) still requires provider=openai.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("vol_crush.integrations.llm")

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_JSON_OBJECT_OK_PROVIDERS = {"openai"}
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CODEX_TIMEOUT_SECONDS = 300
_CODEX_BINARY_CANDIDATES = (
    "/usr/local/bin/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-x64/vendor/x86_64-apple-darwin/codex/codex",
)


def _discover_codex_binary(configured_path: str | None = None) -> str:
    candidates: list[str] = []
    if configured_path:
        candidates.append(str(Path(configured_path).expanduser()))

    which_path = shutil.which("codex")
    if which_path:
        candidates.append(which_path)

    candidates.extend(_CODEX_BINARY_CANDIDATES)

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate

    raise RuntimeError(
        "Codex CLI binary not found. Set llm.codex_binary in config.yaml or "
        "VOL_CRUSH_CODEX_BINARY in the environment."
    )


def _extract_codex_message(stdout: str) -> str:
    message = ""
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            message = item.get("text") or message

    if message:
        return message

    raise RuntimeError("Codex CLI did not return a final agent message.")


class LLMClient:
    """Wrapper around OpenAI-compatible chat completions.

    provider:
        - "openai"     → api.openai.com (default)
        - "openrouter" → openrouter.ai/api/v1 (access to Claude, GPT, Gemini, ...)
        - "codex_cli"  → local Codex CLI `exec` shell-out
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        provider: str = "openai",
        base_url: str | None = None,
        fallback_model: str | None = None,
        codex_binary: str | None = None,
        codex_workdir: str | None = None,
        codex_timeout_seconds: int = _DEFAULT_CODEX_TIMEOUT_SECONDS,
    ):
        provider = (provider or "openai").lower()
        if provider not in {"openai", "openrouter", "codex_cli"}:
            raise ValueError(
                "Unsupported LLM provider: "
                f"{provider!r}. Use 'openai', 'openrouter', or 'codex_cli'."
            )

        self.provider = provider
        self.model = model
        self.fallback_model = fallback_model or None
        self.codex_binary = None
        self.codex_workdir = str(
            Path(codex_workdir).expanduser()
            if codex_workdir
            else _PROJECT_ROOT
        )
        self.codex_timeout_seconds = int(codex_timeout_seconds)
        self.client = None

        if provider == "codex_cli":
            self.codex_binary = _discover_codex_binary(codex_binary)
            return

        from openai import OpenAI

        if base_url is None and provider == "openrouter":
            base_url = _OPENROUTER_BASE_URL

        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def _create_codex_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
    ) -> str:
        prompt = "\n\n".join(
            f"<{message['role']}>\n{message['content'].strip()}\n</{message['role']}>"
            for message in messages
        )
        args = [
            self.codex_binary or _discover_codex_binary(None),
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--json",
            "--ephemeral",
            "-C",
            self.codex_workdir,
            "--model",
            model,
            "-",
        ]
        result = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
            timeout=self.codex_timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"Codex CLI failed with exit code {result.returncode}: {detail}"
            )
        return _extract_codex_message(result.stdout)

    def _create_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        extra_kwargs: dict[str, Any],
    ) -> str:
        if self.provider == "codex_cli":
            return self._create_codex_completion(model=model, messages=messages)
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **extra_kwargs,
        )
        return response.choices[0].message.content or ""

    def _with_fallback(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        extra_kwargs: dict[str, Any],
    ) -> str:
        try:
            return self._create_completion(
                self.model, messages, temperature, max_tokens, extra_kwargs
            )
        except Exception as exc:
            if not self.fallback_model or self.fallback_model == self.model:
                raise
            logger.warning(
                "Primary model %s failed (%s: %s); retrying with fallback %s",
                self.model,
                type(exc).__name__,
                exc,
                self.fallback_model,
            )
            return self._create_completion(
                self.fallback_model,
                messages,
                temperature,
                max_tokens,
                extra_kwargs,
            )

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        logger.debug(
            "LLM request: provider=%s model=%s temp=%.1f",
            self.provider,
            self.model,
            temperature,
        )
        text = self._with_fallback(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_kwargs={},
        )
        logger.debug("LLM response: %d chars", len(text))
        return text

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> dict[str, Any] | list[Any]:
        """Chat completion expecting JSON response. Parses and returns the object.

        Uses native response_format={"type":"json_object"} on providers that
        support it (OpenAI). Otherwise relies on prompt-based JSON plus robust
        extraction — needed for OpenRouter since not all routed models support
        the json_object response_format (Claude in particular).
        """
        use_native_json = self.provider in _JSON_OBJECT_OK_PROVIDERS
        if use_native_json:
            effective_system = system_prompt
            extra_kwargs: dict[str, Any] = {
                "response_format": {"type": "json_object"}
            }
        else:
            effective_system = (
                system_prompt
                + "\n\nRespond with a single valid JSON object only. "
                "Do not wrap in markdown code fences. Do not include any prose "
                "before or after the JSON."
            )
            extra_kwargs = {}

        logger.debug(
            "LLM JSON request: provider=%s model=%s native_json=%s",
            self.provider,
            self.model,
            use_native_json,
        )
        text = self._with_fallback(
            messages=[
                {"role": "system", "content": effective_system},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_kwargs=extra_kwargs,
        ) or "{}"
        logger.debug("LLM JSON response: %d chars", len(text))

        return _parse_json_response(text)


def _parse_json_response(text: str) -> dict[str, Any] | list[Any]:
    """Parse a JSON response, tolerating code fences and surrounding prose."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass

    first_obj = text.find("{")
    first_arr = text.find("[")
    starts = [i for i in (first_obj, first_arr) if i != -1]
    if starts:
        start = min(starts)
        close = "}" if text[start] == "{" else "]"
        end = text.rfind(close)
        if end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

    logger.error("Failed to parse LLM JSON response. Raw: %s", text[:500])
    raise json.JSONDecodeError("Could not extract JSON from LLM response", text, 0)


def build_llm_client(config: dict[str, Any]) -> LLMClient:
    """Construct an LLMClient from the merged config.

    Reads the ``llm:`` section first; falls back to the legacy ``openai:``
    section for backward compatibility. Raises RuntimeError if no API key is
    configured.
    """
    llm_cfg = config.get("llm") or {}
    openai_cfg = config.get("openai") or {}

    provider = (llm_cfg.get("provider") or "openai").lower()
    api_key = llm_cfg.get("api_key") or ""
    model = llm_cfg.get("model") or ""
    fallback_model = llm_cfg.get("fallback_model") or None
    base_url = llm_cfg.get("base_url") or None
    codex_binary = llm_cfg.get("codex_binary") or None
    codex_workdir = llm_cfg.get("codex_workdir") or None
    codex_timeout_seconds = int(
        llm_cfg.get("codex_timeout_seconds") or _DEFAULT_CODEX_TIMEOUT_SECONDS
    )

    if not api_key and provider == "openai":
        api_key = openai_cfg.get("api_key", "")
    if not model:
        if provider == "openrouter":
            model = "anthropic/claude-sonnet-4.5"
        elif provider == "codex_cli":
            model = "gpt-5.4"
        else:
            model = openai_cfg.get("model") or "gpt-4o"

    if provider != "codex_cli" and not api_key:
        raise RuntimeError(
            f"LLM API key not configured for provider={provider!r}. "
            "Set llm.api_key in config.yaml or VOL_CRUSH_LLM_API_KEY in .env "
            "(or OPENROUTER_API_KEY / VOL_CRUSH_OPENAI_API_KEY)."
        )

    return LLMClient(
        api_key=api_key,
        model=model,
        provider=provider,
        base_url=base_url,
        fallback_model=fallback_model,
        codex_binary=codex_binary,
        codex_workdir=codex_workdir,
        codex_timeout_seconds=codex_timeout_seconds,
    )
