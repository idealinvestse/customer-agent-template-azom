"""Unit and integration tests for WP Ops toolchain v2 (mock only, no network)."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest

from ecom_ops.bot.chat_agent import run_chat
from ecom_ops.bot.openclaw_commands import (
    TELEGRAM_MENU_COMMANDS,
    CommandContext,
    cmd_tools,
)
from ecom_ops.bot.store import ConversationStore
from ecom_ops.integrations.wp_files_ssh import SshFileTransport
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import WpAgentTools, context_for_storage
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_runner import versions_unknown
from ecom_ops.wp_ops.review_workspace import VerifiedTheme
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SECRET = "AZOM-SECRET-SRC-91b7"


def _setup_mock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
    monkeypatch.setenv("XAI_GROK_BIN", "")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpOpsService:
    _setup_mock_env(tmp_path, monkeypatch)
    return WpOpsService(
        registry_path=ROOT / "config" / "wp_sites.mock.yaml",
        data_dir=tmp_path,
    )


def _tools(service: WpOpsService) -> WpAgentTools:
    return WpAgentTools(service)


def _mock_root(tmp_path: Path, site_id: str = "mock-staging") -> Path:
    return tmp_path / "wp_files_mock" / site_id / "wp-content"


def _write_theme_file(
    tmp_path: Path,
    site_id: str,
    slug: str,
    rel: str,
    content: str,
) -> Path:
    target = _mock_root(tmp_path, site_id) / "themes" / slug / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _write_plugin_file(
    tmp_path: Path,
    site_id: str,
    slug: str,
    rel: str,
    content: str,
) -> Path:
    target = _mock_root(tmp_path, site_id) / "plugins" / slug / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tool_call(name: str, arguments: dict | None = None) -> str:
    return f'<tool_call>{json.dumps({"name": name, "arguments": arguments or {}})}</tool_call>'


def _snapshot_tree(folder: Path) -> dict[str, bytes]:
    if not folder.is_dir():
        return {}
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _site_row(site_id: str = "stage", **overrides: object) -> dict:
    row: dict[str, object] = {
        "id": site_id,
        "display_name": site_id,
        "canonical_url": f"https://{site_id}.example.test/",
        "environment": "staging",
        "business_owner": "customer",
        "hosting_provider": "unconfigured",
        "wordpress_version": "6.7",
        "php_version": "8.3",
        "woocommerce": True,
        "woo_version": "9.6",
        "critical_urls": [f"https://{site_id}.example.test/"],
        "maintenance_window": "not agreed",
        "secret_ref": "WP_SECRET_SHOP",
        "alert_owner": "unassigned",
        "alert_confirmations": 2,
        "check_interval_seconds": 60,
        "timeout_seconds": 10,
        "dedupe_window_seconds": 30,
        "capabilities": {"uptime": "available"},
        "files_root": "/var/www/shop",
        "ssh_host": "shop.internal",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. Routing fallthrough
# ---------------------------------------------------------------------------


def test_routing_fallthrough_without_context_refuses_without_llm(tmp_path, monkeypatch):
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")

    def fail_llm(*_args, **_kwargs):
        raise AssertionError("chat_completion must not be called without wp_ops_context")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fail_llm)

    result = run_chat(
        "ändra functions.php",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
    )
    assert result.pending_action is None
    assert "ändras inte" in result.text.lower() or "inte" in result.text.lower()


def test_routing_fallthrough_with_context_executes_file_loop_and_max_tokens(tmp_path, monkeypatch):
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    monkeypatch.delenv("AZOM_TELEGRAM_MODEL_ROUTE", raising=False)

    _write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "functions.php",
        f"<?php\n// {SOURCE_SECRET}\nadd_action('init', 'azom_init');\n",
    )

    ctx = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        site_id="mock-staging",
        operation_ids=(),
    )
    assert ctx is not None

    responses = iter(
        [
            _tool_call(
                "wp_read_theme_plugin_file",
                {
                    "kind": "theme",
                    "slug": "twentytwentyfive",
                    "path": "functions.php",
                    "start_line": 1,
                    "end_line": 3,
                },
            ),
            "Filen functions.php registrerar en init-hook i temat.",
        ]
    )
    calls: list[tuple[list[dict[str, str]], dict]] = []

    def fake_chat(messages, **kwargs):
        calls.append((list(messages), dict(kwargs)))
        return next(responses), 0.001

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)

    result = run_chat(
        "läs functions.php i temat",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        session={"wp_ops_context": ctx},
    )

    assert len(calls) == 2
    assert calls[0][1]["max_tokens"] >= 2048
    assert calls[1][1]["max_tokens"] >= 2048
    assert any(SOURCE_SECRET in json.dumps(m) for m in calls[1][0])
    assert SOURCE_SECRET not in result.text
    assert "<tool_call>" not in result.text
    assert "init-hook" in result.text
    assert result.wp_ops_context is not None
    assert result.wp_ops_context["site_id"] == "mock-staging"


# ---------------------------------------------------------------------------
# 2. Workspace tools end-to-end (wr-*)
# ---------------------------------------------------------------------------


def test_wp_list_components_lists_themes_and_underscore_plugins(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    _write_theme_file(tmp_path, "mock-staging", "twentytwentyfive", "style.css", "/* t25 */\n")
    _write_theme_file(tmp_path, "mock-staging", "storefront_child", "style.css", "/* child */\n")
    _write_plugin_file(tmp_path, "mock-staging", "woocommerce", "woocommerce.php", "<?php\n")
    _write_plugin_file(
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
    ops = ReviewPlans(data_dir=tmp_path, registry_path=ROOT / "config" / "wp_sites.mock.yaml")
    for bad in (".env", "../escape.php"):
        res = ops.create_file("mock-staging", plan_id, bad, "x", actor="oscar")
        assert res.ok is False
        assert res.data["error_class"] == "PathRejected"


def test_workspace_patch_file_success_errors_and_isolation(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    mock_file = _write_theme_file(
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
    base_before = _snapshot_tree(ws_root / "base")

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
    assert _snapshot_tree(ws_root / "base") == base_before
    assert mock_file.read_bytes() == mock_before


def test_workspace_copy_from_parent_oscar_succeeds_and_jonatan_denied(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    parent_tpl = _write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "woocommerce/single-product/price.php",
        "<?php\n// parent price template\n",
    )
    parent_bytes = parent_tpl.read_bytes()
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: twentytwentyfive\n*/\n",
    )

    ops = ReviewPlans(data_dir=tmp_path, registry_path=ROOT / "config" / "wp_sites.mock.yaml")
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
    base_before = _snapshot_tree(ws_root / "base")

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
    assert _snapshot_tree(ws_root / "base") == base_before
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


# ---------------------------------------------------------------------------
# 3. Line-based reading
# ---------------------------------------------------------------------------


def test_read_theme_plugin_file_line_based_and_argument_guards(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    lines = [f"// line {i}" for i in range(1, 11)]
    path = _write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "functions.php",
        "\n".join(lines) + "\n",
    )
    digest = _sha256_bytes(path.read_bytes())

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


# ---------------------------------------------------------------------------
# 4. Overview, search, SFTP batching & bugfixes
# ---------------------------------------------------------------------------


def test_wp_theme_overview_classic_block_tree_woo_and_redaction(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    # Classic theme with 3-level tree and WooCommerce override
    _write_theme_file(tmp_path, "mock-staging", "classic-shop", "style.css", "/* Classic */\n")
    _write_theme_file(tmp_path, "mock-staging", "classic-shop", "inc/sub/helper.php", "<?php\n")
    _write_theme_file(
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
    _write_theme_file(tmp_path, "mock-staging", "block-shop", "theme.json", '{"version": 2}\n')
    _write_theme_file(
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
    _write_plugin_file(
        tmp_path,
        "mock-staging",
        "third-party-ship",
        "third-party-ship.php",
        third_party_body,
    )
    _write_plugin_file(
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
        _write_theme_file(
            tmp_path,
            "mock-staging",
            "twentytwentyfive",
            f"inc/file_{idx:02d}.php",
            f"<?php\n// filler {idx}\n",
        )
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "inc/file_25.php",
        "<?php\n// AzomNeedleMarker found in file 25\n",
    )
    _write_theme_file(
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


def test_ssh_open_batch_opens_single_socket_and_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "1")

    private = paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "ssh-keys" / "stage"
    key_path.parent.mkdir(parents=True)
    private.write_private_key_file(str(key_path))
    host_key = f"{private.get_name()} {private.get_base64()}"

    site = replace(
        parse_site(_site_row()),
        ssh_host="shop.internal",
        ssh_user="azom-wp-reader",
        ssh_port=2222,
        host_public_key=host_key,
    )

    counts = {"socket": 0, "transport": 0, "sftp": 0, "closed": 0}
    remote_files = {
        "/var/www/shop/wp-content/themes/twentytwentyfive/style.css": b"/* style */\n",
        "/var/www/shop/wp-content/themes/twentytwentyfive/functions.php": b"<?php\n",
    }
    remote_dirs = {
        "/var/www/shop/wp-content/themes/twentytwentyfive": ["style.css", "functions.php"],
        "/var/www/shop/wp-content/themes": ["twentytwentyfive"],
    }

    class FakeSock:
        def close(self) -> None:
            return None

    class FakeFile:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def settimeout(self, _timeout: float) -> None:
            return None

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644,
                st_size=len(self._data),
                st_ino=101,
            )

        def read(self, _n: int) -> bytes:
            return self._data

        def close(self) -> None:
            return None

    class FakeChannel:
        def settimeout(self, _timeout: float) -> None:
            return None

    class FakeSFTP:
        def get_channel(self) -> FakeChannel:
            return FakeChannel()

        def lstat(self, path: str) -> SimpleNamespace:
            if path in remote_files:
                return SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o644,
                    st_size=len(remote_files[path]),
                    st_ino=101,
                )
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=0, st_ino=1)

        def listdir_attr(self, path: str) -> list[SimpleNamespace]:
            names = remote_dirs.get(path, [])
            out = []
            for name in names:
                full = f"{path}/{name}"
                is_file = full in remote_files
                out.append(
                    SimpleNamespace(
                        filename=name,
                        st_mode=(stat.S_IFREG | 0o644) if is_file else (stat.S_IFDIR | 0o755),
                    )
                )
            return out

        def open(self, path: str, _mode: str) -> FakeFile:
            return FakeFile(remote_files[path])

        def close(self) -> None:
            counts["closed"] += 1

    class FakeTransport:
        def __init__(self, _sock: object) -> None:
            counts["transport"] += 1
            self.banner_timeout = 0
            self.auth_timeout = 0

        def set_log_channel(self, _name: str) -> None:
            return None

        def start_client(self, timeout: int | None = None) -> None:
            del timeout

        def get_remote_server_key(self) -> paramiko.RSAKey:
            return private

        def auth_publickey(self, _user: str, _key: object) -> None:
            return None

        def is_authenticated(self) -> bool:
            return True

        def close(self) -> None:
            return None

    def fake_connect(*_args, **_kwargs):
        counts["socket"] += 1
        return FakeSock()

    def fake_from_transport(_transport: object) -> FakeSFTP:
        counts["sftp"] += 1
        return FakeSFTP()

    monkeypatch.setattr("ecom_ops.integrations.wp_files_ssh.socket.create_connection", fake_connect)
    monkeypatch.setattr("ecom_ops.integrations.wp_files_ssh.paramiko.Transport", FakeTransport)
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_files_ssh.paramiko.SFTPClient.from_transport",
        staticmethod(fake_from_transport),
    )

    transport = SshFileTransport(site, tmp_path)
    with transport.open_batch() as batch:
        themes = batch.list_dir("/var/www/shop/wp-content/themes")
        files = batch.list_dir("/var/www/shop/wp-content/themes/twentytwentyfive")
        style_bytes = batch.read_bytes("/var/www/shop/wp-content/themes/twentytwentyfive/style.css")
        func_bytes = batch.read_bytes(
            "/var/www/shop/wp-content/themes/twentytwentyfive/functions.php"
        )

    assert themes == ["twentytwentyfive"]
    assert files == ["functions.php", "style.css"]
    assert style_bytes == b"/* style */\n"
    assert func_bytes == b"<?php\n"
    assert counts["socket"] == 1
    assert counts["transport"] == 1
    assert counts["sftp"] == 1
    assert counts["closed"] == 1


def test_site_woo_version_and_versions_unknown():
    populated = parse_site(_site_row(woo_version="9.6.1"))
    assert populated.woo_version == "9.6.1"
    assert versions_unknown(populated) is False

    missing_woo = parse_site(_site_row(woo_version=""))
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


def test_critical_plugin_toggle_warns_and_sets_critical_flag(monkeypatch):
    from ecom_ops.bot.site_content import site_content_turn

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")

    def boom(*_args, **_kwargs):
        raise AssertionError("llm must not run")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)

    turn = site_content_turn("stäng av plugin woocommerce")
    assert turn.consumed is True
    assert turn.pending is not None
    assert turn.pending.kind == "plugin_status"
    assert turn.pending.payload["plugin"] == "woocommerce/woocommerce"
    assert turn.pending.payload["status"] == "inactive"
    assert turn.pending.payload["critical"] is True
    propose_text = dict(turn.results)["propose_plugin_status"]
    assert propose_text.startswith("VARNING:")

    result = run_chat("stäng av plugin woocommerce")
    assert result.pending_action is not None
    assert result.pending_action.kind == "plugin_status"
    assert result.pending_action.payload["critical"] is True


def test_telegram_menu_includes_wp_and_cmd_tools_lists_new_tools(tmp_path):
    menu_names = [entry["command"] for entry in TELEGRAM_MENU_COMMANDS]
    assert "wp" in menu_names

    ctx = CommandContext(
        chat_id=42,
        text="/tools",
        args="",
        store=ConversationStore(path=tmp_path / "tg.json"),
    )
    output = cmd_tools(ctx)
    for expected in (
        "wp_component_context",
        "wp_list_components",
        "wp_theme_overview",
        "wp_plugin_overview",
        "wp_list_component_tree",
        "wp_search_component",
        "wp_read_theme_plugin_file",
        "wp_open_review_workspace",
        "wp_workspace_*",
    ):
        assert expected in output
