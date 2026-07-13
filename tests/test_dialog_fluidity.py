"""OpenClaw-like dialog: sticky context, write confirm rails, follow-ups."""

from __future__ import annotations

from ecom_ops.bot.chat_agent import gather_tool_results, run_chat
from ecom_ops.bot.dialog_actions import (
    parse_order_status_intent,
    parse_product_desc_intent,
    parse_regenerate_nl,
    wants_order_followup,
)
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.store import CaseStore
from ecom_ops.rbac import clear_rbac_cache


def test_parse_order_status_intent_sv():
    got = parse_order_status_intent("sätt order 1001 till completed")
    assert got == {"order_id": "1001", "status": "completed"}
    got2 = parse_order_status_intent("markera order 1001 som klar")
    assert got2 and got2["status"] == "completed"
    got3 = parse_order_status_intent(
        "sätt den till processing", fallback_order_id="1001"
    )
    assert got3 == {"order_id": "1001", "status": "processing"}


def test_parse_product_and_regen():
    p = parse_product_desc_intent("generera produktbeskrivning för produkt 42")
    assert p and p["product_id"] == "42"
    assert parse_regenerate_nl("regenerera aabbccdd") == "aabbccdd"


def test_sticky_order_followup_prefetch(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results(
        "och frakten då?",
        sticky_order_id="1001",
    )
    assert any(n == "lookup_order" for n, _ in pref.results)
    assert "1001" in pref.results[0][1] or "Order" in pref.results[0][1]
    assert wants_order_followup("och frakten då?")


def test_order_status_proposes_not_executes(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=store)
    reply = bot.handle(1, "sätt order 1001 till completed")
    assert reply.reply_markup is not None
    assert "order:set:1001:completed" in str(reply.reply_markup)
    assert "FÖRESLÅ" in reply.text or "bekräft" in reply.lower() or "sätt" in reply.lower()
    # Still in confirm flow — not executed
    state = store.get(1)
    assert state and state.get("flow") == "pending_action"


def test_order_status_confirm_executes_with_agent_actor(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "9:agent")
    clear_rbac_cache()
    store = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=store)
    bot.handle(9, "sätt order 1001 till completed")
    reply = bot.handle_callback(9, "order:set:1001:completed")
    assert "Klart" in reply.text or "already" in reply.lower() or "->" in reply.text
    # Flow cleared
    state = store.get(9)
    assert state is None or state.get("flow") is None


def test_order_status_denied_for_jonatan(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    clear_rbac_cache()
    store = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=store)
    bot.handle(1, "sätt order 1001 till completed")
    reply = bot.handle_callback(1, "order:set:1001:completed")
    assert "Misslyckades" in reply.text or "permission" in reply.lower() or "lacks" in reply.lower()


def test_multi_turn_sticky_order_in_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=store)
    bot.handle(3, "status på order 1001")
    st = store.get(3)
    assert st and (st.get("session") or {}).get("last_order_id") == "1001"
    # Follow-up without repeating order id
    reply = bot.handle(3, "och frakten då?")
    assert "1001" in reply.text or "Order" in reply.text or "frakt" in reply.lower()


def test_case_regenerate_nl_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cstore = CaseStore(path=tmp_path / "cases.db")
    case = cstore.create_case(
        mailbox_id="support_default",
        subject="Hej",
        from_addr="a@b.co",
        body="Var är order 1001?",
        category="order_status",
        draft_reply="GAMMAL",
        order_id="1001",
        message_id="<fluid@x>",
        status="open",
    )
    store = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=store)
    reply = bot.handle(1, f"regenerera {case.id[:8]}")
    assert reply.reply_markup is not None
    # Confirm regen
    conf = bot.handle_callback(1, f"cases:regen:{case.id[:8]}")
    assert "Klart" in conf.text or "regenerat" in conf.lower() or "Draft" in conf.text
    updated = cstore.get(case.id)
    assert updated and updated.draft_reply != "GAMMAL"


def test_run_chat_sticky_args(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = run_chat(
        "statusen då?",
        history=[],
        session={"last_order_id": "1001"},
        sticky_order_id="1001",
    )
    assert result.sticky_order_id == "1001" or "1001" in result.text
