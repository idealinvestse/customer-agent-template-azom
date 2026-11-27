"""Tests for Azom Ops Bridge v1.1.0 plugin lifecycle endpoints, MU-plugin snapshots, and WpBridgeClient."""

from __future__ import annotations

import base64
import importlib.util
import json
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.integrations.wp.bridge_client import (
    InMemoryWpBridgeTransport,
    PinnedBridgeHttpTransport,
    WpBridgeClient,
    canonical_signed_target,
    sign_bridge_request,
)
from ecom_ops.security import SecurityError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "wordpress-plugin" / "azom-ops-bridge"


def _load_builder_module():
    spec = importlib.util.spec_from_file_location(
        "build_wp_bridge_plugin",
        _REPO_ROOT / "bin" / "build-wp-bridge-plugin.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_packager_includes_class_plugins_and_passes_static_audit() -> None:
    mod = _load_builder_module()
    assert "includes/class-plugins.php" in mod._REQUIRED_FILES

    meta = mod.validate_plugin_source(_PLUGIN_DIR)
    assert meta["ok"] is True
    assert meta["php_files"] >= 7
    assert meta["violations"] == []

    zip_bytes, report = mod.build_plugin_zip_bytes(_PLUGIN_DIR)
    assert report["ok"] is True
    assert "azom-ops-bridge/includes/class-plugins.php" in report["files"]

    with zipfile.ZipFile(BytesIO(zip_bytes), "r") as zf:
        names = set(zf.namelist())
        assert "azom-ops-bridge/includes/class-plugins.php" in names
        main_src = zf.read("azom-ops-bridge/azom-ops-bridge.php").decode("utf-8")
        assert "Version:     1.2.0" in main_src
        assert "AZOM_OPS_BRIDGE_VERSION', '1.2.0'" in main_src


def test_php_plugin_v110_routes_and_security_invariants() -> None:
    main_php = (_PLUGIN_DIR / "azom-ops-bridge.php").read_text(encoding="utf-8")
    plugins_php = (_PLUGIN_DIR / "includes" / "class-plugins.php").read_text(encoding="utf-8")
    snapshots_php = (_PLUGIN_DIR / "includes" / "class-snapshots.php").read_text(encoding="utf-8")
    health_php = (_PLUGIN_DIR / "includes" / "class-health.php").read_text(encoding="utf-8")

    # All 6 new routes registered and gated by Azom_Ops_Bridge_Auth::verify_request
    for route in (
        "/plugins/inventory",
        "/plugins/update",
        "/plugins/rollback",
        "/plugins/autoupdate",
        "/plugins/toggle",
        "/plugins/diagnostics",
    ):
        assert f"'{route}'" in main_php

    assert main_php.count("[Azom_Ops_Bridge_Auth::class, 'verify_request']") >= 14

    # Plugin lifecycle hardening invariants in class-plugins.php
    assert "defined('ABSPATH')" in plugins_php
    assert "Plugin_Upgrader" in plugins_php
    assert "Automatic_Upgrader_Skin" in plugins_php
    assert "ZipArchive" in plugins_php
    assert "plugin-backups" in plugins_php
    assert "Require all denied" in plugins_php
    assert "DISALLOW_FILE_MODS" in plugins_php
    assert "AUTOMATIC_UPDATER_DISABLED" in plugins_php
    assert "WP_AUTO_UPDATE_CORE" in plugins_php
    assert "auto_update_plugins" in plugins_php
    assert "auto_update_plugin" in plugins_php
    assert "activate_plugin" in plugins_php
    assert "deactivate_plugins" in plugins_php
    assert "wp_get_paused_plugins" in plugins_php
    assert "16384" in plugins_php
    assert "OPSYS_UNIX" in plugins_php
    assert "0120000" in plugins_php
    assert "invalidate_opcache" in plugins_php
    assert "flush_all_caches" in plugins_php
    assert "build_probe_headers" in plugins_php
    assert "'X-Azom-Health-Probe' => '1'" not in plugins_php
    ops_php = (_PLUGIN_DIR / "includes" / "class-ops.php").read_text(encoding="utf-8")
    assert "X-Azom-Health-Probe" in ops_php
    assert "Azom_Ops_Bridge_Health::build_health_payload" in plugins_php

    # B1-02: Non-greedy / unambiguous backup zip slug resolution for hyphenated slugs
    assert "pbackup-((?:chg|plu|rb)-[a-f0-9]{12})-([A-Za-z0-9._-]{1,61})" in plugins_php
    assert "$expected_suffix = '-' . $safe_expected . '.zip';" in plugins_php

    # B1-03: Reactivate active plugin BEFORE loopback probe & fail on $upgraded !== true
    assert "$was_active" in plugins_php and "$was_network" in plugins_php
    assert "wp_update_plugins" in plugins_php
    assert "if ($upgraded !== true)" in plugins_php
    assert plugins_php.index("activate_plugin($plugin_file, '', $was_network, true)") < plugins_php.index(
        "$http_status = self::run_loopback_probe();"
    )

    # B1-17: Multisite network deactivate + CATCH_GET_CHILD in opcache invalidator
    assert "is_plugin_active_for_network($plugin_file)" in plugins_php
    assert "deactivate_plugins([$plugin_file], false, $was_network)" in plugins_php
    assert "CATCH_GET_CHILD" in plugins_php

    # Reusable health payload builder
    assert "function build_health_payload" in health_php

    # MU-plugin snapshot/apply/rollback confinement in class-snapshots.php
    assert "WPMU_PLUGIN_DIR" in snapshots_php
    assert "mu_plugin_file" in snapshots_php
    assert "'mu_plugin'" in snapshots_php
    assert "object-cache.php" in snapshots_php
    assert "advanced-cache.php" in snapshots_php
    assert "dropin_blocked" in snapshots_php
    assert "is_link($mu_root)" in snapshots_php


def test_in_memory_plugin_inventory_contract_shape() -> None:
    tr = InMemoryWpBridgeTransport(site_id="azom-no", base_url="https://azom.no")
    tr.autoupdate_filters.append({"priority": 10, "callback": "mu_block_updates", "accepted_args": 2})
    client = WpBridgeClient(site_id="azom-no", transport=tr)

    inv = client.plugin_inventory(include_updates=True)
    assert inv["ok"] is True
    assert inv["wp_version"]
    assert inv["php_version"]
    assert set(inv["constants"]) == {
        "disallow_file_mods",
        "automatic_updater_disabled",
        "wp_auto_update_core",
    }
    assert len(inv["autoupdate_filters"]) == 1

    required_plugin_keys = {
        "slug",
        "plugin_file",
        "name",
        "version",
        "active",
        "network_active",
        "update_available",
        "new_version",
        "auto_update",
        "requires_wp",
        "requires_php",
        "tested_wp",
        "wc_tested_up_to",
        "hpos_compatible",
        "is_paused",
        "is_critical",
        "is_bridge",
        "requires_plugins",
    }
    assert len(inv["plugins"]) >= 5
    for row in inv["plugins"]:
        assert required_plugin_keys.issubset(row.keys())

    bridge_rows = [p for p in inv["plugins"] if p["is_bridge"]]
    assert len(bridge_rows) == 1
    assert bridge_rows[0]["slug"] == "azom-ops-bridge"
    assert bridge_rows[0]["is_critical"] is True

    # MU-plugins & drop-ins
    assert len(inv["mu_plugins"]) >= 2
    assert [m["load_order"] for m in inv["mu_plugins"]] == [1, 2]
    for mu in inv["mu_plugins"]:
        assert {"slug", "file", "name", "version", "is_loader", "sha256", "load_order"}.issubset(mu.keys())
    assert len(inv["dropins"]) >= 2
    for di in inv["dropins"]:
        assert di["read_only"] is True
        assert {"file", "name", "description", "read_only"}.issubset(di.keys())

    # include_updates=False suppresses update flags
    inv_no_upd = client.plugin_inventory(include_updates=False)
    assert all(p["update_available"] is False and p["new_version"] is None for p in inv_no_upd["plugins"])


def test_in_memory_plugin_update_rollback_and_protections() -> None:
    tr = InMemoryWpBridgeTransport(site_id="azom-no", base_url="https://azom.no")
    client = WpBridgeClient(site_id="azom-no", transport=tr)

    # Self-update of azom-ops-bridge is refused
    with pytest.raises(SecurityError, match="403"):
        client.update_plugin(
            change_id="chg-self-0001",
            plugin_file="azom-ops-bridge/azom-ops-bridge.php",
        )

    # DISALLOW_FILE_MODS blocks updates
    tr.constants["disallow_file_mods"] = True
    with pytest.raises(SecurityError, match="DISALLOW_FILE_MODS"):
        client.update_plugin(
            change_id="chg-mods-0001",
            plugin_file="wordpress-seo/wp-seo.php",
        )
    tr.constants["disallow_file_mods"] = False

    # target_version mismatch fails closed (409)
    with pytest.raises(SecurityError, match="409"):
        client.update_plugin(
            change_id="chg-ver-0001",
            plugin_file="wordpress-seo/wp-seo.php",
            target_version="99.0.0",
        )

    # Loopback probe failure triggers auto-revert without changing version
    tr.fail_probe_for_plugins.add("wordpress-seo/wp-seo.php")
    failed = client.update_plugin(
        change_id="chg-fail-0001",
        plugin_file="wordpress-seo/wp-seo.php",
    )
    assert failed["ok"] is False
    assert failed["auto_reverted"] is True
    assert failed["http_status"] == 500
    assert failed["to_version"] == "23.4"
    assert tr.plugins["wordpress-seo/wp-seo.php"]["version"] == "23.4"
    tr.fail_probe_for_plugins.clear()

    # Successful atomic update + backup
    updated = client.update_plugin(
        change_id="chg-upd-0002",
        plugin_file="wordpress-seo/wp-seo.php",
        target_version="24.0",
    )
    assert updated["ok"] is True
    assert updated["from_version"] == "23.4"
    assert updated["to_version"] == "24.0"
    assert updated["backup_id"] == "pbackup-chg-upd-0002-wordpress-seo.zip"
    assert len(updated["backup_sha256"]) == 64
    assert updated["auto_reverted"] is False
    assert updated["error"] is None
    assert tr.plugins["wordpress-seo/wp-seo.php"]["version"] == "24.0"

    # Rollback rejects traversal, wrong slug, and bridge plugin
    with pytest.raises(SecurityError, match="400"):
        client.rollback_plugin(
            change_id="chg-rb-bad",
            plugin_file="wordpress-seo/wp-seo.php",
            backup_id="../pbackup-chg-upd-0002-wordpress-seo.zip",
        )
    with pytest.raises(SecurityError, match="400"):
        client.rollback_plugin(
            change_id="chg-rb-slug",
            plugin_file="litespeed-cache/litespeed-cache.php",
            backup_id=updated["backup_id"],
        )
    with pytest.raises(SecurityError, match="403"):
        client.rollback_plugin(
            change_id="chg-rb-bridge",
            plugin_file="azom-ops-bridge/azom-ops-bridge.php",
            backup_id="pbackup-chg-0001-azom-ops-bridge.zip",
        )

    # Successful rollback restores 23.4
    rb = client.rollback_plugin(
        change_id="chg-rb-0002",
        plugin_file="wordpress-seo/wp-seo.php",
        backup_id=updated["backup_id"],
    )
    assert rb["ok"] is True
    assert rb["restored_version"] == "23.4"
    assert rb["http_status"] == 200
    assert tr.plugins["wordpress-seo/wp-seo.php"]["version"] == "23.4"


def test_in_memory_autoupdate_toggle_and_diagnostics() -> None:
    tr = InMemoryWpBridgeTransport(site_id="azom-no", base_url="https://azom.no")
    client = WpBridgeClient(site_id="azom-no", transport=tr)

    # Auto-update refuses azom-ops-bridge
    with pytest.raises(SecurityError, match="403"):
        client.set_plugin_autoupdates(
            changes=[{"plugin_file": "azom-ops-bridge/azom-ops-bridge.php", "enabled": True}]
        )

    # Auto-update mutation with filter override warning
    tr.autoupdate_filters.append({"priority": 20, "callback": "site_policy_filter", "accepted_args": 2})
    au = client.set_plugin_autoupdates(
        changes=[
            {"plugin_file": "wordpress-seo/wp-seo.php", "enabled": True},
            {"plugin_file": "litespeed-cache/litespeed-cache.php", "enabled": False},
        ]
    )
    assert au["ok"] is True
    assert "litespeed-cache/litespeed-cache.php" in au["before"]
    assert "wordpress-seo/wp-seo.php" in au["after"]
    assert "litespeed-cache/litespeed-cache.php" not in au["after"]
    assert len(au["applied"]) == 2
    assert any("auto_update_plugin" in w for w in au["filter_warnings"])

    # Toggle refuses azom-ops-bridge
    with pytest.raises(SecurityError, match="403"):
        client.toggle_plugin(plugin_file="azom-ops-bridge/azom-ops-bridge.php", active=False)

    # Toggle activate / deactivate + auto-revert on probe failure
    t1 = client.toggle_plugin(plugin_file="contact-form-7/wp-contact-form-7.php", active=True)
    assert t1 == {
        "ok": True,
        "plugin_file": "contact-form-7/wp-contact-form-7.php",
        "active": True,
        "auto_reverted": False,
        "http_status": 200,
    }
    assert any(p["plugin"] == "contact-form-7/wp-contact-form-7.php" for p in client.health()["active_plugins"])

    tr.fail_probe_for_plugins.add("contact-form-7/wp-contact-form-7.php")
    t2 = client.toggle_plugin(plugin_file="contact-form-7/wp-contact-form-7.php", active=False)
    assert t2["ok"] is False
    assert t2["auto_reverted"] is True
    assert t2["active"] is True
    assert t2["http_status"] == 500
    tr.fail_probe_for_plugins.clear()

    # Diagnostics
    tr.paused_plugins.append(
        {
            "plugin_file": "broken-plugin/broken.php",
            "slug": "broken-plugin",
            "error_type": 1,
            "error_message": "Uncaught Error in <WP_CONTENT>/plugins/broken-plugin/broken.php",
            "file": "<WP_CONTENT>/plugins/broken-plugin/broken.php",
            "line": 12,
        }
    )
    client.update_plugin(change_id="chg-diag-01", plugin_file="litespeed-cache/litespeed-cache.php")
    diag = client.plugin_diagnostics(hooks=["auto_update_plugin", "woocommerce_checkout_process"], log_lines=20)
    assert diag["ok"] is True
    assert len(diag["paused_plugins"]) == 1
    assert len(diag["debug_log_tail"]) >= 1
    assert set(diag["hook_map"]) == {"auto_update_plugin", "woocommerce_checkout_process"}
    assert len(diag["available_backups"]) == 1
    assert diag["bridge_health"]["bridge_version"] == "1.2.0"


def test_mu_plugin_snapshot_apply_rollback_and_dropin_refusal() -> None:
    tr = InMemoryWpBridgeTransport(site_id="azom-no", base_url="https://azom.no")
    client = WpBridgeClient(site_id="azom-no", transport=tr)

    # Read existing MU-plugin snapshot via both mu_plugin_file and mu_plugin kinds
    snap1 = client.get_snapshot(kind="mu_plugin_file", slug="azom-mu-loader", path="azom-mu-loader.php")
    assert snap1["ok"] is True
    assert snap1["exists"] is True

    snap2 = client.get_snapshot(kind="mu_plugin", slug="azom-mu-loader", path="azom-mu-loader.php")
    assert snap2["sha256"] == snap1["sha256"]

    # Apply a new MU-plugin file and roll it back
    body = b"<?php\nif (!defined('ABSPATH')) { exit; }\n// Guard\n"
    applied = client.apply_files(
        change_id="chg-mu-0001",
        kind="mu_plugin",
        slug="azom-guard",
        entries=[{"path": "guard.php", "op": "created"}],
        files={"guard.php": body},
    )
    assert applied["ok"] is True

    after_snap = client.get_snapshot(kind="mu_plugin_file", slug="azom-guard", path="guard.php")
    assert after_snap["exists"] is True
    assert base64.b64decode(after_snap["content"]) == body

    rb = client.rollback(
        change_id="chg-mu-0001",
        kind="mu_plugin",
        slug="azom-guard",
        entries=[{"path": "guard.php", "op": "deleted"}],
        files={},
    )
    assert rb["ok"] is True
    assert client.get_snapshot(kind="mu_plugin_file", slug="azom-guard", path="guard.php")["exists"] is False

    # Drop-ins and traversal are refused
    for dropin in ("object-cache.php", "advanced-cache.php", "db.php"):
        with pytest.raises(SecurityError, match="drop-ins"):
            client.apply_files(
                change_id="chg-drop-001",
                kind="mu_plugin",
                slug="_root",
                entries=[{"path": dropin, "op": "created"}],
                files={dropin: b"<?php\n"},
            )
    with pytest.raises(SecurityError, match="400"):
        client.get_snapshot(kind="mu_plugin_file", slug="_root", path="../wp-config.php")


def test_live_http_transport_auto_revert_passthrough_and_query_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_pinned_request(url: str, **kwargs: Any):
        seen.append({"url": url, **kwargs})
        if "/plugins/update" in url:
            body = json.dumps(
                {
                    "ok": False,
                    "plugin_file": "wordpress-seo/wp-seo.php",
                    "slug": "wordpress-seo",
                    "from_version": "23.4",
                    "to_version": "23.4",
                    "backup_id": "pbackup-chg-live-wordpress-seo.zip",
                    "backup_sha256": "a" * 64,
                    "http_status": 500,
                    "auto_reverted": True,
                    "error": "Post-update loopback health probe failed; reverted automatically.",
                }
            ).encode("utf-8")
            return 500, {}, body, url
        if "/plugins/toggle" in url:
            body = json.dumps({"ok": False, "error": "Toggling azom-ops-bridge is refused."}).encode("utf-8")
            return 403, {}, body, url
        body = json.dumps({"ok": True, "paused_plugins": [], "debug_log_tail": [], "hook_map": {}, "available_backups": [], "bridge_health": {}}).encode("utf-8")
        return 200, {}, body, url

    monkeypatch.setattr("ecom_ops.integrations.wp.bridge_client.pinned_https_request", fake_pinned_request)

    transport = PinnedBridgeHttpTransport(base_url="https://azom.no", secret="s" * 40)
    client = WpBridgeClient(site_id="azom-no", transport=transport)

    # Query params are canonicalized and signed consistently
    diag = client.plugin_diagnostics(hooks=["auto_update_plugin", "init"], log_lines=50)
    assert diag["ok"] is True
    assert canonical_signed_target(
        "/azom-ops/v1/plugins/diagnostics",
        {"log_lines": 50, "hooks": "auto_update_plugin,init"},
    ) == "/azom-ops/v1/plugins/diagnostics?hooks=auto_update_plugin%2Cinit&log_lines=50"
    assert "hooks=auto_update_plugin%2Cinit&log_lines=50" in seen[0]["url"]

    # Auto-revert HTTP 500 returns structured dict cleanly on update_plugin
    upd = client.update_plugin(change_id="chg-live", plugin_file="wordpress-seo/wp-seo.php")
    assert upd["ok"] is False
    assert upd["auto_reverted"] is True
    assert upd["http_status"] == 500

    # Hard 403 refusal without auto_reverted still raises SecurityError
    with pytest.raises(SecurityError, match="403"):
        client.toggle_plugin(plugin_file="azom-ops-bridge/azom-ops-bridge.php", active=False)

    # Signature changes when include_updates query param changes
    sig_1 = sign_bridge_request(
        secret="s" * 40,
        method="GET",
        route="/azom-ops/v1/plugins/inventory",
        params={"include_updates": "1"},
        timestamp=1700000000,
        nonce="0123456789abcdef",
    )
    sig_0 = sign_bridge_request(
        secret="s" * 40,
        method="GET",
        route="/azom-ops/v1/plugins/inventory",
        params={"include_updates": "0"},
        timestamp=1700000000,
        nonce="0123456789abcdef",
    )
    assert sig_1["X-Azom-Signature"] != sig_0["X-Azom-Signature"]


def test_b1_02_hyphenated_plugin_backup_slug_parsing_and_mismatch_rejection() -> None:
    import re

    # Mirror the exact PHP regexes in class-plugins.php for hyphenated slugs
    std_re = re.compile(r"^pbackup-((?:chg|plu|rb)-[a-f0-9]{12})-([A-Za-z0-9._-]{1,61})\.zip$", re.I)
    for slug in ("litespeed-cache", "klarna-checkout-for-woocommerce", "wordpress-seo"):
        bid = f"pbackup-chg-1234567890ab-{slug}.zip"
        m = std_re.match(bid)
        assert m is not None
        assert m.group(2) == slug
    # Must NOT match 'woocommerce' when backup belongs to 'klarna-checkout-for-woocommerce'
    m_klarna = std_re.match("pbackup-chg-1234567890ab-klarna-checkout-for-woocommerce.zip")
    assert m_klarna is not None and m_klarna.group(2) != "woocommerce"


def test_php_and_in_memory_funnel_canary_probes_and_validation() -> None:
    plugins_php = (_PLUGIN_DIR / "includes" / "class-plugins.php").read_text(encoding="utf-8")
    ops_php = (_PLUGIN_DIR / "includes" / "class-ops.php").read_text(encoding="utf-8")

    # PHP funnel canary markers, sanitization, and reactivation order
    assert "sanitize_canary_paths" in ops_php
    assert "probe_canary_paths" in ops_php
    assert "detect_fatal_body" in ops_php
    assert "Fatal error:" in ops_php
    assert "Parse error:" in ops_php
    assert "There has been a critical error on this website" in ops_php
    assert "canary_paths" in plugins_php
    assert "canary_results" in plugins_php
    assert plugins_php.index("activate_plugin($plugin_file, '', $was_network, true)") < plugins_php.index(
        "self::run_canary_probes($canary_paths)"
    )

    # InMemoryWpBridgeTransport canary validation & failure auto-revert
    tr = InMemoryWpBridgeTransport(site_id="azom-no", base_url="https://azom.no")
    client = WpBridgeClient(site_id="azom-no", transport=tr)

    # Invalid canary_paths rejected with 400
    for bad_paths in (
        ["https://evil.example/"],
        ["/../wp-config.php"],
        ["/wp-admin/"],
        ["/shop/?evil=1"],
    ):
        with pytest.raises(SecurityError, match="400"):
            client.update_plugin(
                change_id="chg-bad-canary",
                plugin_file="woocommerce/woocommerce.php",
                canary_paths=bad_paths,
            )

    # Simulated fatal on /checkout/ auto-reverts and returns canary_results
    tr.fail_canary_paths_for_plugins["woocommerce/woocommerce.php"] = {
        "/checkout/": {"status": 200, "fatal_detected": True}
    }
    failed = client.update_plugin(
        change_id="chg-can-fail",
        plugin_file="woocommerce/woocommerce.php",
        canary_paths=["/", "/shop/", "/cart/", "/checkout/"],
    )
    assert failed["ok"] is False
    assert failed["auto_reverted"] is True
    assert failed["http_status"] == 500
    assert tr.plugins["woocommerce/woocommerce.php"]["version"] == "9.4.2"
    assert len(failed["canary_results"]) == 4
    assert failed["canary_results"][-1] == {
        "path": "/checkout/",
        "status": 200,
        "ok": False,
        "fatal_detected": True,
    }

    # Passing multi-path canary succeeds and updates version
    tr.fail_canary_paths_for_plugins.clear()
    passed = client.update_plugin(
        change_id="chg-can-ok",
        plugin_file="woocommerce/woocommerce.php",
        canary_paths=["/", "/shop/", "/cart/", "/checkout/"],
    )
    assert passed["ok"] is True
    assert tr.plugins["woocommerce/woocommerce.php"]["version"] == "9.5.1"
    assert [c["path"] for c in passed["canary_results"]] == ["/", "/shop/", "/cart/", "/checkout/"]
    assert all(c["ok"] is True and c["fatal_detected"] is False for c in passed["canary_results"])
