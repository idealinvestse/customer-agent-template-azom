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
    assert len(EXTRA_FILE_TOOLS) == 32
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
