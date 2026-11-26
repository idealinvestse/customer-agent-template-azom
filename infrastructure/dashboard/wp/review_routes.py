"""Stateless review-plan read-model, HITL action, and artifact routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Flask, Response, g, render_template, request

from auth import _auth_required
from support import _dashboard_context, _fmt_dt

from .common import (
    REVIEW_ID_RE,
    SITE_ID_RE,
    can_drill,
    can_read_wp,
    file_admin,
    review_http,
    review_ops,
    safe_review_id,
    safe_variant_group,
    sanitize_draft_page,
    scrub_page_draft_meta,
    scrub_preview_meta,
    wp_service,
)


def build_live_overlay_comparison(
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


def site_review_plans(
    site_id: str, *, actor: str, admin: bool, limit: int = 20
) -> list[dict[str, object]]:
    """Return recent wr-* review plans for a site via canonical ``ReviewPlans.describe_site_plans``."""
    if not can_read_wp():
        return []
    listed = review_ops().describe_site_plans(site_id, actor=actor, check_preview_html=admin)
    if not listed.ok:
        return []
    raw_plans = (listed.data or {}).get("plans") or []
    rows: list[dict[str, object]] = []
    for item in raw_plans:
        if not isinstance(item, dict):
            continue
        pid = safe_review_id(item.get("plan_id"))
        if not pid:
            continue
        summary = str(item.get("summary") or "") if admin else ""
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
                "superseded_by": safe_review_id(item.get("superseded_by")),
                "forked_from": safe_review_id(item.get("forked_from")),
                "variant_group": safe_variant_group(item.get("variant_group")),
                "summary": summary,
                "has_interactive_preview": bool(item.get("has_preview_html")) if admin else False,
                "is_standalone_page": bool(item.get("is_standalone")),
            }
        )
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[:limit]


def render_review(
    site_id: str,
    plan_id: str,
    *,
    error_class: str = "",
    result: dict | None = None,
    result_obj: object | None = None,
    action_name: str = "",
):
    if not REVIEW_ID_RE.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    admin = file_admin()
    drill = can_drill()
    shown = review_ops().describe_plan(site_id, plan_id, actor=g.actor["name"], include_diff=admin)
    blocked = review_http(shown)
    if blocked is not None:
        return blocked
    payload = shown.data or {}
    plan = dict(payload.get("plan") or {})
    if not admin:
        plan["summary"] = ""
    superseded_by = safe_review_id(plan.get("superseded_by"))
    forked_from = safe_review_id(plan.get("forked_from"))
    variant_group = safe_variant_group(plan.get("variant_group"))
    plan["superseded_by"] = superseded_by
    plan["forked_from"] = forked_from
    plan["variant_group"] = variant_group

    sibling_variants: list[dict[str, object]] = []
    if variant_group:
        all_site_plans = site_review_plans(site_id, actor=g.actor["name"], admin=admin, limit=100)
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
    screenshots = list(plan.get("artifacts") or []) if admin else []
    sandbox_screenshots = [name for name in screenshots if name.startswith(("base-", "work-"))]
    live_overlay_screenshots = [
        name
        for name in screenshots
        if name.startswith(("compare-", "live-", "preview-", "change-", "focus-"))
    ]
    has_interactive_preview = bool(plan.get("has_preview_html")) if admin else False
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
        scrub_preview_meta(site_id, plan.get("preview_meta"))
        if admin and (has_live_overlay_preview or has_interactive_preview)
        else {}
    )
    page_draft = scrub_page_draft_meta(site_id, plan.get("page_draft_meta")) if admin else {}
    draft_page = sanitize_draft_page(site_id, plan.get("draft_page")) if admin else {}
    if draft_page:
        plan["draft_page"] = draft_page
    is_standalone_page = (
        bool(page_draft or preview_meta.get("preview_kind") == "standalone_page")
        if admin
        else bool(plan.get("is_standalone"))
    )
    synthetic_data = bool(
        preview_meta.get("synthetic_data")
        or page_draft.get("synthetic_data")
        or is_standalone_page
    )
    live_overlay_comparison = build_live_overlay_comparison(
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
    wp_research_allowed = bool(admin or drill)
    wp_research_jobs: list[dict] = []
    if wp_research_allowed:
        try:
            from research_routes import load_linked_research_jobs

            wp_research_jobs = load_linked_research_jobs(
                actor=g.actor["name"],
                target_ref=f"wp:{site_id}:{plan_id}",
                extra_ids=list(plan.get("research_ids") or []),
                limit=10,
            )
        except Exception:
            wp_research_jobs = []
    return render_template(
        "wp_review.html",
        **_dashboard_context(
            plan=plan,
            diff=diff,
            can_review=admin,
            can_drill=drill,
            wp_research_allowed=wp_research_allowed,
            can_wp_research=wp_research_allowed,
            wp_research_jobs=wp_research_jobs,
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


def run_review_action(
    site_id: str,
    plan_id: str,
    *,
    require_fn: Callable[[], bool],
    action_name: str,
    action_fn: Callable[[Any], Any],
    default_error_class: str = "",
):
    """Shared validation, RBAC gate, execution, and re-render dispatcher for review POST routes."""
    if not REVIEW_ID_RE.fullmatch(plan_id):
        return Response("Okänd plan", 404)
    if not require_fn():
        return Response("Saknar behörighet", 403)
    result = action_fn(review_ops())
    blocked = review_http(result)
    if blocked is not None:
        return blocked
    data = result.data or {}
    return render_review(
        site_id,
        plan_id,
        error_class=str(data.get("error_class") or default_error_class),
        result=data,
        result_obj=result,
        action_name=action_name,
    )


def _post_review_push_draft(site_id: str, plan_id: str):
    if not SITE_ID_RE.fullmatch(str(site_id or "")) or not REVIEW_ID_RE.fullmatch(str(plan_id or "")):
        return Response("Okänd plan", 404)
    if not (file_admin() and g.actor.get("is_oscar")):
        return Response("Saknar behörighet", 403)
    base, work = _posted_hashes()
    if not base.strip() or not work.strip():
        return render_review(
            site_id,
            plan_id,
            error_class="DigestMismatch",
            result={"error_class": "DigestMismatch"},
            action_name="push_draft",
        )
    raw_result = wp_service().push_page_draft(
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

        return render_review(
            site_id,
            plan_id,
            error_class=err_cls if not ok else "",
            result=raw_result,
            result_obj=_DictOutcome(ok, msg),
            action_name="push_draft",
        )
    blocked = review_http(raw_result)
    if blocked is not None:
        return blocked
    data = getattr(raw_result, "data", None) or {}
    return render_review(
        site_id,
        plan_id,
        error_class=str(data.get("error_class") or ""),
        result=data,
        result_obj=raw_result,
        action_name="push_draft",
    )


def register_review_routes(app: Flask) -> None:
    @app.route("/wp/<site_id>/reviews/<plan_id>")
    @_auth_required
    def wp_review(site_id: str, plan_id: str):
        return render_review(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/comment", methods=["POST"])
    @_auth_required
    def wp_review_comment(site_id: str, plan_id: str):
        return run_review_action(
            site_id,
            plan_id,
            require_fn=file_admin,
            action_name="comment",
            action_fn=lambda ops: ops.comment(
                site_id, plan_id, request.form.get("text") or "", actor=g.actor["name"]
            ),
        )

    @app.route("/wp/<site_id>/reviews/<plan_id>/staging", methods=["POST"])
    @_auth_required
    def wp_review_staging(site_id: str, plan_id: str):
        base, work = _posted_hashes()
        return run_review_action(
            site_id,
            plan_id,
            require_fn=file_admin,
            action_name="staging",
            action_fn=lambda ops: ops.approve_staging(
                site_id, plan_id, actor=g.actor["name"], base_sha256=base, work_sha256=work
            ),
        )

    @app.route("/wp/<site_id>/reviews/<plan_id>/production", methods=["POST"])
    @_auth_required
    def wp_review_production(site_id: str, plan_id: str):
        base, work = _posted_hashes()
        return run_review_action(
            site_id,
            plan_id,
            require_fn=file_admin,
            action_name="production",
            default_error_class="BackupNotVerified",
            action_fn=lambda ops: ops.request_production(
                site_id, plan_id, actor=g.actor["name"], base_sha256=base, work_sha256=work
            ),
        )

    @app.route("/wp/<site_id>/reviews/<plan_id>/checks", methods=["POST"])
    @_auth_required
    def wp_review_checks(site_id: str, plan_id: str):
        return run_review_action(
            site_id,
            plan_id,
            require_fn=can_drill,
            action_name="checks",
            action_fn=lambda ops: ops.run_checks(site_id, plan_id, actor=g.actor["name"]),
        )

    @app.route("/wp/<site_id>/reviews/<plan_id>/extend", methods=["POST"])
    @_auth_required
    def wp_review_extend(site_id: str, plan_id: str):
        return run_review_action(
            site_id,
            plan_id,
            require_fn=file_admin,
            action_name="extend",
            action_fn=lambda ops: ops.extend(site_id, plan_id, actor=g.actor["name"]),
        )

    @app.route("/wp/<site_id>/reviews/<plan_id>/push-draft", methods=["POST"])
    @_auth_required
    def wp_review_push_draft(site_id: str, plan_id: str):
        return _post_review_push_draft(site_id, plan_id)

    @app.route("/wp/<site_id>/reviews/<plan_id>/artifact/<name>")
    @_auth_required
    def wp_review_artifact(site_id: str, plan_id: str, name: str):
        if not REVIEW_ID_RE.fullmatch(plan_id):
            return Response("Okänd plan", 404)
        if not file_admin():
            return Response("Saknar behörighet", 403)
        path, err = review_ops().artifact_file(site_id, plan_id, name, actor=g.actor["name"])
        if err is not None or path is None:
            blocked = review_http(err) if err is not None else None
            if blocked is not None:
                return blocked
            return Response("Okänd artefakt", 404)
        return Response(
            path.read_bytes(),
            mimetype="image/png",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
