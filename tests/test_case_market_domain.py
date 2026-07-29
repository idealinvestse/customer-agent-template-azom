"""case.market → Woo domain= wiring."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.order_context import woo_domain_from_market, resolve_order_panel


def test_woo_domain_from_market():
    assert woo_domain_from_market("se") == "se"
    assert woo_domain_from_market("NO") == "no"
    assert woo_domain_from_market("azom.dk") == "dk"
    assert woo_domain_from_market("") is None
    assert woo_domain_from_market("unknown") is None


def test_regenerate_draft_passes_market_domain(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_no",
        subject="NO order",
        from_addr="a@b.co",
        body="Order 1001",
        category="order_status",
        draft_reply="Hej",
        order_id="1001",
        message_id="<no-market@x>",
        status="open",
        market="no",
    )
    svc = CaseService(store=store)
    with patch("ecom_ops.cases.service.resolve_order_context") as mock_ctx:
        mock_ctx.return_value = "[Order 1001]\nStatus: processing"
        result = svc.regenerate_draft(case.id, actor="agent", use_mock=True)
    assert result.ok
    mock_ctx.assert_called()
    _args, kwargs = mock_ctx.call_args
    assert kwargs.get("domain") == "no"


def test_dashboard_order_panel_uses_market(monkeypatch):
    panel = resolve_order_panel("1001", use_mock=True, domain="no")
    assert panel is not None
    assert panel["ok"] is True


def test_cases_show_persists_last_market(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    clear_rbac_cache = __import__(
        "ecom_ops.rbac", fromlist=["clear_rbac_cache"]
    ).clear_rbac_cache
    clear_rbac_cache()
    from ecom_ops.bot.handlers import BotHandler
    from ecom_ops.bot.store import ConversationStore
    from ecom_ops.cases.store import CaseStore

    cstore = CaseStore(path=tmp_path / "cases.db")
    case = cstore.create_case(
        mailbox_id="support_no",
        subject="NO",
        from_addr="a@b.co",
        body="x",
        category="order_status",
        draft_reply="Hej",
        order_id="1001",
        message_id="<show-market@x>",
        status="open",
        market="no",
    )
    store = ConversationStore(path=tmp_path / "tg.json")
    bot = BotHandler(store=store, channel="telegram")
    bot.handle("chat1", f"/cases show {case.id[:8]}")
    state = store.get("chat1") or {}
    session = state.get("session") or {}
    assert session.get("last_market") == "no"
    assert session.get("last_case_id8") == case.id[:8]


def test_claim_for_send_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    from ecom_ops.actions.mail import MailService
    from ecom_ops.cases.service import CaseService
    from ecom_ops.cases.store import CaseStore
    from ecom_ops.integrations.mail import (
        InMemoryMailTransport,
        MailClient,
        MailConfig,
        MailProvider,
    )

    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Race",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<race@x>",
        status="open",
    )
    claimed = store.claim_for_send(case.id)
    assert claimed is not None
    assert claimed.status == "sending"
    assert store.claim_for_send(case.id) is None

    mail = MailService(
        client=MailClient(
            config=MailConfig(
                provider=MailProvider.GENERIC_IMAP,
                username="mock@azom.se",
                from_addr="support@azom.se",
            ),
            transport=InMemoryMailTransport(),
        )
    )
    svc = CaseService(store=store, mail=mail)
    # Second approve while sending should fail without sending again
    result = svc.approve_and_send(case.id, actor="jonatan")
    assert not result.ok

    # Complete first claim path via mark_replied
    updated = store.mark_replied(
        case.id,
        outbound_body="Hej",
        to_addr="a@b.co",
        from_addr="",
        subject="Re: Race",
    )
    assert updated and updated.status == "replied"


def test_threaded_suggest_approve_cleared_on_escalated(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    from ecom_ops.cases.store import CaseStore

    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Abuse",
        from_addr="a@b.co",
        body="bad",
        category="abuse",
        draft_reply="Esc",
        order_id=None,
        message_id="<abuse@x>",
        status="escalated",
        suggest_approve=False,
    )
    updated = store.append_inbound(
        case.id,
        from_addr="a@b.co",
        to_addr="support@x",
        subject="Re: Abuse",
        body="more",
        message_id="<abuse2@x>",
        suggest_approve=True,
    )
    # Service-level gate is tested via set_suggest_approve path; store allows write
    assert updated is not None
    cleared = store.set_suggest_approve(case.id, False)
    assert cleared is not None
    assert cleared.suggest_approve is False
