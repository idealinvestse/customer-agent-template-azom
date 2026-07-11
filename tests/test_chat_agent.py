"""Chat agent tools + Telegram hybrid vNext coverage."""

from __future__ import annotations

import pytest

from ecom_ops.bot.chat_agent import (
    FALLBACK_NO_KEY,
    SOFT_ESCALATE_NUDGE,
    ToolPrefetch,
    gather_tool_results,
    parse_approve_nl,
    run_chat,
    tool_list_cases,
    tool_lookup_order,
    wants_escalate,
    wants_hard_escalate_confirm,
)
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.reply import BotReply
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.store import CaseStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return ConversationStore(path=tmp_path / "telegram_state.json", ttl_seconds=3600)


@pytest.fixture
def handler(store, escalation):
    return BotHandler(store=store, escalation=escalation)


def test_wants_escalate():
    assert wants_escalate("eskalera till Oscar")
    assert not wants_escalate("hur många öppna ärenden?")
    assert wants_hard_escalate_confirm("eskalera detta")
    assert not wants_hard_escalate_confirm(
        "kan du kolla order 1001 och eventuellt eskalera till oscar om det ser fel ut?"
    )


def test_gather_returns_tool_prefetch(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results("Vad är status på order 1001?")
    assert isinstance(pref, ToolPrefetch)
    assert not any(n.startswith("__") for n, _ in pref.results)
    assert pref.results[0][0] == "lookup_order"
    assert "1001" in pref.results[0][1] or "Order" in pref.results[0][1]
    assert pref.digest
    assert "Headset" in pref.results[0][1] or "Rader" in pref.results[0][1]


def test_gather_tools_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    cstore = CaseStore(path=tmp_path / "cases.db")
    cstore.create_case(
        mailbox_id="support_default",
        subject="Var är mitt paket?",
        from_addr="a@b.co",
        body="Hej",
        category="shipping",
        draft_reply="Vi kollar.",
        order_id=None,
        message_id="<m1@x>",
        status="open",
    )
    pref = gather_tool_results("Hur många öppna ärenden har vi?")
    assert any(n == "list_cases" for n, _ in pref.results)
    listed, _ids = tool_list_cases()
    assert "Öppna" in listed or "öppna" in listed.lower() or "ärende" in listed.lower()


def test_suggest_intent_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    cstore = CaseStore(path=tmp_path / "cases.db")
    sug = cstore.create_case(
        mailbox_id="support_default",
        subject="Suggestable",
        from_addr="a@b.co",
        body="Hej",
        category="order_status",
        draft_reply="Ok",
        order_id="1001",
        message_id="<sug@x>",
        status="open",
        suggest_approve=True,
        classify_confidence=0.91,
    )
    cstore.create_case(
        mailbox_id="support_default",
        subject="Other",
        from_addr="b@b.co",
        body="Hej",
        category="return",
        draft_reply="Ok",
        order_id=None,
        message_id="<oth@x>",
        status="open",
        suggest_approve=False,
    )
    pref = gather_tool_results("Vad kan jag godkänna?")
    assert pref.suggest_case_ids == [sug.id[:8]]
    assert "★" in pref.results[0][1] or "91%" in pref.results[0][1]


def test_parse_approve_nl_no_autosend():
    assert parse_approve_nl("godkänn aabbccdd") == "aabbccdd"
    assert parse_approve_nl("approve aabbccdd please") == "aabbccdd"
    assert parse_approve_nl("visa aabbccdd") is None


def test_approve_nl_confirm_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cstore = CaseStore(path=tmp_path / "cases.db")
    case = cstore.create_case(
        mailbox_id="support_default",
        subject="Fråga",
        from_addr="a@b.co",
        body="Hej",
        category="other",
        draft_reply="Tack",
        order_id=None,
        message_id="<ap@x>",
        status="open",
    )
    conv = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=conv)
    reply = bot.handle(1, f"godkänn {case.id[:8]}")
    assert "skickar inte" in reply.lower() or "Godkänn" in reply.text
    assert reply.reply_markup is not None
    assert cstore.get(case.id).status == "open"


def test_run_chat_no_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = run_chat("Hej", history=[], session={})
    assert result.text == FALLBACK_NO_KEY
    assert len(result.messages) == 2
    assert result.cost_usd == 0.0


def test_run_chat_tool_first_without_llm(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    result = run_chat("Vad är status på order 1001?", history=[], session={})
    assert "Order" in result.text or "1001" in result.text
    assert FALLBACK_NO_KEY not in result.text
    assert result.tool_digest


def test_run_chat_budget_skip(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    class FakeTel:
        def within_budget(self, cap):
            return False

        def sum_cost_usd(self):
            return 999.0

        def record(self, **kwargs):
            pass

    result = run_chat("Hej", history=[], session={}, telemetry=FakeTel())
    assert "budget" in result.text.lower() or "OpenRouter" in result.text


def test_run_chat_persists_history(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    class FakeTel:
        def within_budget(self, cap):
            return True

        def record(self, **kwargs):
            pass

    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.chat_completion",
        lambda messages, **kw: ("Svar här", 0.002),
    )
    result = run_chat(
        "Hej",
        history=[
            {"role": "user", "content": "tidigare"},
            {"role": "assistant", "content": "hej"},
        ],
        session={"model": "default"},
        telemetry=FakeTel(),
    )
    assert result.text == "Svar här"
    assert len(result.messages) == 4


def test_nl_order_uses_chat_not_cold_dump(handler, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    reply = handler.handle(99, "Kan du kolla status på order 1001 åt mig?")
    assert "1001" in reply.text or "Order" in reply.text
    assert "Headset" in reply.text or "Rader" in reply.text


def test_soft_escalate_no_sticky_flow(handler, store, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    def fake_chat(messages, **kwargs):
        return "Jag kan eskalera till Oscar om du vill.", 0.0

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)
    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, cap: True
    )
    reply = handler.handle(
        77,
        "Det här ser konstigt ut, kanske värt att eskalera till oscar efteråt",
    )
    # Soft nudge — no sticky escalate_confirm
    state = store.get(77)
    assert state is None or state.get("flow") != "escalate_confirm"
    assert SOFT_ESCALATE_NUDGE in reply.text or "eskalera" in reply.lower()


def test_hard_escalate_still_sticky(handler, store):
    reply = handler.handle(42, "eskalera till oscar")
    assert reply.reply_markup is not None
    assert store.get(42)["flow"] == "escalate_confirm"


def test_cases_list_attaches_triage_buttons(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cstore = CaseStore(path=tmp_path / "cases.db")
    case = cstore.create_case(
        mailbox_id="support_default",
        subject="Suggestable",
        from_addr="a@b.co",
        body="Hej",
        category="order_status",
        draft_reply="Ok",
        order_id="1001",
        message_id="<sug2@x>",
        status="open",
        suggest_approve=True,
        classify_confidence=0.9,
    )
    conv = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=conv)
    reply = bot.handle(1, "Visa öppna ärenden i kön")
    assert case.id[:8] in reply.text or "Öppna" in reply.text
    assert reply.reply_markup is not None
    assert "cases:approve:" in str(reply.reply_markup)


def test_tool_digest_in_context(handler, store, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    handler.handle(5, "status på order 1001")
    ctx = handler.handle(5, "/context")
    assert "tool_digest" in ctx.text
    assert "lookup_order" in ctx.text or "1001" in ctx.text or "Order" in ctx.text


def test_followup_uses_prior_digest(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    seen: dict = {}

    def fake_chat(messages, **kwargs):
        seen["sys"] = " ".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        return "Frakten syns inte i datan.", 0.0

    class FakeTel:
        def within_budget(self, cap):
            return True

        def record(self, **kwargs):
            pass

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)
    run_chat(
        "och frakten då?",
        history=[],
        session={},
        prior_digest="lookup_order: [Order 1001] | Status: processing",
        telemetry=FakeTel(),
    )
    assert "Prior tool digest" in seen.get("sys", "")
    assert "1001" in seen.get("sys", "")


def test_callback_show_and_approve(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    cstore = CaseStore(path=tmp_path / "cases.db")
    case = cstore.create_case(
        mailbox_id="support_default",
        subject="Fråga",
        from_addr="a@b.co",
        body="Hej",
        category="other",
        draft_reply="Tack för ditt meddelande.",
        order_id=None,
        message_id="<m2@x>",
        status="open",
    )
    conv = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=conv)

    shown = dispatch_openclaw_command(1, f"/cases show {case.id[:8]}", conv)
    assert isinstance(shown, BotReply)
    assert shown.reply_markup is not None
    assert cstore.get(case.id).status == "open"

    via_cb = bot.handle_callback(1, f"cases:show:{case.id[:8]}")
    assert case.id[:8] in via_cb.text
    assert cstore.get(case.id).status == "open"

    reply = bot.handle_callback(1, f"cases:approve:{case.id[:8]}")
    assert "Skickat" in reply.text or "Misslyckades" in reply.text or "approve" in reply.lower()


def test_lookup_order_tool_mock(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    out = tool_lookup_order("1001")
    assert "1001" in out or "Order" in out
    assert "Headset" in out or "Rader" in out
