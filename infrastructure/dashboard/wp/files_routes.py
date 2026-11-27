"""Stateless theme/plugin file browse, propose, apply, plan, and deploy routes."""

from __future__ import annotations

from urllib.parse import urlencode

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import _dashboard_context, _flash_q

from .common import (
    KINDS,
    PLAN_ID_RE,
    can_edit,
    file_admin,
    file_block,
    is_denied,
    scrub_posted,
    wp_service,
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
    svc = wp_service()
    actor = g.actor["name"]
    shown = svc.show_site(site_id, actor=actor)
    if is_denied(shown):
        return Response("Saknar behörighet", 403)
    if not shown.ok and (shown.data or {}).get("error_class") == "UnknownSite":
        return Response("Okänd webbplats", 404)
    site = (shown.data or {}).get("site")
    selected_kind = kind if kind is not None else (request.args.get("kind") or "")
    selected_slug = slug if slug is not None else (request.args.get("slug") or "")
    selected_path = relative if relative is not None else (request.args.get("path") or "")
    if selected_kind not in KINDS:
        selected_kind = ""
    notice = notice or (request.args.get("notice") or "")
    if notice not in {"proposed", "applied"}:
        notice = ""
    plan = plan or (request.args.get("plan") or "")
    if not PLAN_ID_RE.fullmatch(plan):
        plan = ""
    slugs: list[str] = []
    files: list[str] = []
    content = draft
    file_error = None
    ssh = False
    if selected_kind and not selected_slug:
        listed = svc.list_components(site_id, selected_kind, actor=actor)
        blocked = file_block(site_id, listed)
        if blocked is not None:
            return blocked
        if listed.ok:
            slugs = list((listed.data or {}).get("slugs") or [])
            ssh = bool((listed.data or {}).get("ssh"))
        else:
            file_error = listed.message
    elif selected_kind and selected_slug and not selected_path:
        listed = svc.list_files(site_id, selected_kind, selected_slug, actor=actor)
        blocked = file_block(site_id, listed)
        if blocked is not None:
            return blocked
        if listed.ok:
            files = list((listed.data or {}).get("files") or [])
            ssh = bool((listed.data or {}).get("ssh"))
        else:
            file_error = listed.message
    elif selected_kind and selected_slug and selected_path and draft is None:
        loaded = svc.read_file(
            site_id,
            selected_kind,
            selected_slug,
            selected_path,
            actor=actor,
            admin_only=True,
        )
        blocked = file_block(site_id, loaded)
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
            can_edit=can_edit(site),
            error=None if shown.ok else shown.message,
        ),
    )


def _render_file_plan(site_id: str, plan_id: str, *, notice: str = "", error_class: str = ""):
    if not PLAN_ID_RE.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not (file_admin() and g.actor.get("is_oscar")):
        return Response("Saknar behörighet", 403)
    svc = wp_service()
    shown = svc.show_site(site_id, actor=g.actor["name"])
    if is_denied(shown):
        return Response("Saknar behörighet", 403)
    if not shown.ok and (shown.data or {}).get("error_class") == "UnknownSite":
        return Response("Okänd webbplats", 404)
    review = svc.review_file_plan(site_id, plan_id, actor=g.actor["name"])
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
    if not (file_admin() and g.actor.get("is_oscar")):
        return Response("Saknar behörighet", 403)
    plan_id = request.form.get("plan_id") or ""
    if not PLAN_ID_RE.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    svc = wp_service()
    review = svc.review_file_plan(site_id, plan_id, actor=g.actor["name"])
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
    result = svc.deploy_file_production(site_id, plan_id, actor=g.actor["name"])
    error_class = str((result.data or {}).get("error_class") or "BackupNotVerified")
    return _render_file_plan(site_id, plan_id, notice="blocked", error_class=error_class)


def register_files_routes(app: Flask) -> None:
    @app.route("/wp/<site_id>/files")
    @_auth_required
    def wp_files(site_id: str):
        return _render_files(site_id)

    @app.route("/wp/<site_id>/files/propose", methods=["POST"])
    @_auth_required
    def wp_file_propose(site_id: str):
        kind = request.form.get("kind") or ""
        slug = request.form.get("slug") or ""
        relative = request.form.get("path") or ""
        content = request.form.get("content") or ""
        result = wp_service().propose_file(
            site_id,
            kind,
            slug,
            relative,
            content,
            actor=g.actor["name"],
        )
        blocked = file_block(site_id, result)
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
            draft=scrub_posted(site_id, content),
            plan=plan if PLAN_ID_RE.fullmatch(plan) else "",
            notice="proposed",
        )

    @app.route("/wp/<site_id>/files/apply", methods=["POST"])
    @_auth_required
    def wp_file_apply(site_id: str):
        kind = request.form.get("kind") or ""
        slug = request.form.get("slug") or ""
        relative = request.form.get("path") or ""
        result = wp_service().apply_file(
            site_id,
            request.form.get("plan_id") or "",
            request.form.get("content") or "",
            actor=g.actor["name"],
        )
        blocked = file_block(site_id, result)
        if blocked is not None:
            return blocked
        plan = request.form.get("plan_id") or ""
        if result.ok and PLAN_ID_RE.fullmatch(plan):
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
