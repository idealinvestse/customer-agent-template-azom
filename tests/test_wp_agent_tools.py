from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.store import ConversationStore
from ecom_ops.wp_ops.agent_tools import (
    WP_TOOL_MAX_CALLS,
    ToolOutput,
    WpAgentTools,
    context_for_storage,
    run_bounded_wp_tool_loop,
)
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def agent_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    service = WpOpsService(registry_path=ROOT / "config" / "wp_sites.mock.yaml", data_dir=tmp_path)
    return WpAgentTools(service), tmp_path


def call(name: str, args: dict[str, Any] | None = None) -> str:
    return f'<tool_call>{{"name":"{name}","arguments":{json.dumps(args or {})}}}</tool_call>'


def test_named_reads_are_registered_sanitized_and_permission_checked(agent_tools):
    tools, _ = agent_tools
    sites = tools.execute("wp_list_sites", {}, actor="jonatan")
    assert sites.data["ok"] is True
    assert {row["site_id"] for row in sites.data["sites"]} == {"mock-shop", "mock-staging"}
    assert "canonical_url" not in json.dumps(sites.data)
    assert "secret_ref" not in json.dumps(sites.data)

    status = tools.execute("wp_site_status", {"site_id": "mock-shop"}, actor="jonatan")
    assert status.data["ok"] is True
    assert status.data["site_id"] == "mock-shop"
    assert status.data["live_calls"] is False
    assert "ssh_host" not in json.dumps(status.data)

    denied = tools.execute("wp_site_status", {"site_id": "mock-shop"}, actor="not-a-role")
    assert denied.data["status"] == "denied"
    assert denied.data["error_class"] == "AccessDenied"


def test_unknown_site_and_unconfigured_security_are_never_green(agent_tools):
    tools, _ = agent_tools
    unknown = tools.execute("wp_site_status", {"site_id": "private-shop"}, actor="oscar")
    assert unknown.data["ok"] is False
    assert unknown.data["error_class"] == "UnknownSiteOrDenied"
    assert "private-shop" not in json.dumps(unknown.data)

    security = tools.execute(
        "wp_security_findings", {"site_id": "mock-shop"}, actor="jonatan"
    )
    assert security.data["status"] == "unknown"
    assert security.data["coverage"] == "not_configured"
    assert security.data["findings"] is None
    assert "clean" not in json.dumps(security.data).lower()


def test_backup_unknown_and_plan_preview_is_locked_non_executable(agent_tools):
    tools, data_dir = agent_tools
    backup = tools.execute("wp_backup_status", {"site_id": "mock-shop"}, actor="jonatan")
    assert backup.data["status"] == "unknown"
    assert backup.data["backups"] == []

    plan = tools.execute(
        "wp_prepare_update_plan",
        {"site_id": "mock-shop", "component_filter": ["core", "plugins"]},
        actor="oscar",
    )
    record = plan.data["plan"]
    assert plan.data["status"] == "unsupported"
    assert record["target_versions"] == {}
    assert record["executable"] is False
    assert record["approval_available"] is False
    assert record["digest"]
    assert record["status"] == "unsupported"
    assert (data_dir / "wp_ops_plans.json").is_file()
    assert plan.operation_ids == ()
    stored = json.loads((data_dir / "wp_ops_plans.json").read_text(encoding="utf-8"))
    assert stored["plans"][0]["plan_id"] == record["plan_id"]
    digest_payload = {
        key: value
        for key, value in stored["plans"][0].items()
        if key not in {"digest", "_scope", "_scope_digest"}
    }
    canonical = json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == record["digest"]

    stored["plans"][0]["site_id"] = "mock-staging"
    (data_dir / "wp_ops_plans.json").write_text(json.dumps(stored), encoding="utf-8")
    assert tools._load_plans() == []


def test_restore_preview_requires_site_scoped_backup_and_stays_unsupported(agent_tools):
    tools, _ = agent_tools
    missing = tools.execute(
        "wp_prepare_restore_plan",
        {"site_id": "mock-shop", "backup_id": "bk-000000000000"},
        actor="oscar",
    )
    assert missing.data["ok"] is False
    assert missing.data["error_class"] == "UnknownBackupOrDenied"


def test_open_plans_are_scoped_to_actor_site_channel_and_conversation(agent_tools):
    tools, _ = agent_tools
    plan = {
        "plan_id": "wp-plan-0123456789abcdef",
        "site_id": "mock-shop",
        "operation": "update",
        "status": "pending_approval",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "created_by": "oscar",
    }
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    scope = {"channel": "telegram", "conversation_id": "telegram:42"}
    scope_canonical = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    tools._store_plan(
        {
            **plan,
            "digest": hashlib.sha256(canonical.encode()).hexdigest(),
            "_scope": scope,
            "_scope_digest": hashlib.sha256(scope_canonical.encode()).hexdigest(),
        }
    )

    other_conversation = tools.execute(
        "wp_list_open_operations",
        {"site_id": "mock-shop"},
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:99",
    )
    assert other_conversation.data["operations"] == []

    same_conversation = tools.execute(
        "wp_list_open_operations",
        {"site_id": "mock-shop"},
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
    )
    assert same_conversation.data["operations"] == [
        {"operation_id": "wp-plan-0123456789abcdef", "type": "update", "state": "pending_approval"}
    ]


def test_invalid_arguments_and_unknown_model_tools_fail_closed(agent_tools):
    tools, _ = agent_tools
    extra = tools.execute(
        "wp_site_status", {"site_id": "mock-shop", "host": "attacker.invalid"}, actor="oscar"
    )
    assert extra.data["error_class"] == "InvalidToolArguments"
    unknown = tools.execute("wp_shell", {"command": "id"}, actor="oscar")
    assert unknown.data["error_class"] == "InvalidToolRequest"


def test_loop_executes_at_most_three_calls_and_keeps_site_context():
    class FakeTools:
        def __init__(self):
            self.names: list[str] = []

        def execute(
            self, name, arguments, *, actor, sticky_site_id=None, channel="", conversation_id=""
        ):
            del arguments, actor, sticky_site_id, channel, conversation_id
            self.names.append(name)
            return ToolOutput({"ok": True, "status": "ok", "site_id": "mock-shop"}, "mock-shop")

    tools = FakeTools()
    completed = 0

    def complete(messages):
        nonlocal completed
        del messages
        completed += 1
        if completed <= WP_TOOL_MAX_CALLS:
            return call("wp_list_sites"), 0.01
        return call("wp_site_status", {"site_id": "mock-shop"}), 0.01

    result = run_bounded_wp_tool_loop(
        call("wp_list_sites"),
        [],
        complete=complete,
        actor="agent",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
        wp_intent=True,
    )
    assert result.handled is True
    assert "<tool_call>" not in result.content
    assert len(tools.names) == WP_TOOL_MAX_CALLS
    assert result.calls == WP_TOOL_MAX_CALLS
    assert result.site_id == "mock-shop"


def test_loop_enforces_total_result_and_tool_time_budgets(monkeypatch):
    class FullTools:
        def execute(
            self, name, arguments, *, actor, sticky_site_id=None, channel="", conversation_id=""
        ):
            del name, arguments, actor, sticky_site_id, channel, conversation_id
            return ToolOutput({"ok": True, "text": "x" * 3800})

    complete_responses = iter([call("wp_list_sites"), call("wp_list_sites"), "Partial results only."])
    result = run_bounded_wp_tool_loop(
        call("wp_list_sites"),
        [],
        complete=lambda _messages: (next(complete_responses), 0.0),
        actor="agent",
        channel="telegram",
        conversation_id="telegram:42",
        tools=FullTools(),
        wp_intent=True,
    )
    assert result.calls == 3
    assert result.results[-1]["error_class"] == "ToolResultBudgetExceeded"

    class SlowTools:
        def execute(
            self, name, arguments, *, actor, sticky_site_id=None, channel="", conversation_id=""
        ):
            import time

            del name, arguments, actor, sticky_site_id, channel, conversation_id
            time.sleep(0.05)
            return ToolOutput({"ok": True, "status": "ok"})

    monkeypatch.setattr("ecom_ops.wp_ops.agent_tools.WP_TOOL_MAX_SECONDS", 0.001)
    timed = run_bounded_wp_tool_loop(
        call("wp_list_sites"),
        [],
        complete=lambda _messages: ("Should not be reached.", 0.0),
        actor="agent",
        channel="telegram",
        conversation_id="telegram:42",
        tools=SlowTools(),
        wp_intent=True,
    )
    assert timed.calls == 1
    assert timed.results[0]["error_class"] == "ToolTimeBudgetExceeded"


def test_run_chat_uses_registered_wordpress_tool_and_never_persists_protocol(tmp_path, monkeypatch):
    from ecom_ops.bot.chat_agent import run_chat

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic")
    responses = iter(
        [
            call("wp_site_status", {"site_id": "mock-shop"}),
            "Mock Shop har okänd hälsa eftersom inga kontroller har körts.",
        ]
    )
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return next(responses), 0.002

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)
    result = run_chat(
        "Visa WordPress-status för mock-shop",
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:42",
        history=[],
        session={},
    )
    assert result.text == "Mock Shop har okänd hälsa eftersom inga kontroller har körts."
    assert "<tool_call>" not in result.text
    assert "secret_ref" not in json.dumps(calls[1][0])
    assert "canonical_url" not in json.dumps(calls[1][0])
    assert result.wp_ops_context["site_id"] == "mock-shop"
    assert result.wp_ops_context["actor_id"] == "jonatan"
    assert result.cost_usd == pytest.approx(0.004)
    assert len(calls) == 2
    assert "ssh_host" not in json.dumps(calls[1][0])


def test_loop_rejects_oversized_result_and_invalid_context_is_not_reused():
    class HugeTools:
        def __init__(self):
            self.received_site_ids = []

        def execute(
            self, name, arguments, *, actor, sticky_site_id=None, channel="", conversation_id=""
        ):
            del name, actor, channel, conversation_id
            self.received_site_ids.append(arguments.get("site_id", sticky_site_id))
            return ToolOutput({"ok": True, "text": "x" * 10_000})

    context = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        site_id="mock-shop",
        operation_ids=("op-1",),
    )
    next_responses = iter(["Done."])
    tools = HugeTools()
    result = run_bounded_wp_tool_loop(
        call("wp_site_status"),
        [],
        complete=lambda _messages: (next(next_responses), 0.0),
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:42",
        context=context,
        tools=tools,
        wp_intent=True,
    )
    assert result.results[0]["error_class"] == "ToolResultTooLarge"
    assert result.site_id is None
    assert tools.received_site_ids == [None]


def test_telegram_can_select_clear_and_actor_switch_drops_site_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    store = ConversationStore(path=tmp_path / "telegram.json")
    handler = BotHandler(store=store)

    chosen = handler.handle(42, "/wp use mock-shop")
    assert "mock-shop" in chosen.text
    assert store.get(42)["session"]["wp_ops_context"]["site_id"] == "mock-shop"

    cleared = handler.handle(42, "/wp clear")
    assert "rensad" in cleared.text.lower()
    after_switch = store.get(42)
    assert after_switch is None or "wp_ops_context" not in after_switch.get("session", {})

    store.set(
        42,
        {
            "session": {
                "wp_ops_context": context_for_storage(
                    actor="jonatan",
                    channel="telegram",
                    conversation_id="telegram:42",
                    site_id="mock-shop",
                    operation_ids=("incident-1",),
                )
            }
        },
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")
    result = handler.handle(42, "/wp sites")
    assert "Registrerade" in result.text
    after_actor_switch = store.get(42)
    assert after_actor_switch is None or "wp_ops_context" not in after_actor_switch.get("session", {})
