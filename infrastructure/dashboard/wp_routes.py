"""WordPress operations pages. Checks and mock backups do not call customer sites.

The file page edits one existing theme or plugin file. It has no command field.
Opening a site does not read those files.
"""

from __future__ import annotations

import json
import re
import secrets
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
        review_plans = _site_review_plans(site_id, actor=g.actor["name"], admin=_file_admin()) if site else []
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

    @app.route("/wp/<site_id>/reviews/<plan_id>/push-draft", methods=["POST"])
    @_auth_required
    def wp_review_push_draft(site_id: str, plan_id: str):
        return _post_review_push_draft(site_id, plan_id)

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
        loaded = svc.read_file(
            site_id,
            selected_kind,
            selected_slug,
            selected_path,
            actor=actor,
            admin_only=True,
        )
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


def _safe_review_id(value: object) -> str:
    text = str(value or "").strip()
    return text if _REVIEW_ID.fullmatch(text) else ""


def _safe_variant_group(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return ""
    return re.sub(r"[\r\n\t]+", " ", text)


def _safe_http_url(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith(("https://", "http://")) and not any(ch in text for ch in "\r\n\t\"'<> "):
        return text[:400]
    return ""


def _sanitize_draft_page(site_id: str, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    edit_url = _safe_http_url(raw.get("edit_url"))
    link = _safe_http_url(raw.get("link"))
    if not (raw.get("id") or edit_url or link or raw.get("slug")):
        return {}
    return {
        "id": raw.get("id"),
        "slug": _scrub_posted(site_id, str(raw.get("slug") or "").strip())[:120],
        "status": _scrub_posted(site_id, str(raw.get("status") or "draft").strip())[:40],
        "edit_url": edit_url,
        "link": link,
        "pushed_at": str(raw.get("pushed_at") or "").strip()[:40],
        "pushed_by": _scrub_posted(site_id, str(raw.get("pushed_by") or "").strip())[:60],
    }


def _load_raw_plan_extras(ops: object, site_id: str) -> dict[str, dict[str, object]]:
    """Read optional iterative-workspace metadata from wp_review_plans.json safely."""
    path = getattr(ops, "path", None)
    if path is None or not hasattr(path, "is_file") or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    plans = raw.get("plans") if isinstance(raw, dict) else None
    if not isinstance(plans, list):
        return {}
    out: dict[str, dict[str, object]] = {}
    for row in plans:
        if not isinstance(row, dict):
            continue
        if row.get("site_id") != site_id:
            continue
        pid = _safe_review_id(row.get("plan_id"))
        if not pid:
            continue
        out[pid] = {
            "summary": str(row.get("summary") or "")[:200],
            "forked_from": _safe_review_id(row.get("forked_from")),
            "variant_group": _safe_variant_group(row.get("variant_group")),
            "preview_path": str(row.get("preview_path") or "")[:200],
            "focus_selector": str(row.get("focus_selector") or "")[:120],
            "draft_page": _sanitize_draft_page(site_id, row.get("draft_page")),
        }
    return out


def _read_preview_meta(ops: object, site_id: str, plan_id: str) -> dict[str, object]:
    """Read bounded non-secret live-overlay or standalone-page metadata from artifacts/preview.json."""
    from ecom_ops.wp_ops.review_workspace import ReviewError, workspace_root

    data_dir = getattr(ops, "data_dir", None)
    if data_dir is None:
        return {}
    try:
        root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return {}
    art_dir = (root / "artifacts").resolve()
    raw_file = art_dir / "preview.json"
    if raw_file.is_symlink() or not raw_file.is_file():
        return {}
    candidate = raw_file.resolve()
    if candidate.parent != art_dir or candidate.is_symlink():
        return {}
    try:
        if candidate.stat().st_size > 512 * 1024:
            return {}
        parsed = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    preview_kind = str(parsed.get("preview_kind") or "")[:40]
    synthetic_data = parsed.get("synthetic_data") is True or preview_kind == "standalone_page"
    return {
        "preview_kind": preview_kind,
        "path": _scrub_posted(site_id, str(parsed.get("path") or ""))[:200],
        "focus_target": _scrub_posted(site_id, str(parsed.get("focus_target") or ""))[:160],
        "focus_matched": parsed.get("focus_matched") is True,
        "synthetic_data": synthetic_data,
    }


def _read_page_draft_meta(ops: object, site_id: str, plan_id: str) -> dict[str, object]:
    """Read bounded, secret-scrubbed summary metadata from work/page_draft.json."""
    from ecom_ops.wp_ops.review_workspace import ReviewError, workspace_root

    data_dir = getattr(ops, "data_dir", None)
    if data_dir is None:
        return {}
    try:
        root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return {}
    work_dir = (root / "work").resolve()
    raw_file = work_dir / "page_draft.json"
    if raw_file.is_symlink() or not raw_file.is_file():
        return {}
    candidate = raw_file.resolve()
    if candidate.parent != work_dir or candidate.is_symlink():
        return {}
    try:
        if candidate.stat().st_size > 256 * 1024:
            return {}
        parsed = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    synth_ctx = parsed.get("synthetic_context")
    synth_flag = parsed.get("synthetic_data")
    if synth_flag is None and isinstance(synth_ctx, dict):
        synth_flag = synth_ctx.get("synthetic_data")
    shell_raw = str(parsed.get("shell_path") or "/").strip() or "/"
    return {
        "slug": _scrub_posted(site_id, str(parsed.get("slug") or "").strip())[:120],
        "title": _scrub_posted(site_id, str(parsed.get("title") or "").strip())[:160],
        "page_type": _scrub_posted(site_id, str(parsed.get("page_type") or "").strip())[:60],
        "shell_path": _scrub_posted(site_id, shell_raw)[:200],
        "template_file": _scrub_posted(site_id, str(parsed.get("template_file") or "").strip())[:160],
        "synthetic_data": synth_flag is not False,
    }


def _is_standalone_plan(ops: object, site_id: str, plan_id: str) -> bool:
    """Detect whether a wr-* plan is a standalone net-new page draft without leaking content."""
    from ecom_ops.wp_ops.review_workspace import ReviewError, workspace_root

    data_dir = getattr(ops, "data_dir", None)
    if data_dir is None:
        return False
    try:
        root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError:
        return False
    work_dir = (root / "work").resolve()
    draft_file = work_dir / "page_draft.json"
    if not draft_file.is_symlink() and draft_file.is_file():
        try:
            if draft_file.resolve().parent == work_dir:
                return True
        except OSError:
            pass
    meta = _read_preview_meta(ops, site_id, plan_id)
    return meta.get("preview_kind") == "standalone_page"


def _build_live_overlay_comparison(
    live_overlay_shots: list[str],
    *,
    standalone: bool = False,
) -> dict[str, list[dict[str, str]]]:
    """Group live-overlay or standalone-page screenshots into Compare, Fokus, Desktop, and Mobil sections."""
    present = set(live_overlay_shots)
    compare_items: list[dict[str, str]] = []
    for name, label in (
        ("compare-desktop.png", "Jämförelsebild — Desktop"),
        ("compare-mobile.png", "Jämförelsebild — Mobil"),
    ):
        if name in present:
            compare_items.append({"name": name, "label": label, "role": "compare"})

    focus_items: list[dict[str, str]] = []
    focus_labels = (
        (
            ("change-desktop.png", "Dator — fokus"),
            ("focus-desktop.png", "Dator — fokus"),
            ("change-mobile.png", "Mobil — fokus"),
            ("focus-mobile.png", "Mobil — fokus"),
        )
        if standalone
        else (
            ("change-desktop.png", "Dator — markerad ändring"),
            ("focus-desktop.png", "Dator — fokus på ändringen"),
            ("change-mobile.png", "Mobil — markerad ändring"),
            ("focus-mobile.png", "Mobil — fokus på ändringen"),
        )
    )
    for name, label in focus_labels:
        if name in present:
            focus_items.append({"name": name, "label": label, "role": "focus"})

    desktop_items: list[dict[str, str]] = []
    if not standalone and "live-desktop.png" in present:
        desktop_items.append({"name": "live-desktop.png", "label": "Live (före)", "role": "live"})
    if "preview-desktop.png" in present:
        desktop_items.append(
            {
                "name": "preview-desktop.png",
                "label": "Desktop-förhandsvisning" if standalone else "Förhandsvisning (efter)",
                "role": "preview",
            }
        )

    mobile_items: list[dict[str, str]] = []
    if not standalone and "live-mobile.png" in present:
        mobile_items.append({"name": "live-mobile.png", "label": "Live (före)", "role": "live"})
    if "preview-mobile.png" in present:
        mobile_items.append(
            {
                "name": "preview-mobile.png",
                "label": "Mobil-förhandsvisning" if standalone else "Förhandsvisning (efter)",
                "role": "preview",
            }
        )

    return {
        "compare": compare_items,
        "focus": focus_items,
        "desktop": desktop_items,
        "mobile": mobile_items,
    }


def _has_preview_html_fast(ops: object, site_id: str, plan_id: str) -> bool:
    """Check ``artifacts/preview.html`` directly on disk without re-parsing ``wp_review_plans.json``."""
    from ecom_ops.wp_ops.review_plan import _safe_preview_html_path

    data_dir = getattr(ops, "data_dir", None)
    if data_dir is None:
        return False
    return _safe_preview_html_path(data_dir, site_id, plan_id) is not None


def _site_review_plans(site_id: str, *, actor: str, admin: bool, limit: int = 20) -> list[dict[str, object]]:
    """Return recent wr-* review plans for a site, safe for both viewers and admins."""
    if not _can_read_wp():
        return []
    ops = _review_ops()
    listed = ops.list_site(site_id, actor=actor)
    if not listed.ok:
        return []
    raw_plans = (listed.data or {}).get("plans") or []
    extras = _load_raw_plan_extras(ops, site_id)
    rows: list[dict[str, object]] = []
    for item in raw_plans:
        if not isinstance(item, dict):
            continue
        pid = _safe_review_id(item.get("plan_id"))
        if not pid:
            continue
        ex = extras.get(pid, {})
        summary = str(item.get("summary") or ex.get("summary") or "") if admin else ""
        rows.append(
            {
                "plan_id": pid,
                "site_id": str(item.get("site_id") or site_id),
                "kind": str(item.get("kind") or ""),
                "slug": str(item.get("slug") or ""),
                "status": str(item.get("status") or ""),
                "risk": str(item.get("risk") or "low"),
                "created_at": str(item.get("created_at") or ""),
                "created_at_fmt": _fmt_dt(item.get("created_at")),
                "superseded_by": _safe_review_id(item.get("superseded_by")),
                "forked_from": _safe_review_id(item.get("forked_from") or ex.get("forked_from")),
                "variant_group": _safe_variant_group(item.get("variant_group") or ex.get("variant_group")),
                "summary": summary,
                "has_interactive_preview": _has_preview_html_fast(ops, site_id, pid) if admin else False,
                "is_standalone_page": _is_standalone_plan(ops, site_id, pid),
            }
        )
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[:limit]


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
    ops = _review_ops()
    shown = ops.show(site_id, plan_id, actor=g.actor["name"], include_diff=admin)
    blocked = _review_http(shown)
    if blocked is not None:
        return blocked
    payload = shown.data or {}
    plan = dict(payload.get("plan") or {})
    extras_map = _load_raw_plan_extras(ops, site_id)
    plan_extra = extras_map.get(plan_id, {})
    if admin and not plan.get("summary") and plan_extra.get("summary"):
        plan["summary"] = plan_extra["summary"]
    superseded_by = _safe_review_id(plan.get("superseded_by"))
    forked_from = _safe_review_id(plan.get("forked_from") or plan_extra.get("forked_from"))
    variant_group = _safe_variant_group(plan.get("variant_group") or plan_extra.get("variant_group"))
    plan["superseded_by"] = superseded_by
    plan["forked_from"] = forked_from
    plan["variant_group"] = variant_group

    sibling_variants: list[dict[str, object]] = []
    if variant_group:
        all_site_plans = _site_review_plans(site_id, actor=g.actor["name"], admin=admin, limit=100)
        matching = [row for row in all_site_plans if row.get("variant_group") == variant_group]
        matching.sort(key=lambda r: str(r.get("created_at") or ""))
        if len(matching) > 1:
            for idx, row in enumerate(matching, start=1):
                sibling_variants.append(
                    {
                        **row,
                        "index": idx,
                        "is_current": row.get("plan_id") == plan_id,
                    }
                )

    diff = str(payload.get("diff") or "") if admin else ""
    screenshots = ops.list_artifacts(site_id, plan_id, actor=g.actor["name"]) if admin else []
    sandbox_screenshots = [name for name in screenshots if name.startswith(("base-", "work-"))]
    live_overlay_screenshots = [
        name
        for name in screenshots
        if name.startswith(("compare-", "live-", "preview-", "change-", "focus-"))
    ]
    has_interactive_preview = _has_preview_html_fast(ops, site_id, plan_id) if admin else False
    browser_passed = any(
        isinstance(row, dict)
        and row.get("job") == "browser_smoke"
        and row.get("status") == "passed"
        and row.get("executed") is True
        for row in (plan.get("checks") or [])
    )
    has_sandbox_smoke = bool(sandbox_screenshots and browser_passed)
    has_live_overlay_preview = bool(live_overlay_screenshots)
    has_rendered_preview = has_sandbox_smoke or has_live_overlay_preview
    preview_meta = (
        _read_preview_meta(ops, site_id, plan_id)
        if admin and (has_live_overlay_preview or has_interactive_preview)
        else {}
    )
    page_draft = _read_page_draft_meta(ops, site_id, plan_id) if admin else {}
    draft_page = (
        _sanitize_draft_page(site_id, plan.get("draft_page") or plan_extra.get("draft_page"))
        if admin
        else {}
    )
    if draft_page and not plan.get("draft_page"):
        plan["draft_page"] = draft_page
    is_standalone_page = (
        bool(page_draft or preview_meta.get("preview_kind") == "standalone_page")
        if admin
        else _is_standalone_plan(ops, site_id, plan_id)
    )
    synthetic_data = bool(
        preview_meta.get("synthetic_data")
        or page_draft.get("synthetic_data")
        or is_standalone_page
    )
    live_overlay_comparison = _build_live_overlay_comparison(
        live_overlay_screenshots,
        standalone=is_standalone_page,
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
            screenshots=sandbox_screenshots,
            sandbox_screenshots=sandbox_screenshots,
            live_overlay_screenshots=live_overlay_screenshots,
            live_overlay_comparison=live_overlay_comparison,
            preview_meta=preview_meta,
            page_draft=page_draft,
            draft_page=draft_page,
            is_standalone_page=is_standalone_page,
            synthetic_data=synthetic_data,
            has_sandbox_smoke=has_sandbox_smoke,
            has_live_overlay_preview=has_live_overlay_preview,
            has_rendered_preview=has_rendered_preview,
            has_interactive_preview=has_interactive_preview,
            superseded_by=superseded_by,
            forked_from=forked_from,
            variant_group=variant_group,
            sibling_variants=sibling_variants,
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


def _post_review_push_draft(site_id: str, plan_id: str):
    if not _SITE_ID.fullmatch(str(site_id or "")) or not _REVIEW_ID.fullmatch(str(plan_id or "")):
        return Response("Okänd plan", 404)
    if not (_file_admin() and g.actor.get("is_oscar")):
        return Response("Saknar behörighet", 403)
    base, work = _posted_hashes()
    if not base.strip() or not work.strip():
        return _render_review(
            site_id,
            plan_id,
            error_class="DigestMismatch",
            result={"error_class": "DigestMismatch"},
            action_name="push_draft",
        )
    from ecom_ops.wp_ops import WpOpsService

    svc = WpOpsService()
    raw_result = svc.push_page_draft(
        site_id,
        plan_id,
        actor=g.actor["name"],
        base_sha256=base,
        work_sha256=work,
    )
    if isinstance(raw_result, dict):
        ok = bool(raw_result.get("ok"))
        err_cls = str(raw_result.get("error_class") or "")
        msg = str(
            raw_result.get("message")
            or ("WordPress-utkast skapat (status=draft)." if ok else err_cls or "Kunde inte skicka utkast.")
        )
        if err_cls == "AccessDenied":
            return Response("Saknar behörighet", 403)
        if err_cls in {"UnknownSite", "UnknownPlan", "StalePlan"}:
            return Response("Okänd plan", 404)
        if err_cls == "Expired":
            return Response("Planen har gått ut", 403)

        class _DictOutcome:
            def __init__(self, ok_val: bool, message_val: str) -> None:
                self.ok = ok_val
                self.message = message_val

        return _render_review(
            site_id,
            plan_id,
            error_class=err_cls if not ok else "",
            result=raw_result,
            result_obj=_DictOutcome(ok, msg),
            action_name="push_draft",
        )
    blocked = _review_http(raw_result)
    if blocked is not None:
        return blocked
    data = getattr(raw_result, "data", None) or {}
    return _render_review(
        site_id,
        plan_id,
        error_class=str(data.get("error_class") or ""),
        result=data,
        result_obj=raw_result,
        action_name="push_draft",
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


_SCRIPT_BLOCK_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_STRAY_SCRIPT_RE = re.compile(r"</?script\b[^>]*>", re.IGNORECASE)
_SCRIPT_SRC_ATTR_RE = re.compile(r"\bsrc\s*=", re.IGNORECASE)
_AZOM_GUARD_ATTR_RE = re.compile(
    r"""\b(?:data-azom-guard\s*=\s*(?:"1"|'1'|1(?=[\s>/]|$))|data-azom-sandbox\s*=\s*(?:"guard"|'guard'|guard(?=[\s>/]|$)))""",
    re.IGNORECASE,
)
_AZOM_TOOLBAR_RE = re.compile(r"""\bid\s*=\s*(?:"azom-sandbox-toolbar"|'azom-sandbox-toolbar')""", re.IGNORECASE)
_GUARD_PARAM_RES = (
    ("highlight_token", re.compile(r"""\bvar\s+highlightToken\s*=\s*("(?:\\.|[^"\\])*")\s*;""")),
    ("focus_text", re.compile(r"""\bvar\s+focusText\s*=\s*("(?:\\.|[^"\\])*")\s*;""")),
    ("focus_selector", re.compile(r"""\bvar\s+focusSelector\s*=\s*("(?:\\.|[^"\\])*")\s*;""")),
)
_HTML_OPEN_TAG_RE = re.compile(r"""<([a-zA-Z][a-zA-Z0-9:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>""")
_TAG_ATTR_RE = re.compile(
    r"""([\s/]*)([a-zA-Z_:][a-zA-Z0-9_.:-]*)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'=<>`/]+))?""",
    re.DOTALL,
)
_UNSAFE_URL_ATTRS = frozenset({"href", "src", "action", "formaction", "xlink:href"})
_UNSAFE_SCHEME_RE = re.compile(r"^\s*(?:javascript|vbscript)\s*:", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _strip_tag_unsafe_attrs(match: re.Match[str]) -> str:
    tag_name = match.group(1)
    raw_tail = match.group(2) or ""
    if not raw_tail:
        return match.group(0)
    self_close = False
    tail = raw_tail
    if tail.rstrip().endswith("/"):
        stripped = tail.rstrip()
        # Treat trailing "/" as self-closing slash only if it is not inside an unquoted value
        if stripped == "/" or stripped[-2] in " \t\r\n\"'":
            self_close = True
            tail = stripped[:-1]
    kept: list[str] = []
    pos = 0
    first = True
    while pos < len(tail):
        m = _TAG_ATTR_RE.match(tail, pos)
        if m is None:
            pos += 1
            continue
        sep, name, raw_val = m.group(1), m.group(2), m.group(3)
        if first and not sep:
            # Guard against malformed tail bytes right after tag name
            pos = m.end()
            continue
        first = False
        pos = m.end()
        lname = name.lower()
        if lname.startswith("on") and len(lname) > 2:
            continue
        if raw_val is not None and lname in _UNSAFE_URL_ATTRS:
            unquoted = raw_val[1:-1] if len(raw_val) >= 2 and raw_val[0] in "\"'" else raw_val
            compact = re.sub(r"[\x00-\x20]+", "", unquoted)
            if _UNSAFE_SCHEME_RE.match(compact):
                continue
        clean_sep = " " if (not sep or "/" in sep) else sep
        kept.append(f"{clean_sep}{name}={raw_val}" if raw_val is not None else f"{clean_sep}{name}")
    suffix = " />" if self_close else ">"
    return f"<{tag_name}{''.join(kept)}{suffix}"


def _build_canonical_guard_script(
    *,
    nonce: str,
    highlight_token: str = "",
    focus_text: str = "",
    focus_selector: str = "",
) -> str:
    from ecom_ops.integrations.wp_live_preview import build_interactive_sandbox_html

    synthetic = build_interactive_sandbox_html(
        "<html><body></body></html>",
        site_canonical_url="",
        site_id="",
        plan_id="",
        path="/",
        highlight_token=highlight_token,
        focus_text=focus_text,
        focus_selector=focus_selector,
    )
    match = _SCRIPT_BLOCK_RE.search(synthetic)
    if match is None:
        return ""
    attrs = match.group(1) or ""
    body = match.group(2) or ""
    return f'<script nonce="{nonce}"{attrs}>{body}</script>'


def _extract_genuine_guard_params(html_text: str) -> tuple[bool, dict[str, str]]:
    """Extract only safe JSON string parameters if a genuine Azom guard script was present."""
    found = False
    params = {"highlight_token": "", "focus_text": "", "focus_selector": ""}
    for match in _SCRIPT_BLOCK_RE.finditer(html_text):
        attrs = match.group(1) or ""
        body = match.group(2) or ""
        if _SCRIPT_SRC_ATTR_RE.search(attrs) or not _AZOM_GUARD_ATTR_RE.search(attrs):
            continue
        if "Blockerad i sandbox-läget" not in body or "#azom-sandbox-toast" not in body:
            continue
        found = True
        extracted: dict[str, str] = {"highlight_token": "", "focus_text": "", "focus_selector": ""}
        for key, pat in _GUARD_PARAM_RES:
            pm = pat.search(body)
            if pm is None:
                continue
            try:
                val = json.loads(pm.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(val, str):
                extracted[key] = val[:400]
        params = extracted
    return found, params


def _harden_preview_html(html_text: str, nonce: str) -> str:
    """Strip ALL on-disk scripts & inline handlers; emit only server-generated canonical guard."""
    had_genuine_guard, guard_params = _extract_genuine_guard_params(html_text)

    # Zero on-disk JavaScript survives into the served response (prevents spoofed guard tags).
    working = _SCRIPT_BLOCK_RE.sub("", html_text)
    working = _STRAY_SCRIPT_RE.sub("", working)
    working = _HTML_OPEN_TAG_RE.sub(_strip_tag_unsafe_attrs, working)

    if had_genuine_guard or _AZOM_TOOLBAR_RE.search(working):
        canonical_guard = _build_canonical_guard_script(nonce=nonce, **guard_params)
        if canonical_guard:
            body_close = _BODY_CLOSE_RE.search(working)
            if body_close is not None:
                working = (
                    working[: body_close.start()]
                    + canonical_guard
                    + "\n"
                    + working[body_close.start() :]
                )
            else:
                working = working + "\n" + canonical_guard
    return working


def _preview_csp_for_site(site_id: str, *, nonce: str = "") -> str:
    del site_id  # Opaque CSP sandbox never grants origin/script-host trust to the previewed site.
    script_src = f"'nonce-{nonce}'" if nonce else "'none'"
    return (
        "sandbox allow-scripts; "
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
    if not _file_admin():
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
    nonce = secrets.token_urlsafe(16)
    html_text = _harden_preview_html(_scrub_posted(site_id, html_text), nonce)
    return Response(
        html_text,
        status=200,
        mimetype="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": _preview_csp_for_site(site_id, nonce=nonce),
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
