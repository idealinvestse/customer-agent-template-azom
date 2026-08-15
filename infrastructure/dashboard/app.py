"""Flask dashboard factory — route modules live beside this file."""

from __future__ import annotations

import os
import sys
from datetime import UTC
from pathlib import Path

from flask import Flask, g, request

_ROOT = Path(__file__).resolve().parents[2]
_DASH_DIR = Path(__file__).resolve().parent
if str(_ROOT / "skills") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills"))
if str(_DASH_DIR) not in sys.path:
    sys.path.insert(0, str(_DASH_DIR))

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
from settings_store import apply_env_overlays  # noqa: E402
from webhooks import register_webhook_routes  # noqa: E402


def _case_age(iso: str | None) -> str:
    """Human age from ISO timestamp for case queue."""
    if not iso:
        return ""
    try:
        from datetime import datetime

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
    application = Flask(__name__)
    application.jinja_env.globals["case_age"] = _case_age
    init_auth(application)
    register_health_routes(application)
    register_webhook_routes(application)
    register_core_routes(application)
    register_cases_routes(application)
    register_oscar_routes(application)
    register_marketing_routes(application)
    register_oauth_routes(application)

    @application.before_request
    def _load_overlays():
        apply_env_overlays()

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
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'",
        )
        return resp

    return application


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    app.run(host=host, port=port, debug=False)
