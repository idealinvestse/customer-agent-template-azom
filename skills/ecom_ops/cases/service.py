"""Case ingest from mail + approve/send drafts (Cases 2.0)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ecom_ops.actions.mail import MailService
from ecom_ops.actions.support import SupportService, extract_order_id
from ecom_ops.cases.mailboxes import MailboxConfig, enabled_mailboxes
from ecom_ops.cases.store import Case, CaseStore
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.mail import MailClient, MailMessage, client_from_env
from ecom_ops.order_context import (
    draft_has_order_block,
    resolve_order_context,
)
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import SecurityError, validate_site
from ecom_ops.telemetry import Telemetry, default_telemetry

_ACTIVE = ("open", "escalated")


def _edit_distance_ratio(a: str, b: str) -> float:
    """Normalized Levenshtein distance in [0, 1] (1 = totally different)."""
    s1, s2 = a or "", b or ""
    if s1 == s2:
        return 0.0
    n, m = len(s1), len(s2)
    if n == 0 or m == 0:
        return 1.0
    # Bound work for long drafts
    if n * m > 250_000:
        # Cheap proxy: char set / length delta
        return min(1.0, abs(n - m) / max(n, m) + (0.0 if s1[:80] == s2[:80] else 0.5))
    prev = list(range(m + 1))
    for i, c1 in enumerate(s1, 1):
        cur = [i]
        for j, c2 in enumerate(s2, 1):
            ins, delete, sub = cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (c1 != c2)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[m] / max(n, m)


def _seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        raw = str(iso).replace("Z", "+00:00")
        created = datetime.fromisoformat(raw)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
    except Exception:
        return None


@dataclass(frozen=True)
class IngestResult:
    ok: bool
    message: str
    created: int = 0
    skipped: int = 0
    errors: int = 0
    cases: list[dict[str, Any]] | None = None
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "created": self.created,
            "skipped": self.skipped,
            "errors": self.errors,
            "cases": self.cases or [],
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
        }


@dataclass(frozen=True)
class CaseActionResult:
    ok: bool
    message: str
    case: dict[str, Any] | None = None
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "case": self.case,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
        }


def _enrich_draft_with_order(
    draft: str | None,
    order_id: str | None,
    *,
    use_mock: bool | None = None,
    order_context: str | None = None,
) -> str:
    base = (draft or "").strip()
    if not order_id:
        return base
    if draft_has_order_block(base, order_id):
        return base
    block = (order_context or "").strip()
    if not block:
        block = resolve_order_context(order_id, use_mock=use_mock) or ""
    if not block:
        return base
    if block in base:
        return base
    return f"{block}\n\n{base}"


def _outbound_thread_headers(
    case: Case, store: CaseStore
) -> tuple[str | None, str | None]:
    """Build In-Reply-To / References for an outbound case reply."""
    msgs = store.messages(case.id)
    inbound = [m for m in msgs if m.direction == "inbound"]
    parent: str | None = None
    refs: list[str] = []
    if inbound:
        last = inbound[-1]
        parent = (last.message_id or case.message_id or "").strip() or None
        if last.references_header:
            refs.extend(last.references_header.split())
        if last.in_reply_to:
            refs.append(last.in_reply_to)
    else:
        parent = (case.message_id or "").strip() or None
    if parent:
        refs.append(parent)
    seen: set[str] = set()
    unique: list[str] = []
    for part in refs:
        p = part.strip()
        if p and p not in seen:
            seen.add(p)
            unique.append(p)
    return parent, (" ".join(unique) if unique else None)


class CaseService:
    def __init__(
        self,
        store: CaseStore | None = None,
        *,
        mail: MailService | None = None,
        support: SupportService | None = None,
        telemetry: Telemetry | None = None,
        escalation: EscalationService | None = None,
        mail_client: MailClient | None = None,
    ) -> None:
        self.store = store or CaseStore()
        self.mail = mail or MailService(client=mail_client)
        self.support = support or SupportService()
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation
        self._injected_mail_client = mail_client

    def evaluate_auto_send_eligibility(
        self,
        case: Case | dict[str, Any],
        *,
        auto_sends_today: int = 0,
    ) -> bool:
        """Rails-only checkpoint: eligibility for a future Oscar auto-send experiment.

        Never sends mail. ``poll`` / ingest must not call this to dispatch outbound
        mail — human ``approve_and_send`` remains the live path while
        ``auto_send_enabled`` defaults to false (and even when True until an
        experiment wires a sender).
        """
        from ecom_ops.cases.auto_send import should_auto_send

        if isinstance(case, Case):
            category = case.category
            confidence = float(case.classify_confidence or 0.0)
            order_id = case.order_id
            escalated = case.status == "escalated" or bool(case.escalation_id)
        else:
            category = str(case.get("category") or "")
            confidence = float(case.get("classify_confidence") or 0.0)
            order_id = case.get("order_id")
            escalated = (
                str(case.get("status") or "") == "escalated"
                or bool(case.get("escalation_id"))
            )
        return should_auto_send(
            category=category,
            confidence=confidence,
            order_id=order_id,
            escalated=escalated,
            auto_sends_today=auto_sends_today,
        )

    def poll(
        self,
        *,
        limit_per_mailbox: int = 20,
        actor: Actor | str | None = None,
        use_mock: bool | None = None,
    ) -> IngestResult:
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        try:
            require_permission(actor_obj, Permission.MAIL_READ)
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
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
            return IngestResult(ok=True, message="No enabled mailboxes", created=0)

        created = 0
        skipped = 0
        errors = 0
        created_cases: list[dict[str, Any]] = []
        error_details: list[dict[str, str]] = []

        for mb in mailboxes:
            client = self._injected_mail_client or client_from_env(
                provider=mb.provider, use_mock=use_mock
            )
            try:
                messages = client.fetch(
                    folder="INBOX", unread_only=True, limit=limit_per_mailbox
                )
            except Exception as exc:
                errors += 1
                err_text = str(exc)[:200]
                error_details.append({"mailbox_id": mb.id, "error": err_text})
                self.telemetry.record(
                    action="case_poll_error",
                    site=mb.site,
                    meta={"mailbox_id": mb.id, "error": err_text},
                )
                continue

            for msg in messages:
                result = self._ingest_message(
                    mb, msg, actor=actor_obj, client=client, use_mock=use_mock
                )
                if result is None:
                    skipped += 1
                else:
                    created += 1
                    created_cases.append(result.to_dict())

        self.telemetry.record(
            action="case_poll",
            site="azom",
            meta={
                "created": created,
                "skipped": skipped,
                "errors": errors,
                "mailboxes": len(mailboxes),
            },
        )
        # All mailboxes failed → not ok; partial success still ok=True
        all_failed = errors > 0 and errors == len(mailboxes)
        escalated = False
        ticket_id: str | None = None
        if errors > 0:
            ticket = self.escalation.escalate_critical(
                f"Case poll: {errors} mailbox(es) failed",
                details={
                    "errors": errors,
                    "mailboxes": len(mailboxes),
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
                extra={"mailboxes": len(mailboxes), "skipped": skipped},
            )
        except Exception:
            pass
        return IngestResult(
            ok=not all_failed,
            message=(
                f"Polled {len(mailboxes)} mailbox(es)"
                + (f" ({errors} failed)" if errors else "")
            ),
            created=created,
            skipped=skipped,
            errors=errors,
            cases=created_cases,
            escalated=escalated,
            ticket_id=ticket_id,
        )

    def _ingest_message(
        self,
        mb: MailboxConfig,
        msg: MailMessage,
        *,
        actor: Actor,
        client: MailClient,
        use_mock: bool | None = None,
    ) -> Case | None:
        mid = (msg.message_id or msg.uid or "").strip() or None
        if mid and self.store.find_by_message_id(mid):
            self._best_effort_mark_read(client, msg)
            return None

        body = msg.body or ""
        subject = msg.subject or "(no subject)"
        from_addr = msg.from_addr or "unknown@unknown"
        in_reply_to = getattr(msg, "in_reply_to", None)
        references_header = getattr(msg, "references_header", None)

        # Resolve Woo order once for LLM prompt + template prepend (avoid double-fetch).
        preview_order_id = extract_order_id(f"{subject}\n\n{body}")
        order_ctx = resolve_order_context(preview_order_id, use_mock=use_mock)

        support = self.support.handle(
            f"{subject}\n\n{body}",
            customer_email=from_addr if "@" in from_addr else None,
            language=mb.language,
            site=mb.site,
            actor="agent",
            use_mock=use_mock,
            order_context=order_ctx,
        )
        draft = _enrich_draft_with_order(
            support.reply,
            support.order_id,
            use_mock=use_mock,
            order_context=order_ctx,
        )

        threaded = self.store.find_by_thread_headers(
            in_reply_to=in_reply_to,
            references_header=references_header,
            from_addr=from_addr,
            subject=subject,
            mailbox_id=mb.id,
        )

        if threaded:
            case = self.store.append_inbound(
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
                suggest_approve=bool(getattr(support, "suggest_approve", False)),
            )
            if case is None:
                return None
            case = self._maybe_escalate(case, support)
            self.telemetry.record(
                action="case_threaded",
                site=mb.site,
                meta={
                    "case_id": case.id,
                    "mailbox_id": mb.id,
                    "category": case.category,
                    "actor": actor.name,
                },
            )
            self._best_effort_mark_read(client, msg)
            return case

        status = "open"
        priority = "normal"
        escalation_id = None
        if support.escalated and support.ticket_id:
            status = "escalated"
            priority = "high"
            escalation_id = support.ticket_id
        elif support.escalated:
            ticket = self.escalation.escalate_critical(
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

        case = self.store.create_case(
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
        self.telemetry.record(
            action="case_created",
            site=mb.site,
            meta={
                "case_id": case.id,
                "mailbox_id": mb.id,
                "category": case.category,
                "status": case.status,
                "actor": actor.name,
            },
        )
        self._best_effort_mark_read(client, msg)
        return case

    def _maybe_escalate(self, case: Case, support: Any) -> Case:
        if not getattr(support, "escalated", False):
            return case
        if case.escalation_id:
            return case
        ticket_id = getattr(support, "ticket_id", None)
        if not ticket_id:
            ticket = self.escalation.escalate_critical(
                f"Case threaded escalate: {case.subject[:80]}",
                details={"case_id": case.id, "category": case.category},
            )
            ticket_id = ticket.id
        updated = self.store.set_escalation(case.id, ticket_id)
        return updated or case

    def _best_effort_mark_read(self, client: MailClient, msg: MailMessage) -> None:
        uid = (msg.uid or "").strip()
        if not uid:
            return
        try:
            client.mark_read(uid, folder="INBOX")
        except Exception as exc:
            self.telemetry.record(
                action="case_mark_read_error",
                site="azom",
                meta={"uid": uid, "error": str(exc)[:200]},
            )

    def list_open(self, *, limit: int = 50) -> list[Case]:
        return self.store.list_cases(status="open,escalated", limit=limit)

    def get(self, case_id: str) -> Case | None:
        return self.store.get(case_id)

    def next_in_queue(
        self,
        after_id: str,
        *,
        status: str = "open,escalated",
        mailbox_id: str | None = None,
        category: str | None = None,
        suggest_only: bool = False,
        limit: int = 100,
    ) -> Case | None:
        """Return the next case after ``after_id`` using list-view sort order.

        Sort: escalated → high priority → suggest_approve → newest first.
        Used by dashboard "Godkänn & nästa" / "Nästa".
        """
        rows = self.store.list_cases(
            status=status if status != "all" else None,
            mailbox_id=mailbox_id or None,
            category=category or None,
            suggest_approve=True if suggest_only else None,
            limit=limit,
        )
        rows.sort(key=lambda c: c.created_at or "", reverse=True)
        rows.sort(key=lambda c: 0 if getattr(c, "suggest_approve", False) else 1)
        rows.sort(key=lambda c: 0 if (c.priority or "") == "high" else 1)
        rows.sort(key=lambda c: 0 if c.status == "escalated" else 1)
        found = False
        for c in rows:
            if found:
                return c
            if c.id == after_id:
                found = True
        return None

    def save_draft(
        self,
        case_id: str,
        body: str,
        *,
        actor: Actor | str | None = None,
    ) -> CaseActionResult:
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        try:
            require_permission(actor_obj, Permission.CASE_REPLY)
            case = self.store.get(case_id)
            if not case:
                return CaseActionResult(ok=False, message="Case not found")
            if case.status not in _ACTIVE:
                return CaseActionResult(
                    ok=False,
                    message=f"Case status is {case.status}, expected open/escalated",
                    case=case.to_dict(),
                )
            previous = case.draft_reply or ""
            updated = self.store.update_draft(case_id, body)
            edit_dist = _edit_distance_ratio(previous, body)
            self.telemetry.record(
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
            ticket = self.escalation.escalate_critical(
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
        self,
        case_id: str,
        *,
        actor: Actor | str | None = None,
        use_mock: bool | None = None,
    ) -> CaseActionResult:
        """Re-run support draft + order context; never sends mail.

        Draft generation uses operator ``agent`` (SUPPORT_REPLY) while the
        human caller needs CASE_REPLY (Jonatan may regenerate / approve).
        """
        try:
            actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
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
            case = self.store.get(case_id)
            if not case:
                return CaseActionResult(ok=False, message="Case not found")
            if case.status not in _ACTIVE:
                return CaseActionResult(
                    ok=False,
                    message=f"Case status is {case.status}, expected open/escalated",
                    case=case.to_dict(),
                )

            inbound_body, inbound_subject = self._inbound_text_for_regen(case)
            text = f"{inbound_subject}\n\n{inbound_body}".strip()
            order_id = case.order_id or extract_order_id(text)
            order_ctx = resolve_order_context(order_id, use_mock=use_mock)

            # Ingest path uses agent for SUPPORT_REPLY; keep same here.
            support = self.support.handle(
                text,
                customer_email=case.from_addr if "@" in (case.from_addr or "") else None,
                language=case.language or "sv",
                site=case.site or "azom",
                actor="agent",
                use_mock=use_mock,
                order_context=order_ctx,
            )
            draft = _enrich_draft_with_order(
                support.reply,
                support.order_id or order_id,
                use_mock=use_mock,
                order_context=order_ctx,
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
            # Preserve abuse/escalated presentation — do not demote to routine
            if case.status == "escalated" and case.category == "abuse":
                category = "abuse"
            if (
                case.status == "escalated"
                or getattr(support, "escalated", False)
                or category == "abuse"
            ):
                suggest = False

            patched = self._patch_case_after_regen(
                case.id,
                draft=draft or previous,
                category=category,
                order_id=support.order_id or order_id or case.order_id,
                classify_confidence=conf if isinstance(conf, (int, float)) else None,
                classify_method=method,
                suggest_approve=suggest,
            )
            if getattr(support, "escalated", False) and patched and not patched.escalation_id:
                patched = self._maybe_escalate(patched, support)

            self.telemetry.record(
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
            final = patched or self.store.get(case_id) or case
            return CaseActionResult(
                ok=True,
                message=f"Draft regenerated for {case_id[:8]}",
                case=final.to_dict(),
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
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

    def _inbound_text_for_regen(self, case: Case) -> tuple[str, str]:
        msgs = self.store.messages(case.id)
        inbound = [m for m in msgs if (m.direction or "") == "inbound"]
        if inbound:
            last = inbound[-1]
            body = (last.body or "").strip()
            subject = (last.subject or case.subject or "").strip()
            if body:
                return body, subject
        # Fallback: subject + empty (still classifiable)
        return (case.subject or "").strip(), case.subject or ""

    def _patch_case_after_regen(
        self,
        case_id: str,
        *,
        draft: str,
        category: str,
        order_id: str | None,
        classify_confidence: float | None,
        classify_method: str | None,
        suggest_approve: bool,
    ) -> Case | None:
        """Update draft + AI fields without inserting phantom messages."""
        with self.store._conn() as conn:
            conn.execute(
                """
                UPDATE cases SET
                    draft_reply = ?,
                    category = ?,
                    order_id = COALESCE(?, order_id),
                    classify_confidence = ?,
                    classify_method = ?,
                    suggest_approve = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    draft,
                    category,
                    order_id,
                    classify_confidence,
                    classify_method,
                    1 if suggest_approve else 0,
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    case_id,
                ),
            )
        return self.store.get(case_id)

    def close(
        self,
        case_id: str,
        *,
        actor: Actor | str | None = None,
        reason: str | None = None,
    ) -> CaseActionResult:
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        try:
            require_permission(actor_obj, Permission.CASE_REPLY)
            case = self.store.get(case_id)
            if not case:
                return CaseActionResult(ok=False, message="Case not found")
            if case.status == "closed":
                return CaseActionResult(
                    ok=True, message="Already closed", case=case.to_dict()
                )
            updated = self.store.close(case_id)
            self.telemetry.record(
                action="case_closed",
                site=case.site,
                meta={
                    "case_id": case_id,
                    "actor": actor_obj.name,
                    "reason": (reason or "")[:200],
                },
            )
            return CaseActionResult(
                ok=True,
                message="Case closed",
                case=updated.to_dict() if updated else case.to_dict(),
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"Case close denied for {actor_obj.name}",
                details={"error": str(exc), "case_id": case_id},
            )
            return CaseActionResult(
                ok=False,
                message=str(exc),
                escalated=True,
                ticket_id=ticket.id,
            )

    def approve_and_send(
        self,
        case_id: str,
        *,
        actor: Actor | str | None = None,
        body_override: str | None = None,
    ) -> CaseActionResult:
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        try:
            require_permission(actor_obj, Permission.CASE_REPLY)
            case = self.store.get(case_id)
            if not case:
                return CaseActionResult(ok=False, message="Case not found")
            if case.status not in _ACTIVE:
                return CaseActionResult(
                    ok=False,
                    message=f"Case status is {case.status}, expected open/escalated",
                    case=case.to_dict(),
                )
            body = (body_override or case.draft_reply or "").strip()
            if not body:
                return CaseActionResult(ok=False, message="No draft to send", case=case.to_dict())

            subject = case.subject
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"

            in_reply_to, references_header = _outbound_thread_headers(case, self.store)

            # Jonatan has CASE_REPLY but not MAIL_SEND — case approve is the
            # intentional send path; pass the real approving actor for audit.
            send = self.mail.send(
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
                return CaseActionResult(
                    ok=False,
                    message=send.message,
                    case=case.to_dict(),
                    escalated=send.escalated,
                    ticket_id=send.ticket_id,
                )

            updated = self.store.mark_replied(
                case_id,
                outbound_body=body,
                to_addr=case.from_addr,
                from_addr="",
                subject=subject,
            )
            self.telemetry.record(
                action="case_replied",
                site=case.site,
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
            return CaseActionResult(
                ok=True,
                message="Reply sent and case marked replied",
                case=updated.to_dict() if updated else case.to_dict(),
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
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
