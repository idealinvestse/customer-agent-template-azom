"""Mailbox poll + ingest. Must never send customer mail or call approve."""

from __future__ import annotations

import logging
from typing import Any

from ecom_ops.actions.support import extract_order_id
from ecom_ops.cases.mailboxes import MailboxConfig, enabled_mailboxes
from ecom_ops.cases.results import IngestResult
from ecom_ops.cases.store import Case
from ecom_ops.integrations.mail import MailClient, MailMessage, client_from_env
from ecom_ops.json_logging import configure_json_logging
from ecom_ops.order_context import resolve_order_context, woo_domain_from_market
from ecom_ops.profile import load_profile
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import validate_site

_log = logging.getLogger("ecom_ops.cases")


def run_poll(
    svc: Any,
    *,
    limit_per_mailbox: int = 20,
    actor: Any = None,
    use_mock: bool | None = None,
) -> IngestResult:
    """Poll enabled mailboxes and ingest inbound mail. Never sends."""
    configure_json_logging()
    actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
    _log.info("cases_poll_start", extra={"actor": actor_obj.name, "action": "cases_poll"})
    try:
        require_permission(actor_obj, Permission.MAIL_READ)
    except AccessDenied as exc:
        ticket = svc.escalation.escalate_critical(
            f"Case poll denied for {actor_obj.name}",
            details={"error": str(exc)},
        )
        return IngestResult(
            ok=False,
            message=str(exc),
            escalated=True,
            ticket_id=ticket.id,
        )

    mailboxes = enabled_mailboxes()
    if not mailboxes:
        if use_mock:
            return IngestResult(ok=True, message="No enabled mailboxes", created=0)
        ticket = svc.escalation.escalate_critical(
            "Case poll: no enabled mailboxes",
            details={"detail": "no_enabled_mailboxes"},
        )
        try:
            from ecom_ops.ops_status import write_last_case_poll

            write_last_case_poll(
                ok=False,
                errors=1,
                created=0,
                extra={"detail": "no_enabled_mailboxes", "mailboxes": 0},
            )
        except Exception:
            pass
        return IngestResult(
            ok=False,
            message="No enabled mailboxes configured",
            created=0,
            errors=1,
            escalated=True,
            ticket_id=ticket.id,
        )

    try:
        svc.store.release_stale_send_claims()
    except Exception:
        pass

    created = 0
    skipped = 0
    errors = 0
    created_cases: list[dict[str, Any]] = []
    error_details: list[dict[str, str]] = []
    per_mailbox: list[dict[str, Any]] = []

    for mb in mailboxes:
        if svc._shutdown_requested:
            break
        client = svc._injected_mail_client or client_from_env(
            provider=mb.provider,
            env_prefix=mb.env_prefix,
            use_mock=use_mock,
        )
        mb_created = 0
        mb_skipped = 0
        try:
            messages = client.fetch(
                folder="INBOX", unread_only=True, limit=limit_per_mailbox
            )
        except Exception as exc:
            errors += 1
            err_text = str(exc)[:200]
            error_details.append({"mailbox_id": mb.id, "error": err_text})
            per_mailbox.append({
                "mailbox_id": mb.id,
                "status": "error",
                "error": err_text,
                "created": 0,
                "skipped": 0,
            })
            svc.telemetry.record(
                action="case_poll_error",
                site=mb.site,
                meta={"mailbox_id": mb.id, "error": err_text},
            )
            continue

        for msg in messages:
            try:
                result = ingest_message(
                    svc, mb, msg, actor=actor_obj, client=client, use_mock=use_mock
                )
            except Exception as exc:
                errors += 1
                err_text = str(exc)[:200]
                error_details.append({"mailbox_id": mb.id, "error": err_text})
                _log.warning(
                    "cases_ingest_error",
                    extra={"mailbox_id": mb.id, "error": err_text},
                )
                continue
            if result is None:
                skipped += 1
                mb_skipped += 1
            else:
                created += 1
                mb_created += 1
                created_cases.append(result.to_dict())
        per_mailbox.append({
            "mailbox_id": mb.id,
            "status": "ok",
            "created": mb_created,
            "skipped": mb_skipped,
        })

    all_failed = errors > 0 and errors == len(mailboxes)
    partial = errors > 0 and not all_failed
    svc.telemetry.record(
        action="case_poll",
        site=load_profile().customer,
        meta={
            "created": created,
            "skipped": skipped,
            "errors": errors,
            "mailboxes": len(mailboxes),
            "partial": partial,
            "all_failed": all_failed,
            "failed_mailboxes": [e.get("mailbox_id") for e in error_details],
        },
    )
    escalated = False
    ticket_id: str | None = None
    if errors > 0:
        summary = (
            f"Case poll: ALL {errors} mailbox(es) failed"
            if all_failed
            else f"Case poll: PARTIAL — {errors}/{len(mailboxes)} mailbox(es) failed"
        )
        ticket = svc.escalation.escalate_critical(
            summary,
            details={
                "errors": errors,
                "mailboxes": len(mailboxes),
                "partial": partial,
                "all_failed": all_failed,
                "failures": error_details,
            },
        )
        escalated = True
        ticket_id = ticket.id
    try:
        from ecom_ops.ops_status import write_last_case_poll

        write_last_case_poll(
            ok=not all_failed,
            errors=errors,
            created=created,
            extra={
                "mailboxes": len(mailboxes),
                "skipped": skipped,
                "partial": partial,
                "failures": error_details[:10],
            },
        )
    except Exception:
        pass
    if all_failed:
        msg = f"Polled {len(mailboxes)} mailbox(es) — all failed"
    elif partial:
        failed_ids = ",".join(
            str(e.get("mailbox_id") or "?") for e in error_details[:5]
        )
        msg = (
            f"Polled {len(mailboxes)} mailbox(es) — PARTIAL "
            f"({errors} failed: {failed_ids})"
        )
    else:
        msg = f"Polled {len(mailboxes)} mailbox(es)"
    return IngestResult(
        ok=not all_failed,
        message=msg,
        created=created,
        skipped=skipped,
        errors=errors,
        cases=created_cases,
        escalated=escalated,
        ticket_id=ticket_id,
        per_mailbox=per_mailbox,
    )


def ingest_message(
    svc: Any,
    mb: MailboxConfig,
    msg: MailMessage,
    *,
    actor: Actor,
    client: MailClient,
    use_mock: bool | None = None,
) -> Case | None:
    from ecom_ops.cases.service import _enrich_draft_with_order

    mid = (msg.message_id or msg.uid or "").strip() or None
    if mid and svc.store.find_by_message_id(mid):
        svc._best_effort_mark_read(client, msg)
        return None

    body = msg.body or ""
    subject = msg.subject or "(no subject)"
    from_addr = msg.from_addr or "unknown@unknown"
    in_reply_to = getattr(msg, "in_reply_to", None)
    references_header = getattr(msg, "references_header", None)

    preview_order_id = extract_order_id(f"{subject}\n\n{body}")
    woo_domain = woo_domain_from_market(mb.market)
    order_ctx = resolve_order_context(
        preview_order_id, use_mock=use_mock, domain=woo_domain
    )

    support = svc.support.handle(
        f"{subject}\n\n{body}",
        customer_email=from_addr if "@" in from_addr else None,
        language=mb.language,
        site=mb.site,
        actor=load_profile().operator_actor,
        use_mock=use_mock,
        order_context=order_ctx,
    )
    draft = _enrich_draft_with_order(
        support.reply,
        support.order_id,
        use_mock=use_mock,
        order_context=order_ctx,
        domain=woo_domain,
    )

    threaded = svc.store.find_by_thread_headers(
        in_reply_to=in_reply_to,
        references_header=references_header,
        from_addr=from_addr,
        subject=subject,
        mailbox_id=mb.id,
    )

    if threaded:
        suggest = bool(getattr(support, "suggest_approve", False))
        cat = getattr(support, "category", None)
        cat_val = getattr(cat, "value", cat) if cat is not None else None
        if (
            threaded.status == "escalated"
            or threaded.category == "abuse"
            or cat_val == "abuse"
        ):
            suggest = False
        thread_priority = None
        if (
            cat_val in {"return", "billing"}
            and threaded.status != "escalated"
            and cat_val != "abuse"
            and threaded.category != "abuse"
        ):
            thread_priority = "high"
        case = svc.store.append_inbound(
            threaded.id,
            from_addr=from_addr,
            to_addr=mb.address,
            subject=subject,
            body=body,
            message_id=mid,
            in_reply_to=in_reply_to,
            references_header=references_header,
            draft_reply=draft,
            category=support.category.value,
            order_id=support.order_id or threaded.order_id,
            classify_confidence=getattr(support, "confidence", None),
            classify_method=getattr(support, "classify_method", None),
            suggest_approve=suggest,
            priority=thread_priority,
        )
        if case is None:
            return None
        case = svc._maybe_escalate(case, support)
        if case.status == "escalated" or case.category == "abuse":
            if getattr(case, "suggest_approve", False):
                case = svc.store.set_suggest_approve(case.id, False) or case
        svc.telemetry.record(
            action="case_threaded",
            site=mb.site,
            meta={
                "case_id": case.id,
                "mailbox_id": mb.id,
                "category": case.category,
                "actor": actor.name,
            },
        )
        case = svc._maybe_record_shadow(case)
        svc._best_effort_mark_read(client, msg)
        return case

    status = "open"
    priority = "normal"
    escalation_id = None
    if support.escalated and support.ticket_id:
        status = "escalated"
        priority = "high"
        escalation_id = support.ticket_id
    elif support.escalated:
        ticket = svc.escalation.escalate_critical(
            f"Case ingest escalated: {subject[:80]}",
            details={
                "mailbox_id": mb.id,
                "from_addr": from_addr,
                "category": support.category.value,
            },
        )
        status = "escalated"
        priority = "high"
        escalation_id = ticket.id
    elif support.category.value in {"return", "billing"}:
        priority = "high"

    case = svc.store.create_case(
        mailbox_id=mb.id,
        subject=subject,
        from_addr=from_addr,
        body=body,
        category=support.category.value,
        draft_reply=draft,
        order_id=support.order_id,
        message_id=mid,
        site=validate_site(mb.site),
        market=mb.market,
        language=mb.language,
        to_addr=mb.address,
        status=status,
        priority=priority,
        escalation_id=escalation_id,
        in_reply_to=in_reply_to,
        references_header=references_header,
        classify_confidence=getattr(support, "confidence", None),
        classify_method=getattr(support, "classify_method", None),
        suggest_approve=bool(getattr(support, "suggest_approve", False))
        and status != "escalated",
    )
    svc.telemetry.record(
        action="case_created",
        site=mb.site,
        case_id=case.id,
        meta={
            "case_id": case.id,
            "mailbox_id": mb.id,
            "category": case.category,
            "status": case.status,
            "actor": actor.name,
        },
    )
    case = svc._maybe_record_shadow(case)
    svc._best_effort_mark_read(client, msg)
    return case
