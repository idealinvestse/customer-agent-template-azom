"""Case ingest from mail + approve/send drafts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ecom_ops.actions.mail import MailService
from ecom_ops.actions.support import SupportService
from ecom_ops.cases.mailboxes import MailboxConfig, enabled_mailboxes
from ecom_ops.cases.store import Case, CaseStore
from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.mail import MailClient, MailMessage, client_from_env
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import SecurityError, validate_site
from ecom_ops.telemetry import Telemetry, default_telemetry


@dataclass(frozen=True)
class IngestResult:
    ok: bool
    message: str
    created: int = 0
    skipped: int = 0
    cases: list[dict[str, Any]] | None = None
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "created": self.created,
            "skipped": self.skipped,
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
        created_cases: list[dict[str, Any]] = []

        for mb in mailboxes:
            client = client_from_env(provider=mb.provider, use_mock=use_mock)
            try:
                messages = client.fetch(folder="INBOX", unread_only=True, limit=limit_per_mailbox)
            except Exception as exc:
                self.telemetry.record(
                    action="case_poll_error",
                    site=mb.site,
                    meta={"mailbox_id": mb.id, "error": str(exc)[:200]},
                )
                continue

            for msg in messages:
                result = self._ingest_message(mb, msg, actor=actor_obj)
                if result is None:
                    skipped += 1
                else:
                    created += 1
                    created_cases.append(result.to_dict())

        self.telemetry.record(
            action="case_poll",
            site="azom",
            meta={"created": created, "skipped": skipped, "mailboxes": len(mailboxes)},
        )
        return IngestResult(
            ok=True,
            message=f"Polled {len(mailboxes)} mailbox(es)",
            created=created,
            skipped=skipped,
            cases=created_cases,
        )

    def _ingest_message(
        self,
        mb: MailboxConfig,
        msg: MailMessage,
        *,
        actor: Actor,
    ) -> Case | None:
        mid = (msg.message_id or msg.uid or "").strip() or None
        if mid and self.store.find_by_message_id(mid):
            return None

        body = msg.body or ""
        subject = msg.subject or "(no subject)"
        from_addr = msg.from_addr or "unknown@unknown"

        support = self.support.handle(
            f"{subject}\n\n{body}",
            customer_email=from_addr if "@" in from_addr else None,
            language=mb.language,
            site=mb.site,
            actor="agent",  # draft generation uses operator
        )

        case = self.store.create_case(
            mailbox_id=mb.id,
            subject=subject,
            from_addr=from_addr,
            body=body,
            category=support.category.value,
            draft_reply=support.reply,
            order_id=support.order_id,
            message_id=mid,
            site=validate_site(mb.site),
            market=mb.market,
            language=mb.language,
            to_addr=mb.address,
        )
        self.telemetry.record(
            action="case_created",
            site=mb.site,
            meta={
                "case_id": case.id,
                "mailbox_id": mb.id,
                "category": case.category,
                "actor": actor.name,
            },
        )
        return case

    def list_open(self, *, limit: int = 50) -> list[Case]:
        return self.store.list_cases(status="open", limit=limit)

    def get(self, case_id: str) -> Case | None:
        return self.store.get(case_id)

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
            if case.status != "open":
                return CaseActionResult(
                    ok=False,
                    message=f"Case status is {case.status}, expected open",
                    case=case.to_dict(),
                )
            body = (body_override or case.draft_reply or "").strip()
            if not body:
                return CaseActionResult(ok=False, message="No draft to send", case=case.to_dict())

            subject = case.subject
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"

            send = self.mail.send(
                to=case.from_addr,
                subject=subject,
                body=body,
                site=case.site,
                actor="agent",  # mail send via operator; approval audited below
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
