"""Per-mailbox mail probe matrix. Never flips enabled: true."""

from __future__ import annotations

import os
from typing import Any

from ecom_ops.cases.mailboxes import MailboxConfig, load_mailboxes


def _prefix_keys(prefix: str) -> tuple[str, str, str]:
    p = prefix if prefix.endswith("_") else f"{prefix}_"
    return f"{p}USERNAME", f"{p}PASSWORD", f"{p}FROM"


def mailbox_credential_present(mb: MailboxConfig) -> bool:
    if mb.env_prefix:
        user_k, _pw_k, from_k = _prefix_keys(mb.env_prefix)
        if os.environ.get(user_k, "").strip() or os.environ.get(from_k, "").strip():
            return True
    return bool(
        os.environ.get("MAIL_USERNAME", "").strip()
        or os.environ.get("MAIL_FROM", "").strip()
    )


def probe_mailbox_matrix() -> dict[str, Any]:
    """Dry-check every configured mailbox. Disabled ones stay disabled."""
    rows: list[dict[str, Any]] = []
    for mb in load_mailboxes():
        creds = mailbox_credential_present(mb)
        if not mb.enabled:
            status = "disabled_ready" if creds else "not_configured"
            detail = (
                "enabled: false — credentials present (Oscar may flip)"
                if creds
                else "enabled: false — not configured (do not auto-enable)"
            )
        else:
            status = "enabled" if creds else "enabled_missing_creds"
            detail = "enabled mailbox" if creds else "enabled but MAIL_* missing"
        rows.append(
            {
                "id": mb.id,
                "market": mb.market,
                "language": mb.language,
                "enabled": mb.enabled,
                "env_prefix": mb.env_prefix,
                "credentials_present": creds,
                "status": status,
                "detail": detail,
            }
        )
    return {
        "ok": True,
        "mailboxes": rows,
        "enabled_flipped": False,
        "message": "Mailbox matrix (read-only; NO/DK stay disabled until Oscar enables).",
    }
