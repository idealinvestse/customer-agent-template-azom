"""Tests for Wave 3 Azom Ops Bridge PHP plugin builder, HMAC signing, and WpBridgeClient."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ecom_ops.integrations.wp.bridge_client import (
    InMemoryWpBridgeTransport,
    WpBridgeClient,
    bridge_client_for_site,
    bridge_enabled,
    sign_bridge_request,
)
from ecom_ops.wp_ops.models import Site


def test_plugin_packager_check_and_deterministic_zip() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "build_wp_bridge_plugin",
        repo_root / "bin" / "build-wp-bridge-plugin.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    meta = mod.validate_plugin_source()
    assert meta["ok"] is True
    assert meta["php_files"] >= 6

    zip1, meta1 = mod.build_plugin_zip_bytes()
    zip2, meta2 = mod.build_plugin_zip_bytes()
    assert zip1 == zip2
    assert meta1["sha256"] == meta2["sha256"]
    assert zip1[:2] == b"PK"


def test_bridge_hmac_signature_and_fail_closed_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    hdr_a = sign_bridge_request(
        secret="a" * 32,
        method="POST",
        route="/azom-ops/v1/apply",
        timestamp="1700000000",
        nonce="nonce-1",
        body=b'{"files":[]}',
    )
    hdr_b = sign_bridge_request(
        secret="a" * 32,
        method="POST",
        route="/azom-ops/v1/apply",
        timestamp="1700000000",
        nonce="nonce-1",
        body=b'{"files":[]}',
    )
    assert hdr_a == hdr_b and len(hdr_a["X-Azom-Signature"]) == 64

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)
    monkeypatch.setenv("AZOM_WP_BRIDGE_ENABLED", "1")
    assert bridge_enabled() is True

    monkeypatch.setenv("AZOM_WP_DEPLOY_KILL", "1")
    assert bridge_enabled() is False

    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_BRIDGE_ENABLED", raising=False)
    assert bridge_enabled() is False

    site = Site(
        id="azom-no",
        display_name="Azom NO",
        canonical_url="https://azom.no/",
        environment="production",
        business_owner="customer",
        hosting_provider="unconfigured",
        wordpress_version="6.7",
        php_version="8.3",
        woocommerce=True,
        critical_urls=("https://azom.no/",),
        maintenance_window="not agreed",
        secret_ref="WP_SECRET_NO",
        alert_owner="oscar",
        alert_confirmations=2,
        check_interval_seconds=60,
        timeout_seconds=10,
        dedupe_window_seconds=30,
        retention_days=7,
        capabilities={},
        files_root="/var/www/shop",
        ssh_host="azom.no",
    )
    assert bridge_client_for_site(site) is None


def test_in_memory_bridge_client_lifecycle() -> None:
    tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    client = WpBridgeClient(site_id="mock-staging", transport=tr)
    assert client.is_live is False

    health = client.health()
    assert health["multilingual"] == "wpml"
    assert health["maintenance_active"] is False

    tr_res = client.translations(post_id=42)
    assert set(tr_res["translations"]) == {"nb", "sv", "da"}

    applied = client.apply_files(
        change_id="chg-112233445566",
        kind="theme",
        slug="azom-child",
        entries=[{"path": "style.css", "op": "updated"}],
        files={"style.css": b"body { color: #000; }"},
    )
    assert applied["ok"] is True

    snap = client.get_snapshot(kind="theme_file", slug="azom-child", path="style.css")
    assert snap["exists"] is True

    flushed = client.flush_cache()
    assert "wp_object_cache" in flushed["flushed"]

    maint = client.set_maintenance(enabled=True, duration_seconds=120)
    assert maint["enabled"] is True

    rb = client.rollback(
        change_id="chg-112233445566",
        kind="theme",
        slug="azom-child",
        entries=[{"path": "style.css", "op": "updated"}],
        files={"style.css": b""},
    )
    assert rb["ok"] is True


def test_php_plugin_security_hardening_and_query_hmac_and_untrusted_tools() -> None:
    from ecom_ops.wp_ops.tools.registry import UNTRUSTED_OUTPUT_TOOLS

    # V-27: all three bridge read tools mark output untrusted
    for tool_name in ("wp_bridge_health", "wp_list_translations", "wp_change_history"):
        assert tool_name in UNTRUSTED_OUTPUT_TOOLS

    # V-06: query string changes the canonical HMAC signature
    sig_no_q = sign_bridge_request(
        secret="a" * 32,
        method="GET",
        route="/azom-ops/v1/translations",
        timestamp="1700000000",
        nonce="nonce-q",
        body=b"",
    )
    sig_with_q = sign_bridge_request(
        secret="a" * 32,
        method="GET",
        route="/azom-ops/v1/translations?post_id=42",
        timestamp="1700000000",
        nonce="nonce-q",
        body=b"",
    )
    assert sig_no_q["X-Azom-Signature"] != sig_with_q["X-Azom-Signature"]

    # PHP plugin source invariants (V-01, V-02, V-05, V-06, V-07, V-08, V-09, V-11, V-13)
    plugin_dir = Path(__file__).resolve().parents[2] / "wordpress-plugin" / "azom-ops-bridge"
    main_php = (plugin_dir / "azom-ops-bridge.php").read_text(encoding="utf-8")
    auth_php = (plugin_dir / "includes" / "class-auth.php").read_text(encoding="utf-8")
    ops_php = (plugin_dir / "includes" / "class-ops.php").read_text(encoding="utf-8")
    snap_php = (plugin_dir / "includes" / "class-snapshots.php").read_text(encoding="utf-8")
    tr_php = (plugin_dir / "includes" / "class-translations.php").read_text(encoding="utf-8")
    uninstall_php = (plugin_dir / "uninstall.php").read_text(encoding="utf-8")

    # V-01 + V-13: option-backed maintenance + REST authentication exemption for /azom-ops/v1/
    assert "azom_ops_maintenance_until" in ops_php
    assert "azom_ops_maintenance_until" in uninstall_php
    assert "rest_authentication_errors" in main_php
    assert "/azom-ops/v1/" in main_php

    # V-02: opcache_invalidate and cache flush helpers exist in snapshots/ops
    assert "opcache_invalidate" in snap_php

    # V-05 + V-06 + V-07: DB nonce table, canonical sorted query params signing, inet_pton CIDR matching
    assert "azom_ops_nonces" in auth_php and "azom_ops_nonces" in uninstall_php
    assert "get_query_params" in auth_php and "http_build_query" in auth_php
    assert "inet_pton" in auth_php

    # V-08 + V-09: segment symlink check & dotted/uppercase plugin/theme slugs allowed
    assert "is_link" in snap_php
    assert "A-Za-z0-9._-" in snap_php

    # V-11: Polylang checked before WPML
    assert tr_php.index("pll_languages_list") < tr_php.index("wpml_active_languages")

    # B1-01: Anchored URL path parsing & signed maintenance probe header (no static bypass)
    assert "wp_parse_url($uri, PHP_URL_PATH)" in main_php
    assert "#/(?:wp-json/)?azom-ops/v1(?:/|$)#" in main_php
    assert "strpos($rest_route, '/azom-ops/v1/') === 0" in main_php
    assert "wp_parse_url($uri, PHP_URL_PATH)" in ops_php
    assert "#/(?:wp-json/)?azom-ops/v1(?:/|$)#" in ops_php
    assert "build_probe_headers" in ops_php and "is_valid_probe_request" in ops_php
    assert "hash_hmac" in ops_php
    assert "'X-Azom-Health-Probe' => '1'" not in snap_php
    assert "build_probe_headers" in snap_php

    # B1-13: Canonical sha256 parity in class-snapshots.php
    assert "ksort($data, SORT_STRING)" in snap_php
    assert "JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES" in snap_php

    # B1-17: Option-gated nonce table install & handle_rollback pre-state revert
    assert "ensure_table_installed" in auth_php
    assert "azom_ops_bridge_nonce_table_version" in auth_php
    assert "Rollback write failed; reverted earlier files." in snap_php


def test_b1_13_b1_16_b1_17_snapshot_sha256_builder_strings_and_bridge_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    from ecom_ops.wp_ops.service import WpOpsService

    # B1-13: InMemoryWpBridgeTransport canonical sha256 + modified_gmt parity
    tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    client = WpBridgeClient(site_id="mock-staging", transport=tr)
    snap_page = client.get_snapshot(kind="page", item_id=42)
    assert "modified_gmt" in snap_page["data"]
    expected_page_sha = hashlib.sha256(
        json.dumps(snap_page["data"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert snap_page["sha256"] == expected_page_sha

    snap_settings = client.get_snapshot(kind="settings")
    expected_settings_sha = hashlib.sha256(
        json.dumps(snap_settings["data"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert snap_settings["sha256"] == expected_settings_sha

    # B1-16: build-wp-bridge-plugin.py comment stripper does not get blinded by https:// or # inside strings
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "build_wp_bridge_plugin",
        repo_root / "bin" / "build-wp-bridge-plugin.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fake_plugin = tmp_path / "fake-bridge"
    (fake_plugin / "includes").mkdir(parents=True)
    for rel in mod._REQUIRED_FILES:
        dest = fake_plugin / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".php"):
            dest.write_text("<?php\nif (!defined('ABSPATH')) { exit; }\n", encoding="utf-8")
        else:
            dest.write_text("Stable tag: 1.2.0\n", encoding="utf-8")

    # Inject banned call AFTER https:// string literal on the same line
    (fake_plugin / "includes" / "class-ops.php").write_text(
        "<?php\nif (!defined('ABSPATH')) { exit; }\n$u = \"https://azom.no\"; eval($_GET['x']);\n",
        encoding="utf-8",
    )
    bad_url_check = mod.validate_plugin_source(fake_plugin)
    assert bad_url_check["ok"] is False
    assert "eval(" in bad_url_check["error"]

    # Inject banned call AFTER # inside string literal
    (fake_plugin / "includes" / "class-ops.php").write_text(
        "<?php\nif (!defined('ABSPATH')) { exit; }\n$h = '#anchor'; system('id');\n",
        encoding="utf-8",
    )
    bad_hash_check = mod.validate_plugin_source(fake_plugin)
    assert bad_hash_check["ok"] is False
    assert "system(" in bad_hash_check["error"]

    # B1-17: WpOpsService bridge facades honor self.bridge_factory
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)
    custom_tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://custom.local")
    factory_hits: list[str] = []

    def _factory(site: Site) -> WpBridgeClient:
        factory_hits.append(site.id)
        return WpBridgeClient(site_id=site.id, transport=custom_tr)

    svc = WpOpsService(data_dir=tmp_path, bridge_factory=_factory)
    assert svc.bridge_health("mock-staging", actor="jonatan").ok is True
    assert svc.bridge_translations("mock-staging", post_id=10, actor="jonatan").ok is True
    assert svc.bridge_cache_flush("mock-staging", actor="oscar").ok is True
    assert svc.bridge_maintenance("mock-staging", enabled=True, actor="oscar").ok is True
    assert len(factory_hits) == 4
    assert custom_tr.cache_flushes == 1
    assert custom_tr.maintenance_active is True
