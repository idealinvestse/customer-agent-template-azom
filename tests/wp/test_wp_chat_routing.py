"""Tests for WP chat routing fallthrough with and without wp_ops_context."""

from __future__ import annotations

import json

from _helpers import (
    tool_call,
    write_theme_file,
)

from ecom_ops.bot.chat_agent import run_chat
from ecom_ops.wp_ops.agent_tools import context_for_storage

SOURCE_SECRET = "AZOM-SECRET-SRC-91b7"


def test_routing_fallthrough_without_context_refuses_without_llm(tmp_path, monkeypatch, wp_env):
    wp_env(mock="1", files_ssh="0", sandbox="0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")

    def fail_llm(*_args, **_kwargs):
        raise AssertionError("chat_completion must not be called without wp_ops_context")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fail_llm)

    result = run_chat(
        "ändra functions.php",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
    )
    assert result.pending_action is None
    assert "ändras inte" in result.text.lower() or "inte" in result.text.lower()


def test_routing_fallthrough_with_context_executes_file_loop_and_max_tokens(
    tmp_path, monkeypatch, wp_env
):
    wp_env(mock="1", files_ssh="0", sandbox="0")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    monkeypatch.delenv("AZOM_TELEGRAM_MODEL_ROUTE", raising=False)

    write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "functions.php",
        f"<?php\n// {SOURCE_SECRET}\nadd_action('init', 'azom_init');\n",
    )

    ctx = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        site_id="mock-staging",
        operation_ids=(),
    )
    assert ctx is not None

    responses = iter(
        [
            tool_call(
                "wp_read_theme_plugin_file",
                {
                    "kind": "theme",
                    "slug": "twentytwentyfive",
                    "path": "functions.php",
                    "start_line": 1,
                    "end_line": 3,
                },
            ),
            "Filen functions.php registrerar en init-hook i temat.",
        ]
    )
    calls: list[tuple[list[dict[str, str]], dict]] = []

    def fake_chat(messages, **kwargs):
        calls.append((list(messages), dict(kwargs)))
        return next(responses), 0.001

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)

    result = run_chat(
        "läs functions.php i temat",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        session={"wp_ops_context": ctx},
    )

    assert len(calls) == 2
    assert calls[0][1]["max_tokens"] >= 2048
    assert calls[1][1]["max_tokens"] >= 2048
    assert any(SOURCE_SECRET in json.dumps(m) for m in calls[1][0])
    assert SOURCE_SECRET not in result.text
    assert "<tool_call>" not in result.text
    assert "init-hook" in result.text
    assert result.wp_ops_context is not None
    assert result.wp_ops_context["site_id"] == "mock-staging"
