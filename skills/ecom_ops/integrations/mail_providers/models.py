"""Mail models, protocol, and shared helpers."""

from __future__ import annotations

import base64
import email
import email.policy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class MailProvider(str, Enum):
    GMAIL = "gmail"
    OUTLOOK = "outlook"
    EXCHANGE_GRAPH = "exchange_graph"
    GENERIC_IMAP = "generic_imap"
    GENERIC_POP3 = "generic_pop3"


# Well-known provider defaults
PROVIDER_DEFAULTS: dict[MailProvider, dict[str, Any]] = {
    MailProvider.GMAIL: {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_use_tls": True,
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "pop3_host": "pop.gmail.com",
        "pop3_port": 995,
        "pop3_use_ssl": True,
    },
    MailProvider.OUTLOOK: {
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_use_tls": True,
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_use_ssl": True,
        "pop3_host": "outlook.office365.com",
        "pop3_port": 995,
        "pop3_use_ssl": True,
    },
    MailProvider.EXCHANGE_GRAPH: {
        "graph_base_url": "https://graph.microsoft.com/v1.0",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    },
    MailProvider.GENERIC_IMAP: {},
    MailProvider.GENERIC_POP3: {},
}


@dataclass
class MailMessage:
    subject: str
    body: str
    from_addr: str
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    html_body: str | None = None
    date: str | None = None
    uid: str | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    references_header: str | None = None
    is_read: bool = False
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body": self.body,
            "from_addr": self.from_addr,
            "to_addrs": list(self.to_addrs),
            "cc_addrs": list(self.cc_addrs),
            "html_body": self.html_body,
            "date": self.date,
            "uid": self.uid,
            "message_id": self.message_id,
            "in_reply_to": self.in_reply_to,
            "references_header": self.references_header,
            "is_read": self.is_read,
        }


@dataclass(frozen=True)
class MailConfig:
    provider: MailProvider
    username: str = ""
    password: str = ""
    from_addr: str = ""
    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    # IMAP
    imap_host: str = ""
    imap_port: int = 993
    imap_use_ssl: bool = True
    # POP3
    pop3_host: str = ""
    pop3_port: int = 995
    pop3_use_ssl: bool = True
    # OAuth2 (app password OR OAuth2)
    oauth_access_token: str = ""
    oauth_refresh_token: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url: str = ""
    # Microsoft Graph
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_user: str = ""  # mailbox UPN / user id
    graph_base_url: str = "https://graph.microsoft.com/v1.0"

    @property
    def use_oauth2(self) -> bool:
        return bool(self.oauth_access_token or self.oauth_refresh_token)

    @property
    def use_graph(self) -> bool:
        return self.provider == MailProvider.EXCHANGE_GRAPH

    @property
    def effective_from(self) -> str:
        return self.from_addr or self.username or self.graph_user


class MailTransport(Protocol):
    def send(self, message: MailMessage) -> dict[str, Any]: ...

    def fetch(
        self,
        *,
        folder: str = "INBOX",
        unread_only: bool = True,
        limit: int = 20,
    ) -> list[MailMessage]: ...

    def mark_read(self, uid: str, *, folder: str = "INBOX") -> None: ...


def build_xoauth2_string(username: str, access_token: str) -> str:
    """RFC 7628 SASL XOAUTH2 initial client response."""
    auth = f"user={username}\x01auth=Bearer {access_token}\x01\x01"
    return auth


def _b64_xoauth2(username: str, access_token: str) -> bytes:
    return base64.b64encode(build_xoauth2_string(username, access_token).encode("utf-8"))


def parse_email_message(parsed: email.message.Message, *, uid: str) -> MailMessage:
    subject = str(parsed.get("Subject") or "")
    from_addr = str(parsed.get("From") or "")
    to_raw = str(parsed.get("To") or "")
    cc_raw = str(parsed.get("Cc") or "")
    date = str(parsed.get("Date") or "")
    message_id = str(parsed.get("Message-ID") or "") or None
    in_reply_to = str(parsed.get("In-Reply-To") or "") or None
    references_header = str(parsed.get("References") or "") or None

    body = ""
    html_body = None
    if parsed.is_multipart():
        for part in parsed.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not body:
                try:
                    body = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    body = payload.decode("utf-8", errors="replace") if payload else ""
            elif ctype == "text/html" and html_body is None:
                try:
                    html_body = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    html_body = (
                        payload.decode("utf-8", errors="replace") if payload else None
                    )
    else:
        try:
            content = parsed.get_content()
        except Exception:
            payload = parsed.get_payload(decode=True)
            content = payload.decode("utf-8", errors="replace") if payload else ""
        if parsed.get_content_type() == "text/html":
            html_body = content
        else:
            body = content

    def _split_addrs(raw: str) -> list[str]:
        if not raw.strip():
            return []
        return [a.strip() for a in raw.split(",") if a.strip()]

    return MailMessage(
        subject=subject,
        body=body if isinstance(body, str) else str(body),
        html_body=html_body if isinstance(html_body, str) or html_body is None else str(html_body),
        from_addr=from_addr,
        to_addrs=_split_addrs(to_raw),
        cc_addrs=_split_addrs(cc_raw),
        date=date or None,
        uid=uid,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references_header=references_header,
        is_read=False,
    )

