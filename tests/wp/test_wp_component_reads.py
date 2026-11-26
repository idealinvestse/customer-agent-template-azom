"""Tests for WP line-based file reads, theme/plugin overviews, component search, and Woo version context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import (
    MOCK_REGISTRY,
    REPO_ROOT,
    sha256_bytes,
    site_row,
    write_plugin_file,
    write_theme_file,
)

from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.review_runner import versions_unknown
from ecom_ops.wp_ops.service import WpOpsService

SOURCE_SECRET = "AZOM-SECRET-SRC-91b7"


def _setup_mock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
    monkeypatch.setenv("XAI_GROK_BIN", "")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpOpsService:
    _setup_mock_env(tmp_path, monkeypatch)
    return WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)


def _tools(service: WpOpsService) -> WpAgentTools:
    return WpAgentTools(service)


def test_read_theme_plugin_file_line_based_and_argument_guards(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    lines = [f"// line {i}" for i in range(1, 11)]
    path = write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "functions.php",
        "\n".join(lines) + "\n",
    )
    digest = sha256_bytes(path.read_bytes())

    page1 = tools.execute(
        "wp_read_theme_plugin_file",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "twentytwentyfive",
            "path": "functions.php",
            "start_line": 2,
            "end_line": 5,
        },
        actor="oscar",
    )
    assert page1.data["ok"] is True
    assert page1.data["total_lines"] == 10
    assert page1.data["start_line"] == 2
    assert page1.data["end_line"] == 5
    assert page1.data["next_line"] == 6
    assert page1.data["truncated"] is True
    assert page1.data["sha256"] == digest
    assert page1.data["content"] == "2: // line 2\n3: // line 3\n4: // line 4\n5: // line 5"

    tail = tools.execute(
        "wp_read_theme_plugin_file",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "twentytwentyfive",
            "path": "functions.php",
            "start_line": 8,
            "end_line": 10,
        },
        actor="oscar",
    )
    assert tail.data["ok"] is True
    assert tail.data["start_line"] == 8
    assert tail.data["end_line"] == 10
    assert tail.data["next_line"] is None
    assert tail.data["truncated"] is False
    assert tail.data["content"].startswith("8: // line 8")

    for bad_args in (
        {"start_line": True},
        {"end_line": True},
        {"offset": 0, "start_line": 1},
        {"offset": 0, "end_line": 5},
    ):
        rejected = tools.execute(
            "wp_read_theme_plugin_file",
            {
                "site_id": "mock-staging",
                "kind": "theme",
                "slug": "twentytwentyfive",
                "path": "functions.php",
                **bad_args,
            },
            actor="oscar",
        )
        assert rejected.data["ok"] is False
        assert rejected.data["error_class"] == "InvalidToolArguments"


def test_wp_theme_overview_classic_block_tree_woo_and_redaction(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    # Classic theme with 3-level tree and WooCommerce override
    write_theme_file(tmp_path, "mock-staging", "classic-shop", "style.css", "/* Classic */\n")
    write_theme_file(tmp_path, "mock-staging", "classic-shop", "inc/sub/helper.php", "<?php\n")
    write_theme_file(
        tmp_path,
        "mock-staging",
        "classic-shop",
        "woocommerce/single-product/price.php",
        "<?php\n",
    )

    classic = tools.execute(
        "wp_theme_overview",
        {"site_id": "mock-staging", "slug": "classic-shop"},
        actor="jonatan",
    )
    assert classic.data["ok"] is True
    assert classic.data["theme_kind"] == "classic"
    assert classic.data["theme_relationship"] == "unknown"
    entry_paths = {row["path"] for row in classic.data["entries"]}
    assert "inc" in entry_paths
    assert "inc/sub" in entry_paths
    assert "inc/sub/helper.php" in entry_paths
    assert "woocommerce/single-product/price.php" in classic.data["woocommerce_overrides"]
    blob = json.dumps(classic.data)
    assert "files_root" not in blob
    assert "ssh_host" not in blob
    assert "secret_ref" not in blob
    assert "/var/www/mock-staging" not in blob

    # Block theme (theme.json + templates/*.html)
    write_theme_file(tmp_path, "mock-staging", "block-shop", "theme.json", '{"version": 2}\n')
    write_theme_file(
        tmp_path, "mock-staging", "block-shop", "templates/index.html", "<!-- wp:post-title /-->\n"
    )
    block = tools.execute(
        "wp_theme_overview",
        {"site_id": "mock-staging", "slug": "block-shop"},
        actor="jonatan",
    )
    assert block.data["ok"] is True
    assert block.data["theme_kind"] == "block"


def test_wp_plugin_overview_hooks_hpos_policy_and_oscar_only(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    third_party_body = (
        "<?php\n"
        "/**\n"
        " * Plugin Name: Third Party Shipping\n"
        " * WC tested up to: 9.6.1\n"
        " */\n"
        "add_action('before_woocommerce_init', function() {\n"
        "    \\Automattic\\WooCommerce\\Utilities\\FeaturesUtil::declare_compatibility('custom_order_tables', __FILE__, true);\n"
        "});\n"
        "add_action('woocommerce_checkout_process', 'tp_check');\n"
        "apply_filters('tp_shipping_rate', 10);\n"
    )
    write_plugin_file(
        tmp_path,
        "mock-staging",
        "third-party-ship",
        "third-party-ship.php",
        third_party_body,
    )
    write_plugin_file(
        tmp_path,
        "mock-staging",
        "azom-site-mock-staging",
        "azom-site-mock-staging.php",
        "<?php\n/**\n * Plugin Name: Azom Site\n */\nadd_action('init', 'azom_site_boot');\n",
    )

    for denied_actor in ("jonatan", "agent"):
        denied = tools.execute(
            "wp_plugin_overview",
            {"site_id": "mock-staging", "slug": "third-party-ship"},
            actor=denied_actor,
        )
        assert denied.data["ok"] is False
        assert denied.data["error_class"] == "AccessDenied"
        assert denied.data["status"] == "denied"

    third = tools.execute(
        "wp_plugin_overview",
        {"site_id": "mock-staging", "slug": "third-party-ship"},
        actor="oscar",
    )
    assert third.data["ok"] is True
    assert third.data["has_woo_hpos_declaration"] is True
    assert third.data["wc_tested_up_to"] == "9.6.1"
    assert "before_woocommerce_init" in third.data["hooks"]
    assert "woocommerce_checkout_process" in third.data["hooks"]
    assert "tp_shipping_rate" in third.data["hooks"]
    assert third.data["third_party_edit_policy"] == "read_only_extend_via_site_plugin"
    assert third.data["recommended_site_plugin"] == "azom-site-mock-staging"

    site_plugin = tools.execute(
        "wp_plugin_overview",
        {"site_id": "mock-staging", "slug": "azom-site-mock-staging"},
        actor="oscar",
    )
    assert site_plugin.data["ok"] is True
    assert site_plugin.data["third_party_edit_policy"] == "site_plugin_target"
    assert site_plugin.data["has_woo_hpos_declaration"] is False


def test_wp_search_component_filters_case_and_scans_over_20_files(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    for idx in range(1, 25):
        write_theme_file(
            tmp_path,
            "mock-staging",
            "twentytwentyfive",
            f"inc/file_{idx:02d}.php",
            f"<?php\n// filler {idx}\n",
        )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "inc/file_25.php",
        "<?php\n// AzomNeedleMarker found in file 25\n",
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "assets/ignored.js",
        "// AzomNeedleMarker in js should be ignored\n",
    )

    found = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "twentytwentyfive",
            "query": "azomneedlemarker",
            "extensions": [".php"],
            "case_insensitive": True,
        },
        actor="oscar",
    )
    assert found.data["ok"] is True
    assert found.data["files_scanned"] == 25
    assert [hit["path"] for hit in found.data["hits"]] == ["inc/file_25.php"]


def test_site_woo_version_and_versions_unknown():
    populated = parse_site(site_row(woo_version="9.6.1"))
    assert populated.woo_version == "9.6.1"
    assert versions_unknown(populated) is False

    missing_woo = parse_site(site_row(woo_version=""))
    assert missing_woo.woo_version == ""
    assert versions_unknown(missing_woo) is True


def test_component_context_child_vs_no_child(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    monkeypatch.setattr("ecom_ops.wp_ops.agent_tools.rest_reads_allowed", lambda: True)
    monkeypatch.setattr(
        "ecom_ops.wp_ops.agent_tools.component_rest_source",
        lambda _canonical, site=None: "live",
    )
    monkeypatch.setattr(
        "ecom_ops.wp_ops.agent_tools.load_live_plugin_rows",
        lambda _canonical, site=None: [],
    )

    monkeypatch.setattr(
        "ecom_ops.wp_ops.agent_tools.load_live_theme_rows",
        lambda _canonical, site=None: [
            {
                "status": "active",
                "stylesheet": "azom-child",
                "template": "twentytwentyfive",
                "version": "1.0",
            }
        ],
    )
    child_ctx = tools.execute("wp_component_context", {"site_id": "mock-staging"}, actor="jonatan")
    assert child_ctx.data["ok"] is True
    assert child_ctx.data["theme_relationship"] == "child"

    monkeypatch.setattr(
        "ecom_ops.wp_ops.agent_tools.load_live_theme_rows",
        lambda _canonical, site=None: [
            {
                "status": "active",
                "stylesheet": "twentytwentyfive",
                "template": "twentytwentyfive",
                "version": "1.2",
            }
        ],
    )
    no_child_ctx = tools.execute(
        "wp_component_context", {"site_id": "mock-staging"}, actor="jonatan"
    )
    assert no_child_ctx.data["ok"] is True
    assert no_child_ctx.data["theme_relationship"] == "no_child"
