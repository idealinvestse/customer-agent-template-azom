"""Dashboard Session + Basic Auth, CSRF, and login rate limiting."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import wraps
from time import time as _time
from typing import Any
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    abort,
    g,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from settings_store import apply_env_overlays

_app: Flask | None = None
_LOGIN_WINDOW_SEC = 300
_LOGIN_MAX_FAILURES = 10
_SESSION_HOUR_CHOICES = (8, 24, 168)
_login_failures: dict[str, list[float]] = defaultdict(list)
_login_failures_lock = threading.Lock()


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
        if _is_mock():
            key = "azom-mock-dashboard-secret"
        else:
            existing = target.secret_key if isinstance(target.secret_key, str) else ""
            if existing and existing != "azom-mock-dashboard-secret":
                key = existing
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
    return _rate_limit_retry_after(ip) > 0


def _rate_limit_retry_after(ip: str) -> int:
    now = _time()
    cutoff = now - _LOGIN_WINDOW_SEC
    with _login_failures_lock:
        recent = [t for t in _login_failures[ip] if t > cutoff]
        _login_failures[ip] = recent
        if len(recent) < _LOGIN_MAX_FAILURES:
            return 0
        oldest = min(recent)
        return max(1, int(round((oldest + _LOGIN_WINDOW_SEC) - now)))


def _record_login_failure(ip: str) -> None:
    now = _time()
    cutoff = now - _LOGIN_WINDOW_SEC
    with _login_failures_lock:
        if len(_login_failures) > 1000:
            stale = [
                key
                for key, stamps in _login_failures.items()
                if not any(t > cutoff for t in stamps)
            ]
            for key in stale:
                _login_failures.pop(key, None)
        _login_failures[ip].append(now)


def _clear_login_failures(ip: str) -> None:
    with _login_failures_lock:
        _login_failures.pop(ip, None)


def _safe_next_url(raw: str | None) -> str:
    candidate = (raw or "").strip()
    if not candidate or "\\" in candidate:
        return "/"
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/"
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    if path in {"/login", "/logout"} or path.startswith(("/login/", "/logout/")):
        return "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _is_browser_html_request() -> bool:
    if request.method != "GET":
        return False
    if request.is_json:
        return False
    if (request.headers.get("Authorization") or "").strip():
        return False
    if (request.headers.get("X-Requested-With") or "").strip().lower() == "xmlhttprequest":
        return False
    accept = (request.headers.get("Accept") or "").lower()
    return "text/html" in accept


def _default_session_hours() -> int:
    try:
        return max(1, min(168, int(os.environ.get("DASHBOARD_SESSION_TIMEOUT_H", "8"))))
    except ValueError:
        return 8


def _session_actor() -> tuple[dict | None, bool]:
    """Return ``(actor_dict, expired)`` from the signed Flask session cookie."""
    uname = str(session.get("_auth_user") or "").strip().lower()
    if not uname:
        return None, False

    try:
        from ecom_ops.profile import load_profile

        viewer_name, admin_name = load_profile().actor_usernames()
    except Exception:
        viewer_name, admin_name = "jonatan", "oscar"

    if uname == admin_name:
        actor = {"name": admin_name, "role": "full_admin", "is_oscar": True}
    elif uname == viewer_name:
        actor = {"name": viewer_name, "role": "viewer", "is_oscar": False}
    else:
        session.clear()
        return None, False

    try:
        max_hours = max(1, min(168, int(session.get("_session_max_hours") or _default_session_hours())))
    except (TypeError, ValueError):
        max_hours = _default_session_hours()

    now = datetime.now(UTC)
    last_seen = session.get("_last_seen")
    if last_seen:
        try:
            ls = datetime.fromisoformat(str(last_seen))
            if ls.tzinfo is None:
                ls = ls.replace(tzinfo=UTC)
            if now - ls > timedelta(hours=max_hours):
                session.clear()
                return None, True
        except Exception:
            session.clear()
            return None, True

    session["_last_seen"] = now.isoformat()
    return actor, False


def _establish_session(actor: dict, *, remember_hours: int | None = None) -> None:
    """Create a fresh authenticated session (preventing session fixation)."""
    session.clear()
    hours = (
        max(1, min(168, int(remember_hours)))
        if remember_hours is not None
        else _default_session_hours()
    )
    session["_auth_user"] = actor["name"]
    session["_auth_role"] = actor["role"]
    session["_auth_is_oscar"] = bool(actor.get("is_oscar"))
    session["_auth_method"] = "session"
    session["_session_max_hours"] = hours
    session["_last_seen"] = datetime.now(UTC).isoformat()
    session.permanent = hours > _default_session_hours()
    _ensure_csrf_token()


def _auth_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        _configure_secret_key()
        actor, session_expired = _session_actor()
        if actor is not None:
            g.actor = actor
            _ensure_csrf_token()
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                failed = _validate_csrf()
                if failed is not None:
                    return failed
            return view(*args, **kwargs)

        auth = request.authorization
        if auth is not None:
            ip = request.remote_addr or "unknown"
            retry_after = _rate_limit_retry_after(ip)
            if retry_after > 0:
                return Response(
                    "Too many failed login attempts. Try again later.",
                    429,
                    {"Retry-After": str(retry_after)},
                )
            actor = _authenticate(auth.username or "", auth.password or "")
            if actor is None:
                _record_login_failure(ip)
        if actor is None:
            if _is_browser_html_request():
                next_raw = (
                    request.full_path.rstrip("?")
                    if request.query_string
                    else request.path
                )
                target_params: dict[str, str] = {"next": _safe_next_url(next_raw)}
                if session_expired:
                    target_params["expired"] = "1"
                return redirect(url_for("login", **target_params))
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Azom Dashboard"'},
            )
        g.actor = actor
        _ensure_csrf_token()
        session_timeout_h = _default_session_hours()
        last_seen = session.get("_last_seen")
        now = datetime.now(UTC)
        if last_seen:
            try:
                ls = datetime.fromisoformat(str(last_seen))
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
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def _login_view_context(
    *,
    next_url: str,
    username: str = "",
    error_message: str = "",
    info_message: str = "",
    retry_after: int = 0,
    session_hours: int = 8,
) -> dict[str, Any]:
    try:
        from ecom_ops import __version__ as pkg_version
    except Exception:
        pkg_version = "3.0.0"
    try:
        from ecom_ops.profile import load_profile

        prof = load_profile()
        viewer_name, admin_name = prof.actor_usernames()
        brand_name = getattr(prof, "brand_name", "Azom") or "Azom"
    except Exception:
        viewer_name, admin_name, brand_name = "jonatan", "oscar", "Azom"

    is_https = bool(
        request.is_secure
        or (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https"
    )
    selected_hours = session_hours if session_hours in _SESSION_HOUR_CHOICES else 8
    return {
        "brand_name": brand_name,
        "version": pkg_version,
        "mock_mode": _is_mock(),
        "host_label": request.host or "dashboard",
        "is_https": is_https,
        "viewer_name": viewer_name,
        "admin_name": admin_name,
        "next_url": next_url,
        "username": username,
        "error_message": error_message,
        "info_message": info_message,
        "retry_after": max(0, int(retry_after)),
        "session_hours": selected_hours,
        "csrf_token": _ensure_csrf_token(),
    }


def register_auth_routes(app: Flask) -> None:
    """Register modern session login/logout routes while keeping Basic Auth active."""

    @app.route("/login", methods=["GET", "POST"])
    def login():
        configure_secret_key(app)
        next_url = _safe_next_url(
            request.values.get("next") or request.args.get("next")
        )

        if request.method == "GET":
            actor, expired = _session_actor()
            if actor is not None:
                return redirect(next_url)
            info_msg = ""
            if request.args.get("logged_out") == "1":
                info_msg = "Du har loggats ut. Sessionen är rensad."
            elif expired or request.args.get("expired") == "1":
                info_msg = "Din session gick ut av säkerhetsskäl. Logga in igen."
            ip = request.remote_addr or "unknown"
            retry_after = _rate_limit_retry_after(ip)
            resp = make_response(
                render_template(
                    "login.html",
                    **_login_view_context(
                        next_url=next_url,
                        username=(request.args.get("user") or "").strip(),
                        info_message=info_msg,
                        retry_after=retry_after,
                    ),
                ),
                200,
            )
            resp.headers["Cache-Control"] = "no-store"
            return resp

        # POST /login
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        try:
            session_hours = int(request.form.get("session_hours") or _default_session_hours())
        except ValueError:
            session_hours = _default_session_hours()
        if session_hours not in _SESSION_HOUR_CHOICES:
            session_hours = _default_session_hours()

        csrf_err = _validate_csrf()
        if csrf_err is not None:
            if "text/html" in (request.headers.get("Accept") or "").lower():
                resp = make_response(
                    render_template(
                        "login.html",
                        **_login_view_context(
                            next_url=next_url,
                            username=username,
                            error_message="Säkerhetstoken gick ut. Försök logga in igen.",
                            session_hours=session_hours,
                        ),
                    ),
                    400,
                )
                resp.headers["Cache-Control"] = "no-store"
                return resp
            return csrf_err

        ip = request.remote_addr or "unknown"
        retry_after = _rate_limit_retry_after(ip)
        if retry_after > 0:
            resp = make_response(
                render_template(
                    "login.html",
                    **_login_view_context(
                        next_url=next_url,
                        username=username,
                        error_message=(
                            f"För många misslyckade inloggningsförsök. Vänta {retry_after} s och försök igen."
                        ),
                        retry_after=retry_after,
                        session_hours=session_hours,
                    ),
                ),
                429,
            )
            resp.headers["Retry-After"] = str(retry_after)
            resp.headers["Cache-Control"] = "no-store"
            return resp

        actor = _authenticate(username, password)
        if actor is None:
            _record_login_failure(ip)
            retry_after = _rate_limit_retry_after(ip)
            status_code = 429 if retry_after > 0 else 401
            err_msg = (
                f"För många misslyckade inloggningsförsök. Vänta {retry_after} s och försök igen."
                if retry_after > 0
                else "Fel användarnamn eller lösenord. Kontrollera uppgifterna och försök igen."
            )
            resp = make_response(
                render_template(
                    "login.html",
                    **_login_view_context(
                        next_url=next_url,
                        username=username,
                        error_message=err_msg,
                        retry_after=retry_after,
                        session_hours=session_hours,
                    ),
                ),
                status_code,
            )
            if retry_after > 0:
                resp.headers["Retry-After"] = str(retry_after)
            resp.headers["Cache-Control"] = "no-store"
            return resp

        _clear_login_failures(ip)
        _establish_session(actor, remember_hours=session_hours)
        return redirect(next_url, code=303)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        configure_secret_key(app)
        if request.method == "GET":
            actor, _ = _session_actor()
            return redirect("/" if actor is not None else url_for("login"))
        csrf_err = _validate_csrf()
        if csrf_err is not None:
            return csrf_err
        session.clear()
        return redirect(url_for("login", logged_out="1"), code=303)
