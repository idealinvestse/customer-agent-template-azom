"""Unified RBAC, site resolution, non-blocking locking, and fail-closed audit policy for WP ops."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.locks import site_lock
from ecom_ops.wp_ops.models import Site, WpOpsResult, files_root_narrow
from ecom_ops.wp_ops.registry import load_sites, site_by_id

AccessMode = Literal["read", "operate", "write", "file_read", "file_write"]


@dataclass(frozen=True)
class WpOpsPolicy:
    """Shared authorization, site lookup, locking, and audit gate for WP operations."""

    registry_path: Path | None = None
    data_dir: Path | None = None
    audit_fn: Callable[..., Any] = log_action
    lock_fn: Callable[[str], threading.Lock] = site_lock

    @staticmethod
    def write_permission_for(site: Site) -> Permission:
        """Production writes require ``WP_OPS_ADMIN``; staging writes require ``WP_OPS_OPERATE``."""
        if site.environment == "production":
            return Permission.WP_OPS_ADMIN
        return Permission.WP_OPS_OPERATE

    def audit(
        self,
        actor: str,
        *,
        action: str,
        target_id: str | None,
        success: bool,
        details: dict[str, Any],
        target: str = "wp_sites",
        fail_closed: bool = True,
    ) -> WpOpsResult | None:
        """Record one audit entry; returns ``AuditUnavailable`` on failure when ``fail_closed``."""
        try:
            self.audit_fn(
                actor=actor,
                action=action,
                target=target,
                target_id=target_id,
                success=success,
                details=details,
            )
        except Exception:
            if not fail_closed:
                raise
            return WpOpsResult(
                ok=False,
                message="Audit log is unavailable.",
                data={"error_class": "AuditUnavailable", "executed": False, "live_calls": False},
            )
        return None

    def require(
        self,
        actor: str,
        *permissions: Permission,
        site: Site | None = None,
        action: str = "wp_ops_read",
    ) -> WpOpsResult | None:
        """Verify all ``permissions`` for ``actor``, auditing denials fail-closed."""
        for permission in permissions:
            try:
                actor_obj = resolve_actor(actor)
                require_permission(actor_obj, permission)
            except AccessDenied as exc:
                audit_err = self.audit(
                    actor,
                    action=action,
                    target_id=site.id if site else None,
                    success=False,
                    details={
                        "error_class": "AccessDenied",
                        "permission": permission.value,
                        "executed": False,
                    },
                )
                if audit_err is not None:
                    return audit_err
                return WpOpsResult(
                    ok=False,
                    message=str(exc),
                    data={"error_class": "AccessDenied", "executed": False, "live_calls": False},
                )
        return None

    def load_all_sites(self) -> tuple[tuple[Site, ...], WpOpsResult | None]:
        """Load the site registry, converting schema errors to ``ConfigInvalid``."""
        try:
            return load_sites(self.registry_path, data_dir=self.data_dir), None
        except ValueError as exc:
            return (), WpOpsResult(
                ok=False,
                message=str(exc),
                data={"error_class": "ConfigInvalid", "live_calls": False},
            )

    def resolve_site(
        self,
        site_id: str,
        *,
        actor: str,
        access: AccessMode = "read",
        action: str = "wp_ops_read",
        require_files_root: bool = False,
    ) -> tuple[Site | None, WpOpsResult | None]:
        """Resolve ``site_id``, enforce environment-scoped RBAC, and optionally validate ``files_root``."""
        sites, config_err = self.load_all_sites()
        if config_err is not None:
            return None, config_err
        site = site_by_id(sites, site_id)
        if site is None:
            data: dict[str, Any] = {
                "error_class": "UnknownSite",
                "site_id": site_id,
                "live_calls": False,
            }
            if access in {"write", "file_read", "file_write"}:
                data["executed"] = False
            return None, WpOpsResult(ok=False, message="Unknown site", data=data)

        perms = self._permissions_for(site, access)
        denied = self.require(actor, *perms, site=site, action=action)
        if denied is not None:
            return None, denied

        if require_files_root or access in {"file_read", "file_write"}:
            if not site.files_root:
                return None, WpOpsResult(
                    ok=False,
                    message="The site has no files root.",
                    data={
                        "error_class": "FileRootUnconfigured",
                        "executed": False,
                        "ssh": False,
                        "live_calls": False,
                    },
                )
            if not files_root_narrow(site.files_root):
                return None, WpOpsResult(
                    ok=False,
                    message="The files root is too broad.",
                    data={
                        "error_class": "PathRejected",
                        "executed": False,
                        "ssh": False,
                        "live_calls": False,
                    },
                )
        return site, None

    @contextmanager
    def try_site_lock(
        self,
        site: Site,
        *,
        actor: str,
        action: str,
        operation: str | None = None,
        permission: Permission | None = None,
        busy_message: str = "Site check already running",
    ) -> Iterator[WpOpsResult | None]:
        """Acquire the per-site lock without blocking, auditing ``SiteBusy`` on contention."""
        lock = self.lock_fn(site.id)
        if not lock.acquire(blocking=False):
            audit_err = self.audit(
                actor,
                action=action,
                target_id=site.id,
                success=False,
                details={
                    "environment": site.environment,
                    "permission": (permission or self.write_permission_for(site)).value,
                    "operation": operation or action,
                    "plan_id": None,
                    "error_class": "SiteBusy",
                    "executed": False,
                },
            )
            if audit_err is not None:
                yield audit_err
                return
            yield WpOpsResult(
                ok=False,
                message=busy_message,
                data={
                    "error_class": "SiteBusy",
                    "site_id": site.id,
                    "executed": False,
                    "live_calls": False,
                },
            )
            return
        try:
            yield None
        finally:
            lock.release()

    def _permissions_for(self, site: Site, access: AccessMode) -> tuple[Permission, ...]:
        if access in {"read", "file_read"}:
            return (Permission.WP_OPS_READ,)
        if access == "file_write":
            if site.environment == "production":
                return (Permission.WP_OPS_ADMIN, Permission.CODE_EDIT)
            return (Permission.WP_OPS_OPERATE,)
        return (self.write_permission_for(site),)
