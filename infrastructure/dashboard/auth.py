"""Dashboard Basic Auth, CSRF, and login rate limiting."""

from __future__ import annotations

import hashlib
import os
import secrets
from collections import defaultdict
from functools import wraps
from time import time as _time
from typing import Any

from flask import Flask, Response, g, request, session
from werkzeug.security import check_password_hash

from settings_store import apply_env_overlays

_app: Flask | None = None
_LOGIN_WINDOW_SEC = 300
_LOGIN_MAX_FAILURES = 10
_login_failures: dict[str, list[float]] = defaultdict(list)


def init_auth(app: Flask) -> None:
    global _app
    _app = app
    configure_secret_key(app)


def _is_mock() -> bool:
    from ecom_ops.runtime_env import env_flag

    return env_flag("AZOM_USE_MOCK", default=False)


def configure_secret_key(app: Flask | None = None) -> None:
    """Ensure Flask sessions work for CSRF tokens."""
    apply_env_overlays()
    target = app or _app
    if target is None:
        from flask import current_app

        target = current_app
    key = os.environ.get("DASHBOARD_SECRET_KEY", "").strip()
    if not key:
        material = (
            os.environ.get("DASHBOARD_PASSWORD_HASH")
            or os.environ.get("DASHBOARD_OSCAR_PASSWORD_HASH")
            or os.environ.get("DASHBOARD_PASSWORD")
            or os.environ.get("DASHBOARD_OSCAR_PASSWORD")
            or ""
        )
        if material:
            key = hashlib.sha256(f"azom-dash:{material}".encode()).hexdigest()
        elif _is_mock():
            key = "azom-mock-dashboard-secret"
        else:
            key = secrets.token_hex(32)
    target.secret_key = key


def _configure_secret_key() -> None:
    configure_secret_key()


def _password_ok(expected_plain: str, expected_hash: str, password: str) -> bool:
    if expected_hash:
        if expected_hash.startswith(("pbkdf2:", "scrypt:", "argon2:")):
            try:
                return check_password_hash(expected_hash, password)
            except Exception:
                return False
        got = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(got, expected_hash)
    if expected_plain:
        return secrets.compare_digest(password, expected_plain)
    return False


def _authenticate(username: str, password: str) -> dict | None:
    """Return actor dict or None. Mock default passwords only when AZOM_USE_MOCK."""
    apply_env_overlays()
    user = (username or "").strip().lower()
    try:
        from ecom_ops.profile import load_profile

        prof = load_profile()
        viewer_name, admin_name = prof.actor_usernames()
    except Exception:
        viewer_name, admin_name = "jonatan", "oscar"
    if user == viewer_name:
        plain = os.environ.get("DASHBOARD_PASSWORD", "").strip()
        hashed = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
        if _password_ok(plain, hashed, password):
            return {"name": viewer_name, "role": "viewer", "is_oscar": False}
        if not plain and not hashed and _is_mock() and password == viewer_name:
            return {"name": viewer_name, "role": "viewer", "is_oscar": False}
        return None
    if user == admin_name:
        plain = os.environ.get("DASHBOARD_OSCAR_PASSWORD", "").strip()
        hashed = os.environ.get("DASHBOARD_OSCAR_PASSWORD_HASH", "").strip()
        if _password_ok(plain, hashed, password):
            return {"name": admin_name, "role": "full_admin", "is_oscar": True}
        if not plain and not hashed and _is_mock() and password == admin_name:
            return {"name": admin_name, "role": "full_admin", "is_oscar": True}
        return None
    return None


def _dashboard_actor(actor_dict: dict) -> Any:
    from ecom_ops.rbac import Actor as _Actor

    return _Actor(name=actor_dict["name"], role=actor_dict["role"])


def _ensure_csrf_token() -> str:
    _configure_secret_key()
    tok = session.get("csrf_token")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf_token"] = tok
    return str(tok)


def _validate_csrf() -> Response | None:
    expected = session.get("csrf_token")
    got = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("_csrf")
        or ""
    )
    if not got:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            got = str(body.get("_csrf") or "")
    if (
        not expected
        or not got
        or not secrets.compare_digest(str(got), str(expected))
    ):
        return Response("CSRF validation failed", 400)
    return None


def _login_rate_limited(ip: str) -> bool:
    now = _time()
    cutoff = now - _LOGIN_WINDOW_SEC
    recent = [t for t in _login_failures[ip] if t > cutoff]
    _login_failures[ip] = recent
    return len(recent) >= _LOGIN_MAX_FAILURES


def _record_login_failure(ip: str) -> None:
    _login_failures[ip].append(_time())


def _auth_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        from datetime import UTC, datetime, timedelta

        _configure_secret_key()
        auth = request.authorization
        actor = None
        if auth is not None:
            ip = request.remote_addr or "unknown"
            if _login_rate_limited(ip):
                return Response(
                    "Too many failed login attempts. Try again later.",
                    429,
                    {"Retry-After": str(_LOGIN_WINDOW_SEC)},
                )
            actor = _authenticate(auth.username or "", auth.password or "")
            if actor is None:
                _record_login_failure(ip)
        if actor is None:
            return Response(
                "Authentication required (jonatan or oscar)",
                401,
                {"WWW-Authenticate": 'Basic realm="Azom Dashboard"'},
            )
        g.actor = actor
        _ensure_csrf_token()
        session_timeout_h = int(os.environ.get("DASHBOARD_SESSION_TIMEOUT_H", "8"))
        last_seen = session.get("_last_seen")
        now = datetime.now(UTC)
        if last_seen:
            try:
                ls = datetime.fromisoformat(last_seen)
                if ls.tzinfo is None:
                    ls = ls.replace(tzinfo=UTC)
                if now - ls > timedelta(hours=session_timeout_h):
                    session.clear()
                    return Response(
                        "Session expired — please re-authenticate",
                        401,
                        {"WWW-Authenticate": 'Basic realm="Azom Dashboard"'},
                    )
            except Exception:
                pass
        session["_last_seen"] = now.isoformat()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            failed = _validate_csrf()
            if failed is not None:
                return failed
        return view(*args, **kwargs)

    return wrapper


def _oscar_required(view):
    @wraps(view)
    @_auth_required
    def wrapper(*args, **kwargs):
        if not g.actor.get("is_oscar"):
            return Response("Oscar only", 403)
        return view(*args, **kwargs)

    return wrapper
