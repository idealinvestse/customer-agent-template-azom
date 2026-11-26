"""Stateless SSH settings routes for WordPress sites."""

from __future__ import annotations

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import _dashboard_context, _flash, _flash_q

from .common import can_save_ssh, file_block, is_denied, wp_service


def _post_ssh(site_id: str):
    result = wp_service().save_ssh_settings(
        site_id,
        actor=g.actor["name"],
        files_root=request.form.get("files_root") or "",
        ssh_host=request.form.get("ssh_host") or "",
        ssh_user=request.form.get("ssh_user") or "",
        ssh_port=request.form.get("ssh_port") or "",
        ssh_identity_file="",
        private_key=request.form.get("ssh_private_key") or "",
        clear_key=(request.form.get("clear_ssh_key") or "") in {"1", "on", "true"},
        global_user="",
        global_port="",
        global_identity_file="",
        host_public_key=request.form.get("host_public_key") or "",
    )
    blocked = file_block(site_id, result)
    if blocked is not None:
        return blocked
    if result.ok:
        _flash(result.message, "ok")
        return redirect(url_for("wp_ssh", site_id=site_id) + f"?{_flash_q(result.message)}")
    _flash(result.message, "error")
    return redirect(url_for("wp_ssh", site_id=site_id) + f"?{_flash_q(err=result.message)}")


def _render_ssh(site_id: str):
    svc = wp_service()
    result = svc.ssh_settings(site_id, actor=g.actor["name"])
    blocked = file_block(site_id, result)
    if blocked is not None:
        return blocked
    shown = svc.show_site(site_id, actor=g.actor["name"])
    if is_denied(shown):
        return Response("Saknar behörighet", 403)
    site = (shown.data or {}).get("site") if shown.ok else None
    ssh = (result.data or {}).get("ssh") if result.ok else None
    error = None if result.ok else result.message
    return render_template(
        "wp_ssh.html",
        **_dashboard_context(
            site=site,
            ssh=ssh,
            can_save=can_save_ssh(),
            error=error,
        ),
    )


def register_ssh_routes(app: Flask) -> None:
    @app.route("/wp/<site_id>/ssh", methods=["GET", "POST"])
    @_auth_required
    def wp_ssh(site_id: str):
        if request.method == "POST":
            return _post_ssh(site_id)
        return _render_ssh(site_id)
