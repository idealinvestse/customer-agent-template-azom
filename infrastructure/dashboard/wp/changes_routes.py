"""Change history, per-change snapshot verification, rollback, and Bridge ops routes."""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, g, redirect, render_template, request, url_for

from auth import _auth_required
from support import _dashboard_context, _flash, _flash_q, _fmt_dt

from .common import (
    CHANGE_ID_RE,
    SITE_ID_RE,
    can_drill,
    can_read_wp,
    can_restore,
    file_admin,
    file_block,
    is_denied,
    wp_service,
)

BATCH_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{3,63}$")


def _repo_bin_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "bin"


def _bridge_health_cache_path(data_dir: Path) -> Path:
    return data_dir / "wp_bridge_health.json"


def _load_cached_bridge_health(data_dir: Path, site_id: str) -> dict[str, Any]:
    from ecom_ops.wp_ops.config import is_mock_mode

    path = _bridge_health_cache_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        row = raw.get(site_id) if isinstance(raw, dict) else None
        if not isinstance(row, dict):
            return {}
        if "mock_mode" in row and "health" in row and isinstance(row.get("health"), dict):
            if bool(row.get("mock_mode")) != bool(is_mock_mode()):
                return {}
            return dict(row["health"])
        return row
    except Exception:
        return {}


def _save_cached_bridge_health(data_dir: Path, site_id: str, health: dict[str, Any]) -> None:
    from ecom_ops.wp_ops.config import is_mock_mode
    from ecom_ops.wp_ops.locks import advisory_file_lock
    from ecom_ops.wp_ops.ssh_settings import atomic_write_text

    path = _bridge_health_cache_path(data_dir)
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
            "health": health,
        }
        atomic_write_text(path, json.dumps(existing, indent=2, ensure_ascii=False))


def _site_fe_plans(svc: Any, site_id: str) -> list[dict[str, Any]]:
    try:
        state = svc.store.load()
    except Exception:
        return []
    rows = [
        row
        for row in (state.get("file_edits") or [])
        if isinstance(row, dict) and row.get("site_id") == site_id
    ]
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[:20]


def _posture_summary(site_id: str, svc: Any, actor: str) -> dict[str, Any]:
    from ecom_ops.integrations.wp.bridge_client import bridge_enabled, bridge_secret_env_name
    from ecom_ops.integrations.wp.sftp_deploy_transport import deploy_ssh_enabled
    from ecom_ops.runtime_env import env_flag
    from ecom_ops.wp_ops.config import is_mock_mode, ssh_files_enabled
    from ecom_ops.wp_ops.deploy_orchestrator import live_deploy_enabled
    from ecom_ops.wp_ops.registry import load_sites, site_by_id
    from ecom_ops.wp_ops.ssh_settings import describe_deploy_ssh

    bridge_secret_present = False
    deploy_ssh: dict[str, Any] = {}
    try:
        site = site_by_id(load_sites(svc.registry_path, data_dir=svc.data_dir), site_id)
    except Exception:
        site = None
    if site is not None:
        import os

        env_name = bridge_secret_env_name(site.id)
        bridge_secret_present = len(os.environ.get(env_name, "").strip()) >= 32
        deploy_ssh = describe_deploy_ssh(site, svc.data_dir, include_paths=file_admin())
    return {
        "mock_mode": is_mock_mode(),
        "read_ssh_enabled": ssh_files_enabled(),
        "deploy_ssh_enabled": deploy_ssh_enabled(),
        "bridge_enabled": bridge_enabled(),
        "bridge_secret_present": bridge_secret_present,
        "live_deploy_enabled": live_deploy_enabled(),
        "restore_enabled": env_flag("AZOM_WP_RESTORE_ENABLED"),
        "kill_switch_active": env_flag("AZOM_WP_DEPLOY_KILL"),
        "deploy_ssh": deploy_ssh,
        "actor": actor,
    }


def _redirect_changes(
    site_id: str, *, ok: bool, message: str, batch_id: str | None = None
):
    _flash(message, "ok" if ok else "error")
    frag = _flash_q(message) if ok else _flash_q(err=message)
    base = url_for("wp_changes", site_id=site_id)
    if batch_id:
        return redirect(f"{base}?batch_id={batch_id}&{frag}")
    return redirect(f"{base}?{frag}")


def _redirect_change_detail(site_id: str, change_id: str, *, ok: bool, message: str):
    _flash(message, "ok" if ok else "error")
    frag = _flash_q(message) if ok else _flash_q(err=message)
    return redirect(url_for("wp_change_detail", site_id=site_id, change_id=change_id) + f"?{frag}")


def _list_changes_with_batch(
    svc: Any,
    site_id: str,
    *,
    actor: str,
    category: str | None,
    status: str | None,
    batch_id: str | None,
    limit: int = 50,
) -> tuple[Any, list[dict[str, Any]]]:
    params = inspect.signature(svc.list_changes).parameters
    if batch_id and "batch_id" in params:
        listed = svc.list_changes(
            site_id,
            actor=actor,
            category=category,
            status=status,
            batch_id=batch_id,
            limit=limit,
        )
        return listed, list((listed.data or {}).get("changes") or [])

    listed = svc.list_changes(
        site_id,
        actor=actor,
        category=category,
        status=status,
        limit=200 if batch_id else limit,
    )
    if is_denied(listed):
        return listed, []

    if not batch_id:
        return listed, list((listed.data or {}).get("changes") or [])

    try:
        from ecom_ops.wp_ops.change_ledger import ChangeLedger

        ledger = ChangeLedger(data_dir=svc.data_dir)
        l_params = inspect.signature(ledger.list_changes).parameters
        if "batch_id" in l_params:
            rows = ledger.list_changes(
                site_id,
                category=category,
                status=status,
                batch_id=batch_id,
                limit=limit,
            )
            return listed, list(rows)
    except Exception:
        pass

    raw_rows = list((listed.data or {}).get("changes") or [])
    filtered = [
        r
        for r in raw_rows
        if isinstance(r, dict)
        and (
            str(r.get("batch_id") or "") == batch_id
            or str((r.get("metadata") or {}).get("batch_id") or "") == batch_id
        )
    ]
    return listed, filtered[:limit]


def _preview_batch_rollback_safe(
    data_dir: Path,
    site_id: str,
    batch_id: str,
    *,
    actor: str,
    fallback_changes: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        from ecom_ops.wp_ops.rollback_service import ChangeRollbackService

        rsvc = ChangeRollbackService(data_dir=data_dir)
        fn = getattr(rsvc, "preview_batch_rollback", None)
        if callable(fn):
            res = fn(site_id, batch_id, actor=actor)
            if isinstance(res, dict):
                return res
    except Exception:
        pass
    rollbackable = [
        c
        for c in fallback_changes
        if c.get("snapshot_verified")
        and c.get("status") != "rolled_back"
        and not c.get("rolled_back_by")
    ]
    return {
        "ok": True,
        "site_id": site_id,
        "batch_id": batch_id,
        "total_changes": len(fallback_changes),
        "rollbackable_count": len(rollbackable),
        "can_rollback": bool(rollbackable),
    }


def register_changes_routes(app: Flask) -> None:
    @app.route("/wp/<site_id>/changes")
    @_auth_required
    def wp_changes(site_id: str):
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
        category = (request.args.get("category") or "").strip() or None
        status = (request.args.get("status") or "").strip() or None
        raw_batch = (request.args.get("batch_id") or "").strip()
        batch_id = raw_batch if (raw_batch and BATCH_ID_RE.fullmatch(raw_batch)) else None
        listed, changes = _list_changes_with_batch(
            svc,
            site_id,
            actor=actor,
            category=category,
            status=status,
            batch_id=batch_id,
            limit=50,
        )
        if is_denied(listed):
            return Response("Saknar behörighet", 403)
        for row in changes:
            row["created_at_fmt"] = _fmt_dt(row.get("created_at"))
        batch_preview = (
            _preview_batch_rollback_safe(
                svc.data_dir,
                site_id,
                batch_id,
                actor=actor,
                fallback_changes=changes,
            )
            if batch_id
            else {}
        )
        fe_plans = _site_fe_plans(svc, site_id)
        for fp in fe_plans:
            fp["created_at_fmt"] = _fmt_dt(fp.get("created_at"))
        is_oscar_admin = bool(can_restore() and g.actor.get("is_oscar"))
        posture = _posture_summary(site_id, svc, actor)
        bridge_health = _load_cached_bridge_health(svc.data_dir, site_id)
        return render_template(
            "wp_changes.html",
            **_dashboard_context(
                site=site,
                changes=changes,
                fe_plans=fe_plans,
                filter_category=category or "",
                filter_status=status or "",
                filter_batch_id=batch_id or "",
                batch_preview=batch_preview,
                posture=posture,
                bridge_health=bridge_health,
                can_restore=is_oscar_admin,
                can_operate=can_drill(),
                can_admin=file_admin() and bool(g.actor.get("is_oscar")),
            ),
        )

    @app.route("/wp/<site_id>/changes/batch/<batch_id>/rollback", methods=["POST"])
    @_auth_required
    def wp_changes_batch_rollback(site_id: str, batch_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")) or not BATCH_ID_RE.fullmatch(str(batch_id or "")):
            return Response("Okänd batch", 404)
        if not (can_restore() and g.actor.get("is_oscar")):
            return Response("Saknar behörighet", 403)
        confirm_batch_id = (request.form.get("confirm_batch_id") or batch_id).strip()
        from ecom_ops.wp_ops.rollback_service import ChangeRollbackService

        rsvc = ChangeRollbackService(data_dir=wp_service().data_dir)
        fn = getattr(rsvc, "execute_batch_rollback", None)
        if not callable(fn):
            return _redirect_changes(
                site_id,
                ok=False,
                message="Batch-återställning är inte tillgänglig i denna version.",
                batch_id=batch_id,
            )
        res = fn(
            site_id,
            batch_id,
            actor=g.actor["name"],
            confirm_batch_id=confirm_batch_id,
        )
        if isinstance(res, dict):
            if res.get("error_class") == "AccessDenied":
                return Response("Saknar behörighet", 403)
            ok = bool(res.get("ok"))
            rolled = res.get("rolled_back_count")
            msg = str(
                res.get("message")
                or (
                    f"Batch {batch_id} återställd ({rolled} ändringar)."
                    if ok and rolled is not None
                    else (f"Batch {batch_id} återställd." if ok else "Batch-återställning misslyckades.")
                )
            )
            return _redirect_changes(site_id, ok=ok, message=msg, batch_id=batch_id)
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_changes(
            site_id,
            ok=bool(getattr(res, "ok", False)),
            message=str(getattr(res, "message", "") or "Batch-återställning klar."),
            batch_id=batch_id,
        )

    @app.route("/wp/<site_id>/changes/<change_id>")
    @_auth_required
    def wp_change_detail(site_id: str, change_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")) or not CHANGE_ID_RE.fullmatch(str(change_id or "")):
            return Response("Okänd ändring", 404)
        svc = wp_service()
        actor = g.actor["name"]
        shown = svc.show_site(site_id, actor=actor)
        blocked = file_block(site_id, shown)
        if blocked is not None:
            return blocked
        site = (shown.data or {}).get("site")
        preview = svc.preview_change_rollback(site_id, change_id, actor=actor)
        if not preview.get("ok"):
            err = preview.get("error_class")
            if err == "AccessDenied":
                return Response("Saknar behörighet", 403)
            return Response("Okänd ändring", 404)
        change = dict(preview["change"])
        change["created_at_fmt"] = _fmt_dt(change.get("created_at"))
        return render_template(
            "wp_change_detail.html",
            **_dashboard_context(
                site=site,
                change=change,
                preview=preview,
                can_restore=bool(can_restore() and g.actor.get("is_oscar")),
                can_see_diff=bool(preview.get("can_see_diff")),
            ),
        )

    @app.route("/wp/<site_id>/changes/<change_id>/verify", methods=["POST"])
    @_auth_required
    def wp_change_verify(site_id: str, change_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")) or not CHANGE_ID_RE.fullmatch(str(change_id or "")):
            return Response("Okänd ändring", 404)
        if not can_read_wp():
            return Response("Saknar behörighet", 403)
        from ecom_ops.wp_ops.change_ledger import ChangeLedger

        res = ChangeLedger(data_dir=wp_service().data_dir).verify_snapshot(site_id, change_id)
        if res.get("verified"):
            return _redirect_change_detail(site_id, change_id, ok=True, message="Snapshot SHA-256 verifierat.")
        return _redirect_change_detail(
            site_id,
            change_id,
            ok=False,
            message=f"Snapshot-verifiering misslyckades ({res.get('error_class') or 'fel'}).",
        )

    @app.route("/wp/<site_id>/changes/<change_id>/rollback", methods=["POST"])
    @_auth_required
    def wp_change_rollback(site_id: str, change_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")) or not CHANGE_ID_RE.fullmatch(str(change_id or "")):
            return Response("Okänd ändring", 404)
        if not (can_restore() and g.actor.get("is_oscar")):
            return Response("Saknar behörighet", 403)
        expected_before = (request.form.get("expected_before_sha256") or "").strip()
        dry_run = (request.form.get("dry_run") or "") in {"1", "true", "on"}
        res = wp_service().execute_change_rollback(
            site_id,
            change_id,
            actor=g.actor["name"],
            expected_before_sha256=expected_before,
            dry_run=dry_run,
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_change_detail(site_id, change_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/bridge/health", methods=["POST"])
    @_auth_required
    def wp_bridge_health(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not (can_drill() or file_admin()):
            return Response("Saknar behörighet", 403)
        svc = wp_service()
        res = svc.bridge_health(site_id, actor=g.actor["name"])
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        if res.ok:
            health = (res.data or {}).get("health") or {}
            _save_cached_bridge_health(svc.data_dir, site_id, health)
        return _redirect_changes(site_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/bridge/cache-flush", methods=["POST"])
    @_auth_required
    def wp_bridge_cache_flush(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not (file_admin() and g.actor.get("is_oscar")):
            return Response("Saknar behörighet", 403)
        res = wp_service().bridge_cache_flush(site_id, actor=g.actor["name"])
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_changes(site_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/bridge/maintenance", methods=["POST"])
    @_auth_required
    def wp_bridge_maintenance(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not (file_admin() and g.actor.get("is_oscar")):
            return Response("Saknar behörighet", 403)
        enabled = (request.form.get("enabled") or "") in {"1", "true", "on"}
        try:
            duration = int(request.form.get("duration_seconds") or 300)
        except ValueError:
            duration = 300
        res = wp_service().bridge_maintenance(
            site_id,
            enabled=enabled,
            duration_seconds=duration,
            actor=g.actor["name"],
        )
        if is_denied(res):
            return Response("Saknar behörighet", 403)
        return _redirect_changes(site_id, ok=res.ok, message=res.message)

    @app.route("/wp/<site_id>/bridge/download-zip")
    @_auth_required
    def wp_bridge_download_zip(site_id: str):
        if not SITE_ID_RE.fullmatch(str(site_id or "")):
            return Response("Okänd webbplats", 404)
        if not (file_admin() and g.actor.get("is_oscar")):
            return Response("Saknar behörighet", 403)
        builder_path = _repo_bin_dir() / "build-wp-bridge-plugin.py"
        if not builder_path.is_file():
            return Response("Pluginpaket saknas i denna installation.", 503)
        import importlib.util

        spec = importlib.util.spec_from_file_location("build_wp_bridge_plugin", builder_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        try:
            raw, _meta = mod.build_plugin_zip_bytes()
        except Exception:
            return Response("Kunde inte bygga pluginpaket.", 503)
        return Response(
            raw,
            mimetype="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="azom-ops-bridge.zip"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


__all__ = ["register_changes_routes"]
