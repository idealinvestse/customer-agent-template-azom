"""Shared runtime env overlay bootstrap.

Loads ``AZOM_DATA_DIR/runtime.env`` + ``secrets.env`` into ``os.environ``
once per process so CLI, poll, bot, and dashboard share the same secrets.

Security-critical posture keys are never taken from overlay files — systemd
/ process env wins. That keeps live fail-closed allowlists, mock mode, and
kill-switches from flipping via a dashboard-written file.
"""

from __future__ import annotations

import os
from pathlib import Path

# Never applied from overlay (live fail-closed / HITL posture).
PROTECTED_OVERLAY_KEYS = frozenset(
    {
        "AZOM_USE_MOCK",
        "AZOM_NULL_SEND",
        "AZOM_AUTO_SEND_KILL",
        "AZOM_ADS_MUTATE_KILL",
        "AZOM_GA_MUTATE_KILL",
        "AZOM_MP_KILL",
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ACTOR_MAP",
        "MESSENGER_ALLOWED_PSIDS",
        "MESSENGER_ACTOR_MAP",
        "DASHBOARD_SECRET_KEY",
    }
)

# Integration secrets / non-posture runtime that Oscar may store in secrets.env.
_ALLOW_PREFIXES = (
    "MAIL_",
    "WOO_",
    "WP_",
    "GRAPH_",
    "SSH_",
    "OPENROUTER_",
    "GMAIL_",
    "GOOGLE_",
    "GA4_",
    "AZOM_GA4_",
    "AZOM_GADS_",
    "SMTP_",
    "IMAP_",
)

_ALLOW_KEYS = frozenset(
    {
        "MAIL_PROVIDER",
        "AZOM_DASHBOARD_PUBLIC_URL",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
        "TELEGRAM_BOT_TOKEN",
        "MESSENGER_PAGE_ACCESS_TOKEN",
        "MESSENGER_APP_SECRET",
        "MESSENGER_VERIFY_TOKEN",
        "WOO_WEBHOOK_SECRET",
        "METRICS_SCRAPE_TOKEN",
    }
)

_bootstrapped = False


def env_flag(name: str, default: bool = False) -> bool:
    """Parse a truthy env flag. Accepts 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def overlay_key_allowed(key: str) -> bool:
    """Return True if ``key`` may be applied from an overlay file."""
    if not key or key in PROTECTED_OVERLAY_KEYS:
        return False
    if key in _ALLOW_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in _ALLOW_PREFIXES)


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def apply_overlays(*, data_dir: Path | None = None) -> list[str]:
    """Apply allowlisted overlay keys. Returns keys that were set."""
    base = data_dir or Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    applied: list[str] = []
    merged: dict[str, str] = {}
    for name in ("runtime.env", "secrets.env"):
        merged.update(_parse_env_file(base / name))
    for key, val in merged.items():
        if not overlay_key_allowed(key):
            continue
        os.environ[key] = val
        applied.append(key)
    return applied


def bootstrap_runtime(*, data_dir: Path | None = None, force: bool = False) -> list[str]:
    """Apply overlays once per process. ``force`` is for tests."""
    global _bootstrapped
    if _bootstrapped and not force:
        return []
    applied = apply_overlays(data_dir=data_dir)
    _bootstrapped = True
    return applied


def reset_bootstrap_for_tests() -> None:
    """Reset the once-per-process guard (tests only)."""
    global _bootstrapped
    _bootstrapped = False
