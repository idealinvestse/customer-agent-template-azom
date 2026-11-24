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
    class NoRoutes:
        def any_configured(self):
            return False

    monkeypatch.setattr("ecom_ops.bot.chat_agent.get_model_router", lambda: NoRoutes())
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

    class OpenRouterOnly:
        def any_configured(self):
            return True

        def has_route_outside_openrouter_budget(self):
            return False

    monkeypatch.setattr("ecom_ops.bot.chat_agent.get_model_router", lambda: OpenRouterOnly())

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


def test_mapped_oscar_is_told_to_the_model(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    seen: dict[str, str] = {}

    class Ready:
        def any_configured(self):
            return True

        def has_route_outside_openrouter_budget(self):
            return True

        def preferred_route_id(self):
            return "xai_grok_build"

    class Tel:
        def within_budget(self, cap):
            del cap
            return True

        def sum_cost_usd(self):
            return 0.0

        def record(self, **kwargs):
            del kwargs

    def fake_chat(messages, **kwargs):
        del kwargs
        seen["sys"] = " ".join(
            item.get("content") or "" for item in messages if item.get("role") == "system"
        )
        return "En rad i chatten räcker inte för Oscar-behörighet.", 0.0

    monkeypatch.setattr("ecom_ops.bot.chat_agent.get_model_router", lambda: Ready())
    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)
    result = run_chat("Jag är oscar", actor="oscar", telemetry=Tel())
    assert "Aktör i den här chatten: oscar, roll full_admin." in seen["sys"]
    assert "Du talar med Oscar." in seen["sys"]
    assert result.soft_escalate_nudge is False
    assert SOFT_ESCALATE_NUDGE not in result.text


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


def test_integration_check_lists_orders_without_llm(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def boom(*_args, **_kwargs):
        raise AssertionError("llm")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)
    result = run_chat("Kontrollera integrationen")
    assert "1001" in result.text
    assert "svarade" in result.text
    assert "customer@example.com" not in result.text


def test_market_pointer_lists_orders(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results("Det gäller azom.no")
    assert pref.sticky_market == "no"
    assert pref.results[0][0] == "list_recent_orders"
    assert "1001" in pref.results[0][1]


def test_tools_question_is_the_capability_list(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    result = run_chat("Vilka verktyg har du?")
    assert "Woo-kopplingen" in result.text
    assert "Order lookup failed" not in result.text


def test_short_retry_keeps_market(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results("Kolla nu", sticky_market="no")
    assert pref.sticky_market == "no"
    assert pref.results[0][0] == "list_recent_orders"


def test_woo_failure_hides_response_body(monkeypatch):
    from ecom_ops.bot.chat_agent import tool_list_recent_orders
    from ecom_ops.security import SecurityError

    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def fail(self, **_kwargs):
        raise SecurityError("WooCommerce API error 401: secret-body ck_live")

    monkeypatch.setattr(
        "ecom_ops.integrations.woocommerce.WooCommerceClient.list_orders",
        fail,
    )
    text, shown = tool_list_recent_orders(domain="no", offset=3)
    assert "HTTP 401" in text
    assert "secret-body" not in text
    assert "ck_live" not in text
    assert shown == 3


def test_next_page_skips_shown_orders(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results("nästa 1", listed_offset=1)
    assert pref.order_list_shown == 2
    assert "1002" in pref.results[0][1]
    assert "1001" not in pref.results[0][1]
    assert "<tool_call>" not in pref.results[0][1]


def test_fler_ordrar_continues_without_llm(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def boom(*_args, **_kwargs):
        raise AssertionError("llm")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)
    result = run_chat(
        "kan du plocka fram fler ordrar",
        session={"order_list_shown": 1, "last_market": "no"},
    )
    assert "1002" in result.text
    assert "1001" not in result.text
    assert "<tool_call>" not in result.text
    assert result.order_list_shown == 2


def test_model_tool_call_is_executed(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    from ecom_ops.bot.chat_agent import _fulfill_model_tool_call

    raw = (
        '<tool_call>\n{"name": "list_recent_orders", "arguments": '
        '{"store": "azom.no", "limit": 1, "offset": 1}}\n</tool_call>'
    )
    text, shown = _fulfill_model_tool_call(raw, domain=None, offset=0)
    assert text is not None
    assert "1002" in text
    assert "tool_call" not in text
    assert shown == 2


def test_list_products_skips_llm(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def boom(*_args, **_kwargs):
        raise AssertionError("llm")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)
    result = run_chat("visa produkter")
    assert "501" in result.text
    assert "Azom Pro Headset" in result.text
    assert "lager 12" in result.text
    assert result.pending_action is None
    assert result.sticky_product_id == "501"


def test_list_pages_includes_about(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def boom(*_args, **_kwargs):
        raise AssertionError("llm")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)
    result = run_chat("visa sidor")
    assert "Om oss" in result.text
    assert "2" in result.text
    assert result.sticky_page_id == "2"


def test_product_rename_is_pending_until_confirm(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def boom(*_args, **_kwargs):
        raise AssertionError("write")

    monkeypatch.setattr(
        "ecom_ops.integrations.woocommerce.WooCommerceClient.update_product",
        boom,
    )
    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)
    result = run_chat("ändra produkt 501 namn till Nytt namn")
    assert result.pending_action is not None
    assert result.pending_action.kind == "product_update"
    assert result.pending_action.payload["fields"]["name"] == "Nytt namn"
    assert "Ja" in result.text
    assert "Azom Pro Headset" in result.text
    from ecom_ops.integrations.woocommerce import client_from_env

    fresh = client_from_env(use_mock=True)
    assert fresh.get_product("501")["name"] == "Azom Pro Headset"


def test_product_update_persists_on_shared_client(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    from ecom_ops.bot.catalog import execute_product_update
    from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient

    transport = InMemoryWooTransport()
    client = WooCommerceClient(base_url="https://mock.local", transport=transport)

    def same_client(**_kwargs):
        return client

    monkeypatch.setattr("ecom_ops.integrations.woocommerce.client_from_env", same_client)
    ok, msg = execute_product_update(
        product_id="501",
        fields={"name": "Nytt namn", "sku": "EVIL"},
        actor="oscar",
    )
    assert ok
    assert transport.products["501"]["name"] == "Nytt namn"
    assert "sku" not in transport.products["501"]
    assert "EVIL" not in msg
    ok_stock, _msg = execute_product_update(
        product_id="501",
        fields={"stock_quantity": 3},
        actor="oscar",
    )
    assert ok_stock
    assert transport.products["501"]["stock_quantity"] == 3
    assert transport.products["501"]["manage_stock"] is True


def test_page_rename_is_pending_until_confirm(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def boom(*_args, **_kwargs):
        raise AssertionError("write")

    monkeypatch.setattr(
        "ecom_ops.integrations.wordpress.WordPressClient.update_page",
        boom,
    )
    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)
    result = run_chat("ändra sidan Om oss titel till Om Azom")
    assert result.pending_action is not None
    assert result.pending_action.kind == "page_update"
    assert result.pending_action.payload["page_id"] == "2"
    assert result.pending_action.payload["fields"]["title"] == "Om Azom"
    assert "Ja" in result.text


def test_page_update_persists_on_shared_client(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    from ecom_ops.bot.catalog import execute_page_update
    from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient

    transport = InMemoryWpTransport()
    client = WordPressClient(base_url="https://mock.local", transport=transport)

    def same_client(**_kwargs):
        return client

    monkeypatch.setattr("ecom_ops.integrations.wordpress.wp_client_from_env", same_client)
    ok, msg = execute_page_update(
        page_id="2",
        fields={"title": "Om Azom", "content": "<p>Hej</p>"},
        actor="oscar",
    )
    assert ok
    assert "2" in msg
    assert transport.posts[2]["title"]["rendered"] == "Om Azom"
    assert transport.posts[2]["content"] == "<p>Hej</p>"


def test_invalid_price_does_not_propose(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    result = run_chat("ändra produkt 501 pris till gratis")
    assert result.pending_action is None
    assert "Priset" in result.text


def test_next_products_are_not_orders(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results("nästa 20 produkter", listed_offset=5)
    assert pref.results[0][0] == "list_products"
    assert "1001" not in pref.results[0][1]


def test_sticky_product_rename(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    result = run_chat("ändra namn till Z", session={"last_product_id": "501"})
    assert result.pending_action is not None
    assert result.pending_action.payload["product_id"] == "501"
    assert result.pending_action.payload["fields"]["name"] == "Z"


def test_several_product_matches_do_not_write(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def two(self, **_kwargs):
        return [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]

    monkeypatch.setattr(
        "ecom_ops.integrations.woocommerce.WooCommerceClient.list_products",
        two,
    )
    pref = gather_tool_results("ändra produkt headset namn till X")
    assert pref.pending_action is None
    assert "Flera produkter" in pref.results[0][1]


def test_model_product_write_is_not_executed(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    from ecom_ops.bot.chat_agent import _fulfill_model_tool_call

    raw = (
        '<tool_call>{"name": "update_product", "arguments": '
        '{"product_id": "501", "fields": {"name": "Hack"}}}</tool_call>'
    )
    text, shown = _fulfill_model_tool_call(raw, domain=None, offset=0)
    assert text is not None
    assert "inte gjord" in text
    assert "Hack" not in text
    assert "tool_call" not in text
    assert shown is None


def test_generated_description_stays_a_proposal(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    pref = gather_tool_results("generera produktbeskrivning för produkt 42")
    assert pref.pending_action is not None
    assert pref.pending_action.kind == "product_desc"
