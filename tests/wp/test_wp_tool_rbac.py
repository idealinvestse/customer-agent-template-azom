"""Tests for WP tool schema registration and RBAC boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import (
    write_plugin_file,
    write_theme_file,
)

from ecom_ops.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    clear_rbac_cache,
)
from ecom_ops.wp_ops.agent_tools import (
    ALL_FILE_TOOLS,
    EXTRA_FILE_TOOLS,
    FILE_TOOL_SCHEMAS,
    WP_TOOL_SCHEMAS,
)
from ecom_ops.wp_ops.file_changes import FILE_TOOL_NAMES


def test_tool_schema_registration_and_wp_playbook_rbac(
    monkeypatch: pytest.MonkeyPatch, wp_service_tools, no_openrouter
) -> None:
    del no_openrouter
    assert len(EXTRA_FILE_TOOLS) == 53
    assert ALL_FILE_TOOLS == FILE_TOOL_NAMES | EXTRA_FILE_TOOLS
    for name in ALL_FILE_TOOLS:
        if name == "wp_read_theme_plugin_file":
            assert name in WP_TOOL_SCHEMAS
        else:
            assert name in FILE_TOOL_SCHEMAS

    _service, tools = wp_service_tools(mock="1", files_ssh="0", sandbox="0", reset_router=True)

    monkeypatch.setitem(ROLE_PERMISSIONS, "viewer", frozenset({Permission.READ}))
    clear_rbac_cache()

    pb = tools.execute("wp_playbook", {"topic": "child_theme_edit"}, actor="jonatan")
    assert pb.data["ok"] is True
    assert pb.data["topic"] == "child_theme_edit"
    assert pb.data["live_calls"] is False

    bad = tools.execute("wp_playbook", {"topic": "unknown_topic"}, actor="jonatan")
    assert bad.data["ok"] is False
    assert bad.data["error_class"] == "UnknownPlaybook"


def test_rbac_boundaries_on_reads_diffs_static_checks_and_woo_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_env, wp_service_tools
) -> None:
    _service, tools = wp_service_tools(mock="1", files_ssh="0", sandbox="0", reset_router=True)

    secret = "sk_live_" + "RbacSecret12345"
    get_var = "$_GET"
    write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "style.css",
        "/*\nTheme Name: Twenty Twenty-Five\n*/\n",
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: twentytwentyfive\n*/\n",
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "functions.php",
        f"<?php\ndefined('ABSPATH') || exit;\n$api_key = '{secret}';\nadd_action('init', 'azom_child_init');\n",
    )
    write_plugin_file(
        tmp_path,
        "mock-staging",
        "woocommerce",
        "templates/single-product/price.php",
        "<?php\n/**\n * @version 3.0.0\n */\necho 'price';\n",
    )

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Badge work"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]

    patched = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "functions.php",
            "old_text": "add_action('init', 'azom_child_init');",
            "new_text": f"add_action('init', 'azom_child_init');\necho {get_var}['badge'];",
        },
        actor="oscar",
    )
    assert patched.data["ok"] is True

    for actor in ("jonatan", "agent"):
        for tool_name, args in (
            (
                "wp_read_theme_plugin_file",
                {
                    "site_id": "mock-staging",
                    "kind": "theme",
                    "slug": "azom-child",
                    "path": "functions.php",
                },
            ),
            (
                "wp_workspace_read_file",
                {"site_id": "mock-staging", "plan_id": plan_id, "path": "functions.php"},
            ),
            ("wp_workspace_diff", {"site_id": "mock-staging", "plan_id": plan_id}),
            (
                "wp_code_index",
                {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child"},
            ),
            ("wp_code_index", {"site_id": "mock-staging", "plan_id": plan_id}),
            (
                "wp_search_component",
                {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "query": "azom"},
            ),
            (
                "wp_search_component",
                {"site_id": "mock-staging", "plan_id": plan_id, "query": "azom"},
            ),
            (
                "wp_workspace_copy_woo_template",
                {
                    "site_id": "mock-staging",
                    "plan_id": plan_id,
                    "template": "single-product/price.php",
                },
            ),
        ):
            res = tools.execute(tool_name, args, actor=actor)
            assert res.data["ok"] is False, (tool_name, actor)
            assert res.data["error_class"] == "AccessDenied", (tool_name, actor)

    monkeypatch.setitem(
        ROLE_PERMISSIONS,
        "operator",
        ROLE_PERMISSIONS["operator"] | {Permission.WP_OPS_ADMIN},
    )
    diff_no_code_edit = tools.execute(
        "wp_workspace_diff",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="agent",
    )
    assert diff_no_code_edit.data["ok"] is False
    assert diff_no_code_edit.data["error_class"] == "AccessDenied"
    monkeypatch.undo()
    wp_env(mock="1", files_ssh="0", sandbox="0", reset_router=True)

    diff_ok = tools.execute(
        "wp_workspace_diff",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="oscar",
    )
    assert diff_ok.data["ok"] is True
    assert f"+echo {get_var}['badge'];" in diff_ok.data["unified_diff"]
    assert secret not in diff_ok.data["unified_diff"]

    ws_read = tools.execute(
        "wp_workspace_read_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "functions.php",
            "start_line": 1,
            "end_line": 10,
        },
        actor="oscar",
    )
    assert ws_read.data["ok"] is True
    assert secret not in ws_read.data["content"]
    assert "azom_child_init" in ws_read.data["content"]

    idx_ok = tools.execute(
        "wp_code_index",
        {"site_id": "mock-staging", "plan_id": plan_id, "symbols": ["hook"]},
        actor="oscar",
    )
    assert idx_ok.data["ok"] is True
    assert idx_ok.data["source_scope"] == "workspace"
    assert any(e["name"] == "init" for e in idx_ok.data["entries"])

    idx_inst = tools.execute(
        "wp_code_index",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child"},
        actor="oscar",
    )
    assert idx_inst.data["ok"] is True
    assert idx_inst.data["source_scope"] == "installed"
    assert secret not in json.dumps(idx_inst.data)

    for non_admin in ("jonatan", "agent"):
        sc_na = tools.execute(
            "wp_workspace_static_checks",
            {"site_id": "mock-staging", "plan_id": plan_id},
            actor=non_admin,
        )
        assert sc_na.data["ok"] is True
        assert sc_na.data["checks_passed"] is False
        assert sc_na.data["findings"]
        assert all("excerpt" not in f for f in sc_na.data["findings"])

    sc_admin = tools.execute(
        "wp_workspace_static_checks",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="oscar",
    )
    assert sc_admin.data["ok"] is True
    assert all("excerpt" in f for f in sc_admin.data["findings"])
    assert secret not in json.dumps(sc_admin.data)


def test_plugin_lifecycle_tools_rbac_and_preview_only(
    tmp_path: Path, wp_service_tools
) -> None:
    _service, tools = wp_service_tools(mock="1", files_ssh="0", sandbox="0", reset_router=True)

    # Read-only plugin inventory and MU-plugins/drop-ins allowed for viewer (jonatan), operator (agent), admin (oscar)
    for actor in ("jonatan", "agent", "oscar"):
        inv = tools.execute("wp_plugin_inventory", {"site_id": "mock-staging"}, actor=actor)
        assert inv.data["ok"] is True, (actor, inv.data)
        assert isinstance(inv.data.get("plugins"), list)
        assert inv.data["dashboard_path"] == "/wp/mock-staging/plugins"

        mu = tools.execute("wp_mu_plugins_and_dropins", {"site_id": "mock-staging"}, actor=actor)
        assert mu.data["ok"] is True, (actor, mu.data)
        assert mu.data["dropins_read_only"] is True
        assert isinstance(mu.data.get("mu_plugins"), list)
        assert isinstance(mu.data.get("dropins"), list)

    # Admin-only diagnostics, autoupdate plan, and recovery plan deny jonatan and agent
    for actor in ("jonatan", "agent"):
        for tool_name, args in (
            ("wp_plugin_diagnostics", {"site_id": "mock-staging", "log_lines": 20}),
            ("wp_plan_plugin_autoupdate", {"site_id": "mock-staging", "preset": "non_critical_only"}),
            (
                "wp_plan_plugin_recovery",
                {"site_id": "mock-staging", "slug": "akismet", "action": "deactivate"},
            ),
        ):
            denied = tools.execute(tool_name, args, actor=actor)
            assert denied.data["ok"] is False, (tool_name, actor)
            assert denied.data["error_class"] == "AccessDenied", (tool_name, actor)

    # wp_plan_plugin_updates RBAC: viewer denied; operator allowed staging non-critical only, denied prod or critical
    v_upd = tools.execute(
        "wp_plan_plugin_updates",
        {"site_id": "mock-staging", "selection_mode": "policy", "policy": "non_critical_only"},
        actor="jonatan",
    )
    assert v_upd.data["ok"] is False and v_upd.data["error_class"] == "AccessDenied"

    op_staging = tools.execute(
        "wp_plan_plugin_updates",
        {"site_id": "mock-staging", "selection_mode": "policy", "policy": "non_critical_only"},
        actor="agent",
    )
    assert op_staging.data["ok"] is True, op_staging.data
    assert op_staging.data["executed"] is False
    assert op_staging.data["approval_required"] is True
    assert str(op_staging.data["plan_id"]).startswith("plu-")

    for denied_args in (
        {"site_id": "mock-shop", "selection_mode": "policy", "policy": "non_critical_only"},
        {
            "site_id": "mock-staging",
            "selection_mode": "all",
            "include_critical": True,
        },
    ):
        op_denied = tools.execute("wp_plan_plugin_updates", denied_args, actor="agent")
        assert op_denied.data["ok"] is False and op_denied.data["error_class"] == "AccessDenied"

    # Oscar (WP_OPS_ADMIN) can run diagnostics and create preview-only plu-*/pau-*/plr-* plans (never executed)
    diag = tools.execute(
        "wp_plugin_diagnostics",
        {"site_id": "mock-staging", "hooks": ["init"], "log_lines": 20},
        actor="oscar",
    )
    assert diag.data["ok"] is True, diag.data
    assert isinstance(diag.data.get("debug_log_tail"), list)

    plu = tools.execute(
        "wp_plan_plugin_updates",
        {"site_id": "mock-shop", "selection_mode": "policy", "policy": "non_critical_only"},
        actor="oscar",
    )
    assert plu.data["ok"] is True and plu.data["executed"] is False and plu.data["approval_required"] is True
    assert str(plu.data["plan_id"]).startswith("plu-")

    pau = tools.execute(
        "wp_plan_plugin_autoupdate",
        {"site_id": "mock-staging", "preset": "non_critical_only"},
        actor="oscar",
    )
    assert pau.data["ok"] is True and pau.data["executed"] is False and pau.data["approval_required"] is True
    assert str(pau.data["plan_id"]).startswith("pau-")

    plr = tools.execute(
        "wp_plan_plugin_recovery",
        {"site_id": "mock-staging", "slug": "akismet", "action": "deactivate"},
        actor="oscar",
    )
    assert plr.data["ok"] is True and plr.data["executed"] is False and plr.data["approval_required"] is True
    assert str(plr.data["plan_id"]).startswith("plr-")

    # Upgraded wp_plugin_overview fields (requires_plugins, is_critical, hook_priorities, auto_update, update_available)
    write_plugin_file(
        tmp_path,
        "mock-staging",
        "azom-custom",
        "azom-custom.php",
        (
            "<?php\n/**\n * Plugin Name: Azom Custom\n * Requires Plugins: woocommerce, acf\n"
            " * WC tested up to: 9.8\n */\n"
            "defined('ABSPATH') || exit;\n"
            "add_action('plugins_loaded', 'azom_custom_boot', 20);\n"
        ),
    )
    ov = tools.execute(
        "wp_plugin_overview",
        {"site_id": "mock-staging", "slug": "azom-custom"},
        actor="oscar",
    )
    assert ov.data["ok"] is True, ov.data
    assert ov.data["requires_plugins"] == ["woocommerce", "acf"]
    assert ov.data["is_critical"] is False
    assert "auto_update" in ov.data and "update_available" in ov.data and "new_version" in ov.data
    assert any(
        hp["hook"] == "plugins_loaded" and hp["priority"] == 20 for hp in ov.data["hook_priorities"]
    )
