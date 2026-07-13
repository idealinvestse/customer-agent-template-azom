"""Propose / confirm write actions for Telegram (order status, product desc).

Never silent site mutation — always human confirm path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ecom_ops.security import ALLOWED_ORDER_STATUSES, validate_order_id, validate_order_status

# Common Swedish/English status phrases → Woo status
_STATUS_ALIASES: dict[str, str] = {
    "completed": "completed",
    "complete": "completed",
    "klar": "completed",
    "färdig": "completed",
    "fardig": "completed",
    "levererad": "completed",
    "processing": "processing",
    "behandles": "processing",
    "behandlas": "processing",
    "under behandling": "processing",
    "on-hold": "on-hold",
    "on hold": "on-hold",
    "pausad": "on-hold",
    "väntar": "on-hold",
    "pending": "pending",
    "väntande": "pending",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "avbruten": "cancelled",
    "makulerad": "cancelled",
    "refunded": "refunded",
    "återbetald": "refunded",
    "failed": "failed",
    "misslyckad": "failed",
}

_ORDER_UPDATE_RE = re.compile(
    r"\b("
    r"sätt|satt|ändra|uppdatera|byt|markera|set|update|change|"
    r"flytta\s+till|gör\s+om\s+till"
    r")\b"
    r".{0,40}?"
    r"(?:order|ordernr|#)?\s*(\d{4,12})?"
    r".{0,40}?"
    r"\b("
    + "|".join(re.escape(k) for k in sorted(_STATUS_ALIASES, key=len, reverse=True))
    + r"|"
    + "|".join(re.escape(s) for s in ALLOWED_ORDER_STATUSES)
    + r")\b",
    re.I,
)

_ORDER_UPDATE_RE2 = re.compile(
    r"\b(?:order|ordernr|#)\s*(\d{4,12})\b"
    r".{0,30}?"
    r"\b(?:till|to|som|→|->)\s*"
    r"("
    + "|".join(re.escape(k) for k in sorted(_STATUS_ALIASES, key=len, reverse=True))
    + r"|"
    + "|".join(re.escape(s) for s in ALLOWED_ORDER_STATUSES)
    + r")\b",
    re.I,
)

_PRODUCT_DESC_RE = re.compile(
    r"\b("
    r"produktbeskrivning|product\s*desc(?:ription)?|skriv\s+beskrivning|"
    r"generera\s+(?:en\s+)?(?:produkt)?beskrivning|description\s+for"
    r")\b"
    r".{0,60}?"
    r"(?:produkt|product|#)?\s*(\d{1,12})\b",
    re.I,
)

_REGEN_NL_RE = re.compile(
    r"\b(regenerera|regen|skriv\s+om|nya?\s+utkast|redraft)\b"
    r".{0,24}\b([0-9a-f]{8})\b"
    r"|\b([0-9a-f]{8})\b.{0,16}\b(regenerera|regen)\b",
    re.I,
)

_PRONOUN_ORDER_RE = re.compile(
    r"\b("
    r"samma\s+order|den\s+ordern|denna\s+order|ordern|"
    r"den\s+här\s+ordern|det\s+ordernumret|"
    r"the\s+order|that\s+order|same\s+order|"
    r"frakt(?:en)?|tracking|leverans(?:en)?|status(?:en)?\s+då|"
    r"och\s+frakt|varför|hur\s+länge|när\s+kommer"
    r")\b",
    re.I,
)

_PRONOUN_CASE_RE = re.compile(
    r"\b("
    r"samma\s+ärende|det\s+ärendet|detta\s+ärende|"
    r"draft(?:en)?|utkast(?:et)?|svaret"
    r")\b",
    re.I,
)


@dataclass
class PendingAction:
    kind: str  # order_status | product_desc | case_regenerate
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": dict(self.payload)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> PendingAction | None:
        if not raw or not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        if not kind:
            return None
        return cls(kind=kind, payload=dict(payload))


def _resolve_status_token(token: str) -> str | None:
    t = (token or "").strip().lower().replace("_", "-")
    if t in ALLOWED_ORDER_STATUSES:
        return t
    mapped = _STATUS_ALIASES.get(t) or _STATUS_ALIASES.get(t.replace("-", " "))
    if mapped:
        return mapped
    return None


def parse_order_status_intent(
    text: str, *, fallback_order_id: str | None = None
) -> dict[str, str] | None:
    """Return {order_id, status} if user wants a Woo status change."""
    raw = text or ""
    for rx in (_ORDER_UPDATE_RE2, _ORDER_UPDATE_RE):
        m = rx.search(raw)
        if not m:
            continue
        groups = [g for g in m.groups() if g]
        order_id = None
        status_tok = None
        for g in groups:
            if g and g.isdigit() and len(g) >= 4:
                order_id = g
            else:
                status_tok = g
        if not order_id:
            order_id = fallback_order_id
        if not order_id or not status_tok:
            continue
        status = _resolve_status_token(status_tok)
        if not status:
            continue
        try:
            oid = validate_order_id(order_id)
            st = validate_order_status(status)
            return {"order_id": oid, "status": st}
        except Exception:
            continue
    return None


def parse_product_desc_intent(text: str) -> dict[str, str] | None:
    raw = text or ""
    m = _PRODUCT_DESC_RE.search(raw)
    if m and m.group(2):
        try:
            return {"product_id": validate_order_id(m.group(2)), "language": "sv"}
        except Exception:
            pass
    # Soft match: product-desc keywords + any number
    if re.search(
        r"\b(produktbeskrivning|product\s*desc|beskrivning\s+för)\b",
        raw,
        re.I,
    ):
        num = re.search(r"\b(\d{1,12})\b", raw)
        if num:
            try:
                return {
                    "product_id": validate_order_id(num.group(1)),
                    "language": "sv",
                }
            except Exception:
                return None
        return {"product_id": "", "language": "sv"}
    return None


def parse_regenerate_nl(text: str) -> str | None:
    m = _REGEN_NL_RE.search(text or "")
    if not m:
        return None
    return m.group(2) or m.group(3)


def wants_order_followup(text: str) -> bool:
    return bool(_PRONOUN_ORDER_RE.search(text or ""))


def wants_case_followup(text: str) -> bool:
    return bool(_PRONOUN_CASE_RE.search(text or ""))


def execute_order_status(
    *,
    order_id: str,
    status: str,
    actor: str,
) -> tuple[bool, str]:
    from ecom_ops.actions.order_status import OrderStatusService

    result = OrderStatusService().update(
        order_id=order_id, status=status, actor=actor, note="telegram confirm"
    )
    if result.ok:
        return True, result.message
    return False, result.message


def execute_product_desc(
    *,
    product_id: str,
    language: str = "sv",
    publish: bool = False,
    actor: str,
) -> tuple[bool, str]:
    from ecom_ops.actions.product_desc import ProductDescService

    result = ProductDescService().generate(
        product_id=product_id or None,
        language=language or "sv",
        publish=publish,
        actor=actor,
    )
    if not result.ok:
        return False, result.message
    short = (result.short_description or "")[:280]
    msg = result.message
    if short:
        msg = f"{msg}\n\n{short}"
    if result.published:
        msg += "\n(publicerad i Woo)"
    elif product_id:
        msg += "\n(inte publicerad — bekräfta publish separat via CLI om du vill)"
    return True, msg


def execute_case_regenerate(*, case_id: str, actor: str) -> tuple[bool, str]:
    from ecom_ops.cases.service import CaseService

    result = CaseService().regenerate_draft(case_id, actor=actor)
    if result.ok:
        draft = ""
        if result.case:
            draft = (result.case.get("draft_reply") or "")[:400]
        return True, f"{result.message}\n\nDraft:\n{draft or '(tom)'}"
    return False, result.message
