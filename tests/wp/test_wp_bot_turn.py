"""Focused unit tests for bot/wp_turn.py extraction and canonical tool registry sync (PR 6.4 / SF-03)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ecom_ops.bot import chat_agent, wp_session, wp_turn
from ecom_ops.wp_ops.tools.registry import (
    TOOL_CAPABILITIES,
    TOOL_REGISTRY,
    WP_TOOL_LABELS,
    WP_TOOL_SHORT_LABELS,
)


def test_chat_agent_labels_and_capabilities_synced_with_canonical_registry() -> None:
    """Zero drift between chat_agent labels/capabilities and wp_ops.tools.registry."""
    assert chat_agent._WP_TOOL_LABELS is WP_TOOL_SHORT_LABELS
    assert wp_turn.WP_TOOL_LABELS is WP_TOOL_LABELS
    assert wp_turn.WP_TOOL_SHORT_LABELS is WP_TOOL_SHORT_LABELS
    assert wp_turn.TOOL_CAPABILITIES is TOOL_CAPABILITIES
    assert set(chat_agent._WP_TOOL_LABELS) == set(TOOL_REGISTRY)

    caps_text = chat_agent.tool_capabilities()
    for group_tools in TOOL_CAPABILITIES.values():
        for tool_name in group_tools:
            assert tool_name in caps_text, f"{tool_name} missing from tool_capabilities()"

    # Prompt budget regression check
    full_prompt, _ver = chat_agent._build_system_prompt(mode="chat_wp_file")
    assert len(full_prompt) < 12986


def test_wp_session_image_validation_reexported_on_chat_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Image validation lives cleanly in wp_session with compatibility aliases on chat_agent."""
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    assert chat_agent._validate_chat_image_paths is wp_session.validate_chat_image_paths
    assert chat_agent._is_allowed_chat_image_path is wp_session.is_allowed_chat_image_path
    assert chat_agent._valid_active_wp_review is wp_session.valid_active_wp_review
    assert chat_agent._merge_active_wp_review is wp_session.merge_active_wp_review
    assert chat_agent._pick_followup_images is wp_session.pick_followup_images

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    good = tmp_path / "shot.png"
    good.write_bytes(png_bytes)
    assert chat_agent._validate_chat_image_paths([str(good)]) == [str(good.resolve())]


def test_wp_turn_preserves_chat_agent_completion_and_file_loop_monkeypatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patching chat_agent.chat_completion & run_bounded_file_tool_loop flows through wp_turn."""
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, cap: True
    )

    class _FakeRouter:
        def any_configured(self) -> bool:
            return True

        def has_route_outside_openrouter_budget(self) -> bool:
            return False

        def preferred_route_id(self) -> str:
            return "openrouter"

    monkeypatch.setattr(chat_agent, "get_model_router", lambda: _FakeRouter())

    seen_profiles: list[str | None] = []

    def _fake_chat_completion(
        _messages: list[dict[str, Any]],
        *,
        profile_id: str | None = None,
        **_kwargs: Any,
    ) -> tuple[str, float]:
        seen_profiles.append(profile_id)
        return (
            '<tool_call>{"name":"wp_search_component","arguments":{"site_id":"mock-staging","kind":"theme","slug":"azom-child","query":"button"}}</tool_call>',
            0.002,
        )

    loop_calls: list[dict[str, Any]] = []

    def _fake_file_loop(content: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        loop_calls.append({"content": content, "actor": kwargs.get("actor"), "allow_wp_first": kwargs.get("allow_wp_first")})
        return type(
            "_FakeLoop",
            (),
            {
                "handled": True,
                "calls": 1,
                "content": "Hittade knappen i style.css.",
                "followup_cost_usd": 0.001,
                "site_id": "mock-staging",
                "operation_ids": (),
                "stage_plan_id": None,
                "review_stage_plan_id": None,
                "attachments": (),
                "model_error": False,
                "tool_seconds": 0.05,
                "active_review_meta": None,
            },
        )()

    monkeypatch.setattr(chat_agent, "chat_completion", _fake_chat_completion)
    monkeypatch.setattr(chat_agent, "run_bounded_file_tool_loop", _fake_file_loop)

    res = chat_agent.run_chat(
        "kolla style.css för knappen på mock-staging",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
    )
    assert res.text == "Hittade knappen i style.css."
    assert seen_profiles == ["wp_tool_router"]
    assert len(loop_calls) == 1
    assert loop_calls[0]["actor"] == "oscar"
    assert loop_calls[0]["allow_wp_first"] is True
