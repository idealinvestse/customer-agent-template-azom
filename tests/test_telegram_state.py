"""Telegram bot conversation state tests."""

from __future__ import annotations

import time

import pytest

from ecom_ops.bot.handlers import BotHandler
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
    assert "Azom Ops Bot" in reply
    assert "/order" in reply


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
    assert store.get(42) is None


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
    assert store.get(8) is None


def test_cancel_clears_state(handler, store):
    handler.handle(3, "Support question here please")
    assert store.get(3) is not None
    handler.handle(3, "/cancel")
    assert store.get(3) is None
