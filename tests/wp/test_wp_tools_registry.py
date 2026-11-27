"""Unit tests for the declarative ``wp_ops/tools/registry.py`` and PR 5.2 handlers."""

from __future__ import annotations

from pathlib import Path

from ecom_ops.wp_ops import agent_tools
from ecom_ops.wp_ops.file_changes import FILE_TOOL_NAMES as LEGACY_FILE_TOOL_NAMES
from ecom_ops.wp_ops.files import LocalTree
from ecom_ops.wp_ops.tools.components import walk_component_tree
from ecom_ops.wp_ops.tools.loops import _BRIDGE_READ_TOOLS
from ecom_ops.wp_ops.tools.progress import safe_tool_detail, safe_tool_outcome
from ecom_ops.wp_ops.tools.registry import (
    ALL_FILE_TOOLS,
    DEEP_PHASE_TOOLS,
    EXTRA_FILE_TOOLS,
    FE_PLAN_TOOLS,
    FILE_TOOL_NAMES,
    FILE_TOOL_SCHEMAS,
    READ_BUDGET_TOOLS,
    SEARCH_BUDGET_TOOLS,
    TOOL_CAPABILITIES,
    TOOL_REGISTRY,
    UNTRUSTED_OUTPUT_TOOLS,
    WEB_FETCH_BUDGET_TOOLS,
    WEB_SEARCH_BUDGET_TOOLS,
    WORKSPACE_MUTATING_TOOLS,
    WORKSPACE_TOOLS,
    WP_TOOL_LABELS,
    WP_TOOL_NAMES,
    WP_TOOL_SCHEMAS,
    WP_TOOL_SHORT_LABELS,
)


def test_registry_completeness_and_derived_sets() -> None:
    assert len(TOOL_REGISTRY) == 77
    assert len(WP_TOOL_SCHEMAS) == 8
    assert len(WP_TOOL_NAMES) == 8
    assert FILE_TOOL_NAMES == LEGACY_FILE_TOOL_NAMES
    assert len(FILE_TOOL_NAMES) == 21
    assert len(EXTRA_FILE_TOOLS) == 49
    assert len(ALL_FILE_TOOLS) == 70
    assert len(FILE_TOOL_SCHEMAS) == 69
    assert WP_TOOL_NAMES | ALL_FILE_TOOLS == frozenset(TOOL_REGISTRY)
    assert ALL_FILE_TOOLS - set(FILE_TOOL_SCHEMAS) == {"wp_read_theme_plugin_file"}
    assert len(FE_PLAN_TOOLS) == 6
    assert len(WORKSPACE_TOOLS) == 18
    assert len(WORKSPACE_MUTATING_TOOLS) == 10
    assert len(READ_BUDGET_TOOLS) == 6
    assert len(SEARCH_BUDGET_TOOLS) == 2
    assert WEB_SEARCH_BUDGET_TOOLS == frozenset({"web_search"})
    assert WEB_FETCH_BUDGET_TOOLS == frozenset({"web_fetch_extract"})
    new_plugin_tools = {
        "wp_plugin_inventory",
        "wp_mu_plugins_and_dropins",
        "wp_plugin_diagnostics",
        "wp_plan_plugin_updates",
        "wp_plan_plugin_autoupdate",
        "wp_plan_plugin_recovery",
    }
    seo_tools = {
        "wp_seo_audit",
        "wp_seo_plan_meta",
        "wp_seo_batch_plan",
        "wp_seo_drift_digest",
        "wp_seo_strategy",
        "wp_seo_campaign",
        "wp_seo_link_package",
        "wp_seo_autopilot",
    }
    assert {
        "wp_component_intel",
        "web_search",
        "web_fetch_extract",
        "web_research_get",
        *new_plugin_tools,
        *seo_tools,
    }.issubset(UNTRUSTED_OUTPUT_TOOLS)
    assert {"wp_plugin_inventory", "wp_mu_plugins_and_dropins", "wp_seo_audit"}.issubset(
        _BRIDGE_READ_TOOLS
    )
    assert len(_BRIDGE_READ_TOOLS) == 6
    assert "wp_plugin_diagnostics" in READ_BUDGET_TOOLS
    assert {
        "wp_plugin_diagnostics",
        "wp_plan_plugin_updates",
        "wp_plan_plugin_autoupdate",
        "wp_plan_plugin_recovery",
        "wp_seo_plan_meta",
        "wp_seo_batch_plan",
        "wp_seo_drift_digest",
        "wp_seo_strategy",
        "wp_seo_campaign",
        "wp_seo_link_package",
        "wp_seo_autopilot",
    }.issubset(DEEP_PHASE_TOOLS)
    assert "wp_seo_audit" not in DEEP_PHASE_TOOLS
    assert len(DEEP_PHASE_TOOLS) == 35
    assert set(WP_TOOL_LABELS) == set(TOOL_REGISTRY)
    assert set(WP_TOOL_SHORT_LABELS) == set(TOOL_REGISTRY)
    assert all(WP_TOOL_LABELS[name].strip() for name in TOOL_REGISTRY)
    assert all(WP_TOOL_SHORT_LABELS[name].strip() for name in TOOL_REGISTRY)

    assert set(TOOL_CAPABILITIES["web_research"]) == {
        "web_search",
        "web_fetch_extract",
        "web_research_start",
        "web_research_status",
        "web_research_get",
        "wp_workspace_attach_research",
    }
    assert set(TOOL_CAPABILITIES["seo"]) == seo_tools

    # Capability groups cover all 77 tools without duplicates
    cap_union: list[str] = []
    for tools_in_group in TOOL_CAPABILITIES.values():
        cap_union.extend(tools_in_group)
    assert len(cap_union) == len(set(cap_union)) == 77

    # Upgraded schemas: mu_plugin kind + 7 scaffold templates
    for tool_name in (
        "wp_read_theme_plugin_file",
        "wp_list_components",
        "wp_list_component_tree",
        "wp_code_index",
        "wp_open_review_workspace",
        "wp_propose_file_diff",
        "wp_propose_new_file",
    ):
        kind_prop = TOOL_REGISTRY[tool_name].schema["properties"]["kind"]
        assert "mu_plugin" in kind_prop.get("enum", []), f"{tool_name} missing mu_plugin in kind enum"

    scaffold_enum = set(TOOL_REGISTRY["wp_workspace_scaffold"].schema["properties"]["template"]["enum"])
    assert scaffold_enum == {
        "child_theme_basics",
        "site_plugin_basics",
        "woo_compat_declaration",
        "mu_plugin_basics",
        "mu_plugin_loader",
        "plugin_hook_extension",
        "custom_plugin_full",
    }

    # Facade re-exports match registry objects identically
    assert agent_tools.WP_TOOL_SCHEMAS is WP_TOOL_SCHEMAS
    assert agent_tools.FILE_TOOL_SCHEMAS is FILE_TOOL_SCHEMAS
    assert agent_tools.WP_TOOL_NAMES is WP_TOOL_NAMES
    assert agent_tools.FILE_TOOL_NAMES is FILE_TOOL_NAMES
    assert agent_tools.EXTRA_FILE_TOOLS is EXTRA_FILE_TOOLS
    assert agent_tools.ALL_FILE_TOOLS is ALL_FILE_TOOLS
    assert agent_tools._WP_TOOL_LABELS is WP_TOOL_LABELS
    assert agent_tools._FE_PLAN_TOOLS is FE_PLAN_TOOLS
    assert agent_tools._WORKSPACE_TOOLS is WORKSPACE_TOOLS
    assert agent_tools._WORKSPACE_MUTATING_TOOLS is WORKSPACE_MUTATING_TOOLS
    assert agent_tools._READ_BUDGET_TOOLS is READ_BUDGET_TOOLS
    assert agent_tools._SEARCH_BUDGET_TOOLS is SEARCH_BUDGET_TOOLS
    assert agent_tools._DEEP_PHASE_TOOLS is DEEP_PHASE_TOOLS


def test_registry_outcome_formatters_cover_all_tools() -> None:
    for name in sorted(TOOL_REGISTRY):
        ok_msg = safe_tool_outcome(name, {"ok": True, "status": "ok"}, reads=1)
        assert isinstance(ok_msg, str) and ok_msg.strip(), f"empty ok outcome for {name}"
        err_msg = safe_tool_outcome(name, {"ok": False, "error_class": "AccessDenied"})
        assert err_msg == "AccessDenied"
    assert safe_tool_outcome("wp_list_products", {"ok": True, "products": [{"id": 1}, {"id": 2}]}) == "2 produkter"
    assert safe_tool_outcome("wp_list_pages", {"ok": True, "pages": [{"id": 1}]}) == "1 sidor/guider"
    assert safe_tool_outcome("wp_workspace_bulk_replace", {"ok": True, "dry_run": True}) == "torrkörning klar"
    assert safe_tool_outcome("wp_workspace_run_checks", {"ok": True, "status": "tests_passed", "staging_ready": True}) == (
        "kontroller: tests_passed · staging klar"
    )
    assert (
        safe_tool_outcome(
            "wp_plugin_inventory",
            {"ok": True, "plugins": [{"slug": "a", "update_available": True}, {"slug": "b", "update_available": False}]},
        )
        == "2 plugins (1 uppdateringar)"
    )
    assert (
        safe_tool_outcome(
            "wp_mu_plugins_and_dropins",
            {"ok": True, "mu_plugins": [{"slug": "m"}], "dropins": [{"file": "object-cache.php"}]},
        )
        == "1 MU-plugins, 1 drop-ins"
    )
    assert (
        safe_tool_outcome("wp_plan_plugin_updates", {"ok": True, "plan_id": "plu-0123456789ab"})
        == "uppdateringsplan plu-0123456789ab skapad"
    )
    assert (
        safe_tool_outcome("wp_plan_plugin_autoupdate", {"ok": True, "plan_id": "pau-0123456789ab"})
        == "autouppdateringsplan pau-0123456789ab skapad"
    )
    assert (
        safe_tool_outcome("wp_plan_plugin_recovery", {"ok": True, "plan_id": "plr-0123456789ab"})
        == "återställningsplan plr-0123456789ab skapad"
    )
    assert safe_tool_detail(
        "wp_list_component_tree",
        {"site_id": "mock-staging", "kind": "mu_plugin", "slug": "azom-guard"},
        None,
    ) == "mock-staging · mu_plugin/azom-guard"
    assert safe_tool_detail(
        "wp_plan_plugin_recovery",
        {"site_id": "mock-staging", "slug": "broken-plug", "action": "quarantine_sftp", "change_id": "chg-0123456789ab"},
        None,
    ) == "mock-staging · chg-0123456789ab · broken-plug · quarantine_sftp"
    assert (
        safe_tool_outcome(
            "wp_seo_audit",
            {"ok": True, "audit_id": "seoa-0123456789ab", "score": 82, "grade": "B", "issues": [{"code": "a"}]},
        )
        == "SEO-audit seoa-0123456789ab: 82/100 (B), 1 fynd"
    )
    assert (
        safe_tool_outcome("wp_seo_plan_meta", {"ok": True, "plan_id": "seom-0123456789ab"})
        == "SEO-plan seom-0123456789ab skapad"
    )
    assert (
        safe_tool_outcome("wp_seo_strategy", {"ok": True, "strategy_id": "seos-0123456789ab"})
        == "SEO-strategi seos-0123456789ab skapad"
    )
    assert (
        safe_tool_outcome("wp_seo_campaign", {"ok": True, "campaign_id": "seoc-0123456789ab"})
        == "SEO-kampanj seoc-0123456789ab skapad"
    )
    assert (
        safe_tool_outcome("wp_seo_link_package", {"ok": True, "package_id": "seol-0123456789ab"})
        == "länkpaket seol-0123456789ab skapat"
    )
    assert (
        safe_tool_outcome(
            "wp_seo_link_package",
            {"ok": True, "mode": "verify", "package_id": "seol-0123456789ab"},
        )
        == "länkpaket seol-0123456789ab verifierat"
    )
    assert (
        safe_tool_outcome("wp_seo_link_package", {"ok": True, "mode": "cumulative"})
        == "kumulativ ankartextmix beräknad"
    )
    assert (
        safe_tool_outcome(
            "wp_seo_link_package",
            {"ok": True, "mode": "vendors", "candidates": [{"vendor_id": "bazoom"}, {"vendor_id": "whitepress"}]},
        )
        == "2 SEO-butiker granskade"
    )
    assert (
        safe_tool_outcome(
            "wp_seo_autopilot",
            {"ok": True, "package_id": "seol-0123456789ab", "selected_vendor_id": "bazoom"},
        )
        == "SEO-autopilot klar (seol-0123456789ab · bazoom)"
    )
    assert (
        safe_tool_outcome("wp_seo_batch_plan", {"ok": True, "batch_id": "seob-0123456789ab"})
        == "SEO-batch seob-0123456789ab skapad"
    )
    assert (
        safe_tool_outcome("wp_seo_drift_digest", {"ok": True, "days": 7})
        == "SEO+plugin-digest (7d) klar"
    )
    assert (
        safe_tool_outcome("wp_seo_audit", {"ok": True, "scope": "gsc"})
        == "GSC quick-wins klar"
    )
    assert (
        safe_tool_detail(
            "wp_seo_audit",
            {"site_id": "mock-staging", "scope": "media"},
            None,
        )
        == "mock-staging · media"
    )


def test_wp_ops_has_zero_urllib_request_anywhere() -> None:
    wp_ops_dir = Path("skills/ecom_ops/wp_ops")
    for py_file in sorted(wp_ops_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        assert "urllib.request" not in text, f"urllib.request found in {py_file}"
        assert "urlopen" not in text, f"urlopen found in {py_file}"
        assert "http.client" not in text, f"http.client found in {py_file}"


def test_walk_component_tree_classifies_extensionless_files_correctly(tmp_path: Path) -> None:
    theme_dir = tmp_path / "wp-content" / "themes" / "demo-child"
    (theme_dir / "templates").mkdir(parents=True)
    (theme_dir / "LICENSE").write_text("GPL-2.0\n", encoding="utf-8")
    (theme_dir / "Makefile").write_text("all:\n", encoding="utf-8")
    (theme_dir / "style.css").write_text("/* Theme Name: Demo */\n", encoding="utf-8")
    (theme_dir / "templates" / "front-page.html").write_text("<main></main>\n", encoding="utf-8")

    transport = LocalTree("/var/www/demo", tmp_path)
    root_names, entries, _woo, root_readable, truncated = walk_component_tree(
        transport,
        "/var/www/demo/wp-content/themes/demo-child",
        max_depth=3,
        max_entries=50,
    )
    assert root_readable is True
    assert truncated is False
    assert "LICENSE" in root_names and "Makefile" in root_names
    by_path = {row["path"]: row["kind"] for row in entries}
    assert by_path["LICENSE"] == "file"
    assert by_path["Makefile"] == "file"
    assert by_path["style.css"] == "file"
    assert by_path["templates"] == "directory"
    assert by_path["templates/front-page.html"] == "file"
