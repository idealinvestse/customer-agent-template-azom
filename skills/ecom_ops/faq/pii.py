"""Lightweight PII redaction for FAQ dataset exports (ops hygiene, not GDPR)."""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
# Nordic mobiles / landlines — not ISO dates (YYYY-MM-DD).
_PHONE_RE = re.compile(
    r"(?<!\d)(?:"
    r"\+\d{1,3}[\s-]?\d{2,4}[\s-]?\d{2,4}[\s-]?\d{2,4}"
    r"|0\d{1,3}[\s-]?\d{2,3}[\s-]?\d{2,3}[\s-]?\d{2,3}"
    r")(?!\d)"
)
# Swedish personnummer-ish YYYYMMDD-XXXX or YYMMDD-XXXX
_PN_RE = re.compile(r"\b(?:19|20)?\d{6}[-+]?\d{4}\b")


def redact_pii(text: str) -> str:
    """Replace emails, phone-like runs, and personnummer patterns."""
    out = text or ""
    out = _EMAIL_RE.sub("[EMAIL]", out)
    out = _PN_RE.sub("[PERSONNUMMER]", out)
    out = _PHONE_RE.sub("[TELEFON]", out)
    return out
