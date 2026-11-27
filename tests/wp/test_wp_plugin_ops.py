"""Comprehensive tests for Slice 2: plugin policy, lifecycle service, SFTP quarantine, MU-plugins, scaffolds & static checks."""

from __future__ import annotations

import io
import stat
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _helpers import passing_catalog

from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient
from ecom_ops.integrations.wp.sftp_deploy_transport import SshDeployTransport
from ecom_ops.integrations.wp.sftp_transport import SshFileTransport
from ecom_ops.runtime_env import PROTECTED_OVERLAY_KEYS, overlay_key_allowed
from ecom_ops.wp_ops.change_ledger import ChangeLedger
from ecom_ops.wp_ops.deploy_orchestrator import DeployOrchestrator
from ecom_ops.wp_ops.file_paths import (
    COMPONENT_KINDS,
    PathRejected,
    TransportError,
    component_dir,
    components_parent,
    resolve_file,
    resolve_mu_root_file,
)
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.plugin_policy import (
    BRIDGE_PLUGIN_SLUG,
    CRITICAL_PLUGIN_SLUGS,
    classify_update_risk,
    default_autoupdate_policy,
    evaluate_autoupdate_drift,
    is_bridge_plugin,
    is_critical_plugin,
    normalize_plugin_slug,
    select_plugin_updates,
    validate_autoupdate_policy,
)
from ecom_ops.wp_ops.review.plans import ReviewPlans
from ecom_ops.wp_ops.review.scaffolds import SCAFFOLD_TEMPLATES, scaffold_work
from ecom_ops.wp_ops.review.workspace_io import iter_files
from ecom_ops.wp_ops.rollback_service import ChangeRollbackService
from ecom_ops.wp_ops.service import WpOpsService
from ecom_ops.wp_ops.ssh_settings import save_deploy_ssh
from ecom_ops.wp_ops.static_checks import run_static_checks


def _site(site_id: str = "mock-staging", environment: str = "staging") -> Site:
    return Site(
        id=site_id,
        display_name=site_id,
        canonical_url="https://staging.example.test/",
        environment=environment,
        business_owner="customer",
        hosting_provider="unconfigured",
        wordpress_version="6.7.1",
        php_version="8.3.12",
        woocommerce=True,
        critical_urls=("https://staging.example.test/",),
        maintenance_window="not agreed",
        secret_ref="WP_SECRET_MOCK_STAGING",
        alert_owner="oscar",
        alert_confirmations=2,
        check_interval_seconds=60,
        timeout_seconds=10,
        dedupe_window_seconds=30,
        retention_days=7,
        capabilities={},
        files_root="/var/www/staging",
        ssh_host="staging.example.test",
    )


class _FakeSftpHandle(io.BytesIO):
    def __init__(self, store: dict[str, bytes], path: str, mode: str) -> None:
        self._store = store
        self._path = path
        self._mode = mode
        initial = store.get(path, b"") if "r" in mode else b""
        super().__init__(initial)

    def close(self) -> None:
        if "w" in self._mode:
            self._store[self._path] = self.getvalue()
        super().close()

    def __exit__(self, *args: Any) -> None:
        self.close()


class _FakeSftpQuarantineClient:
    def __init__(
        self,
        *,
        files: dict[str, bytes] | None = None,
        dirs: set[str] | None = None,
        symlinks: set[str] | None = None,
    ) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.dirs: set[str] = set(
            dirs
            or {
                "/var",
                "/var/www",
                "/var/www/staging",
                "/var/www/staging/wp-content",
                "/var/www/staging/wp-content/plugins",
                "/var/www/staging/wp-content/mu-plugins",
            }
        )
        self.symlinks: set[str] = set(symlinks or set())

    def lstat(self, path: str) -> SimpleNamespace:
        clean = path.rstrip("/") or "/"
        if clean in self.symlinks:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_size=0)
        if clean in self.files:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=len(self.files[clean]))
        if clean in self.dirs or any(k.startswith(clean + "/") for k in self.files):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=4096)
        raise FileNotFoundError(clean)

    def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        names: set[str] = set()
        for entry in list(self.dirs) + list(self.files) + list(self.symlinks):
            if entry.startswith(prefix):
                rest = entry[len(prefix) :]
                if rest and "/" not in rest:
                    names.add(rest)
        return sorted(names)

    def open(self, path: str, mode: str) -> _FakeSftpHandle:
        if "r" in mode and path not in self.files:
            raise FileNotFoundError(path)
        return _FakeSftpHandle(self.files, path, mode)

    def posix_rename(self, src: str, dst: str) -> None:
        if src in self.files:
            self.files[dst] = self.files.pop(src)
            return
        if src in self.dirs:
            self.dirs.remove(src)
            self.dirs.add(dst)
            moved_files = {k: v for k, v in self.files.items() if k.startswith(src + "/")}
            for k, v in moved_files.items():
                del self.files[k]
                self.files[dst + k[len(src) :]] = v
            return
        raise FileNotFoundError(src)


# --- 1. Env Gates & Protected Overlay Keys ------------------------------ #


def test_plugin_env_gates_are_protected_overlay_keys() -> None:
    for key in (
        "AZOM_WP_PLUGIN_UPDATES_ENABLED",
        "AZOM_WP_AUTOUPDATE_WRITE_ENABLED",
        "AZOM_WP_PLUGIN_MUTATE_KILL",
    ):
        assert key in PROTECTED_OVERLAY_KEYS
        assert overlay_key_allowed(key) is False


# --- 2. Plugin Policy, Semver Risk, Selection & Canary Ordering --------- #


def test_plugin_policy_classification_selection_and_canary_order() -> None:
    assert BRIDGE_PLUGIN_SLUG in CRITICAL_PLUGIN_SLUGS
    assert is_bridge_plugin("azom-ops-bridge/azom-ops-bridge.php") is True
    assert is_critical_plugin("woocommerce/woocommerce.php") is True
    assert is_critical_plugin("klarna-checkout-for-woocommerce") is True
    assert is_critical_plugin("litespeed-cache") is False
    assert normalize_plugin_slug("WordPress-SEO/wp-seo.php") == "wordpress-seo"

    assert classify_update_risk("1.2.3", "2.0.0") == "major"
    assert classify_update_risk("1.2.3", "1.3.0") == "minor"
    assert classify_update_risk("1.2.3", "1.2.4") == "patch"
    assert classify_update_risk("1.2.3", "1.2.4", security_advisory=True) == "security"
    assert classify_update_risk("1.2.3", "1.2.3") == "unknown"
    assert classify_update_risk("", "1.2.4") == "unknown"

    tr = InMemoryWpBridgeTransport()
    plugins = list(tr.plugins.values())

    # non_critical_only excludes WooCommerce, Klarna, and azom-ops-bridge
    sel_nc = select_plugin_updates(
        plugins, selection_mode="policy", policy="non_critical_only", canary=True
    )
    slugs_nc = [p["slug"] for p in sel_nc["selected"]]
    assert "woocommerce" not in slugs_nc
    assert "klarna-checkout-for-woocommerce" not in slugs_nc
    assert "azom-ops-bridge" not in slugs_nc
    assert set(slugs_nc) == {"litespeed-cache", "wordpress-seo", "contact-form-7"}
    assert sel_nc["batches"][0]["stage"] == "canary"
    assert len(sel_nc["batches"][0]["plugins"]) == 1

    # patch_only excludes Yoast major (23.4 -> 24.0)
    sel_patch = select_plugin_updates(
        plugins, selection_mode="policy", policy="patch_only", canary=True
    )
    assert "wordpress-seo" not in [p["slug"] for p in sel_patch["selected"]]
    assert set(p["slug"] for p in sel_patch["selected"]) == {"litespeed-cache", "contact-form-7"}

    # all + include_critical puts non-critical first, critical last in isolated 1-plugin batches
    sel_all = select_plugin_updates(
        plugins, selection_mode="all", include_critical=True, canary=True, exclude_slugs=["wordpress-seo"]
    )
    assert sel_all["has_critical"] is True
    assert sel_all["batches"][0]["stage"] == "canary"
    assert sel_all["batches"][-1]["stage"] == "critical"
    assert len(sel_all["batches"][-1]["plugins"]) == 1
    assert "wordpress-seo" not in [p["slug"] for p in sel_all["selected"]]

    # single mode requires 1 slug
    sel_single = select_plugin_updates(plugins, selection_mode="single", slugs=["litespeed-cache"])
    assert [p["slug"] for p in sel_single["selected"]] == ["litespeed-cache"]
    with pytest.raises(ValueError, match="exactly one"):
        select_plugin_updates(plugins, selection_mode="single", slugs=["a", "b"])


def test_autoupdate_policy_validation_and_drift_detection() -> None:
    pol = default_autoupdate_policy("mock-staging")
    assert pol["preset"] == "non_critical_only"
    assert BRIDGE_PLUGIN_SLUG in pol["deny_slugs"]

    with pytest.raises(ValueError, match="azom-ops-bridge"):
        validate_autoupdate_policy(
            "mock-staging", {"preset": "allowlist_only", "allow_slugs": ["azom-ops-bridge"]}
        )

    tr = InMemoryWpBridgeTransport()
    tr.constants["automatic_updater_disabled"] = True
    tr.autoupdate_filters = [{"priority": 10, "callback": "custom_force_filter"}]
    drift = evaluate_autoupdate_drift(
        list(tr.plugins.values()),
        pol,
        constants=tr.constants,
        autoupdate_filters=tr.autoupdate_filters,
    )
    assert drift["in_sync"] is False
    # wordpress-seo & contact-form-7 are non-critical with auto_update=False -> drift
    drifted_slugs = {d["slug"] for d in drift["drift_items"]}
    assert {"wordpress-seo", "contact-form-7"} <= drifted_slugs
    assert "woocommerce" not in drifted_slugs
    assert len(drift["override_warnings"]) >= 2


# --- 3. PluginOpsService / WpOpsService End-to-End Lifecycle ------------ #


def _make_service(
    tmp_path: Path,
    *,
    transport: InMemoryWpBridgeTransport | None = None,
    clock=None,
) -> tuple[WpOpsService, InMemoryWpBridgeTransport]:
    tr = transport or InMemoryWpBridgeTransport(site_id="mock-staging")
    svc = WpOpsService(
        data_dir=tmp_path,
        clock=clock,
        bridge_factory=lambda site: WpBridgeClient(site_id=site.id, transport=tr),
    )
    return svc, tr


def test_inventory_mu_dropins_and_diagnostics_rbac(tmp_path: Path, wp_env) -> None:
    wp_env(mock="1", files_ssh="0")
    svc, _tr = _make_service(tmp_path)

    inv = svc.plugin_inventory("mock-staging", actor="jonatan", include_updates=True, include_wporg=True)
    assert inv.ok is True
    assert inv.data["summary"]["total"] >= 5
    assert inv.data["summary"]["updates_available"] >= 4
    woo_row = next(p for p in inv.data["plugins"] if p["slug"] == "woocommerce")
    assert woo_row["is_critical"] is True
    assert woo_row["wporg"]["source"] == "mock"

    mu = svc.mu_plugins_and_dropins("mock-staging", actor="jonatan")
    assert mu.ok is True
    assert mu.data["dropins_read_only"] is True
    assert mu.data["mu_count"] == 2
    assert mu.data["dropin_count"] == 2

    # Non-admin viewer gets redacted debug_log_tail
    diag_viewer = svc.plugin_diagnostics("mock-staging", actor="jonatan")
    assert diag_viewer.ok is True
    assert diag_viewer.data["debug_log_redacted"] is True
    assert diag_viewer.data["debug_log_tail"] == []

    # Admin Oscar gets full scrubbed debug_log_tail + hook_map
    diag_admin = svc.plugin_diagnostics("mock-staging", actor="oscar", hooks=["woocommerce_checkout_process"])
    assert diag_admin.ok is True
    assert diag_admin.data["debug_log_redacted"] is False
    assert len(diag_admin.data["debug_log_tail"]) >= 1
    assert "woocommerce_checkout_process" in diag_admin.data["hook_map"]


def test_plugin_update_plan_execute_canary_stop_and_rollback(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    now = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
    clock_box = [now]
    svc, tr = _make_service(tmp_path, clock=lambda: clock_box[0])

    # Plan single non-critical plugin update
    planned = svc.plan_plugin_updates(
        "mock-staging", actor="agent", selection_mode="single", slugs=["litespeed-cache"]
    )
    assert planned.ok is True
    plan_id = planned.data["plan_id"]
    plan_sha = planned.data["plan_sha256"]
    assert plan_id.startswith("plu-")
    assert planned.data["executed"] is False
    assert planned.data["approval_required"] is True

    # List plans returns it
    listed = svc.list_plugin_update_plans("mock-staging", actor="jonatan")
    assert listed.ok is True
    assert any(p["plan_id"] == plan_id for p in listed.data["plans"])

    # Viewer cannot execute
    denied = svc.execute_plugin_update_plan(
        "mock-staging", plan_id, actor="jonatan", expected_sha256=plan_sha
    )
    assert denied.ok is False and denied.data["error_class"] == "AccessDenied"

    # Kill switch blocks execution
    monkeypatch.setenv("AZOM_WP_PLUGIN_MUTATE_KILL", "1")
    killed = svc.execute_plugin_update_plan(
        "mock-staging", plan_id, actor="oscar", expected_sha256=plan_sha
    )
    assert killed.ok is False and killed.data["error_class"] == "PluginMutateKillSwitchActive"
    monkeypatch.delenv("AZOM_WP_PLUGIN_MUTATE_KILL", raising=False)

    # Wrong SHA rejected
    bad_sha = svc.execute_plugin_update_plan(
        "mock-staging", plan_id, actor="oscar", expected_sha256="0" * 64
    )
    assert bad_sha.ok is False and bad_sha.data["error_class"] == "DigestMismatch"

    # Live gate fail-closed when mock=0 and AZOM_WP_PLUGIN_UPDATES_ENABLED unset
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WP_PLUGIN_UPDATES_ENABLED", raising=False)
    live_blocked = svc.execute_plugin_update_plan(
        "mock-staging", plan_id, actor="oscar", expected_sha256=plan_sha
    )
    assert live_blocked.ok is False and live_blocked.data["error_class"] == "PluginUpdatesDisabled"
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    # Execute succeeds + records Change Vault plugin_update
    exec_res = svc.execute_plugin_update_plan(
        "mock-staging", plan_id, actor="oscar", expected_sha256=plan_sha
    )
    assert exec_res.ok is True
    assert exec_res.data["executed"] is True
    assert tr.plugins["litespeed-cache/litespeed-cache.php"]["version"] == "6.5.2"
    chg_id = exec_res.data["change_ids"][0]
    ledger = ChangeLedger(data_dir=tmp_path)
    rec = ledger.get_change("mock-staging", chg_id)
    assert rec is not None and rec["category"] == "plugin_update"

    # Rollback plugin_update via ChangeRollbackService restores 6.5.0
    rb = ChangeRollbackService(data_dir=tmp_path).execute_rollback(
        "mock-staging",
        chg_id,
        actor="oscar",
        expected_before_sha256=rec["before_sha256"],
        bridge_client=WpBridgeClient(site_id="mock-staging", transport=tr),
    )
    assert rb.ok is True
    assert tr.plugins["litespeed-cache/litespeed-cache.php"]["version"] == "6.5.0"

    # Critical plugin plan requires allow_critical=True at execution
    crit_plan = svc.plan_plugin_updates(
        "mock-staging",
        actor="oscar",
        selection_mode="single",
        slugs=["woocommerce"],
        include_critical=True,
    )
    assert crit_plan.ok is True and crit_plan.data["has_critical"] is True
    need_crit = svc.execute_plugin_update_plan(
        "mock-staging",
        crit_plan.data["plan_id"],
        actor="oscar",
        expected_sha256=crit_plan.data["plan_sha256"],
        allow_critical=False,
    )
    assert need_crit.ok is False and need_crit.data["error_class"] == "CriticalApprovalRequired"

    # Canary stop-on-fail halts before remaining plugins
    tr.fail_probe_for_plugins.add("contact-form-7/wp-contact-form-7.php")
    canary_plan = svc.plan_plugin_updates(
        "mock-staging",
        actor="oscar",
        selection_mode="selected",
        slugs=["contact-form-7", "wordpress-seo"],
        canary=True,
    )
    assert canary_plan.ok is True
    # contact-form-7 is inactive patch -> selected as canary first
    halted = svc.execute_plugin_update_plan(
        "mock-staging",
        canary_plan.data["plan_id"],
        actor="oscar",
        expected_sha256=canary_plan.data["plan_sha256"],
    )
    assert halted.ok is False
    assert halted.data["error_class"] == "CanaryUpdateHalted"
    assert halted.data["halted_at"] == "contact-form-7"
    assert tr.plugins["wordpress-seo/wp-seo.php"]["version"] == "23.4"

    # Expired plan (after 12h TTL) is refused
    clock_box[0] = now + timedelta(hours=13)
    expired = svc.execute_plugin_update_plan(
        "mock-staging",
        crit_plan.data["plan_id"],
        actor="oscar",
        expected_sha256=crit_plan.data["plan_sha256"],
        allow_critical=True,
    )
    assert expired.ok is False and expired.data["error_class"] == "Expired"


def test_autoupdate_policy_plan_execute_and_rollback(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    svc, tr = _make_service(tmp_path)

    # Save policy refuses azom-ops-bridge
    bad_save = svc.save_autoupdate_policy(
        "mock-staging",
        actor="oscar",
        policy={"preset": "allowlist_only", "allow_slugs": ["azom-ops-bridge"]},
    )
    assert bad_save.ok is False and bad_save.data["error_class"] == "BridgeSelfProtectionRefused"

    saved = svc.save_autoupdate_policy(
        "mock-staging",
        actor="oscar",
        policy={"preset": "allowlist_only", "allow_slugs": ["litespeed-cache", "wordpress-seo"]},
    )
    assert saved.ok is True

    drift = svc.check_autoupdate_drift("mock-staging", actor="jonatan")
    assert drift.ok is True and drift.data["in_sync"] is False

    # Critical autoupdate requires allow_critical=True
    crit_refused = svc.plan_plugin_autoupdate(
        "mock-staging",
        actor="oscar",
        changes=[{"plugin_file": "woocommerce/woocommerce.php", "enabled": True}],
        allow_critical=False,
    )
    assert crit_refused.ok is False and crit_refused.data["error_class"] == "CriticalApprovalRequired"

    pau = svc.plan_plugin_autoupdate(
        "mock-staging",
        actor="oscar",
        changes=[{"plugin_file": "wordpress-seo/wp-seo.php", "enabled": True}],
    )
    assert pau.ok is True
    assert pau.data["plan_id"].startswith("pau-")
    assert pau.data["executed"] is False

    # Live gate check
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WP_AUTOUPDATE_WRITE_ENABLED", raising=False)
    blocked = svc.execute_plugin_autoupdate_plan(
        "mock-staging",
        pau.data["plan_id"],
        actor="oscar",
        expected_sha256=pau.data["plan_sha256"],
    )
    assert blocked.ok is False and blocked.data["error_class"] == "AutoupdateWriteDisabled"
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    exec_pau = svc.execute_plugin_autoupdate_plan(
        "mock-staging",
        pau.data["plan_id"],
        actor="oscar",
        expected_sha256=pau.data["plan_sha256"],
    )
    assert exec_pau.ok is True
    assert tr.plugins["wordpress-seo/wp-seo.php"]["auto_update"] is True
    chg_id = exec_pau.data["change_id"]
    rec = ChangeLedger(data_dir=tmp_path).get_change("mock-staging", chg_id)
    assert rec is not None and rec["category"] == "plugin_autoupdate"

    rb = ChangeRollbackService(data_dir=tmp_path).execute_rollback(
        "mock-staging",
        chg_id,
        actor="oscar",
        expected_before_sha256=rec["before_sha256"],
        bridge_client=WpBridgeClient(site_id="mock-staging", transport=tr),
    )
    assert rb.ok is True
    assert tr.plugins["wordpress-seo/wp-seo.php"]["auto_update"] is False


def test_recovery_plan_quarantine_and_bridge_self_protection(
    tmp_path: Path, wp_env
) -> None:
    wp_env(mock="1", files_ssh="0")
    svc, tr = _make_service(tmp_path)

    # Seed mock plugin & mu-plugin on LocalTree
    mock_plugins = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "plugins" / "broken-plugin"
    mock_plugins.mkdir(parents=True)
    (mock_plugins / "broken-plugin.php").write_text("<?php\n// broken\n", encoding="utf-8")
    mock_mu = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "mu-plugins"
    mock_mu.mkdir(parents=True)
    (mock_mu / "bad-mu.php").write_text("<?php\n// bad mu\n", encoding="utf-8")

    # azom-ops-bridge is always refused
    bridge_q = svc.quarantine_plugin("mock-staging", "azom-ops-bridge", actor="oscar")
    assert bridge_q.ok is False and bridge_q.data["error_class"] == "BridgeSelfProtectionRefused"
    bridge_plan = svc.plan_plugin_recovery(
        "mock-staging", actor="oscar", slug="azom-ops-bridge", action="deactivate"
    )
    assert bridge_plan.ok is False and bridge_plan.data["error_class"] == "BridgeSelfProtectionRefused"

    # Recovery plan: deactivate
    deact_plan = svc.plan_plugin_recovery(
        "mock-staging", actor="oscar", slug="wordpress-seo/wp-seo.php", action="deactivate"
    )
    assert deact_plan.ok is True and deact_plan.data["plan_id"].startswith("plr-")
    deact_exec = svc.execute_plugin_recovery_plan(
        "mock-staging",
        deact_plan.data["plan_id"],
        actor="oscar",
        expected_sha256=deact_plan.data["plan_sha256"],
    )
    assert deact_exec.ok is True
    assert tr.plugins["wordpress-seo/wp-seo.php"]["active"] is False

    # Recovery plan: quarantine_sftp + restore_from_quarantine
    q_plan = svc.plan_plugin_recovery(
        "mock-staging", actor="oscar", slug="broken-plugin", action="quarantine_sftp"
    )
    assert q_plan.ok is True
    q_exec = svc.execute_plugin_recovery_plan(
        "mock-staging",
        q_plan.data["plan_id"],
        actor="oscar",
        expected_sha256=q_plan.data["plan_sha256"],
    )
    assert q_exec.ok is True
    assert not mock_plugins.exists()
    assert q_exec.data["quarantine_name"].startswith("broken-plugin.azom-quarantine-")

    u_exec = svc.unquarantine_plugin(
        "mock-staging", "broken-plugin", actor="oscar", quarantine_name=q_exec.data["quarantine_name"]
    )
    assert u_exec.ok is True
    assert mock_plugins.is_dir()

    # MU-plugin file quarantine & unquarantine
    mu_q = svc.quarantine_plugin("mock-staging", "bad-mu.php", actor="oscar", kind="mu_plugin")
    assert mu_q.ok is True
    assert not (mock_mu / "bad-mu.php").exists()
    assert not mu_q.data["quarantine_name"].endswith(".php")
    mu_u = svc.unquarantine_plugin("mock-staging", "bad-mu.php", actor="oscar", kind="mu_plugin")
    assert mu_u.ok is True
    assert (mock_mu / "bad-mu.php").is_file()


# --- 4. SFTP Quarantine, debug.log Tail & Read-Only Reader Invariant ---- #


def test_sftp_reader_mu_plugins_readonly_and_deployer_quarantine(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    site = _site("mock-staging")

    # SshFileTransport accepts mu-plugins/ reads & listings but NEVER writes
    reader = SshFileTransport(site, tmp_path)
    assert reader._safe_path("/var/www/staging/wp-content/mu-plugins", listing=True)
    assert reader._safe_path("/var/www/staging/wp-content/mu-plugins/azom-loader.php", listing=False)
    assert reader._safe_path("/var/www/staging/wp-content/mu-plugins/azom-mu/hooks.php", listing=False)
    with pytest.raises(TransportError, match="SshWriteDisabled"):
        reader.write_bytes("/var/www/staging/wp-content/mu-plugins/azom-loader.php", b"x")
    with pytest.raises(TransportError, match="SshWriteDisabled"):
        reader.create_bytes("/var/www/staging/wp-content/mu-plugins/azom-loader.php", b"x")

    # SshDeployTransport quarantine/unquarantine/list/debug.log
    save_deploy_ssh(
        tmp_path,
        site,
        deploy_ssh_user="azom-wp-deployer",
        files_root="/var/www/staging",
        ssh_host="staging.example.test",
        ssh_port="22",
    )
    fake_sftp = _FakeSftpQuarantineClient(
        files={
            "/var/www/staging/wp-content/plugins/faulty/faulty.php": b"<?php\n",
            "/var/www/staging/wp-content/mu-plugins/mu-bad.php": b"<?php\n",
            "/var/www/staging/wp-content/debug.log": (
                b"Fatal error in /var/www/staging/wp-content/plugins/faulty/faulty.php token=sk-secret1234567890\n"
            ),
        },
        dirs={
            "/var",
            "/var/www",
            "/var/www/staging",
            "/var/www/staging/wp-content",
            "/var/www/staging/wp-content/plugins",
            "/var/www/staging/wp-content/plugins/faulty",
            "/var/www/staging/wp-content/mu-plugins",
        },
        symlinks={"/var/www/staging/wp-content/plugins/link-plugin"},
    )
    dt = SshDeployTransport(site, tmp_path)

    @contextmanager
    def _fake_conn():
        yield fake_sftp

    monkeypatch.setattr(dt, "_session", _fake_conn)

    # Bridge & symlink refused
    with pytest.raises(TransportError, match="BridgeSelfProtectionRefused"):
        dt.quarantine_plugin("azom-ops-bridge")
    with pytest.raises(PathRejected, match="symlink"):
        dt.quarantine_plugin("link-plugin")

    # Kill switch blocks quarantine
    monkeypatch.setenv("AZOM_WP_PLUGIN_MUTATE_KILL", "1")
    with pytest.raises(TransportError, match="PluginMutateKillSwitchActive"):
        dt.quarantine_plugin("faulty")
    monkeypatch.delenv("AZOM_WP_PLUGIN_MUTATE_KILL", raising=False)

    q_res = dt.quarantine_plugin("faulty", reason="WSOD")
    assert q_res["ok"] is True
    mu_res = dt.quarantine_mu_plugin("mu-bad.php")
    assert mu_res["ok"] is True

    listed = dt.list_quarantined()
    assert len(listed["plugins"]) == 1 and listed["plugins"][0]["target"] == "faulty"
    assert len(listed["mu_plugins"]) == 1 and listed["mu_plugins"][0]["target"] == "mu-bad.php"

    assert dt.unquarantine_plugin("faulty")["ok"] is True
    assert dt.unquarantine_mu_plugin("mu-bad.php")["ok"] is True

    tail = dt.read_debug_log_tail(max_lines=20)
    assert tail["ok"] is True and tail["exists"] is True
    assert "<WP_ROOT>" in tail["lines"][0]
    assert "/var/www/staging" not in tail["lines"][0]


# --- 5. First-Class MU-Plugin, Custom azom-* Plugins, Scaffolds & Static Checks --- #


def test_mu_plugin_paths_review_deploy_rollback_and_custom_azom_slugs(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", passing_catalog)

    assert "mu_plugin" in COMPONENT_KINDS
    assert components_parent("/var/www/site", "mu_plugin") == "/var/www/site/wp-content/mu-plugins"
    assert component_dir("/var/www/site", "mu_plugin", "azom-mu-test") == "/var/www/site/wp-content/mu-plugins/azom-mu-test"
    assert resolve_file("/var/www/site", "mu_plugin", "azom-mu-test", "loader.php") == (
        "/var/www/site/wp-content/mu-plugins/azom-mu-test/loader.php"
    )
    assert resolve_mu_root_file("/var/www/site", "azom-loader.php") == (
        "/var/www/site/wp-content/mu-plugins/azom-loader.php"
    )

    plans = ReviewPlans(data_dir=tmp_path)

    # WooCoreRefused & ThirdPartyPluginRefused & BridgeSelfProtectionRefused preserved
    assert plans.open("mock-staging", "plugin", "woocommerce", actor="oscar").data["error_class"] == "WooCoreRefused"
    assert plans.open("mock-staging", "plugin", "akismet", actor="oscar").data["error_class"] == "ThirdPartyPluginRefused"
    assert (
        plans.open("mock-staging", "plugin", "azom-ops-bridge", actor="oscar").data["error_class"]
        == "BridgeSelfProtectionRefused"
    )

    # Allowlisted custom azom-* plugin works
    custom = plans.open("mock-staging", "plugin", "azom-custom-badges", actor="oscar")
    assert custom.ok is True

    # First-class kind="mu_plugin" opens, scaffolds, deploys via Bridge with category="mu_plugin_deploy"
    mu_plan = plans.open("mock-staging", "mu_plugin", "azom-mu-guard", actor="oscar")
    assert mu_plan.ok is True
    plan_id = mu_plan.data["plan_id"]
    sc = plans.workspace_scaffold("mock-staging", plan_id, "mu_plugin_loader", actor="oscar")
    assert sc.ok is True
    chk = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert chk.ok is True
    row = plans.get(plan_id, site_id="mock-staging")
    assert row is not None

    # Legacy request_production still refuses with BackupNotVerified
    prod_refused = plans.request_production(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=row["base_sha256"],
        work_sha256=row["work_sha256"],
    )
    assert prod_refused.ok is False and prod_refused.data["error_class"] == "BackupNotVerified"

    # DeployOrchestrator deploys mu_plugin via Bridge & records mu_plugin_deploy
    tr = InMemoryWpBridgeTransport(site_id="mock-staging")
    bc = WpBridgeClient(site_id="mock-staging", transport=tr)
    dep = DeployOrchestrator(data_dir=tmp_path).deploy_workspace(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=row["base_sha256"],
        work_sha256=row["work_sha256"],
        transport_mode="bridge",
        bridge_client=bc,
        health_check_fn=lambda: True,
    )
    assert dep.ok is True
    chg_id = dep.data["change_id"]
    rec = ChangeLedger(data_dir=tmp_path).get_change("mock-staging", chg_id)
    assert rec is not None and rec["category"] == "mu_plugin_deploy"
    assert "mu_plugin/azom-mu-guard/includes/bootstrap.php" in tr.files

    # Rollback removes created MU-plugin file
    rb = ChangeRollbackService(data_dir=tmp_path).execute_rollback(
        "mock-staging",
        chg_id,
        actor="oscar",
        expected_before_sha256=rec["before_sha256"],
        bridge_client=bc,
    )
    assert rb.ok is True
    assert "mu_plugin/azom-mu-guard/includes/bootstrap.php" not in tr.files


def test_all_seven_scaffolds_pass_static_checks_and_new_rules_detect_hazards(tmp_path: Path) -> None:
    expected = {
        "child_theme_basics",
        "site_plugin_basics",
        "woo_compat_declaration",
        "mu_plugin_basics",
        "mu_plugin_loader",
        "plugin_hook_extension",
        "custom_plugin_full",
    }
    assert SCAFFOLD_TEMPLATES == expected

    for template in sorted(expected):
        ws = tmp_path / template
        (ws / "base").mkdir(parents=True)
        (ws / "work").mkdir(parents=True)
        res = scaffold_work(ws, template, component_slug="azom-demo")
        assert res["created"] or res["updated"]
        files = [(rel, data.decode("utf-8")) for rel, data in iter_files(ws / "work")]
        kind = "mu_plugin" if template.startswith("mu_") else ("theme" if template.startswith("child_") else "plugin")
        checked = run_static_checks(files, component_kind=kind)
        assert checked["ok"] is True, f"Scaffold {template} failed static checks: {checked['findings']}"

    # mu_activation_hook_noop
    mu_bad = run_static_checks(
        {"mu-plugins/azom-bad.php": "<?php\ndefined('ABSPATH') || exit;\nregister_activation_hook(__FILE__, 'fn');\n"}
    )
    assert any(f["rule"] == "mu_activation_hook_noop" for f in mu_bad["findings"])

    # unguarded_woo_call + missing_hpos_declaration
    woo_bad = run_static_checks(
        {"azom-orders.php": "<?php\ndefined('ABSPATH') || exit;\n$o = wc_get_order(123);\n"}
    )
    rules = {f["rule"] for f in woo_bad["findings"]}
    assert "unguarded_woo_call" in rules
    assert "missing_hpos_declaration" in rules

    # Guarded + HPOS-declared passes cleanly
    woo_good = run_static_checks(
        {
            "azom-orders.php": (
                "<?php\ndefined('ABSPATH') || exit;\n"
                "add_action('before_woocommerce_init', static function (): void {\n"
                "    \\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::declare_compatibility('custom_order_tables', __FILE__, true);\n"
                "});\n"
                "if (class_exists('WooCommerce')) { $o = wc_get_order(123); }\n"
            )
        }
    )
    assert woo_good["ok"] is True


def test_legacy_restore_still_refused_and_zero_banned_tokens_in_wp_ops(tmp_path: Path, wp_env) -> None:
    wp_env(mock="1", files_ssh="0")
    svc = WpOpsService(data_dir=tmp_path)
    res = svc.restore("mock-staging", actor="oscar", change_ref="CHG-1")
    assert res.ok is False and res.data["error_class"] == "RestoreNotInMvp"

    wp_ops_dir = Path(__file__).resolve().parents[2] / "skills" / "ecom_ops" / "wp_ops"
    banned = (
        "urllib." + "request",
        "url" + "open",
        "http." + "client",
        "re" + "quests",
        "sub" + "process",
        "os." + "system",
        "integrations." + "wordpress",
    )
    for py_file in wp_ops_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"Banned token {token!r} found in {py_file}"


def test_b1_09_plan_plugin_recovery_resolves_real_plugin_file_and_checks_snapshot_slug(
    tmp_path: Path, wp_env
) -> None:
    wp_env(mock="1", files_ssh="0")
    svc, _tr = _make_service(tmp_path)

    # wordpress-seo has main file wordpress-seo/wp-seo.php (NOT wordpress-seo/wordpress-seo.php)
    planned = svc.plan_plugin_recovery(
        "mock-staging",
        actor="oscar",
        slug="wordpress-seo",
        action="deactivate",
    )
    assert planned.ok is True
    assert planned.data["plan"]["plugin_file"] == "wordpress-seo/wp-seo.php"

    # Execute deactivate recovery plan succeeds against real plugin_file
    executed = svc.execute_plugin_recovery_plan(
        "mock-staging",
        planned.data["plan_id"],
        actor="oscar",
        expected_sha256=planned.data["plan_sha256"],
    )
    assert executed.ok is True

    # rollback_snapshot rejects a change_id belonging to a DIFFERENT plugin slug
    ledger = ChangeLedger(data_dir=tmp_path)
    yoast_chg = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="plugin_update",
        transport="bridge",
        actor="oscar",
        summary="Uppdaterade wordpress-seo",
        target_ref="plugin:wordpress-seo",
        before_data={"slug": "wordpress-seo", "plugin_file": "wordpress-seo/wp-seo.php", "backup_id": "pbackup-chg-1234567890ab-wordpress-seo.zip"},
        after_data={"slug": "wordpress-seo", "plugin_file": "wordpress-seo/wp-seo.php"},
        metadata={"slug": "wordpress-seo", "plugin_file": "wordpress-seo/wp-seo.php", "backup_id": "pbackup-chg-1234567890ab-wordpress-seo.zip"},
    )
    mismatched = svc.plan_plugin_recovery(
        "mock-staging",
        actor="oscar",
        slug="litespeed-cache",
        action="rollback_snapshot",
        change_id=yoast_chg["change_id"],
    )
    assert mismatched.ok is False
    assert mismatched.data["error_class"] == "SlugMismatch"
