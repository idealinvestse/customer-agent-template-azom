"""SMTP send + IMAP receive transport."""

from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

import requests

from ecom_ops.integrations.mail_providers.models import (
    MailConfig,
    MailMessage,
    MailProvider,
    _b64_xoauth2,
    build_xoauth2_string,
    parse_email_message,
)
from ecom_ops.security import SecurityError


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
                messages.append(parse_email_message(parsed, uid=msg_id.decode()))
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


