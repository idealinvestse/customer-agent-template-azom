"""WordPress operations pages. Checks and mock backups do not call customer sites.

The file page edits one existing theme or plugin file. It has no command field.
Opening a site does not read those files.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import _dashboard_context, _flash, _flash_q, _fmt_dt

_PLAN_ID = re.compile(r"^fe-[a-f0-9]{12}$")
_REVIEW_ID = re.compile(r"^wr-[a-f0-9]{12}$")
_SITE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
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
        _flash(result.message, "ok" if result.ok else "error")
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

    @app.route("/wp/<site_id>/files/plan/<plan_id>")
    @_auth_required
    def wp_file_plan(site_id: str, plan_id: str):
        return _render_file_plan(site_id, plan_id)

    @app.route("/wp/<site_id>/files/deploy", methods=["POST"])
    @_auth_required
    def wp_file_deploy(site_id: str):
        return _post_file_deploy(site_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>")
    @_auth_required
    def wp_review(site_id: str, plan_id: str):
        return _render_review(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/comment", methods=["POST"])
    @_auth_required
    def wp_review_comment(site_id: str, plan_id: str):
        return _post_review_comment(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/staging", methods=["POST"])
    @_auth_required
    def wp_review_staging(site_id: str, plan_id: str):
        return _post_review_staging(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/production", methods=["POST"])
    @_auth_required
    def wp_review_production(site_id: str, plan_id: str):
        return _post_review_production(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/preview", methods=["GET", "POST"])
    @_auth_required
    def wp_review_preview(site_id: str, plan_id: str):
        if request.method == "GET":
            return _get_review_preview_html(site_id, plan_id)
        return _post_review_preview(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/checks", methods=["POST"])
    @_auth_required
    def wp_review_checks(site_id: str, plan_id: str):
        return _post_review_checks(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/extend", methods=["POST"])
    @_auth_required
    def wp_review_extend(site_id: str, plan_id: str):
        return _post_review_extend(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/artifact/<name>")
    @_auth_required
    def wp_review_artifact(site_id: str, plan_id: str, name: str):
        if not _REVIEW_ID.fullmatch(plan_id):
            return Response("Okänd plan", 404)
        if not _file_admin():
            return Response("Saknar behörighet", 403)
        path, err = _review_ops().artifact_file(site_id, plan_id, name, actor=g.actor["name"])
        if err is not None or path is None:
            blocked = _review_http(err) if err is not None else None
            if blocked is not None:
                return blocked
            return Response("Okänd artefakt", 404)
        return Response(
            path.read_bytes(),
            mimetype="image/png",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


def _post_ssh(site_id: str):
    from ecom_ops.wp_ops import WpOpsService

    result = WpOpsService().save_ssh_settings(
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
    blocked = _file_block(site_id, result)
    if blocked is not None:
        return blocked
    if result.ok:
        _flash(result.message, "ok")
        return redirect(url_for("wp_ssh", site_id=site_id) + f"?{_flash_q(result.message)}")
    _flash(result.message, "error")
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
    ok = getattr(result, "ok", False)
    _flash(message, "ok" if ok else "error")
    frag = _flash_q(message) if ok else _flash_q(err=message)
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


def _file_admin() -> bool:
    from ecom_ops.rbac import Actor, Permission

    actor = Actor(name=g.actor["name"], role=g.actor["role"])
    return actor.has(Permission.WP_OPS_ADMIN) and actor.has(Permission.CODE_EDIT)


def _render_file_plan(site_id: str, plan_id: str, *, notice: str = "", error_class: str = ""):
    from ecom_ops.wp_ops import WpOpsService

    if not _PLAN_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not _file_admin():
        return Response("Saknar behörighet", 403)
    shown = WpOpsService().show_site(site_id, actor=g.actor["name"])
    if _denied(shown):
        return Response("Saknar behörighet", 403)
    if not shown.ok and (shown.data or {}).get("error_class") == "UnknownSite":
        return Response("Okänd webbplats", 404)
    review = WpOpsService().review_file_plan(site_id, plan_id, actor=g.actor["name"])
    if not review.ok:
        return Response("Okänd plan", 404)
    plan = dict((review.data or {}).get("plan") or {})
    diff = str((review.data or {}).get("diff") or "")
    return render_template(
        "wp_file_plan.html",
        **_dashboard_context(
            site=(shown.data or {}).get("site"),
            plan=plan,
            diff=diff,
            notice=notice,
            error_class=error_class,
            executed=False,
        ),
    )


def _post_file_deploy(site_id: str):
    from ecom_ops.wp_ops import WpOpsService

    if not _file_admin():
        return Response("Saknar behörighet", 403)
    plan_id = request.form.get("plan_id") or ""
    if not _PLAN_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    review = WpOpsService().review_file_plan(site_id, plan_id, actor=g.actor["name"])
    if not review.ok:
        return Response("Okänd plan", 404)
    plan = dict((review.data or {}).get("plan") or {})
    before = request.form.get("before_digest") or ""
    after = request.form.get("after_digest") or ""
    environment = request.form.get("environment") or ""
    relative = request.form.get("path") or ""
    if (
        before != str(plan.get("before_digest") or "")
        or after != str(plan.get("after_digest") or "")
        or environment != str(plan.get("environment") or "")
        or relative != str(plan.get("relative_path") or "")
        or environment == ""
    ):
        return _render_file_plan(site_id, plan_id, error_class="DigestMismatch")
    result = WpOpsService().deploy_file_production(site_id, plan_id, actor=g.actor["name"])
    error_class = str((result.data or {}).get("error_class") or "BackupNotVerified")
    return _render_file_plan(site_id, plan_id, notice="blocked", error_class=error_class)


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


def _review_ops():
    from ecom_ops.wp_ops.review_plan import ReviewPlans

    return ReviewPlans()


def _review_http(result: object):
    data = getattr(result, "data", None) or {}
    kind = data.get("error_class")
    if kind == "AccessDenied" or _denied(result):
        return Response("Saknar behörighet", 403)
    if kind in {"UnknownSite", "UnknownPlan", "StalePlan"}:
        return Response("Okänd plan", 404)
    if kind == "Expired":
        return Response("Planen har gått ut", 403)
    return None


def _render_review(
    site_id: str,
    plan_id: str,
    *,
    error_class: str = "",
    result: dict | None = None,
    result_obj: object | None = None,
    action_name: str = "",
):
    if not _REVIEW_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    admin = _file_admin()
    drill = _can_drill()
    shown = _review_ops().show(site_id, plan_id, actor=g.actor["name"], include_diff=admin)
    blocked = _review_http(shown)
    if blocked is not None:
        return blocked
    payload = shown.data or {}
    plan = dict(payload.get("plan") or {})
    diff = str(payload.get("diff") or "") if admin else ""
    screenshots = _review_ops().list_artifacts(site_id, plan_id, actor=g.actor["name"]) if admin else []
    has_interactive_preview = _review_ops().has_preview_html(site_id, plan_id, actor=g.actor["name"])
    browser_passed = any(
        isinstance(row, dict)
        and row.get("job") == "browser_smoke"
        and row.get("status") == "passed"
        and row.get("executed") is True
        for row in (plan.get("checks") or [])
    )
    outcome = result or {}
    effective_err = error_class or str(outcome.get("error_class") or "")
    review_feedback = None
    if result_obj is not None:
        review_feedback = {
            "ok": bool(getattr(result_obj, "ok", False)),
            "action": action_name,
            "message": str(getattr(result_obj, "message", "") or ""),
            "error_class": effective_err,
            "production_untouched": (
                action_name == "production"
                or effective_err in {"BackupNotVerified", "RestoreNotInMvp"}
            ),
        }
    return render_template(
        "wp_review.html",
        **_dashboard_context(
            plan=plan,
            diff=diff,
            can_review=admin,
            can_drill=drill,
            comments=list(plan.get("comments") or []),
            sandbox=dict(plan.get("sandbox") or {}),
            expires_at_fmt=_fmt_dt(plan.get("expires_at")),
            review_feedback=review_feedback,
            screenshots=screenshots,
            has_rendered_preview=bool(screenshots and browser_passed),
            has_interactive_preview=has_interactive_preview,
            error_class=effective_err,
            executed=outcome.get("executed") is True,
            bytes_copied=False,
            restore=str(outcome.get("restore") or ""),
            label=str(outcome.get("label") or plan.get("label") or ""),
            live_calls=False,
        ),
    )


def _posted_hashes() -> tuple[str, str]:
    return request.form.get("base_sha256") or "", request.form.get("work_sha256") or ""


def _post_review_comment(site_id: str, plan_id: str):
    if not _REVIEW_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not _file_admin():
        return Response("Saknar behörighet", 403)
    result = _review_ops().comment(site_id, plan_id, request.form.get("text") or "", actor=g.actor["name"])
    blocked = _review_http(result)
    if blocked is not None:
        return blocked
    return _render_review(
        site_id,
        plan_id,
        result=result.data or {},
        result_obj=result,
        action_name="comment",
    )


def _post_review_staging(site_id: str, plan_id: str):
    if not _REVIEW_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not _file_admin():
        return Response("Saknar behörighet", 403)
    base, work = _posted_hashes()
    result = _review_ops().approve_staging(
        site_id, plan_id, actor=g.actor["name"], base_sha256=base, work_sha256=work
    )
    blocked = _review_http(result)
    if blocked is not None:
        return blocked
    data = result.data or {}
    return _render_review(
        site_id,
        plan_id,
        error_class=str(data.get("error_class") or ""),
        result=data,
        result_obj=result,
        action_name="staging",
    )


def _post_review_production(site_id: str, plan_id: str):
    if not _REVIEW_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not _file_admin():
        return Response("Saknar behörighet", 403)
    base, work = _posted_hashes()
    result = _review_ops().request_production(
        site_id, plan_id, actor=g.actor["name"], base_sha256=base, work_sha256=work
    )
    blocked = _review_http(result)
    if blocked is not None:
        return blocked
    data = result.data or {}
    return _render_review(
        site_id,
        plan_id,
        error_class=str(data.get("error_class") or "BackupNotVerified"),
        result=data,
        result_obj=result,
        action_name="production",
    )


def _post_review_extend(site_id: str, plan_id: str):
    if not _REVIEW_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not _file_admin():
        return Response("Saknar behörighet", 403)
    result = _review_ops().extend(site_id, plan_id, actor=g.actor["name"])
    blocked = _review_http(result)
    if blocked is not None:
        return blocked
    return _render_review(
        site_id,
        plan_id,
        result=result.data or {},
        result_obj=result,
        action_name="extend",
    )


def _post_review_checks(site_id: str, plan_id: str):
    if not _REVIEW_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not _can_drill():
        return Response("Saknar behörighet", 403)
    result = _review_ops().run_checks(site_id, plan_id, actor=g.actor["name"])
    blocked = _review_http(result)
    if blocked is not None:
        return blocked
    data = result.data or {}
    return _render_review(
        site_id,
        plan_id,
        error_class=str(data.get("error_class") or ""),
        result=data,
        result_obj=result,
        action_name="checks",
    )


def _can_read_wp() -> bool:
    from ecom_ops.rbac import Actor, Permission

    actor = Actor(name=g.actor["name"], role=g.actor["role"])
    return actor.has(Permission.WP_OPS_READ)


def _preview_csp_for_site(site_id: str) -> str:
    from urllib.parse import urlparse

    from ecom_ops.wp_ops.registry import load_sites, site_by_id

    extra_script_hosts: list[str] = []
    try:
        site = site_by_id(load_sites(), site_id)
        if site is not None:
            parsed = urlparse(str(site.canonical_url or ""))
            host = (parsed.hostname or "").strip().lower().rstrip(".")
            if parsed.scheme == "https" and host and not any(ch in host for ch in " ;'\""):
                extra_script_hosts.append(f"https://{host}")
                if host.startswith("www."):
                    bare = host[4:]
                    if bare:
                        extra_script_hosts.append(f"https://{bare}")
                else:
                    extra_script_hosts.append(f"https://www.{host}")
    except Exception:
        pass
    script_src = " ".join(["'unsafe-inline'", *extra_script_hosts])
    return (
        "default-src 'none'; "
        f"script-src {script_src}; "
        "style-src 'unsafe-inline' https: data:; "
        "img-src https: data: blob:; "
        "font-src https: data:; "
        "media-src https: data:; "
        "connect-src 'none'; "
        "form-action 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'self'"
    )


def _get_review_preview_html(site_id: str, plan_id: str):
    if not _SITE_ID.fullmatch(str(site_id or "")) or not _REVIEW_ID.fullmatch(str(plan_id or "")):
        return Response("Okänd plan", 404)
    if not _can_read_wp():
        return Response("Saknar behörighet", 403)
    path, err = _review_ops().preview_html_file(site_id, plan_id, actor=g.actor["name"])
    if err is not None or path is None:
        blocked = _review_http(err) if err is not None else None
        if blocked is not None:
            return blocked
        return Response("Sandbox-förhandsvisning saknas", 404)
    try:
        html_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Response("Sandbox-förhandsvisning saknas", 404)
    html_text = _scrub_posted(site_id, html_text)
    return Response(
        html_text,
        status=200,
        mimetype="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": _preview_csp_for_site(site_id),
        },
    )


def _post_review_preview(site_id: str, plan_id: str):
    if not _REVIEW_ID.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not _can_drill():
        return Response("Saknar behörighet", 403)
    result = _review_ops().preview(site_id, plan_id, actor=g.actor["name"])
    blocked = _review_http(result)
    if blocked is not None:
        return blocked
    data = result.data or {}
    return _render_review(
        site_id,
        plan_id,
        error_class=str(data.get("error_class") or ""),
        result=data,
        result_obj=result,
        action_name="preview",
    )
