"""Flask dashboard factory — route modules live beside this file."""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Flask, Response, g, render_template, request, send_from_directory

_ROOT = Path(__file__).resolve().parents[2]
_DASH_DIR = Path(__file__).resolve().parent
if str(_ROOT / "skills") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills"))
if str(_DASH_DIR) not in sys.path:
    sys.path.insert(0, str(_DASH_DIR))

from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from auth import (  # noqa: E402, F401
    _authenticate,
    _configure_secret_key,
    _ensure_csrf_token,
    init_auth,
)
from cases_routes import register_cases_routes  # noqa: E402
from core_routes import register_core_routes  # noqa: E402
from health_routes import register_health_routes  # noqa: E402
from marketing_routes import register_marketing_routes  # noqa: E402
from oauth_routes import register_oauth_routes  # noqa: E402
from oscar_routes import register_oscar_routes  # noqa: E402
from research_routes import register_research_routes  # noqa: E402
from settings_store import apply_env_overlays  # noqa: E402
from support import _fmt_dt  # noqa: E402
from webhooks import register_webhook_routes  # noqa: E402
from wp_routes import register_wp_routes  # noqa: E402


def _fmt_usd(value: Any) -> str:
    """Format USD cost consistently with tabular precision."""
    if value is None or value == "" or isinstance(value, bool):
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(num):
        return "—"
    if num == 0:
        return "$0.00"
    if abs(num) < 0.01:
        return f"${num:.4f}"
    return f"${num:.2f}"


def _fmt_json(value: Any) -> str:
    """Pretty-print dict/list for collapsible inspection."""
    if value is None or value == "" or value == {} or value == []:
        return ""
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _fmt_sv_bool(value: Any) -> str:
    """Format boolean in Swedish."""
    if value is None:
        return "—"
    return "Ja" if bool(value) else "Nej"


def _url_quote(value: Any) -> str:
    """Percent-encode a query parameter value."""
    return quote(str(value or ""), safe="")


def _case_age(iso: str | None) -> str:
    """Human age from ISO timestamp for case queue."""
    if not iso:
        return ""
    try:
        raw = str(iso).replace("Z", "+00:00")
        created = datetime.fromisoformat(raw)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - created
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"
    except Exception:
        return ""


def create_app() -> Flask:
    apply_env_overlays()
    application = Flask(__name__)
    application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=1)
    helpers = {
        "case_age": _case_age,
        "fmt_dt": _fmt_dt,
        "fmt_usd": _fmt_usd,
        "fmt_json": _fmt_json,
        "fmt_sv_bool": _fmt_sv_bool,
        "url_quote": _url_quote,
    }
    application.jinja_env.globals.update(helpers)
    application.jinja_env.filters.update(helpers)

    from ecom_ops.runtime_env import env_flag

    live = not env_flag("AZOM_USE_MOCK", default=True)
    cookie_raw = os.environ.get("DASHBOARD_COOKIE_SECURE", "").strip().lower()
    if cookie_raw in {"1", "true", "yes", "on"}:
        cookie_secure = True
    elif cookie_raw in {"0", "false", "no", "off"}:
        cookie_secure = False
    else:
        cookie_secure = live
    application.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cookie_secure,
    )
    init_auth(application)
    register_health_routes(application)
    register_webhook_routes(application)
    register_core_routes(application)
    register_cases_routes(application)
    register_oscar_routes(application)
    register_marketing_routes(application)
    register_wp_routes(application)
    register_oauth_routes(application)
    register_research_routes(application)

    @application.context_processor
    def _inject_csrf() -> dict[str, str]:
        try:
            if getattr(g, "actor", None):
                return {"csrf_token": _ensure_csrf_token()}
        except Exception:
            pass
        return {"csrf_token": ""}

    @application.context_processor
    def _inject_null_send_banner() -> dict[str, bool]:
        try:
            from ecom_ops.runtime_profile import null_send_active

            return {"null_send_active": bool(null_send_active())}
        except Exception:
            return {"null_send_active": False}

    @application.route("/favicon.ico")
    def favicon():
        static_dir = _DASH_DIR / "static"
        svg = static_dir / "favicon.svg"
        if svg.is_file():
            return send_from_directory(
                str(static_dir), "favicon.svg", mimetype="image/svg+xml"
            )
        return Response("", 204)

    def _render_http_error(status: int, title: str, message: str):
        if (
            getattr(g, "actor", None)
            and not request.is_json
            and request.accept_mimetypes.best != "application/json"
        ):
            try:
                from support import _dashboard_context

                return (
                    render_template(
                        "error.html",
                        **_dashboard_context(
                            status_code=status,
                            code=status,
                            title=title,
                            message=message,
                            description=message,
                            back_url="/",
                            error_code=status,
                            error_title=title,
                            error_message=message,
                        ),
                    ),
                    status,
                )
            except Exception:
                pass
        return Response(message, status, mimetype="text/plain")

    @application.errorhandler(403)
    def _handle_403(_exc):
        return _render_http_error(
            403,
            "Åtkomst nekad",
            "Du saknar behörighet för den här sidan.",
        )

    @application.errorhandler(404)
    def _handle_404(_exc):
        return _render_http_error(
            404,
            "Sidan hittades inte",
            "Sidan eller resursen du sökte finns inte.",
        )

    @application.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'",
        )
        return resp

    return application


def _resolve_dashboard_threads(raw: str | None = None) -> int:
    text = (os.environ.get("DASHBOARD_THREADS") if raw is None else raw) or ""
    try:
        parsed = int(str(text).strip())
    except (TypeError, ValueError):
        return 12
    return max(4, min(32, parsed))


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    threads = _resolve_dashboard_threads()
    try:
        from waitress import serve

        serve(app, host=host, port=port, threads=threads)
    except ImportError:
        app.run(host=host, port=port, debug=False)
