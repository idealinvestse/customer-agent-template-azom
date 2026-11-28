"""Isolated Google Search Console (GSC) OAuth2 authorization-code flow.

Strictly isolated from Gmail (``MAIL_OAUTH_*``) and Google Marketing
(``GOOGLE_OAUTH_*`` / GA4 / Ads):
- Reads ONLY ``GSC_OAUTH_*`` / ``AZOM_GSC_*`` client credentials.
- Requests ONLY ``https://www.googleapis.com/auth/webmasters.readonly`` with
  ``include_granted_scopes=false``.
- Persists tokens atomically (0600) under ``AZOM_DATA_DIR/oauth/gsc.json``.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ecom_ops.integrations._http_guard import pinned_https_request
from ecom_ops.integrations._http_transport import _redact_error_text
from ecom_ops.runtime_env import env_flag
from ecom_ops.security import SecurityError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_SITES_URL = "https://searchconsole.googleapis.com/webmasters/v3/sites"
_OAUTH_ORIGIN = "https://oauth2.googleapis.com"
_GSC_API_ORIGIN = "https://searchconsole.googleapis.com"

GSC_READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
STATE_TTL_SEC = 600

HttpRequestFn = Callable[..., tuple[int, dict[str, str], bytes, str]]


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _is_mock() -> bool:
    return env_flag("AZOM_USE_MOCK", default=False)


def gsc_oauth_client_id() -> str:
    """Return isolated GSC OAuth client ID (never falls back to Gmail/GA4/Ads)."""
    return (
        os.environ.get("GSC_OAUTH_CLIENT_ID")
        or os.environ.get("AZOM_GSC_CLIENT_ID")
        or ""
    ).strip()


def gsc_oauth_client_secret() -> str:
    """Return isolated GSC OAuth client secret (never falls back to Gmail/GA4/Ads)."""
    return (
        os.environ.get("GSC_OAUTH_CLIENT_SECRET")
        or os.environ.get("AZOM_GSC_CLIENT_SECRET")
        or ""
    ).strip()


def gsc_oauth_redirect_uri() -> str:
    """Return GSC OAuth callback URI."""
    explicit = (
        os.environ.get("GSC_OAUTH_REDIRECT_URI")
        or os.environ.get("AZOM_GSC_REDIRECT_URI")
        or ""
    ).strip()
    if explicit:
        return explicit
    public_url = os.environ.get("AZOM_DASHBOARD_PUBLIC_URL", "").strip()
    if public_url:
        return public_url.rstrip("/") + "/oauth/gsc/callback"
    return "http://127.0.0.1:8080/oauth/gsc/callback"


def gsc_oauth_configured() -> bool:
    """Return True when isolated GSC OAuth client ID and secret are present."""
    return bool(gsc_oauth_client_id() and gsc_oauth_client_secret())


def _atomic_write_0600(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class GscTokenBundle:
    """Isolated Google Search Console OAuth token bundle."""

    access_token: str
    refresh_token: str
    expires_at: float | None
    token_type: str = "Bearer"
    scope: str = GSC_READONLY_SCOPE
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
            "updated_at": self.updated_at or datetime.now(UTC).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GscTokenBundle:
        raw_exp = data.get("expires_at")
        try:
            expires_at = float(raw_exp) if raw_exp is not None and raw_exp != "" else None
        except (TypeError, ValueError):
            expires_at = None
        return cls(
            access_token=str(data.get("access_token") or "").strip(),
            refresh_token=str(data.get("refresh_token") or "").strip(),
            expires_at=expires_at,
            token_type=str(data.get("token_type") or "Bearer").strip() or "Bearer",
            scope=str(data.get("scope") or GSC_READONLY_SCOPE).strip() or GSC_READONLY_SCOPE,
            updated_at=str(data.get("updated_at") or "").strip(),
        )


class GscOAuthStore:
    """Persist isolated GSC OAuth tokens under ``AZOM_DATA_DIR/oauth/gsc.json``."""

    def __init__(self, data_dir: Path | None = None) -> None:
        base = data_dir or _data_dir()
        self.oauth_dir = base / "oauth"
        self.token_path = self.oauth_dir / "gsc.json"
        self.state_path = self.oauth_dir / "gsc_oauth_state.json"

    def save(self, bundle: GscTokenBundle | Mapping[str, Any]) -> GscTokenBundle:
        if isinstance(bundle, GscTokenBundle):
            normalized = GscTokenBundle(
                access_token=bundle.access_token,
                refresh_token=bundle.refresh_token,
                expires_at=bundle.expires_at,
                token_type=bundle.token_type or "Bearer",
                scope=bundle.scope or GSC_READONLY_SCOPE,
                updated_at=datetime.now(UTC).isoformat(),
            )
        else:
            raw = dict(bundle)
            raw["updated_at"] = datetime.now(UTC).isoformat()
            normalized = GscTokenBundle.from_dict(raw)
        _atomic_write_0600(self.token_path, json.dumps(normalized.to_dict(), indent=2))
        return normalized

    def load(self) -> GscTokenBundle | None:
        if not self.token_path.is_file():
            return None
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        bundle = GscTokenBundle.from_dict(data)
        if not bundle.access_token and not bundle.refresh_token:
            return None
        return bundle

    def has_tokens(self) -> bool:
        bundle = self.load()
        return bool(bundle and (bundle.refresh_token or bundle.access_token))

    def clear(self) -> None:
        if self.token_path.is_file():
            self.token_path.unlink()
        self.clear_state()

    def create_state(self) -> str:
        state = secrets.token_urlsafe(32)
        payload = {
            "state": state,
            "created_at": time.time(),
            "expires_at": time.time() + STATE_TTL_SEC,
        }
        _atomic_write_0600(self.state_path, json.dumps(payload))
        return state

    def validate_state(self, state: str) -> bool:
        """Check CSRF state via constant-time comparison without deleting on mismatch."""
        if not state or not self.state_path.is_file():
            return False
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if not isinstance(data, dict):
            return False
        expected = str(data.get("state") or "")
        if not expected or not hmac.compare_digest(expected, str(state)):
            return False
        try:
            expires_at = float(data.get("expires_at") or 0)
        except (TypeError, ValueError):
            return False
        return time.time() <= expires_at

    def clear_state(self) -> None:
        if self.state_path.is_file():
            self.state_path.unlink()

    def consume_state(self, state: str) -> bool:
        """Validate CSRF state and consume on success (or matching expired state)."""
        if not self.validate_state(state):
            if state and self.state_path.is_file():
                try:
                    data = json.loads(self.state_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return False
                if isinstance(data, dict):
                    expected = str(data.get("state") or "")
                    try:
                        expired = time.time() > float(data.get("expires_at") or 0)
                    except (TypeError, ValueError):
                        expired = False
                    if expected and hmac.compare_digest(expected, str(state)) and expired:
                        self.clear_state()
            return False
        self.clear_state()
        return True

    def public_status(self) -> dict[str, Any]:
        """Return non-secret GSC OAuth connection status (never exposes tokens/secrets)."""
        bundle = self.load()
        return {
            "configured": gsc_oauth_configured(),
            "connected": bool(bundle and (bundle.refresh_token or bundle.access_token)),
            "has_refresh_token": bool(bundle and bundle.refresh_token),
            "expires_at": bundle.expires_at if bundle else None,
            "scope": bundle.scope if bundle else GSC_READONLY_SCOPE,
            "updated_at": bundle.updated_at if bundle else "",
            "redirect_uri": gsc_oauth_redirect_uri(),
            "mock_mode": _is_mock(),
        }


def build_gsc_authorize_url(state: str) -> str:
    """Build Google OAuth2 authorization URL scoped strictly to ``webmasters.readonly``."""
    clean_state = str(state or "").strip()
    if not clean_state:
        raise ValueError("OAuth state is required")
    client_id = gsc_oauth_client_id()
    if not client_id and not _is_mock():
        raise SecurityError("GSC_OAUTH_CLIENT_ID required for GSC OAuth")
    params = {
        "client_id": client_id,
        "redirect_uri": gsc_oauth_redirect_uri(),
        "response_type": "code",
        "scope": GSC_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": clean_state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_gsc_code(
    code: str,
    *,
    store: GscOAuthStore | None = None,
    http_request: HttpRequestFn | None = None,
) -> GscTokenBundle:
    """Exchange authorization code for isolated GSC tokens and save atomically."""
    clean_code = str(code or "").strip()
    if not clean_code:
        raise ValueError("Authorization code is required")
    target_store = store or GscOAuthStore()
    if _is_mock() and http_request is None:
        return target_store.save(
            GscTokenBundle(
                access_token="mock-gsc-access",
                refresh_token="mock-gsc-refresh",
                expires_at=time.time() + 3600,
                token_type="Bearer",
                scope=GSC_READONLY_SCOPE,
            )
        )
    if clean_code == "mock" and http_request is None:
        raise ValueError("Mock OAuth code rejected when AZOM_USE_MOCK is off")

    client_id = gsc_oauth_client_id()
    client_secret = gsc_oauth_client_secret()
    if not client_id or not client_secret:
        raise SecurityError("GSC_OAUTH_CLIENT_ID and GSC_OAUTH_CLIENT_SECRET required")

    req_fn = http_request or pinned_https_request
    form_body = urlencode(
        {
            "code": clean_code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": gsc_oauth_redirect_uri(),
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    try:
        status, _headers, raw, _final_url = req_fn(
            TOKEN_URL,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=form_body,
            connect_timeout=5.0,
            read_timeout=20.0,
            max_redirects=0,
            allowed_auth_origins=(_OAUTH_ORIGIN,),
        )
    except Exception as exc:
        raise SecurityError(
            _redact_error_text(f"GSC OAuth token exchange transport error: {exc}")
        ) from exc

    if status < 200 or status >= 300:
        raise SecurityError(f"GSC token exchange failed (HTTP {status}).")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        raise SecurityError("GSC token response was not valid JSON.") from exc
    if not isinstance(data, dict):
        raise SecurityError("GSC token response had unexpected shape.")

    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise SecurityError("GSC token response missing access_token")

    existing = target_store.load()
    refresh_token = str(data.get("refresh_token") or "").strip() or (
        existing.refresh_token if existing else ""
    )
    try:
        expires_in = float(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600.0
    bundle = GscTokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.time() + max(60.0, expires_in),
        token_type=str(data.get("token_type") or "Bearer").strip() or "Bearer",
        scope=str(data.get("scope") or GSC_READONLY_SCOPE).strip() or GSC_READONLY_SCOPE,
    )
    return target_store.save(bundle)


def refresh_gsc_access_token(
    store: GscOAuthStore | None = None,
    *,
    http_request: HttpRequestFn | None = None,
) -> GscTokenBundle:
    """Refresh stored GSC OAuth access token using the isolated refresh token."""
    target_store = store or GscOAuthStore()
    existing = target_store.load()
    if _is_mock() and http_request is None:
        return target_store.save(
            GscTokenBundle(
                access_token="mock-gsc-access",
                refresh_token=(existing.refresh_token if existing else "") or "mock-gsc-refresh",
                expires_at=time.time() + 3600,
                token_type="Bearer",
                scope=GSC_READONLY_SCOPE,
            )
        )
    if not existing or not existing.refresh_token:
        raise SecurityError("GSC OAuth refresh_token missing")
    client_id = gsc_oauth_client_id()
    client_secret = gsc_oauth_client_secret()
    if not client_id or not client_secret:
        raise SecurityError("GSC_OAUTH_CLIENT_ID and GSC_OAUTH_CLIENT_SECRET required")

    req_fn = http_request or pinned_https_request
    form_body = urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": existing.refresh_token,
        }
    ).encode("utf-8")
    try:
        status, _headers, raw, _final_url = req_fn(
            TOKEN_URL,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=form_body,
            connect_timeout=5.0,
            read_timeout=20.0,
            max_redirects=0,
            allowed_auth_origins=(_OAUTH_ORIGIN,),
        )
    except Exception as exc:
        raise SecurityError(
            _redact_error_text(f"GSC OAuth token refresh transport error: {exc}")
        ) from exc

    if status < 200 or status >= 300:
        raise SecurityError(f"GSC token refresh failed (HTTP {status}).")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        raise SecurityError("GSC refresh response was not valid JSON.") from exc
    if not isinstance(data, dict):
        raise SecurityError("GSC refresh response had unexpected shape.")

    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise SecurityError("GSC refresh response missing access_token")
    try:
        expires_in = float(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600.0
    bundle = GscTokenBundle(
        access_token=access_token,
        refresh_token=str(data.get("refresh_token") or "").strip() or existing.refresh_token,
        expires_at=time.time() + max(60.0, expires_in),
        token_type=str(data.get("token_type") or existing.token_type or "Bearer").strip()
        or "Bearer",
        scope=str(data.get("scope") or existing.scope or GSC_READONLY_SCOPE).strip()
        or GSC_READONLY_SCOPE,
    )
    return target_store.save(bundle)


def list_gsc_properties(
    access_token: str,
    *,
    http_request: HttpRequestFn | None = None,
) -> list[dict[str, str]]:
    """List verified Search Console properties (read-only ``/webmasters/v3/sites``)."""
    if _is_mock() and http_request is None:
        return [
            {"site_url": "sc-domain:azom.no", "permission_level": "siteOwner"},
            {"site_url": "https://azom.no/", "permission_level": "siteOwner"},
            {"site_url": "https://azom.se/", "permission_level": "siteOwner"},
            {"site_url": "https://azom.dk/", "permission_level": "siteOwner"},
        ]
    token = str(access_token or "").strip()
    if not token:
        raise SecurityError("GSC access token required to list properties")

    req_fn = http_request or pinned_https_request
    try:
        status, _headers, raw, _final_url = req_fn(
            GSC_SITES_URL,
            method="GET",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            connect_timeout=5.0,
            read_timeout=15.0,
            max_redirects=0,
            allowed_auth_origins=(_GSC_API_ORIGIN,),
        )
    except Exception as exc:
        raise SecurityError(
            _redact_error_text(f"GSC sites lookup transport error: {exc}")
        ) from exc

    if status < 200 or status >= 300:
        raise SecurityError(f"GSC sites lookup failed (HTTP {status}).")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception as exc:
        raise SecurityError("GSC sites response was not valid JSON.") from exc
    if not isinstance(data, dict):
        return []

    out: list[dict[str, str]] = []
    for entry in list(data.get("siteEntry") or []):
        if not isinstance(entry, dict):
            continue
        site_url = str(entry.get("siteUrl") or "").strip()
        if not site_url:
            continue
        out.append(
            {
                "site_url": site_url,
                "permission_level": str(entry.get("permissionLevel") or "").strip(),
            }
        )
    return out


__all__ = [
    "AUTH_URL",
    "GSC_READONLY_SCOPE",
    "GSC_SITES_URL",
    "TOKEN_URL",
    "GscOAuthStore",
    "GscTokenBundle",
    "build_gsc_authorize_url",
    "exchange_gsc_code",
    "gsc_oauth_client_id",
    "gsc_oauth_client_secret",
    "gsc_oauth_configured",
    "gsc_oauth_redirect_uri",
    "list_gsc_properties",
    "refresh_gsc_access_token",
]
