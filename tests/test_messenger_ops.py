"""Messenger adapter, deep links, channel actors."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from ecom_ops.bot.actors import ChannelActorDenied, channel_peer_allowed, resolve_channel_actor
from ecom_ops.bot.dashboard_links import case_detail_url, cases_list_url, dashboard_url
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.messenger_adapter import (
    actions_to_messenger_buttons,
    parse_webhook_payload,
    process_inbound,
    reply_to_messenger_messages,
    verify_signature,
    verify_webhook_challenge,
)
from ecom_ops.bot.reply import ActionButton, ActionMarkup, approve_case_actions
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.store import CaseStore
from ecom_ops.rbac import clear_rbac_cache


def test_dashboard_links(monkeypatch):
    monkeypatch.delenv("AZOM_DASHBOARD_PUBLIC_URL", raising=False)
    assert dashboard_url("/cases") is None
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.com/")
    assert case_detail_url("aabbccdd") == (
        "https://ops.example.com/cases/aabbccdd?from=messenger"
    )
    assert "suggest=1" in (cases_list_url(suggest=True) or "")


def test_verify_challenge(monkeypatch):
    monkeypatch.setenv("MESSENGER_VERIFY_TOKEN", "sekret")
    assert (
        verify_webhook_challenge(
            mode="subscribe", token="sekret", challenge="12345"
        )
        == "12345"
    )
    assert (
        verify_webhook_challenge(mode="subscribe", token="wrong", challenge="1")
        is None
    )


def test_verify_signature(monkeypatch):
    monkeypatch.setenv("MESSENGER_APP_SECRET", "appsekret")
    body = b'{"object":"page"}'
    dig = hmac.new(b"appsekret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, f"sha256={dig}")
    assert not verify_signature(body, "sha256=deadbeef")
    assert not verify_signature(body, None)


def test_parse_message_and_postback():
    payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "message": {"mid": "m1", "text": "/help"},
                    },
                    {
                        "sender": {"id": "PSID1"},
                        "postback": {"payload": "cases:list", "title": "Lista"},
                    },
                ]
            }
        ],
    }
    events = parse_webhook_payload(payload)
    assert len(events) == 2
    assert events[0].text == "/help"
    assert events[1].postback == "cases:list"


def test_messenger_buttons_from_actions(monkeypatch):
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.com")
    actions = approve_case_actions("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    buttons = actions_to_messenger_buttons(actions)
    assert any(b.get("type") == "postback" for b in buttons)
    assert any(b.get("type") == "web_url" for b in buttons)
    url_btn = next(b for b in buttons if b.get("type") == "web_url")
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in url_btn["url"]
    msgs = reply_to_messenger_messages(
        __import__("ecom_ops.bot.reply", fromlist=["BotReply"]).BotReply(
            text="Case aabbccdd", actions=actions
        )
    )
    assert msgs and "attachment" in msgs[0]


def test_messenger_url_survives_three_button_cap(monkeypatch):
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.com")
    from ecom_ops.bot.recovery import approve_success_reply

    reply = approve_success_reply(
        "aaaaaaaa-1111-1111-1111-111111111111",
        next_case_id="bbbbbbbb-2222-2222-2222-222222222222",
    )
    buttons = actions_to_messenger_buttons(reply.actions)
    assert len(buttons) <= 3
    assert any(b.get("type") == "web_url" for b in buttons)
    assert any(
        b.get("type") == "postback" and "approve" in str(b.get("payload"))
        for b in buttons
    )


def test_reply_markup_only_still_renders_messenger_buttons():
    from ecom_ops.bot.reply import BotReply, yes_no_keyboard

    reply = BotReply(
        text="Bekräfta?",
        reply_markup=yes_no_keyboard(yes_data="action:yes", no_data="action:no"),
    )
    msgs = reply_to_messenger_messages(reply)
    assert any("attachment" in m for m in msgs)
    buttons = msgs[-1]["attachment"]["payload"]["buttons"]
    assert any(b.get("payload") == "action:yes" for b in buttons)


def test_case_detail_url_uses_full_id(monkeypatch):
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.com")
    full = "abcdef01-2345-6789-abcd-ef0123456789"
    url = case_detail_url(full)
    assert url and full in url
    assert "from=messenger" in url


def test_channel_actor_messenger(monkeypatch):
    monkeypatch.setenv("MESSENGER_ACTOR_MAP", "99:jonatan")
    assert resolve_channel_actor("messenger", "99") == "jonatan"
    with pytest.raises(ChannelActorDenied):
        resolve_channel_actor("messenger", "1")
    monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", "99")
    assert channel_peer_allowed("messenger", "99")
    assert not channel_peer_allowed("messenger", "1")


def test_messenger_handler_approve_postback(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.com")
    monkeypatch.delenv("MESSENGER_ACTOR_MAP", raising=False)
    monkeypatch.delenv("MESSENGER_ALLOWED_PSIDS", raising=False)
    clear_rbac_cache()
    cstore = CaseStore(path=tmp_path / "cases.db")
    case = cstore.create_case(
        mailbox_id="support_default",
        subject="Fråga",
        from_addr="a@b.co",
        body="Hej",
        category="order_status",
        draft_reply="Tack, vi kollar.",
        order_id="1001",
        message_id="<ms@x>",
        status="open",
    )
    store = ConversationStore(path=tmp_path / "messenger_state.json")
    bot = BotHandler(store=store, channel="messenger")
    from ecom_ops.bot.messenger_adapter import InboundEvent

    reply = process_inbound(
        InboundEvent(peer_id="psid-jonatan", postback=f"cases:approve:{case.id[:8]}"),
        bot,
    )
    assert "Skickat" in reply.text
    assert cstore.get(case.id).status == "replied"


def test_messenger_deny_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", "only-me")
    store = ConversationStore(path=tmp_path / "m.json")
    bot = BotHandler(store=store, channel="messenger")
    reply = bot.handle("other", "/help")
    assert "behörig" in reply.lower() or "MESSENGER_ALLOWED" in reply.text
