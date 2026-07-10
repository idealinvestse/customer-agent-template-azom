"""Gmail OAuth2 authorization-code flow + token persistence."""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from ecom_ops.security import SecurityError, get_env

GMAIL_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SCOPES = (
    "https://mail.google.com/ "
    "https://www.googleapis.com/auth/gmail.send "
    "https://www.googleapis.com/auth/gmail.readonly"
)


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _is_mock() -> bool:
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


def gmail_oauth_configured() -> bool:
    return bool(
        get_env("MAIL_OAUTH_CLIENT_ID")
        and get_env("MAIL_OAUTH_CLIENT_SECRET")
    )


def gmail_redirect_uri() -> str:
    return (
        get_env("MAIL_OAUTH_REDIRECT_URI")
        or f"http://{os.environ.get('DASHBOARD_HOST', '127.0.0.1')}:{os.environ.get('DASHBOARD_PORT', '8080')}/oauth/gmail/callback"
    )


@dataclass(frozen=True)
class GmailTokenBundle:
    access_token: str
    refresh_token: str
    expires_at: float | None
    token_type: str
    scope: str
    email: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
            "email": self.email,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GmailTokenBundle:
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=float(data["expires_at"]) if data.get("expires_at") else None,
            token_type=str(data.get("token_type") or "Bearer"),
            scope=str(data.get("scope") or ""),
            email=str(data["email"]) if data.get("email") else None,
        )


class GmailOAuthStore:
    """Persist Gmail OAuth tokens under AZOM_DATA_DIR/oauth/gmail.json."""

    def __init__(self, data_dir: Path | None = None) -> None:
        base = data_dir or _data_dir()
        self.oauth_dir = base / "oauth"
        self.token_path = self.oauth_dir / "gmail.json"
        self.state_path = self.oauth_dir / "gmail_oauth_state.json"

    def has_tokens(self) -> bool:
        bundle = self.load_tokens()
        return bool(bundle and (bundle.refresh_token or bundle.access_token))

    def load_tokens(self) -> GmailTokenBundle | None:
        if not self.token_path.is_file():
            return None
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        bundle = GmailTokenBundle.from_dict(data)
        if not bundle.access_token and not bundle.refresh_token:
            return None
        return bundle

    def save_tokens(self, bundle: GmailTokenBundle) -> None:
        self.oauth_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(bundle.to_dict(), indent=2)
        self.token_path.write_text(payload, encoding="utf-8")
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass

    def clear_tokens(self) -> None:
        if self.token_path.is_file():
            self.token_path.unlink()

    def create_state(self) -> str:
        state = secrets.token_urlsafe(32)
        self.oauth_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": state,
            "created_at": time.time(),
            "expires_at": time.time() + 600,
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(self.state_path, 0o600)
        except OSError:
            pass
        return state

    def validate_state(self, state: str) -> bool:
        if not state or not self.state_path.is_file():
            return False
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if data.get("state") != state:
            return False
        if time.time() > float(data.get("expires_at", 0)):
            return False
        return True

    def clear_state(self) -> None:
        if self.state_path.is_file():
            self.state_path.unlink()

    def build_authorize_url(self, *, state: str) -> str:
        client_id = get_env("MAIL_OAUTH_CLIENT_ID")
        if not client_id:
            raise SecurityError("MAIL_OAUTH_CLIENT_ID required for Gmail OAuth")
        params = {
            "client_id": client_id,
            "redirect_uri": gmail_redirect_uri(),
            "response_type": "code",
            "scope": GMAIL_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{GMAIL_AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> GmailTokenBundle:
        if _is_mock():
            bundle = GmailTokenBundle(
                access_token="mock-access-token",
                refresh_token="mock-refresh-token",
                expires_at=time.time() + 3600,
                token_type="Bearer",
                scope=GMAIL_SCOPES,
                email="mock@gmail.com",
            )
            self.save_tokens(bundle)
            return bundle

        client_id = get_env("MAIL_OAUTH_CLIENT_ID")
        client_secret = get_env("MAIL_OAUTH_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise SecurityError("MAIL_OAUTH_CLIENT_ID and MAIL_OAUTH_CLIENT_SECRET required")

        resp = requests.post(
            GMAIL_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": gmail_redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise SecurityError(f"Gmail token exchange failed: {resp.status_code}")
        data = resp.json()
        access = str(data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or "")
        if not access:
            raise SecurityError("Gmail token response missing access_token")
        expires_in = data.get("expires_in")
        expires_at = time.time() + float(expires_in) if expires_in else None
        bundle = GmailTokenBundle(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
            token_type=str(data.get("token_type") or "Bearer"),
            scope=str(data.get("scope") or GMAIL_SCOPES),
        )
        self.save_tokens(bundle)
        return bundle

    def mock_connect(self) -> GmailTokenBundle:
        """Simulate OAuth consent in mock/dev mode."""
        bundle = GmailTokenBundle(
            access_token="mock-access-token",
            refresh_token="mock-refresh-token",
            expires_at=time.time() + 86400,
            token_type="Bearer",
            scope=GMAIL_SCOPES,
            email="mock@gmail.com",
        )
        self.save_tokens(bundle)
        return bundle


def apply_stored_gmail_tokens(config: Any) -> Any:
    """Merge stored Gmail tokens into MailConfig when provider is gmail."""
    from ecom_ops.integrations.mail import MailConfig, MailProvider

    if not isinstance(config, MailConfig):
        return config
    if config.provider != MailProvider.GMAIL:
        return config
    bundle = GmailOAuthStore().load_tokens()
    if not bundle:
        return config
    return MailConfig(
        provider=config.provider,
        username=config.username,
        password=config.password,
        from_addr=config.from_addr,
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        smtp_use_tls=config.smtp_use_tls,
        smtp_use_ssl=config.smtp_use_ssl,
        imap_host=config.imap_host,
        imap_port=config.imap_port,
        imap_use_ssl=config.imap_use_ssl,
        pop3_host=config.pop3_host,
        pop3_port=config.pop3_port,
        pop3_use_ssl=config.pop3_use_ssl,
        oauth_access_token=bundle.access_token or config.oauth_access_token,
        oauth_refresh_token=bundle.refresh_token or config.oauth_refresh_token,
        oauth_client_id=config.oauth_client_id or get_env("MAIL_OAUTH_CLIENT_ID", "") or "",
        oauth_client_secret=config.oauth_client_secret or get_env("MAIL_OAUTH_CLIENT_SECRET", "") or "",
        oauth_token_url=config.oauth_token_url or GMAIL_TOKEN_URL,
        graph_tenant_id=config.graph_tenant_id,
        graph_client_id=config.graph_client_id,
        graph_client_secret=config.graph_client_secret,
        graph_user=config.graph_user,
        graph_base_url=config.graph_base_url,
    )
