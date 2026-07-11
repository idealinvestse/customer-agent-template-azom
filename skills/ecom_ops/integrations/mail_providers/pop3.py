"""POP3 receive transport."""

from __future__ import annotations

import email
import email.policy
import poplib
from typing import Any

from ecom_ops.integrations.mail_providers.models import (
    MailConfig,
    MailMessage,
    parse_email_message,
)
from ecom_ops.security import SecurityError


class Pop3Transport:
    """POP3 receive (password auth)."""

    def __init__(self, config: MailConfig) -> None:
        self.config = config

    def send(self, message: MailMessage) -> dict[str, Any]:
        # POP3 cannot send — delegate is not available; require SMTP for send
        raise SecurityError("POP3 transport cannot send; use SMTP or Graph provider")

    def fetch(
        self,
        *,
        folder: str = "INBOX",
        unread_only: bool = True,
        limit: int = 20,
    ) -> list[MailMessage]:
        del folder, unread_only  # POP3 has no folders / unread flags
        cfg = self.config
        if not cfg.pop3_host:
            raise SecurityError("pop3_host is required for POP3 fetch")
        if cfg.pop3_use_ssl:
            conn = poplib.POP3_SSL(cfg.pop3_host, cfg.pop3_port, timeout=30)
        else:
            conn = poplib.POP3(cfg.pop3_host, cfg.pop3_port, timeout=30)
        messages: list[MailMessage] = []
        try:
            if not cfg.username or not cfg.password:
                raise SecurityError("POP3 username and password are required")
            conn.user(cfg.username)
            conn.pass_(cfg.password)
            count, _ = conn.stat()
            start = max(1, count - max(1, min(limit, 100)) + 1)
            for i in range(count, start - 1, -1):
                _resp, lines, _octets = conn.retr(i)
                raw = b"\r\n".join(lines)
                parsed = email.message_from_bytes(raw, policy=email.policy.default)
                messages.append(parse_email_message(parsed, uid=str(i)))
        finally:
            try:
                conn.quit()
            except Exception:
                pass
        return messages

    def mark_read(self, uid: str, *, folder: str = "INBOX") -> None:
        del uid, folder
        # POP3 mark-as-read is typically delete-on-quit; no-op for safety
        return None

