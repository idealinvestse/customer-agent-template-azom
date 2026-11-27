"""Untrusted web text neutralization and random-nonce boundary framing.

Leaf module: no ``integrations.*``, ``urllib``, ``http.client``, ``requests``,
or ``subprocess`` imports.
"""

from __future__ import annotations

import os
import re
import secrets

from ecom_ops.security import SECRET_ENV_KEYS
from ecom_ops.wp_ops.credentials import redact_secret_text

_CONTROL_TAG_RE = re.compile(r"<\s*/?\s*(?:tool_call|clarify)\b[^>]*>", re.IGNORECASE)
_DELIMITER_SPOOF_RE = re.compile(r"<<<\s*(?:END_)?UNTRUSTED_[^\r\n]*?>>>", re.IGNORECASE)
_DELIMITER_BARE_RE = re.compile(r"<<<\s*(?:END_)?UNTRUSTED_", re.IGNORECASE)
_ZERO_WIDTH_RE = re.compile(r"[\u00ad\u034f\u200b-\u200f\u2060\ufeff]")
_BIDI_OVERRIDE_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
_SAFE_LABEL_RE = re.compile(r"[^A-Z0-9_]+")

_SEARCH_SECRET_ENV_KEYS = (
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "EXA_API_KEY",
    "SEARXNG_API_KEY",
    "PARALLEL_API_KEY",
)


def _active_extra_secrets(extra_secrets: list[str] | None) -> list[str]:
    out: list[str] = list(extra_secrets or [])
    for key in SECRET_ENV_KEYS + _SEARCH_SECRET_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if len(val) >= 8:
            out.append(val)
    for env_key, env_val in os.environ.items():
        if env_key.startswith("WP_SECRET_"):
            val = (env_val or "").strip()
            if len(val) >= 8:
                out.append(val)
    return out


def sanitize_untrusted_text(
    text: str,
    *,
    max_chars: int = 8000,
    extra_secrets: list[str] | None = None,
) -> str:
    """Neutralize prompt-injection control tags, bidi/zero-width chars, and redact secrets."""
    cleaned = str(text or "").replace("\x00", "")
    for _ in range(8):
        prev = cleaned
        cleaned = _ZERO_WIDTH_RE.sub("", cleaned)
        cleaned = _BIDI_OVERRIDE_RE.sub("", cleaned)
        cleaned = _CONTROL_TAG_RE.sub("", cleaned)
        cleaned = _DELIMITER_SPOOF_RE.sub("", cleaned)
        cleaned = _DELIMITER_BARE_RE.sub("", cleaned)
        if cleaned == prev:
            break
    cleaned = redact_secret_text(cleaned, extra_secrets=_active_extra_secrets(extra_secrets))
    cleaned = cleaned.strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        if max_chars <= 3:
            return cleaned[:max_chars]
        return cleaned[: max_chars - 3].rstrip() + "..."
    return cleaned


def frame_untrusted_web_data(
    text: str,
    *,
    label: str = "WEB_DATA",
    max_chars: int = 8000,
) -> str:
    """Sanitize ``text`` and wrap it in a per-call random-nonce untrusted boundary."""
    safe_label = _SAFE_LABEL_RE.sub("_", str(label or "WEB_DATA").strip().upper()).strip("_") or "WEB_DATA"
    sanitized = sanitize_untrusted_text(text, max_chars=max_chars)
    nonce = secrets.token_hex(8)
    return (
        f"<<<UNTRUSTED_{safe_label} nonce={nonce}>>>\n"
        f"{sanitized}\n"
        f"<<<END_UNTRUSTED_{safe_label} nonce={nonce}>>>"
    )


__all__ = [
    "frame_untrusted_web_data",
    "sanitize_untrusted_text",
]
