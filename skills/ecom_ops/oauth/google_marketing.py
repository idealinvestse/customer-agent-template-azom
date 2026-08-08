"""Google marketing OAuth2 (GA4 + Ads) authorization-code flow."""

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

from ecom_ops.security import get_env

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/analytics.readonly "
    "https://www.googleapis.com/auth/adwords "
    "https://www.googleapis.com/auth/content"
)
SCOPES_EDIT = SCOPES + " https://www.googleapis.com/auth/analytics.edit"
STATE_TTL_SEC = 600


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _is_mock() -> bool:
    return os.environ.get("AZOM_USE_MOCK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def google_marketing_oauth_configured() -> bool:
    return bool(
        get_env("GOOGLE_OAUTH_CLIENT_ID")
        and get_env("GOOGLE_OAUTH_CLIENT_SECRET")
    )


def google_marketing_redirect_uri() -> str:
    return (
        get_env("GOOGLE_OAUTH_REDIRECT_URI")
        or f"http://{os.environ.get('DASHBOARD_HOST', '127.0.0.1')}:{os.environ.get('DASHBOARD_PORT', '8080')}/oauth/google/callback"
    )


@dataclass(frozen=True)
class GoogleMarketingTokenBundle:
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
    def from_dict(cls, data: dict[str, Any]) -> GoogleMarketingTokenBundle:
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=float(data["expires_at"]) if data.get("expires_at") else None,
            token_type=str(data.get("token_type") or "Bearer"),
            scope=str(data.get("scope") or ""),
            email=str(data["email"]) if data.get("email") else None,
        )


class GoogleMarketingOAuthStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        base = data_dir or _data_dir()
        self.oauth_dir = base / "oauth"
        self.token_path = self.oauth_dir / "google_marketing.json"
        self.state_path = self.oauth_dir / "google_marketing_oauth_state.json"

    def has_tokens(self) -> bool:
        bundle = self.load_tokens()
        return bool(bundle and (bundle.refresh_token or bundle.access_token))

    def load_tokens(self) -> GoogleMarketingTokenBundle | None:
        if not self.token_path.is_file():
            return None
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return GoogleMarketingTokenBundle.from_dict(data)

    def save_tokens(self, bundle: GoogleMarketingTokenBundle) -> None:
        self.oauth_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(bundle.to_dict(), indent=2), encoding="utf-8"
        )
        tmp.replace(self.token_path)
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
            "expires_at": time.time() + STATE_TTL_SEC,
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(self.state_path, 0o600)
        except OSError:
            pass
        return state

    def validate_state(self, state: str) -> bool:
        """Check state without deleting — mismatch must not wipe pending CSRF."""
        if not state or not self.state_path.is_file():
            return False
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if str(data.get("state") or "") != state:
            return False
        if time.time() > float(data.get("expires_at") or 0):
            return False
        return True

    def clear_state(self) -> None:
        if self.state_path.is_file():
            self.state_path.unlink()

    def consume_state(self, state: str) -> bool:
        """Validate then delete only on success (or expired matching state)."""
        if not self.validate_state(state):
            # Drop only if expired *and* state matches (stale legitimate callback)
            if self.state_path.is_file():
                try:
                    data = json.loads(self.state_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return False
                if str(data.get("state") or "") == state and time.time() > float(
                    data.get("expires_at") or 0
                ):
                    self.clear_state()
            return False
        self.clear_state()
        return True


def build_authorize_url(*, include_edit: bool = False) -> tuple[str, str]:
    store = GoogleMarketingOAuthStore()
    state = store.create_state()
    if _is_mock():
        return f"/oauth/google/callback?code=mock&state={state}", state
    params = {
        "client_id": get_env("GOOGLE_OAUTH_CLIENT_ID") or "",
        "redirect_uri": google_marketing_redirect_uri(),
        "response_type": "code",
        "scope": SCOPES_EDIT if include_edit else SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def exchange_code(code: str) -> GoogleMarketingTokenBundle:
    """Exchange auth code. Mock tokens only when AZOM_USE_MOCK is on."""
    store = GoogleMarketingOAuthStore()
    if _is_mock():
        if code not in {"mock", "mock-access"} and not code:
            raise ValueError("Mock OAuth requires code=mock")
        bundle = GoogleMarketingTokenBundle(
            access_token="mock-access",
            refresh_token="mock-refresh",
            expires_at=time.time() + 3600,
            token_type="Bearer",
            scope=SCOPES,
            email="mock-marketing@azom.se",
        )
        store.save_tokens(bundle)
        return bundle
    if code == "mock":
        raise ValueError("Mock OAuth code rejected when AZOM_USE_MOCK is off")
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": get_env("GOOGLE_OAUTH_CLIENT_ID") or "",
            "client_secret": get_env("GOOGLE_OAUTH_CLIENT_SECRET") or "",
            "redirect_uri": google_marketing_redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    bundle = GoogleMarketingTokenBundle(
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        expires_at=time.time() + float(data.get("expires_in") or 3600),
        token_type=str(data.get("token_type") or "Bearer"),
        scope=str(data.get("scope") or SCOPES),
    )
    store.save_tokens(bundle)
    return bundle


def ensure_fresh_access_token(
    *,
    data_dir: Path | None = None,
    skew_sec: float = 60.0,
) -> str:
    """Return a usable marketing access token; refresh via OAuth when expired.

    Env ``GOOGLE_OAUTH_ACCESS_TOKEN`` wins when set (tests / short-lived inject).
    """
    env_tok = (get_env("GOOGLE_OAUTH_ACCESS_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    store = GoogleMarketingOAuthStore(data_dir=data_dir)
    bundle = store.load_tokens()
    if not bundle:
        return ""
    now = time.time()
    if (
        bundle.access_token
        and (
            bundle.expires_at is None
            or now < float(bundle.expires_at) - skew_sec
        )
    ):
        return bundle.access_token
    if not bundle.refresh_token:
        return bundle.access_token or ""
    if _is_mock():
        refreshed = GoogleMarketingTokenBundle(
            access_token="mock-access-refreshed",
            refresh_token=bundle.refresh_token,
            expires_at=now + 3600,
            token_type=bundle.token_type,
            scope=bundle.scope,
            email=bundle.email,
        )
        store.save_tokens(refreshed)
        return refreshed.access_token
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": get_env("GOOGLE_OAUTH_CLIENT_ID") or "",
            "client_secret": get_env("GOOGLE_OAUTH_CLIENT_SECRET") or "",
            "refresh_token": bundle.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    new_access = str(data.get("access_token") or "")
    refreshed = GoogleMarketingTokenBundle(
        access_token=new_access,
        refresh_token=str(data.get("refresh_token") or bundle.refresh_token),
        expires_at=now + float(data.get("expires_in") or 3600),
        token_type=str(data.get("token_type") or bundle.token_type),
        scope=str(data.get("scope") or bundle.scope),
        email=bundle.email,
    )
    store.save_tokens(refreshed)
    return refreshed.access_token
