"""HITL approve-and-send — isolated from poll/ingest. Never called by poll."""

from __future__ import annotations

import logging
from typing import Any

from ecom_ops.actions.mail import NULL_SEND_REFUSED_MSG
from ecom_ops.cases.results import ACTIVE_CASE_STATUSES, CaseActionResult
from ecom_ops.profile import load_profile
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.runtime_profile import null_send_active
from ecom_ops.security import SecurityError

_log = logging.getLogger("ecom_ops.cases")


def approve_and_send(
    svc: Any,
    case_id: str,
    *,
    actor: Actor | str | None = None,
    body_override: str | None = None,
) -> CaseActionResult:
    """Human approve path. Callers: CLI reply, dashboard, Messenger/Telegram button."""
    from ecom_ops.cases.service import (
        _edit_distance_ratio,
        _outbound_thread_headers,
        _seconds_since,
    )

    actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
    try:
        require_permission(actor_obj, Permission.CASE_REPLY)
        case = svc.store.get(case_id)
        if not case:
            return CaseActionResult(ok=False, message="Case not found")
        if case.status not in ACTIVE_CASE_STATUSES:
            return CaseActionResult(
                ok=False,
                message=f"Case status is {case.status}, expected open/escalated",
                case=case.to_dict(),
            )
        body = (body_override or case.draft_reply or "").strip()
        if not body:
            return CaseActionResult(ok=False, message="No draft to send", case=case.to_dict())

        if null_send_active():
            _log.info(
                "case_reply_blocked_null_send",
                extra={"actor": actor_obj.name, "case_id": case_id, "action": "approve"},
            )
            svc.telemetry.record(
                action="case_reply_blocked_null_send",
                site=case.site or load_profile().customer,
                case_id=case_id,
                meta={
                    "case_id": case_id,
                    "actor": actor_obj.name,
                    "reason": "null_send",
                },
            )
            return CaseActionResult(
                ok=False,
                message=NULL_SEND_REFUSED_MSG,
                case=case.to_dict(),
            )

        prior_status = case.status
        claimed = svc.store.claim_for_send(case_id)
        if not claimed:
            fresh = svc.store.get(case_id)
            return CaseActionResult(
                ok=False,
                message=(
                    f"Case status is {fresh.status if fresh else 'unknown'}, "
                    "expected open/escalated (already claimed or replied)"
                ),
                case=fresh.to_dict() if fresh else case.to_dict(),
            )

        subject = case.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        in_reply_to, references_header = _outbound_thread_headers(case, svc.store)

        send = svc.mail.send(
            to=case.from_addr,
            subject=subject,
            body=body,
            site=case.site,
            actor=actor_obj,
            required_permission=Permission.CASE_REPLY,
            in_reply_to=in_reply_to,
            references_header=references_header,
        )
        if not send.ok:
            svc.store.release_send_claim(case_id, status=prior_status)
            restored = svc.store.get(case_id) or case
            return CaseActionResult(
                ok=False,
                message=send.message,
                case=restored.to_dict(),
                escalated=send.escalated,
                ticket_id=send.ticket_id,
            )

        updated = svc.store.mark_replied(
            case_id,
            outbound_body=body,
            to_addr=case.from_addr,
            from_addr="",
            subject=subject,
            message_id=(
                (send.provider_status or {}).get("message_id")
                if isinstance(send.provider_status, dict)
                else None
            ),
        )
        if not updated:
            return CaseActionResult(
                ok=False,
                message="Mail sent but case could not be marked replied",
                case=(svc.store.get(case_id) or claimed).to_dict(),
            )
        svc.telemetry.record(
            action="case_replied",
            site=case.site,
            case_id=case_id,
            meta={
                "case_id": case_id,
                "approved_by": actor_obj.name,
                "category": case.category,
                "suggest_approve": bool(getattr(case, "suggest_approve", False)),
                "time_to_approve_sec": _seconds_since(case.created_at),
                "draft_edit_distance": round(
                    _edit_distance_ratio(case.draft_reply or "", body), 4
                ),
            },
        )
        from ecom_ops.audit import log_action

        log_action(
            actor=actor_obj.name,
            action="case_reply_send",
            target="case",
            target_id=case_id,
            details={
                "to": case.from_addr,
                "category": case.category,
                "suggest_approve": bool(getattr(case, "suggest_approve", False)),
            },
        )
        return CaseActionResult(
            ok=True,
            message="Reply sent and case marked replied",
            case=updated.to_dict() if updated else case.to_dict(),
        )
    except AccessDenied as exc:
        ticket = svc.escalation.escalate_critical(
            f"Case reply denied for {actor_obj.name}",
            details={"error": str(exc), "case_id": case_id},
        )
        return CaseActionResult(
            ok=False,
            message=str(exc),
            escalated=True,
            ticket_id=ticket.id,
        )
    except SecurityError as exc:
        return CaseActionResult(ok=False, message=str(exc))
