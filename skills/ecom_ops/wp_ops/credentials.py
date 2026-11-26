"""Per-site credential references.

The value stays in the process environment under that site's own
``WP_SECRET_*`` name. Callers never receive another site's value, and the
shared content-client variables are not a fallback.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ecom_ops.rbac import AccessDenied
from ecom_ops.wp_ops.models import Site

_URI_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s]+:)[^@/\s]+(@)")
_AUTH_HEADER_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_TOKEN_LITERAL_RE = re.compile(
    r"(?:"
    r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9_-]{8,}\b|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}\b|"
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{8,}\b|"
    r"\bxai-[A-Za-z0-9_-]{8,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|"
    r"\bAIza[0-9A-Za-z_-]{16,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)
_CRED_LINE_RE = re.compile(
    r"(?im)^([^\r\n]*?(?:db_password|db_pass|db_user|secret|password|passwd|passphrase|"
    r"token|api[_-]?key|private[_-]?key|auth(?:orization)?[_-]?(?:key|token|secret|salt)?|"
    r"logged_in_(?:key|salt)|nonce_(?:key|salt)|secure_auth_(?:key|salt)|"
    r"consumer[_-]?(?:key|secret)|access[_-]?key|signing[_-]?key|client[_-]?secret|"
    r"webhook[_-]?secret|encryption[_-]?key)[^\r\n]*?(?:=>|=|:|,))(.+)$"
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(\b(?:[A-Za-z0-9_]*(?:secret|password|passwd|passphrase|token|api[_-]?key|"
    r"private[_-]?key|consumer[_-]?(?:key|secret)|client[_-]?secret|access[_-]?key|"
    r"signing[_-]?key|webhook[_-]?secret|auth[_-]?(?:key|salt|token))[A-Za-z0-9_]*)"
    r"\s*(?:=|=>|:)\s*)(['\"])(?:\\.|(?!\2).)*\2"
)
_SECRET_KEY_NAME = (
    r"(?:[A-Za-z0-9_]*(?:"
    r"secret|password|passwd|passphrase|api[_-]?key|private[_-]?key|"
    r"consumer[_-]?(?:key|secret)|client[_-]?(?:secret|key)|"
    r"access[_-]?(?:key|token)|signing[_-]?key|webhook[_-]?(?:secret|token)|"
    r"auth[_-]?(?:key|token|secret|salt)|logged_in_(?:key|salt)|"
    r"secure_auth_(?:key|salt)|nonce_(?:key|salt)|"
    r"db_pass(?:word)?|dsn|database_url|bearer[_-]?token|refresh[_-]?token|bot[_-]?token"
    r")|token|authorization)"
)
_REDACT_SECRET_LINE_RE = re.compile(
    r"(?im)^.*(?:"
    r"[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s]+:[^@/\s]+@|"
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{6,}|"
    r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9_-]{8,}\b|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}\b|"
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{8,}\b|"
    r"\bxai-[A-Za-z0-9_-]{8,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|"
    r"\bAIza[0-9A-Za-z_-]{16,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"define\s*\(\s*['\"]"
    + _SECRET_KEY_NAME
    + r"['\"]\s*,\s*['\"][^'\"\r\n]{1,}['\"]|"
    r"(?:^|[\s$'\"(,{\[])"
    + _SECRET_KEY_NAME
    + r"['\"]?\s*(?:=|=>|:)\s*"
    r"(?!true\b|false\b|null\b|none\b|undefined\b|''|\"\")"
    r"(?:['\"][^'\"\r\n]{3,}['\"]|[^\s'\",;}\]]{6,})"
    r").*$"
)


@dataclass
class BoundCredential:
    ref: str
    present: bool
    value: str = field(default="", repr=False, compare=False)

    def __str__(self) -> str:
        return f"BoundCredential(ref={self.ref}, present={self.present})"


def bind_credential(site: Site, ref: str | None = None) -> BoundCredential:
    """Bind ``site`` to its own secret ref. A different ref is denied first."""
    wanted = site.secret_ref if ref is None else ref
    if wanted != site.secret_ref:
        raise AccessDenied("secret ref does not belong to this site")
    raw = os.environ.get(site.secret_ref)
    present = bool(raw and str(raw).strip())
    return BoundCredential(ref=site.secret_ref, present=present, value=raw if present else "")


def scrub_secrets(payload: object, secrets: list[str]) -> object:
    """Replace bound secret values that leaked into a nested structure."""
    blocked = [item for item in secrets if item and len(item) >= 8]

    def _one(value: object) -> object:
        if isinstance(value, str):
            out = value
            for secret in blocked:
                if secret in out:
                    out = out.replace(secret, "***")
            return out
        if isinstance(value, dict):
            return {str(key): _one(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_one(item) for item in value]
        return value

    return _one(payload)


def redact_secret_text(
    text: str,
    extra_secrets: list[str] | None = None,
    *,
    redact_whole_lines: bool = False,
    line_placeholder: str = "[credential-bearing source line redacted]",
) -> str:
    """Redact credentials, tokens, URI passwords, and auth headers from free text."""
    raw = str(text or "")
    if extra_secrets:
        cleaned = scrub_secrets(raw, extra_secrets)
        out = cleaned if isinstance(cleaned, str) else raw
    else:
        out = raw
    if redact_whole_lines:
        return _REDACT_SECRET_LINE_RE.sub(line_placeholder, out)
    out = _URI_CRED_RE.sub(r"\1***\2", out)
    out = _AUTH_HEADER_RE.sub(r"\1 [REDACTED]", out)
    out = _TOKEN_LITERAL_RE.sub("[REDACTED]", out)
    out = _CRED_LINE_RE.sub(r"\1 [REDACTED]", out)
    return out


def scrub_compact(
    text: str,
    *,
    max_len: int = 160,
    extra_secrets: list[str] | None = None,
) -> str:
    """Collapse whitespace, redact secret tokens/assignments/headers/URIs, and bound length."""
    collapsed = " ".join(str(text or "").split())
    if extra_secrets:
        scrubbed = scrub_secrets(collapsed, extra_secrets)
        collapsed = scrubbed if isinstance(scrubbed, str) else collapsed
    cleaned = _URI_CRED_RE.sub(r"\1***\2", collapsed)
    cleaned = _AUTH_HEADER_RE.sub(r"\1 ***", cleaned)
    cleaned = _SECRET_ASSIGN_RE.sub(r"\1'***'", cleaned)
    cleaned = _TOKEN_LITERAL_RE.sub("***", cleaned)
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3] + "..."
    return cleaned
