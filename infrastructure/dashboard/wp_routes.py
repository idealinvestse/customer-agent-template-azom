"""WordPress operations pages. Checks and mock backups do not call customer sites."""

from __future__ import annotations

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import _dashboard_context, _flash_q


def register_wp_routes(app: Flask) -> None:
    @app.route("/wp")
    @_auth_required
    def wp_sites():
        from ecom_ops.wp_ops import WpOpsService

        result = WpOpsService().list_sites(actor=g.actor["name"])
        if _denied(result):
            return Response("Saknar behörighet", 403)
        sites = (result.data or {}).get("sites") or []
        return render_template(
            "wp_sites.html",
            **_dashboard_context(sites=sites, error=None if result.ok else result.message),
        )

    @app.route("/wp/<site_id>")
    @_auth_required
    def wp_site_detail(site_id: str):
        from ecom_ops.wp_ops import WpOpsService

        result = WpOpsService().show_site(site_id, actor=g.actor["name"])
        if _denied(result):
            return Response("Saknar behörighet", 403)
        if not result.ok and (result.data or {}).get("error_class") == "UnknownSite":
            return Response("Okänd webbplats", 404)
        data = result.data or {}
        site = data.get("site")
        return render_template(
            "wp_site.html",
            **_dashboard_context(
                site=site,
                checks=data.get("checks") or [],
                incidents=data.get("incidents") or [],
                backups=data.get("backups") or [],
                drills=data.get("drills") or [],
                staging_targets=data.get("staging_targets") or [],
                can_run=_can_run(site),
                can_drill=_can_drill(),
                error=None if result.ok else result.message,
            ),
        )

    @app.route("/wp/<site_id>/check", methods=["POST"])
    @_auth_required
    def wp_site_check(site_id: str):
        from ecom_ops.wp_ops import WpOpsService

        result = WpOpsService().run_checks(site_id, actor=g.actor["name"])
        if _denied(result):
            return Response("Saknar behörighet", 403)
        if not result.ok and (result.data or {}).get("error_class") == "UnknownSite":
            return Response("Okänd webbplats", 404)
        frag = _flash_q(result.message) if result.ok else _flash_q(err=result.message)
        return redirect(url_for("wp_site_detail", site_id=site_id) + f"?{frag}")

    @app.route("/wp/<site_id>/backup", methods=["POST"])
    @_auth_required
    def wp_site_backup(site_id: str):
        from ecom_ops.wp_ops import WpOpsService

        result = WpOpsService().create_backup(site_id, actor=g.actor["name"])
        return _redirect_site(site_id, result)

    @app.route("/wp/<site_id>/restore-drill", methods=["POST"])
    @_auth_required
    def wp_restore_drill(site_id: str):
        from ecom_ops.wp_ops import WpOpsService

        result = WpOpsService().restore_drill(
            request.form.get("backup_id") or "",
            target_site_id=request.form.get("target_site_id") or "",
            actor=g.actor["name"],
        )
        return _redirect_site(site_id, result)


def _redirect_site(site_id: str, result: object):
    if _denied(result):
        return Response("Saknar behörighet", 403)
    data = getattr(result, "data", None) or {}
    if not getattr(result, "ok", False) and data.get("error_class") == "UnknownSite":
        return Response("Okänd webbplats", 404)
    message = getattr(result, "message", "")
    frag = _flash_q(message) if getattr(result, "ok", False) else _flash_q(err=message)
    return redirect(url_for("wp_site_detail", site_id=site_id) + f"?{frag}")


def _denied(result: object) -> bool:
    data = getattr(result, "data", None) or {}
    return data.get("error_class") == "AccessDenied"


def _can_run(site: dict | None) -> bool:
    if not site:
        return False
    from ecom_ops.rbac import Actor, Permission

    actor = Actor(name=g.actor["name"], role=g.actor["role"])
    if site.get("environment") == "production":
        return actor.has(Permission.WP_OPS_ADMIN)
    return actor.has(Permission.WP_OPS_OPERATE)


def _can_drill() -> bool:
    from ecom_ops.rbac import Actor, Permission

    actor = Actor(name=g.actor["name"], role=g.actor["role"])
    return actor.has(Permission.WP_OPS_OPERATE)
