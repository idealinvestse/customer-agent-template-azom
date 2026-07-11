"""Cases 2.0: mark_read, threading, escalate, order enrich, draft/close, telegram."""

from __future__ import annotations

import pytest

from ecom_ops.actions.mail import MailService
from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.service import CaseService, _enrich_draft_with_order
from ecom_ops.cases.store import CaseStore
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailMessage,
    MailProvider,
)
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor


@pytest.fixture
def case_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return CaseStore(path=tmp_path / "cases.db")


@pytest.fixture
def mail_client():
    transport = InMemoryMailTransport()
    return MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            username="mock@azom.se",
            from_addr="support@azom.se",
        ),
        transport=transport,
    )


def _client_with(messages: list[MailMessage]) -> tuple[MailClient, InMemoryMailTransport]:
    transport = InMemoryMailTransport()
    transport.inbox = list(messages)
    client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            username="mock@azom.se",
            from_addr="support@azom.se",
        ),
        transport=transport,
    )
    return client, transport


def test_mark_read_after_poll(case_store, monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    client, transport = _client_with(
        [
            MailMessage(
                subject="Hej",
                body="Fråga om leverans",
                from_addr="a@b.co",
                message_id="<mr-1@x>",
                uid="uid-mr-1",
            )
        ]
    )
    monkeypatch.setattr(
        "ecom_ops.cases.service.client_from_env", lambda **kw: client
    )
    svc = CaseService(store=case_store, mail=MailService(client=client))
    result = svc.poll(actor="agent", use_mock=True)
    assert result.ok
    assert result.created >= 1
    assert any(c[0] == "mark_read" for c in transport.calls)
    assert transport.inbox[0].is_read is True


def test_threading_in_reply_to(case_store, monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    root = case_store.create_case(
        mailbox_id="support_default",
        subject="Orderfråga",
        from_addr="kund@example.com",
        body="Första",
        category="order_status",
        draft_reply="Hej",
        order_id=None,
        message_id="<root-thread@x>",
    )
    client, _ = _client_with(
        [
            MailMessage(
                subject="Re: Orderfråga",
                body="Uppföljning",
                from_addr="kund@example.com",
                message_id="<follow@x>",
                uid="uid-follow",
                in_reply_to="<root-thread@x>",
            )
        ]
    )
    monkeypatch.setattr(
        "ecom_ops.cases.service.client_from_env", lambda **kw: client
    )
    svc = CaseService(store=case_store, mail=MailService(client=client))
    result = svc.poll(actor="agent", use_mock=True)
    assert result.ok
    assert result.created == 1
    msgs = case_store.messages(root.id)
    assert len([m for m in msgs if m.direction == "inbound"]) == 2
    assert len(case_store.list_cases(status="open")) == 1


def test_abuse_sets_escalated(case_store, monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    client, _ = _client_with(
        [
            MailMessage(
                subject="GDPR chargeback legal",
                body="I will sue and file a chargeback GDPR complaint",
                from_addr="angry@example.com",
                message_id="<abuse@x>",
                uid="uid-abuse",
            )
        ]
    )
    monkeypatch.setattr(
        "ecom_ops.cases.service.client_from_env", lambda **kw: client
    )
    svc = CaseService(store=case_store, mail=MailService(client=client))
    result = svc.poll(actor="agent", use_mock=True)
    assert result.ok
    assert result.created >= 1
    cases = case_store.list_cases(status="escalated")
    assert len(cases) >= 1
    assert cases[0].escalation_id
    assert cases[0].priority == "high"


def test_order_enrich_draft(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    draft = _enrich_draft_with_order("Hej kund", "1001", use_mock=True)
    assert "Order 1001" in draft or "1001" in draft
    assert "Hej kund" in draft


def test_save_draft_and_close(case_store, tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="Draft test",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="old",
        order_id=None,
        message_id="<draft@x>",
    )
    svc = CaseService(store=case_store)
    saved = svc.save_draft(case.id, "new draft body", actor="jonatan")
    assert saved.ok
    assert case_store.get(case.id).draft_reply == "new draft body"
    closed = svc.close(case.id, actor="jonatan", reason="done")
    assert closed.ok
    assert case_store.get(case.id).status == "closed"


def test_close_requires_case_reply(case_store, tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    # read_only without CASE_REPLY? viewer has it. Use a fake actor via monkeypatch.
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="RBAC",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="y",
        order_id=None,
        message_id="<rbac@x>",
    )
    svc = CaseService(store=case_store)

    def deny(*args, **kwargs):
        raise AccessDenied("nope")

    monkeypatch.setattr("ecom_ops.cases.service.require_permission", deny)
    result = svc.close(case.id, actor="jonatan")
    assert not result.ok
    assert result.escalated


def test_cli_close_and_draft(capsys, case_store, monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="CLI2",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="y",
        order_id=None,
        message_id="<cli2@x>",
    )
    from ecom_ops.cli import main

    assert main(["--mock", "--actor", "jonatan", "cases", "draft", "--id", case.id, "--body", "sparad"]) == 0
    assert case_store.get(case.id).draft_reply == "sparad"
    assert main(["--mock", "--actor", "jonatan", "cases", "close", "--id", case.id]) == 0
    assert case_store.get(case.id).status == "closed"


def test_telegram_cases_approve_close(case_store, monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="TG case",
        from_addr="kund@example.com",
        body="help",
        category="other",
        draft_reply="Tack för ditt meddelande.",
        order_id=None,
        message_id="<tg@x>",
    )
    store = ConversationStore(path=tmp_path / "tg.json")
    listed = dispatch_openclaw_command(1, "/cases", store)
    assert listed and case.id[:8] in listed

    shown = dispatch_openclaw_command(1, f"/cases show {case.id[:8]}", store)
    assert shown and "TG case" in shown

    client, _ = _client_with([])
    real = CaseService(store=case_store, mail=MailService(client=client))
    monkeypatch.setattr(
        "ecom_ops.cases.service.CaseService",
        lambda *a, **k: real,
    )

    approved = dispatch_openclaw_command(1, f"/cases approve {case.id[:8]}", store)
    assert approved and ("Skickat" in approved or "replied" in approved.lower())
    assert case_store.get(case.id).status == "replied"

    case2 = case_store.create_case(
        mailbox_id="support_default",
        subject="TG close",
        from_addr="kund@example.com",
        body="x",
        category="other",
        draft_reply="y",
        order_id=None,
        message_id="<tg2@x>",
    )
    closed = dispatch_openclaw_command(1, f"/cases close {case2.id[:8]}", store)
    assert closed and "Stängt" in closed
    assert case_store.get(case2.id).status == "closed"


def test_jonatan_still_has_case_reply():
    actor = resolve_actor("jonatan")
    assert actor.has(Permission.CASE_REPLY)
    require_permission(actor, Permission.CASE_REPLY)
