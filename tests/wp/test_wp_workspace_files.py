"""Tests for WP component listing, workspace file creation/patching, parent copy, and status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import (
    MOCK_REGISTRY,
    REPO_ROOT,
    snapshot_tree,
    write_plugin_file,
    write_theme_file,
)

SOURCE_SECRET = "AZOM-SECRET-SRC-91b7"

from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import VerifiedTheme
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


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpOpsService:
    _setup_mock_env(tmp_path, monkeypatch)
    return WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)


def _tools(service: WpOpsService) -> WpAgentTools:
    return WpAgentTools(service)


def test_wp_list_components_lists_themes_and_underscore_plugins(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    write_theme_file(tmp_path, "mock-staging", "twentytwentyfive", "style.css", "/* t25 */\n")
    write_theme_file(tmp_path, "mock-staging", "storefront_child", "style.css", "/* child */\n")
    write_plugin_file(tmp_path, "mock-staging", "woocommerce", "woocommerce.php", "<?php\n")
    write_plugin_file(
        tmp_path,
        "mock-staging",
        "my_custom_plugin",
        "my_custom_plugin.php",
        "<?php\n/* Plugin Name: My Custom */\n",
    )

    themes = tools.execute(
        "wp_list_components",
        {"site_id": "mock-staging", "kind": "theme"},
        actor="jonatan",
    )
    assert themes.data["ok"] is True
    assert set(themes.data["slugs"]) == {"storefront_child", "twentytwentyfive"}

    plugins = tools.execute(
        "wp_list_components",
        {"site_id": "mock-staging", "kind": "plugin"},
        actor="jonatan",
    )
    assert plugins.data["ok"] is True
    assert "my_custom_plugin" in plugins.data["slugs"]
    assert "woocommerce" in plugins.data["slugs"]


def test_workspace_create_file_nested_and_rejections(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin", "summary": "Site plugin tweaks"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]
    assert plan_id.startswith("wr-")

    created = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "woocommerce/single-product/price.php",
            "content": "<?php\n// custom price template\n",
        },
        actor="oscar",
    )
    assert created.data["ok"] is True
    assert created.data["path"] == "woocommerce/single-product/price.php"
    work_file = (
        tmp_path
        / "wp_workspaces"
        / "mock-staging"
        / plan_id
        / "work"
        / "woocommerce"
        / "single-product"
        / "price.php"
    )
    assert work_file.is_file()

    dup = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "woocommerce/single-product/price.php",
            "content": "<?php\n",
        },
        actor="oscar",
    )
    assert dup.data["ok"] is False
    assert dup.data["error_class"] == "AlreadyExists"

    # Nested traversal, sensitive files, and disallowed extensions via tool dispatcher
    traversal = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "woocommerce/../wp-config.php",
            "content": "<?php\n",
        },
        actor="oscar",
    )
    assert traversal.data["ok"] is False
    assert traversal.data["error_class"] == "PathRejected"

    leading_dotdot = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "../outside.php",
            "content": "<?php\n",
        },
        actor="oscar",
    )
    assert leading_dotdot.data["ok"] is False
    assert leading_dotdot.data["error_class"] == "InvalidFileTarget"

    dotenv = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": ".env",
            "content": "SECRET=1\n",
        },
        actor="oscar",
    )
    assert dotenv.data["ok"] is False
    assert dotenv.data["error_class"] == "InvalidFileTarget"

    wp_config = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "wp-config.php",
            "content": "<?php\n",
        },
        actor="oscar",
    )
    assert wp_config.data["ok"] is False
    assert wp_config.data["error_class"] == "PathRejected"

    exe_file = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "bin/payload.exe",
            "content": "MZ",
        },
        actor="oscar",
    )
    assert exe_file.data["ok"] is False
    assert exe_file.data["error_class"] == "PathRejected"

    # Also verify ReviewPlans.create_file directly rejects ".env" and "../escape.php" with PathRejected
    ops = ReviewPlans(data_dir=tmp_path, registry_path=REPO_ROOT / "config" / "wp_sites.mock.yaml")
    for bad in (".env", "../escape.php"):
        res = ops.create_file("mock-staging", plan_id, bad, "x", actor="oscar")
        assert res.ok is False
        assert res.data["error_class"] == "PathRejected"


def test_workspace_patch_file_success_errors_and_isolation(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    mock_file = write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "style.css",
        "/* untouchable */\n",
    )
    mock_before = mock_file.read_bytes()

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin", "summary": "Patch test"},
        actor="oscar",
    )
    plan_id = opened.data["plan_id"]
    ws_root = tmp_path / "wp_workspaces" / "mock-staging" / plan_id
    base_before = snapshot_tree(ws_root / "base")

    created = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "includes/helper.php",
            "content": "<?php\n// line alpha\n// dup\n// dup\nreturn 1;\n",
        },
        actor="oscar",
    )
    assert created.data["ok"] is True
    initial_work_sha = created.data["work_sha256"]
    file_sha = created.data["after_sha256"]

    # Unique match succeeds and updates work_sha256
    patched = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "includes/helper.php",
            "old_text": "// line alpha",
            "new_text": "// line beta",
            "base_sha256": file_sha,
        },
        actor="oscar",
    )
    assert patched.data["ok"] is True
    assert patched.data["work_sha256"] != initial_work_sha
    assert patched.data["before_sha256"] == file_sha
    assert patched.data["after_sha256"] != file_sha

    # PatchTargetNotFound
    not_found = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "includes/helper.php",
            "old_text": "// missing target",
            "new_text": "// anything",
        },
        actor="oscar",
    )
    assert not_found.data["ok"] is False
    assert not_found.data["error_class"] == "PatchTargetNotFound"

    # PatchAmbiguous
    ambiguous = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "includes/helper.php",
            "old_text": "// dup",
            "new_text": "// unique",
        },
        actor="oscar",
    )
    assert ambiguous.data["ok"] is False
    assert ambiguous.data["error_class"] == "PatchAmbiguous"

    # DigestMismatch (wrong sha)
    mismatch = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "includes/helper.php",
            "old_text": "// line beta",
            "new_text": "// line gamma",
            "base_sha256": "0" * 64,
        },
        actor="oscar",
    )
    assert mismatch.data["ok"] is False
    assert mismatch.data["error_class"] == "DigestMismatch"

    # NoChange
    no_change = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "includes/helper.php",
            "old_text": "// line beta",
            "new_text": "// line beta",
        },
        actor="oscar",
    )
    assert no_change.data["ok"] is False
    assert no_change.data["error_class"] == "NoChange"

    # Neither base/ nor wp_files_mock was altered
    assert snapshot_tree(ws_root / "base") == base_before
    assert mock_file.read_bytes() == mock_before


def test_workspace_copy_from_parent_oscar_succeeds_and_jonatan_denied(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    parent_tpl = write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "woocommerce/single-product/price.php",
        "<?php\n// parent price template\n",
    )
    parent_bytes = parent_tpl.read_bytes()
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: twentytwentyfive\n*/\n",
    )

    ops = ReviewPlans(data_dir=tmp_path, registry_path=REPO_ROOT / "config" / "wp_sites.mock.yaml")
    opened = ops.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="twentytwentyfive"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    ws_root = tmp_path / "wp_workspaces" / "mock-staging" / plan_id
    base_before = snapshot_tree(ws_root / "base")

    denied = tools.execute(
        "wp_workspace_copy_from_parent",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "woocommerce/single-product/price.php",
        },
        actor="jonatan",
    )
    assert denied.data["ok"] is False
    assert denied.data["error_class"] == "AccessDenied"
    assert denied.data["status"] == "denied"

    copied = tools.execute(
        "wp_workspace_copy_from_parent",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "woocommerce/single-product/price.php",
        },
        actor="oscar",
    )
    assert copied.data["ok"] is True
    assert copied.data["parent_slug"] == "twentytwentyfive"
    assert copied.data["path"] == "woocommerce/single-product/price.php"
    work_copy = ws_root / "work" / "woocommerce" / "single-product" / "price.php"
    assert work_copy.read_bytes() == parent_bytes
    assert snapshot_tree(ws_root / "base") == base_before
    assert parent_tpl.read_bytes() == parent_bytes


def test_workspace_status_and_run_checks_honest_without_leaking_source(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin", "summary": "Checks test"},
        actor="oscar",
    )
    plan_id = opened.data["plan_id"]

    created = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "includes/logic.php",
            "content": f"<?php\n// {SOURCE_SECRET}\nfunction azom_ok() {{ return true; }}\n",
        },
        actor="oscar",
    )
    assert created.data["ok"] is True

    status = tools.execute(
        "wp_workspace_status",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="jonatan",
    )
    assert status.data["ok"] is True
    assert status.data["review_path"] == f"/wp/mock-staging/reviews/{plan_id}"
    assert any(c["path"] == "includes/logic.php" for c in status.data["changed_files"])
    assert SOURCE_SECRET not in json.dumps(status.data)

    checked = tools.execute(
        "wp_workspace_run_checks",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="oscar",
    )
    assert checked.data["ok"] is True
    assert checked.data["status"] == "checks_incomplete"
    assert checked.data["review_path"] == f"/wp/mock-staging/reviews/{plan_id}"
    assert checked.data["executed"] is False
    assert checked.data["live_calls"] is False
    assert any(row["status"] == "unavailable" for row in checked.data["checks"])
    assert SOURCE_SECRET not in json.dumps(checked.data)
