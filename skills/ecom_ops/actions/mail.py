"""Mail actions: send, fetch, reply with RBAC + telemetry + escalation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ecom_ops.escalation import EscalationService, default_escalation
from ecom_ops.integrations.mail import MailClient, MailMessage, client_from_env
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import SecurityError, validate_site
from ecom_ops.telemetry import Telemetry, default_telemetry


@dataclass(frozen=True)
class MailSendResult:
    ok: bool
    message: str
    to: list[str] | None = None
    subject: str | None = None
    provider_status: dict[str, Any] | None = None
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "to": self.to,
            "subject": self.subject,
            "provider_status": self.provider_status,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
        }


@dataclass(frozen=True)
class MailFetchResult:
    ok: bool
    message: str
    messages: list[dict[str, Any]]
    count: int
    escalated: bool = False
    ticket_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "messages": self.messages,
            "count": self.count,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
        }


class MailService:
    def __init__(
        self,
        client: MailClient | None = None,
        *,
        telemetry: Telemetry | None = None,
        escalation: EscalationService | None = None,
    ) -> None:
        self.client = client or client_from_env(use_mock=None)
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation

    def send(
        self,
        *,
        to: str | list[str],
        subject: str,
        body: str,
        cc: str | list[str] | None = None,
        html_body: str | None = None,
        site: str = "azom",
        actor: Actor | str | None = None,
        required_permission: Permission | None = None,
    ) -> MailSendResult:
        site = validate_site(site)
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        try:
            require_permission(
                actor_obj, required_permission or Permission.MAIL_SEND
            )
            status = self.client.send(
                to=to,
                subject=subject,
                body=body,
                cc=cc,
                html_body=html_body,
            )
            to_list = status.get("to") or (
                [to] if isinstance(to, str) else list(to)
            )
            self.telemetry.record(
                action="mail_send",
                site=site,
                unit_type="emails",
                meta={
                    "to": to_list,
                    "subject": subject[:120],
                    "actor": actor_obj.name,
                },
            )
            return MailSendResult(
                ok=True,
                message="Email sent",
                to=list(to_list) if isinstance(to_list, list) else [str(to_list)],
                subject=subject,
                provider_status=status,
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"Mail send denied for {actor_obj.name}",
                details={"error": str(exc), "site": site, "subject": subject[:120]},
            )
            return MailSendResult(
                ok=False,
                message=str(exc),
                escalated=True,
                ticket_id=ticket.id,
            )
        except SecurityError as exc:
            return MailSendResult(ok=False, message=str(exc))
        except Exception as exc:
            ticket = self.escalation.escalate_critical(
                "Mail send failed",
                details={"error": str(exc), "site": site, "subject": subject[:120]},
            )
            return MailSendResult(
                ok=False,
                message=f"Failed: {exc}",
                escalated=True,
                ticket_id=ticket.id,
            )

    def fetch(
        self,
        *,
        folder: str = "INBOX",
        unread_only: bool = True,
        limit: int = 20,
        site: str = "azom",
        actor: Actor | str | None = None,
    ) -> MailFetchResult:
        site = validate_site(site)
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        try:
            require_permission(actor_obj, Permission.MAIL_READ)
            if unread_only:
                msgs = self.client.fetch_unread(folder=folder, limit=limit)
            else:
                msgs = self.client.fetch(
                    folder=folder, unread_only=False, limit=limit
                )
            payload = [m.to_dict() for m in msgs]
            self.telemetry.record(
                action="mail_fetch",
                site=site,
                unit_type="emails",
                units=float(len(payload)),
                meta={
                    "folder": folder,
                    "unread_only": unread_only,
                    "count": len(payload),
                    "actor": actor_obj.name,
                },
            )
            return MailFetchResult(
                ok=True,
                message=f"Fetched {len(payload)} message(s)",
                messages=payload,
                count=len(payload),
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"Mail fetch denied for {actor_obj.name}",
                details={"error": str(exc), "site": site},
            )
            return MailFetchResult(
                ok=False,
                message=str(exc),
                messages=[],
                count=0,
                escalated=True,
                ticket_id=ticket.id,
            )
        except SecurityError as exc:
            return MailFetchResult(
                ok=False, message=str(exc), messages=[], count=0
            )
        except Exception as exc:
            ticket = self.escalation.escalate_critical(
                "Mail fetch failed",
                details={"error": str(exc), "site": site},
            )
            return MailFetchResult(
                ok=False,
                message=f"Failed: {exc}",
                messages=[],
                count=0,
                escalated=True,
                ticket_id=ticket.id,
            )

    def reply(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        original_uid: str | None = None,
        html_body: str | None = None,
        site: str = "azom",
        actor: Actor | str | None = None,
    ) -> MailSendResult:
        """Reply path: send to original sender (RBAC = MAIL_SEND)."""
        # If we have a full original message in memory path, prefer client.reply;
        # for CLI we reconstruct a minimal original.
        original = MailMessage(
            subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
            body="",
            from_addr=to,
            uid=original_uid,
        )
        site = validate_site(site)
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)
        try:
            require_permission(actor_obj, Permission.MAIL_SEND)
            # Use client.reply when subject does not already include Re:
            if original_uid:
                # Fetch isn't free; send with Re: subject for pilot CLI path
                subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                status = self.client.send(
                    to=to, subject=subj, body=body, html_body=html_body
                )
            else:
                status = self.client.reply(original, body=body, html_body=html_body)
            self.telemetry.record(
                action="mail_reply",
                site=site,
                unit_type="emails",
                meta={
                    "to": to,
                    "subject": subject[:120],
                    "actor": actor_obj.name,
                    "original_uid": original_uid,
                },
            )
            return MailSendResult(
                ok=True,
                message="Reply sent",
                to=[to],
                subject=status.get("subject") or subject,
                provider_status=status,
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"Mail reply denied for {actor_obj.name}",
                details={"error": str(exc), "site": site},
            )
            return MailSendResult(
                ok=False,
                message=str(exc),
                escalated=True,
                ticket_id=ticket.id,
            )
        except SecurityError as exc:
            return MailSendResult(ok=False, message=str(exc))
        except Exception as exc:
            ticket = self.escalation.escalate_critical(
                "Mail reply failed",
                details={"error": str(exc), "site": site},
            )
            return MailSendResult(
                ok=False,
                message=f"Failed: {exc}",
                escalated=True,
                ticket_id=ticket.id,
            )


def send_mail(
    *,
    to: str | list[str],
    subject: str,
    body: str,
    site: str = "azom",
    actor: str | None = None,
) -> MailSendResult:
    return MailService().send(
        to=to, subject=subject, body=body, site=site, actor=actor
    )


def fetch_mail(
    *,
    folder: str = "INBOX",
    unread_only: bool = True,
    limit: int = 20,
    site: str = "azom",
    actor: str | None = None,
) -> MailFetchResult:
    return MailService().fetch(
        folder=folder,
        unread_only=unread_only,
        limit=limit,
        site=site,
        actor=actor,
    )
