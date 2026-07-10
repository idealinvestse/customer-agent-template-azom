"""RBAC and escalation tests."""

from ecom_ops.escalation import EscalationReason, EscalationService
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor


def test_jonatan_is_viewer():
    actor = resolve_actor("jonatan")
    assert actor.role == "viewer"
    assert actor.has(Permission.READ)
    assert actor.has(Permission.MAIL_READ)
    assert not actor.has(Permission.ORDER_STATUS_UPDATE)
    assert not actor.has(Permission.MAIL_SEND)


def test_oscar_is_full_admin():
    actor = resolve_actor("oscar")
    assert actor.role == "full_admin"
    assert actor.has(Permission.CODE_EDIT)
    assert actor.has(Permission.ORDER_STATUS_UPDATE)
    assert actor.has(Permission.MAIL_SEND)


def test_agent_is_operator():
    actor = resolve_actor("agent")
    assert actor.role == "operator"
    require_permission(actor, Permission.SUPPORT_REPLY)
    require_permission(actor, Permission.MAIL_SEND)
    try:
        require_permission(actor, Permission.CODE_EDIT)
        assert False, "expected AccessDenied"
    except AccessDenied:
        pass


def test_escalation_assigns_oscar(tmp_path):
    store = tmp_path / "esc.jsonl"
    svc = EscalationService(store_path=store, notifiers=[])
    ticket = svc.escalate_critical("disk full", details={"host": "vps1", "password": "secret"})
    assert ticket.assignee == "oscar"
    assert ticket.reason == EscalationReason.CRITICAL
    raw = store.read_text(encoding="utf-8")
    assert "oscar" in raw
    assert "secret" not in raw
    assert "***REDACTED***" in raw

    code = svc.escalate_code_edit("edit theme.php")
    assert code.assignee == "oscar"
    assert code.reason == EscalationReason.CODE_EDIT
