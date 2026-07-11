"""Microsoft Graph mail transport."""

from __future__ import annotations

from typing import Any

import requests

from ecom_ops.integrations.mail_providers.models import MailConfig, MailMessage
from ecom_ops.security import SecurityError


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

