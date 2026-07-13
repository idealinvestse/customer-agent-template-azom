"""Telegram bot conversation state + OpenClaw hybrid chat tests."""

from __future__ import annotations

import time

import pytest

from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.openclaw_commands import TELEGRAM_MENU_COMMANDS, dispatch_openclaw_command
from ecom_ops.bot.reply import BotReply, chunk_text
from ecom_ops.bot.store import ConversationStore, clamp_messages


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return ConversationStore(path=tmp_path / "telegram_state.json", ttl_seconds=3600)


@pytest.fixture
def handler(store, escalation):
    return BotHandler(store=store, escalation=escalation)


def test_store_set_get_clear(store):
    store.set("123", {"flow": "escalate_confirm", "step": "confirm", "slots": {}})
    entry = store.get("123")
    assert entry is not None
    assert entry["flow"] == "escalate_confirm"
    store.clear("123")
    assert store.get("123") is None


def test_store_ttl_expiry(store):
    store.set("99", {"flow": "test", "step": "x", "slots": {}})
    store._data["99"]["updated_at"] = time.time() - 7200
    store._save()
    assert store.get("99") is None


def test_store_clamps_messages(store):
    # OpenClaw-like longer memory: MAX_MESSAGES=40; force overflow
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    store.set("1", {"flow": None, "messages": msgs, "session": {}})
    got = store.get("1")
    assert got is not None
    assert len(got["messages"]) == 40
    assert got["messages"][0]["content"] == "m10"
    assert got["messages"][-1]["content"] == "m49"


def test_clamp_messages_filters():
    assert clamp_messages([{"role": "user", "content": "hi"}, {"role": "x", "content": "no"}]) == [
        {"role": "user", "content": "hi"}
    ]


def test_help_command(handler):
    reply = handler.handle(1, "/help")
    assert "OpenClaw" in reply or "AzomOps" in reply
    assert "/order" in reply
    assert "/commands" in reply
    low = reply.lower()
    assert (
        "hybrid" in low
        or "fråga" in low
        or "fritext" in low
        or "chat" in low
        or "tråd" in low
        or "skriv fritt" in low
    )
    assert "approve" in low or "knapp" in low or "bekräft" in low


def test_commands_catalog(handler):
    reply = handler.handle(1, "/commands")
    assert "/status" in reply
    assert "/whoami" in reply
    assert "/new" in reply
    assert "/tools" in reply
    assert "/tasks" in reply


def test_whoami_and_id_alias(handler):
    assert "chat_id: 9" in handler.handle(9, "/whoami")
    assert "chat_id: 9" in handler.handle(9, "/id")


def test_status_command(handler):
    reply = handler.handle(1, "/status")
    assert "AzomOps status" in reply or "Version:" in reply


def test_model_verbose_think_session(handler, store):
    handler.handle(11, "/model gpt-test")
    handler.handle(11, "/verbose on")
    handler.handle(11, "/think high")
    state = store.get(11)
    assert state is not None
    assert state["session"]["model"] == "gpt-test"
    assert state["session"]["verbose"] == "on"
    assert state["session"]["think"] == "high"
    ctx = handler.handle(11, "/context")
    assert "gpt-test" in ctx or "model" in ctx.lower() or "session" in ctx.lower()
    assert "turns:" in ctx.text


def test_model_copy_mentions_chat(handler):
    reply = handler.handle(1, "/model")
    assert "LLM-chat" in reply.text or "fritext" in reply.lower() or "chat" in reply.lower()
    assert "MVP" not in reply.text


def test_tools_lists_chat_tools(handler):
    reply = handler.handle(1, "/tools")
    assert "lookup_order" in reply
    assert "list_cases" in reply or "show_case" in reply


def test_new_resets_session(handler, store):
    handler.handle(12, "/model keep-me")
    handler.handle(12, "/new")
    assert store.get(12) is None


def test_new_clears_message_history(handler, store, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    handler.handle(13, "Hej bot")
    assert store.get(13) and store.get(13).get("messages")
    handler.handle(13, "/new")
    assert store.get(13) is None


def test_reset_soft_keeps_session_clears_messages(handler, store, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    handler.handle(14, "/model pinned")
    handler.handle(14, "Hej igen")
    assert store.get(14).get("messages")
    handler.handle(14, "/reset soft")
    state = store.get(14)
    assert state is not None
    assert state["session"]["model"] == "pinned"
    assert state.get("messages") == []


def test_unknown_slash(handler):
    reply = handler.handle(1, "/foobar")
    assert "Okänt" in reply or "commands" in reply.lower()


def test_menu_commands_registered():
    names = {c["command"] for c in TELEGRAM_MENU_COMMANDS}
    assert {"help", "status", "order", "cases", "stop"} <= names


def test_chat_fallback_without_api_key(handler, store, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Pure greeting → no-key fallback copy
    reply = handler.handle(42, "Hej botten")
    assert "OPENROUTER" in reply.text or "/order" in reply.text or "/help" in reply.text
    assert "Support-draft" not in reply.text
    # Ops NL still works tool-first without LLM
    ops = handler.handle(43, "Hur mår systemet?")
    assert "Snabbstatus" in ops.text or "Version" in ops.text or "OpenRouter" in ops.text
    state = store.get(42)
    assert state is None or not state.get("flow")


def test_chat_with_mock_llm(handler, store, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fake_chat(messages, **kwargs):
        return "Hej! Jag är AzomOps.", 0.001

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)
    monkeypatch.setattr("ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, cap: True)

    reply = handler.handle(42, "Hej där")
    assert "AzomOps" in reply.text
    state = store.get(42)
    assert state is not None
    assert len(state["messages"]) == 2
    assert state["messages"][0]["role"] == "user"
    assert state["messages"][1]["role"] == "assistant"


def test_chat_uses_session_model(handler, store, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    seen: dict = {}

    def fake_chat(messages, **kwargs):
        seen["model"] = kwargs.get("model")
        return "ok", 0.0

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)
    monkeypatch.setattr("ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, cap: True)
    handler.handle(50, "/model openai/gpt-test-pin")
    handler.handle(50, "Hej")
    assert seen.get("model") == "openai/gpt-test-pin"


def test_escalate_confirm_flow(handler, store):
    reply = handler.handle(42, "Eskalera till Oscar: kund hotar")
    assert "Oscar" in reply.text
    assert reply.reply_markup is not None
    state = store.get(42)
    assert state is not None
    assert state["flow"] == "escalate_confirm"

    confirm = handler.handle(42, "ja")
    assert "Oscar" in confirm.text or "Ticket" in confirm.text
    assert store.get(42) is None or store.get(42).get("flow") is None


def test_escalate_cancel(handler, store):
    handler.handle(7, "eskalera detta")
    reply = handler.handle(7, "nej")
    assert "inte eskalerad" in reply.lower()


def test_escalate_callback(handler, store):
    handler.handle(8, "eskalera till oscar snälla")
    reply = handler.handle_callback(8, "escalate:yes")
    assert "Ticket" in reply.text or "Oscar" in reply.text


def test_order_lookup_command(handler):
    reply = handler.handle(5, "/order 1001")
    assert "Order" in reply or "order" in reply.lower()


def test_order_lookup_flow(handler, store):
    handler.handle(8, "/order")
    assert store.get(8) is not None
    reply = handler.handle(8, "1001")
    assert "1001" in reply or "Order" in reply
    assert store.get(8) is None or store.get(8).get("flow") is None


def test_cancel_clears_flow(handler, store, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    handler.handle(3, "eskalera till oscar")
    assert store.get(3) is not None
    handler.handle(3, "/cancel")
    state = store.get(3)
    assert state is None or state.get("flow") is None


def test_dispatch_direct(store):
    reply = dispatch_openclaw_command(1, "/help", store)
    assert reply is not None
    assert "/commands" in reply


def test_chunk_text():
    short = chunk_text("hi")
    assert short == ["hi"]
    long = "x" * 5000
    parts = chunk_text(long, limit=4000)
    assert len(parts) == 2
    assert "".join(parts) == long


def test_bot_reply_str_compat():
    r = BotReply(text="Hello ★föreslå")
    assert "★" in r
    assert "föreslå" in r.lower()
    assert r.splitlines() == ["Hello ★föreslå"]
