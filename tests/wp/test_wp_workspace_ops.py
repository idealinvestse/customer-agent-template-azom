"""Tests for WP workspace atomic apply, bulk replace, scaffolding, theme.json, Woo copy, and search."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import (
    MOCK_REGISTRY,
    REPO_ROOT,
    sha256_text,
    write_plugin_file,
    write_theme_file,
)

from ecom_ops.model_router import reset_model_router
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import workspace_root
from ecom_ops.wp_ops.service import WpOpsService


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
    reset_model_router()


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpOpsService:
    _setup_mock_env(tmp_path, monkeypatch)
    return WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)


def _tools(service: WpOpsService) -> WpAgentTools:
    return WpAgentTools(service)


def test_workspace_apply_changes_atomic_and_rollback_on_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/* Theme Name: Azom Child\nTemplate: parent\n*/\n",
    )
    write_theme_file(tmp_path, "mock-staging", "azom-child", "a.php", "<?php\n// alpha\n")
    write_theme_file(tmp_path, "mock-staging", "azom-child", "b.php", "<?php\n// beta\n")
    write_theme_file(tmp_path, "mock-staging", "azom-child", "old.php", "<?php\n// delete me\n")

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child"},
        actor="oscar",
    )
    plan_id = opened.data["plan_id"]
    ws_work = workspace_root(tmp_path, "mock-staging", plan_id) / "work"

    bad = tools.execute(
        "wp_workspace_apply_changes",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "changes": [
                {
                    "path": "a.php",
                    "op": "edit",
                    "edits": [{"old_text": "// alpha", "new_text": "// alpha2"}],
                },
                {
                    "path": "b.php",
                    "op": "edit",
                    "edits": [{"old_text": "// missing", "new_text": "// x"}],
                },
            ],
        },
        actor="agent",
    )
    assert bad.data["ok"] is False
    assert bad.data["error_class"] == "PatchTargetNotFound"
    assert (ws_work / "a.php").read_text(encoding="utf-8") == "<?php\n// alpha\n"

    orig_write_bytes = Path.write_bytes
    write_count = {"n": 0}

    def flaky_write_bytes(self: Path, data: bytes) -> int:
        if self.name in {"a.php", "new.php"} and self.parent == ws_work:
            write_count["n"] += 1
            if write_count["n"] == 2:
                raise OSError("disk full")
        return orig_write_bytes(self, data)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "write_bytes", flaky_write_bytes)
        io_fail = tools.execute(
            "wp_workspace_apply_changes",
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "changes": [
                    {
                        "path": "a.php",
                        "op": "edit",
                        "edits": [{"old_text": "// alpha", "new_text": "// partial"}],
                    },
                    {"path": "new.php", "op": "create", "content": "<?php\n// created\n"},
                ],
            },
            actor="oscar",
        )

    assert io_fail.data["ok"] is False
    assert io_fail.data["error_class"] == "WriteFailed"
    assert (ws_work / "a.php").read_text(encoding="utf-8") == "<?php\n// alpha\n"
    assert not (ws_work / "new.php").exists()

    ok_apply = tools.execute(
        "wp_workspace_apply_changes",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "changes": [
                {
                    "path": "a.php",
                    "op": "edit",
                    "base_sha256": sha256_text("<?php\n// alpha\n"),
                    "edits": [{"old_text": "// alpha", "new_text": "// updated"}],
                },
                {"path": "inc/created.php", "op": "create", "content": "<?php\n// new\n"},
                {"path": "old.php", "op": "delete"},
            ],
        },
        actor="agent",
    )
    assert ok_apply.data["ok"] is True
    assert ok_apply.data["count"] == 3
    assert (ws_work / "a.php").read_text(encoding="utf-8") == "<?php\n// updated\n"
    assert (ws_work / "inc/created.php").is_file()
    assert not (ws_work / "old.php").exists()

    reverted = tools.execute(
        "wp_workspace_apply_changes",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "changes": [
                {"path": "a.php", "op": "revert"},
                {"path": "inc/created.php", "op": "revert"},
            ],
        },
        actor="agent",
    )
    assert reverted.data["ok"] is True
    assert (ws_work / "a.php").read_text(encoding="utf-8") == "<?php\n// alpha\n"
    assert not (ws_work / "inc/created.php").exists()


def test_workspace_bulk_replace_dry_run_vs_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin"},
        actor="agent",
    )
    plan_id = opened.data["plan_id"]
    for rel, body in (
        ("inc/one.php", "<?php\nazom_old_prefix_run();\n"),
        ("inc/two.php", "<?php\nazom_old_prefix_stop();\n"),
        ("assets/app.js", "azom_old_prefix_js();\n"),
    ):
        assert (
            tools.execute(
                "wp_workspace_create_file",
                {"site_id": "mock-staging", "plan_id": plan_id, "path": rel, "content": body},
                actor="agent",
            ).data["ok"]
            is True
        )

    before_status = tools.execute(
        "wp_workspace_status", {"site_id": "mock-staging", "plan_id": plan_id}, actor="jonatan"
    )
    before_sha = before_status.data["work_sha256"]

    dry = tools.execute(
        "wp_workspace_bulk_replace",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "find": r"azom_old_prefix_(\w+)",
            "replace": r"azom_new_\1",
            "mode": "regex",
            "extensions": [".php"],
            "path_prefix": "inc",
            "dry_run": True,
        },
        actor="agent",
    )
    assert dry.data["ok"] is True
    assert dry.data["dry_run"] is True
    assert dry.data["files_changed"] == 2
    assert dry.data["total_matches"] == 2
    assert {f["path"] for f in dry.data["files"]} == {"inc/one.php", "inc/two.php"}

    ws_work = workspace_root(tmp_path, "mock-staging", plan_id) / "work"
    assert "azom_old_prefix_run" in (ws_work / "inc/one.php").read_text(encoding="utf-8")

    direct_dry = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY).workspace_bulk_replace(
        "mock-staging", plan_id, "azom_old_prefix_", "azom_new_", actor="agent", dry_run=True
    )
    assert "+azom_new_run();" in direct_dry.data["diff"]
    assert (
        tools.execute(
            "wp_workspace_status",
            {"site_id": "mock-staging", "plan_id": plan_id},
            actor="jonatan",
        ).data["work_sha256"]
        == before_sha
    )

    applied = tools.execute(
        "wp_workspace_bulk_replace",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "find": r"azom_old_prefix_(\w+)",
            "replace": r"azom_new_\1",
            "mode": "regex",
            "extensions": [".php"],
            "path_prefix": "inc",
            "dry_run": False,
        },
        actor="agent",
    )
    assert applied.data["ok"] is True
    assert applied.data["files_changed"] == 2
    assert applied.data["work_sha256"] != before_sha
    assert "azom_new_run();" in (ws_work / "inc/one.php").read_text(encoding="utf-8")
    assert "azom_old_prefix_js();" in (ws_work / "assets/app.js").read_text(encoding="utf-8")


def test_workspace_scaffold_all_three_templates_and_theme_json_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    theme_ws = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "brand-child"},
        actor="agent",
    )
    assert theme_ws.data["ok"] is True
    theme_pid = theme_ws.data["plan_id"]

    denied_scaffold = tools.execute(
        "wp_workspace_scaffold",
        {"site_id": "mock-staging", "plan_id": theme_pid, "template": "child_theme_basics"},
        actor="jonatan",
    )
    assert denied_scaffold.data["error_class"] == "AccessDenied"

    sc_child = tools.execute(
        "wp_workspace_scaffold",
        {"site_id": "mock-staging", "plan_id": theme_pid, "template": "child_theme_basics"},
        actor="agent",
    )
    assert sc_child.data["ok"] is True
    assert set(sc_child.data["created"]) == {"style.css", "functions.php"}

    tj_ok = tools.execute(
        "wp_workspace_theme_json",
        {
            "site_id": "mock-staging",
            "plan_id": theme_pid,
            "operations": [
                {"op": "set", "pointer": "/styles/color/text", "value": "#111111"},
                {"op": "set", "pointer": "/settings/layout/contentSize", "value": "800px"},
            ],
        },
        actor="agent",
    )
    assert tj_ok.data["ok"] is True
    assert tj_ok.data["version"] == 3
    tj_bad = tools.execute(
        "wp_workspace_theme_json",
        {
            "site_id": "mock-staging",
            "plan_id": theme_pid,
            "operations": [{"op": "set", "pointer": "/settings/__proto__/bad", "value": 1}],
        },
        actor="agent",
    )
    assert tj_bad.data["ok"] is False
    assert tj_bad.data["error_class"] == "InvalidThemeJson"

    plugin_ws = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin"},
        actor="agent",
    )
    plugin_pid = plugin_ws.data["plan_id"]
    ws_work = workspace_root(tmp_path, "mock-staging", plugin_pid) / "work"
    (ws_work / "azom-site-mock-staging.php").unlink()

    sc_plugin = tools.execute(
        "wp_workspace_scaffold",
        {"site_id": "mock-staging", "plan_id": plugin_pid, "template": "site_plugin_basics"},
        actor="agent",
    )
    assert sc_plugin.data["ok"] is True
    assert sc_plugin.data["created"] == ["azom-site-mock-staging.php"]

    sc_hpos = tools.execute(
        "wp_workspace_scaffold",
        {
            "site_id": "mock-staging",
            "plan_id": plugin_pid,
            "template": "woo_compat_declaration",
        },
        actor="agent",
    )
    assert sc_hpos.data["ok"] is True
    assert "includes/woo-hpos-compat.php" in sc_hpos.data["created"]
    assert "azom-site-mock-staging.php" in sc_hpos.data["updated"]
    assert "custom_order_tables" in (ws_work / "azom-site-mock-staging.php").read_text(
        encoding="utf-8"
    )

    tj_plugin = tools.execute(
        "wp_workspace_theme_json",
        {
            "site_id": "mock-staging",
            "plan_id": plugin_pid,
            "operations": [{"op": "set", "pointer": "/settings/x", "value": 1}],
        },
        actor="agent",
    )
    assert tj_plugin.data["ok"] is False
    assert tj_plugin.data["error_class"] == "InvalidTarget"


def test_woo_copy_audit_hierarchy_intel_list_and_search_enhancements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)
    secret = "Bearer SearchSecretToken9988"

    write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "style.css",
        "/* Theme Name: Twenty Twenty-Five */\n",
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: twentytwentyfive\n*/\n",
    )
    write_theme_file(
        tmp_path, "mock-staging", "azom-child", "front-page.php", "<?php\n// front\n"
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "woocommerce/cart/cart.php",
        "<?php\n/**\n * @version 7.0.0\n */\n",
    )
    write_plugin_file(
        tmp_path,
        "mock-staging",
        "woocommerce",
        "templates/single-product/price.php",
        "<?php\n/**\n * @version 3.0.0\n */\n// woo core price\n",
    )

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child"},
        actor="oscar",
    )
    plan_id = opened.data["plan_id"]

    copied_woo = tools.execute(
        "wp_workspace_copy_woo_template",
        {"site_id": "mock-staging", "plan_id": plan_id, "template": "single-product/price.php"},
        actor="oscar",
    )
    assert copied_woo.data["ok"] is True
    assert copied_woo.data["path"] == "woocommerce/single-product/price.php"
    assert copied_woo.data["source_template"] == "woocommerce/templates/single-product/price.php"

    plug_ws = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin"},
        actor="oscar",
    )
    plug_copy = tools.execute(
        "wp_workspace_copy_woo_template",
        {
            "site_id": "mock-staging",
            "plan_id": plug_ws.data["plan_id"],
            "template": "single-product/price.php",
        },
        actor="oscar",
    )
    assert plug_copy.data["ok"] is False
    assert plug_copy.data["error_class"] == "InvalidTarget"

    audit = tools.execute(
        "wc_template_overrides_audit",
        {"site_id": "mock-staging", "theme_slug": "azom-child"},
        actor="jonatan",
    )
    assert audit.data["ok"] is True
    assert audit.data["outdated"] == 1
    assert audit.data["critical_overrides"] == ["woocommerce/cart/cart.php"]

    intel = tools.execute(
        "wp_component_intel",
        {"kind": "plugin", "slug": "woocommerce", "installed_version": "9.0.0"},
        actor="jonatan",
    )
    assert intel.data["ok"] is True
    assert intel.data["source"] == "mock"
    assert intel.data["newer_available"] is True

    hier = tools.execute(
        "wp_template_hierarchy",
        {"site_id": "mock-staging", "context": "front_page", "theme_slug": "azom-child"},
        actor="jonatan",
    )
    assert hier.data["ok"] is True
    assert hier.data["theme_kind"] == "classic"
    assert hier.data["parent_theme"] == "twentytwentyfive"
    assert hier.data["first_match"] == "front-page.php"

    ws_list = tools.execute("wp_list_workspaces", {"site_id": "mock-staging"}, actor="jonatan")
    assert ws_list.data["ok"] is True
    assert {p["plan_id"] for p in ws_list.data["plans"]} >= {plan_id, plug_ws.data["plan_id"]}

    files_list = tools.execute(
        "wp_workspace_list_files",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="jonatan",
    )
    assert files_list.data["ok"] is True
    statuses = {f["path"]: f["status"] for f in files_list.data["files"]}
    assert statuses["woocommerce/single-product/price.php"] == "created"
    assert statuses["front-page.php"] == "unchanged"

    assert (
        tools.execute(
            "wp_workspace_create_file",
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "path": "inc/search_target.php",
                "content": f"<?php\n// line before {secret}\nfunction azom_marker_42() {{}}\n// line after\n",
            },
            actor="oscar",
        ).data["ok"]
        is True
    )

    found = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "query": r"azom_marker_\d+",
            "mode": "regex",
            "context_lines": 1,
        },
        actor="oscar",
    )
    assert found.data["ok"] is True
    assert found.data["source_scope"] == "workspace"
    assert found.data["mode"] == "regex"
    assert len(found.data["hits"]) == 1
    hit = found.data["hits"][0]
    assert hit["path"] == "inc/search_target.php"
    assert hit["before"] and "[REDACTED]" in hit["before"][0]
    assert secret not in json.dumps(found.data)
    assert hit["after"] == ["// line after"]
