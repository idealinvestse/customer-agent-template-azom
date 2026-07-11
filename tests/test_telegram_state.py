"""Telegram bot conversation state + OpenClaw command tests."""

from __future__ import annotations

import time

import pytest

from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.openclaw_commands import TELEGRAM_MENU_COMMANDS, dispatch_openclaw_command
from ecom_ops.bot.store import ConversationStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return ConversationStore(path=tmp_path / "telegram_state.json", ttl_seconds=3600)


@pytest.fixture
def handler(store, escalation):
    return BotHandler(store=store, escalation=escalation)


def test_store_set_get_clear(store):
    store.set("123", {"flow": "support_draft", "step": "confirm", "slots": {}})
    entry = store.get("123")
    assert entry is not None
    assert entry["flow"] == "support_draft"
    store.clear("123")
    assert store.get("123") is None


def test_store_ttl_expiry(store):
    store.set("99", {"flow": "test", "step": "x", "slots": {}})
    store._data["99"]["updated_at"] = time.time() - 7200
    store._save()
    assert store.get("99") is None


def test_help_command(handler):
    reply = handler.handle(1, "/help")
    assert "OpenClaw" in reply or "AzomOps" in reply
    assert "/order" in reply
    assert "/commands" in reply


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


def test_new_resets_session(handler, store):
    handler.handle(12, "/model keep-me")
    handler.handle(12, "/new")
    assert store.get(12) is None


def test_unknown_slash(handler):
    reply = handler.handle(1, "/foobar")
    assert "Okänt" in reply or "commands" in reply.lower()


def test_menu_commands_registered():
    names = {c["command"] for c in TELEGRAM_MENU_COMMANDS}
    assert {"help", "status", "order", "cases", "stop"} <= names


def test_support_draft_flow(handler, store):
    reply = handler.handle(42, "Hej, jag behöver hjälp med en retur")
    assert "Support-draft" in reply
    assert "ja/nej" in reply.lower() or "Skicka till Oscar" in reply

    state = store.get(42)
    assert state is not None
    assert state["flow"] == "support_draft"
    assert state["step"] == "confirm"

    confirm = handler.handle(42, "ja")
    assert "Oscar" in confirm or "Ticket" in confirm
    assert store.get(42) is None or store.get(42).get("flow") is None


def test_support_draft_cancel(handler, store):
    handler.handle(7, "Hej, jag behöver hjälp")
    reply = handler.handle(7, "nej")
    assert "inte eskalerad" in reply.lower() or "avbruten" in reply.lower()


def test_order_lookup_command(handler):
    reply = handler.handle(5, "/order 1001")
    assert "Order" in reply or "order" in reply.lower()


def test_order_lookup_flow(handler, store):
    handler.handle(8, "/order")
    assert store.get(8) is not None
    reply = handler.handle(8, "1001")
    assert "1001" in reply or "Order" in reply
    assert store.get(8) is None or store.get(8).get("flow") is None


def test_cancel_clears_state(handler, store):
    handler.handle(3, "Support question here please")
    assert store.get(3) is not None
    handler.handle(3, "/cancel")
    state = store.get(3)
    assert state is None or state.get("flow") is None


def test_dispatch_direct(store):
    reply = dispatch_openclaw_command(1, "/help", store)
    assert reply is not None
    assert "/commands" in reply
