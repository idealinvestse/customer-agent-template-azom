"""Customer support automation (classify, draft reply, escalate critical)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ecom_ops.escalation import EscalationReason, EscalationService, Severity, default_escalation
from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.security import SecurityError, sanitize_text, validate_email, validate_site
from ecom_ops.telemetry import Telemetry, default_telemetry


class SupportCategory(str, Enum):
    ORDER_STATUS = "order_status"
    SHIPPING = "shipping"
    PRODUCT = "product"
    RETURN = "return"
    BILLING = "billing"
    TECHNICAL = "technical"
    ABUSE = "abuse"
    OTHER = "other"


CRITICAL_KEYWORDS = (
    "legal",
    "lawyer",
    "advokat",
    "gdpr complaint",
    "datainspektionen",
    "chargeback",
    "police",
    "threat",
    "suicide",
    "bomb",
    "hacked",
    "ransomware",
)

ORDER_RE = re.compile(r"\b(?:order|orderid|ordernr|ordernummer|#)\s*[:#]?\s*(\d{4,12})\b", re.I)


@dataclass(frozen=True)
class SupportResult:
    ok: bool
    category: SupportCategory
    reply: str | None
    order_id: str | None
    escalated: bool
    ticket_id: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "category": self.category.value,
            "reply": self.reply,
            "order_id": self.order_id,
            "escalated": self.escalated,
            "ticket_id": self.ticket_id,
            "message": self.message,
        }


def classify_message(text: str) -> SupportCategory:
    t = text.lower()
    if any(k in t for k in CRITICAL_KEYWORDS):
        return SupportCategory.ABUSE
    if any(k in t for k in ("return", "refund", "retur", "återbetal", "reklamation")):
        return SupportCategory.RETURN
    if any(k in t for k in ("ship", "leverans", "frakt", "tracking", "spårning")):
        return SupportCategory.SHIPPING
    if any(k in t for k in ("invoice", "faktura", "payment", "betalning", "billing")):
        return SupportCategory.BILLING
    if any(k in t for k in ("order", "status", "where is my", "var är min")):
        return SupportCategory.ORDER_STATUS
    if any(k in t for k in ("broken", "bug", "error", "fungerar inte", "login")):
        return SupportCategory.TECHNICAL
    if any(k in t for k in ("product", "produkt", "size", "storlek", "spec")):
        return SupportCategory.PRODUCT
    return SupportCategory.OTHER


def extract_order_id(text: str) -> str | None:
    m = ORDER_RE.search(text)
    return m.group(1) if m else None


def draft_reply(
    *,
    category: SupportCategory,
    customer_name: str | None,
    order_id: str | None,
    language: str = "sv",
) -> str:
    name = customer_name or "du"
    lang = language.lower()
    oid = order_id or "ditt ordernummer"

    if lang in {"no", "nb"}:
        greeting = f"Hei {name},"
        sign = "Vennlig hilsen\nAzom Support"
    elif lang in {"da", "dk"}:
        greeting = f"Hej {name},"
        sign = "Venlig hilsen\nAzom Support"
    elif lang in {"en"}:
        greeting = f"Hi {name},"
        sign = "Best regards\nAzom Support"
    else:
        greeting = f"Hej {name},"
        sign = "Vänliga hälsningar\nAzom Support"

    bodies = {
        SupportCategory.ORDER_STATUS: (
            f"Tack för ditt meddelande. Vi tittar på order {oid} och återkommer "
            f"så snart status är bekräftad."
            if lang not in {"en"}
            else f"Thanks for reaching out. We are checking order {oid} and will update you shortly."
        ),
        SupportCategory.SHIPPING: (
            "Tack! Vi kontrollerar leveransstatus och spårningsinformation åt dig."
            if lang not in {"en"}
            else "Thanks! We are checking shipping and tracking details for you."
        ),
        SupportCategory.RETURN: (
            "Vi hjälper dig med retur/reklamation. Bifoga gärna ordernummer och foton om det gäller skada."
            if lang not in {"en"}
            else "We can help with returns. Please include order number and photos if damaged."
        ),
        SupportCategory.BILLING: (
            "Vi tar en titt på faktura/betalning och återkommer med bekräftelse."
            if lang not in {"en"}
            else "We will review the invoice/payment and confirm shortly."
        ),
        SupportCategory.PRODUCT: (
            "Tack för din produktfråga. Vi återkommer med korrekt information."
            if lang not in {"en"}
            else "Thanks for your product question. We will follow up with accurate details."
        ),
        SupportCategory.TECHNICAL: (
            "Vi felsöker gärna. Beskriv gärna steg, enhet och eventuella felmeddelanden."
            if lang not in {"en"}
            else "Happy to troubleshoot. Please share steps, device, and any error messages."
        ),
        SupportCategory.OTHER: (
            "Tack för att du kontaktade oss. Vi återkommer så snart vi kan."
            if lang not in {"en"}
            else "Thanks for contacting us. We will get back to you soon."
        ),
        SupportCategory.ABUSE: (
            "Ditt ärende har eskalerats till ansvarig personal."
            if lang not in {"en"}
            else "Your case has been escalated to a human specialist."
        ),
    }
    body = bodies.get(category, bodies[SupportCategory.OTHER])
    return f"{greeting}\n\n{body}\n\n{sign}"


class SupportService:
    def __init__(
        self,
        *,
        telemetry: Telemetry | None = None,
        escalation: EscalationService | None = None,
    ) -> None:
        self.telemetry = telemetry or default_telemetry
        self.escalation = escalation or default_escalation

    def handle(
        self,
        message: str,
        *,
        customer_email: str | None = None,
        customer_name: str | None = None,
        language: str = "sv",
        site: str = "azom",
        actor: Actor | str | None = None,
    ) -> SupportResult:
        site = validate_site(site)
        actor_obj = actor if isinstance(actor, Actor) else resolve_actor(actor)

        try:
            require_permission(actor_obj, Permission.SUPPORT_REPLY)
            text = sanitize_text(message, max_len=10000)
            if customer_email:
                customer_email = validate_email(customer_email)

            category = classify_message(text)
            order_id = extract_order_id(text)

            if category == SupportCategory.ABUSE:
                ticket = self.escalation.escalate(
                    reason=EscalationReason.CRITICAL,
                    summary="Critical support message requires human review",
                    details={
                        "site": site,
                        "customer_email": customer_email,
                        "category": category.value,
                        "excerpt": text[:300],
                    },
                    severity=Severity.CRITICAL,
                )
                reply = draft_reply(
                    category=category,
                    customer_name=customer_name,
                    order_id=order_id,
                    language=language,
                )
                self.telemetry.record(
                    action="support_escalated",
                    site=site,
                    meta={"category": category.value, "ticket_id": ticket.id},
                )
                return SupportResult(
                    ok=True,
                    category=category,
                    reply=reply,
                    order_id=order_id,
                    escalated=True,
                    ticket_id=ticket.id,
                    message="Escalated to Oscar",
                )

            reply = draft_reply(
                category=category,
                customer_name=customer_name,
                order_id=order_id,
                language=language,
            )
            self.telemetry.record(
                action="support_reply",
                site=site,
                meta={
                    "category": category.value,
                    "order_id": order_id,
                    "actor": actor_obj.name,
                },
            )
            return SupportResult(
                ok=True,
                category=category,
                reply=reply,
                order_id=order_id,
                escalated=False,
                ticket_id=None,
                message="Draft reply ready",
            )
        except AccessDenied as exc:
            ticket = self.escalation.escalate_critical(
                f"Support action denied for {actor_obj.name}",
                details={"error": str(exc), "site": site},
            )
            return SupportResult(
                ok=False,
                category=SupportCategory.OTHER,
                reply=None,
                order_id=None,
                escalated=True,
                ticket_id=ticket.id,
                message=str(exc),
            )
        except SecurityError as exc:
            return SupportResult(
                ok=False,
                category=SupportCategory.OTHER,
                reply=None,
                order_id=None,
                escalated=False,
                ticket_id=None,
                message=str(exc),
            )


def handle_support_message(
    message: str,
    *,
    customer_email: str | None = None,
    customer_name: str | None = None,
    language: str = "sv",
    site: str = "azom",
    actor: str | None = None,
) -> SupportResult:
    return SupportService().handle(
        message,
        customer_email=customer_email,
        customer_name=customer_name,
        language=language,
        site=site,
        actor=actor,
    )
