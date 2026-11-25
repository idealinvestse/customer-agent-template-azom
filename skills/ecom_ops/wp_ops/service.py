"""WordPress site health, local mock backups, and typed theme or plugin edits.

There is no live HTTP client and no production restore. A file edit is not a
shell: the editor computes one path and a transport lists, reads, or replaces
that file. Live SSH stays off unless mock mode is off and the owner sets
``AZOM_WP_FILES_SSH=1``.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.backup import MockBackup, new_manifest, valid_backup_id
from ecom_ops.wp_ops.connectors import (
    MockRest,
    MockUptime,
    ProbeOutcome,
    RestConnector,
    UptimeConnector,
)
from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
from ecom_ops.wp_ops.models import Site, clean_error_class, tone_for
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.store import WpOpsStore

_SIGNAL_CHECKS = frozenset({"http_uptime", "tls", "dns", "wp_rest", "inventory"})
_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


def _site_lock(site_id: str) -> threading.Lock:
    with _LOCK_GUARD:
        lock = _LOCKS.get(site_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[site_id] = lock
        return lock


@dataclass
class WpOpsResult:
    ok: bool
    message: str
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "data": self.data}


class WpOpsService:
    """Registry, mock probes, and incident deduplication.

    Production updates and restores are not implemented. ``restore`` refuses
    even for an actor who holds ``WP_OPS_RESTORE``. A staging drill records a
    local manifest check and does not copy site files. Theme and plugin edits
    use a typed path inside the site files root. They are not Phase 4 updates.
    """

    def __init__(
        self,
        *,
        registry_path: Path | None = None,
        data_dir: Path | None = None,
        uptime: UptimeConnector | None = None,
        rest: RestConnector | None = None,
        backup: MockBackup | None = None,
        clock: Callable[[], datetime] | None = None,
        files_factory: Callable[[Site], Any] | None = None,
    ) -> None:
        self.registry_path = registry_path
        base = data_dir or Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
        self.data_dir = base
        self.files_factory = files_factory
        self.store = WpOpsStore(base / "wp_ops_state.json")
        self.uptime = uptime or MockUptime()
        self.rest = rest or MockRest()
        self.backup = backup or MockBackup()
        self.clock = clock or (lambda: datetime.now(UTC))

    def list_sites(self, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_READ)
        if denied is not None:
            return denied
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return self._config_error(str(exc))
        state = self._safe_state()
        if isinstance(state, WpOpsResult):
            return state
        rows = [self._summary(site, state) for site in sites]
        return WpOpsResult(ok=True, message="sites", data={"sites": rows, "live_calls": False})

    def show_site(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_READ)
        if denied is not None:
            return denied
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return self._config_error(str(exc))
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
            message="site",
            data={
                "site": self._summary(site, state),
                "checks": self._overlay_backup_check(
                    self._checks_for(state, site.id), site, state
                ),
                "incidents": self._incidents_for(state, site.id),
                "backups": self._backups_for(state, site.id),
                "drills": self._drills_for(state, site.id),
                "staging_targets": self._staging_targets(sites),
                "live_calls": False,
            },
        )

    def list_components(self, site_id: str, kind: str, *, actor: str) -> WpOpsResult:
        return self._file_editor().list_components(site_id, kind, actor=actor)

    def list_files(self, site_id: str, kind: str, slug: str, *, actor: str) -> WpOpsResult:
        return self._file_editor().list_files(site_id, kind, slug, actor=actor)

    def read_file(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        *,
        actor: str,
        admin_only: bool = False,
    ) -> WpOpsResult:
        return self._file_editor().read_file(
            site_id,
            kind,
            slug,
            relative,
            actor=actor,
            admin_only=admin_only,
        )

    def propose_file(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        *,
        actor: str,
    ) -> WpOpsResult:
        return self._file_editor().propose(
            site_id, kind, slug, relative, new_text, actor=actor
        )

    def apply_file(
        self,
        site_id: str,
        plan_id: str,
        new_text: str,
        *,
        actor: str,
    ) -> WpOpsResult:
        return self._file_editor().apply(site_id, plan_id, new_text, actor=actor)

    def _file_changes(self):
        from ecom_ops.wp_ops.file_changes import FileChangeOps

        return FileChangeOps(self._file_editor())

    def list_component_tree(
        self, site_id: str, kind: str, slug: str, relative: str, *, actor: str
    ) -> WpOpsResult:
        return self._file_changes().list_tree(site_id, kind, slug, relative, actor=actor)

    def search_component(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        query: str,
        *,
        actor: str,
        extensions: list[str] | None = None,
        case_insensitive: bool = True,
        mode: str = "literal",
        context_lines: int = 0,
    ) -> WpOpsResult:
        return self._file_changes().search(
            site_id,
            kind,
            slug,
            relative,
            query,
            actor=actor,
            extensions=extensions,
            case_insensitive=case_insensitive,
            mode=mode,
            context_lines=context_lines,
        )

    def _review_plans(self):
        from ecom_ops.wp_ops.review_plan import ReviewPlans

        return ReviewPlans(
            data_dir=self.data_dir,
            registry_path=self.registry_path,
            clock=self.clock,
            transport_factory=self.files_factory,
        )

    def list_workspaces(
        self,
        site_id: str | None = None,
        *,
        actor: str,
        status: str | None = None,
        include_terminal: bool = False,
    ) -> WpOpsResult:
        return self._review_plans().list_plans(
            site_id,
            actor=actor,
            status=status,
            include_terminal=include_terminal,
        )

    def workspace_list_files(
        self, site_id: str, plan_id: str, rel_dir: str = "", *, actor: str
    ) -> WpOpsResult:
        return self._review_plans().workspace_list_files(site_id, plan_id, rel_dir, actor=actor)

    def workspace_read_file(
        self,
        site_id: str,
        plan_id: str,
        rel_path: str,
        *,
        actor: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> WpOpsResult:
        return self._review_plans().workspace_read_file(
            site_id,
            plan_id,
            rel_path,
            actor=actor,
            start_line=start_line,
            end_line=end_line,
        )

    def workspace_diff(
        self,
        site_id: str,
        plan_id: str,
        rel_path: str | None = None,
        *,
        actor: str,
    ) -> WpOpsResult:
        return self._review_plans().workspace_diff(site_id, plan_id, rel_path, actor=actor)

    def workspace_apply_changes(
        self,
        site_id: str,
        plan_id: str,
        changes: list[dict[str, Any]],
        *,
        actor: str,
        allow_create: bool = True,
        allow_delete: bool = True,
    ) -> WpOpsResult:
        return self._review_plans().workspace_apply_changes(
            site_id,
            plan_id,
            changes,
            actor=actor,
            allow_create=allow_create,
            allow_delete=allow_delete,
        )

    def workspace_bulk_replace(
        self,
        site_id: str,
        plan_id: str,
        find: str,
        replace: str,
        *,
        actor: str,
        mode: str = "literal",
        extensions: list[str] | None = None,
        path_prefix: str | None = None,
        dry_run: bool = False,
    ) -> WpOpsResult:
        return self._review_plans().workspace_bulk_replace(
            site_id,
            plan_id,
            find,
            replace,
            actor=actor,
            mode=mode,
            extensions=extensions,
            path_prefix=path_prefix,
            dry_run=dry_run,
        )

    def workspace_scaffold(
        self,
        site_id: str,
        plan_id: str,
        template: str,
        *,
        actor: str,
        component_slug: str | None = None,
    ) -> WpOpsResult:
        return self._review_plans().workspace_scaffold(
            site_id,
            plan_id,
            template,
            actor=actor,
            component_slug=component_slug,
        )

    def workspace_theme_json(
        self,
        site_id: str,
        plan_id: str,
        operations: list[dict[str, Any]],
        *,
        actor: str,
    ) -> WpOpsResult:
        return self._review_plans().workspace_theme_json(
            site_id,
            plan_id,
            operations,
            actor=actor,
        )

    def audit_overrides(
        self,
        site_id: str,
        slug: str,
        *,
        actor: str,
        woo_version: str | None = None,
        reference_versions: dict[str, str] | None = None,
    ) -> WpOpsResult:
        from ecom_ops.wp_ops.files import PathRejected, component_dir
        from ecom_ops.wp_ops.review_workspace import ReviewError, read_site_component
        from ecom_ops.wp_ops.woo_templates import audit_overrides as run_override_audit

        editor = self._file_editor()
        site, denied = editor._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        try:
            component_dir(site.files_root, "theme", slug)
        except PathRejected:
            return editor._fail("PathRejected", "Kind or path is not editable.")
        ssh = editor._ssh(site)
        transport = editor._transport(site)
        try:
            raw_files = read_site_component(transport, site.files_root, "theme", slug)
        except ReviewError as exc:
            return editor._fail(exc.error_class, exc.error_class, site=site, ssh=ssh)
        text_files = [(rel, data.decode("utf-8", errors="replace")) for rel, data in raw_files]
        report = run_override_audit(
            text_files,
            woo_version=woo_version or site.woo_version or None,
            reference_versions=reference_versions,
        )
        return WpOpsResult(
            ok=True,
            message="woo overrides audited",
            data={
                "site_id": site.id,
                "slug": slug,
                **report,
                "ssh": ssh,
                "live_calls": ssh,
            },
        )

    def propose_file_diff(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        base_sha256: str,
        summary: str,
        *,
        actor: str,
        parent_slug: str | None = None,
    ) -> WpOpsResult:
        return self._file_changes().propose(
            site_id,
            kind,
            slug,
            relative,
            new_text,
            base_sha256,
            summary,
            actor=actor,
            create=False,
            parent_slug=parent_slug,
        )

    def propose_new_file(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        base_sha256: str,
        summary: str,
        *,
        actor: str,
        parent_slug: str | None = None,
    ) -> WpOpsResult:
        return self._file_changes().propose(
            site_id,
            kind,
            slug,
            relative,
            new_text,
            base_sha256,
            summary,
            actor=actor,
            create=True,
            parent_slug=parent_slug,
        )

    def validate_file_plan(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().validate(site_id, plan_id, actor=actor)

    def preview_file_stage(self, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().preview_stage(plan_id, actor=actor)

    def stage_file_plan(self, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().stage(plan_id, actor=actor)

    def deny_file_plan(self, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().deny(plan_id, actor=actor)

    def request_file_production(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().request_production(site_id, plan_id, actor=actor)

    def deploy_file_production(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().deploy_production(site_id, plan_id, actor=actor)

    def verify_file_plan(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().verify(site_id, plan_id, actor=actor)

    def rollback_file_plan(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().rollback(site_id, plan_id, actor=actor)

    def review_file_plan(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return self._file_changes().review(site_id, plan_id, actor=actor)

    def ssh_settings(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_ops_ssh_settings")
        if denied is not None:
            return denied
        registry_site, site, failed = self._ssh_site(site_id)
        if failed is not None:
            return failed
        assert registry_site is not None and site is not None
        from ecom_ops.wp_ops.ssh_settings import describe_ssh

        try:
            view = describe_ssh(
                site,
                registry_site,
                self.data_dir,
                include_paths=self._ssh_admin(actor),
            )
        except ValueError as exc:
            return self._config_error(str(exc))
        return WpOpsResult(ok=True, message="ssh settings", data={"ssh": view, "live_calls": False})

    def save_ssh_settings(
        self,
        site_id: str,
        *,
        actor: str,
        files_root: str,
        ssh_host: str,
        ssh_user: str,
        ssh_port: str,
        ssh_identity_file: str,
        private_key: str,
        clear_key: bool,
        global_user: str,
        global_port: str,
        global_identity_file: str,
        host_public_key: str = "",
    ) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_ADMIN, action="wp_ops_ssh_settings")
        if denied is not None:
            return denied
        registry_site, _site, failed = self._ssh_site(site_id)
        if failed is not None:
            return failed
        assert registry_site is not None
        from ecom_ops.wp_ops.ssh_settings import SettingsRejected, save_ssh

        try:
            saved = save_ssh(
                self.data_dir,
                registry_site,
                files_root=files_root,
                ssh_host=ssh_host,
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                ssh_identity_file=ssh_identity_file,
                private_key=private_key,
                clear_key=clear_key,
                global_user=global_user,
                global_port=global_port,
                global_identity_file=global_identity_file,
                host_public_key=host_public_key,
            )
        except SettingsRejected as exc:
            self._audit_ssh(actor, site_id, success=False, changed=[], identity_present=False)
            return WpOpsResult(
                ok=False,
                message=str(exc),
                data={"error_class": "ConfigInvalid", "live_calls": False},
            )
        except ValueError as exc:
            return self._config_error(str(exc))
        self._audit_ssh(
            actor,
            site_id,
            success=True,
            changed=list(saved["changed"]),
            identity_present=bool(saved["identity_present"]),
        )
        message = "SSH-inställningar sparade"
        if saved.get("global_notice"):
            message = f"{message}. {saved['global_notice']}"
        return WpOpsResult(
            ok=True,
            message=message,
            data={"changed": list(saved["changed"]), "live_calls": False},
        )

    def run_checks(self, site_id: str, *, actor: str, scheduled: bool = False) -> WpOpsResult:
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return self._config_error(str(exc))
        site = site_by_id(sites, site_id)
        if site is None:
            return WpOpsResult(
                ok=False,
                message="Unknown site",
                data={"error_class": "UnknownSite", "site_id": site_id, "executed": False},
            )
        permission = (
            Permission.WP_OPS_ADMIN if site.environment == "production" else Permission.WP_OPS_OPERATE
        )
        denied = self._need(actor, permission, site=site, action="wp_ops_check")
        if denied is not None:
            return denied
        lock = _site_lock(site.id)
        if not lock.acquire(blocking=False):
            self._audit(actor, site, success=False, error_class="SiteBusy", permission=permission.value)
            return WpOpsResult(
                ok=False,
                message="Site check already running",
                data={"error_class": "SiteBusy", "site_id": site.id, "executed": False, "live_calls": False},
            )
        try:
            return self._run_locked(site, actor=actor, scheduled=scheduled, permission=permission.value)
        finally:
            lock.release()

    def run_due(self, *, actor: str) -> WpOpsResult:
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return self._config_error(str(exc))
        state = self._safe_state()
        if isinstance(state, WpOpsResult):
            return state
        now = self._now()
        ran: list[str] = []
        skipped: list[dict[str, str]] = []
        for site in sites:
            last_raw = state.get("last_run", {}).get(site.id)
            if last_raw:
                last = datetime.fromisoformat(str(last_raw))
                if now - last < timedelta(seconds=site.check_interval_seconds):
                    skipped.append({"site_id": site.id, "error_class": "NotDue"})
                    continue
            result = self.run_checks(site.id, actor=actor, scheduled=True)
            error_class = str((result.data or {}).get("error_class") or "")
            if result.ok:
                ran.append(site.id)
            else:
                skipped.append({"site_id": site.id, "error_class": error_class or "CheckFailed"})
        denied_all = bool(skipped) and not ran and all(
            row["error_class"] == "AccessDenied" for row in skipped
        )
        return WpOpsResult(
            ok=not denied_all,
            message="due checks",
            data={"ran": ran, "skipped": skipped, "live_calls": False},
        )

    def restore(self, site_id: str, *, actor: str, change_ref: str = "") -> WpOpsResult:
        """Refuse every restore. The change reference is not stored or returned."""
        del change_ref
        denied = self._need(actor, Permission.WP_OPS_RESTORE, action="wp_ops_restore")
        if denied is not None:
            return denied
        self._audit_raw(
            actor,
            action="wp_ops_restore",
            target_id=site_id,
            success=False,
            details={"error_class": "RestoreNotInMvp", "executed": False, "permission": "wp_ops_restore"},
        )
        return WpOpsResult(
            ok=False,
            message="Restore is not available in this phase.",
            data={"error_class": "RestoreNotInMvp", "executed": False, "live_calls": False},
        )

    def create_backup(self, site_id: str, *, actor: str) -> WpOpsResult:
        """Store one local mock manifest. Files and database are labels, not bytes."""
        site, denied = self._site_for_write(site_id, actor=actor, action="wp_ops_backup")
        if isinstance(site, WpOpsResult):
            return site
        if denied is not None:
            return denied
        lock = _site_lock(site.id)
        if not lock.acquire(blocking=False):
            self._audit_raw(
                actor,
                action="wp_ops_backup",
                target_id=site.id,
                success=False,
                details={
                    "error_class": "SiteBusy",
                    "executed": False,
                    "environment": site.environment,
                    "permission": self._write_permission(site),
                    "operation": "backup_create",
                    "plan_id": None,
                    "live_calls": False,
                },
            )
            return self._busy(site.id)
        try:
            return self._create_locked(site, actor=actor)
        finally:
            lock.release()

    def list_backups(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self._need(actor, Permission.WP_OPS_READ, action="wp_ops_backup")
        if denied is not None:
            return denied
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return self._config_error(str(exc))
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
            data={"site_id": site.id, "backups": self._backups_for(state, site.id), "live_calls": False},
        )

    def restore_drill(self, backup_id: str, *, target_site_id: str, actor: str) -> WpOpsResult:
        """Record a staging drill of a verified manifest. Production targets are refused."""
        denied = self._need(actor, Permission.WP_OPS_OPERATE, action="wp_ops_restore_drill")
        if denied is not None:
            return denied
        if not valid_backup_id(str(backup_id or "")):
            return self._drill_refused("UnknownBackup", "Unknown backup")
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return self._config_error(str(exc))
        target = site_by_id(sites, target_site_id)
        if target is None:
            return WpOpsResult(
                ok=False,
                message="Unknown site",
                data={"error_class": "UnknownSite", "site_id": target_site_id, "executed": False, "live_calls": False},
            )
        if target.environment != "staging":
            self._audit_raw(
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
            return self._drill_refused("TargetNotStaging", "Production restore is not available.")
        lock = _site_lock(target.id)
        if not lock.acquire(blocking=False):
            return self._busy(target.id)
        try:
            return self._drill_locked(backup_id, target=target, actor=actor)
        finally:
            lock.release()

    @contextmanager
    def hold_site(self, site_id: str) -> Iterator[None]:
        lock = _site_lock(site_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def _site_for_write(
        self, site_id: str, *, actor: str, action: str
    ) -> tuple[Site | WpOpsResult, WpOpsResult | None]:
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return self._config_error(str(exc)), None
        site = site_by_id(sites, site_id)
        if site is None:
            return (
                WpOpsResult(
                    ok=False,
                    message="Unknown site",
                    data={"error_class": "UnknownSite", "site_id": site_id, "executed": False, "live_calls": False},
                ),
                None,
            )
        denied = self._need(actor, self._permission_for(site), site=site, action=action)
        if denied is not None:
            return denied, None
        return site, None

    def _permission_for(self, site: Site) -> Permission:
        if site.environment == "production":
            return Permission.WP_OPS_ADMIN
        return Permission.WP_OPS_OPERATE

    def _write_permission(self, site: Site) -> str:
        return self._permission_for(site).value

    def _busy(self, site_id: str) -> WpOpsResult:
        return WpOpsResult(
            ok=False,
            message="Site check already running",
            data={"error_class": "SiteBusy", "site_id": site_id, "executed": False, "live_calls": False},
        )

    def _drill_refused(self, error_class: str, message: str) -> WpOpsResult:
        return WpOpsResult(
            ok=False,
            message=message,
            data={"error_class": error_class, "executed": False, "live_calls": False},
        )

    def _create_locked(self, site: Site, *, actor: str) -> WpOpsResult:
        now = self._now()
        bound = bind_credential(site)
        secrets = [bound.value] if bound.present else []
        record = new_manifest(site, created_at=now.isoformat())
        try:
            passed = bool(self.backup.verify(record))
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
        self._audit_raw(
            actor,
            action="wp_ops_backup",
            target_id=site.id,
            success=passed,
            details={
                "environment": site.environment,
                "permission": self._write_permission(site),
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
        public = self._public_backup(scrubbed, now)
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
        try:
            state = self.store.load()
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "executed": False, "live_calls": False},
            )
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
        public_backup = self._public_backup(stored, now)
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
                    "drill": self._public_drill(previous),
                    "duplicate": True,
                    "executed": False,
                    "live_calls": False,
                },
            )
        started = time.perf_counter()
        try:
            passed = bool(self.backup.verify(stored))
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
        self._audit_raw(
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
        return WpOpsResult(
            ok=True,
            message="staging drill recorded",
            data={"drill": self._public_drill(drill), "duplicate": False, "executed": True, "live_calls": False},
        )

    def _run_locked(self, site: Site, *, actor: str, scheduled: bool, permission: str) -> WpOpsResult:
        now = self._now()
        bound = bind_credential(site)
        secrets = [bound.value] if bound.present else []
        try:
            state = self.store.load()
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "executed": False, "live_calls": False},
            )
        if not scheduled and self._is_duplicate(state, site, now):
            summary = self._summary(site, state)
            return WpOpsResult(
                ok=True,
                message="duplicate check",
                data={
                    "site": summary,
                    "checks": self._checks_for(state, site.id),
                    "duplicate": True,
                    "executed": False,
                    "live_calls": False,
                    "partial": False,
                    "healthy": summary["healthy"],
                    "down": summary["down"],
                },
            )
        records, partial = self._collect(site, credential_present=bound.present, now=now)
        records = scrub_secrets(records, secrets)
        assert isinstance(records, list)

        def mutate(current: dict[str, Any]) -> None:
            self._apply(current, site, records, now=now, scheduled=scheduled)

        try:
            updated = self.store.update(mutate)
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "executed": False, "live_calls": False},
            )
        summary = self._summary(site, updated)
        self._audit(
            actor,
            site,
            success=True,
            error_class=None,
            permission=permission,
            extra={"result": "fail" if summary["down"] or not summary["healthy"] else "ok", "partial": partial},
        )
        return WpOpsResult(
            ok=True,
            message="checks finished",
            data={
                "site": summary,
                "checks": self._checks_for(updated, site.id),
                "duplicate": False,
                "executed": True,
                "live_calls": False,
                "partial": partial,
                "healthy": summary["healthy"],
                "down": summary["down"],
            },
        )

    def _collect(
        self, site: Site, *, credential_present: bool, now: datetime
    ) -> tuple[list[dict[str, Any]], bool]:
        rows: list[dict[str, Any]] = []
        partial = False
        for url in site.probe_urls():
            outcome, failed, duration_ms = self._call_uptime(url, timeout=site.timeout_seconds)
            partial = partial or failed
            rows.append(
                self._row(
                    site, "http_uptime", url, outcome.result, outcome.error_class, now, duration_ms
                )
            )
            rows.extend(self._tls_dns(site, url, outcome, now, duration_ms))
        rest_outcome, rest_failed, rest_ms = self._call_rest(
            site, credential_present=credential_present
        )
        partial = partial or rest_failed
        rows.append(
            self._row(
                site,
                "wp_rest",
                site.probe_urls()[0],
                rest_outcome.result,
                rest_outcome.error_class,
                now,
                rest_ms,
            )
        )
        if site.wordpress_version and site.php_version:
            inventory = ProbeOutcome(result="ok")
        else:
            inventory = ProbeOutcome(result="unknown", error_class="InventoryIncomplete")
        rows.append(self._row(site, "inventory", site.id, inventory.result, inventory.error_class, now, 0))
        rows.append(self._row(site, "backup", site.id, "unknown", "NotImplemented", now, 0))
        rows.append(self._row(site, "checksum", site.id, "unknown", "NotImplemented", now, 0))
        return rows, partial

    def _call_uptime(self, url: str, *, timeout: float) -> tuple[ProbeOutcome, bool, int]:
        started = time.perf_counter()
        try:
            outcome = self.uptime.probe(url, timeout=timeout)
            return outcome, False, self._duration_ms(started)
        except Exception:
            return ProbeOutcome(result="fail", error_class="CheckFailed"), True, self._duration_ms(started)

    def _call_rest(self, site: Site, *, credential_present: bool) -> tuple[ProbeOutcome, bool, int]:
        started = time.perf_counter()
        try:
            outcome = self.rest.probe(
                site, credential_present=credential_present, timeout=site.timeout_seconds
            )
            return outcome, False, self._duration_ms(started)
        except Exception:
            return ProbeOutcome(result="fail", error_class="CheckFailed"), True, self._duration_ms(started)

    def _duration_ms(self, started: float) -> int:
        return max(0, int((time.perf_counter() - started) * 1000))

    def _tls_dns(
        self, site: Site, url: str, outcome: ProbeOutcome, now: datetime, duration_ms: int
    ) -> list[dict[str, Any]]:
        if outcome.tls_days is None:
            tls = self._row(site, "tls", url, "unknown", "TlsUnavailable", now, duration_ms)
        elif outcome.tls_days <= 14:
            tls = self._row(site, "tls", url, "fail", "CertificateExpiring", now, duration_ms)
        else:
            tls = self._row(site, "tls", url, "ok", None, now, duration_ms)
        if outcome.dns_ok is True:
            dns = self._row(site, "dns", url, "ok", None, now, duration_ms)
        elif outcome.dns_ok is False:
            dns = self._row(site, "dns", url, "fail", "DnsFailed", now, duration_ms)
        else:
            dns = self._row(site, "dns", url, "unknown", "DnsUnavailable", now, duration_ms)
        return [tls, dns]

    def _row(
        self,
        site: Site,
        check: str,
        target: str,
        result: str,
        error_class: str | None,
        now: datetime,
        duration_ms: int,
    ) -> dict[str, Any]:
        if result not in {"ok", "fail", "unknown"}:
            result = "unknown"
            error_class = "CheckFailed"
        clean = None if result == "ok" else clean_error_class(error_class or "CheckFailed")
        return {
            "site_id": site.id,
            "environment": site.environment,
            "check": check,
            "target": target,
            "result": result,
            "tone": tone_for(result),
            "green": result == "ok",
            "duration_ms": duration_ms,
            "timeout_seconds": site.timeout_seconds,
            "error_class": clean,
            "checked_at": now.isoformat(),
        }

    def _apply(
        self,
        state: dict[str, Any],
        site: Site,
        records: list[dict[str, Any]],
        *,
        now: datetime,
        scheduled: bool,
    ) -> None:
        checks: dict[str, Any] = state.setdefault("checks", {})
        streaks: dict[str, int] = state.setdefault("streaks", {})
        incidents: list[dict[str, Any]] = state.setdefault("incidents", [])
        for row in records:
            key = f"{site.id}|{row['check']}|{row['target']}"
            checks[key] = row
            if row["check"] not in _SIGNAL_CHECKS:
                continue
            if row["result"] == "unknown":
                continue
            if row["result"] == "ok":
                streaks[key] = 0
                self._recover(incidents, site.id, row["check"], row["target"], now)
                continue
            streaks[key] = int(streaks.get(key) or 0) + 1
            if streaks[key] >= site.alert_confirmations:
                self._open_or_update(incidents, site, row, streaks[key], now)
        state.setdefault("last_run", {})[site.id] = now.isoformat()
        if not scheduled:
            state.setdefault("on_demand", {})[site.id] = now.isoformat()

    def _open_or_update(
        self,
        incidents: list[dict[str, Any]],
        site: Site,
        row: dict[str, Any],
        streak: int,
        now: datetime,
    ) -> None:
        for incident in incidents:
            if (
                incident.get("state") == "open"
                and incident.get("site_id") == site.id
                and incident.get("check") == row["check"]
                and incident.get("target") == row["target"]
            ):
                incident["failure_count"] = streak
                incident["updated_at"] = now.isoformat()
                incident["error_class"] = row.get("error_class")
                return
        incidents.append(
            {
                "id": str(uuid.uuid4()),
                "site_id": site.id,
                "environment": site.environment,
                "check": row["check"],
                "target": row["target"],
                "state": "open",
                "opened_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "recovered_at": None,
                "failure_count": streak,
                "owner": site.alert_owner,
                "error_class": row.get("error_class"),
            }
        )

    def _recover(
        self,
        incidents: list[dict[str, Any]],
        site_id: str,
        check: str,
        target: str,
        now: datetime,
    ) -> None:
        for incident in incidents:
            if (
                incident.get("state") == "open"
                and incident.get("site_id") == site_id
                and incident.get("check") == check
                and incident.get("target") == target
            ):
                incident["state"] = "recovered"
                incident["recovered_at"] = now.isoformat()
                incident["updated_at"] = now.isoformat()

    def _summary(self, site: Site, state: dict[str, Any]) -> dict[str, Any]:
        present = bind_credential(site).present
        body = site.public_dict(credential_present=present)
        checks = self._checks_for(state, site.id)
        catalog = self._catalog_backup_view(site, state)
        backup = catalog or next((row for row in checks if row["check"] == "backup"), None)
        if backup is None:
            backup = {
                "check": "backup",
                "result": "unknown",
                "tone": "attention",
                "green": False,
                "error_class": "NotRun",
            }
        incidents = [row for row in self._incidents_for(state, site.id) if row.get("state") == "open"]
        last = state.get("last_run", {}).get(site.id)
        required = [row for row in checks if row["check"] in {"http_uptime", "wp_rest", "backup", "checksum"}]
        healthy = bool(required) and all(row.get("result") == "ok" for row in required)
        body.update(
            {
                "healthy": healthy,
                "down": any(row.get("check") == "http_uptime" for row in incidents),
                "open_incidents": len(incidents),
                "last_check": last,
                "backup": {
                    "result": backup["result"],
                    "tone": "attention" if backup["result"] != "ok" else backup["tone"],
                    "green": False,
                    "error_class": backup.get("error_class"),
                    "offsite": False,
                    "encryption": backup.get("encryption") or "unconfigured",
                    "provider": backup.get("provider") or "mock",
                },
                "live_calls": False,
            }
        )
        return body

    def _backups_for(self, state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        now = self._now()
        rows = [
            self._public_backup(row, now)
            for row in state.get("backups", [])
            if isinstance(row, dict) and row.get("site_id") == site_id
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows

    def _drills_for(self, state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        rows = [
            self._public_drill(row)
            for row in state.get("drills", [])
            if isinstance(row, dict)
            and (row.get("source_site_id") == site_id or row.get("target_site_id") == site_id)
        ]
        rows.sort(key=lambda row: str(row.get("finished_at") or ""), reverse=True)
        return rows

    def _staging_targets(self, sites: tuple[Site, ...] | list[Site]) -> list[dict[str, str]]:
        return [
            {"id": site.id, "display_name": site.display_name}
            for site in sites
            if site.environment == "staging"
        ]

    def _catalog_backup_view(self, site: Site, state: dict[str, Any]) -> dict[str, Any] | None:
        rows = self._backups_for(state, site.id)
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

    def _overlay_backup_check(
        self, checks: list[dict[str, Any]], site: Site, state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        view = self._catalog_backup_view(site, state)
        if view is None:
            return checks
        latest = self._backups_for(state, site.id)[0]
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

    def _public_backup(self, record: dict[str, Any], now: datetime) -> dict[str, Any]:
        completion = record.get("completion")
        verified_at = record.get("verified_at")
        if completion == "verified" and verified_at:
            verified = datetime.fromisoformat(str(verified_at))
            if verified.tzinfo is None:
                verified = verified.replace(tzinfo=UTC)
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

    def _public_drill(self, record: dict[str, Any]) -> dict[str, Any]:
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

    def _recent_drill(
        self,
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
            finished = datetime.fromisoformat(str(row.get("finished_at")))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=UTC)
            if now - finished < timedelta(seconds=window_seconds):
                return row
        return None

    def _checks_for(self, state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        rows = []
        for row in state.get("checks", {}).values():
            if isinstance(row, dict) and row.get("site_id") == site_id:
                rows.append(row)
        return rows

    def _incidents_for(self, state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        return [
            row
            for row in state.get("incidents", [])
            if isinstance(row, dict) and row.get("site_id") == site_id
        ]

    def _is_duplicate(self, state: dict[str, Any], site: Site, now: datetime) -> bool:
        raw = state.get("on_demand", {}).get(site.id)
        if not raw:
            return False
        previous = datetime.fromisoformat(str(raw))
        return now - previous < timedelta(seconds=site.dedupe_window_seconds)

    def _safe_state(self) -> dict[str, Any] | WpOpsResult:
        try:
            return self.store.load()
        except ValueError:
            return WpOpsResult(
                ok=False,
                message="Check state is unreadable",
                data={"error_class": "StateUnreadable", "live_calls": False},
            )

    def _ssh_site(self, site_id: str) -> tuple[Site | None, Site | None, WpOpsResult | None]:
        try:
            registry_sites = load_sites(
                self.registry_path, data_dir=self.data_dir, apply_overlay=False
            )
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return None, None, self._config_error(str(exc))
        registry_site = site_by_id(registry_sites, site_id)
        site = site_by_id(sites, site_id)
        if registry_site is None or site is None:
            return None, None, WpOpsResult(
                ok=False,
                message="Unknown site",
                data={"error_class": "UnknownSite", "site_id": site_id, "live_calls": False},
            )
        return registry_site, site, None

    def _ssh_admin(self, actor: str) -> bool:
        try:
            return resolve_actor(actor).has(Permission.WP_OPS_ADMIN)
        except AccessDenied:
            return False

    def _audit_ssh(
        self,
        actor: str,
        site_id: str,
        *,
        success: bool,
        changed: list[str],
        identity_present: bool,
    ) -> None:
        self._audit_raw(
            actor,
            action="wp_ops_ssh_settings",
            target_id=site_id,
            success=success,
            details={
                "changed": changed,
                "identity_present": identity_present,
                "live_calls": False,
            },
        )

    def _file_editor(self):
        from ecom_ops.wp_ops.files import FileEditor

        def factory(site: Site):
            if self.files_factory is not None:
                return self.files_factory(site)
            from ecom_ops.integrations.wp_files_ssh import build_file_transport

            return build_file_transport(site, self.data_dir)

        return FileEditor(
            registry_path=self.registry_path,
            store=self.store,
            clock=self.clock,
            factory=factory,
            audit=self._audit_raw,
            need=self._need,
            lock=_site_lock,
        )

    def _need(
        self,
        actor: str,
        permission: Permission,
        *,
        site: Site | None = None,
        action: str = "wp_ops_read",
    ) -> WpOpsResult | None:
        try:
            actor_obj = resolve_actor(actor)
            require_permission(actor_obj, permission)
        except AccessDenied as exc:
            self._audit_raw(
                actor,
                action=action,
                target_id=site.id if site else None,
                success=False,
                details={"error_class": "AccessDenied", "permission": permission.value, "executed": False},
            )
            return WpOpsResult(
                ok=False,
                message=str(exc),
                data={"error_class": "AccessDenied", "executed": False, "live_calls": False},
            )
        return None

    def _audit(
        self,
        actor: str,
        site: Site,
        *,
        success: bool,
        error_class: str | None,
        permission: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "environment": site.environment,
            "permission": permission,
            "operation": "health_check",
            "plan_id": None,
            "error_class": error_class,
        }
        if extra:
            details.update(extra)
        self._audit_raw(actor, action="wp_ops_check", target_id=site.id, success=success, details=details)

    def _audit_raw(
        self,
        actor: str,
        *,
        action: str,
        target_id: str | None,
        success: bool,
        details: dict[str, Any],
    ) -> None:
        log_action(
            actor=actor,
            action=action,
            target="wp_sites",
            target_id=target_id,
            success=success,
            details=details,
        )

    def _config_error(self, message: str) -> WpOpsResult:
        return WpOpsResult(
            ok=False,
            message=message,
            data={"error_class": "ConfigInvalid", "live_calls": False},
        )

    def _now(self) -> datetime:
        current = self.clock()
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current
