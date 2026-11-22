"""WordPress operations pages. Checks and mock backups do not call customer sites.

The file page edits one existing theme or plugin file. It has no command field.
Opening a site does not read those files.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import _dashboard_context, _flash_q

_PLAN_ID = re.compile(r"^fe-[a-f0-9]{12}$")
_KINDS = frozenset({"theme", "plugin"})


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

    @app.route("/wp/<site_id>/ssh", methods=["GET", "POST"])
    @_auth_required
    def wp_ssh(site_id: str):
        if request.method == "POST":
            return _post_ssh(site_id)
        return _render_ssh(site_id)

    @app.route("/wp/<site_id>/files")
    @_auth_required
    def wp_files(site_id: str):
        return _render_files(site_id)

    @app.route("/wp/<site_id>/files/propose", methods=["POST"])
    @_auth_required
    def wp_file_propose(site_id: str):
        from ecom_ops.wp_ops import WpOpsService

        kind = request.form.get("kind") or ""
        slug = request.form.get("slug") or ""
        relative = request.form.get("path") or ""
        content = request.form.get("content") or ""
        result = WpOpsService().propose_file(
            site_id,
            kind,
            slug,
            relative,
            content,
            actor=g.actor["name"],
        )
        blocked = _file_block(site_id, result)
        if blocked is not None:
            return blocked
        if not result.ok:
            return redirect(_files_location(site_id, kind, slug, relative, err=result.message))
        plan = str(((result.data or {}).get("plan") or {}).get("id") or "")
        return _render_files(
            site_id,
            kind=kind,
            slug=slug,
            relative=relative,
            draft=_scrub_posted(site_id, content),
            plan=plan if _PLAN_ID.fullmatch(plan) else "",
            notice="proposed",
        )

    @app.route("/wp/<site_id>/files/apply", methods=["POST"])
    @_auth_required
    def wp_file_apply(site_id: str):
        from ecom_ops.wp_ops import WpOpsService

        kind = request.form.get("kind") or ""
        slug = request.form.get("slug") or ""
        relative = request.form.get("path") or ""
        result = WpOpsService().apply_file(
            site_id,
            request.form.get("plan_id") or "",
            request.form.get("content") or "",
            actor=g.actor["name"],
        )
        blocked = _file_block(site_id, result)
        if blocked is not None:
            return blocked
        plan = request.form.get("plan_id") or ""
        if result.ok and _PLAN_ID.fullmatch(plan):
            return redirect(
                _files_location(site_id, kind, slug, relative, plan=plan, notice="applied")
            )
        return redirect(_files_location(site_id, kind, slug, relative, err=result.message))


def _post_ssh(site_id: str):
    from ecom_ops.wp_ops import WpOpsService

    result = WpOpsService().save_ssh_settings(
        site_id,
        actor=g.actor["name"],
        files_root=request.form.get("files_root") or "",
        ssh_host=request.form.get("ssh_host") or "",
        ssh_user=request.form.get("ssh_user") or "",
        ssh_port=request.form.get("ssh_port") or "",
        ssh_identity_file=request.form.get("ssh_identity_file") or "",
        private_key=request.form.get("ssh_private_key") or "",
        clear_key=(request.form.get("clear_ssh_key") or "") in {"1", "on", "true"},
        global_user=request.form.get("global_ssh_user") or "",
        global_port=request.form.get("global_ssh_port") or "",
        global_identity_file=request.form.get("global_ssh_identity_file") or "",
        host_public_key=request.form.get("host_public_key") or "",
    )
    blocked = _file_block(site_id, result)
    if blocked is not None:
        return blocked
    if result.ok:
        return redirect(url_for("wp_ssh", site_id=site_id) + f"?{_flash_q(result.message)}")
    return redirect(url_for("wp_ssh", site_id=site_id) + f"?{_flash_q(err=result.message)}")


def _render_ssh(site_id: str):
    from ecom_ops.wp_ops import WpOpsService

    result = WpOpsService().ssh_settings(site_id, actor=g.actor["name"])
    blocked = _file_block(site_id, result)
    if blocked is not None:
        return blocked
    shown = WpOpsService().show_site(site_id, actor=g.actor["name"])
    if _denied(shown):
        return Response("Saknar behörighet", 403)
    site = (shown.data or {}).get("site") if shown.ok else None
    ssh = (result.data or {}).get("ssh") if result.ok else None
    error = None if result.ok else result.message
    return render_template(
        "wp_ssh.html",
        **_dashboard_context(
            site=site,
            ssh=ssh,
            can_save=_can_save_ssh(),
            error=error,
        ),
    )


def _can_save_ssh() -> bool:
    from ecom_ops.rbac import Actor, Permission

    actor = Actor(name=g.actor["name"], role=g.actor["role"])
    return actor.has(Permission.WP_OPS_ADMIN)


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


def _render_files(
    site_id: str,
    *,
    kind: str | None = None,
    slug: str | None = None,
    relative: str | None = None,
    draft: str | None = None,
    plan: str = "",
    notice: str = "",
):
    from ecom_ops.wp_ops import WpOpsService

    shown = WpOpsService().show_site(site_id, actor=g.actor["name"])
    if _denied(shown):
        return Response("Saknar behörighet", 403)
    if not shown.ok and (shown.data or {}).get("error_class") == "UnknownSite":
        return Response("Okänd webbplats", 404)
    site = (shown.data or {}).get("site")
    selected_kind = kind if kind is not None else (request.args.get("kind") or "")
    selected_slug = slug if slug is not None else (request.args.get("slug") or "")
    selected_path = relative if relative is not None else (request.args.get("path") or "")
    if selected_kind not in _KINDS:
        selected_kind = ""
    notice = notice or (request.args.get("notice") or "")
    if notice not in {"proposed", "applied"}:
        notice = ""
    plan = plan or (request.args.get("plan") or "")
    if not _PLAN_ID.fullmatch(plan):
        plan = ""
    svc = WpOpsService()
    actor = g.actor["name"]
    slugs: list[str] = []
    files: list[str] = []
    content = draft
    file_error = None
    ssh = False
    if selected_kind and not selected_slug:
        listed = svc.list_components(site_id, selected_kind, actor=actor)
        blocked = _file_block(site_id, listed)
        if blocked is not None:
            return blocked
        if listed.ok:
            slugs = list((listed.data or {}).get("slugs") or [])
            ssh = bool((listed.data or {}).get("ssh"))
        else:
            file_error = listed.message
    elif selected_kind and selected_slug and not selected_path:
        listed = svc.list_files(site_id, selected_kind, selected_slug, actor=actor)
        blocked = _file_block(site_id, listed)
        if blocked is not None:
            return blocked
        if listed.ok:
            files = list((listed.data or {}).get("files") or [])
            ssh = bool((listed.data or {}).get("ssh"))
        else:
            file_error = listed.message
    elif selected_kind and selected_slug and selected_path and draft is None:
        loaded = svc.read_file(site_id, selected_kind, selected_slug, selected_path, actor=actor)
        blocked = _file_block(site_id, loaded)
        if blocked is not None:
            return blocked
        if loaded.ok:
            content = str((loaded.data or {}).get("content") or "")
            ssh = bool((loaded.data or {}).get("ssh"))
        else:
            file_error = loaded.message
    return render_template(
        "wp_files.html",
        **_dashboard_context(
            site=site,
            kind=selected_kind,
            slug=selected_slug,
            relative_path=selected_path,
            slugs=slugs,
            files=files,
            content=content,
            plan=plan,
            notice=notice,
            file_error=file_error,
            ssh=ssh,
            can_edit=_can_edit(site),
            error=None if shown.ok else shown.message,
        ),
    )


def _files_location(
    site_id: str,
    kind: str,
    slug: str,
    relative: str,
    *,
    plan: str = "",
    notice: str = "",
    err: str | None = None,
) -> str:
    params = {"kind": kind, "slug": slug, "path": relative}
    if plan:
        params["plan"] = plan
    if notice:
        params["notice"] = notice
    query = urlencode({key: value for key, value in params.items() if value})
    if err is not None:
        query = f"{query}&{_flash_q(err=err)}" if query else _flash_q(err=err)
    base = url_for("wp_files", site_id=site_id)
    return f"{base}?{query}" if query else base


def _file_block(site_id: str, result: object):
    del site_id
    if _denied(result):
        return Response("Saknar behörighet", 403)
    data = getattr(result, "data", None) or {}
    if not getattr(result, "ok", False) and data.get("error_class") == "UnknownSite":
        return Response("Okänd webbplats", 404)
    return None


def _scrub_posted(site_id: str, text: str) -> str:
    from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
    from ecom_ops.wp_ops.registry import load_sites, site_by_id

    try:
        site = site_by_id(load_sites(), site_id)
    except ValueError:
        return text
    if site is None:
        return text
    bound = bind_credential(site)
    if not bound.present:
        return text
    cleaned = scrub_secrets(text, [bound.value])
    return cleaned if isinstance(cleaned, str) else text


def _can_edit(site: dict | None) -> bool:
    if not site:
        return False
    from ecom_ops.rbac import Actor, Permission

    actor = Actor(name=g.actor["name"], role=g.actor["role"])
    if site.get("environment") == "production":
        return actor.has(Permission.WP_OPS_ADMIN) and actor.has(Permission.CODE_EDIT)
    return actor.has(Permission.WP_OPS_OPERATE)


def _can_drill() -> bool:
    from ecom_ops.rbac import Actor, Permission

    actor = Actor(name=g.actor["name"], role=g.actor["role"])
    return actor.has(Permission.WP_OPS_OPERATE)
