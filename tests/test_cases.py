"""Case store, poller, and approve/send tests."""

from __future__ import annotations

import pytest

from ecom_ops.actions.mail import MailService
from ecom_ops.cases.mailboxes import load_mailboxes
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailMessage,
    MailProvider,
)
from ecom_ops.rbac import Permission, resolve_actor


@pytest.fixture
def case_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return CaseStore(path=tmp_path / "cases.db")


@pytest.fixture
def mail_client():
    transport = InMemoryMailTransport()
    # seed already has messages; add one with message_id
    transport.inbox.append(
        MailMessage(
            subject="Var är order 1001?",
            body="Hej, jag väntar på leverans.",
            from_addr="kund@example.com",
            to_addrs=["support@azom.se"],
            message_id="<case-test-1@azom>",
            uid="uid-case-1",
        )
    )
    return MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            username="mock@azom.se",
            from_addr="support@azom.se",
        ),
        transport=transport,
    )


def test_jonatan_has_case_reply():
    actor = resolve_actor("jonatan")
    assert actor.has(Permission.CASE_REPLY)


def test_create_and_dedupe(case_store):
    c1 = case_store.create_case(
        mailbox_id="support_default",
        subject="Test",
        from_addr="a@b.co",
        body="hello",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<dup@x>",
    )
    c2 = case_store.create_case(
        mailbox_id="support_default",
        subject="Test",
        from_addr="a@b.co",
        body="hello",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<dup@x>",
    )
    assert c1.id == c2.id
    assert len(case_store.list_cases(status="open")) == 1


def test_load_mailboxes():
    boxes = load_mailboxes()
    assert any(m.id == "support_default" for m in boxes)


def test_poll_creates_cases(case_store, mail_client, monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    def fake_client_from_env(**kwargs):
        return mail_client

    monkeypatch.setattr(
        "ecom_ops.cases.service.client_from_env", fake_client_from_env
    )
    svc = CaseService(
        store=case_store,
        mail=MailService(client=mail_client),
    )
    result = svc.poll(actor="agent", use_mock=True)
    assert result.ok
    assert result.created >= 1
    opens = case_store.list_cases(status="open")
    assert len(opens) >= 1


def test_approve_and_send(case_store, mail_client, tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="Hjälp",
        from_addr="kund@example.com",
        body="Behöver hjälp",
        category="other",
        draft_reply="Tack för ditt meddelande.",
        order_id=None,
        message_id="<send-test@x>",
    )
    svc = CaseService(store=case_store, mail=MailService(client=mail_client))
    result = svc.approve_and_send(case.id, actor="jonatan")
    assert result.ok, result.message
    updated = case_store.get(case.id)
    assert updated is not None
    assert updated.status == "replied"
    msgs = case_store.messages(case.id)
    assert any(m.direction == "outbound" for m in msgs)


def test_cli_cases_list(capsys, case_store, monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    case_store.create_case(
        mailbox_id="support_default",
        subject="CLI",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="y",
        order_id=None,
        message_id="<cli@x>",
    )
    from ecom_ops.cli import main

    code = main(["--mock", "cases", "list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "CLI" in out
