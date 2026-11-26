"""Local mock backup manifests.

The digest covers canonical metadata. It is not encryption. This provider
does not copy site files, write a database dump, or send bytes anywhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
from ecom_ops.wp_ops.models import Site, WpOpsResult, ensure_utc, parse_iso_utc
from ecom_ops.wp_ops.policy import WpOpsPolicy
from ecom_ops.wp_ops.registry import site_by_id
from ecom_ops.wp_ops.store import WpOpsStore

_BACKUP_ID = re.compile(r"^bk-[a-f0-9]{12}$")

_PARTS = ["files", "database"]
_DIGEST_FIELDS = (
    "id",
    "site_id",
    "environment",
    "provider",
    "offsite",
    "encryption",
    "destination_ref",
    "parts",
    "created_at",
    "retention_days",
    "woocommerce",
    "payload",
    "bytes_copied",
)


def valid_backup_id(value: str) -> bool:
    return bool(_BACKUP_ID.fullmatch(value))


class BackupProvider(Protocol):
    def verify(self, record: dict[str, Any]) -> bool:
        """Return true when the manifest digest still matches."""


def integrity_digest(record: dict[str, Any]) -> str:
    body = {key: record.get(key) for key in _DIGEST_FIELDS}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_manifest(site: Site, *, created_at: str) -> dict[str, Any]:
    backup_id = "bk-" + uuid.uuid4().hex[:12]
    record: dict[str, Any] = {
        "id": backup_id,
        "site_id": site.id,
        "environment": site.environment,
        "provider": "mock",
        "offsite": False,
        "encryption": "unconfigured",
        "destination_ref": f"mock:{site.id}:{backup_id}",
        "parts": list(_PARTS),
        "created_at": created_at,
        "verified_at": None,
        "completion": "pending",
        "integrity": "pending",
        "digest": "",
        "retention_days": site.retention_days,
        "woocommerce": site.woocommerce,
        "payload": "metadata",
        "bytes_copied": False,
        "live_calls": False,
    }
    record["digest"] = integrity_digest(record)
    return record


class MockBackup:
    """Verify a local manifest. Never marks the set as off-site or encrypted."""

    def verify(self, record: dict[str, Any]) -> bool:
        if record.get("provider") != "mock" or record.get("offsite") is not False:
            return False
        if record.get("encryption") != "unconfigured" or record.get("payload") != "metadata":
            return False
        if record.get("bytes_copied") is not False or record.get("parts") != _PARTS:
            return False
        return record.get("digest") == integrity_digest(record)


class BackupService:
    """Local mock backup manifests, staging restore drills, and hard-refused restore()."""

    def __init__(
        self,
        *,
        store: WpOpsStore,
        policy: WpOpsPolicy,
        provider: BackupProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.provider = provider or MockBackup()
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return ensure_utc(self.clock())

    def _safe_state(self, *, include_executed: bool = False) -> dict[str, Any] | WpOpsResult:
        try:
            return self.store.load()
        except ValueError:
            data: dict[str, Any] = {"error_class": "StateUnreadable", "live_calls": False}
            if include_executed:
                data["executed"] = False
            return WpOpsResult(ok=False, message="Check state is unreadable", data=data)

    @staticmethod
    def _drill_refused(error_class: str, message: str) -> WpOpsResult:
        return WpOpsResult(
            ok=False,
            message=message,
            data={"error_class": error_class, "executed": False, "live_calls": False},
        )

    def restore(self, site_id: str, *, actor: str, change_ref: str = "") -> WpOpsResult:
        """Refuse every restore. The change reference is not stored or returned."""
        del change_ref
        denied = self.policy.require(actor, Permission.WP_OPS_RESTORE, action="wp_ops_restore")
        if denied is not None:
            return denied
        audit_err = self.policy.audit(
            actor,
            action="wp_ops_restore",
            target_id=site_id,
            success=False,
            details={"error_class": "RestoreNotInMvp", "executed": False, "permission": "wp_ops_restore"},
        )
        if audit_err is not None:
            return audit_err
        return WpOpsResult(
            ok=False,
            message="Restore is not available in this phase.",
            data={"error_class": "RestoreNotInMvp", "executed": False, "live_calls": False},
        )

    def create_backup(self, site_id: str, *, actor: str) -> WpOpsResult:
        """Store one local mock manifest. Files and database are labels, not bytes."""
        site, err = self.policy.resolve_site(site_id, actor=actor, access="write", action="wp_ops_backup")
        if err is not None:
            return err
        assert site is not None
        with self.policy.try_site_lock(
            site,
            actor=actor,
            action="wp_ops_backup",
            operation="backup_create",
        ) as busy:
            if busy is not None:
                return busy
            return self._create_locked(site, actor=actor)

    def list_backups(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self.policy.require(actor, Permission.WP_OPS_READ, action="wp_ops_backup")
        if denied is not None:
            return denied
        sites, config_err = self.policy.load_all_sites()
        if config_err is not None:
            return config_err
        site = site_by_id(sites, site_id)
        if site is None:
            return WpOpsResult(
                ok=False,
                message="Unknown site",
                data={"error_class": "UnknownSite", "site_id": site_id, "live_calls": False},
            )
        state = self._safe_state()
        if isinstance(state, WpOpsResult):
            return state
        return WpOpsResult(
            ok=True,
            message="backups",
            data={"site_id": site.id, "backups": self.backups_for(state, site.id), "live_calls": False},
        )

    def restore_drill(self, backup_id: str, *, target_site_id: str, actor: str) -> WpOpsResult:
        """Record a staging drill of a verified manifest. Production targets are refused."""
        denied = self.policy.require(actor, Permission.WP_OPS_OPERATE, action="wp_ops_restore_drill")
        if denied is not None:
            return denied
        if not valid_backup_id(str(backup_id or "")):
            return self._drill_refused("UnknownBackup", "Unknown backup")
        sites, config_err = self.policy.load_all_sites()
        if config_err is not None:
            return config_err
        target = site_by_id(sites, target_site_id)
        if target is None:
            return WpOpsResult(
                ok=False,
                message="Unknown site",
                data={"error_class": "UnknownSite", "site_id": target_site_id, "executed": False, "live_calls": False},
            )
        if target.environment != "staging":
            audit_err = self.policy.audit(
                actor,
                action="wp_ops_restore_drill",
                target_id=target.id,
                success=False,
                details={
                    "error_class": "TargetNotStaging",
                    "executed": False,
                    "environment": target.environment,
                    "permission": Permission.WP_OPS_OPERATE.value,
                    "operation": "restore_drill",
                    "plan_id": None,
                    "bytes_copied": False,
                    "live_calls": False,
                },
            )
            if audit_err is not None:
                return audit_err
            return self._drill_refused("TargetNotStaging", "Production restore is not available.")
        with self.policy.try_site_lock(
            target,
            actor=actor,
            action="wp_ops_restore_drill",
            operation="restore_drill",
            permission=Permission.WP_OPS_OPERATE,
        ) as busy:
            if busy is not None:
                return busy
            return self._drill_locked(backup_id, target=target, actor=actor)

    def _create_locked(self, site: Site, *, actor: str) -> WpOpsResult:
        now = self._now()
        bound = bind_credential(site)
        secrets = [bound.value] if bound.present else []
        record = new_manifest(site, created_at=now.isoformat())
        try:
            passed = bool(self.provider.verify(record))
        except Exception:
            passed = False
        if passed:
            record["completion"] = "verified"
            record["integrity"] = "passed"
            record["verified_at"] = now.isoformat()
            error_class = None
        else:
            record["completion"] = "failed"
            record["integrity"] = "failed"
            record["verified_at"] = None
            error_class = "IntegrityFailed"
        scrubbed = scrub_secrets(record, secrets)
        assert isinstance(scrubbed, dict)

        def mutate(current: dict[str, Any]) -> None:
            current.setdefault("backups", []).append(scrubbed)

        try:
            self.store.update(mutate)
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "executed": False, "live_calls": False},
            )
        audit_err = self.policy.audit(
            actor,
            action="wp_ops_backup",
            target_id=site.id,
            success=passed,
            details={
                "environment": site.environment,
                "permission": self.policy.write_permission_for(site).value,
                "operation": "backup_create",
                "plan_id": None,
                "error_class": error_class,
                "result": scrubbed["completion"],
                "backup_id": scrubbed["id"],
                "offsite": False,
                "encryption": "unconfigured",
                "bytes_copied": False,
                "live_calls": False,
            },
        )
        if audit_err is not None:
            return audit_err
        public = self.public_backup(scrubbed, now)
        if passed:
            return WpOpsResult(
                ok=True,
                message="backup verified",
                data={"backup": public, "executed": True, "live_calls": False},
            )
        return WpOpsResult(
            ok=False,
            message="Backup integrity check failed.",
            data={
                "backup": public,
                "executed": True,
                "error_class": "IntegrityFailed",
                "live_calls": False,
            },
        )

    def _drill_locked(self, backup_id: str, *, target: Site, actor: str) -> WpOpsResult:
        state = self._safe_state(include_executed=True)
        if isinstance(state, WpOpsResult):
            return state
        now = self._now()
        stored = next(
            (
                row
                for row in state.get("backups", [])
                if isinstance(row, dict) and row.get("id") == backup_id
            ),
            None,
        )
        if stored is None:
            return self._drill_refused("UnknownBackup", "Unknown backup")
        public_backup = self.public_backup(stored, now)
        if public_backup["completion"] == "expired":
            return self._drill_refused("BackupExpired", "Backup is past retention.")
        if public_backup["completion"] != "verified" or public_backup["integrity"] != "passed":
            return self._drill_refused("BackupNotVerified", "Backup is not verified.")
        previous = self._recent_drill(state, backup_id, target.id, now, target.dedupe_window_seconds)
        if previous is not None:
            return WpOpsResult(
                ok=True,
                message="duplicate drill",
                data={
                    "drill": self.public_drill(previous),
                    "duplicate": True,
                    "executed": False,
                    "live_calls": False,
                },
            )
        started = time.perf_counter()
        try:
            passed = bool(self.provider.verify(stored))
        except Exception:
            passed = False
        recovery_seconds = round(max(0.0, time.perf_counter() - started), 3)
        if not passed:
            return self._drill_refused("IntegrityFailed", "Backup integrity check failed.")
        woo = bool(stored.get("woocommerce"))
        drill = {
            "id": "dr-" + uuid.uuid4().hex[:12],
            "source_backup_id": backup_id,
            "source_site_id": stored.get("site_id"),
            "target_site_id": target.id,
            "target_environment": "staging",
            "parts": ["files", "database"],
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
            "recovery_seconds": recovery_seconds,
            "integrity": "passed",
            "order_reconciliation": "required" if woo else "not_applicable",
            "orders_mutated": False,
            "bytes_copied": False,
            "provider": "mock",
            "offsite": False,
            "encryption": "unconfigured",
            "payload": "metadata",
            "live_calls": False,
        }

        def mutate(current: dict[str, Any]) -> None:
            current.setdefault("drills", []).append(drill)

        try:
            self.store.update(mutate)
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "executed": False, "live_calls": False},
            )
        audit_err = self.policy.audit(
            actor,
            action="wp_ops_restore_drill",
            target_id=target.id,
            success=True,
            details={
                "environment": target.environment,
                "permission": Permission.WP_OPS_OPERATE.value,
                "operation": "restore_drill",
                "plan_id": None,
                "error_class": None,
                "backup_id": backup_id,
                "source_site_id": stored.get("site_id"),
                "target_site_id": target.id,
                "recovery_seconds": recovery_seconds,
                "order_reconciliation": drill["order_reconciliation"],
                "orders_mutated": False,
                "bytes_copied": False,
                "offsite": False,
                "encryption": "unconfigured",
                "live_calls": False,
            },
        )
        if audit_err is not None:
            return audit_err
        return WpOpsResult(
            ok=True,
            message="staging drill recorded",
            data={"drill": self.public_drill(drill), "duplicate": False, "executed": True, "live_calls": False},
        )

    def backups_for(self, state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        now = self._now()
        rows = [
            self.public_backup(row, now)
            for row in state.get("backups", [])
            if isinstance(row, dict) and row.get("site_id") == site_id
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows

    def drills_for(self, state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        rows = [
            self.public_drill(row)
            for row in state.get("drills", [])
            if isinstance(row, dict)
            and (row.get("source_site_id") == site_id or row.get("target_site_id") == site_id)
        ]
        rows.sort(key=lambda row: str(row.get("finished_at") or ""), reverse=True)
        return rows

    def catalog_backup_view(self, site: Site, state: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.backups_for(state, site.id)
        if not rows:
            return None
        latest = rows[0]
        base = {
            "tone": "attention",
            "green": False,
            "offsite": False,
            "encryption": "unconfigured",
            "provider": "mock",
        }
        if latest["completion"] == "verified" and latest["integrity"] == "passed":
            return {**base, "result": "verified", "error_class": None}
        if latest["completion"] == "failed":
            return {**base, "result": "fail", "error_class": "IntegrityFailed"}
        return {**base, "result": "unknown", "error_class": "BackupExpired"}

    def overlay_backup_check(
        self, checks: list[dict[str, Any]], site: Site, state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        view = self.catalog_backup_view(site, state)
        if view is None:
            return checks
        latest = self.backups_for(state, site.id)[0]
        row = {
            "site_id": site.id,
            "environment": site.environment,
            "check": "backup",
            "target": site.id,
            "result": view["result"],
            "tone": "attention",
            "green": False,
            "error_class": view.get("error_class"),
            "checked_at": latest.get("verified_at") or latest.get("created_at"),
        }
        replaced = False
        out: list[dict[str, Any]] = []
        for item in checks:
            if item.get("check") == "backup" and not replaced:
                out.append(row)
                replaced = True
            else:
                out.append(item)
        if not replaced:
            out.append(row)
        return out

    @staticmethod
    def public_backup(record: dict[str, Any], now: datetime) -> dict[str, Any]:
        completion = record.get("completion")
        verified_at = record.get("verified_at")
        if completion == "verified" and verified_at:
            verified = parse_iso_utc(str(verified_at))
            if verified is not None:
                limit = int(record.get("retention_days") or 14)
                if now - verified > timedelta(days=limit):
                    completion = "expired"
        return {
            "id": record.get("id"),
            "site_id": record.get("site_id"),
            "environment": record.get("environment"),
            "provider": "mock",
            "offsite": False,
            "encryption": "unconfigured",
            "destination_ref": record.get("destination_ref"),
            "parts": list(record.get("parts") or []),
            "created_at": record.get("created_at"),
            "verified_at": verified_at,
            "completion": completion,
            "integrity": record.get("integrity"),
            "digest": record.get("digest"),
            "retention_days": record.get("retention_days"),
            "woocommerce": record.get("woocommerce"),
            "payload": "metadata",
            "bytes_copied": False,
            "live_calls": False,
        }

    @staticmethod
    def public_drill(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record.get("id"),
            "source_backup_id": record.get("source_backup_id"),
            "source_site_id": record.get("source_site_id"),
            "target_site_id": record.get("target_site_id"),
            "target_environment": record.get("target_environment"),
            "parts": list(record.get("parts") or []),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "recovery_seconds": record.get("recovery_seconds"),
            "integrity": record.get("integrity"),
            "order_reconciliation": record.get("order_reconciliation"),
            "orders_mutated": False,
            "bytes_copied": False,
            "provider": "mock",
            "offsite": False,
            "encryption": "unconfigured",
            "payload": "metadata",
            "live_calls": False,
        }

    @staticmethod
    def _recent_drill(
        state: dict[str, Any],
        backup_id: str,
        target_id: str,
        now: datetime,
        window_seconds: int,
    ) -> dict[str, Any] | None:
        for row in reversed(state.get("drills", [])):
            if not isinstance(row, dict):
                continue
            if row.get("source_backup_id") != backup_id or row.get("target_site_id") != target_id:
                continue
            finished = parse_iso_utc(str(row.get("finished_at") or ""))
            if finished is not None and now - finished < timedelta(seconds=window_seconds):
                return row
        return None
