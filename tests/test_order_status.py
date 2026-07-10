"""Order status action tests."""

from ecom_ops.actions.order_status import OrderStatusService
from ecom_ops.rbac import resolve_actor


def test_update_order_status_success(woo, telemetry, escalation):
    svc = OrderStatusService(woo=woo, telemetry=telemetry, escalation=escalation)
    result = svc.update(order_id="1001", status="completed", actor="agent")
    assert result.ok
    assert result.previous_status == "processing"
    assert result.new_status == "completed"
    assert result.order is not None
    assert result.order.status == "completed"


def test_update_order_status_noop(woo, telemetry, escalation):
    svc = OrderStatusService(woo=woo, telemetry=telemetry, escalation=escalation)
    result = svc.update(order_id="1001", status="processing")
    assert result.ok
    assert "already" in result.message


def test_jonatan_cannot_update(woo, telemetry, escalation):
    svc = OrderStatusService(woo=woo, telemetry=telemetry, escalation=escalation)
    result = svc.update(
        order_id="1001",
        status="completed",
        actor=resolve_actor("jonatan"),
    )
    assert not result.ok
    assert result.escalated
    assert result.ticket_id


def test_invalid_status(woo, telemetry, escalation):
    svc = OrderStatusService(woo=woo, telemetry=telemetry, escalation=escalation)
    result = svc.update(order_id="1001", status="yeeted")
    assert not result.ok
    assert not result.escalated
