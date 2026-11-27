"""Nordic customer PII detection for WordPress workspace drafts and fixtures.

Leaf module with no internal package imports so both ``review_plan`` and
``agent_tools`` can enforce zero-PII rules without circular imports.
"""

from __future__ import annotations

import html as _html_mod
import re
import urllib.parse
from typing import Any

_EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
_ALLOWED_SYNTHETIC_EMAIL_SUFFIXES = (
    ".invalid",
    ".example",
    "@example.com",
    "@example.org",
    "@example.net",
)
_NORDIC_PHONE_RE = re.compile(
    r"(?:(?<!\d)(?:\+|00)(?:46|47|45)[\s.-]?(?:\(0\)[\s.-]?)?(?:\d[\s.-]?){7,10}\b|"
    r"(?<!\d)07[02369][\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d))"
)
_SE_PERSONNUMMER_RE = re.compile(
    r"(?<!\d)(?:19|20)?\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[-+]\d{4}(?!\d)"
)
_NO_FODSELSNUMMER_RE = re.compile(
    r"(?<!\d)(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2])\d{2}\s?\d{5}(?!\d)"
)
_ZERO_WIDTH_RE = re.compile(r"[\u00ad\u034f\u200b-\u200f\u2060\ufeff]")


def normalize_pii_text(raw: str) -> str:
    """Decode iterative URL/HTML encoding and strip zero-width characters."""
    text = str(raw or "")
    for _ in range(4):
        nxt = _html_mod.unescape(urllib.parse.unquote(text))
        if nxt == text:
            break
        text = nxt
    return _ZERO_WIDTH_RE.sub("", text)


def contains_real_pii(value: Any) -> bool:
    """Detect real customer PII (non-synthetic emails, Nordic phones, national IDs) in keys & values."""
    if isinstance(value, str):
        candidates = (value, normalize_pii_text(value))
        for cand in candidates:
            for match in _EMAIL_IN_TEXT_RE.finditer(cand):
                addr = match.group(0).strip().lower()
                if not any(addr.endswith(suffix) for suffix in _ALLOWED_SYNTHETIC_EMAIL_SUFFIXES):
                    return True
            if _NORDIC_PHONE_RE.search(cand):
                return True
            if _SE_PERSONNUMMER_RE.search(cand):
                return True
            if _NO_FODSELSNUMMER_RE.search(cand):
                return True
        return False
    if isinstance(value, dict):
        return any(contains_real_pii(k) or contains_real_pii(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_real_pii(v) for v in value)
    return False
