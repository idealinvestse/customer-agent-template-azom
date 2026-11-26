"""Stateless interactive sandbox preview routes with canonical HTML hardening and CSP."""

from __future__ import annotations

import secrets

from flask import Flask, Response, g, request

from auth import _auth_required

from .common import (
    REVIEW_ID_RE,
    SITE_ID_RE,
    can_drill,
    file_admin,
    review_http,
    review_ops,
    scrub_posted,
)
from .review_routes import run_review_action


def harden_dashboard_preview_html(html_text: str, nonce: str) -> str:
    """Harden served preview HTML using canonical leaf sanitizer and direct guard builder."""
    from ecom_ops.integrations.wp.sandbox_html import build_sandbox_guard_script
    from ecom_ops.wp_ops.html_sanitize import harden_preview_html

    return harden_preview_html(
        html_text,
        nonce=nonce,
        guard_script_builder=build_sandbox_guard_script,
    )


def preview_csp_for_site(site_id: str, *, nonce: str = "") -> str:
    """Return opaque sandbox CSP header (never trusts the previewed site's origin)."""
    del site_id
    from ecom_ops.wp_ops.html_sanitize import preview_csp_header

    return preview_csp_header(nonce=nonce)


def _get_review_preview_html(site_id: str, plan_id: str):
    if not SITE_ID_RE.fullmatch(str(site_id or "")) or not REVIEW_ID_RE.fullmatch(str(plan_id or "")):
        return Response("Okänd plan", 404)
    if not file_admin():
        return Response("Saknar behörighet", 403)
    path, err = review_ops().preview_html_file(site_id, plan_id, actor=g.actor["name"])
    if err is not None or path is None:
        blocked = review_http(err) if err is not None else None
        if blocked is not None:
            return blocked
        return Response("Sandbox-förhandsvisning saknas", 404)
    try:
        html_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Response("Sandbox-förhandsvisning saknas", 404)
    nonce = secrets.token_urlsafe(16)
    hardened = harden_dashboard_preview_html(scrub_posted(site_id, html_text), nonce)
    return Response(
        hardened,
        status=200,
        mimetype="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": preview_csp_for_site(site_id, nonce=nonce),
        },
    )


def register_preview_routes(app: Flask) -> None:
    @app.route("/wp/<site_id>/reviews/<plan_id>/preview", methods=["GET", "POST"])
    @_auth_required
    def wp_review_preview(site_id: str, plan_id: str):
        if request.method == "GET":
            return _get_review_preview_html(site_id, plan_id)
        return run_review_action(
            site_id,
            plan_id,
            require_fn=can_drill,
            action_name="preview",
            action_fn=lambda ops: ops.preview(site_id, plan_id, actor=g.actor["name"]),
        )
