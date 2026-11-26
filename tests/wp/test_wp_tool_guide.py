"""Tests for WP tool guide budgets, Telegram menu commands, and critical plugin warnings."""

from __future__ import annotations

from ecom_ops.bot.chat_agent import (
    _build_system_prompt,
    run_chat,
)
from ecom_ops.bot.openclaw_commands import (
    TELEGRAM_MENU_COMMANDS,
    CommandContext,
    cmd_tools,
)
from ecom_ops.bot.site_content import site_content_turn
from ecom_ops.bot.store import ConversationStore
from ecom_ops.wp_ops.agent_tools import WP_TOOL_GUIDE


def test_critical_plugin_toggle_warns_and_sets_critical_flag(monkeypatch):

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")

    def boom(*_args, **_kwargs):
        raise AssertionError("llm must not run")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)

    turn = site_content_turn("stäng av plugin woocommerce")
    assert turn.consumed is True
    assert turn.pending is not None
    assert turn.pending.kind == "plugin_status"
    assert turn.pending.payload["plugin"] == "woocommerce/woocommerce"
    assert turn.pending.payload["status"] == "inactive"
    assert turn.pending.payload["critical"] is True
    propose_text = dict(turn.results)["propose_plugin_status"]
    assert propose_text.startswith("VARNING:")

    result = run_chat("stäng av plugin woocommerce")
    assert result.pending_action is not None
    assert result.pending_action.kind == "plugin_status"
    assert result.pending_action.payload["critical"] is True


def test_telegram_menu_includes_wp_and_cmd_tools_lists_new_tools(tmp_path):
    menu_names = [entry["command"] for entry in TELEGRAM_MENU_COMMANDS]
    assert "wp" in menu_names

    ctx = CommandContext(
        chat_id=42,
        text="/tools",
        args="",
        store=ConversationStore(path=tmp_path / "tg.json"),
    )
    output = cmd_tools(ctx)
    for expected in (
        "wp_component_context",
        "wp_list_components",
        "wp_theme_overview",
        "wp_plugin_overview",
        "wp_list_component_tree",
        "wp_search_component",
        "wp_read_theme_plugin_file",
        "wp_open_review_workspace",
        "wp_workspace_*",
    ):
        assert expected in output


def test_prompt_and_wp_tool_guide_budgets() -> None:
    assert len(WP_TOOL_GUIDE) <= 7000
    full_prompt, _version = _build_system_prompt()
    assert len(full_prompt) < 12986
