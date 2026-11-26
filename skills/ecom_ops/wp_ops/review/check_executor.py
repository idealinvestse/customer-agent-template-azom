"""Check catalog execution and current-evidence verification for review plans.

Looks up ``run_catalog`` through the ``ecom_ops.wp_ops.review_plan`` facade module
so test monkeypatches targeting ``ecom_ops.wp_ops.review_plan.run_catalog`` remain
effective without leaking real local processes.
"""

from __future__ import annotations

import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.file_transport import LocalTree
from ecom_ops.wp_ops.models import WpOpsResult
from ecom_ops.wp_ops.review.checks import (
    PROFILE,
    PROFILE_VERSION,
    bind_artifacts,
    requirements,
    run_catalog,
    run_is_publishable,
    stamp_run,
    suite_status,
)
from ecom_ops.wp_ops.review.preview_coordinator import list_artifact_pngs
from ecom_ops.wp_ops.review.runner import sandbox_enabled
from ecom_ops.wp_ops.review.store import (
    _fail,
    _generation,
    _ok,
    _resolve_parent_theme,
    _text_files,
    _unified,
)
from ecom_ops.wp_ops.review.workspace_io import (
    ReviewError,
    tree_hash,
    workspace_root,
    write_diff,
    write_manifest,
)

_FACADE_MODULE = "ecom_ops.wp_ops.review_plan"


def resolve_run_catalog() -> Callable[..., list[dict[str, Any]]]:
    """Return ``review_plan.run_catalog`` when loaded so facade monkeypatches take effect."""
    facade = sys.modules.get(_FACADE_MODULE)
    if facade is not None:
        fn = getattr(facade, "run_catalog", None)
        if callable(fn):
            return fn
    return run_catalog


def verify_current_evidence(data_dir: Path, plan: dict[str, Any]) -> WpOpsResult | None:
    """Return ``None`` when ``plan`` has fresh, publishable check evidence for its current tree."""
    plan_id = str(plan.get("plan_id") or "")
    site_id = str(plan.get("site_id") or "")
    try:
        root = workspace_root(data_dir, site_id, plan_id)
    except ReviewError as exc:
        return _fail(exc.error_class, exc.error_class, plan_id=plan_id)
    if not (root / "base").is_dir() or not (root / "work").is_dir():
        return _fail(
            "WorkspaceNotResumable",
            "Arbetsytan saknas.",
            plan_id=plan_id,
            status="not_resumable",
        )
    base_now = tree_hash(root / "base")
    work_now = tree_hash(root / "work")
    if base_now != plan.get("base_sha256") or work_now != plan.get("work_sha256"):
        return _fail("RevisionStale", "Revisionen har ändrats.", plan_id=plan_id, site_id=site_id)
    files = _text_files(root / "work")
    kind = str(plan.get("kind") or "")
    risk = str(plan.get("risk") or "low")
    run = plan.get("check_run") if isinstance(plan.get("check_run"), dict) else {}
    if not run_is_publishable(kind, files, risk, list(plan.get("checks") or []), run):
        return _fail("EvidenceMissing", "Körningsbeviset räcker inte.", plan_id=plan_id, site_id=site_id)
    if run.get("base_sha256") != base_now or run.get("work_sha256") != work_now:
        return _fail(
            "RevisionStale",
            "Körningen gäller en äldre revision.",
            plan_id=plan_id,
            site_id=site_id,
        )
    if run.get("site_id") != site_id or run.get("plan_id") != plan_id:
        return _fail(
            "EvidenceMismatch",
            "Körningen gäller en annan plan.",
            plan_id=plan_id,
            site_id=site_id,
        )
    return None


def run_sandbox_catalog(
    plans_obj: Any,
    plan: dict[str, Any],
    root: Path,
    kind: str,
    files: list[tuple[str, str]],
    risk: str,
    artifact_dir: Path,
) -> list[dict[str, Any]]:
    """Run local vertical theme/plugin sandbox checks when enabled, falling back to ``run_catalog``."""
    catalog_fn = resolve_run_catalog()
    site_id = str(plan.get("site_id") or "")
    plan_id = str(plan.get("plan_id") or "")
    slug = str(plan.get("slug") or "")
    site, missing = plans_obj._site(site_id)
    if missing is not None or site is None:
        return catalog_fn(kind, files, risk, artifact_dir=artifact_dir)
    try:
        if kind == "theme":
            from ecom_ops.integrations.wp_browser_smoke import run_vertical_theme_render

            parent_theme_slug: str | None = None
            parent_theme_dir: Path | None = None
            if plan.get("theme_relationship") == "child" or (root / "base" / "style.css").is_file():
                resolved_parent = _resolve_parent_theme(root, slug, None)
                if resolved_parent:
                    parent_theme_slug = resolved_parent
                    transport = plans_obj._transport(site)
                    if isinstance(transport, LocalTree):
                        themes_dir = (transport.local_root / "wp-content" / "themes").resolve()
                        candidate = (themes_dir / resolved_parent).resolve()
                        if (
                            candidate.parent == themes_dir
                            and not candidate.is_symlink()
                            and candidate.is_dir()
                        ):
                            parent_theme_dir = candidate
            kwargs: dict[str, Any] = {
                "site": site,
                "site_id": site_id,
                "plan_id": plan_id,
                "slug": slug,
                "base_dir": root / "base",
                "work_dir": root / "work",
                "artifact_dir": artifact_dir,
                "risk": risk,
            }
            if parent_theme_slug is not None:
                kwargs["parent_theme_slug"] = parent_theme_slug
                if parent_theme_dir is not None:
                    kwargs["parent_theme_dir"] = parent_theme_dir
            outcome = run_vertical_theme_render(**kwargs)
            return list(outcome["checks"])
        if kind == "plugin":
            from ecom_ops.integrations.wp_browser_smoke import run_vertical_plugin_smoke

            outcome = run_vertical_plugin_smoke(
                site=site,
                site_id=site_id,
                plan_id=plan_id,
                slug=slug,
                base_dir=root / "base",
                work_dir=root / "work",
                artifact_dir=artifact_dir,
                risk=risk,
            )
            return list(outcome["checks"])
    except ReviewError:
        return catalog_fn(kind, files, risk, artifact_dir=artifact_dir)
    return catalog_fn(kind, files, risk, artifact_dir=artifact_dir)


def _maybe_run_theme_preview(
    plans_obj: Any,
    site_id: str,
    plan_id: str,
    kind: str,
    *,
    actor: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if kind != "theme":
        return None, None
    from ecom_ops.integrations.wp_live_preview import live_preview_enabled

    if not live_preview_enabled():
        return None, None
    try:
        pv = plans_obj.live_preview(site_id, plan_id, actor=actor, path="/")
        if pv.ok:
            return dict(pv.data or {}), None
        return None, str((pv.data or {}).get("error_class") or "LivePreviewFailed")
    except ReviewError as exc:
        return None, exc.error_class
    except Exception:
        return None, "LivePreviewFailed"


def execute_checks(
    plans_obj: Any,
    site_id: str,
    plan_id: str,
    *,
    actor: str,
) -> WpOpsResult:
    """Run check catalog for one review plan, stamp evidence, and optionally run live preview."""
    denied = plans_obj._need(actor, Permission.WP_OPS_OPERATE, action="wp_review_checks")
    if denied is not None:
        return denied
    with plans_obj._locks(site_id, plan_id):
        plan = plans_obj._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        blocked = plans_obj._fresh(plan)
        if blocked is not None:
            return blocked
        if plan.get("status") in {"rejected", "fork_not_approved", "draft"}:
            return _fail(
                str(plan.get("error_class") or "NotValidated"),
                "Planen är inte validerad.",
            )
        root = workspace_root(plans_obj.data_dir, site_id, plan_id)
        files = _text_files(root / "work")
        kind = str(plan.get("kind") or "")
        risk = str(plan.get("risk") or "low")
        policy = requirements(kind, files, risk)
        generation = _generation(plan) + 1
        run_id = "cr-" + secrets.token_hex(6)
        snapshot = {
            "run_id": run_id,
            "site_id": site_id,
            "plan_id": plan_id,
            "base_sha256": tree_hash(root / "base"),
            "work_sha256": tree_hash(root / "work"),
            "policy": policy,
            "profile": PROFILE,
            "profile_version": PROFILE_VERSION,
            "started_at": plans_obj._now().isoformat(),
            "finished_at": "",
            "published": False,
        }
        plan["check_generation"] = generation
        plan["status"] = "checks_running"
        plan["check_run"] = snapshot
        plans_obj._replace(plan)
        artifact_dir = root / "artifacts"

    try:
        if sandbox_enabled():
            results = plans_obj._run_sandbox_catalog(plan, root, kind, files, risk, artifact_dir)
        else:
            results = resolve_run_catalog()(kind, files, risk, artifact_dir=artifact_dir)
        crashed = False
    except Exception:
        results = []
        crashed = True

    with plans_obj._locks(site_id, plan_id):
        plan = plans_obj._row(site_id, plan_id)
        if plan is None:
            return _fail("UnknownPlan", "Okänd plan.")
        if plan.get("status") == "not_resumable":
            return _fail(
                "WorkspaceNotResumable",
                "Planens arbetsyta finns inte i backupen och kan inte återupptas.",
                plan_id=plan_id,
                status="not_resumable",
            )
        if plan.get("status") == "expired" or plans_obj._past(plan):
            if plan.get("status") != "expired":
                plans_obj._mark_expired(plan)
            return _fail("Expired", "Planen har gått ut.", plan_id=plan_id, status="expired")
        if _generation(plan) != generation:
            return _fail(
                "StaleRun",
                "En nyare ändring finns. Den här körningen skrevs inte.",
                plan_id=plan_id,
                site_id=site_id,
                status=plan.get("status"),
            )
        root = workspace_root(plans_obj.data_dir, site_id, plan_id)
        base_now = tree_hash(root / "base")
        work_now = tree_hash(root / "work")
        stamped = stamp_run(
            results,
            run_id=run_id,
            site_id=site_id,
            plan_id=plan_id,
            base_sha256=str(snapshot["base_sha256"]),
            work_sha256=str(snapshot["work_sha256"]),
            policy=policy,
            profile=PROFILE,
            profile_version=PROFILE_VERSION,
        )
        bind_artifacts(artifact_dir, stamped, snapshot)
        moved = base_now != snapshot["base_sha256"] or work_now != snapshot["work_sha256"]
        status = "checks_incomplete" if crashed or moved else suite_status(kind, files, risk, stamped)
        published = False
        if status == "tests_passed" and not moved and not crashed:
            candidate = {**snapshot, "published": True, "finished_at": plans_obj._now().isoformat()}
            if run_is_publishable(kind, files, risk, stamped, candidate):
                published = True
                snapshot = candidate
            else:
                status = "checks_incomplete"
        if not published:
            snapshot = {**snapshot, "published": False, "finished_at": plans_obj._now().isoformat()}
        plan["checks"] = stamped
        plan["status"] = status
        plan["check_run"] = snapshot
        plan["base_sha256"] = base_now
        plan["work_sha256"] = work_now
        plan["error_class"] = "RevisionChanged" if moved else ""
        write_diff(root, _unified(root))
        write_manifest(root, plan)
        plans_obj._replace(plan)

    for row in results:
        plans_obj._audit(
            actor,
            plan,
            "wp_review_check",
            row.get("status") == "passed",
            job=row.get("job"),
            outcome=row.get("status"),
            run_id=run_id,
        )

    browser_row = next(
        (r for r in results if isinstance(r, dict) and r.get("job") == "browser_smoke"),
        None,
    )
    rep_sandbox = (
        isinstance(browser_row, dict)
        and browser_row.get("status") in {"passed", "failed"}
        and browser_row.get("reason") != "sandbox_not_representative"
    )
    sandbox_shots = (
        [n for n in list_artifact_pngs(artifact_dir) if n.startswith(("base-", "work-"))]
        if rep_sandbox
        else []
    )
    preview_data, preview_error = _maybe_run_theme_preview(
        plans_obj, site_id, plan_id, kind, actor=actor
    )

    screenshots: list[str] = []
    if preview_data:
        for name in preview_data.get("screenshots") or []:
            if isinstance(name, str) and name not in screenshots:
                screenshots.append(name)
    for name in sandbox_shots:
        if name not in screenshots:
            screenshots.append(name)
    screenshots = screenshots[:4]

    out_extra: dict[str, Any] = {
        "plan_id": plan_id,
        "site_id": site_id,
        "status": status,
        "checks": stamped,
        "check_run": snapshot,
        "screenshots": screenshots,
        "screenshot_paths": [str(artifact_dir / name) for name in screenshots],
        "sandbox_representative": rep_sandbox,
    }
    if preview_data is not None:
        out_extra["preview"] = preview_data
        out_extra["preview_status"] = "ok"
        out_extra["preview_note"] = (
            "Live-overlay-förhandsvisning lyckades med riktiga skärmdumpar från livesajten "
            "(lokal PHP-sandlåda är inte representativ för temat)."
            if not rep_sandbox
            else "Live-overlay-förhandsvisning lyckades."
        )
    elif preview_error is not None:
        out_extra["preview_error"] = preview_error
        out_extra["preview_status"] = "failed"
        out_extra["preview_note"] = f"Live-overlay-förhandsvisning misslyckades ({preview_error})."
    else:
        out_extra["preview_status"] = "skipped"
        out_extra["preview_note"] = ""

    msg = (
        "Kontrollerna är bokförda. Live-overlay-förhandsvisning skapades med riktiga skärmdumpar "
        "från livesajten (lokal PHP-sandlåda är inte representativ för temat)."
        if preview_data is not None and not rep_sandbox
        else "Kontrollerna är bokförda."
    )
    return _ok(msg, **out_extra)


__all__ = [
    "execute_checks",
    "resolve_run_catalog",
    "run_sandbox_catalog",
    "verify_current_evidence",
]
