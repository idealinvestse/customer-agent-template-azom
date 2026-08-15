"""Draft save / regenerate. Never sends customer mail."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ecom_ops.actions.support import extract_order_id
from ecom_ops.cases.results import ACTIVE_CASE_STATUSES, CaseActionResult
from ecom_ops.order_context import resolve_order_context, woo_domain_from_market
from ecom_ops.profile import load_profile
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor


def save_draft(
    svc: Any,
    case_id: str,
    body: str,
    *,
    actor: Any = None,
) -> CaseActionResult:
    from ecom_ops.cases.service import _edit_distance_ratio, _seconds_since

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
        previous = case.draft_reply or ""
        updated = svc.store.update_draft(case_id, body)
        edit_dist = _edit_distance_ratio(previous, body)
        svc.telemetry.record(
            action="case_draft_saved",
            site=case.site,
            meta={
                "case_id": case_id,
                "actor": actor_obj.name,
                "draft_edit_distance": round(edit_dist, 4),
                "time_to_first_edit_sec": _seconds_since(case.created_at),
            },
        )
        return CaseActionResult(
            ok=True,
            message="Draft saved",
            case=updated.to_dict() if updated else case.to_dict(),
        )
    except AccessDenied as exc:
        ticket = svc.escalation.escalate_critical(
            f"Case draft save denied for {actor_obj.name}",
            details={"error": str(exc), "case_id": case_id},
        )
        return CaseActionResult(
            ok=False,
            message=str(exc),
            escalated=True,
            ticket_id=ticket.id,
        )


def regenerate_draft(
    svc: Any,
    case_id: str,
    *,
    actor: Any = None,
    use_mock: bool | None = None,
) -> CaseActionResult:
    """Re-run support draft + order context; never sends mail."""
    from ecom_ops.cases.service import (
        REGENERATE_COOLDOWN_SEC,
        _edit_distance_ratio,
        _enrich_draft_with_order,
    )

    try:
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
    except AccessDenied as exc:
        ticket = svc.escalation.escalate_critical(
            f"Case draft regenerate denied: {exc}",
            details={"error": str(exc), "case_id": case_id},
        )
        return CaseActionResult(
            ok=False,
            message=str(exc),
            escalated=True,
            ticket_id=ticket.id,
        )
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

        if case.draft_regenerated_at:
            try:
                raw = str(case.draft_regenerated_at).replace("Z", "+00:00")
                ts = datetime.fromisoformat(raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - ts).total_seconds()
                if age < REGENERATE_COOLDOWN_SEC:
                    return CaseActionResult(
                        ok=False,
                        message="Vänta minst 60 sekunder innan du regenererar igen.",
                        case=case.to_dict(),
                    )
            except ValueError:
                pass

        inbound_body, inbound_subject = svc._inbound_text_for_regen(case)
        text = f"{inbound_subject}\n\n{inbound_body}".strip()
        order_id = case.order_id or extract_order_id(text)
        woo_domain = woo_domain_from_market(case.market)
        order_ctx = resolve_order_context(
            order_id, use_mock=use_mock, domain=woo_domain
        )

        support = svc.support.handle(
            text,
            customer_email=case.from_addr if "@" in (case.from_addr or "") else None,
            language=case.language or "sv",
            site=case.site or load_profile().customer,
            actor=load_profile().operator_actor,
            use_mock=use_mock,
            order_context=order_ctx,
        )
        draft = _enrich_draft_with_order(
            support.reply,
            support.order_id or order_id,
            use_mock=use_mock,
            order_context=order_ctx,
            domain=woo_domain,
        )
        previous = case.draft_reply or ""
        conf = getattr(support, "confidence", None)
        method = getattr(support, "classify_method", None)
        suggest = bool(getattr(support, "suggest_approve", False))
        category = (
            support.category.value
            if hasattr(support.category, "value")
            else str(support.category)
        )
        if case.status == "escalated" and case.category == "abuse":
            category = "abuse"
        if (
            case.status == "escalated"
            or getattr(support, "escalated", False)
            or category == "abuse"
        ):
            suggest = False

        patched = svc._patch_case_after_regen(
            case.id,
            draft=draft or previous,
            draft_before_regen=previous,
            category=category,
            order_id=support.order_id or order_id or case.order_id,
            classify_confidence=conf if isinstance(conf, (int, float)) else None,
            classify_method=method,
            suggest_approve=suggest,
        )
        if getattr(support, "escalated", False) and patched and not patched.escalation_id:
            patched = svc._maybe_escalate(patched, support)

        svc.telemetry.record(
            action="case_draft_regenerated",
            site=case.site,
            meta={
                "case_id": case_id,
                "actor": actor_obj.name,
                "category": category,
                "classify_method": method,
                "confidence": conf,
                "suggest_approve": suggest,
                "draft_edit_distance": round(
                    _edit_distance_ratio(previous, draft or previous), 4
                ),
            },
        )
        final = patched or svc.store.get(case_id) or case
        final = svc._maybe_record_shadow(final)
        return CaseActionResult(
            ok=True,
            message=f"Draft regenerated for {case_id[:8]}",
            case=final.to_dict(),
        )
    except AccessDenied as exc:
        ticket = svc.escalation.escalate_critical(
            f"Case draft regenerate denied for {actor_obj.name}",
            details={"error": str(exc), "case_id": case_id},
        )
        return CaseActionResult(
            ok=False,
            message=str(exc),
            escalated=True,
            ticket_id=ticket.id,
        )
    except Exception as exc:
        return CaseActionResult(ok=False, message=f"Regenerate failed: {exc}")
