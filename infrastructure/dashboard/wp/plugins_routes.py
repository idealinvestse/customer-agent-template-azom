"""Stateless WordPress plugin lifecycle, auto-update policy, MU-plugin, and recovery routes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import _dashboard_context, _flash, _fmt_dt

from .common import (
    CHANGE_ID_RE,
    SITE_ID_RE,
    can_read_wp,
    can_restore,
    file_admin,
    file_block,
    is_denied,
    wp_service,
)

PLU_ID_RE = re.compile(r"^plu-[a-f0-9]{12}$")
PAU_ID_RE = re.compile(r"^pau-[a-f0-9]{12}$")
PLR_ID_RE = re.compile(r"^plr-[a-f0-9]{12}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$", re.I)

_PLUGIN_CHANGE_CATEGORIES = frozenset(
    {
        "plugin_update",
        "plugin_autoupdate",
        "plugin_quarantine",
        "plugin_rollback",
        "plugin_toggle",
        "mu_plugin_deploy",
    }
)
_BRIDGE_SLUG = "azom-ops-bridge"


def _is_oscar_admin() -> bool:
    return bool(file_admin() and g.actor.get("is_oscar"))


def _is_oscar_restore() -> bool:
    return bool(can_restore() and g.actor.get("is_oscar"))


def _redirect_plugins(site_id: str, *, ok: bool, message: str):
    _flash(message, "ok" if ok else "error")
    return redirect(url_for("wp_plugins", site_id=site_id))


def _safe_slug(raw: str) -> str:
    slug = str(raw or "").strip()
    if not slug or ".." in slug or "/" in slug or "\\" in slug:
        return ""
    if not SLUG_RE.fullmatch(slug):
        return ""
    return slug


def _diagnostics_cache_path(data_dir: Path) -> Path:
    return data_dir / "wp_plugin_diagnostics.json"


def _load_cached_diagnostics(data_dir: Path, site_id: str) -> dict[str, Any]:
    from ecom_ops.wp_ops.config import is_mock_mode

    path = _diagnostics_cache_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        row = raw.get(site_id) if isinstance(raw, dict) else None
        if not isinstance(row, dict):
            return {}
        if bool(row.get("mock_mode")) != bool(is_mock_mode()):
            return {}
        diag = row.get("diagnostics")
        return dict(diag) if isinstance(diag, dict) else {}
    except Exception:
        return {}


def _save_cached_diagnostics(data_dir: Path, site_id: str, diagnostics: dict[str, Any]) -> None:
    from ecom_ops.wp_ops.config import is_mock_mode
    from ecom_ops.wp_ops.locks import advisory_file_lock
    from ecom_ops.wp_ops.ssh_settings import atomic_write_text

    path = _diagnostics_cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with advisory_file_lock(path.with_suffix(".json.lock"), mode=0o600):
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        existing[site_id] = {
            "mock_mode": bool(is_mock_mode()),
            "checked_at": datetime.now(UTC).isoformat(),
            "diagnostics": diagnostics,
        }
        atomic_write_text(path, json.dumps(existing, indent=2, ensure_ascii=False))


def _load_plans_from_file(path: Path, site_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    from ecom_ops.wp_ops.file_paths import plan_expired

    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    if isinstance(raw, list):
        items = [r for r in raw if isinstance(r, dict)]
    elif isinstance(raw, dict):
        plans_val = raw.get("plans")
        if isinstance(plans_val, list):
            items = [r for r in plans_val if isinstance(r, dict)]
        elif isinstance(plans_val, dict):
            items = [r for r in plans_val.values() if isinstance(r, dict)]
        else:
            items = [r for r in raw.values() if isinstance(r, dict)]
    filtered = [row for row in items if str(row.get("site_id") or "") == site_id]
    filtered.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    now_dt = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for row in filtered[:limit]:
        copy = dict(row)
        status = str(copy.get("status") or "planned").strip().lower()
        if status in {"planned", "pending", ""} and plan_expired(copy, now_dt):
            copy["status"] = "expired"
        copy["created_at_fmt"] = _fmt_dt(copy.get("created_at"))
        out.append(copy)
    return out


def _plugin_posture(site_id: str) -> dict[str, Any]:
    from ecom_ops.integrations.wp.bridge_client import bridge_enabled
    from ecom_ops.integrations.wp.sftp_deploy_transport import deploy_ssh_enabled
    from ecom_ops.runtime_env import env_flag
    from ecom_ops.wp_ops.config import is_mock_mode

    del site_id
    deploy_kill = env_flag("AZOM_WP_DEPLOY_KILL")
    plugin_kill = env_flag("AZOM_WP_PLUGIN_MUTATE_KILL")
    return {
        "mock_mode": is_mock_mode(),
        "bridge_enabled": bridge_enabled(),
        "deploy_ssh_enabled": deploy_ssh_enabled(),
        "plugin_updates_enabled": env_flag("AZOM_WP_PLUGIN_UPDATES_ENABLED"),
        "autoupdate_write_enabled": env_flag("AZOM_WP_AUTOUPDATE_WRITE_ENABLED"),
        "restore_enabled": env_flag("AZOM_WP_RESTORE_ENABLED"),
        "deploy_kill_active": deploy_kill,
        "plugin_mutate_kill_active": plugin_kill,
        "kill_switch_active": bool(deploy_kill or plugin_kill),
    }


def _extract_inventory(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[Any]]:
    inv = data.get("inventory") if isinstance(data.get("inventory"), dict) else data
    plugins = [p for p in (inv.get("plugins") or []) if isinstance(p, dict)]
    mu_plugins = [m for m in (inv.get("mu_plugins") or []) if isinstance(m, dict)]
    dropins = [d for d in (inv.get("dropins") or []) if isinstance(d, dict)]
    constants = dict(inv.get("constants") or {}) if isinstance(inv.get("constants"), dict) else {}
    filters = list(inv.get("autoupdate_filters") or [])
    return plugins, mu_plugins, dropins, constants, filters


def _extract_plan_meta(data: dict[str, Any]) -> tuple[str, str]:
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else data
    plan_id = str(plan.get("plan_id") or data.get("plan_id") or "").strip()
    plan_sha = str(plan.get("plan_sha256") or data.get("plan_sha256") or "").strip()
    return plan_id, plan_sha


def register_plugins_routes(app: Flask) -> None:
    @app.route("/wp/<site_id>/plugins")
    @_auth_required
    def wp_plugins(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        svc = wp_service()
        actor = g.actor["name"]
        shown = svc.show_site(site_id, actor=actor)
        blocked = file_block(site_id, shown)
        if blocked is not None:
            return blocked
        if not can_read_wp():
            return Response("Saknar behörighet", 403)
        site = (shown.data or {}).get("site")
        can_admin = _is_oscar_admin()
        can_rec = _is_oscar_restore()

        inv_res = svc.plugin_inventory(site_id, actor=actor, include_updates=True, include_wporg=False)
        if is_denied(inv_res):
            return Response("Saknar behörighet", 403)
        inv_error = "" if inv_res.ok else str(inv_res.message or "Kunde inte läsa plugininventering.")
        plugins, mu_plugins, dropins, constants, autoupdate_filters = _extract_inventory(
            dict(inv_res.data or {})
        )

        # Ensure MU-plugins & drop-ins are always populated even if inventory omitted them
        if not mu_plugins and not dropins:
            mu_res = svc.mu_plugins_and_dropins(site_id, actor=actor)
            if mu_res.ok:
                mu_data = dict(mu_res.data or {})
                mu_plugins = [m for m in (mu_data.get("mu_plugins") or []) if isinstance(m, dict)]
                dropins = [d for d in (mu_data.get("dropins") or []) if isinstance(d, dict)]

        mu_plugins.sort(key=lambda m: (int(m.get("load_order") or 0), str(m.get("file") or "")))

        policy_res = svc.get_autoupdate_policy(site_id, actor=actor)
        autoupdate_policy = (
            dict((policy_res.data or {}).get("policy") or {}) if policy_res.ok else {}
        )
        drift_res = svc.check_autoupdate_drift(site_id, actor=actor)
        autoupdate_drift = dict(drift_res.data or {}) if drift_res.ok else {}

        update_plans: list[dict[str, Any]] = []
        plans_res = svc.list_plugin_update_plans(site_id, actor=actor)
        if plans_res.ok:
            for item in (plans_res.data or {}).get("plans") or []:
                if isinstance(item, dict):
                    row = dict(item)
                    row["created_at_fmt"] = _fmt_dt(row.get("created_at"))
                    update_plans.append(row)

        autoupdate_plans: list[dict[str, Any]] = []
        recovery_plans: list[dict[str, Any]] = []
        if can_admin:
            autoupdate_plans = _load_plans_from_file(
                svc.data_dir / "wp_plugin_autoupdate_plans.json", site_id
            )
        if can_rec:
            recovery_plans = _load_plans_from_file(
                svc.data_dir / "wp_plugin_recovery_plans.json", site_id
            )

        listed_changes = svc.list_changes(site_id, actor=actor, limit=30)
        plugin_changes: list[dict[str, Any]] = []
        if listed_changes.ok:
            for row in (listed_changes.data or {}).get("changes") or []:
                if isinstance(row, dict) and str(row.get("category") or "") in _PLUGIN_CHANGE_CATEGORIES:
                    copy = dict(row)
                    copy["created_at_fmt"] = _fmt_dt(copy.get("created_at"))
                    plugin_changes.append(copy)

        diagnostics = _load_cached_diagnostics(svc.data_dir, site_id) if can_admin else {}
        posture = _plugin_posture(site_id)
        pending_count = sum(1 for p in plugins if p.get("update_available"))
        active_count = sum(1 for p in plugins if p.get("active"))
        critical_count = sum(1 for p in plugins if p.get("is_critical"))

        return render_template(
            "wp_plugins.html",
            **_dashboard_context(
                site=site,
                posture=posture,
                inv_error=inv_error,
                plugins=plugins,
                mu_plugins=mu_plugins,
                dropins=dropins,
                constants=constants,
                autoupdate_filters=autoupdate_filters,
                autoupdate_policy=autoupdate_policy,
                autoupdate_drift=autoupdate_drift,
                update_plans=update_plans,
                autoupdate_plans=autoupdate_plans,
                recovery_plans=recovery_plans,
                plugin_changes=plugin_changes[:15],
                diagnostics=diagnostics,
                pending_count=pending_count,
                active_count=active_count,
                critical_count=critical_count,
                can_admin=can_admin,
                can_restore=can_rec,
            ),
        )

    @app.route("/wp/<site_id>/plugins/plan-update", methods=["POST"])
    @_auth_required
    def wp_plugins_plan_update(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_admin():
            return Response("Saknar behörighet", 403)
        mode = (request.form.get("selection_mode") or "policy").strip().lower()
        if mode not in {"single", "selected", "all", "policy"}:
            mode = "policy"
        policy = (request.form.get("policy") or "non_critical_only").strip().lower()
        if policy not in {"security_only", "patch_only", "minor_safe", "non_critical_only"}:
            policy = "non_critical_only"
        raw_slugs = request.form.getlist("slugs")
        slugs_csv = (request.form.get("slugs_csv") or "").strip()
        slugs = [s.strip() for s in raw_slugs if s.strip()]
        if slugs_csv:
            slugs.extend(s.strip() for s in slugs_csv.split(",") if s.strip())
        exclude_csv = (request.form.get("exclude_slugs") or "").strip()
        exclude_slugs = [s.strip() for s in exclude_csv.split(",") if s.strip()] or None
        include_critical = (request.form.get("include_critical") or "") in {"1", "true", "on"}

        res = wp_service().plan_plugin_updates(
            site_id,
            actor=g.actor["name"],
            selection_mode=mode,
            slugs=slugs or None,
            policy=policy,
            exclude_slugs=exclude_slugs,
            include_critical=include_critical,
            canary=True,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        plan_id, plan_sha = _extract_plan_meta(dict(res.data or {}))
        msg = res.message
        if res.ok and plan_id:
            msg = f"Uppdateringsplan skapad: {plan_id} (SHA-256: {plan_sha[:12]}…). Granska och godkänn nedan."
        return _redirect_plugins(site_id, ok=res.ok, message=msg)

    @app.route("/wp/<site_id>/plugins/execute-update", methods=["POST"])
    @_auth_required
    def wp_plugins_execute_update(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_admin():
            return Response("Saknar behörighet", 403)
        plan_id = (request.form.get("plan_id") or "").strip()
        expected_sha256 = (request.form.get("expected_sha256") or "").strip().lower()
        allow_critical = (request.form.get("allow_critical") or "") in {"1", "true", "on"}
        if not PLU_ID_RE.fullmatch(plan_id) or not SHA256_RE.fullmatch(expected_sha256):
            return _redirect_plugins(
                site_id,
                ok=False,
                message="Ogiltigt plan-ID (plu-*) eller saknad SHA-256-hash.",
            )
        res = wp_service().execute_plugin_update_plan(
            site_id,
            plan_id,
            actor=g.actor["name"],
            expected_sha256=expected_sha256,
            allow_critical=allow_critical,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_plugins(site_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/plugins/autoupdate/plan", methods=["POST"])
    @_auth_required
    def wp_plugins_autoupdate_plan(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_admin():
            return Response("Saknar behörighet", 403)
        preset = (request.form.get("preset") or "non_critical_only").strip().lower()
        if preset not in {"disable_all", "non_critical_only", "security_only", "custom"}:
            preset = "non_critical_only"
        allow_critical = (request.form.get("allow_critical") or "") in {"1", "true", "on"}
        changes: list[dict[str, Any]] | None = None
        raw_changes = (request.form.get("changes_json") or "").strip()
        if raw_changes:
            try:
                parsed = json.loads(raw_changes)
            except json.JSONDecodeError:
                return _redirect_plugins(
                    site_id, ok=False, message="Ogiltig JSON för auto-uppdateringsändringar."
                )
            if not isinstance(parsed, list):
                return _redirect_plugins(
                    site_id, ok=False, message="changes_json måste vara en JSON-lista."
                )
            changes = parsed
        res = wp_service().plan_plugin_autoupdate(
            site_id,
            actor=g.actor["name"],
            preset=preset,
            changes=changes,
            allow_critical=allow_critical,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        plan_id, plan_sha = _extract_plan_meta(dict(res.data or {}))
        msg = res.message
        if res.ok and plan_id:
            msg = f"Auto-uppdateringsplan skapad: {plan_id} (SHA-256: {plan_sha[:12]}…)."
        return _redirect_plugins(site_id, ok=res.ok, message=msg)

    @app.route("/wp/<site_id>/plugins/autoupdate/execute", methods=["POST"])
    @_auth_required
    def wp_plugins_autoupdate_execute(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_admin():
            return Response("Saknar behörighet", 403)
        plan_id = (request.form.get("plan_id") or "").strip()
        expected_sha256 = (request.form.get("expected_sha256") or "").strip().lower()
        if not PAU_ID_RE.fullmatch(plan_id) or not SHA256_RE.fullmatch(expected_sha256):
            return _redirect_plugins(
                site_id,
                ok=False,
                message="Ogiltigt auto-uppdateringsplan-ID (pau-*) eller SHA-256-hash.",
            )
        res = wp_service().execute_plugin_autoupdate_plan(
            site_id,
            plan_id,
            actor=g.actor["name"],
            expected_sha256=expected_sha256,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_plugins(site_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/plugins/diagnostics", methods=["POST"])
    @_auth_required
    def wp_plugins_diagnostics(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_admin():
            return Response("Saknar behörighet", 403)
        raw_hooks = (request.form.get("hooks") or "").strip()
        hooks = [h.strip() for h in raw_hooks.split(",") if h.strip()] or None
        try:
            log_lines = max(10, min(200, int(request.form.get("log_lines") or 80)))
        except ValueError:
            log_lines = 80
        svc = wp_service()
        res = svc.plugin_diagnostics(
            site_id,
            actor=g.actor["name"],
            hooks=hooks,
            log_lines=log_lines,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        if res.ok:
            _save_cached_diagnostics(svc.data_dir, site_id, dict(res.data or {}))
        return _redirect_plugins(site_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/plugins/recovery/plan", methods=["POST"])
    @_auth_required
    def wp_plugins_recovery_plan(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_restore():
            return Response("Saknar behörighet", 403)
        slug = _safe_slug(request.form.get("slug") or "")
        action = (request.form.get("action") or "").strip().lower()
        change_id = (request.form.get("change_id") or "").strip().lower() or None
        if not slug:
            return _redirect_plugins(site_id, ok=False, message="Ange ett giltigt plugin-slug.")
        if slug.lower() == _BRIDGE_SLUG:
            return _redirect_plugins(
                site_id,
                ok=False,
                message="azom-ops-bridge är självskyddat och får inte avaktiveras eller sättas i karantän.",
            )
        if action not in {"rollback_snapshot", "deactivate", "quarantine_sftp", "restore_from_quarantine"}:
            return _redirect_plugins(site_id, ok=False, message="Ogiltig återställningsåtgärd.")
        if change_id and not CHANGE_ID_RE.fullmatch(change_id):
            return _redirect_plugins(site_id, ok=False, message="Ogiltigt change-id (chg-*).")
        res = wp_service().plan_plugin_recovery(
            site_id,
            actor=g.actor["name"],
            slug=slug,
            action=action,
            change_id=change_id,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        plan_id, plan_sha = _extract_plan_meta(dict(res.data or {}))
        msg = res.message
        if res.ok and plan_id:
            msg = f"Återställningsplan skapad: {plan_id} (SHA-256: {plan_sha[:12]}…)."
        return _redirect_plugins(site_id, ok=res.ok, message=msg)

    @app.route("/wp/<site_id>/plugins/recovery/execute", methods=["POST"])
    @_auth_required
    def wp_plugins_recovery_execute(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_restore():
            return Response("Saknar behörighet", 403)
        plan_id = (request.form.get("plan_id") or "").strip()
        expected_sha256 = (request.form.get("expected_sha256") or "").strip().lower()
        if not PLR_ID_RE.fullmatch(plan_id) or not SHA256_RE.fullmatch(expected_sha256):
            return _redirect_plugins(
                site_id,
                ok=False,
                message="Ogiltigt återställningsplan-ID (plr-*) eller SHA-256-hash.",
            )
        res = wp_service().execute_plugin_recovery_plan(
            site_id,
            plan_id,
            actor=g.actor["name"],
            expected_sha256=expected_sha256,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_plugins(site_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/plugins/quarantine", methods=["POST"])
    @_auth_required
    def wp_plugins_quarantine(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_restore():
            return Response("Saknar behörighet", 403)
        slug = _safe_slug(request.form.get("slug") or "")
        kind = (request.form.get("kind") or "plugin").strip().lower()
        if kind not in {"plugin", "mu_plugin"}:
            kind = "plugin"
        if not slug:
            return _redirect_plugins(site_id, ok=False, message="Ange ett giltigt plugin-slug för karantän.")
        if slug.lower() == _BRIDGE_SLUG:
            return _redirect_plugins(
                site_id,
                ok=False,
                message="azom-ops-bridge är självskyddat och kan inte sättas i karantän.",
            )
        res = wp_service().quarantine_plugin(
            site_id,
            slug,
            actor=g.actor["name"],
            kind=kind,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_plugins(site_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/plugins/unquarantine", methods=["POST"])
    @_auth_required
    def wp_plugins_unquarantine(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not _is_oscar_restore():
            return Response("Saknar behörighet", 403)
        slug = _safe_slug(request.form.get("slug") or "")
        raw_quarantine_name = (request.form.get("quarantine_name") or "").strip()
        quarantine_name = _safe_slug(raw_quarantine_name) if raw_quarantine_name else ""
        if raw_quarantine_name and not quarantine_name:
            return _redirect_plugins(
                site_id,
                ok=False,
                message="Ogiltigt karantännamn.",
            )
        kind = (request.form.get("kind") or "plugin").strip().lower()
        if kind not in {"plugin", "mu_plugin"}:
            kind = "plugin"
        if not slug:
            return _redirect_plugins(site_id, ok=False, message="Ange ett giltigt plugin-slug för återställning.")
        if slug.lower() == _BRIDGE_SLUG:
            return _redirect_plugins(
                site_id,
                ok=False,
                message="azom-ops-bridge är självskyddat.",
            )
        res = wp_service().unquarantine_plugin(
            site_id,
            slug,
            actor=g.actor["name"],
            quarantine_name=quarantine_name,
            kind=kind,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_plugins(site_id, ok=res.ok, message=res.message)


__all__ = ["register_plugins_routes"]
