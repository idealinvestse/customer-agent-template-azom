"""Support automation tests."""

from ecom_ops.actions.support import (
    SupportCategory,
    SupportService,
    classify_message,
    extract_order_id,
)


def test_classify_and_extract():
    assert classify_message("Where is order 12345?") == SupportCategory.ORDER_STATUS
    assert extract_order_id("Ordernr: 998877") == "998877"
    assert classify_message("I want a refund please") == SupportCategory.RETURN
    assert classify_message("We will call the lawyer about GDPR complaint") == SupportCategory.ABUSE


def test_support_draft_reply(telemetry, escalation):
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle(
        "Hej, var är min order 1001?",
        customer_name="Anna",
        customer_email="anna@example.com",
        language="sv",
        actor="agent",
    )
    assert result.ok
    assert result.category == SupportCategory.ORDER_STATUS
    assert result.order_id == "1001"
    assert result.reply and "Anna" in result.reply
    assert not result.escalated


def test_critical_escalates_to_oscar(telemetry, escalation):
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle(
        "This is a chargeback and legal threat regarding order 55",
        actor="agent",
    )
    assert result.ok
    assert result.escalated
    assert result.ticket_id
    assert result.category == SupportCategory.ABUSE


def test_jonatan_cannot_reply(telemetry, escalation):
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle("Hej", actor="jonatan")
    assert not result.ok
    assert result.escalated
