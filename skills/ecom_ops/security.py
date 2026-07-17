"""Security helpers: input validation, secret hygiene, safe paths."""

from __future__ import annotations

import os
import re
from typing import Any

# WooCommerce order statuses (subset used in V1 pilot)
ALLOWED_ORDER_STATUSES = frozenset(
    {
        "pending",
        "processing",
        "on-hold",
        "completed",
        "cancelled",
        "refunded",
        "failed",
        "trash",
    }
)

# SSH: only non-destructive read/health commands in auto mode
SSH_ALLOWLIST = frozenset(
    {
        "uptime",
        "df -h",
        "free -m",
        "whoami",
        "hostname",
        "uname -a",
        "systemctl is-active nginx",
        "systemctl is-active php-fpm",
        "systemctl is-active mysql",
        "systemctl is-active mariadb",
        "docker ps",
        "nginx -t",
    }
)

# Patterns that always escalate (critical / code edit)
CRITICAL_SSH_PATTERNS = (
    re.compile(r"\brm\s+(-[a-zA-Z]*f|-[a-zA-Z]*r)", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r">\s*/dev/", re.I),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b", re.I),
    re.compile(r"\buserdel\b|\bpasswd\b|\bchmod\s+777\b", re.I),
    re.compile(r"\bDROP\s+TABLE\b|\bDROP\s+DATABASE\b", re.I),
    re.compile(r"\bgit\s+push\b|\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bsed\s+-i\b|\btee\b.*>|\bnano\b|\bvim\b|\bvi\s+", re.I),
)

SECRET_ENV_KEYS = (
    "WOO_CONSUMER_KEY",
    "WOO_CONSUMER_SECRET",
    "WP_APP_PASSWORD",
    "WOO_WEBHOOK_SECRET",
    "SSH_PRIVATE_KEY",
    "SSH_PASSWORD",
    "OPENROUTER_API_KEY",
    "MAILCOW_API_KEY",
    "SMTP_PASSWORD",
    "MAIL_PASSWORD",
    "MAIL_OAUTH_ACCESS_TOKEN",
    "MAIL_OAUTH_REFRESH_TOKEN",
    "MAIL_OAUTH_CLIENT_SECRET",
    "GRAPH_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_PASSWORD_HASH",
    "DASHBOARD_OSCAR_PASSWORD",
    "DASHBOARD_OSCAR_PASSWORD_HASH",
    "DASHBOARD_SECRET_KEY",
)

_ORDER_ID_RE = re.compile(r"^\d{1,12}$")
_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.I)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SecurityError(ValueError):
    """Raised when a security validation fails."""


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SecurityError(f"Missing required secret/env: {name}")
    return value


def get_env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def validate_order_id(order_id: str | int) -> str:
    oid = str(order_id).strip()
    if not _ORDER_ID_RE.match(oid):
        raise SecurityError(f"Invalid order id: {order_id!r}")
    return oid


def validate_order_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized not in ALLOWED_ORDER_STATUSES:
        raise SecurityError(
            f"Invalid order status {status!r}. Allowed: {sorted(ALLOWED_ORDER_STATUSES)}"
        )
    return normalized


def validate_site(site: str) -> str:
    s = site.strip().lower()
    if not _SITE_RE.match(s):
        raise SecurityError(f"Invalid site id: {site!r}")
    return s


def validate_email(email: str) -> str:
    e = email.strip()
    if not _EMAIL_RE.match(e) or len(e) > 254:
        raise SecurityError(f"Invalid email: {email!r}")
    return e


def sanitize_text(text: str, *, max_len: int = 8000) -> str:
    if text is None:
        raise SecurityError("Text must not be None")
    cleaned = text.replace("\x00", "").strip()
    if not cleaned:
        raise SecurityError("Text must not be empty")
    if len(cleaned) > max_len:
        raise SecurityError(f"Text exceeds max length ({max_len})")
    return cleaned


def redact_secrets(payload: Any) -> Any:
    """Recursively redact known secret keys from logs/telemetry.

    Matches both substring patterns (SECRET/PASSWORD/TOKEN/API_KEY/PRIVATE_KEY)
    and the explicit ``SECRET_ENV_KEYS`` allowlist (covers keys like
    ``WOO_CONSUMER_KEY`` that lack the substring markers).
    """
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            key_upper = str(k).upper()
            if any(s in key_upper for s in ("SECRET", "PASSWORD", "TOKEN", "API_KEY", "PRIVATE_KEY")):
                out[k] = "***REDACTED***"
            elif str(k) in SECRET_ENV_KEYS:
                out[k] = "***REDACTED***"
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(payload, list):
        return [redact_secrets(x) for x in payload]
    return payload


def is_ssh_allowlisted(command: str) -> bool:
    cmd = " ".join(command.strip().split())
    return cmd in SSH_ALLOWLIST


def is_critical_ssh_command(command: str) -> bool:
    if is_ssh_allowlisted(command):
        return False
    for pattern in CRITICAL_SSH_PATTERNS:
        if pattern.search(command):
            return True
    # Non-allowlisted write-ish commands are treated as critical in V1
    return not is_ssh_allowlisted(command)


def assert_no_path_traversal(path: str) -> str:
    if ".." in path.replace("\\", "/").split("/"):
        raise SecurityError(f"Path traversal blocked: {path!r}")
    if path.startswith("~") or os.path.isabs(path):
        # Absolute paths allowed only if under controlled workspace via env
        workspace = os.environ.get("AZOM_WORKSPACE", "")
        if workspace and os.path.commonpath(
            [os.path.realpath(path), os.path.realpath(workspace)]
        ) == os.path.realpath(workspace):
            return path
        raise SecurityError(f"Absolute path not allowed: {path!r}")
    return path
