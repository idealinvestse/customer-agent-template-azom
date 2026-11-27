"""Secret-redaction wrappers for WordPress tool outputs over ``wp_ops.credentials``."""

from __future__ import annotations

import re
from typing import Any

from ecom_ops.wp_ops.credentials import bind_credential, redact_secret_text
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.tools.validation import utf8_window

_CONTROL_OR_TAG_RE = re.compile(
    r"(?i)[\x00-\x1f\x7f]|</?(?:tool_call|clarify)\b[^>]*>|<<<(?:END_)?UNTRUSTED_[^>\r\n]{0,120}>>>|<<<(?:END_)?UNTRUSTED_"
)


def site_bound_secrets(site_obj: Site | None) -> list[str]:
    """Return bound per-site secret values (empty if unset or inaccessible)."""
    if site_obj is None:
        return []
    try:
        bound = bind_credential(site_obj)
        return [bound.value] if bound.present else []
    except Exception:
        return []


def scrub_bounded(site_obj: Site | None, text: Any, *, max_len: int) -> str:
    """Redact bound secrets + credential patterns, collapse lines, and cap length."""
    raw = str(text or "")
    extra = site_bound_secrets(site_obj)
    cleaned = redact_secret_text(raw, extra)
    cleaned = redact_secret_text(cleaned, redact_whole_lines=True)
    cleaned = _CONTROL_OR_TAG_RE.sub(" ", cleaned)
    collapsed = " ".join(cleaned.splitlines()).strip()
    if len(collapsed) > max_len:
        return collapsed[: max_len - 3] + "..."
    return collapsed


def scrub_keep_newlines(site_obj: Site | None, text: Any, *, max_bytes: int) -> tuple[str, bool]:
    """Redact bound secrets + credential patterns while preserving newlines and UTF-8 byte cap."""
    raw = str(text or "")
    extra = site_bound_secrets(site_obj)
    cleaned = redact_secret_text(raw, extra)
    cleaned = redact_secret_text(cleaned, redact_whole_lines=True)
    encoded = cleaned.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return cleaned, False
    window, _ = utf8_window(encoded, 0, max_bytes)
    return window, True


def scrub_nested(value: Any, *, extra_secrets: list[str] | None = None) -> Any:
    """Recursively apply ``redact_secret_text`` to strings inside nested dicts/lists."""
    if isinstance(value, str):
        return redact_secret_text(value, extra_secrets)
    if isinstance(value, dict):
        return {k: scrub_nested(v, extra_secrets=extra_secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_nested(item, extra_secrets=extra_secrets) for item in value]
    return value


__all__ = [
    "scrub_bounded",
    "scrub_keep_newlines",
    "scrub_nested",
    "site_bound_secrets",
]
