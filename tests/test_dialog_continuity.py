"""Dialog continuity: recovery footers, no-LLM digest, sticky tighten, approve next."""

from __future__ import annotations

from ecom_ops.bot.chat_agent import FALLBACK_NO_KEY, gather_tool_results, run_chat, tool_list_cases
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.recovery import (
    FOOTER_EMPTY_QUEUE,
    continuity_fallback,
    empty_queue_text,
)
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.store import CaseStore
from ecom_ops.rbac import clear_rbac_cache


def test_empty_queue_has_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    text, ids = tool_list_cases()
    assert ids == []
    assert "Inga öppna" in text
    assert "poll" in text.lower() or FOOTER_EMPTY_QUEUE.split(":")[0] in text
    assert "Nästa steg" in text


def test_cases_list_empty_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    store = ConversationStore(path=tmp_path / "tg.json")
    reply = dispatch_openclaw_command(1, "/cases list", store)
    assert reply is not None
    text = reply if isinstance(reply, str) else reply.text
    assert "Nästa steg" in text
    assert "poll" in text.lower() or "dashboard" in text.lower()


def test_no_llm_uses_prior_digest(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    result = run_chat(
        "tack",
        history=[],
        session={},
        prior_digest="lookup_order: Order 1001 | processing",
        sticky_order_id="1001",
    )
    assert FALLBACK_NO_KEY not in result.text
    assert "1001" in result.text or "lookup_order" in result.text
    assert "sparad kontext" in result.text.lower() or "digest" in result.text.lower()


def test_no_llm_uses_sticky_only(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = run_chat(
        "ok",
        history=[],
        session={"last_order_id": "1001", "last_case_id8": "aabbccdd"},
        sticky_order_id="1001",
        sticky_case_id8="aabbccdd",
    )
    assert FALLBACK_NO_KEY not in result.text
    assert "1001" in result.text
    assert "aabbccdd" in result.text


def test_continuity_fallback_none_without_context():
    assert continuity_fallback() is None
    assert continuity_fallback(prior_digest="  ") is None


def test_sticky_short_chitchat_does_not_prefetch_order(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results("hej", sticky_order_id="1001")
    assert not any(n == "lookup_order" for n, _ in pref.results)


def test_sticky_order_followup_still_works(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results("och frakten då?", sticky_order_id="1001")
    assert any(n == "lookup_order" for n, _ in pref.results)


def test_approve_fail_keyboard(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    clear_rbac_cache()
    cstore = CaseStore(path=tmp_path / "cases.db")
    case = cstore.create_case(
        mailbox_id="support_default",
        subject="Tom draft",
        from_addr="a@b.co",
        body="Hej",
        category="other",
        draft_reply="",
        order_id=None,
        message_id="<nodraft@x>",
        status="open",
    )
    store = ConversationStore(path=tmp_path / "tg.json")
    reply = dispatch_openclaw_command(1, f"/cases approve {case.id[:8]}", store)
    assert reply is not None
    text = reply.text if hasattr(reply, "text") else str(reply)
    assert "Misslyckades" in text
    assert "Nästa steg" in text
    assert hasattr(reply, "reply_markup") and reply.reply_markup is not None
    markup = str(reply.reply_markup)
    assert f"cases:regen:{case.id[:8]}" in markup
    assert f"cases:show:{case.id[:8]}" in markup
    assert "cases:list" in markup


def test_approve_success_next_keyboard(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    clear_rbac_cache()
    cstore = CaseStore(path=tmp_path / "cases.db")
    first = cstore.create_case(
        mailbox_id="support_default",
        subject="Första",
        from_addr="a@b.co",
        body="Hej",
        category="order_status",
        draft_reply="Tack, vi kollar.",
        order_id="1001",
        message_id="<a1@x>",
        status="open",
        suggest_approve=True,
        classify_confidence=0.9,
    )
    second = cstore.create_case(
        mailbox_id="support_default",
        subject="Andra",
        from_addr="b@b.co",
        body="Hej",
        category="shipping",
        draft_reply="På väg.",
        order_id="1002",
        message_id="<a2@x>",
        status="open",
    )
    store = ConversationStore(path=tmp_path / "tg.json")
    reply = dispatch_openclaw_command(1, f"/cases approve {first.id[:8]}", store)
    assert reply is not None
    text = reply.text if hasattr(reply, "text") else str(reply)
    assert "Skickat" in text
    assert "Nästa i kö" in text or second.id[:8] in text
    assert hasattr(reply, "reply_markup") and reply.reply_markup is not None
    markup = str(reply.reply_markup)
    assert f"cases:approve:{second.id[:8]}" in markup or f"cases:show:{second.id[:8]}" in markup
    assert cstore.get(first.id).status == "replied"


def test_callback_deny_parity(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "999")
    store = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=store)
    reply = bot.handle_callback(1, "cases:list")
    assert "behörig" in reply.lower() or "TELEGRAM_ALLOWED" in reply.text
    assert "Nästa steg" in reply.text or "Oscar" in reply.text


def test_context_shows_sticky(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = ConversationStore(path=tmp_path / "tg.json")
    store.set(
        1,
        {
            "flow": None,
            "step": None,
            "slots": {},
            "messages": [],
            "tool_digest": "lookup_order: 1001",
            "session": {"last_order_id": "1001", "last_case_id8": "deadbeef"},
        },
    )
    reply = dispatch_openclaw_command(1, "/context", store)
    text = reply if isinstance(reply, str) else reply.text
    assert "last_order_id: 1001" in text
    assert "last_case_id8: deadbeef" in text


def test_empty_queue_text_suggest():
    text = empty_queue_text(suggest_only=True)
    assert "★" in text
    assert "Nästa steg" in text
