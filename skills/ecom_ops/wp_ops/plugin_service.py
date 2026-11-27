"""WordPress plugin lifecycle service: inventory, updates, auto-updates, diagnostics & recovery.

Enforces RBAC, kill switches (``AZOM_WP_DEPLOY_KILL`` and ``AZOM_WP_PLUGIN_MUTATE_KILL``),
fail-closed live env gates, immutable SHA-256 hash-pinned plans (12h TTL), Change Vault
snapshots, and strict ``azom-ops-bridge`` self-protection.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.change_ledger import ChangeLedger, valid_change_id
from ecom_ops.wp_ops.config import data_dir_path, is_mock_mode
from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.file_paths import (
    SEGMENT_RE,
    SLUG_RE,
    PathRejected,
    TransportError,
    plan_expired,
    sensitive_relative,
)
from ecom_ops.wp_ops.file_transport import LocalTree
from ecom_ops.wp_ops.locks import advisory_file_lock, hold_site
from ecom_ops.wp_ops.models import Site, WpOpsResult, fail_result, ok_result
from ecom_ops.wp_ops.plugin_policy import (
    AUTOUPDATE_PRESETS,
    classify_update_risk,
    default_autoupdate_policy,
    evaluate_autoupdate_drift,
    is_bridge_plugin,
    is_critical_plugin,
    normalize_plugin_slug,
    select_plugin_updates,
    validate_autoupdate_policy,
)
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.store import atomic_write_text
from ecom_ops.wp_ops.wporg_intel import fetch_component_intel

_PLAN_TTL = timedelta(hours=12)
_PLU_RE = re.compile(r"^plu-[a-f0-9]{12}$")
_PAU_RE = re.compile(r"^pau-[a-f0-9]{12}$")
_PLR_RE = re.compile(r"^plr-[a-f0-9]{12}$")
_RECOVERY_ACTIONS = frozenset(
    {"rollback_snapshot", "deactivate", "quarantine_sftp", "restore_from_quarantine"}
)
_QUARANTINE_MARKER = ".azom-quarantine-"
_QUARANTINE_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.azom-quarantine-[a-f0-9]{8}$"
)
_STORE_LOCK = threading.RLock()
WOO_FUNNEL_CANARY_PATHS: tuple[str, ...] = ("/", "/shop/", "/cart/", "/checkout/")
DEFAULT_CANARY_PATHS: tuple[str, ...] = ("/",)
_FUNNEL_PLUGIN_KEYWORDS: tuple[str, ...] = (
    "woo",
    "checkout",
    "cart",
    "klarna",
    "stripe",
    "vipps",
    "svea",
    "payment",
    "theme",
    "elementor",
    "divi",
    "bricks",
    "oxygen",
    "flatsome",
    "woodmart",
    "storefront",
    "astra",
    "kadence",
    "generatepress",
    "beaver-builder",
    "wpbakery",
    "blocks",
)


def canary_paths_for_plugin(item: dict[str, Any], *, is_woocommerce_site: bool = True) -> list[str]:
    """Select default funnel canary paths for a plugin update item."""
    slug = normalize_plugin_slug(str(item.get("slug") or item.get("plugin_file") or ""))
    critical = bool(item.get("is_critical")) or is_critical_plugin(slug)
    touches_funnel = critical or any(kw in slug for kw in _FUNNEL_PLUGIN_KEYWORDS)
    if touches_funnel and (is_woocommerce_site or critical or "woo" in slug):
        return list(WOO_FUNNEL_CANARY_PATHS)
    return list(DEFAULT_CANARY_PATHS)


def _canonical_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _kill_switch_block() -> WpOpsResult | None:
    if env_flag("AZOM_WP_DEPLOY_KILL"):
        return fail_result(
            "DeployKillSwitchActive",
            "Nödstopp (AZOM_WP_DEPLOY_KILL) är aktivt.",
            executed=False,
        )
    if env_flag("AZOM_WP_PLUGIN_MUTATE_KILL"):
        return fail_result(
            "PluginMutateKillSwitchActive",
            "Nödstopp för plugin-ändringar (AZOM_WP_PLUGIN_MUTATE_KILL) är aktivt.",
            executed=False,
        )
    return None


class PluginOpsService:
    """Lifecycle service for plugin inventory, canary updates, auto-updates, and recovery."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        registry_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        bridge_factory: Callable[[Site], Any] | None = None,
        deploy_transport_factory: Callable[[Site], Any] | None = None,
        files_factory: Callable[[Site], Any] | None = None,
    ) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.registry_path = registry_path
        self.clock = clock or (lambda: datetime.now(UTC))
        self.bridge_factory = bridge_factory
        self.deploy_transport_factory = deploy_transport_factory
        self.files_factory = files_factory
        self.ledger = ChangeLedger(data_dir=self.data_dir)

    def _now(self) -> datetime:
        cur = self.clock()
        return cur.replace(tzinfo=UTC) if cur.tzinfo is None else cur

    def _update_plans_path(self) -> Path:
        return self.data_dir / "wp_plugin_update_plans.json"

    def _autoupdate_plans_path(self) -> Path:
        return self.data_dir / "wp_plugin_autoupdate_plans.json"

    def _recovery_plans_path(self) -> Path:
        return self.data_dir / "wp_plugin_recovery_plans.json"

    def _policies_path(self) -> Path:
        return self.data_dir / "wp_plugin_policies.json"

    def _audit(
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
            target="wp_plugins",
            target_id=target_id,
            success=success,
            details=details,
        )

    def _require(
        self,
        actor: str,
        *perms: Permission,
        action: str,
        target_id: str | None = None,
    ) -> tuple[Any | None, WpOpsResult | None]:
        try:
            resolved = resolve_actor(actor)
            for perm in perms:
                require_permission(resolved, perm)
            return resolved, None
        except AccessDenied as exc:
            self._audit(
                actor,
                action=action,
                target_id=target_id,
                success=False,
                details={"error_class": "AccessDenied", "executed": False},
            )
            return None, fail_result("AccessDenied", str(exc), executed=False)

    def _resolve_site(self, site_id: str) -> tuple[Site | None, WpOpsResult | None]:
        try:
            sites = load_sites(self.registry_path, data_dir=self.data_dir)
        except ValueError as exc:
            return None, fail_result("ConfigInvalid", str(exc), executed=False)
        site = site_by_id(sites, site_id)
        if site is None:
            return None, fail_result(
                "UnknownSite",
                "Okänd webbplats.",
                site_id=site_id,
                executed=False,
            )
        return site, None

    def _bridge_client(self, site: Site) -> tuple[Any | None, WpOpsResult | None]:
        if self.bridge_factory is not None:
            client = self.bridge_factory(site)
        else:
            from ecom_ops.integrations.wp.bridge_client import bridge_client_for_site

            client = bridge_client_for_site(site)
        if client is None:
            return None, fail_result(
                "BridgeUnconfigured",
                "Azom Ops Bridge är inte aktiverad eller saknar hemlighet.",
                site_id=site.id,
                executed=False,
                live_calls=False,
            )
        return client, None

    def _deploy_transport(self, site: Site) -> Any | None:
        if self.deploy_transport_factory is not None:
            return self.deploy_transport_factory(site)
        if is_mock_mode():
            return None
        from ecom_ops.integrations.wp.sftp_deploy_transport import (
            SshDeployTransport,
            deploy_ssh_enabled,
        )

        if not deploy_ssh_enabled():
            return None
        return SshDeployTransport(site, self.data_dir)

    def _mock_local_tree(self, site: Site) -> LocalTree:
        if self.files_factory is not None:
            tr = self.files_factory(site)
            if isinstance(tr, LocalTree):
                return tr
        root = site.files_root or "/var/www/unconfigured"
        return LocalTree(root, self.data_dir / "wp_files_mock" / site.id)

    # --- JSON persistence helpers --------------------------------------- #

    def _load_json_dict(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _load_plans(self, path: Path) -> list[dict[str, Any]]:
        raw = self._load_json_dict(path)
        plans = raw.get("plans")
        if not isinstance(plans, list):
            return []
        return [dict(row) for row in plans if isinstance(row, dict)]

    def _save_plans(self, path: Path, plans: list[dict[str, Any]]) -> None:
        payload = json.dumps({"plans": plans}, ensure_ascii=False, indent=2)
        atomic_write_text(path, payload)

    def _append_plan(self, path: Path, plan: dict[str, Any]) -> None:
        with _STORE_LOCK, advisory_file_lock(path.with_suffix(".json.lock")):
            rows = self._load_plans(path)
            rows.append(dict(plan))
            self._save_plans(path, rows[-200:])

    def _update_plan(self, path: Path, updated: dict[str, Any]) -> None:
        pid = str(updated.get("plan_id") or "")
        with _STORE_LOCK, advisory_file_lock(path.with_suffix(".json.lock")):
            rows = self._load_plans(path)
            replaced = False
            for idx, row in enumerate(rows):
                if row.get("plan_id") == pid:
                    rows[idx] = dict(updated)
                    replaced = True
                    break
            if not replaced:
                rows.append(dict(updated))
            self._save_plans(path, rows[-200:])

    def _find_plan(self, path: Path, site_id: str, plan_id: str) -> dict[str, Any] | None:
        for row in self._load_plans(path):
            if row.get("plan_id") == plan_id and row.get("site_id") == site_id:
                return row
        return None

    # --- Inventory, MU-Plugins & Diagnostics ---------------------------- #

    def plugin_inventory(
        self,
        site_id: str,
        *,
        actor: str,
        include_updates: bool = True,
        include_wporg: bool = False,
    ) -> WpOpsResult:
        """Fetch full plugin, MU-plugin, and drop-in inventory from Bridge."""
        _actor, denied = self._require(
            actor, Permission.WP_OPS_READ, action="wp_plugin_inventory", target_id=site_id
        )
        if denied is not None:
            return denied
        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)
        client, bridge_err = self._bridge_client(site)
        if bridge_err is not None or client is None:
            return bridge_err or fail_result("BridgeUnconfigured", "Bridge ej konfigurerad.", executed=False)
        try:
            raw = client.plugin_inventory(include_updates=include_updates)
        except Exception as exc:
            return fail_result(
                "BridgeError",
                f"Kunde inte läsa plugin-inventering: {str(exc)[:160]}",
                site_id=site.id,
                executed=False,
                live_calls=bool(getattr(client, "is_live", False)),
            )

        plugins: list[dict[str, Any]] = []
        updates_count = 0
        critical_updates = 0
        active_count = 0
        autoupdate_count = 0
        for row in list(raw.get("plugins") or []):
            if not isinstance(row, dict):
                continue
            item = dict(row)
            slug = normalize_plugin_slug(str(item.get("slug") or item.get("plugin_file") or ""))
            item["slug"] = slug
            item["is_bridge"] = bool(item.get("is_bridge")) or is_bridge_plugin(slug)
            item["is_critical"] = bool(item.get("is_critical")) or is_critical_plugin(slug)
            if item.get("active"):
                active_count += 1
            if item.get("auto_update"):
                autoupdate_count += 1
            if item.get("update_available") and item.get("new_version"):
                updates_count += 1
                if item["is_critical"]:
                    critical_updates += 1
                item["risk_class"] = classify_update_risk(
                    str(item.get("version") or ""),
                    str(item.get("new_version") or ""),
                    security_advisory=bool(item.get("security_advisory")),
                )
            else:
                item["risk_class"] = "none"
            if include_wporg and slug and not item["is_bridge"] and len(plugins) < 50:
                try:
                    item["wporg"] = fetch_component_intel(
                        "plugin", slug, installed_version=str(item.get("version") or "")
                    )
                except ValueError:
                    item["wporg"] = None
            plugins.append(item)

        mu_plugins = [dict(r) for r in list(raw.get("mu_plugins") or []) if isinstance(r, dict)]
        dropins = [
            {**dict(r), "read_only": True}
            for r in list(raw.get("dropins") or [])
            if isinstance(r, dict)
        ]

        return ok_result(
            f"Plugin-inventering hämtad ({len(plugins)} plugins, {updates_count} uppdateringar).",
            site_id=site.id,
            plugins=plugins,
            mu_plugins=mu_plugins,
            dropins=dropins,
            constants=dict(raw.get("constants") or {}),
            autoupdate_filters=list(raw.get("autoupdate_filters") or []),
            wp_version=str(raw.get("wp_version") or site.wordpress_version or ""),
            php_version=str(raw.get("php_version") or site.php_version or ""),
            summary={
                "total": len(plugins),
                "active": active_count,
                "updates_available": updates_count,
                "critical_updates": critical_updates,
                "auto_update_enabled": autoupdate_count,
                "mu_plugins": len(mu_plugins),
                "dropins": len(dropins),
            },
            executed=False,
            live_calls=bool(getattr(client, "is_live", False)),
        )

    def mu_plugins_and_dropins(self, site_id: str, *, actor: str) -> WpOpsResult:
        """Inspect must-use plugins and read-only WordPress drop-ins."""
        inv = self.plugin_inventory(site_id, actor=actor, include_updates=False, include_wporg=False)
        if not inv.ok:
            return inv
        mu_plugins = list((inv.data or {}).get("mu_plugins") or [])
        dropins = list((inv.data or {}).get("dropins") or [])
        return ok_result(
            f"MU-plugins ({len(mu_plugins)}) och drop-ins ({len(dropins)}, skrivskyddade).",
            site_id=(inv.data or {}).get("site_id", site_id),
            mu_plugins=mu_plugins,
            dropins=dropins,
            mu_count=len(mu_plugins),
            dropin_count=len(dropins),
            dropins_read_only=True,
            executed=False,
            live_calls=bool((inv.data or {}).get("live_calls", False)),
        )

    def plugin_diagnostics(
        self,
        site_id: str,
        *,
        actor: str,
        hooks: list[str] | None = None,
        log_lines: int = 80,
    ) -> WpOpsResult:
        """Inspect paused plugins, hook maps, backups, and debug.log tail (WSOD fallback aware)."""
        actor_obj, denied = self._require(
            actor, Permission.WP_OPS_READ, action="wp_plugin_diagnostics", target_id=site_id
        )
        if denied is not None or actor_obj is None:
            return denied or fail_result("AccessDenied", "Saknar behörighet.", executed=False)
        is_admin = bool(actor_obj.has(Permission.WP_OPS_ADMIN))

        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)

        line_cap = min(max(int(log_lines or 80), 1), 200)
        bridge_data: dict[str, Any] | None = None
        bridge_error = ""
        live_calls = False

        client, _bridge_err = self._bridge_client(site)
        if client is not None:
            try:
                bridge_data = client.plugin_diagnostics(hooks=hooks, log_lines=line_cap)
                live_calls = bool(getattr(client, "is_live", False))
            except Exception as exc:
                bridge_error = str(exc)[:160]

        quarantined: dict[str, Any] = {"plugins": [], "mu_plugins": []}
        sftp_log_tail: list[str] = []
        source = "bridge"

        if bridge_data is None:
            source = "sftp_fallback"
            dt = self._deploy_transport(site)
            if dt is not None and is_admin:
                try:
                    tail_res = dt.read_debug_log_tail(max_lines=line_cap)
                    sftp_log_tail = list(tail_res.get("lines") or [])
                    live_calls = live_calls or (not is_mock_mode())
                except Exception:
                    pass
                try:
                    q_res = dt.list_quarantined()
                    quarantined = {
                        "plugins": list(q_res.get("plugins") or []),
                        "mu_plugins": list(q_res.get("mu_plugins") or []),
                    }
                except Exception:
                    pass
            elif is_mock_mode():
                quarantined = self._mock_list_quarantined(site)
                sftp_log_tail = self._mock_read_debug_log(site, max_lines=line_cap) if is_admin else []
            else:
                return fail_result(
                    "DiagnosticsUnavailable",
                    f"Bridge-diagnostik misslyckades ({bridge_error or 'ej konfigurerad'}) och SFTP-fallback saknas.",
                    site_id=site.id,
                    executed=False,
                    live_calls=live_calls,
                )
        else:
            if is_mock_mode():
                quarantined = self._mock_list_quarantined(site)
            elif is_admin:
                dt = self._deploy_transport(site)
                if dt is not None:
                    try:
                        q_res = dt.list_quarantined()
                        quarantined = {
                            "plugins": list(q_res.get("plugins") or []),
                            "mu_plugins": list(q_res.get("mu_plugins") or []),
                        }
                    except Exception:
                        pass

        raw_log = list((bridge_data or {}).get("debug_log_tail") or sftp_log_tail)
        scrubbed_log = [redact_secret_text(str(line))[:400] for line in raw_log[:line_cap]]
        paused = list((bridge_data or {}).get("paused_plugins") or [])
        hook_map = dict((bridge_data or {}).get("hook_map") or {})
        available_backups = list((bridge_data or {}).get("available_backups") or [])
        bridge_health = dict((bridge_data or {}).get("bridge_health") or {})

        if not is_admin:
            return ok_result(
                "Diagnostiksammanfattning (rå debug.log dold för icke-admin).",
                site_id=site.id,
                source=source,
                paused_plugins=paused,
                debug_log_tail=[],
                debug_log_redacted=True,
                debug_log_line_count=len(scrubbed_log),
                hook_counts={str(k): len(v) if isinstance(v, list) else 0 for k, v in hook_map.items()},
                available_backup_count=len(available_backups),
                bridge_health=bridge_health,
                quarantined=quarantined,
                bridge_error=bridge_error,
                executed=False,
                live_calls=live_calls,
            )

        return ok_result(
            "Plugin-diagnostik hämtad.",
            site_id=site.id,
            source=source,
            paused_plugins=paused,
            debug_log_tail=scrubbed_log,
            debug_log_redacted=False,
            debug_log_line_count=len(scrubbed_log),
            hook_map=hook_map,
            available_backups=available_backups,
            bridge_health=bridge_health,
            quarantined=quarantined,
            bridge_error=bridge_error,
            executed=False,
            live_calls=live_calls,
        )

    # --- Safe Plugin Update Plans & Execution --------------------------- #

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
        """Build an immutable, SHA-256-pinned ``plu-*`` plugin update plan (never executes)."""
        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)

        needed = (
            Permission.WP_OPS_ADMIN
            if (site.environment == "production" or include_critical)
            else Permission.WP_OPS_OPERATE
        )
        _actor, denied = self._require(
            actor, needed, action="wp_plugin_update_plan", target_id=site.id
        )
        if denied is not None:
            return denied

        inv = self.plugin_inventory(site.id, actor=actor, include_updates=True, include_wporg=False)
        if not inv.ok:
            return inv
        inv_data = inv.data or {}

        try:
            selection = select_plugin_updates(
                list(inv_data.get("plugins") or []),
                selection_mode=selection_mode,
                slugs=slugs,
                policy=policy,
                exclude_slugs=exclude_slugs,
                include_critical=include_critical,
                canary=canary,
                wp_version=str(inv_data.get("wp_version") or ""),
                php_version=str(inv_data.get("php_version") or ""),
            )
        except ValueError as exc:
            return fail_result("InvalidPlanArguments", str(exc), site_id=site.id, executed=False)

        warnings = list(selection["warnings"])
        constants = dict(inv_data.get("constants") or {})
        if constants.get("disallow_file_mods"):
            warnings.append(
                "DISALLOW_FILE_MODS är aktiv på sajten — live-uppdatering via Bridge kommer blockeras tills konstanten stängs av."
            )

        batches_with_canary: list[dict[str, Any]] = []
        plan_canary_paths: list[str] = []
        per_plugin_canary_paths: dict[str, list[str]] = {}
        for batch in list(selection["batches"]):
            batch_copy = dict(batch)
            enriched_plugins: list[dict[str, Any]] = []
            for pitem in list(batch.get("plugins") or []):
                p_copy = dict(pitem)
                c_paths = canary_paths_for_plugin(p_copy, is_woocommerce_site=bool(site.woocommerce))
                p_copy["canary_paths"] = c_paths
                pslug = str(p_copy.get("slug") or "")
                if pslug:
                    per_plugin_canary_paths[pslug] = c_paths
                for cp in c_paths:
                    if cp not in plan_canary_paths:
                        plan_canary_paths.append(cp)
                enriched_plugins.append(p_copy)
            batch_copy["plugins"] = enriched_plugins
            batches_with_canary.append(batch_copy)
        if not plan_canary_paths:
            plan_canary_paths = list(DEFAULT_CANARY_PATHS)

        preconditions: dict[str, Any] = {
            "canary_paths": plan_canary_paths,
            "per_plugin_canary_paths": per_plugin_canary_paths,
            "disallow_file_mods": bool(constants.get("disallow_file_mods")),
            "requires_allow_critical": bool(selection["has_critical"]),
        }

        now = self._now()
        plan_id = "plu-" + uuid.uuid4().hex[:12]
        canonical_body = {
            "plan_id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "selection_mode": str(selection_mode).strip().lower(),
            "policy": str(policy).strip().lower(),
            "include_critical": bool(include_critical),
            "canary": bool(canary),
            "batches": batches_with_canary,
        }
        plan_sha256 = _canonical_sha256(canonical_body)
        plan: dict[str, Any] = {
            **canonical_body,
            "plan_sha256": plan_sha256,
            "selected_count": len(selection["selected"]),
            "skipped": selection["skipped"],
            "warnings": warnings,
            "has_critical": bool(selection["has_critical"]),
            "disallow_file_mods": bool(constants.get("disallow_file_mods")),
            "canary_paths": plan_canary_paths,
            "preconditions": preconditions,
            "status": "planned",
            "executed": False,
            "approval_required": True,
            "created_by": actor,
            "created_at": now.isoformat(),
            "expires_at": (now + _PLAN_TTL).isoformat(),
            "results": [],
        }
        with hold_site(site.id):
            self._append_plan(self._update_plans_path(), plan)

        self._audit(
            actor,
            action="wp_plugin_update_plan",
            target_id=f"{site.id}:{plan_id}",
            success=True,
            details={
                "plan_id": plan_id,
                "plan_sha256": plan_sha256,
                "selected_count": plan["selected_count"],
                "has_critical": plan["has_critical"],
                "canary_paths": plan_canary_paths,
                "executed": False,
            },
        )
        return ok_result(
            f"Uppdateringsplan {plan_id} skapad ({plan['selected_count']} plugin(s), godkännande krävs).",
            plan=plan,
            plan_id=plan_id,
            plan_sha256=plan_sha256,
            batches=plan["batches"],
            skipped=plan["skipped"],
            warnings=plan["warnings"],
            has_critical=plan["has_critical"],
            canary_paths=plan_canary_paths,
            preconditions=preconditions,
            executed=False,
            approval_required=True,
            live_calls=bool(inv_data.get("live_calls", False)),
        )

    def list_plugin_update_plans(self, site_id: str, *, actor: str) -> WpOpsResult:
        """List stored ``plu-*`` update plans for ``site_id``."""
        _actor, denied = self._require(
            actor, Permission.WP_OPS_READ, action="wp_plugin_update_plan_list", target_id=site_id
        )
        if denied is not None:
            return denied
        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)
        now = self._now()
        rows: list[dict[str, Any]] = []
        for plan in self._load_plans(self._update_plans_path()):
            if plan.get("site_id") != site.id:
                continue
            item = dict(plan)
            if item.get("status") == "planned" and plan_expired(item, now):
                item["status"] = "expired"
            rows.append(item)
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return ok_result(
            f"{len(rows)} uppdateringsplan(er).",
            site_id=site.id,
            plans=rows,
            executed=False,
            live_calls=False,
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
        """Execute a hash-pinned ``plu-*`` plan sequentially with canary stop-on-fail."""
        killed = _kill_switch_block()
        if killed is not None:
            return killed

        _actor, denied = self._require(
            actor,
            Permission.WP_OPS_ADMIN,
            action="wp_plugin_update_execute",
            target_id=f"{site_id}:{plan_id}",
        )
        if denied is not None:
            return denied

        if not is_mock_mode():
            if not env_flag("AZOM_WP_PLUGIN_UPDATES_ENABLED"):
                return fail_result(
                    "PluginUpdatesDisabled",
                    "Live-uppdatering av plugins är avstängd (sätt AZOM_WP_PLUGIN_UPDATES_ENABLED=1 i processmiljön).",
                    site_id=site_id,
                    plan_id=plan_id,
                    executed=False,
                    live_calls=False,
                )
            if not env_flag("AZOM_WP_BRIDGE_ENABLED"):
                return fail_result(
                    "BridgeDisabled",
                    "Azom Ops Bridge krävs för plugin-uppdateringar (AZOM_WP_BRIDGE_ENABLED=1).",
                    site_id=site_id,
                    plan_id=plan_id,
                    executed=False,
                    live_calls=False,
                )

        if not _PLU_RE.fullmatch(str(plan_id or "").strip()):
            return fail_result("UnknownPlan", "Ogiltigt plan-id.", executed=False)

        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)

        with hold_site(site.id):
            plan = self._find_plan(self._update_plans_path(), site.id, plan_id)
            if plan is None:
                return fail_result("UnknownPlan", "Uppdateringsplanen hittades inte.", executed=False)
            if plan.get("status") != "planned":
                return fail_result(
                    "PlanNotExecutable",
                    f"Planen kan inte köras (status={plan.get('status')}).",
                    plan_id=plan_id,
                    status=plan.get("status"),
                    executed=False,
                )
            if plan_expired(plan, self._now()):
                plan["status"] = "expired"
                self._update_plan(self._update_plans_path(), plan)
                return fail_result("Expired", "Planen har gått ut (12h TTL).", plan_id=plan_id, executed=False)

            recomputed = _canonical_sha256(
                {
                    "plan_id": plan["plan_id"],
                    "site_id": plan["site_id"],
                    "environment": plan["environment"],
                    "selection_mode": plan["selection_mode"],
                    "policy": plan["policy"],
                    "include_critical": bool(plan["include_critical"]),
                    "canary": bool(plan["canary"]),
                    "batches": plan["batches"],
                }
            )
            if (
                str(expected_sha256 or "").strip() != str(plan.get("plan_sha256") or "")
                or recomputed != plan.get("plan_sha256")
            ):
                return fail_result(
                    "DigestMismatch",
                    "Låst plan_sha256 matchar inte planen.",
                    plan_id=plan_id,
                    executed=False,
                )

            if plan.get("has_critical") and not allow_critical:
                return fail_result(
                    "CriticalApprovalRequired",
                    "Planen innehåller kritiska WooCommerce-/betalplugins och kräver explicit allow_critical=True.",
                    plan_id=plan_id,
                    executed=False,
                )

            batches = list(plan.get("batches") or [])
            if not batches:
                return fail_result(
                    "NoUpdatesSelected",
                    "Planen innehåller inga valda plugin-uppdateringar.",
                    plan_id=plan_id,
                    executed=False,
                )

            client, bridge_err = self._bridge_client(site)
            if bridge_err is not None or client is None:
                return bridge_err or fail_result("BridgeUnconfigured", "Bridge ej konfigurerad.", executed=False)

            step_results: list[dict[str, Any]] = []
            change_ids: list[str] = []
            live_calls = bool(getattr(client, "is_live", False))

            for batch in batches:
                stage = str(batch.get("stage") or "batch")
                for item in list(batch.get("plugins") or []):
                    slug = normalize_plugin_slug(str(item.get("slug") or item.get("plugin_file") or ""))
                    plugin_file = str(item.get("plugin_file") or "")
                    from_ver = str(item.get("from_version") or "")
                    to_ver = str(item.get("to_version") or "")
                    if is_bridge_plugin(slug) or is_bridge_plugin(plugin_file):
                        return fail_result(
                            "BridgeSelfProtectionRefused",
                            "azom-ops-bridge kan aldrig uppdateras via automatik.",
                            plan_id=plan_id,
                            executed=bool(change_ids),
                        )

                    req_id = "chg-" + uuid.uuid4().hex[:12]
                    item_canary_paths = list(
                        item.get("canary_paths")
                        or canary_paths_for_plugin(item, is_woocommerce_site=bool(site.woocommerce))
                    )
                    try:
                        res = client.update_plugin(
                            change_id=req_id,
                            plugin_file=plugin_file,
                            target_version=to_ver or None,
                            backup_before=True,
                            probe_health=True,
                            canary_paths=item_canary_paths,
                        )
                    except Exception as exc:
                        res = {
                            "ok": False,
                            "plugin_file": plugin_file,
                            "slug": slug,
                            "from_version": from_ver,
                            "to_version": to_ver,
                            "backup_id": "",
                            "backup_sha256": "",
                            "http_status": 0,
                            "auto_reverted": False,
                            "canary_results": [],
                            "error": str(exc)[:160],
                        }

                    ok_step = bool(res.get("ok"))
                    auto_reverted = bool(res.get("auto_reverted"))
                    backup_id = str(res.get("backup_id") or "")
                    backup_sha = str(res.get("backup_sha256") or "")
                    step_canary_results = [
                        dict(cr) for cr in list(res.get("canary_results") or []) if isinstance(cr, dict)
                    ]
                    ledger_row = self.ledger.record_change(
                        site_id=site.id,
                        environment=site.environment,
                        category="plugin_update",
                        transport="bridge",
                        actor=actor,
                        summary=f"Plugin-uppdatering {slug}: {from_ver} -> {res.get('to_version') or to_ver}",
                        target_ref=f"plugin:{slug}",
                        before_data={
                            "slug": slug,
                            "plugin_file": plugin_file,
                            "version": from_ver,
                            "backup_id": backup_id,
                            "backup_sha256": backup_sha,
                        },
                        after_data={
                            "slug": slug,
                            "plugin_file": plugin_file,
                            "version": str(res.get("to_version") or (from_ver if not ok_step else to_ver)),
                            "http_status": int(res.get("http_status") or 0),
                            "auto_reverted": auto_reverted,
                            "backup_id": backup_id,
                            "canary_paths": item_canary_paths,
                            "canary_results": step_canary_results,
                            "error": res.get("error"),
                        },
                        status="applied" if ok_step else ("rolled_back" if auto_reverted else "failed"),
                        batch_id=plan_id,
                        metadata={
                            "plan_id": plan_id,
                            "stage": stage,
                            "slug": slug,
                            "plugin_file": plugin_file,
                            "backup_id": backup_id,
                            "backup_sha256": backup_sha,
                            "bridge_change_id": req_id,
                            "canary_paths": item_canary_paths,
                            "canary_results": step_canary_results,
                        },
                    )
                    step_rec = {
                        "slug": slug,
                        "plugin_file": plugin_file,
                        "stage": stage,
                        "ok": ok_step,
                        "from_version": from_ver,
                        "to_version": str(res.get("to_version") or to_ver),
                        "backup_id": backup_id,
                        "http_status": int(res.get("http_status") or 0),
                        "auto_reverted": auto_reverted,
                        "canary_paths": item_canary_paths,
                        "canary_results": step_canary_results,
                        "error": res.get("error"),
                        "change_id": ledger_row["change_id"],
                    }
                    step_results.append(step_rec)
                    change_ids.append(ledger_row["change_id"])

                    if not ok_step:
                        all_canary_results = [
                            {"slug": sr["slug"], **cr}
                            for sr in step_results
                            for cr in sr.get("canary_results", [])
                        ]
                        plan["status"] = "halted_on_failure"
                        plan["executed"] = True
                        plan["executed_at"] = self._now().isoformat()
                        plan["executed_by"] = actor
                        plan["results"] = step_results
                        plan["canary_results"] = all_canary_results
                        self._update_plan(self._update_plans_path(), plan)
                        self._audit(
                            actor,
                            action="wp_plugin_update_execute",
                            target_id=f"{site.id}:{plan_id}",
                            success=False,
                            details={
                                "error_class": "CanaryUpdateHalted",
                                "halted_at": slug,
                                "auto_reverted": auto_reverted,
                                "completed": len(step_results) - 1,
                                "canary_results": step_canary_results,
                                "live_calls": live_calls,
                            },
                        )
                        return fail_result(
                            "CanaryUpdateHalted",
                            f"Uppdateringen stoppades vid '{slug}' (canary stop-on-fail; auto_reverted={auto_reverted}).",
                            site_id=site.id,
                            plan_id=plan_id,
                            halted_at=slug,
                            results=step_results,
                            canary_results=all_canary_results,
                            change_ids=change_ids,
                            executed=len(step_results) > 1,
                            live_calls=live_calls,
                        )

            all_canary_results = [
                {"slug": sr["slug"], **cr}
                for sr in step_results
                for cr in sr.get("canary_results", [])
            ]
            plan["status"] = "executed"
            plan["executed"] = True
            plan["executed_at"] = self._now().isoformat()
            plan["executed_by"] = actor
            plan["results"] = step_results
            plan["canary_results"] = all_canary_results
            self._update_plan(self._update_plans_path(), plan)

        self._audit(
            actor,
            action="wp_plugin_update_execute",
            target_id=f"{site.id}:{plan_id}",
            success=True,
            details={
                "updated_count": len(step_results),
                "change_ids": change_ids,
                "live_calls": live_calls,
            },
        )
        return ok_result(
            f"Uppdateringsplan {plan_id} genomförd ({len(step_results)} plugin(s)).",
            site_id=site.id,
            plan_id=plan_id,
            results=step_results,
            canary_results=all_canary_results,
            change_ids=change_ids,
            executed=True,
            live_calls=live_calls,
        )

    # --- Auto-Update Policies, Drift & Plans ---------------------------- #

    def get_autoupdate_policy(self, site_id: str, *, actor: str) -> WpOpsResult:
        """Read declarative per-site auto-update policy (or fail-closed default)."""
        _actor, denied = self._require(
            actor, Permission.WP_OPS_READ, action="wp_plugin_autoupdate_policy_get", target_id=site_id
        )
        if denied is not None:
            return denied
        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)
        store = self._load_json_dict(self._policies_path())
        sites_map = store.get("sites") if isinstance(store.get("sites"), dict) else {}
        stored = sites_map.get(site.id)
        if isinstance(stored, dict):
            try:
                pol = validate_autoupdate_policy(site.id, stored)
                is_default = False
            except ValueError:
                pol = default_autoupdate_policy(site.id)
                is_default = True
        else:
            pol = default_autoupdate_policy(site.id)
            is_default = True
        return ok_result(
            "Autouppdateringspolicy hämtad.",
            site_id=site.id,
            policy=pol,
            is_default=is_default,
            executed=False,
            live_calls=False,
        )

    def save_autoupdate_policy(
        self, site_id: str, *, actor: str, policy: dict[str, Any]
    ) -> WpOpsResult:
        """Validate and persist a per-site auto-update policy."""
        _actor, denied = self._require(
            actor, Permission.WP_OPS_ADMIN, action="wp_plugin_autoupdate_policy_save", target_id=site_id
        )
        if denied is not None:
            return denied
        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)
        try:
            clean = validate_autoupdate_policy(site.id, policy)
        except ValueError as exc:
            err_cls = (
                "BridgeSelfProtectionRefused"
                if "azom-ops-bridge" in str(exc)
                else "InvalidPolicy"
            )
            return fail_result(err_cls, str(exc), site_id=site.id, executed=False)
        clean["updated_at"] = self._now().isoformat()
        clean["updated_by"] = actor

        path = self._policies_path()
        with _STORE_LOCK, advisory_file_lock(path.with_suffix(".json.lock")):
            store = self._load_json_dict(path)
            sites_map = dict(store.get("sites") or {}) if isinstance(store.get("sites"), dict) else {}
            sites_map[site.id] = clean
            atomic_write_text(
                path, json.dumps({"sites": sites_map}, ensure_ascii=False, indent=2)
            )

        self._audit(
            actor,
            action="wp_plugin_autoupdate_policy_save",
            target_id=site.id,
            success=True,
            details={"preset": clean["preset"], "allow_critical": clean["allow_critical"]},
        )
        return ok_result(
            "Autouppdateringspolicy sparad.",
            site_id=site.id,
            policy=clean,
            executed=False,
            live_calls=False,
        )

    def check_autoupdate_drift(self, site_id: str, *, actor: str) -> WpOpsResult:
        """Compare live WordPress ``auto_update_plugins`` + constants/filters against site policy."""
        pol_res = self.get_autoupdate_policy(site_id, actor=actor)
        if not pol_res.ok:
            return pol_res
        inv_res = self.plugin_inventory(site_id, actor=actor, include_updates=False, include_wporg=False)
        if not inv_res.ok:
            return inv_res
        policy = dict((pol_res.data or {}).get("policy") or {})
        inv = inv_res.data or {}
        drift = evaluate_autoupdate_drift(
            list(inv.get("plugins") or []),
            policy,
            constants=dict(inv.get("constants") or {}),
            autoupdate_filters=list(inv.get("autoupdate_filters") or []),
        )
        return ok_result(
            "Autouppdateringsdrift kontrollerad."
            if drift["in_sync"]
            else f"Drift upptäckt för {drift['drift_count']} plugin(s).",
            site_id=inv.get("site_id", site_id),
            policy=policy,
            **drift,
            executed=False,
            live_calls=bool(inv.get("live_calls", False)),
        )

    def plan_plugin_autoupdate(
        self,
        site_id: str,
        *,
        actor: str,
        preset: str = "non_critical_only",
        changes: list[dict[str, Any]] | None = None,
        allow_critical: bool = False,
    ) -> WpOpsResult:
        """Create an immutable ``pau-*`` plan for ``auto_update_plugins`` changes (never executes)."""
        _actor, denied = self._require(
            actor, Permission.WP_OPS_ADMIN, action="wp_plugin_autoupdate_plan", target_id=site_id
        )
        if denied is not None:
            return denied
        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)

        inv_res = self.plugin_inventory(site.id, actor=actor, include_updates=False, include_wporg=False)
        if not inv_res.ok:
            return inv_res
        inv = inv_res.data or {}
        plugins = list(inv.get("plugins") or [])
        by_slug = {str(p.get("slug") or ""): p for p in plugins}
        by_file = {str(p.get("plugin_file") or ""): p for p in plugins}

        planned_changes: list[dict[str, Any]] = []
        warnings: list[str] = []

        if changes is not None:
            if not isinstance(changes, list):
                return fail_result("InvalidPlanArguments", "changes måste vara en lista.", executed=False)
            for row in changes:
                if not isinstance(row, dict):
                    return fail_result("InvalidPlanArguments", "Ogiltig rad i changes.", executed=False)
                raw_target = str(row.get("plugin_file") or row.get("slug") or "").strip()
                slug = normalize_plugin_slug(raw_target)
                if not slug:
                    return fail_result("InvalidPlanArguments", "Saknar plugin-slug eller fil.", executed=False)
                if is_bridge_plugin(slug) or is_bridge_plugin(raw_target):
                    return fail_result(
                        "BridgeSelfProtectionRefused",
                        "azom-ops-bridge kan aldrig få autouppdatering ändrad.",
                        site_id=site.id,
                        executed=False,
                    )
                enabled = bool(row.get("enabled"))
                if enabled and is_critical_plugin(slug) and not allow_critical:
                    return fail_result(
                        "CriticalApprovalRequired",
                        f"Kritiskt plugin '{slug}' kräver allow_critical=True för att aktivera autouppdatering.",
                        site_id=site.id,
                        executed=False,
                    )
                matched = by_file.get(raw_target) or by_slug.get(slug)
                plugin_file = (
                    str(matched.get("plugin_file") or raw_target)
                    if matched is not None
                    else raw_target
                )
                if "/" not in plugin_file and not plugin_file.endswith(".php"):
                    plugin_file = f"{slug}/{slug}.php"
                planned_changes.append(
                    {
                        "slug": slug,
                        "plugin_file": plugin_file,
                        "enabled": enabled,
                        "is_critical": is_critical_plugin(slug),
                    }
                )
            drift_warnings = evaluate_autoupdate_drift(
                plugins,
                default_autoupdate_policy(site.id),
                constants=dict(inv.get("constants") or {}),
                autoupdate_filters=list(inv.get("autoupdate_filters") or []),
            )["override_warnings"]
            warnings.extend(drift_warnings)
        else:
            clean_preset = str(preset or "non_critical_only").strip().lower()
            if clean_preset not in AUTOUPDATE_PRESETS:
                return fail_result(
                    "InvalidPlanArguments",
                    f"Ogiltig preset: {preset!r}.",
                    site_id=site.id,
                    executed=False,
                )
            stored_pol = self.get_autoupdate_policy(site.id, actor=actor)
            base_pol = dict((stored_pol.data or {}).get("policy") or default_autoupdate_policy(site.id))
            base_pol["preset"] = clean_preset
            base_pol["allow_critical"] = bool(allow_critical)
            if not allow_critical:
                base_pol = validate_autoupdate_policy(site.id, base_pol)
            drift = evaluate_autoupdate_drift(
                plugins,
                base_pol,
                constants=dict(inv.get("constants") or {}),
                autoupdate_filters=list(inv.get("autoupdate_filters") or []),
            )
            planned_changes = [
                {
                    "slug": item["slug"],
                    "plugin_file": item["plugin_file"],
                    "enabled": bool(item["enabled"]),
                    "is_critical": is_critical_plugin(item["slug"]),
                }
                for item in drift["desired_changes"]
            ]
            warnings.extend(drift["override_warnings"])

        now = self._now()
        plan_id = "pau-" + uuid.uuid4().hex[:12]
        canonical_body = {
            "plan_id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "preset": str(preset or "non_critical_only").strip().lower(),
            "allow_critical": bool(allow_critical),
            "changes": planned_changes,
        }
        plan_sha256 = _canonical_sha256(canonical_body)
        plan: dict[str, Any] = {
            **canonical_body,
            "plan_sha256": plan_sha256,
            "warnings": warnings,
            "status": "planned",
            "executed": False,
            "approval_required": True,
            "created_by": actor,
            "created_at": now.isoformat(),
            "expires_at": (now + _PLAN_TTL).isoformat(),
        }
        with hold_site(site.id):
            self._append_plan(self._autoupdate_plans_path(), plan)

        self._audit(
            actor,
            action="wp_plugin_autoupdate_plan",
            target_id=f"{site.id}:{plan_id}",
            success=True,
            details={"plan_id": plan_id, "changes_count": len(planned_changes), "executed": False},
        )
        return ok_result(
            f"Autouppdateringsplan {plan_id} skapad ({len(planned_changes)} ändring(ar), godkännande krävs).",
            plan=plan,
            plan_id=plan_id,
            plan_sha256=plan_sha256,
            changes=planned_changes,
            warnings=warnings,
            executed=False,
            approval_required=True,
            live_calls=bool(inv.get("live_calls", False)),
        )

    def execute_plugin_autoupdate_plan(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        expected_sha256: str,
    ) -> WpOpsResult:
        """Execute a hash-pinned ``pau-*`` plan against Bridge with Change Vault snapshot."""
        killed = _kill_switch_block()
        if killed is not None:
            return killed

        _actor, denied = self._require(
            actor,
            Permission.WP_OPS_ADMIN,
            action="wp_plugin_autoupdate_execute",
            target_id=f"{site_id}:{plan_id}",
        )
        if denied is not None:
            return denied

        if not is_mock_mode():
            if not env_flag("AZOM_WP_AUTOUPDATE_WRITE_ENABLED"):
                return fail_result(
                    "AutoupdateWriteDisabled",
                    "Skrivning av autouppdateringar är avstängd (sätt AZOM_WP_AUTOUPDATE_WRITE_ENABLED=1 i processmiljön).",
                    site_id=site_id,
                    plan_id=plan_id,
                    executed=False,
                    live_calls=False,
                )
            if not env_flag("AZOM_WP_BRIDGE_ENABLED"):
                return fail_result(
                    "BridgeDisabled",
                    "Azom Ops Bridge krävs för autouppdateringsändringar (AZOM_WP_BRIDGE_ENABLED=1).",
                    site_id=site_id,
                    plan_id=plan_id,
                    executed=False,
                    live_calls=False,
                )

        if not _PAU_RE.fullmatch(str(plan_id or "").strip()):
            return fail_result("UnknownPlan", "Ogiltigt plan-id.", executed=False)

        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)

        with hold_site(site.id):
            plan = self._find_plan(self._autoupdate_plans_path(), site.id, plan_id)
            if plan is None:
                return fail_result("UnknownPlan", "Autouppdateringsplanen hittades inte.", executed=False)
            if plan.get("status") != "planned":
                return fail_result(
                    "PlanNotExecutable",
                    f"Planen kan inte köras (status={plan.get('status')}).",
                    plan_id=plan_id,
                    executed=False,
                )
            if plan_expired(plan, self._now()):
                plan["status"] = "expired"
                self._update_plan(self._autoupdate_plans_path(), plan)
                return fail_result("Expired", "Planen har gått ut (12h TTL).", plan_id=plan_id, executed=False)

            recomputed = _canonical_sha256(
                {
                    "plan_id": plan["plan_id"],
                    "site_id": plan["site_id"],
                    "environment": plan["environment"],
                    "preset": plan["preset"],
                    "allow_critical": bool(plan["allow_critical"]),
                    "changes": plan["changes"],
                }
            )
            if (
                str(expected_sha256 or "").strip() != str(plan.get("plan_sha256") or "")
                or recomputed != plan.get("plan_sha256")
            ):
                return fail_result("DigestMismatch", "Låst plan_sha256 matchar inte.", plan_id=plan_id, executed=False)

            bridge_changes: list[dict[str, Any]] = []
            for item in list(plan.get("changes") or []):
                pfile = str(item.get("plugin_file") or "")
                if is_bridge_plugin(pfile) or is_bridge_plugin(str(item.get("slug") or "")):
                    return fail_result(
                        "BridgeSelfProtectionRefused",
                        "azom-ops-bridge kan aldrig få autouppdatering ändrad.",
                        plan_id=plan_id,
                        executed=False,
                    )
                bridge_changes.append({"plugin_file": pfile, "enabled": bool(item.get("enabled"))})

            if not bridge_changes:
                plan["status"] = "executed"
                plan["executed"] = True
                plan["executed_at"] = self._now().isoformat()
                plan["executed_by"] = actor
                self._update_plan(self._autoupdate_plans_path(), plan)
                return ok_result(
                    f"Autouppdateringsplan {plan_id} hade inga ändringar att applicera (redan i synk).",
                    site_id=site.id,
                    plan_id=plan_id,
                    change_id="",
                    result={"ok": True, "before": [], "after": [], "applied": [], "filter_warnings": []},
                    executed=False,
                    live_calls=False,
                )

            client, bridge_err = self._bridge_client(site)
            if bridge_err is not None or client is None:
                return bridge_err or fail_result("BridgeUnconfigured", "Bridge ej konfigurerad.", executed=False)
            live_calls = bool(getattr(client, "is_live", False))
            try:
                res = client.set_plugin_autoupdates(changes=bridge_changes)
            except Exception as exc:
                return fail_result(
                    "BridgeError",
                    f"Autouppdateringsändring misslyckades: {str(exc)[:160]}",
                    site_id=site.id,
                    plan_id=plan_id,
                    executed=False,
                    live_calls=live_calls,
                )

            ledger_row = self.ledger.record_change(
                site_id=site.id,
                environment=site.environment,
                category="plugin_autoupdate",
                transport="bridge",
                actor=actor,
                summary=f"Autouppdateringar uppdaterade ({len(bridge_changes)} ändring(ar))",
                target_ref=f"autoupdate:{site.id}",
                before_data={"auto_update_plugins": list(res.get("before") or [])},
                after_data={"auto_update_plugins": list(res.get("after") or [])},
                status="applied",
                metadata={
                    "plan_id": plan_id,
                    "applied": list(res.get("applied") or []),
                    "filter_warnings": list(res.get("filter_warnings") or []),
                },
            )
            plan["status"] = "executed"
            plan["executed"] = True
            plan["executed_at"] = self._now().isoformat()
            plan["executed_by"] = actor
            plan["change_id"] = ledger_row["change_id"]
            self._update_plan(self._autoupdate_plans_path(), plan)

        self._audit(
            actor,
            action="wp_plugin_autoupdate_execute",
            target_id=f"{site.id}:{plan_id}",
            success=True,
            details={"change_id": ledger_row["change_id"], "live_calls": live_calls},
        )
        return ok_result(
            f"Autouppdateringsplan {plan_id} genomförd (snapshot {ledger_row['change_id']}).",
            site_id=site.id,
            plan_id=plan_id,
            change_id=ledger_row["change_id"],
            result=res,
            executed=True,
            live_calls=live_calls,
        )

    # --- Recovery Plans & Out-of-Band Quarantine ------------------------ #

    def plan_plugin_recovery(
        self,
        site_id: str,
        *,
        actor: str,
        slug: str,
        action: str,
        change_id: str | None = None,
    ) -> WpOpsResult:
        """Create an immutable ``plr-*`` recovery/quarantine plan (never executes)."""
        _actor, denied = self._require(
            actor, Permission.WP_OPS_ADMIN, action="wp_plugin_recovery_plan", target_id=site_id
        )
        if denied is not None:
            return denied

        clean_action = str(action or "").strip().lower()
        if clean_action not in _RECOVERY_ACTIONS:
            return fail_result(
                "InvalidRecoveryAction",
                f"Ogiltig återställningsåtgärd: {action!r}.",
                site_id=site_id,
                executed=False,
            )

        raw_slug = str(slug or "").strip()
        clean_slug = normalize_plugin_slug(raw_slug)
        if not clean_slug or not SLUG_RE.fullmatch(clean_slug):
            return fail_result("SlugRejected", "Ogiltigt plugin-slug.", site_id=site_id, executed=False)
        if is_bridge_plugin(clean_slug) or is_bridge_plugin(raw_slug):
            return fail_result(
                "BridgeSelfProtectionRefused",
                "azom-ops-bridge är självskyddad och kan aldrig avaktiveras eller sättas i karantän.",
                site_id=site_id,
                executed=False,
            )

        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)

        if "/" in raw_slug:
            plugin_file = raw_slug
        else:
            plugin_file = f"{clean_slug}/{clean_slug}.php"
            try:
                inv = self.plugin_inventory(site.id, actor=actor, include_updates=False)
                if inv.ok:
                    for row in list(inv.data.get("plugins") or []):
                        if normalize_plugin_slug(str(row.get("slug") or "")) == clean_slug:
                            resolved_pf = str(row.get("plugin_file") or "").strip()
                            if resolved_pf:
                                plugin_file = resolved_pf
                                break
            except Exception:
                pass
        backup_id = ""
        verified_change_id = ""
        if clean_action == "rollback_snapshot":
            cid = str(change_id or "").strip()
            if not valid_change_id(cid):
                return fail_result(
                    "UnknownChange",
                    "rollback_snapshot kräver ett giltigt change_id (chg-...).",
                    site_id=site.id,
                    executed=False,
                )
            rec = self.ledger.get_change(site.id, cid)
            if rec is None:
                return fail_result("UnknownChange", "Ändringen hittades inte.", site_id=site.id, executed=False)
            verify = self.ledger.verify_snapshot(site.id, cid)
            if not verify.get("verified"):
                return fail_result(
                    str(verify.get("error_class") or "SnapshotCorrupted"),
                    "Snapshotets integritet kunde inte verifieras.",
                    site_id=site.id,
                    executed=False,
                )
            meta = rec.get("metadata") or {}
            snap_before: dict[str, Any] = {}
            backup_id = str(meta.get("backup_id") or "")
            if not backup_id or not meta.get("slug"):
                snap = self.ledger.snapshots.read_snapshot(site.id, cid)
                snap_before = dict(snap.get("before") or {})
                if not backup_id:
                    backup_id = str(snap_before.get("backup_id") or "")
            rec_slug = normalize_plugin_slug(
                str(
                    meta.get("slug")
                    or meta.get("plugin_file")
                    or snap_before.get("slug")
                    or snap_before.get("plugin_file")
                    or ""
                )
            )
            if not rec_slug or rec_slug != clean_slug:
                return fail_result(
                    "SlugMismatch",
                    f"Ändring {cid} tillhör inte plugin '{clean_slug}'.",
                    site_id=site.id,
                    executed=False,
                )
            if not backup_id:
                return fail_result(
                    "BackupMissing",
                    "Ändringen saknar backup_id för plugin-återställning.",
                    site_id=site.id,
                    executed=False,
                )
            plugin_file = str(meta.get("plugin_file") or snap_before.get("plugin_file") or plugin_file)
            verified_change_id = cid

        now = self._now()
        plan_id = "plr-" + uuid.uuid4().hex[:12]
        canonical_body = {
            "plan_id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "slug": clean_slug,
            "plugin_file": plugin_file,
            "action": clean_action,
            "change_id": verified_change_id,
            "backup_id": backup_id,
        }
        plan_sha256 = _canonical_sha256(canonical_body)
        plan: dict[str, Any] = {
            **canonical_body,
            "plan_sha256": plan_sha256,
            "is_critical": is_critical_plugin(clean_slug),
            "status": "planned",
            "executed": False,
            "approval_required": True,
            "created_by": actor,
            "created_at": now.isoformat(),
            "expires_at": (now + _PLAN_TTL).isoformat(),
        }
        with hold_site(site.id):
            self._append_plan(self._recovery_plans_path(), plan)

        self._audit(
            actor,
            action="wp_plugin_recovery_plan",
            target_id=f"{site.id}:{plan_id}",
            success=True,
            details={"plan_id": plan_id, "slug": clean_slug, "recovery_action": clean_action, "executed": False},
        )
        return ok_result(
            f"Återställningsplan {plan_id} skapad ({clean_action} på {clean_slug}, godkännande krävs).",
            plan=plan,
            plan_id=plan_id,
            plan_sha256=plan_sha256,
            executed=False,
            approval_required=True,
            live_calls=False,
        )

    def execute_plugin_recovery_plan(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        expected_sha256: str,
    ) -> WpOpsResult:
        """Execute a hash-pinned ``plr-*`` recovery plan with Change Vault recording."""
        killed = _kill_switch_block()
        if killed is not None:
            return killed

        _actor, denied = self._require(
            actor,
            Permission.WP_OPS_ADMIN,
            Permission.WP_OPS_RESTORE,
            action="wp_plugin_recovery_execute",
            target_id=f"{site_id}:{plan_id}",
        )
        if denied is not None:
            return denied

        if not is_mock_mode() and not env_flag("AZOM_WP_RESTORE_ENABLED"):
            return fail_result(
                "RestoreGateDisabled",
                "Live-återställning är avstängd (sätt AZOM_WP_RESTORE_ENABLED=1 i processmiljön).",
                site_id=site_id,
                plan_id=plan_id,
                executed=False,
                live_calls=False,
            )

        if not _PLR_RE.fullmatch(str(plan_id or "").strip()):
            return fail_result("UnknownPlan", "Ogiltigt plan-id.", executed=False)

        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)

        with hold_site(site.id):
            plan = self._find_plan(self._recovery_plans_path(), site.id, plan_id)
            if plan is None:
                return fail_result("UnknownPlan", "Återställningsplanen hittades inte.", executed=False)
            if plan.get("status") != "planned":
                return fail_result(
                    "PlanNotExecutable",
                    f"Planen kan inte köras (status={plan.get('status')}).",
                    plan_id=plan_id,
                    executed=False,
                )
            if plan_expired(plan, self._now()):
                plan["status"] = "expired"
                self._update_plan(self._recovery_plans_path(), plan)
                return fail_result("Expired", "Planen har gått ut (12h TTL).", plan_id=plan_id, executed=False)

            recomputed = _canonical_sha256(
                {
                    "plan_id": plan["plan_id"],
                    "site_id": plan["site_id"],
                    "environment": plan["environment"],
                    "slug": plan["slug"],
                    "plugin_file": plan["plugin_file"],
                    "action": plan["action"],
                    "change_id": plan["change_id"],
                    "backup_id": plan["backup_id"],
                }
            )
            if (
                str(expected_sha256 or "").strip() != str(plan.get("plan_sha256") or "")
                or recomputed != plan.get("plan_sha256")
            ):
                return fail_result("DigestMismatch", "Låst plan_sha256 matchar inte.", plan_id=plan_id, executed=False)

            slug = str(plan["slug"])
            if is_bridge_plugin(slug) or is_bridge_plugin(str(plan.get("plugin_file") or "")):
                return fail_result(
                    "BridgeSelfProtectionRefused",
                    "azom-ops-bridge är självskyddad.",
                    plan_id=plan_id,
                    executed=False,
                )

            rec_action = str(plan["action"])
            if rec_action in {"quarantine_sftp", "restore_from_quarantine"}:
                if rec_action == "quarantine_sftp":
                    out = self._quarantine_locked(site, slug, actor=actor, kind="plugin", plan_id=plan_id)
                else:
                    out = self._unquarantine_locked(
                        site, slug, actor=actor, quarantine_name="", kind="plugin", plan_id=plan_id
                    )
                if not out.ok:
                    return out
                plan["status"] = "executed"
                plan["executed"] = True
                plan["executed_at"] = self._now().isoformat()
                plan["executed_by"] = actor
                plan["result_change_id"] = (out.data or {}).get("change_id", "")
                self._update_plan(self._recovery_plans_path(), plan)
                return ok_result(
                    out.message,
                    site_id=site.id,
                    plan_id=plan_id,
                    action=rec_action,
                    **{k: v for k, v in (out.data or {}).items() if k not in {"site_id", "plan_id"}},
                )

            if not is_mock_mode() and not env_flag("AZOM_WP_BRIDGE_ENABLED"):
                return fail_result(
                    "BridgeDisabled",
                    "Azom Ops Bridge krävs för denna återställning (AZOM_WP_BRIDGE_ENABLED=1).",
                    site_id=site.id,
                    plan_id=plan_id,
                    executed=False,
                    live_calls=False,
                )

            client, bridge_err = self._bridge_client(site)
            if bridge_err is not None or client is None:
                return bridge_err or fail_result("BridgeUnconfigured", "Bridge ej konfigurerad.", executed=False)
            live_calls = bool(getattr(client, "is_live", False))

            if rec_action == "rollback_snapshot":
                try:
                    res = client.rollback_plugin(
                        change_id=str(plan.get("change_id") or plan_id),
                        plugin_file=str(plan["plugin_file"]),
                        backup_id=str(plan["backup_id"]),
                        probe_health=True,
                    )
                except Exception as exc:
                    return fail_result(
                        "RollbackFailed",
                        f"Plugin-återställning misslyckades: {str(exc)[:160]}",
                        site_id=site.id,
                        plan_id=plan_id,
                        executed=False,
                        live_calls=live_calls,
                    )
                if not res.get("ok"):
                    return fail_result(
                        "RollbackFailed",
                        "Bridge rapporterade misslyckad plugin-återställning.",
                        site_id=site.id,
                        plan_id=plan_id,
                        result=res,
                        executed=False,
                        live_calls=live_calls,
                    )
                ledger_row = self.ledger.record_change(
                    site_id=site.id,
                    environment=site.environment,
                    category="plugin_rollback",
                    transport="bridge",
                    actor=actor,
                    summary=f"Plugin-rollback {slug} från {plan['backup_id']}",
                    target_ref=f"plugin:{slug}",
                    before_data={"slug": slug, "plugin_file": plan["plugin_file"]},
                    after_data={
                        "slug": slug,
                        "plugin_file": plan["plugin_file"],
                        "restored_version": str(res.get("restored_version") or ""),
                        "backup_id": str(plan["backup_id"]),
                        "http_status": int(res.get("http_status") or 200),
                    },
                    status="applied",
                    parent_change_id=str(plan.get("change_id") or "") or None,
                    metadata={
                        "plan_id": plan_id,
                        "slug": slug,
                        "plugin_file": plan["plugin_file"],
                        "backup_id": plan["backup_id"],
                    },
                )
                if plan.get("change_id"):
                    self.ledger.mark_rolled_back(
                        site.id,
                        str(plan["change_id"]),
                        rollback_change_id=ledger_row["change_id"],
                    )
            else:
                # deactivate
                try:
                    res = client.toggle_plugin(
                        plugin_file=str(plan["plugin_file"]),
                        active=False,
                        probe_health=True,
                    )
                except Exception as exc:
                    return fail_result(
                        "DeactivateFailed",
                        f"Avaktivering misslyckades: {str(exc)[:160]}",
                        site_id=site.id,
                        plan_id=plan_id,
                        executed=False,
                        live_calls=live_calls,
                    )
                if not res.get("ok"):
                    return fail_result(
                        "DeactivateFailed",
                        "Bridge rapporterade misslyckad avaktivering.",
                        site_id=site.id,
                        plan_id=plan_id,
                        result=res,
                        executed=False,
                        live_calls=live_calls,
                    )
                ledger_row = self.ledger.record_change(
                    site_id=site.id,
                    environment=site.environment,
                    category="plugin_rollback",
                    transport="bridge",
                    actor=actor,
                    summary=f"Nödavaktivering av plugin {slug}",
                    target_ref=f"plugin:{slug}",
                    before_data={"slug": slug, "plugin_file": plan["plugin_file"], "active": True},
                    after_data={
                        "slug": slug,
                        "plugin_file": plan["plugin_file"],
                        "active": False,
                        "http_status": int(res.get("http_status") or 200),
                    },
                    status="applied",
                    metadata={
                        "plan_id": plan_id,
                        "slug": slug,
                        "plugin_file": plan["plugin_file"],
                        "recovery_action": "deactivate",
                    },
                )

            plan["status"] = "executed"
            plan["executed"] = True
            plan["executed_at"] = self._now().isoformat()
            plan["executed_by"] = actor
            plan["result_change_id"] = ledger_row["change_id"]
            self._update_plan(self._recovery_plans_path(), plan)

        self._audit(
            actor,
            action="wp_plugin_recovery_execute",
            target_id=f"{site.id}:{plan_id}",
            success=True,
            details={
                "recovery_action": rec_action,
                "slug": slug,
                "change_id": ledger_row["change_id"],
                "live_calls": live_calls,
            },
        )
        return ok_result(
            f"Återställningsplan {plan_id} genomförd ({rec_action} på {slug}).",
            site_id=site.id,
            plan_id=plan_id,
            action=rec_action,
            change_id=ledger_row["change_id"],
            result=res,
            executed=True,
            live_calls=live_calls,
        )

    def quarantine_plugin(
        self,
        site_id: str,
        slug: str,
        *,
        actor: str,
        kind: str = "plugin",
    ) -> WpOpsResult:
        """Quarantine a plugin dir or MU-plugin out-of-band via ``SshDeployTransport`` (or mock tree)."""
        killed = _kill_switch_block()
        if killed is not None:
            return killed
        _actor, denied = self._require(
            actor,
            Permission.WP_OPS_ADMIN,
            Permission.WP_OPS_RESTORE,
            action="wp_plugin_quarantine",
            target_id=f"{site_id}:{slug}",
        )
        if denied is not None:
            return denied
        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)
        with hold_site(site.id):
            return self._quarantine_locked(site, slug, actor=actor, kind=kind)

    def unquarantine_plugin(
        self,
        site_id: str,
        slug: str,
        *,
        actor: str,
        quarantine_name: str = "",
        kind: str = "plugin",
    ) -> WpOpsResult:
        """Restore a quarantined plugin dir or MU-plugin out-of-band."""
        killed = _kill_switch_block()
        if killed is not None:
            return killed
        _actor, denied = self._require(
            actor,
            Permission.WP_OPS_ADMIN,
            Permission.WP_OPS_RESTORE,
            action="wp_plugin_unquarantine",
            target_id=f"{site_id}:{slug}",
        )
        if denied is not None:
            return denied
        site, err = self._resolve_site(site_id)
        if err is not None or site is None:
            return err or fail_result("UnknownSite", "Okänd webbplats.", executed=False)
        with hold_site(site.id):
            return self._unquarantine_locked(
                site, slug, actor=actor, quarantine_name=quarantine_name, kind=kind
            )

    def _validate_quarantine_target(self, slug: str, kind: str) -> tuple[str, WpOpsResult | None]:
        clean_kind = str(kind or "plugin").strip().lower()
        if clean_kind not in {"plugin", "mu_plugin"}:
            return "", fail_result("InvalidTarget", "kind måste vara 'plugin' eller 'mu_plugin'.", executed=False)
        clean = str(slug or "").strip()
        if is_bridge_plugin(clean):
            return "", fail_result(
                "BridgeSelfProtectionRefused",
                "azom-ops-bridge är självskyddad och kan aldrig sättas i karantän.",
                executed=False,
            )
        if clean_kind == "plugin":
            norm = normalize_plugin_slug(clean)
            if not norm or not SLUG_RE.fullmatch(norm):
                return "", fail_result("SlugRejected", "Ogiltigt plugin-slug.", executed=False)
            return norm, None
        if (
            not clean
            or "/" in clean
            or "\\" in clean
            or clean in {".", ".."}
            or sensitive_relative(clean)
            or _QUARANTINE_MARKER in clean
        ):
            return "", fail_result("PathRejected", "Ogiltigt MU-plugin-mål.", executed=False)
        if clean.lower().endswith(".php"):
            if SEGMENT_RE.fullmatch(clean) is None:
                return "", fail_result("PathRejected", "Ogiltigt MU-plugin-filnamn.", executed=False)
            return clean, None
        if not SLUG_RE.fullmatch(clean):
            return "", fail_result("SlugRejected", "Ogiltigt MU-plugin-slug.", executed=False)
        return clean, None

    def _quarantine_locked(
        self,
        site: Site,
        slug: str,
        *,
        actor: str,
        kind: str = "plugin",
        plan_id: str = "",
    ) -> WpOpsResult:
        target, invalid = self._validate_quarantine_target(slug, kind)
        if invalid is not None:
            return invalid
        clean_kind = str(kind or "plugin").strip().lower()

        if not is_mock_mode():
            if not env_flag("AZOM_WP_RESTORE_ENABLED"):
                return fail_result(
                    "RestoreGateDisabled",
                    "Live-karantän kräver AZOM_WP_RESTORE_ENABLED=1 i processmiljön.",
                    site_id=site.id,
                    executed=False,
                    live_calls=False,
                )
            dt = self._deploy_transport(site)
            if dt is None:
                return fail_result(
                    "DeploySshUnconfigured",
                    "Out-of-band karantän kräver AZOM_WP_DEPLOY_SSH=1 och *-wp-deployer-nyckel.",
                    site_id=site.id,
                    executed=False,
                    live_calls=False,
                )
            transport_name = "sftp_deploy"
            live_calls = True
        else:
            dt = self.deploy_transport_factory(site) if self.deploy_transport_factory is not None else None
            transport_name = "sftp_deploy" if dt is not None else "local_mock"
            live_calls = False

        try:
            if dt is not None:
                res = (
                    dt.quarantine_mu_plugin(target)
                    if clean_kind == "mu_plugin"
                    else dt.quarantine_plugin(target)
                )
            else:
                res = self._mock_quarantine(site, target, kind=clean_kind)
        except (PathRejected, TransportError) as exc:
            err_cls = exc.error_class if isinstance(exc, TransportError) else "PathRejected"
            return fail_result(
                err_cls,
                f"Karantän misslyckades ({err_cls}).",
                site_id=site.id,
                executed=False,
                live_calls=live_calls,
            )

        q_name = str(res.get("quarantine_name") or "")
        ledger_row = self.ledger.record_change(
            site_id=site.id,
            environment=site.environment,
            category="plugin_quarantine",
            transport=transport_name,
            actor=actor,
            summary=f"Karantän ({clean_kind}): {target} -> {q_name}",
            target_ref=f"{clean_kind}:{target}",
            before_data={"action": "quarantine", "kind": clean_kind, "target": target, "quarantined": False},
            after_data={
                "action": "quarantine",
                "kind": clean_kind,
                "target": target,
                "quarantined": True,
                "quarantine_name": q_name,
            },
            status="applied",
            metadata={
                "action": "quarantine",
                "kind": clean_kind,
                "target": target,
                "quarantine_name": q_name,
                "plan_id": plan_id,
            },
        )
        self._audit(
            actor,
            action="wp_plugin_quarantine",
            target_id=f"{site.id}:{target}",
            success=True,
            details={"kind": clean_kind, "quarantine_name": q_name, "change_id": ledger_row["change_id"]},
        )
        return ok_result(
            f"{target} satt i karantän ({q_name}).",
            site_id=site.id,
            kind=clean_kind,
            target=target,
            quarantine_name=q_name,
            change_id=ledger_row["change_id"],
            transport=transport_name,
            executed=True,
            live_calls=live_calls,
        )

    def _unquarantine_locked(
        self,
        site: Site,
        slug: str,
        *,
        actor: str,
        quarantine_name: str = "",
        kind: str = "plugin",
        plan_id: str = "",
    ) -> WpOpsResult:
        target, invalid = self._validate_quarantine_target(slug, kind)
        if invalid is not None:
            return invalid
        clean_kind = str(kind or "plugin").strip().lower()

        if not is_mock_mode():
            if not env_flag("AZOM_WP_RESTORE_ENABLED"):
                return fail_result(
                    "RestoreGateDisabled",
                    "Återställning från karantän kräver AZOM_WP_RESTORE_ENABLED=1 i processmiljön.",
                    site_id=site.id,
                    executed=False,
                    live_calls=False,
                )
            dt = self._deploy_transport(site)
            if dt is None:
                return fail_result(
                    "DeploySshUnconfigured",
                    "Out-of-band återställning kräver AZOM_WP_DEPLOY_SSH=1 och *-wp-deployer-nyckel.",
                    site_id=site.id,
                    executed=False,
                    live_calls=False,
                )
            transport_name = "sftp_deploy"
            live_calls = True
        else:
            dt = self.deploy_transport_factory(site) if self.deploy_transport_factory is not None else None
            transport_name = "sftp_deploy" if dt is not None else "local_mock"
            live_calls = False

        try:
            if dt is not None:
                res = (
                    dt.unquarantine_mu_plugin(target, quarantine_name=quarantine_name)
                    if clean_kind == "mu_plugin"
                    else dt.unquarantine_plugin(target, quarantine_name=quarantine_name)
                )
            else:
                res = self._mock_unquarantine(
                    site, target, quarantine_name=quarantine_name, kind=clean_kind
                )
        except (PathRejected, TransportError) as exc:
            err_cls = exc.error_class if isinstance(exc, TransportError) else "PathRejected"
            return fail_result(
                err_cls,
                f"Återställning från karantän misslyckades ({err_cls}).",
                site_id=site.id,
                executed=False,
                live_calls=live_calls,
            )

        restored_from = str(res.get("restored_from") or quarantine_name)
        ledger_row = self.ledger.record_change(
            site_id=site.id,
            environment=site.environment,
            category="plugin_quarantine",
            transport=transport_name,
            actor=actor,
            summary=f"Återställd från karantän ({clean_kind}): {restored_from} -> {target}",
            target_ref=f"{clean_kind}:{target}",
            before_data={
                "action": "unquarantine",
                "kind": clean_kind,
                "target": target,
                "quarantined": True,
                "quarantine_name": restored_from,
            },
            after_data={
                "action": "unquarantine",
                "kind": clean_kind,
                "target": target,
                "quarantined": False,
            },
            status="applied",
            metadata={
                "action": "unquarantine",
                "kind": clean_kind,
                "target": target,
                "quarantine_name": restored_from,
                "plan_id": plan_id,
            },
        )
        self._audit(
            actor,
            action="wp_plugin_unquarantine",
            target_id=f"{site.id}:{target}",
            success=True,
            details={"kind": clean_kind, "restored_from": restored_from, "change_id": ledger_row["change_id"]},
        )
        return ok_result(
            f"{target} återställd från karantän ({restored_from}).",
            site_id=site.id,
            kind=clean_kind,
            target=target,
            restored_from=restored_from,
            change_id=ledger_row["change_id"],
            transport=transport_name,
            executed=True,
            live_calls=live_calls,
        )

    # --- Local mock tree quarantine & debug.log helpers ----------------- #

    def _mock_quarantine(self, site: Site, target: str, *, kind: str) -> dict[str, Any]:
        tree = self._mock_local_tree(site)
        folder = "mu-plugins" if kind == "mu_plugin" else "plugins"
        parent = tree._local_checked(f"{tree.remote_root}/wp-content/{folder}")
        src = tree._local_checked(f"{tree.remote_root}/wp-content/{folder}/{target}")
        if not src.exists():
            raise TransportError("FileMissing")
        if src.is_symlink():
            raise PathRejected("symlink")
        if kind == "plugin" and not src.is_dir():
            raise PathRejected("directory")
        qid = uuid.uuid4().hex[:8]
        q_name = f"{target}{_QUARANTINE_MARKER}{qid}"
        dst = parent / q_name
        src.rename(dst)
        return {"ok": True, "folder": folder, "target": target, "quarantine_name": q_name, "quarantine_id": qid}

    def _mock_unquarantine(
        self, site: Site, target: str, *, quarantine_name: str = "", kind: str
    ) -> dict[str, Any]:
        tree = self._mock_local_tree(site)
        folder = "mu-plugins" if kind == "mu_plugin" else "plugins"
        parent = tree._local_checked(f"{tree.remote_root}/wp-content/{folder}")
        dst = tree._local_checked(f"{tree.remote_root}/wp-content/{folder}/{target}")
        if dst.exists():
            raise TransportError("FileExistsConflict")
        prefix = f"{target}{_QUARANTINE_MARKER}"
        chosen = str(quarantine_name or "").strip()
        if chosen:
            if not chosen.startswith(prefix) or _QUARANTINE_NAME_RE.fullmatch(chosen) is None:
                raise PathRejected("quarantine_name")
        else:
            if not parent.is_dir():
                raise TransportError("FileMissing")
            matches = sorted(
                p.name
                for p in parent.iterdir()
                if not p.is_symlink()
                and p.name.startswith(prefix)
                and _QUARANTINE_NAME_RE.fullmatch(p.name)
            )
            if not matches:
                raise TransportError("FileMissing")
            chosen = matches[-1]
        src = tree._local_checked(f"{tree.remote_root}/wp-content/{folder}/{chosen}")
        if not src.exists() or src.is_symlink():
            raise TransportError("FileMissing")
        src.rename(dst)
        return {"ok": True, "folder": folder, "target": target, "restored_from": chosen}

    def _mock_list_quarantined(self, site: Site) -> dict[str, list[dict[str, str]]]:
        tree = self._mock_local_tree(site)
        out: dict[str, list[dict[str, str]]] = {"plugins": [], "mu_plugins": []}
        for folder, key in (("plugins", "plugins"), ("mu-plugins", "mu_plugins")):
            try:
                parent = tree._local_checked(f"{tree.remote_root}/wp-content/{folder}")
            except PathRejected:
                continue
            if not parent.is_dir():
                continue
            for entry in sorted(parent.iterdir(), key=lambda p: p.name):
                if entry.is_symlink() or _QUARANTINE_NAME_RE.fullmatch(entry.name) is None:
                    continue
                orig, _, qid = entry.name.partition(_QUARANTINE_MARKER)
                out[key].append(
                    {
                        "target": orig,
                        "quarantine_name": entry.name,
                        "quarantine_id": qid,
                        "kind": "plugin" if folder == "plugins" else "mu_plugin",
                    }
                )
        return out

    def _mock_read_debug_log(self, site: Site, *, max_lines: int = 80) -> list[str]:
        tree = self._mock_local_tree(site)
        try:
            log_path = tree._local_checked(f"{tree.remote_root}/wp-content/debug.log")
        except PathRejected:
            return []
        if not log_path.is_file() or log_path.is_symlink():
            return []
        raw = log_path.read_bytes()[-16384:]
        text = raw.decode("utf-8", errors="replace")
        return [redact_secret_text(line)[:400] for line in text.splitlines()[-max_lines:]]


__all__ = ["PluginOpsService"]
