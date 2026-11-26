"""Stateless site list, detail, check, backup, and restore-drill routes."""

from __future__ import annotations

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import _dashboard_context, _flash, _flash_q

from .common import can_drill, can_run, file_admin, is_denied, redirect_site, wp_service
from .review_routes import site_review_plans


def register_sites_routes(app: Flask) -> None:
    @app.route("/wp")
    @_auth_required
    def wp_sites():
        result = wp_service().list_sites(actor=g.actor["name"])
        if is_denied(result):
            return Response("Saknar behörighet", 403)
        sites = (result.data or {}).get("sites") or []
        return render_template(
            "wp_sites.html",
            **_dashboard_context(sites=sites, error=None if result.ok else result.message),
        )

    @app.route("/wp/<site_id>")
    @_auth_required
    def wp_site_detail(site_id: str):
        result = wp_service().show_site(site_id, actor=g.actor["name"])
        if is_denied(result):
            return Response("Saknar behörighet", 403)
        if not result.ok and (result.data or {}).get("error_class") == "UnknownSite":
            return Response("Okänd webbplats", 404)
        data = result.data or {}
        site = data.get("site")
        review_plans = site_review_plans(site_id, actor=g.actor["name"], admin=file_admin()) if site else []
        return render_template(
            "wp_site.html",
            **_dashboard_context(
                site=site,
                checks=data.get("checks") or [],
                incidents=data.get("incidents") or [],
                backups=data.get("backups") or [],
                drills=data.get("drills") or [],
                staging_targets=data.get("staging_targets") or [],
                review_plans=review_plans,
                can_run=can_run(site),
                can_drill=can_drill(),
                error=None if result.ok else result.message,
            ),
        )

    @app.route("/wp/<site_id>/check", methods=["POST"])
    @_auth_required
    def wp_site_check(site_id: str):
        result = wp_service().run_checks(site_id, actor=g.actor["name"])
        if is_denied(result):
            return Response("Saknar behörighet", 403)
        if not result.ok and (result.data or {}).get("error_class") == "UnknownSite":
            return Response("Okänd webbplats", 404)
        _flash(result.message, "ok" if result.ok else "error")
        frag = _flash_q(result.message) if result.ok else _flash_q(err=result.message)
        return redirect(url_for("wp_site_detail", site_id=site_id) + f"?{frag}")

    @app.route("/wp/<site_id>/backup", methods=["POST"])
    @_auth_required
    def wp_site_backup(site_id: str):
        result = wp_service().create_backup(site_id, actor=g.actor["name"])
        return redirect_site(site_id, result)

    @app.route("/wp/<site_id>/restore-drill", methods=["POST"])
    @_auth_required
    def wp_restore_drill(site_id: str):
        result = wp_service().restore_drill(
            request.form.get("backup_id") or "",
            target_site_id=request.form.get("target_site_id") or "",
            actor=g.actor["name"],
        )
        return redirect_site(site_id, result)
