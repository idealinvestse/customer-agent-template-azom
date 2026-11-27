"""Safe, hash-pinned per-change rollback service backed by ``ChangeLedger``.

Preserves existing fail-closed ``BackupService.restore()`` (``RestoreNotInMvp``)
and ``FileChangeOps.rollback()`` invariants while providing verified per-change
rollback for dashboard/CLI recovery once ``AZOM_WP_RESTORE_ENABLED=1`` (in live)
or in mock mode.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.change_ledger import ChangeLedger, valid_batch_id, valid_change_id
from ecom_ops.wp_ops.config import data_dir_path, is_mock_mode
from ecom_ops.wp_ops.file_paths import (
    PathRejected,
    TransportError,
    resolve_file,
    resolve_mu_root_file,
)
from ecom_ops.wp_ops.file_transport import LocalTree, transport_is_remote
from ecom_ops.wp_ops.locks import hold_site
from ecom_ops.wp_ops.models import Site, WpOpsResult, fail_result, ok_result
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.ssh_settings import load_deploy_overlay, stored_deploy_key_file

logger = logging.getLogger(__name__)

_FILE_CATEGORIES = frozenset({"file_edit", "workspace_deploy", "mu_plugin_deploy"})
_PLUGIN_MUTATE_CATEGORIES = frozenset(
    {"plugin_update", "plugin_autoupdate", "plugin_quarantine", "plugin_rollback"}
)
_SEO_CATEGORIES = frozenset({"seo_meta", "media_alt"})
_SEO_META_ROLLBACK_FIELDS = (
    "meta_title",
    "meta_description",
    "focus_keyphrase",
    "canonical",
    "robots",
    "og_title",
    "og_description",
    "schema_type",
    "slug",
    "title",
    "excerpt",
)
_SEO_PLUGIN_ONLY_FIELDS = frozenset(
    {
        "meta_title",
        "meta_description",
        "focus_keyphrase",
        "canonical",
        "robots",
        "og_title",
        "og_description",
        "schema_type",
    }
)
_SEO_WOO_REST_FIELDS = ("name", "slug", "description", "short_description")
_SEO_WP_REST_FIELDS = ("title", "content", "excerpt", "slug")
_MEDIA_ALT_ROLLBACK_FIELDS = ("alt_text", "title", "caption", "description")


def _resolve_rollback_abs(files_root: str, kind: str, slug: str, rel: str) -> str:
    if kind == "mu_plugin" and slug in {"", "_root", "mu-plugins"}:
        return resolve_mu_root_file(files_root, rel)
    return resolve_file(files_root, kind, slug, rel)


def _best_effort_bridge_cache_flush(site: Site | None, bridge_client: Any = None) -> None:
    try:
        from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site, bridge_enabled

        bc = bridge_client or (
            bridge_client_for_site(site) if (site is not None and bridge_enabled()) else None
        )
        if bc is not None:
            bc.flush_cache()
    except Exception:
        pass


def _deploy_kill_active() -> bool:
    return env_flag("AZOM_WP_DEPLOY_KILL")


def _plugin_mutate_kill_active() -> bool:
    return env_flag("AZOM_WP_PLUGIN_MUTATE_KILL")


def _seo_kill_active() -> bool:
    return env_flag("AZOM_SEO_KILL")


def _restore_live_enabled() -> bool:
    return env_flag("AZOM_WP_RESTORE_ENABLED") and not _deploy_kill_active()


class ChangeRollbackService:
    """Preview and execute verified per-change rollbacks from ``ChangeLedger``."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        registry_path: Path | None = None,
    ) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.registry_path = registry_path
        self.ledger = ChangeLedger(data_dir=self.data_dir)

    def _resolve_site(self, site_id: str) -> Site | None:
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
            return site_by_id(sites, site_id)
        except Exception:
            return None

    def _audit(
        self,
        actor: str,
        *,
        site_id: str,
        change_id: str,
        success: bool,
        details: dict[str, Any],
    ) -> WpOpsResult | None:
        try:
            log_action(
                actor=actor,
                action="wp_ops_change_rollback",
                target="wp_changes",
                target_id=f"{site_id}:{change_id}",
                success=success,
                details=details,
            )
            return None
        except Exception:
            return fail_result(
                "AuditUnavailable",
                "Auditloggen kunde inte skrivas; återställning avbröts.",
                executed=False,
            )

    def preview_rollback(
        self,
        site_id: str,
        change_id: str,
        *,
        actor: str,
        woo_client: Any = None,
        wp_client: Any = None,
        bridge_client: Any = None,
    ) -> dict[str, Any]:
        """Return snapshot verification, reverse diff, drift check, and rollback readiness."""
        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, Permission.WP_OPS_READ)
        except AccessDenied as exc:
            return {"ok": False, "error_class": "AccessDenied", "message": str(exc)}

        if not valid_change_id(change_id):
            return {"ok": False, "error_class": "UnknownChange", "message": "Ogiltigt ändrings-id."}

        record = self.ledger.get_change(site_id, change_id)
        if record is None:
            return {"ok": False, "error_class": "UnknownChange", "message": "Ändringen hittades inte."}

        verify = self.ledger.verify_snapshot(site_id, change_id)
        can_see_diff = actor_obj.has(Permission.WP_OPS_ADMIN) and actor_obj.has(Permission.CODE_EDIT)
        diff_info = (
            self.ledger.read_snapshot_diff(site_id, change_id)
            if can_see_diff
            else {"ok": True, "kind": "redacted", "diff": "", "fields": []}
        )

        drift_detected = False
        drift_note = ""
        block_reason = ""
        seo_kill = record["category"] in _SEO_CATEGORIES and _seo_kill_active()
        plugin_kill = record["category"] in _PLUGIN_MUTATE_CATEGORIES and _plugin_mutate_kill_active()
        rec_meta = record.get("metadata") or {}
        non_reversible_plugin_rb = (
            record["category"] == "plugin_rollback"
            and rec_meta.get("recovery_action") != "deactivate"
        )
        if not verify.get("verified"):
            block_reason = str(verify.get("error_class") or "SnapshotCorrupted")
        elif record.get("status") == "rolled_back" or record.get("rolled_back_by"):
            block_reason = "AlreadyRolledBack"
        elif _deploy_kill_active():
            block_reason = "DeployKillSwitchActive"
        elif plugin_kill:
            block_reason = "PluginMutateKillSwitchActive"
        elif seo_kill:
            block_reason = "SeoKillSwitchActive"
        elif not is_mock_mode() and not _restore_live_enabled():
            block_reason = "RestoreGateDisabled"
        elif record["category"] in {"cache_flush", "maintenance"} or non_reversible_plugin_rb:
            block_reason = "NonReversibleOperation"
        elif record["category"] == "page_draft":
            block_reason = "DraftManualTrash"

        # Best-effort live/mock drift check (compares current state to recorded after_data).
        if verify.get("verified") and can_see_diff:
            try:
                snap = self.ledger.snapshots.read_snapshot(record["site_id"], record["change_id"])
                drift_detected, drift_note = self._detect_drift(
                    record,
                    snap,
                    woo_client=woo_client,
                    wp_client=wp_client,
                    bridge_client=bridge_client,
                )
            except Exception:
                drift_note = "Kunde inte läsa nuvarande läge för driftkontroll."

        can_rollback = bool(
            verify.get("verified")
            and not _deploy_kill_active()
            and not plugin_kill
            and not seo_kill
            and not non_reversible_plugin_rb
            and (is_mock_mode() or _restore_live_enabled())
            and record.get("status") != "rolled_back"
            and not record.get("rolled_back_by")
            and record["category"] not in {"cache_flush", "maintenance", "page_draft"}
        )
        return {
            "ok": True,
            "change": record,
            "verified": bool(verify.get("verified")),
            "verify": verify,
            "diff": diff_info,
            "drift_detected": drift_detected,
            "drift_note": drift_note,
            "can_rollback": can_rollback,
            "block_reason": block_reason,
            "can_see_diff": can_see_diff,
        }

    def execute_rollback(
        self,
        site_id: str,
        change_id: str,
        *,
        actor: str,
        expected_before_sha256: str,
        dry_run: bool = False,
        woo_client: Any = None,
        wp_client: Any = None,
        bridge_client: Any = None,
        deploy_transport: Any = None,
    ) -> WpOpsResult:
        """Execute a verified, hash-pinned rollback and record a linked audit/ledger entry."""
        if _deploy_kill_active():
            return fail_result(
                "DeployKillSwitchActive",
                "Nödstopp (AZOM_WP_DEPLOY_KILL) är aktivt.",
                executed=False,
            )
        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, Permission.WP_OPS_RESTORE)
            require_permission(actor_obj, Permission.WP_OPS_ADMIN)
        except AccessDenied as exc:
            return fail_result("AccessDenied", str(exc), executed=False)

        if not valid_change_id(change_id):
            return fail_result("UnknownChange", "Ogiltigt ändrings-id.", executed=False)

        initial_record = self.ledger.get_change(site_id, change_id)
        if initial_record is None:
            return fail_result("UnknownChange", "Ändringen hittades inte.", executed=False)

        resolved_site_id = str(initial_record["site_id"])
        with hold_site(resolved_site_id):
            return self._execute_rollback_locked(
                resolved_site_id,
                change_id,
                actor_obj=actor_obj,
                expected_before_sha256=expected_before_sha256,
                dry_run=dry_run,
                woo_client=woo_client,
                wp_client=wp_client,
                bridge_client=bridge_client,
                deploy_transport=deploy_transport,
            )

    def _execute_rollback_locked(
        self,
        resolved_site_id: str,
        change_id: str,
        *,
        actor_obj: Any,
        expected_before_sha256: str,
        dry_run: bool = False,
        woo_client: Any = None,
        wp_client: Any = None,
        bridge_client: Any = None,
        deploy_transport: Any = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> WpOpsResult:
        record = self.ledger.get_change(resolved_site_id, change_id)
        if record is None:
            return fail_result("UnknownChange", "Ändringen hittades inte.", executed=False)

        if record["category"] in _PLUGIN_MUTATE_CATEGORIES and _plugin_mutate_kill_active():
            return fail_result(
                "PluginMutateKillSwitchActive",
                "Nödstopp för plugin-ändringar (AZOM_WP_PLUGIN_MUTATE_KILL) är aktivt.",
                executed=False,
            )

        if record["category"] in _SEO_CATEGORIES and _seo_kill_active():
            return fail_result(
                "SeoKillSwitchActive",
                "Nödstopp för SEO-ändringar (AZOM_SEO_KILL) är aktivt.",
                executed=False,
            )

        if record["category"] in _FILE_CATEGORIES:
            try:
                require_permission(actor_obj, Permission.CODE_EDIT)
            except AccessDenied as exc:
                return fail_result("AccessDenied", str(exc), executed=False)

        if record.get("status") == "rolled_back" or record.get("rolled_back_by"):
            return fail_result(
                "AlreadyRolledBack",
                f"Ändring {change_id} är redan återställd.",
                executed=False,
            )

        if not is_mock_mode() and not _restore_live_enabled():
            self._audit(
                actor_obj.name,
                site_id=record["site_id"],
                change_id=change_id,
                success=False,
                details={"error_class": "RestoreGateDisabled", "executed": False},
            )
            return fail_result(
                "RestoreGateDisabled",
                "Live-återställning är avstängd (sätt AZOM_WP_RESTORE_ENABLED=1 i processmiljön).",
                executed=False,
                live_calls=False,
            )

        if str(expected_before_sha256 or "").strip() != str(record["before_sha256"]):
            return fail_result(
                "DigestMismatch",
                "Låst before_sha256 matchar inte ändringens snapshot.",
                executed=False,
            )

        verify = self.ledger.verify_snapshot(record["site_id"], change_id)
        if not verify.get("verified"):
            return fail_result(
                str(verify.get("error_class") or "SnapshotCorrupted"),
                "Snapshotets integritet kunde inte verifieras.",
                executed=False,
            )

        snap = self.ledger.snapshots.read_snapshot(record["site_id"], change_id)
        if dry_run:
            return ok_result(
                "Torrkörning: snapshot verifierat och klart för återställning.",
                change_id=change_id,
                site_id=record["site_id"],
                category=record["category"],
                dry_run=True,
                executed=False,
                before_sha256=record["before_sha256"],
            )

        site = self._resolve_site(record["site_id"])
        try:
            outcome = self._apply_reverse(
                record,
                snap,
                site=site,
                woo_client=woo_client,
                wp_client=wp_client,
                bridge_client=bridge_client,
                deploy_transport=deploy_transport,
            )
        except Exception as exc:
            self._audit(
                actor_obj.name,
                site_id=record["site_id"],
                change_id=change_id,
                success=False,
                details={
                    "error_class": "RollbackFailed",
                    "category": record["category"],
                    "reason": str(exc)[:160],
                },
            )
            return fail_result(
                "RollbackFailed",
                f"Återställning misslyckades: {str(exc)[:160]}",
                executed=False,
            )

        rb_meta = {"rollback_of": change_id, **record.get("metadata", {})}
        if extra_metadata:
            rb_meta.update(extra_metadata)

        if snap["kind"] == "files":
            rollback_row = self.ledger.record_change(
                site_id=record["site_id"],
                environment=record["environment"],
                category=record["category"],
                transport=str(outcome.get("transport") or record["transport"]),
                actor=actor_obj.name,
                summary=f"Återställning av {change_id}: {record['summary']}"[:220],
                target_ref=record["target_ref"],
                file_entries=outcome["reverse_entries"],
                before_files=snap["after_files"],
                after_files=snap["before_files"],
                status="applied",
                parent_change_id=change_id,
                metadata=rb_meta,
            )
        else:
            rollback_row = self.ledger.record_change(
                site_id=record["site_id"],
                environment=record["environment"],
                category=record["category"],
                transport=str(outcome.get("transport") or record["transport"]),
                actor=actor_obj.name,
                summary=f"Återställning av {change_id}: {record['summary']}"[:220],
                target_ref=record["target_ref"],
                before_data=snap["after"],
                after_data=snap["before"],
                status="applied",
                parent_change_id=change_id,
                metadata=rb_meta,
            )
        self.ledger.mark_rolled_back(
            record["site_id"],
            change_id,
            rollback_change_id=rollback_row["change_id"],
        )
        audit_err = self._audit(
            actor_obj.name,
            site_id=record["site_id"],
            change_id=change_id,
            success=True,
            details={
                "rollback_change_id": rollback_row["change_id"],
                "category": record["category"],
                "transport": outcome.get("transport"),
                "live_calls": bool(outcome.get("live_calls")),
                "executed": True,
            },
        )
        if audit_err is not None:
            logger.warning("Post-rollback audit write failed for %s", change_id)
        return ok_result(
            f"Ändring {change_id} återställd (ny post {rollback_row['change_id']}).",
            change_id=change_id,
            rollback_change_id=rollback_row["change_id"],
            site_id=record["site_id"],
            executed=True,
            live_calls=bool(outcome.get("live_calls")),
            transport=outcome.get("transport"),
            audit_warning=audit_err is not None,
        )

    def preview_batch_rollback(
        self,
        site_id: str,
        batch_id: str,
        *,
        actor: str = "jonatan",
        woo_client: Any = None,
        wp_client: Any = None,
        bridge_client: Any = None,
    ) -> dict[str, Any]:
        """Preview transactional batch rollback for ``batch_id`` in reverse chronological order."""
        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, Permission.WP_OPS_READ)
        except AccessDenied as exc:
            return {"ok": False, "error_class": "AccessDenied", "message": str(exc)}

        bid = str(batch_id or "").strip()
        if not bid or not valid_batch_id(bid):
            return {
                "ok": False,
                "error_class": "UnknownBatch",
                "message": "Ogiltigt batch_id.",
                "site_id": site_id,
                "batch_id": bid,
            }

        rows = self.ledger.list_batch_changes(bid, site_id=site_id)
        if not rows:
            return {
                "ok": False,
                "error_class": "UnknownBatch",
                "message": f"Inga ändringar hittades för batch {bid}.",
                "site_id": site_id,
                "batch_id": bid,
                "total": 0,
                "items": [],
                "can_rollback": False,
            }

        items: list[dict[str, Any]] = []
        rollbackable_count = 0
        already_rolled_back_count = 0
        blocked_items: list[dict[str, Any]] = []
        drift_count = 0

        for row in rows:
            prev = self.preview_rollback(
                row["site_id"],
                row["change_id"],
                actor=actor,
                woo_client=woo_client,
                wp_client=wp_client,
                bridge_client=bridge_client,
            )
            items.append(prev)
            if prev.get("drift_detected"):
                drift_count += 1
            if prev.get("can_rollback"):
                rollbackable_count += 1
            elif prev.get("block_reason") == "AlreadyRolledBack":
                already_rolled_back_count += 1
            else:
                blocked_items.append(prev)

        block_reason = ""
        if _deploy_kill_active():
            block_reason = "DeployKillSwitchActive"
        elif blocked_items:
            block_reason = str(
                blocked_items[0].get("block_reason")
                or blocked_items[0].get("error_class")
                or "BatchItemBlocked"
            )
        elif rollbackable_count == 0 and already_rolled_back_count > 0:
            block_reason = "AlreadyRolledBack"

        can_rollback = bool(rollbackable_count > 0 and not blocked_items and not _deploy_kill_active())
        return {
            "ok": True,
            "site_id": str(rows[0]["site_id"]),
            "batch_id": bid,
            "total": len(rows),
            "rollbackable_count": rollbackable_count,
            "already_rolled_back_count": already_rolled_back_count,
            "blocked_count": len(blocked_items),
            "drift_count": drift_count,
            "can_rollback": can_rollback,
            "block_reason": block_reason,
            "change_ids": [r["change_id"] for r in rows],
            "items": items,
        }

    def execute_batch_rollback(
        self,
        site_id: str,
        batch_id: str,
        *,
        actor: str = "oscar",
        confirm_batch_id: str = "",
        dry_run: bool = False,
        woo_client: Any = None,
        wp_client: Any = None,
        bridge_client: Any = None,
        deploy_transport: Any = None,
    ) -> dict[str, Any]:
        """Roll back all changes in ``batch_id`` in reverse chronological order under site lock."""
        bid = str(batch_id or "").strip()
        if _deploy_kill_active():
            return {
                "ok": False,
                "error_class": "DeployKillSwitchActive",
                "message": "Nödstopp (AZOM_WP_DEPLOY_KILL) är aktivt.",
                "site_id": site_id,
                "batch_id": bid,
                "executed": False,
                "rolled_back": [],
                "failed": [],
            }

        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, Permission.WP_OPS_RESTORE)
            require_permission(actor_obj, Permission.WP_OPS_ADMIN)
        except AccessDenied as exc:
            return {
                "ok": False,
                "error_class": "AccessDenied",
                "message": str(exc),
                "site_id": site_id,
                "batch_id": bid,
                "executed": False,
                "rolled_back": [],
                "failed": [],
            }

        if not bid or not valid_batch_id(bid):
            return {
                "ok": False,
                "error_class": "UnknownBatch",
                "message": "Ogiltigt batch_id.",
                "site_id": site_id,
                "batch_id": bid,
                "executed": False,
                "rolled_back": [],
                "failed": [],
            }

        if str(confirm_batch_id or "").strip() != bid:
            return {
                "ok": False,
                "error_class": "BatchConfirmationMismatch",
                "message": "confirm_batch_id måste matcha batch_id exakt.",
                "site_id": site_id,
                "batch_id": bid,
                "executed": False,
                "rolled_back": [],
                "failed": [],
            }

        if not is_mock_mode() and not _restore_live_enabled():
            return {
                "ok": False,
                "error_class": "RestoreGateDisabled",
                "message": "Live-återställning är avstängd (sätt AZOM_WP_RESTORE_ENABLED=1 i processmiljön).",
                "site_id": site_id,
                "batch_id": bid,
                "executed": False,
                "live_calls": False,
                "rolled_back": [],
                "failed": [],
            }

        initial_rows = self.ledger.list_batch_changes(bid, site_id=site_id)
        if not initial_rows:
            return {
                "ok": False,
                "error_class": "UnknownBatch",
                "message": f"Inga ändringar hittades för batch {bid}.",
                "site_id": site_id,
                "batch_id": bid,
                "executed": False,
                "rolled_back": [],
                "failed": [],
            }

        resolved_site_id = str(initial_rows[0]["site_id"])
        with hold_site(resolved_site_id):
            preview = self.preview_batch_rollback(
                resolved_site_id,
                bid,
                actor=actor,
                woo_client=woo_client,
                wp_client=wp_client,
                bridge_client=bridge_client,
            )
            if not preview.get("ok"):
                return {
                    "ok": False,
                    "error_class": str(preview.get("error_class") or "UnknownBatch"),
                    "message": str(preview.get("message") or "Kunde inte förhandsgranska batch-återställning."),
                    "site_id": resolved_site_id,
                    "batch_id": bid,
                    "executed": False,
                    "rolled_back": [],
                    "failed": [],
                }

            for item_prev in list(preview.get("items") or []):
                chg = item_prev.get("change") or {}
                if chg.get("category") in _FILE_CATEGORIES:
                    try:
                        require_permission(actor_obj, Permission.CODE_EDIT)
                    except AccessDenied as exc:
                        return {
                            "ok": False,
                            "error_class": "AccessDenied",
                            "message": str(exc),
                            "site_id": resolved_site_id,
                            "batch_id": bid,
                            "executed": False,
                            "rolled_back": [],
                            "failed": [],
                        }

            if not preview.get("can_rollback"):
                reason = str(preview.get("block_reason") or "BatchNotRollbackable")
                return {
                    "ok": False,
                    "error_class": reason,
                    "message": f"Batch {bid} kan inte återställas ({reason}).",
                    "site_id": resolved_site_id,
                    "batch_id": bid,
                    "executed": False,
                    "preview": preview,
                    "rolled_back": [],
                    "failed": [],
                }

            rollbackable_records = [
                item_prev["change"]
                for item_prev in list(preview.get("items") or [])
                if item_prev.get("can_rollback") and isinstance(item_prev.get("change"), dict)
            ]
            skipped_count = int(preview.get("already_rolled_back_count") or 0)

            if dry_run:
                return {
                    "ok": True,
                    "dry_run": True,
                    "site_id": resolved_site_id,
                    "batch_id": bid,
                    "executed": False,
                    "planned_change_ids": [r["change_id"] for r in rollbackable_records],
                    "skipped_count": skipped_count,
                    "rolled_back": [],
                    "failed": [],
                    "message": f"Torrkörning: {len(rollbackable_records)} ändring(ar) i batch {bid} redo för återställning.",
                }

            rolled_back: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            for rec in rollbackable_records:
                res = self._execute_rollback_locked(
                    resolved_site_id,
                    rec["change_id"],
                    actor_obj=actor_obj,
                    expected_before_sha256=rec["before_sha256"],
                    dry_run=False,
                    woo_client=woo_client,
                    wp_client=wp_client,
                    bridge_client=bridge_client,
                    deploy_transport=deploy_transport,
                    extra_metadata={"rollback_of_batch_id": bid},
                )
                if res.ok:
                    rolled_back.append(
                        {
                            "change_id": rec["change_id"],
                            "rollback_change_id": res.data.get("rollback_change_id"),
                            "category": rec["category"],
                            "target_ref": rec["target_ref"],
                        }
                    )
                else:
                    failed.append(
                        {
                            "change_id": rec["change_id"],
                            "error_class": res.data.get("error_class"),
                            "message": res.message,
                        }
                    )
                    break

        try:
            log_action(
                actor=actor_obj.name,
                action="wp_ops_batch_rollback",
                target="wp_changes",
                target_id=f"{resolved_site_id}:{bid}",
                success=len(failed) == 0,
                details={
                    "batch_id": bid,
                    "rolled_back_count": len(rolled_back),
                    "failed_count": len(failed),
                    "skipped_count": skipped_count,
                    "rollback_change_ids": [r["rollback_change_id"] for r in rolled_back],
                },
            )
        except Exception:
            logger.warning("Post-batch-rollback audit write failed for %s", bid)

        ok_all = len(failed) == 0
        return {
            "ok": ok_all,
            "site_id": resolved_site_id,
            "batch_id": bid,
            "executed": len(rolled_back) > 0,
            "rolled_back_count": len(rolled_back),
            "failed_count": len(failed),
            "skipped_count": skipped_count,
            "rolled_back": rolled_back,
            "rollback_change_ids": [r["rollback_change_id"] for r in rolled_back if r.get("rollback_change_id")],
            "failed": failed,
            "error_class": None if ok_all else "BatchRollbackPartialFailure",
            "message": (
                f"Batch {bid} återställd ({len(rolled_back)} ändring(ar) i omvänd ordning)."
                if ok_all
                else f"Batch-återställning för {bid} avbröts efter {len(rolled_back)} återställda och {len(failed)} fel."
            ),
        }

    def _detect_drift(
        self,
        record: dict[str, Any],
        snap: dict[str, Any],
        *,
        woo_client: Any = None,
        wp_client: Any = None,
        bridge_client: Any = None,
    ) -> tuple[bool, str]:
        cat = record["category"]
        meta = record.get("metadata") or {}
        domain = meta.get("domain") or None
        site = self._resolve_site(record["site_id"])
        if snap["kind"] == "entity":
            after = snap.get("after") or {}
            if cat == "product":
                client = woo_client
                if client is None and is_mock_mode():
                    from ecom_ops.integrations.woocommerce import client_from_env

                    client = client_from_env(use_mock=True, domain=domain)
                if client is not None:
                    pid = str(meta.get("product_id") or record["target_ref"].split(":")[-1])
                    curr = client.get_product(pid)
                    for k, v in after.items():
                        if k == "id":
                            continue
                        if str(curr.get(k) or "") != str(v or ""):
                            return True, f"Produktfält '{k}' har ändrats sedan {record['change_id']}."
            elif cat in {"page", "post"}:
                client = wp_client or (
                    self._wp_client(site, domain=domain, use_mock=True) if is_mock_mode() else None
                )
                if client is not None:
                    iid = str(meta.get("item_id") or record["target_ref"].split(":")[-1])
                    curr = (
                        client.get_page(iid, context="edit")
                        if cat == "page"
                        else client.get_post(iid, context="edit")
                    )
                    if after.get("title") is not None and curr.title != after.get("title"):
                        return True, "Titel har ändrats sedan snapshot togs."
                    if after.get("status") is not None and curr.status != after.get("status"):
                        return True, "Status har ändrats sedan snapshot togs."
            elif cat == "seo_meta":
                from ecom_ops.integrations.wp.bridge_client import (
                    bridge_client_for_site,
                    bridge_enabled,
                )

                bc = bridge_client or (
                    bridge_client_for_site(site)
                    if (site is not None and (is_mock_mode() or bridge_enabled()))
                    else None
                )
                if bc is None:
                    if not is_mock_mode():
                        return False, "Driftkontroll hoppades över (live Bridge ej aktiv)."
                    return False, ""
                ref_parts = str(record.get("target_ref") or "").split(":")
                obj_type = str(
                    meta.get("object_type")
                    or after.get("object_type")
                    or (ref_parts[0] if len(ref_parts) >= 2 else "all")
                )
                try:
                    obj_id = int(str(meta.get("object_id") or after.get("object_id") or ref_parts[-1]))
                except ValueError:
                    obj_id = 0
                if obj_id > 0:
                    try:
                        listing = bc.seo_entities(object_type=obj_type, object_id=obj_id, limit=1, page=1)
                    except TypeError:
                        listing = bc.seo_entities(object_type=obj_type, limit=100, page=1)
                    match = next(
                        (
                            row
                            for row in (listing.get("entities") or [])
                            if int(row.get("object_id") or 0) == obj_id
                            and (obj_type == "all" or str(row.get("object_type") or "") == obj_type)
                        ),
                        None,
                    )
                    if match is not None:
                        for k in _SEO_META_ROLLBACK_FIELDS:
                            if k in after and str(match.get(k) or "") != str(after.get(k) or ""):
                                return True, f"SEO-fält '{k}' har ändrats sedan {record['change_id']}."
            elif cat == "media_alt":
                ref_parts = str(record.get("target_ref") or "").split(":")
                try:
                    mid = int(str(meta.get("media_id") or after.get("media_id") or ref_parts[-1]))
                except ValueError:
                    mid = 0
                if mid > 0:
                    use_bridge = bridge_client is not None or record.get("transport") == "bridge"
                    if use_bridge:
                        from ecom_ops.integrations.wp.bridge_client import (
                            bridge_client_for_site,
                            bridge_enabled,
                        )

                        bc = bridge_client or (
                            bridge_client_for_site(site)
                            if (site is not None and (is_mock_mode() or bridge_enabled()))
                            else None
                        )
                        if bc is None:
                            if not is_mock_mode():
                                return False, "Driftkontroll hoppades över (live Bridge ej aktiv)."
                            return False, ""
                        try:
                            inv = bc.seo_media_inventory(media_id=mid, limit=1, page=1)
                        except TypeError:
                            inv = bc.seo_media_inventory(limit=100, page=1)
                        match = next(
                            (row for row in (inv.get("media") or []) if int(row.get("media_id") or 0) == mid),
                            None,
                        )
                        if match is not None:
                            for k in _MEDIA_ALT_ROLLBACK_FIELDS:
                                if k in after and str(match.get(k) or "") != str(after.get(k) or ""):
                                    return True, f"Mediafält '{k}' har ändrats sedan {record['change_id']}."
                    else:
                        client = wp_client or (
                            self._wp_client(site, domain=domain, use_mock=True) if is_mock_mode() else None
                        )
                        if client is None and not is_mock_mode():
                            return False, "Driftkontroll hoppades över i live-läge."
                        if client is not None and hasattr(client, "get_media"):
                            curr = client.get_media(mid)
                            for k in _MEDIA_ALT_ROLLBACK_FIELDS:
                                if k not in after:
                                    continue
                                raw_val = curr.get(k)
                                if isinstance(raw_val, dict):
                                    curr_str = str(raw_val.get("raw") or raw_val.get("rendered") or "")
                                else:
                                    curr_str = str(raw_val or "")
                                if curr_str != str(after.get(k) or ""):
                                    return True, f"Mediafält '{k}' har ändrats sedan {record['change_id']}."
            elif cat == "file_edit":
                pass
            if not is_mock_mode() and cat in {"product", "page", "post"} and woo_client is None and wp_client is None:
                return False, "Driftkontroll hoppades över i live-läge."
            return False, ""
        # Files drift check against LocalTree in mock mode or read-only Bridge snapshot in live mode
        if site is not None and is_mock_mode():
            from ecom_ops.integrations.wp_files_ssh import build_file_transport

            transport = build_file_transport(site, self.data_dir)
            if isinstance(transport, LocalTree):
                kind = str(meta.get("kind") or "theme")
                slug = str(meta.get("slug") or "")
                for entry in snap.get("entries") or []:
                    rel = str(entry.get("path") or "")
                    if not rel or (not slug and kind != "mu_plugin"):
                        continue
                    try:
                        abs_path = _resolve_rollback_abs(site.files_root, kind, slug, rel)
                        curr_bytes = transport.read_bytes(abs_path)
                    except (PathRejected, TransportError, ValueError):
                        curr_bytes = None
                    expected = snap["after_files"].get(rel)
                    if curr_bytes != expected:
                        return True, f"Filen {rel} har ändrats sedan {record['change_id']}."
        elif site is not None and not is_mock_mode():
            from ecom_ops.integrations.wp.bridge_client import (
                bridge_client_for_site,
                bridge_enabled,
            )

            bc = bridge_client or (bridge_client_for_site(site) if bridge_enabled() else None)
            if bc is None:
                return False, "Driftkontroll hoppades över i live-läge."
            import base64

            kind = str(meta.get("kind") or "theme")
            slug = str(meta.get("slug") or "")
            snap_kind = (
                "theme_file"
                if kind == "theme"
                else ("mu_plugin_file" if kind == "mu_plugin" else "plugin_file")
            )
            for entry in snap.get("entries") or []:
                rel = str(entry.get("path") or "")
                if not rel:
                    continue
                live_snap = bc.get_snapshot(kind=snap_kind, slug=slug, path=rel)
                curr_bytes = (
                    base64.b64decode(str(live_snap.get("content") or ""))
                    if live_snap.get("exists")
                    else None
                )
                expected = snap["after_files"].get(rel)
                if curr_bytes != expected:
                    return True, f"Filen {rel} har ändrats sedan {record['change_id']}."
        del domain
        return False, ""

    def _apply_reverse(
        self,
        record: dict[str, Any],
        snap: dict[str, Any],
        *,
        site: Site | None,
        woo_client: Any = None,
        wp_client: Any = None,
        bridge_client: Any = None,
        deploy_transport: Any = None,
    ) -> dict[str, Any]:
        cat = record["category"]
        meta = record.get("metadata") or {}
        domain = meta.get("domain") or None
        use_mock = is_mock_mode()

        if snap["kind"] == "entity":
            before = dict(snap.get("before") or {})
            if cat == "product":
                from ecom_ops.integrations.woocommerce import client_from_env

                woo = woo_client or client_from_env(use_mock=use_mock, domain=domain)
                pid = str(meta.get("product_id") or record["target_ref"].split(":")[-1])
                fields = {k: v for k, v in before.items() if v is not None and k != "id"}
                if not fields:
                    raise ValueError("MissingBeforeSnapshot")
                woo.update_product(pid, fields, allow_empty=True)
                return {"transport": "woo_rest", "live_calls": not use_mock}

            if cat == "order":
                from ecom_ops.integrations.woocommerce import client_from_env

                woo = woo_client or client_from_env(use_mock=use_mock, domain=domain)
                oid = str(meta.get("order_id") or record["target_ref"].split(":")[-1])
                prev_status = str(before.get("status") or "").strip()
                if not prev_status:
                    raise ValueError("Missing previous order status in snapshot")
                woo.update_order_status(oid, prev_status)
                return {"transport": "woo_rest", "live_calls": not use_mock}

            if cat in {"page", "post", "page_draft"}:
                wp = wp_client or self._wp_client(site, domain=domain, use_mock=use_mock)
                iid = str(meta.get("item_id") or record["target_ref"].split(":")[-1])
                if cat == "page_draft":
                    if str(before.get("exists")) == "False" or not before.get("id"):
                        raise ValueError("Draft-skapande kräver manuell papperskorg i wp-admin.")
                    iid = str(before["id"])
                title_v = before.get("title")
                content_v = before.get("content")
                status_v = before.get("status")
                if title_v is None and content_v is None and status_v is None:
                    raise ValueError("MissingBeforeSnapshot")
                if cat == "post":
                    wp.update_post(
                        iid,
                        title=title_v,
                        content=content_v,
                        status=status_v,
                        allow_empty=True,
                        max_content_len=131_072,
                    )
                else:
                    wp.update_page(
                        iid,
                        title=title_v,
                        content=content_v,
                        status=status_v,
                        allow_empty=True,
                        max_content_len=131_072,
                    )
                return {"transport": "wp_rest", "live_calls": not use_mock}

            if cat == "settings":
                wp = wp_client or self._wp_client(site, domain=domain, use_mock=use_mock)
                wp.update_site_identity(
                    title=before.get("title"),
                    description=before.get("description"),
                )
                return {"transport": "wp_rest", "live_calls": not use_mock}

            if cat in {"plugin_toggle", "plugin"}:
                wp = wp_client or self._wp_client(site, domain=domain, use_mock=use_mock)
                plugin_id = str(meta.get("plugin") or record["target_ref"].split(":")[-1])
                prev_status = str(before.get("status") or "").strip()
                if prev_status not in {"active", "inactive"}:
                    raise ValueError("Missing previous plugin status in snapshot")
                wp.set_plugin_status(plugin_id, prev_status)
                return {"transport": "wp_rest", "live_calls": not use_mock}

            if cat == "plugin_update":
                from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site

                bc = bridge_client or (bridge_client_for_site(site) if site is not None else None)
                if bc is None:
                    raise ValueError("Bridge krävs för återställning av plugin-uppdatering.")
                backup_id = str(meta.get("backup_id") or before.get("backup_id") or "").strip()
                plugin_file = str(meta.get("plugin_file") or before.get("plugin_file") or "").strip()
                if not backup_id or not plugin_file:
                    raise ValueError("Missing backup_id or plugin_file in snapshot")
                res = bc.rollback_plugin(
                    change_id=record["change_id"],
                    plugin_file=plugin_file,
                    backup_id=backup_id,
                    probe_health=True,
                )
                if not res.get("ok"):
                    raise ValueError("Bridge plugin rollback failed")
                return {"transport": "bridge", "live_calls": bool(getattr(bc, "is_live", not use_mock))}

            if cat == "plugin_autoupdate":
                from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site

                bc = bridge_client or (bridge_client_for_site(site) if site is not None else None)
                if bc is None:
                    raise ValueError("Bridge krävs för återställning av autouppdateringar.")
                before_list = {str(p) for p in list(before.get("auto_update_plugins") or []) if p}
                after_list = {str(p) for p in list((snap.get("after") or {}).get("auto_update_plugins") or []) if p}
                changes: list[dict[str, Any]] = []
                for pf in sorted(before_list - after_list):
                    changes.append({"plugin_file": pf, "enabled": True})
                for pf in sorted(after_list - before_list):
                    changes.append({"plugin_file": pf, "enabled": False})
                if changes:
                    bc.set_plugin_autoupdates(changes=changes)
                return {"transport": "bridge", "live_calls": bool(getattr(bc, "is_live", not use_mock))}

            if cat == "plugin_quarantine":
                if site is None:
                    raise ValueError("UnknownSite for plugin quarantine rollback")
                orig_action = str(meta.get("action") or before.get("action") or "quarantine")
                q_kind = str(meta.get("kind") or before.get("kind") or "plugin")
                q_target = str(meta.get("target") or before.get("target") or "")
                q_name = str(meta.get("quarantine_name") or (snap.get("after") or {}).get("quarantine_name") or "")
                if not q_target:
                    raise ValueError("Missing target in quarantine snapshot")
                if deploy_transport is not None:
                    if orig_action == "quarantine":
                        if q_kind == "mu_plugin":
                            deploy_transport.unquarantine_mu_plugin(q_target, quarantine_name=q_name)
                        else:
                            deploy_transport.unquarantine_plugin(q_target, quarantine_name=q_name)
                    else:
                        if q_kind == "mu_plugin":
                            deploy_transport.quarantine_mu_plugin(q_target)
                        else:
                            deploy_transport.quarantine_plugin(q_target)
                    return {"transport": "sftp_deploy", "live_calls": not use_mock}
                if use_mock:
                    from ecom_ops.wp_ops.plugin_service import PluginOpsService

                    svc = PluginOpsService(data_dir=self.data_dir, registry_path=self.registry_path)
                    if orig_action == "quarantine":
                        svc._mock_unquarantine(site, q_target, quarantine_name=q_name, kind=q_kind)
                    else:
                        svc._mock_quarantine(site, q_target, kind=q_kind)
                    return {"transport": "local_mock", "live_calls": False}
                from ecom_ops.integrations.wp.sftp_deploy_transport import (
                    SshDeployTransport,
                    deploy_ssh_enabled,
                )

                if not deploy_ssh_enabled():
                    raise ValueError("Deploy SFTP är inte aktiverad för karantän-rollback.")
                dt = SshDeployTransport(site, self.data_dir)
                if orig_action == "quarantine":
                    if q_kind == "mu_plugin":
                        dt.unquarantine_mu_plugin(q_target, quarantine_name=q_name)
                    else:
                        dt.unquarantine_plugin(q_target, quarantine_name=q_name)
                else:
                    if q_kind == "mu_plugin":
                        dt.quarantine_mu_plugin(q_target)
                    else:
                        dt.quarantine_plugin(q_target)
                return {"transport": "sftp_deploy", "live_calls": True}

            if cat == "plugin_rollback":
                if meta.get("recovery_action") == "deactivate" and "active" in before:
                    from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site

                    bc = bridge_client or (bridge_client_for_site(site) if site is not None else None)
                    if bc is None:
                        raise ValueError("Bridge krävs för att återaktivera plugin.")
                    plugin_file = str(meta.get("plugin_file") or before.get("plugin_file") or "")
                    res = bc.toggle_plugin(
                        plugin_file=plugin_file,
                        active=bool(before.get("active")),
                        probe_health=True,
                    )
                    if not isinstance(res, dict) or not res.get("ok"):
                        raise ValueError(
                            str((res or {}).get("error") or "Bridge plugin toggle rollback failed")
                        )
                    return {"transport": "bridge", "live_calls": bool(getattr(bc, "is_live", not use_mock))}
                raise ValueError("Plugin rollback entry cannot be rolled back automatically.")

            if cat == "seo_meta":
                ref_parts = str(record.get("target_ref") or "").split(":")
                obj_type = str(
                    meta.get("object_type")
                    or before.get("object_type")
                    or (ref_parts[0] if len(ref_parts) >= 2 else "product")
                ).strip().lower()
                obj_id = int(str(meta.get("object_id") or before.get("object_id") or ref_parts[-1]))
                after = dict(snap.get("after") or {})
                all_seo_keys = (
                    set(_SEO_META_ROLLBACK_FIELDS)
                    | set(_SEO_WOO_REST_FIELDS)
                    | set(_SEO_WP_REST_FIELDS)
                )
                if after:
                    changed: dict[str, Any] = {
                        k: before[k]
                        for k in all_seo_keys
                        if k in before and before[k] is not None and k in after and before[k] != after.get(k)
                    }
                    if not changed:
                        changed = {
                            k: before[k]
                            for k in all_seo_keys
                            if k in before and before[k] is not None and before[k] != after.get(k)
                        }
                else:
                    changed = {
                        k: before[k]
                        for k in all_seo_keys
                        if k in before and before[k] is not None
                    }
                # Never send an empty slug (PHP class-seo.php rejects empty slug with HTTP 400 on drafts).
                if "slug" in changed and not str(changed["slug"] or "").strip():
                    changed.pop("slug", None)
                if not changed:
                    raise ValueError("MissingBeforeSnapshot")

                seo_plugin_changed = {k: v for k, v in changed.items() if k in _SEO_PLUGIN_ONLY_FIELDS}
                bridge_fields = {k: v for k, v in changed.items() if k in _SEO_META_ROLLBACK_FIELDS}
                extra_content_fields = {k: v for k, v in changed.items() if k not in _SEO_META_ROLLBACK_FIELDS}

                from ecom_ops.integrations.wp.bridge_client import (
                    bridge_client_for_site,
                    bridge_enabled,
                )

                orig_transport = str(record.get("transport") or "").strip().lower()
                prefer_rest_only = (
                    bridge_client is None
                    and orig_transport in {"woo_rest", "wp_rest"}
                    and not seo_plugin_changed
                )
                bc = None
                if not prefer_rest_only:
                    bc = bridge_client or (
                        bridge_client_for_site(site)
                        if (site is not None and (use_mock or bridge_enabled()))
                        else None
                    )
                if seo_plugin_changed and bc is None:
                    raise ValueError(
                        "Bridge krävs för återställning av SEO-plugin-metadata (kan inte återställas via REST)."
                    )

                transports_used: list[str] = []
                live_used = False
                if bc is not None and bridge_fields:
                    res = bc.seo_update_meta(
                        change_id=f"rb-{record['change_id']}",
                        object_type=obj_type,
                        object_id=obj_id,
                        fields=bridge_fields,
                    )
                    if not res.get("ok"):
                        raise ValueError("Bridge SEO meta rollback failed")
                    transports_used.append("bridge")
                    live_used = live_used or bool(getattr(bc, "is_live", not use_mock))

                rest_needed = extra_content_fields if "bridge" in transports_used else changed
                if rest_needed:
                    if obj_type == "product":
                        woo_fields = {
                            k: rest_needed[k]
                            for k in _SEO_WOO_REST_FIELDS
                            if k in rest_needed and rest_needed[k] is not None
                        }
                        if "title" in rest_needed and "name" not in woo_fields:
                            woo_fields["name"] = rest_needed["title"]
                        if woo_fields:
                            from ecom_ops.integrations.woocommerce import client_from_env

                            woo = woo_client or client_from_env(use_mock=use_mock, domain=domain)
                            woo.update_product(str(obj_id), woo_fields, allow_empty=True)
                            transports_used.append("woo_rest")
                            live_used = live_used or (not use_mock)
                    elif obj_type in {"page", "post"}:
                        wp_fields = {
                            k: rest_needed[k]
                            for k in _SEO_WP_REST_FIELDS
                            if k in rest_needed and rest_needed[k] is not None
                        }
                        if wp_fields:
                            wp = wp_client or self._wp_client(site, domain=domain, use_mock=use_mock)
                            if obj_type == "post":
                                wp.update_post(
                                    str(obj_id),
                                    title=wp_fields.get("title"),
                                    content=wp_fields.get("content"),
                                    excerpt=wp_fields.get("excerpt"),
                                    slug=wp_fields.get("slug"),
                                    allow_empty=True,
                                    max_content_len=131_072,
                                )
                            else:
                                wp.update_page(
                                    str(obj_id),
                                    title=wp_fields.get("title"),
                                    content=wp_fields.get("content"),
                                    excerpt=wp_fields.get("excerpt"),
                                    slug=wp_fields.get("slug"),
                                    allow_empty=True,
                                    max_content_len=131_072,
                                )
                            transports_used.append("wp_rest")
                            live_used = live_used or (not use_mock)

                if not transports_used:
                    raise ValueError("Bridge krävs för återställning av SEO-metadata.")
                return {
                    "transport": "+".join(transports_used) if len(transports_used) > 1 else transports_used[0],
                    "live_calls": live_used,
                }

            if cat == "media_alt":
                ref_parts = str(record.get("target_ref") or "").split(":")
                mid = int(str(meta.get("media_id") or before.get("media_id") or ref_parts[-1]))
                fields = {
                    k: before[k]
                    for k in _MEDIA_ALT_ROLLBACK_FIELDS
                    if k in before and before[k] is not None
                }
                if not fields:
                    raise ValueError("MissingBeforeSnapshot")

                if bridge_client is not None or record.get("transport") == "bridge":
                    from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site

                    bc = bridge_client or (bridge_client_for_site(site) if site is not None else None)
                    if bc is not None:
                        res = bc.seo_update_media(
                            change_id=f"rb-{record['change_id']}",
                            media_id=mid,
                            fields=fields,
                        )
                        if not res.get("ok"):
                            raise ValueError("Bridge media rollback failed")
                        return {"transport": "bridge", "live_calls": bool(getattr(bc, "is_live", not use_mock))}

                wp = wp_client or self._wp_client(site, domain=domain, use_mock=use_mock)
                wp.update_media(
                    mid,
                    alt_text=fields.get("alt_text"),
                    title=fields.get("title"),
                    caption=fields.get("caption"),
                    description=fields.get("description"),
                    allow_empty=True,
                )
                return {"transport": "wp_rest", "live_calls": not use_mock}

            raise ValueError(f"Unsupported entity rollback category: {cat}")

        # File / workspace rollback
        if site is None:
            raise ValueError("UnknownSite for file rollback")
        entries = list(snap.get("entries") or [])
        first_entry = entries[0] if entries and isinstance(entries[0], dict) else {}
        ref_parts = str(record.get("target_ref") or "").split(":")
        ref_kind = ref_parts[0] if len(ref_parts) >= 2 and ref_parts[0] in {"theme", "plugin", "mu_plugin"} else ""
        ref_slug = ref_parts[1] if len(ref_parts) >= 2 and ref_parts[0] in {"theme", "plugin", "mu_plugin"} else ""
        kind = str(meta.get("kind") or first_entry.get("kind") or ref_kind or "theme")
        slug = str(meta.get("slug") or first_entry.get("slug") or ref_slug or "")
        before_files = dict(snap.get("before_files") or {})
        reverse_entries: list[dict[str, Any]] = []
        for entry in entries:
            rel = str(entry.get("path") or "")
            was_present = bool(entry.get("before_present"))
            rev_op = "updated" if was_present else "deleted"
            reverse_entries.append({"path": rel, "op": rev_op})

        if deploy_transport is not None:
            dt_root = getattr(deploy_transport, "files_root", "") or site.files_root
            created = [e["path"] for e in entries if not e.get("before_present")]
            files_abs = {
                _resolve_rollback_abs(dt_root, kind, slug, rel): data
                for rel, data in before_files.items()
            }
            created_abs = [_resolve_rollback_abs(dt_root, kind, slug, rel) for rel in created]
            deploy_transport.rollback_bundle(files_abs, created_abs)
            _best_effort_bridge_cache_flush(site, bridge_client)
            return {"transport": "sftp_deploy", "live_calls": not use_mock, "reverse_entries": reverse_entries}

        if bridge_client is not None:
            bridge_client.rollback(change_id=record["change_id"], kind=kind, slug=slug, entries=reverse_entries, files=before_files)
            return {"transport": "bridge", "live_calls": not use_mock, "reverse_entries": reverse_entries}

        if use_mock:
            from ecom_ops.integrations.wp_files_ssh import build_file_transport

            transport = build_file_transport(site, self.data_dir)
            if not isinstance(transport, LocalTree) or transport_is_remote(transport):
                raise ValueError("LocalTree required for mock rollback")
            for entry in entries:
                rel = str(entry.get("path") or "")
                abs_path = _resolve_rollback_abs(site.files_root, kind, slug, rel)
                if entry.get("before_present"):
                    data = before_files[rel]
                    local_dest = transport._local_checked(abs_path)
                    if local_dest.is_file():
                        transport.write_bytes(abs_path, data)
                    else:
                        transport.create_bytes(abs_path, data, overwrite=True)
                else:
                    local_dest = transport._local_checked(abs_path)
                    if local_dest.is_file():
                        local_dest.unlink()
            return {"transport": "local_mock", "live_calls": False, "reverse_entries": reverse_entries}

        # Live file rollback: prefer dedicated SFTP deployer only when configured for site, else Bridge
        from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site, bridge_enabled
        from ecom_ops.integrations.wp.sftp_deploy_transport import (
            SshDeployTransport,
            deploy_ssh_enabled,
        )

        deploy_overlay = load_deploy_overlay(self.data_dir).get(site.id, {})
        has_deploy_ssh = (
            deploy_ssh_enabled()
            and str(deploy_overlay.get("deploy_ssh_user") or "").strip().casefold().endswith("-wp-deployer")
            and stored_deploy_key_file(self.data_dir, site.id).is_file()
        )
        if has_deploy_ssh:
            dt = SshDeployTransport(site, self.data_dir)
            dt_root = dt.files_root or site.files_root
            files_abs = {
                _resolve_rollback_abs(dt_root, kind, slug, rel): data
                for rel, data in before_files.items()
            }
            created_abs = [
                _resolve_rollback_abs(dt_root, kind, slug, e["path"])
                for e in entries
                if not e.get("before_present")
            ]
            dt.rollback_bundle(files_abs, created_abs)
            _best_effort_bridge_cache_flush(site, bridge_client)
            return {"transport": "sftp_deploy", "live_calls": True, "reverse_entries": reverse_entries}

        if bridge_enabled():
            bc = bridge_client_for_site(site)
            if bc is not None:
                bc.rollback(
                    change_id=record["change_id"],
                    kind=kind,
                    slug=slug,
                    entries=reverse_entries,
                    files=before_files,
                )
                return {"transport": "bridge", "live_calls": True, "reverse_entries": reverse_entries}

        raise ValueError("Ingen live-rollbacktransport (AZOM_WP_DEPLOY_SSH / AZOM_WP_BRIDGE_ENABLED) är aktiverad.")

    @staticmethod
    def _wp_client(site: Site | None, *, domain: str | None, use_mock: bool) -> Any:
        from ecom_ops.integrations import wp_client_from_env
        from ecom_ops.integrations.wp_component_reads import wp_client_for_site

        dom_clean = str(domain or "").strip().lower().lstrip(".")
        if not use_mock and site is not None:
            site_host = (urlparse(str(site.canonical_url or "")).hostname or "").lower()
            if not dom_clean or site_host.endswith(f".{dom_clean}"):
                per_site = wp_client_for_site(site, fallback_to_env=False)
                if per_site is not None:
                    return per_site
        return wp_client_from_env(
            base_url=str(site.canonical_url) if (not use_mock and site is not None and not dom_clean) else None,
            use_mock=use_mock,
            domain=domain,
        )


__all__ = ["ChangeRollbackService"]
