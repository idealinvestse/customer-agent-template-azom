"""WordPress site health, local mock backups, and typed theme or plugin edits.

Slim facade delegating health probes to ``HealthChecker``, backup manifests and
staging restore drills to ``BackupService``, RBAC/locking/audit to
``WpOpsPolicy``, and file/review workflows to ``FileEditor``, ``FileChangeOps``,
and ``ReviewPlans``. Live SSH stays off unless mock mode is off and the owner
sets ``AZOM_WP_FILES_SSH=1``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, resolve_actor
from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.backup import BackupProvider, BackupService, MockBackup
from ecom_ops.wp_ops.config import data_dir_path
from ecom_ops.wp_ops.connectors import MockRest, MockUptime, RestConnector, UptimeConnector
from ecom_ops.wp_ops.file_changes import FileChangeOps
from ecom_ops.wp_ops.files import FileEditor
from ecom_ops.wp_ops.health import HealthChecker
from ecom_ops.wp_ops.locks import hold_site as _hold_site
from ecom_ops.wp_ops.locks import site_lock as _site_lock
from ecom_ops.wp_ops.models import Site, WpOpsResult
from ecom_ops.wp_ops.policy import WpOpsPolicy
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.ssh_settings import (
    SettingsRejected,
    describe_deploy_ssh,
    describe_ssh,
    save_deploy_ssh,
    save_ssh,
)
from ecom_ops.wp_ops.store import WpOpsStore

__all__ = ["WpOpsResult", "WpOpsService", "_site_lock"]


class WpOpsService:
    """Registry, mock probes, backups, SSH settings, and file/workspace facade.

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
        backup: BackupProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        files_factory: Callable[[Site], Any] | None = None,
        bridge_factory: Callable[[Site], Any] | None = None,
        deploy_transport_factory: Callable[[Site], Any] | None = None,
    ) -> None:
        self.registry_path = registry_path
        base = data_dir_path(data_dir)
        self.data_dir = base
        self.files_factory = files_factory
        self.bridge_factory = bridge_factory
        self.deploy_transport_factory = deploy_transport_factory
        self.store = WpOpsStore(base / "wp_ops_state.json")
        self.uptime = uptime or MockUptime()
        self.rest = rest or MockRest()
        self.backup = backup or MockBackup()
        self.clock = clock or (lambda: datetime.now(UTC))

    def _policy(self) -> WpOpsPolicy:
        return WpOpsPolicy(
            registry_path=self.registry_path,
            data_dir=self.data_dir,
            audit_fn=log_action,
            lock_fn=_site_lock,
        )

    def _backup_service(self) -> BackupService:
        return BackupService(
            store=self.store,
            policy=self._policy(),
            provider=self.backup,
            clock=self.clock,
        )

    def _health_checker(self) -> HealthChecker:
        return HealthChecker(
            store=self.store,
            policy=self._policy(),
            backups=self._backup_service(),
            uptime=self.uptime,
            rest=self.rest,
            clock=self.clock,
        )

    def list_sites(self, *, actor: str) -> WpOpsResult:
        policy = self._policy()
        denied = policy.require(actor, Permission.WP_OPS_READ)
        if denied is not None:
            return denied
        sites, config_err = policy.load_all_sites()
        if config_err is not None:
            return config_err
        health = self._health_checker()
        state = health.safe_state()
        if isinstance(state, WpOpsResult):
            return state
        rows = [health.summary(site, state) for site in sites]
        return WpOpsResult(ok=True, message="sites", data={"sites": rows, "live_calls": False})

    def show_site(self, site_id: str, *, actor: str) -> WpOpsResult:
        policy = self._policy()
        denied = policy.require(actor, Permission.WP_OPS_READ)
        if denied is not None:
            return denied
        sites, config_err = policy.load_all_sites()
        if config_err is not None:
            return config_err
        site = site_by_id(sites, site_id)
        if site is None:
            return WpOpsResult(
                ok=False,
                message="Unknown site",
                data={"error_class": "UnknownSite", "site_id": site_id, "live_calls": False},
            )
        backups = self._backup_service()
        health = HealthChecker(
            store=self.store,
            policy=policy,
            backups=backups,
            uptime=self.uptime,
            rest=self.rest,
            clock=self.clock,
        )
        state = health.safe_state()
        if isinstance(state, WpOpsResult):
            return state
        return WpOpsResult(
            ok=True,
            message="site",
            data={
                "site": health.summary(site, state),
                "checks": backups.overlay_backup_check(
                    health.checks_for(state, site.id), site, state
                ),
                "incidents": health.incidents_for(state, site.id),
                "backups": backups.backups_for(state, site.id),
                "drills": self._drills_for(state, site.id),
                "staging_targets": health.staging_targets(sites),
                "live_calls": False,
            },
        )

    def _drills_for(self, state: dict[str, Any], site_id: str) -> list[dict[str, Any]]:
        """Preserved hook (patched by dashboard regression tests)."""
        return self._backup_service().drills_for(state, site_id)

    def run_checks(self, site_id: str, *, actor: str, scheduled: bool = False) -> WpOpsResult:
        return self._health_checker().run_checks(site_id, actor=actor, scheduled=scheduled)

    def run_due(self, *, actor: str) -> WpOpsResult:
        return self._health_checker().run_due(actor=actor)

    def restore(self, site_id: str, *, actor: str, change_ref: str = "") -> WpOpsResult:
        """Refuse every restore. The change reference is not stored or returned."""
        return self._backup_service().restore(site_id, actor=actor, change_ref=change_ref)

    def create_backup(self, site_id: str, *, actor: str) -> WpOpsResult:
        """Store one local mock manifest. Files and database are labels, not bytes."""
        return self._backup_service().create_backup(site_id, actor=actor)

    def list_backups(self, site_id: str, *, actor: str) -> WpOpsResult:
        return self._backup_service().list_backups(site_id, actor=actor)

    def restore_drill(self, backup_id: str, *, target_site_id: str, actor: str) -> WpOpsResult:
        """Record a staging drill of a verified manifest. Production targets are refused."""
        return self._backup_service().restore_drill(backup_id, target_site_id=target_site_id, actor=actor)

    @contextmanager
    def hold_site(self, site_id: str) -> Iterator[None]:
        with _hold_site(site_id):
            yield

    def _file_editor(self) -> FileEditor:
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

    def _file_changes(self) -> FileChangeOps:
        return FileChangeOps(self._file_editor())

    def _review_plans(self) -> ReviewPlans:
        return ReviewPlans(
            data_dir=self.data_dir,
            registry_path=self.registry_path,
            clock=self.clock,
            transport_factory=self.files_factory,
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
            site_id, kind, slug, relative, actor=actor, admin_only=admin_only
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
        return self._file_editor().propose(site_id, kind, slug, relative, new_text, actor=actor)

    def apply_file(
        self, site_id: str, plan_id: str, new_text: str, *, actor: str
    ) -> WpOpsResult:
        return self._file_editor().apply(site_id, plan_id, new_text, actor=actor)

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

    def list_workspaces(
        self,
        site_id: str | None = None,
        *,
        actor: str,
        status: str | None = None,
        include_terminal: bool = False,
    ) -> WpOpsResult:
        return self._review_plans().list_plans(
            site_id, actor=actor, status=status, include_terminal=include_terminal
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
            site_id, plan_id, rel_path, actor=actor, start_line=start_line, end_line=end_line
        )

    def workspace_diff(
        self, site_id: str, plan_id: str, rel_path: str | None = None, *, actor: str
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
            site_id, plan_id, template, actor=actor, component_slug=component_slug
        )

    def workspace_theme_json(
        self, site_id: str, plan_id: str, operations: list[dict[str, Any]], *, actor: str
    ) -> WpOpsResult:
        return self._review_plans().workspace_theme_json(site_id, plan_id, operations, actor=actor)

    def push_page_draft(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        base_sha256: str,
        work_sha256: str,
        client: Any = None,
    ) -> dict[str, Any]:
        """Push a verified ``work/page_draft.json`` to WordPress as ``status='draft'`` (Oscar-only HITL)."""
        return self._review_plans().push_page_draft(
            site_id,
            plan_id,
            actor=actor,
            base_sha256=base_sha256,
            work_sha256=work_sha256,
            client=client,
        )

    def ssh_settings(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self._policy().require(actor, Permission.WP_OPS_READ, action="wp_ops_ssh_settings")
        if denied is not None:
            return denied
        registry_site, site, failed = self._ssh_site(site_id)
        if failed is not None:
            return failed
        assert registry_site is not None and site is not None
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
        denied = self._policy().require(actor, Permission.WP_OPS_ADMIN, action="wp_ops_ssh_settings")
        if denied is not None:
            return denied
        registry_site, _site, failed = self._ssh_site(site_id)
        if failed is not None:
            return failed
        assert registry_site is not None
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
        return WpOpsResult(
            ok=True,
            message="SSH-inställningar sparade",
            data={"changed": list(saved["changed"]), "live_calls": False},
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

    @staticmethod
    def _ssh_admin(actor: str) -> bool:
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

    def _need(
        self,
        actor: str,
        permission: Permission,
        *,
        site: Site | None = None,
        action: str = "wp_ops_read",
    ) -> WpOpsResult | None:
        return self._policy().require(actor, permission, site=site, action=action)

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

    @staticmethod
    def _config_error(message: str) -> WpOpsResult:
        return WpOpsResult(
            ok=False,
            message=message,
            data={"error_class": "ConfigInvalid", "live_calls": False},
        )

    # --- Change ledger, rollback, deploy SSH & bridge facades ------------ #

    def list_changes(
        self,
        site_id: str | None = None,
        *,
        actor: str,
        category: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> WpOpsResult:
        denied = self._policy().require(actor, Permission.WP_OPS_READ, action="wp_ops_changes_list")
        if denied is not None:
            return denied
        from ecom_ops.wp_ops.change_ledger import ChangeLedger

        rows = ChangeLedger(data_dir=self.data_dir).list_changes(
            site_id, category=category, status=status, limit=limit
        )
        return WpOpsResult(ok=True, message="changes", data={"changes": rows, "live_calls": False})

    def preview_change_rollback(
        self, site_id: str, change_id: str, *, actor: str
    ) -> dict[str, Any]:
        from ecom_ops.wp_ops.rollback_service import ChangeRollbackService

        return ChangeRollbackService(
            data_dir=self.data_dir, registry_path=self.registry_path
        ).preview_rollback(site_id, change_id, actor=actor)

    def execute_change_rollback(
        self,
        site_id: str,
        change_id: str,
        *,
        actor: str,
        expected_before_sha256: str,
        dry_run: bool = False,
    ) -> WpOpsResult:
        from ecom_ops.wp_ops.rollback_service import ChangeRollbackService

        return ChangeRollbackService(
            data_dir=self.data_dir, registry_path=self.registry_path
        ).execute_rollback(
            site_id,
            change_id,
            actor=actor,
            expected_before_sha256=expected_before_sha256,
            dry_run=dry_run,
        )

    def deploy_ssh_settings(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self._policy().require(actor, Permission.WP_OPS_READ, action="wp_ops_ssh_settings")
        if denied is not None:
            return denied
        _reg, site, failed = self._ssh_site(site_id)
        if failed is not None or site is None:
            return failed or self._config_error("Unknown site")
        view = describe_deploy_ssh(site, self.data_dir, include_paths=self._ssh_admin(actor))
        return WpOpsResult(ok=True, message="deploy ssh settings", data={"deploy_ssh": view, "live_calls": False})

    def save_deploy_ssh_settings(
        self,
        site_id: str,
        *,
        actor: str,
        deploy_ssh_user: str,
        files_root: str = "",
        ssh_host: str = "",
        ssh_port: str = "",
        host_public_key: str = "",
        private_key: str = "",
        clear_key: bool = False,
    ) -> WpOpsResult:
        denied = self._policy().require(actor, Permission.WP_OPS_ADMIN, action="wp_ops_ssh_settings")
        if denied is not None:
            return denied
        registry_site, _site, failed = self._ssh_site(site_id)
        if failed is not None or registry_site is None:
            return failed or self._config_error("Unknown site")
        try:
            saved = save_deploy_ssh(
                self.data_dir,
                registry_site,
                deploy_ssh_user=deploy_ssh_user,
                files_root=files_root,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                host_public_key=host_public_key,
                private_key=private_key,
                clear_key=clear_key,
            )
        except SettingsRejected as exc:
            return WpOpsResult(
                ok=False,
                message=str(exc),
                data={"error_class": "ConfigInvalid", "live_calls": False},
            )
        self._audit_raw(
            actor,
            action="wp_ops_deploy_ssh_settings",
            target_id=site_id,
            success=True,
            details={"deploy_key_stored": bool(saved.get("deploy_key_stored")), "live_calls": False},
        )
        return WpOpsResult(
            ok=True,
            message="Deploy-SSH-inställningar sparade",
            data={**saved, "live_calls": False},
        )

    def _bridge_client_for_site(self, site: Site) -> Any:
        if self.bridge_factory is not None:
            return self.bridge_factory(site)
        from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site

        return bridge_client_for_site(site)

    def bridge_health(self, site_id: str, *, actor: str) -> WpOpsResult:
        denied = self._policy().require(actor, Permission.WP_OPS_READ, action="wp_ops_bridge_health")
        if denied is not None:
            return denied
        sites, config_err = self._policy().load_all_sites()
        if config_err is not None:
            return config_err
        site = site_by_id(sites, site_id)
        if site is None:
            return WpOpsResult(ok=False, message="Unknown site", data={"error_class": "UnknownSite"})

        client = self._bridge_client_for_site(site)
        if client is None:
            return WpOpsResult(
                ok=False,
                message="Azom Ops Bridge är inte aktiverad eller saknar hemlighet.",
                data={"error_class": "BridgeUnconfigured", "live_calls": False},
            )
        try:
            health = client.health()
        except Exception as exc:
            return WpOpsResult(
                ok=False,
                message=f"Bridge-hälsokontroll misslyckades: {str(exc)[:160]}",
                data={"error_class": "BridgeError", "live_calls": client.is_live},
            )
        return WpOpsResult(
            ok=True,
            message="Bridge-hälsokontroll klar",
            data={"health": health, "site_id": site.id, "live_calls": client.is_live},
        )

    def bridge_translations(
        self, site_id: str, *, post_id: int | None = None, actor: str
    ) -> WpOpsResult:
        denied = self._policy().require(
            actor, Permission.WP_OPS_READ, action="wp_ops_bridge_translations"
        )
        if denied is not None:
            return denied
        sites, config_err = self._policy().load_all_sites()
        if config_err is not None:
            return config_err
        site = site_by_id(sites, site_id)
        if site is None:
            return WpOpsResult(ok=False, message="Unknown site", data={"error_class": "UnknownSite"})

        client = self._bridge_client_for_site(site)
        if client is None:
            return WpOpsResult(
                ok=False,
                message="Azom Ops Bridge är inte aktiverad eller saknar hemlighet.",
                data={"error_class": "BridgeUnconfigured", "live_calls": False},
            )
        try:
            payload = client.translations(post_id=post_id)
        except Exception as exc:
            return WpOpsResult(
                ok=False,
                message=f"Översättningsläsning misslyckades: {str(exc)[:160]}",
                data={"error_class": "BridgeError", "live_calls": client.is_live},
            )
        return WpOpsResult(
            ok=True,
            message="Översättningar hämtade",
            data={"translations": payload, "site_id": site.id, "live_calls": client.is_live},
        )

    def bridge_cache_flush(self, site_id: str, *, actor: str) -> WpOpsResult:
        if env_flag("AZOM_WP_DEPLOY_KILL"):
            return WpOpsResult(
                ok=False,
                message="Nödstopp (AZOM_WP_DEPLOY_KILL) är aktivt.",
                data={"error_class": "DeployKillSwitchActive", "live_calls": False},
            )
        denied = self._policy().require(actor, Permission.WP_OPS_ADMIN, action="wp_ops_bridge_cache")
        if denied is not None:
            return denied
        sites, config_err = self._policy().load_all_sites()
        if config_err is not None:
            return config_err
        site = site_by_id(sites, site_id)
        if site is None:
            return WpOpsResult(ok=False, message="Unknown site", data={"error_class": "UnknownSite"})
        from ecom_ops.wp_ops.change_ledger import safe_record_change

        client = self._bridge_client_for_site(site)
        if client is None:
            return WpOpsResult(
                ok=False,
                message="Azom Ops Bridge är inte aktiverad eller saknar hemlighet.",
                data={"error_class": "BridgeUnconfigured", "live_calls": False},
            )
        try:
            res = client.flush_cache()
        except Exception as exc:
            return WpOpsResult(
                ok=False,
                message=f"Cache-rensning misslyckades: {str(exc)[:160]}",
                data={"error_class": "BridgeError", "live_calls": client.is_live},
            )
        safe_record_change(
            data_dir=self.data_dir,
            site_id=site.id,
            environment=site.environment,
            category="cache_flush",
            transport="bridge",
            actor=actor,
            summary=f"Cache rensad ({', '.join(res.get('flushed') or [])})",
            target_ref=f"cache:{site.id}",
            before_data={"flushed": False},
            after_data=res,
        )
        return WpOpsResult(
            ok=True,
            message="Cache rensad via Azom Ops Bridge.",
            data={"result": res, "live_calls": client.is_live},
        )

    def bridge_maintenance(
        self, site_id: str, *, enabled: bool, duration_seconds: int = 300, actor: str
    ) -> WpOpsResult:
        if env_flag("AZOM_WP_DEPLOY_KILL"):
            return WpOpsResult(
                ok=False,
                message="Nödstopp (AZOM_WP_DEPLOY_KILL) är aktivt.",
                data={"error_class": "DeployKillSwitchActive", "live_calls": False},
            )
        denied = self._policy().require(actor, Permission.WP_OPS_ADMIN, action="wp_ops_bridge_maint")
        if denied is not None:
            return denied
        sites, config_err = self._policy().load_all_sites()
        if config_err is not None:
            return config_err
        site = site_by_id(sites, site_id)
        if site is None:
            return WpOpsResult(ok=False, message="Unknown site", data={"error_class": "UnknownSite"})
        from ecom_ops.wp_ops.change_ledger import safe_record_change

        client = self._bridge_client_for_site(site)
        if client is None:
            return WpOpsResult(
                ok=False,
                message="Azom Ops Bridge är inte aktiverad eller saknar hemlighet.",
                data={"error_class": "BridgeUnconfigured", "live_calls": False},
            )
        try:
            res = client.set_maintenance(enabled=enabled, duration_seconds=duration_seconds)
        except Exception as exc:
            return WpOpsResult(
                ok=False,
                message=f"Underhållsläge misslyckades: {str(exc)[:160]}",
                data={"error_class": "BridgeError", "live_calls": client.is_live},
            )
        safe_record_change(
            data_dir=self.data_dir,
            site_id=site.id,
            environment=site.environment,
            category="maintenance",
            transport="bridge",
            actor=actor,
            summary=f"Underhållsläge {'PÅ' if enabled else 'AV'}",
            target_ref=f"maintenance:{site.id}",
            before_data={"enabled": not enabled},
            after_data=res,
        )
        return WpOpsResult(
            ok=True,
            message=f"Underhållsläge {'aktiverat' if enabled else 'avaktiverat'}.",
            data={"result": res, "live_calls": client.is_live},
        )

    # --- Plugin lifecycle, policy, auto-updates, diagnostics & recovery -- #

    def _plugin_service(self) -> Any:
        from ecom_ops.wp_ops.plugin_service import PluginOpsService

        return PluginOpsService(
            data_dir=self.data_dir,
            registry_path=self.registry_path,
            clock=self.clock,
            bridge_factory=self.bridge_factory,
            deploy_transport_factory=self.deploy_transport_factory,
            files_factory=self.files_factory,
        )

    def plugin_inventory(
        self,
        site_id: str,
        *,
        actor: str,
        include_updates: bool = True,
        include_wporg: bool = False,
    ) -> WpOpsResult:
        return self._plugin_service().plugin_inventory(
            site_id,
            actor=actor,
            include_updates=include_updates,
            include_wporg=include_wporg,
        )

    def mu_plugins_and_dropins(self, site_id: str, *, actor: str) -> WpOpsResult:
        return self._plugin_service().mu_plugins_and_dropins(site_id, actor=actor)

    def plugin_diagnostics(
        self,
        site_id: str,
        *,
        actor: str,
        hooks: list[str] | None = None,
        log_lines: int = 80,
    ) -> WpOpsResult:
        return self._plugin_service().plugin_diagnostics(
            site_id, actor=actor, hooks=hooks, log_lines=log_lines
        )

    def plan_plugin_updates(
        self,
        site_id: str,
        *,
        actor: str,
        selection_mode: str = "policy",
        slugs: list[str] | None = None,
        policy: str = "non_critical_only",
        exclude_slugs: list[str] | None = None,
        include_critical: bool = False,
        canary: bool = True,
    ) -> WpOpsResult:
        return self._plugin_service().plan_plugin_updates(
            site_id,
            actor=actor,
            selection_mode=selection_mode,
            slugs=slugs,
            policy=policy,
            exclude_slugs=exclude_slugs,
            include_critical=include_critical,
            canary=canary,
        )

    def execute_plugin_update_plan(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        expected_sha256: str,
        allow_critical: bool = False,
    ) -> WpOpsResult:
        return self._plugin_service().execute_plugin_update_plan(
            site_id,
            plan_id,
            actor=actor,
            expected_sha256=expected_sha256,
            allow_critical=allow_critical,
        )

    def list_plugin_update_plans(self, site_id: str, *, actor: str) -> WpOpsResult:
        return self._plugin_service().list_plugin_update_plans(site_id, actor=actor)

    def get_autoupdate_policy(self, site_id: str, *, actor: str) -> WpOpsResult:
        return self._plugin_service().get_autoupdate_policy(site_id, actor=actor)

    def save_autoupdate_policy(
        self, site_id: str, *, actor: str, policy: dict[str, Any]
    ) -> WpOpsResult:
        return self._plugin_service().save_autoupdate_policy(site_id, actor=actor, policy=policy)

    def check_autoupdate_drift(self, site_id: str, *, actor: str) -> WpOpsResult:
        return self._plugin_service().check_autoupdate_drift(site_id, actor=actor)

    def plan_plugin_autoupdate(
        self,
        site_id: str,
        *,
        actor: str,
        preset: str = "non_critical_only",
        changes: list[dict[str, Any]] | None = None,
        allow_critical: bool = False,
    ) -> WpOpsResult:
        return self._plugin_service().plan_plugin_autoupdate(
            site_id,
            actor=actor,
            preset=preset,
            changes=changes,
            allow_critical=allow_critical,
        )

    def execute_plugin_autoupdate_plan(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        expected_sha256: str,
    ) -> WpOpsResult:
        return self._plugin_service().execute_plugin_autoupdate_plan(
            site_id, plan_id, actor=actor, expected_sha256=expected_sha256
        )

    def plan_plugin_recovery(
        self,
        site_id: str,
        *,
        actor: str,
        slug: str,
        action: str,
        change_id: str | None = None,
    ) -> WpOpsResult:
        return self._plugin_service().plan_plugin_recovery(
            site_id, actor=actor, slug=slug, action=action, change_id=change_id
        )

    def execute_plugin_recovery_plan(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        expected_sha256: str,
    ) -> WpOpsResult:
        return self._plugin_service().execute_plugin_recovery_plan(
            site_id, plan_id, actor=actor, expected_sha256=expected_sha256
        )

    def quarantine_plugin(
        self,
        site_id: str,
        slug: str,
        *,
        actor: str,
        kind: str = "plugin",
    ) -> WpOpsResult:
        return self._plugin_service().quarantine_plugin(site_id, slug, actor=actor, kind=kind)

    def unquarantine_plugin(
        self,
        site_id: str,
        slug: str,
        *,
        actor: str,
        quarantine_name: str = "",
        kind: str = "plugin",
    ) -> WpOpsResult:
        return self._plugin_service().unquarantine_plugin(
            site_id, slug, actor=actor, quarantine_name=quarantine_name, kind=kind
        )
