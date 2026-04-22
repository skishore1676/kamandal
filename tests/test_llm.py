"""Tests for the provider-agnostic LLM client."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from vol_crush.integrations.llm import LLMClient, build_llm_client


def test_build_llm_client_codex_cli_without_api_key(monkeypatch):
    monkeypatch.setattr(
        "vol_crush.integrations.llm._discover_codex_binary",
        lambda _configured: "/tmp/codex",
    )

    client = build_llm_client(
        {
            "llm": {
                "provider": "codex_cli",
                "model": "gpt-5.4",
                "codex_binary": "/tmp/codex",
            }
        }
    )

    assert client.provider == "codex_cli"
    assert client.model == "gpt-5.4"
    assert client.codex_binary == "/tmp/codex"


def test_codex_cli_chat_json_parses_jsonl_agent_message(monkeypatch, tmp_path):
    recorded = {}
    fake_binary = tmp_path / "codex"
    fake_binary.write_text("")

    def _fake_run(args, input, capture_output, text, check, timeout):
        recorded["args"] = args
        recorded["input"] = input
        recorded["timeout"] = timeout
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"thread.started","thread_id":"t1"}\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"ok\\":true}"}}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    client = LLMClient(
        api_key="",
        model="gpt-5.4",
        provider="codex_cli",
        codex_binary=str(fake_binary),
        codex_workdir=str(tmp_path),
        codex_timeout_seconds=77,
    )

    payload = client.chat_json("Return JSON only.", "Say hello.")

    assert payload == {"ok": True}
    assert recorded["args"][:3] == [str(fake_binary), "exec", "--skip-git-repo-check"]
    assert "-C" in recorded["args"]
    assert str(tmp_path) in recorded["args"]
    assert "--model" in recorded["args"]
    assert "gpt-5.4" in recorded["args"]
    assert recorded["timeout"] == 77
    assert "<system>" in recorded["input"]
    assert "<user>" in recorded["input"]


def test_codex_cli_uses_fallback_model_on_failure(monkeypatch, tmp_path):
    calls = []
    fake_binary = tmp_path / "codex"
    fake_binary.write_text("")

    def _fake_run(args, input, capture_output, text, check, timeout):
        calls.append(args)
        model = args[args.index("--model") + 1]
        if model == "gpt-5.4":
            return SimpleNamespace(returncode=1, stdout="", stderr="primary failure")
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    client = LLMClient(
        api_key="",
        model="gpt-5.4",
        provider="codex_cli",
        codex_binary=str(fake_binary),
        codex_workdir=str(tmp_path),
        fallback_model="gpt-5.4-mini",
    )

    assert client.chat("system", "user") == "done"
    assert [args[args.index("--model") + 1] for args in calls] == [
        "gpt-5.4",
        "gpt-5.4-mini",
    ]


def test_codex_cli_raises_when_no_message_returned(monkeypatch, tmp_path):
    fake_binary = tmp_path / "codex"
    fake_binary.write_text("")

    def _fake_run(args, input, capture_output, text, check, timeout):
        return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    client = LLMClient(
        api_key="",
        model="gpt-5.4",
        provider="codex_cli",
        codex_binary=str(fake_binary),
        codex_workdir=str(tmp_path),
    )

    with pytest.raises(RuntimeError, match="did not return a final agent message"):
        client.chat("system", "user")
