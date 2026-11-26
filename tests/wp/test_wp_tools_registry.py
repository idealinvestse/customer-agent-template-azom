"""Unit tests for the declarative ``wp_ops/tools/registry.py`` and PR 5.2 handlers."""

from __future__ import annotations

from pathlib import Path

from ecom_ops.wp_ops import agent_tools
from ecom_ops.wp_ops.file_changes import FILE_TOOL_NAMES as LEGACY_FILE_TOOL_NAMES
from ecom_ops.wp_ops.files import LocalTree
from ecom_ops.wp_ops.tools.components import walk_component_tree
from ecom_ops.wp_ops.tools.progress import safe_tool_outcome
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
    WORKSPACE_MUTATING_TOOLS,
    WORKSPACE_TOOLS,
    WP_TOOL_LABELS,
    WP_TOOL_NAMES,
    WP_TOOL_SCHEMAS,
    WP_TOOL_SHORT_LABELS,
)


def test_registry_completeness_and_derived_sets() -> None:
    assert len(TOOL_REGISTRY) == 54
    assert len(WP_TOOL_SCHEMAS) == 8
    assert len(WP_TOOL_NAMES) == 8
    assert FILE_TOOL_NAMES == LEGACY_FILE_TOOL_NAMES
    assert len(FILE_TOOL_NAMES) == 21
    assert len(EXTRA_FILE_TOOLS) == 26
    assert len(ALL_FILE_TOOLS) == 47
    assert len(FILE_TOOL_SCHEMAS) == 46
    assert WP_TOOL_NAMES | ALL_FILE_TOOLS == frozenset(TOOL_REGISTRY)
    assert ALL_FILE_TOOLS - set(FILE_TOOL_SCHEMAS) == {"wp_read_theme_plugin_file"}
    assert len(FE_PLAN_TOOLS) == 6
    assert len(WORKSPACE_TOOLS) == 17
    assert len(WORKSPACE_MUTATING_TOOLS) == 10
    assert len(READ_BUDGET_TOOLS) == 5
    assert len(SEARCH_BUDGET_TOOLS) == 2
    assert len(DEEP_PHASE_TOOLS) == 21
    assert set(WP_TOOL_LABELS) == set(TOOL_REGISTRY)
    assert set(WP_TOOL_SHORT_LABELS) == set(TOOL_REGISTRY)
    assert all(WP_TOOL_LABELS[name].strip() for name in TOOL_REGISTRY)
    assert all(WP_TOOL_SHORT_LABELS[name].strip() for name in TOOL_REGISTRY)

    # Capability groups cover all 54 tools without duplicates
    cap_union: list[str] = []
    for tools_in_group in TOOL_CAPABILITIES.values():
        cap_union.extend(tools_in_group)
    assert len(cap_union) == len(set(cap_union)) == 54

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


def test_registry_outcome_formatters_cover_all_54_tools() -> None:
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
