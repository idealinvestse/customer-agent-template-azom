"""Mail connector: Gmail, Outlook/Exchange, generic IMAP/POP3/SMTP + MS Graph."""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import os
import poplib
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from enum import Enum
from typing import Any, Protocol

import requests

from ecom_ops.security import SecurityError, get_env, sanitize_text, validate_email


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


class SmtpImapTransport:
    """SMTP send + IMAP receive (password or XOAUTH2)."""

    def __init__(self, config: MailConfig) -> None:
        self.config = config
        self._access_token = config.oauth_access_token

    def _ensure_oauth_token(self) -> str:
        if self._access_token:
            return self._access_token
        if not self.config.oauth_refresh_token:
            raise SecurityError("OAuth2 access_token or refresh_token required")
        token_url = self.config.oauth_token_url
        if not token_url:
            if self.config.provider == MailProvider.GMAIL:
                token_url = "https://oauth2.googleapis.com/token"
            elif self.config.provider == MailProvider.OUTLOOK:
                tenant = self.config.graph_tenant_id or "common"
                token_url = (
                    f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
                )
            else:
                raise SecurityError("oauth_token_url required for OAuth2 refresh")
        data = {
            "client_id": self.config.oauth_client_id,
            "client_secret": self.config.oauth_client_secret,
            "refresh_token": self.config.oauth_refresh_token,
            "grant_type": "refresh_token",
        }
        if self.config.provider == MailProvider.OUTLOOK:
            data["scope"] = "https://outlook.office365.com/SMTP.Send offline_access"
        resp = requests.post(token_url, data=data, timeout=30)
        if resp.status_code >= 400:
            raise SecurityError(f"OAuth2 token refresh failed: {resp.status_code}")
        payload = resp.json()
        self._access_token = str(payload.get("access_token", ""))
        if not self._access_token:
            raise SecurityError("OAuth2 token response missing access_token")
        return self._access_token

    def send(self, message: MailMessage) -> dict[str, Any]:
        cfg = self.config
        if not cfg.smtp_host:
            raise SecurityError("smtp_host is required for SMTP send")
        msg = EmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = message.from_addr or cfg.effective_from
        msg["To"] = ", ".join(message.to_addrs)
        if message.cc_addrs:
            msg["Cc"] = ", ".join(message.cc_addrs)
        if message.in_reply_to:
            msg["In-Reply-To"] = message.in_reply_to
        if message.references_header:
            msg["References"] = message.references_header
        if message.html_body:
            msg.set_content(message.body or "")
            msg.add_alternative(message.html_body, subtype="html")
        else:
            msg.set_content(message.body or "")

        context = ssl.create_default_context()
        if cfg.smtp_use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.smtp_host, cfg.smtp_port, context=context, timeout=30
            )
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
            if cfg.smtp_use_tls:
                server.starttls(context=context)

        try:
            if cfg.use_oauth2:
                token = self._ensure_oauth_token()
                auth_str = build_xoauth2_string(cfg.username, token)
                code, resp = server.docmd(
                    "AUTH", "XOAUTH2 " + base64.b64encode(auth_str.encode()).decode()
                )
                if code != 235:
                    raise SecurityError(f"SMTP XOAUTH2 failed: {code} {resp!r}")
            else:
                if not cfg.username or not cfg.password:
                    raise SecurityError("SMTP username and password are required")
                server.login(cfg.username, cfg.password)
            recipients = list(message.to_addrs) + list(message.cc_addrs)
            server.send_message(msg, to_addrs=recipients)
        finally:
            try:
                server.quit()
            except Exception:
                server.close()

        return {
            "status": "sent",
            "to": message.to_addrs,
            "subject": message.subject,
            "from": msg["From"],
        }

    def _imap_connect(self) -> imaplib.IMAP4:
        cfg = self.config
        if not cfg.imap_host:
            raise SecurityError("imap_host is required for IMAP fetch")
        if cfg.imap_use_ssl:
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
        else:
            conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
        if cfg.use_oauth2:
            token = self._ensure_oauth_token()
            conn.authenticate("XOAUTH2", lambda _: _b64_xoauth2(cfg.username, token))
        else:
            if not cfg.username or not cfg.password:
                raise SecurityError("IMAP username and password are required")
            conn.login(cfg.username, cfg.password)
        return conn

    def fetch(
        self,
        *,
        folder: str = "INBOX",
        unread_only: bool = True,
        limit: int = 20,
    ) -> list[MailMessage]:
        conn = self._imap_connect()
        messages: list[MailMessage] = []
        try:
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                raise SecurityError(f"IMAP select failed for folder {folder!r}")
            criteria = "UNSEEN" if unread_only else "ALL"
            status, data = conn.search(None, criteria)
            if status != "OK":
                raise SecurityError("IMAP search failed")
            ids = data[0].split() if data and data[0] else []
            # Newest first
            ids = list(reversed(ids))[: max(1, min(limit, 100))]
            for msg_id in ids:
                status, msg_data = conn.fetch(msg_id, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw_bytes = msg_data[0][1]
                if not isinstance(raw_bytes, (bytes, bytearray)):
                    continue
                parsed = email.message_from_bytes(raw_bytes, policy=email.policy.default)
                messages.append(_parse_email_message(parsed, uid=msg_id.decode()))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return messages

    def mark_read(self, uid: str, *, folder: str = "INBOX") -> None:
        conn = self._imap_connect()
        try:
            status, _ = conn.select(folder)
            if status != "OK":
                raise SecurityError(f"IMAP select failed for folder {folder!r}")
            conn.store(uid.encode() if isinstance(uid, str) else uid, "+FLAGS", "\\Seen")
        finally:
            try:
                conn.logout()
            except Exception:
                pass


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
                messages.append(_parse_email_message(parsed, uid=str(i)))
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


class GraphMailTransport:
    """Microsoft Graph API mail (OAuth2 client credentials or access token)."""

    def __init__(self, config: MailConfig, *, session: Any | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()
        self._access_token = config.oauth_access_token

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        cfg = self.config
        if not (cfg.graph_tenant_id and cfg.graph_client_id and cfg.graph_client_secret):
            raise SecurityError(
                "Graph requires graph_tenant_id, graph_client_id, graph_client_secret "
                "or oauth_access_token"
            )
        url = (
            f"https://login.microsoftonline.com/{cfg.graph_tenant_id}"
            "/oauth2/v2.0/token"
        )
        data = {
            "client_id": cfg.graph_client_id,
            "client_secret": cfg.graph_client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
        resp = self.session.post(url, data=data, timeout=30)
        if resp.status_code >= 400:
            raise SecurityError(f"Graph token failed: {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        self._access_token = str(payload.get("access_token", ""))
        if not self._access_token:
            raise SecurityError("Graph token response missing access_token")
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }

    def _user_path(self) -> str:
        user = self.config.graph_user or self.config.username
        if not user:
            raise SecurityError("graph_user (mailbox UPN) is required")
        return f"/users/{user}"

    def send(self, message: MailMessage) -> dict[str, Any]:
        base = self.config.graph_base_url.rstrip("/")
        url = f"{base}{self._user_path()}/sendMail"
        graph_msg: dict[str, Any] = {
            "subject": message.subject,
            "body": {
                "contentType": "HTML" if message.html_body else "Text",
                "content": message.html_body or message.body or "",
            },
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in message.to_addrs
            ],
            "ccRecipients": [
                {"emailAddress": {"address": addr}} for addr in message.cc_addrs
            ],
        }
        headers: list[dict[str, str]] = []
        if message.in_reply_to:
            headers.append({"name": "In-Reply-To", "value": message.in_reply_to})
        if message.references_header:
            headers.append({"name": "References", "value": message.references_header})
        if headers:
            graph_msg["internetMessageHeaders"] = headers
        payload = {
            "message": graph_msg,
            "saveToSentItems": True,
        }
        resp = self.session.post(url, headers=self._headers(), json=payload, timeout=30)
        if resp.status_code not in (200, 202):
            raise SecurityError(
                f"Graph sendMail failed: {resp.status_code} {resp.text[:300]}"
            )
        return {
            "status": "sent",
            "to": message.to_addrs,
            "subject": message.subject,
            "provider": "exchange_graph",
        }

    def fetch(
        self,
        *,
        folder: str = "INBOX",
        unread_only: bool = True,
        limit: int = 20,
    ) -> list[MailMessage]:
        base = self.config.graph_base_url.rstrip("/")
        top = max(1, min(limit, 100))
        filter_q = "isRead eq false" if unread_only else ""
        folder_path = "inbox" if folder.upper() == "INBOX" else folder
        url = f"{base}{self._user_path()}/mailFolders/{folder_path}/messages"
        params: dict[str, Any] = {
            "$top": top,
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,bodyPreview,body,from,toRecipients,ccRecipients,"
            "receivedDateTime,isRead,internetMessageId",
        }
        if filter_q:
            params["$filter"] = filter_q
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code >= 400:
            raise SecurityError(
                f"Graph list messages failed: {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        result: list[MailMessage] = []
        for item in data.get("value") or []:
            from_obj = (item.get("from") or {}).get("emailAddress") or {}
            to_list = [
                (r.get("emailAddress") or {}).get("address", "")
                for r in (item.get("toRecipients") or [])
            ]
            cc_list = [
                (r.get("emailAddress") or {}).get("address", "")
                for r in (item.get("ccRecipients") or [])
            ]
            body_obj = item.get("body") or {}
            content = body_obj.get("content") or item.get("bodyPreview") or ""
            result.append(
                MailMessage(
                    subject=str(item.get("subject") or ""),
                    body=content if body_obj.get("contentType") != "HTML" else "",
                    html_body=content if body_obj.get("contentType") == "HTML" else None,
                    from_addr=str(from_obj.get("address") or ""),
                    to_addrs=[a for a in to_list if a],
                    cc_addrs=[a for a in cc_list if a],
                    date=item.get("receivedDateTime"),
                    uid=str(item.get("id") or ""),
                    message_id=item.get("internetMessageId"),
                    is_read=bool(item.get("isRead")),
                    raw=item,
                )
            )
        return result

    def mark_read(self, uid: str, *, folder: str = "INBOX") -> None:
        del folder
        base = self.config.graph_base_url.rstrip("/")
        url = f"{base}{self._user_path()}/messages/{uid}"
        resp = self.session.patch(
            url, headers=self._headers(), json={"isRead": True}, timeout=30
        )
        if resp.status_code >= 400:
            raise SecurityError(
                f"Graph mark_read failed: {resp.status_code} {resp.text[:200]}"
            )


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


def _parse_email_message(parsed: email.message.Message, *, uid: str) -> MailMessage:
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


def config_from_env(
    *,
    provider: str | MailProvider | None = None,
) -> MailConfig:
    """Build MailConfig from environment variables."""
    prov_raw = (
        str(provider)
        if provider
        else get_env("MAIL_PROVIDER", "generic_imap") or "generic_imap"
    )
    try:
        prov = MailProvider(prov_raw.lower().strip())
    except ValueError as exc:
        raise SecurityError(
            f"Invalid MAIL_PROVIDER {prov_raw!r}. "
            f"Allowed: {[p.value for p in MailProvider]}"
        ) from exc

    defaults = PROVIDER_DEFAULTS.get(prov, {})

    def _int(name: str, default: int) -> int:
        raw = get_env(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise SecurityError(f"Invalid integer env {name}={raw!r}") from exc

    def _bool(name: str, default: bool) -> bool:
        raw = get_env(name)
        if raw is None:
            return default
        return raw.lower() in {"1", "true", "yes", "on"}

    config = MailConfig(
        provider=prov,
        username=get_env("MAIL_USERNAME", "") or "",
        password=get_env("MAIL_PASSWORD", "") or get_env("SMTP_PASSWORD", "") or "",
        from_addr=get_env("MAIL_FROM", "") or "",
        smtp_host=get_env("SMTP_HOST", defaults.get("smtp_host", "")) or "",
        smtp_port=_int("SMTP_PORT", int(defaults.get("smtp_port", 587))),
        smtp_use_tls=_bool("SMTP_USE_TLS", bool(defaults.get("smtp_use_tls", True))),
        smtp_use_ssl=_bool("SMTP_USE_SSL", bool(defaults.get("smtp_use_ssl", False))),
        imap_host=get_env("IMAP_HOST", defaults.get("imap_host", "")) or "",
        imap_port=_int("IMAP_PORT", int(defaults.get("imap_port", 993))),
        imap_use_ssl=_bool("IMAP_USE_SSL", bool(defaults.get("imap_use_ssl", True))),
        pop3_host=get_env("POP3_HOST", defaults.get("pop3_host", "")) or "",
        pop3_port=_int("POP3_PORT", int(defaults.get("pop3_port", 995))),
        pop3_use_ssl=_bool("POP3_USE_SSL", bool(defaults.get("pop3_use_ssl", True))),
        oauth_access_token=get_env("MAIL_OAUTH_ACCESS_TOKEN", "") or "",
        oauth_refresh_token=get_env("MAIL_OAUTH_REFRESH_TOKEN", "") or "",
        oauth_client_id=get_env("MAIL_OAUTH_CLIENT_ID", "") or "",
        oauth_client_secret=get_env("MAIL_OAUTH_CLIENT_SECRET", "") or "",
        oauth_token_url=get_env("MAIL_OAUTH_TOKEN_URL", "") or "",
        graph_tenant_id=get_env("GRAPH_TENANT_ID", "") or "",
        graph_client_id=get_env("GRAPH_CLIENT_ID", "") or "",
        graph_client_secret=get_env("GRAPH_CLIENT_SECRET", "") or "",
        graph_user=get_env("GRAPH_USER", "") or "",
        graph_base_url=get_env(
            "GRAPH_BASE_URL",
            defaults.get("graph_base_url", "https://graph.microsoft.com/v1.0"),
        )
        or "https://graph.microsoft.com/v1.0",
    )
    from ecom_ops.oauth.gmail import apply_stored_gmail_tokens

    return apply_stored_gmail_tokens(config)


def _transport_for(config: MailConfig) -> MailTransport:
    if config.provider == MailProvider.EXCHANGE_GRAPH:
        return GraphMailTransport(config)
    if config.provider == MailProvider.GENERIC_POP3:
        return Pop3Transport(config)
    # gmail, outlook, generic_imap → SMTP + IMAP
    return SmtpImapTransport(config)


class MailClient:
    """Unified mail client for send/fetch/mark_read across providers."""

    def __init__(
        self,
        *,
        config: MailConfig | None = None,
        transport: MailTransport | None = None,
    ) -> None:
        self.config = config or config_from_env()
        self.transport = transport or _transport_for(self.config)

    def send(
        self,
        *,
        to: str | list[str],
        subject: str,
        body: str,
        cc: str | list[str] | None = None,
        html_body: str | None = None,
        from_addr: str | None = None,
        in_reply_to: str | None = None,
        references_header: str | None = None,
    ) -> dict[str, Any]:
        to_list = _normalize_addrs(to)
        cc_list = _normalize_addrs(cc) if cc else []
        for addr in to_list + cc_list:
            validate_email(addr)
        subj = sanitize_text(subject, max_len=500)
        body_s = sanitize_text(body, max_len=100_000)
        html_s = sanitize_text(html_body, max_len=200_000) if html_body else None
        sender = from_addr or self.config.effective_from
        if sender:
            validate_email(sender)
        message = MailMessage(
            subject=subj,
            body=body_s,
            html_body=html_s,
            from_addr=sender or "",
            to_addrs=to_list,
            cc_addrs=cc_list,
            in_reply_to=(in_reply_to or "").strip() or None,
            references_header=(references_header or "").strip() or None,
        )
        return self.transport.send(message)

    def fetch_unread(
        self, *, folder: str = "INBOX", limit: int = 20
    ) -> list[MailMessage]:
        return self.transport.fetch(folder=folder, unread_only=True, limit=limit)

    def fetch(
        self, *, folder: str = "INBOX", unread_only: bool = False, limit: int = 20
    ) -> list[MailMessage]:
        return self.transport.fetch(
            folder=folder, unread_only=unread_only, limit=limit
        )

    def mark_read(self, uid: str, *, folder: str = "INBOX") -> None:
        if not uid or not str(uid).strip():
            raise SecurityError("uid is required")
        self.transport.mark_read(str(uid).strip(), folder=folder)

    def reply(
        self,
        original: MailMessage,
        *,
        body: str,
        html_body: str | None = None,
    ) -> dict[str, Any]:
        subject = original.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        parent = (original.message_id or "").strip() or None
        refs_parts: list[str] = []
        if original.references_header:
            refs_parts.extend(original.references_header.split())
        if parent:
            refs_parts.append(parent)
        seen: set[str] = set()
        refs: list[str] = []
        for part in refs_parts:
            p = part.strip()
            if p and p not in seen:
                seen.add(p)
                refs.append(p)
        return self.send(
            to=original.from_addr,
            subject=subject,
            body=body,
            html_body=html_body,
            in_reply_to=parent,
            references_header=" ".join(refs) if refs else None,
        )


def _normalize_addrs(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts
    return [str(v).strip() for v in value if str(v).strip()]


def client_from_env(
    *,
    provider: str | MailProvider | None = None,
    use_mock: bool | None = None,
) -> MailClient:
    mock = use_mock
    if mock is None:
        mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    if mock:
        return MailClient(
            config=MailConfig(
                provider=MailProvider.GENERIC_IMAP,
                username="mock@azom.se",
                from_addr="support@azom.se",
            ),
            transport=InMemoryMailTransport(),
        )
    return MailClient(config=config_from_env(provider=provider))
