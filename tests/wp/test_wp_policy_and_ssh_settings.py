"""Unit tests for PR 1.4: WpOpsPolicy (RBAC/site/lock/audit) and deduplicated ssh_settings."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops import locks
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.policy import WpOpsPolicy
from ecom_ops.wp_ops.ssh_settings import (
    SettingsRejected,
    SshSettingsInput,
    apply_ssh_overlay,
    describe_ssh,
    load_overlay,
    save_ssh,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_REGISTRY = REPO_ROOT / "config" / "wp_sites.mock.yaml"
VALID_HOST_KEY = "ssh-ed25519 " + base64.b64encode(b"fake-ed25519-key-material").decode("ascii") + " test@host"
FAKE_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


def _make_site(site_id: str = "shop-stage", environment: str = "staging"):
    return parse_site(
        {
            "id": site_id,
            "display_name": "Shop Stage",
            "canonical_url": "https://stage.example.test",
            "environment": environment,
            "business_owner": "customer",
            "hosting_provider": "hetzner",
            "wordpress_version": "6.7",
            "php_version": "8.3",
            "woocommerce": True,
            "critical_urls": ["https://stage.example.test/"],
            "maintenance_window": "02:00-04:00",
            "secret_ref": "WP_SECRET_SHOP_STAGE",
            "files_root": "/var/www/shop-stage",
            "ssh_host": "stage.internal",
            "alert_owner": "oscar",
            "alert_confirmations": 2,
            "check_interval_seconds": 300,
            "timeout_seconds": 10,
            "dedupe_window_seconds": 30,
            "capabilities": {
                "hosting_api": "unknown",
                "wp_cli": "unknown",
                "staging": "available",
                "backup": "unknown",
                "restore": "unknown",
                "uptime": "available",
                "security_scanner": "unknown",
            },
        }
    )


def test_wp_ops_policy_rbac_deny_missing_site_and_audit_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    events: list[dict[str, Any]] = []

    def _capture_audit(**kwargs: Any) -> None:
        events.append(kwargs)

    policy = WpOpsPolicy(
        registry_path=MOCK_REGISTRY,
        data_dir=tmp_path,
        audit_fn=_capture_audit,
    )

    # Missing site
    site, err = policy.resolve_site("does-not-exist", actor="oscar", access="read")
    assert site is None
    assert err is not None and err.ok is False
    assert err.data == {"error_class": "UnknownSite", "site_id": "does-not-exist", "live_calls": False}

    # Jonatan (viewer) can read staging/prod, but is denied operate on staging and file_write on prod
    site_ok, err_ok = policy.resolve_site("mock-staging", actor="jonatan", access="read")
    assert err_ok is None and site_ok is not None and site_ok.id == "mock-staging"

    site_denied, err_denied = policy.resolve_site(
        "mock-staging", actor="jonatan", access="operate", action="wp_ops_check"
    )
    assert site_denied is None
    assert err_denied is not None and err_denied.data["error_class"] == "AccessDenied"
    assert len(events) == 1
    assert events[0] == {
        "actor": "jonatan",
        "action": "wp_ops_check",
        "target": "wp_sites",
        "target_id": "mock-staging",
        "success": False,
        "details": {
            "error_class": "AccessDenied",
            "permission": Permission.WP_OPS_OPERATE.value,
            "executed": False,
        },
    }

    # Agent (operator) can operate/file_write on staging, denied on production (requires WP_OPS_ADMIN)
    st_site, st_err = policy.resolve_site(
        "mock-staging", actor="agent", access="file_write", action="wp_ops_file_edit"
    )
    assert st_err is None and st_site is not None

    prod_site, prod_err = policy.resolve_site(
        "mock-shop", actor="agent", access="file_write", action="wp_ops_file_edit"
    )
    assert prod_site is None
    assert prod_err is not None and prod_err.data["error_class"] == "AccessDenied"
    assert events[-1]["details"]["permission"] == Permission.WP_OPS_ADMIN.value

    # Oscar (full_admin) passes file_write on production
    oscar_prod, oscar_err = policy.resolve_site(
        "mock-shop", actor="oscar", access="file_write", action="wp_ops_file_edit"
    )
    assert oscar_err is None and oscar_prod is not None and oscar_prod.id == "mock-shop"


def test_wp_ops_policy_lock_contention_and_audit_unavailable_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    events: list[dict[str, Any]] = []
    policy = WpOpsPolicy(
        registry_path=MOCK_REGISTRY,
        data_dir=tmp_path,
        audit_fn=lambda **kw: events.append(kw),
    )
    site, err = policy.resolve_site("mock-staging", actor="agent", access="operate")
    assert err is None and site is not None

    # Acquire lock externally -> try_site_lock yields SiteBusy and audits with exact schema
    with locks.hold_site(site.id):
        with policy.try_site_lock(
            site, actor="agent", action="wp_ops_check", operation="health_check"
        ) as busy:
            assert busy is not None and busy.ok is False
            assert busy.data["error_class"] == "SiteBusy"
            assert busy.data["site_id"] == site.id
            assert busy.data["executed"] is False
            assert busy.data["live_calls"] is False

    assert len(events) == 1
    assert events[0]["action"] == "wp_ops_check"
    assert events[0]["target"] == "wp_sites"
    assert events[0]["target_id"] == site.id
    assert events[0]["success"] is False
    assert events[0]["details"] == {
        "environment": "staging",
        "permission": Permission.WP_OPS_OPERATE.value,
        "operation": "health_check",
        "plan_id": None,
        "error_class": "SiteBusy",
        "executed": False,
    }

    # Uncontested lock succeeds and releases
    with policy.try_site_lock(site, actor="agent", action="wp_ops_check") as busy2:
        assert busy2 is None

    # Audit failure fails closed with AuditUnavailable
    broken_policy = WpOpsPolicy(
        registry_path=MOCK_REGISTRY,
        data_dir=tmp_path,
        audit_fn=lambda **_kw: (_ for _ in ()).throw(OSError("disk full")),
    )
    res_audit = broken_policy.audit("oscar", action="wp_ops_check", target_id="mock-staging", success=True, details={})
    assert res_audit is not None and res_audit.ok is False
    assert res_audit.data["error_class"] == "AuditUnavailable"

    denied_broken = broken_policy.require("jonatan", Permission.WP_OPS_ADMIN, site=site, action="wp_ops_check")
    assert denied_broken is not None and denied_broken.data["error_class"] == "AuditUnavailable"


def test_ssh_settings_unified_validation_persistence_and_input_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    site = _make_site()

    # SshSettingsInput dataclass is importable and holds the structured settings fields
    bundle = SshSettingsInput(
        files_root="/var/www/custom-root",
        ssh_host="custom.internal",
        ssh_user="azom-wp-reader",
        ssh_port="2222",
        ssh_identity_file="",
        private_key="",
        clear_key=False,
        global_user="",
        global_port="",
        global_identity_file="",
        host_public_key=VALID_HOST_KEY,
    )
    assert bundle.ssh_user == "azom-wp-reader"

    saved = save_ssh(
        tmp_path,
        site,
        files_root="/var/www/custom-root",
        ssh_host="custom.internal",
        ssh_user="azom-wp-reader",
        ssh_port="2222",
        ssh_identity_file="",
        private_key=FAKE_PRIVATE_KEY,
        clear_key=False,
        global_user="",
        global_port="",
        global_identity_file="",
        host_public_key=VALID_HOST_KEY,
    )
    assert saved["identity_present"] is True
    assert "global_notice" not in saved
    assert set(saved["changed"]) >= {"files_root", "ssh_host", "ssh_user", "ssh_port", "host_public_key", "ssh_key"}

    overlay = load_overlay(tmp_path)
    assert overlay[site.id]["files_root"] == "/var/www/custom-root"
    assert overlay[site.id]["ssh_port"] == 2222
    assert "PRIVATE KEY" not in (tmp_path / "wp_ssh.json").read_text(encoding="utf-8")

    applied = apply_ssh_overlay((site,), tmp_path)[0]
    assert applied.files_root == "/var/www/custom-root"
    assert applied.ssh_user == "azom-wp-reader"
    view = describe_ssh(applied, site, tmp_path, include_paths=True)
    assert view["mock"] is True
    assert view["ssh_enabled"] is False
    assert view["ssh_key_stored"] is True
    assert view["host_key_fingerprint"].startswith("SHA256:")

    # Unified validators reject non-wp-reader users, broad roots, bad ports, external identity files, and global SSH overrides
    for bad_kwargs in (
        {"ssh_user": "deploy"},
        {"files_root": "/var/www"},
        {"ssh_port": "70000"},
        {"ssh_identity_file": "/home/user/.ssh/id_ed25519"},
        {"global_user": "root"},
        {"host_public_key": FAKE_PRIVATE_KEY},
    ):
        defaults: dict[str, Any] = {
            "files_root": "/var/www/custom-root",
            "ssh_host": "custom.internal",
            "ssh_user": "azom-wp-reader",
            "ssh_port": "2222",
            "ssh_identity_file": "",
            "private_key": "",
            "clear_key": False,
            "global_user": "",
            "global_port": "",
            "global_identity_file": "",
            "host_public_key": VALID_HOST_KEY,
        }
        defaults.update(bad_kwargs)
        with pytest.raises(SettingsRejected):
            save_ssh(tmp_path, site, **defaults)

    # Corrupted overlay row (e.g. non-reader user or bool port) is rejected on read cleanly
    (tmp_path / "wp_ssh.json").write_text(
        json.dumps({"sites": {site.id: {"ssh_user": "root", "ssh_port": True}}}),
        encoding="utf-8",
    )
    assert load_overlay(tmp_path) == {}
    err_view = describe_ssh(site, site, tmp_path, include_paths=False)
    assert err_view["overlay_error"] != ""
