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
    from ecom_ops.wp_ops.ssh_settings import load_deploy_overlay

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
    deploy_res = svc.deploy_ssh_settings(site_id, actor=g.actor["name"])
    deploy_ssh = dict((deploy_res.data or {}).get("deploy_ssh") or {}) if deploy_res.ok else None
    can_save = can_save_ssh()
    if deploy_ssh is not None and can_save and not deploy_ssh.get("host_public_key"):
        overlay_row = load_deploy_overlay(svc.data_dir).get(site_id, {})
        if isinstance(overlay_row, dict) and overlay_row.get("host_public_key"):
            deploy_ssh["host_public_key"] = str(overlay_row["host_public_key"])
    error = None if result.ok else result.message
    return render_template(
        "wp_ssh.html",
        **_dashboard_context(
            site=site,
            ssh=ssh,
            deploy_ssh=deploy_ssh,
            can_save=can_save,
            error=error,
        ),
    )


def _post_deploy_ssh(site_id: str):
    from ecom_ops.wp_ops.ssh_settings import load_deploy_overlay

    if not (can_save_ssh() and g.actor.get("is_oscar")):
        return Response("Saknar behörighet", 403)
    svc = wp_service()
    submitted_host_key = (request.form.get("host_public_key") or "").strip()
    if not submitted_host_key:
        overlay_row = load_deploy_overlay(svc.data_dir).get(site_id, {})
        if isinstance(overlay_row, dict):
            submitted_host_key = str(overlay_row.get("host_public_key") or "").strip()
    result = svc.save_deploy_ssh_settings(
        site_id,
        actor=g.actor["name"],
        deploy_ssh_user=request.form.get("deploy_ssh_user") or "",
        files_root=request.form.get("files_root") or "",
        ssh_host=request.form.get("ssh_host") or "",
        ssh_port=request.form.get("ssh_port") or "",
        host_public_key=submitted_host_key,
        private_key=request.form.get("deploy_private_key") or "",
        clear_key=(request.form.get("clear_deploy_key") or "") in {"1", "on", "true"},
    )
    blocked = file_block(site_id, result)
    if blocked is not None:
        return blocked
    if result.ok:
        _flash(result.message, "ok")
        return redirect(url_for("wp_ssh", site_id=site_id) + f"?{_flash_q(result.message)}")
    _flash(result.message, "error")
    return redirect(url_for("wp_ssh", site_id=site_id) + f"?{_flash_q(err=result.message)}")


def register_ssh_routes(app: Flask) -> None:
    @app.route("/wp/<site_id>/ssh", methods=["GET", "POST"])
    @_auth_required
    def wp_ssh(site_id: str):
        if request.method == "POST":
            return _post_ssh(site_id)
        return _render_ssh(site_id)

    @app.route("/wp/<site_id>/ssh/deploy", methods=["POST"])
    @_auth_required
    def wp_ssh_deploy(site_id: str):
        return _post_deploy_ssh(site_id)
