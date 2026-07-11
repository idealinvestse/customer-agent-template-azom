"""In-memory mail transport for tests / mock mode."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ecom_ops.integrations.mail_providers.models import MailMessage


class InMemoryMailTransport:
    """Deterministic mock for tests / dry-run pilot."""

    def __init__(self) -> None:
        self.outbox: list[MailMessage] = []
        self.inbox: list[MailMessage] = [
            MailMessage(
                subject="Order 1001 status?",
                body="Hej, var är min order 1001?",
                from_addr="customer@example.com",
                to_addrs=["support@azom.se"],
                date=datetime.now(timezone.utc).isoformat(),
                uid="mock-1",
                message_id="<mock-1@example.com>",
                is_read=False,
            ),
            MailMessage(
                subject="Return request",
                body="I want a refund for order 1002",
                from_addr="buyer@example.com",
                to_addrs=["support@azom.se"],
                date=datetime.now(timezone.utc).isoformat(),
                uid="mock-2",
                message_id="<mock-2@example.com>",
                is_read=False,
            ),
        ]
        self.calls: list[tuple[str, Any]] = []

    def send(self, message: MailMessage) -> dict[str, Any]:
        self.calls.append(("send", message.to_dict()))
        self.outbox.append(message)
        return {
            "status": "sent",
            "to": message.to_addrs,
            "subject": message.subject,
            "provider": "mock",
        }

    def fetch(
        self,
        *,
        folder: str = "INBOX",
        unread_only: bool = True,
        limit: int = 20,
    ) -> list[MailMessage]:
        self.calls.append(
            ("fetch", {"folder": folder, "unread_only": unread_only, "limit": limit})
        )
        msgs = [m for m in self.inbox if (not unread_only or not m.is_read)]
        return msgs[: max(1, min(limit, 100))]

    def mark_read(self, uid: str, *, folder: str = "INBOX") -> None:
        self.calls.append(("mark_read", {"uid": uid, "folder": folder}))
        for i, m in enumerate(self.inbox):
            if m.uid == uid:
                self.inbox[i] = MailMessage(
                    subject=m.subject,
                    body=m.body,
                    from_addr=m.from_addr,
                    to_addrs=list(m.to_addrs),
                    cc_addrs=list(m.cc_addrs),
                    html_body=m.html_body,
                    date=m.date,
                    uid=m.uid,
                    message_id=m.message_id,
                    in_reply_to=m.in_reply_to,
                    references_header=m.references_header,
                    is_read=True,
                    raw=m.raw,
                )

