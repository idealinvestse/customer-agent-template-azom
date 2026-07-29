"""FU1: regenerate case draft without sending mail."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailProvider,
)
from ecom_ops.telemetry import Telemetry


def _svc(tmp_path, telemetry=None, escalation=None):
    store = CaseStore(path=tmp_path / "cases.db")
    client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            from_addr="support@azom.se",
            username="support@azom.se",
        ),
        transport=InMemoryMailTransport(),
    )
    return CaseService(
        store=store,
        mail_client=client,
        telemetry=telemetry or Telemetry(path=tmp_path / "tel.jsonl"),
        escalation=escalation,
    )


def test_regenerate_updates_draft_for_jonatan(tmp_path, monkeypatch, telemetry, escalation):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = _svc(tmp_path, telemetry=telemetry, escalation=escalation)
    case = svc.store.create_case(
        mailbox_id="support_default",
        subject="Var är order 1001?",
        from_addr="anna@example.com",
        body="Hej, var är min order 1001?",
        category="order_status",
        draft_reply="GAMMAL DRAFT SOM SKA BYTAS",
        order_id="1001",
        message_id="<regen-test-1@azom>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )
    result = svc.regenerate_draft(case.id, actor="jonatan", use_mock=True)
    assert result.ok, result.message
    assert result.case is not None
    new_draft = result.case.get("draft_reply") or ""
    assert new_draft
    assert "GAMMAL DRAFT SOM SKA BYTAS" not in new_draft
    assert case.id[:8] in result.message or "regenerat" in result.message.lower()
    # No outbound send
    assert result.case.get("status") in {"open", "escalated"}
    events = telemetry.path.read_text(encoding="utf-8")
    assert "case_draft_regenerated" in events


def test_regenerate_denied_for_unknown_viewer_like_actor(
    tmp_path, monkeypatch, telemetry, escalation
):
    """Agent without CASE_REPLY shouldn't appear — viewer has CASE_REPLY.
    Force a role that lacks CASE_REPLY via custom denial by using empty name path
    is hard; instead monkeypatch require to simulate denial is unnecessary —
    use actor that resolves to viewer is allowed. Test unknown actor escalates deny.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = _svc(tmp_path, telemetry=telemetry, escalation=escalation)
    case = svc.store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order 1001 status?",
        category="order_status",
        draft_reply="old",
        order_id="1001",
        message_id="<regen-test-deny@azom>",
        site="azom",
    )
    result = svc.regenerate_draft(case.id, actor="stranger")
    assert not result.ok
    assert result.escalated or "Unknown" in result.message or "denied" in result.message.lower()


def test_regenerate_active_status_only(tmp_path, telemetry, escalation, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = _svc(tmp_path, telemetry=telemetry, escalation=escalation)
    case = svc.store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order 1001",
        category="order_status",
        draft_reply="old",
        order_id="1001",
        message_id="<regen-test-replied@azom>",
        site="azom",
        status="replied",
    )
    # create_case may force open — set replied explicitly
    svc.store.set_status(case.id, "replied")
    result = svc.regenerate_draft(case.id, actor="jonatan", use_mock=True)
    assert not result.ok
    assert "replied" in result.message or "expected" in result.message.lower()


def test_regenerate_not_found(tmp_path, telemetry, escalation):
    svc = _svc(tmp_path, telemetry=telemetry, escalation=escalation)
    result = svc.regenerate_draft("deadbeef-0000-0000-0000-000000000000", actor="jonatan")
    assert not result.ok
    assert "not found" in result.message.lower()


def test_regenerate_keeps_abuse_escalated(tmp_path, telemetry, escalation, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = _svc(tmp_path, telemetry=telemetry, escalation=escalation)
    case = svc.store.create_case(
        mailbox_id="support_default",
        subject="Chargeback threat legal",
        from_addr="angry@example.com",
        body="This is a chargeback and legal threat. Abuse. Order 55",
        category="abuse",
        draft_reply="old holding",
        order_id="55",
        message_id="<regen-test-abuse@azom>",
        site="azom",
        status="escalated",
        priority="high",
        escalation_id="ticket-abc",
    )
    result = svc.regenerate_draft(case.id, actor="jonatan", use_mock=True)
    assert result.ok, result.message
    assert result.case is not None
    assert result.case.get("status") == "escalated"
    assert result.case.get("escalation_id") == "ticket-abc"
    # Draft may change template but still no silent send
    assert result.case.get("status") != "replied"


def test_regenerate_stores_draft_before_regen(
    tmp_path, monkeypatch, telemetry, escalation
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = _svc(tmp_path, telemetry=telemetry, escalation=escalation)
    case = svc.store.create_case(
        mailbox_id="support_default",
        subject="Var är order 1001?",
        from_addr="anna@example.com",
        body="Hej, var är min order 1001?",
        category="order_status",
        draft_reply="GAMMAL DRAFT",
        order_id="1001",
        message_id="<regen-test-before@azom>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )
    result = svc.regenerate_draft(case.id, actor="jonatan", use_mock=True)
    assert result.ok, result.message
    refreshed = svc.store.get(case.id)
    assert refreshed is not None
    assert refreshed.draft_before_regen == "GAMMAL DRAFT"
    assert refreshed.draft_reply != "GAMMAL DRAFT"
    assert refreshed.draft_regenerated_at


def test_regenerate_denied_within_cooldown(
    tmp_path, monkeypatch, telemetry, escalation
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = _svc(tmp_path, telemetry=telemetry, escalation=escalation)
    case = svc.store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order 1001 status?",
        category="order_status",
        draft_reply="old",
        order_id="1001",
        message_id="<regen-test-cooldown@azom>",
        site="azom",
    )
    first = svc.regenerate_draft(case.id, actor="jonatan", use_mock=True)
    assert first.ok, first.message
    second = svc.regenerate_draft(case.id, actor="jonatan", use_mock=True)
    assert not second.ok
    assert "60" in second.message
    assert second.case is None or second.case.get("status") in {"open", "escalated"}


def test_regenerate_allowed_after_cooldown(
    tmp_path, monkeypatch, telemetry, escalation
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = _svc(tmp_path, telemetry=telemetry, escalation=escalation)
    case = svc.store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order 1001 status?",
        category="order_status",
        draft_reply="old",
        order_id="1001",
        message_id="<regen-test-cooldown-ok@azom>",
        site="azom",
    )
    first = svc.regenerate_draft(case.id, actor="jonatan", use_mock=True)
    assert first.ok, first.message
    past = (
        datetime.now(timezone.utc) - timedelta(seconds=61)
    ).isoformat().replace("+00:00", "Z")
    with svc.store._conn() as conn:
        conn.execute(
            "UPDATE cases SET draft_regenerated_at = ? WHERE id = ?",
            (past, case.id),
        )
    second = svc.regenerate_draft(case.id, actor="jonatan", use_mock=True)
    assert second.ok, second.message
