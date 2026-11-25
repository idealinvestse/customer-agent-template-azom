"""Comprehensive tests for Etapp 4 WordPress/WooCommerce engineering toolbox (v3)."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecom_ops.bot import __main__ as bot_main
from ecom_ops.bot import chat_agent, site_content, telegram_transport
from ecom_ops.bot import job_store as job_store_mod
from ecom_ops.bot.chat_agent import (
    WP_CODE_INTENT_RE,
    ChatCancelled,
    ChatResult,
    _build_system_prompt,
    _use_file_tool_loop,
    _use_read_tool_loop,
    gather_tool_results,
    run_chat,
)
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.progress_events import ProgressEvent, mark_event_consumer
from ecom_ops.bot.reply import BotReply
from ecom_ops.bot.site_content import site_content_turn
from ecom_ops.bot.store import ConversationStore, TelegramConversationStore
from ecom_ops.bot.telegram_transport import PerChatRateLimiter, TelegramApiError
from ecom_ops.integrations.wp_files_ssh import SshFileTransport
from ecom_ops.integrations.wp_org_client import WpOrgClientError, fetch_wporg_json
from ecom_ops.model_router import reset_model_router
from ecom_ops.rbac import ROLE_PERMISSIONS, Permission, clear_rbac_cache
from ecom_ops.wp_ops import agent_tools as agent_tools_mod
from ecom_ops.wp_ops import review_plan as review_plan_mod
from ecom_ops.wp_ops.agent_tools import (
    _WP_TOOL_LABELS,
    ALL_FILE_TOOLS,
    EXTRA_FILE_TOOLS,
    FILE_LOOP_MAX_CALLS,
    FILE_LOOP_MAX_READS,
    FILE_LOOP_MAX_SEARCHES,
    FILE_LOOP_MAX_SECONDS,
    FILE_TOOL_SCHEMAS,
    WP_TOOL_GUIDE,
    WP_TOOL_SCHEMAS,
    WpAgentTools,
    _safe_tool_detail,
    context_for_storage,
    run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.code_index import index_sources
from ecom_ops.wp_ops.file_changes import FILE_TOOL_NAMES, search_texts
from ecom_ops.wp_ops.files import LocalTree
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.page_review_loop import run_bounded_page_review_loop
from ecom_ops.wp_ops.playbooks import (
    PLAYBOOK_TOPICS,
    format_playbook_hints,
    get_playbook,
    select_playbooks,
)
from ecom_ops.wp_ops.review_checks import JOBS, requirements
from ecom_ops.wp_ops.review_knowledge import TEMPLATE_CONTEXTS, template_candidates
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import VerifiedTheme, workspace_root
from ecom_ops.wp_ops.service import WpOpsService
from ecom_ops.wp_ops.static_checks import run_static_checks
from ecom_ops.wp_ops.theme_json import apply_theme_json_operations
from ecom_ops.wp_ops.woo_templates import (
    audit_overrides,
    compare_versions,
    parse_template_version,
)
from ecom_ops.wp_ops.wporg_intel import fetch_component_intel, newer_available

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"


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
    reset_model_router()


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpOpsService:
    _setup_mock_env(tmp_path, monkeypatch)
    return WpOpsService(registry_path=REGISTRY, data_dir=tmp_path)


def _tools(service: WpOpsService) -> WpAgentTools:
    return WpAgentTools(service)


def _mock_content(tmp_path: Path, site_id: str = "mock-staging") -> Path:
    return tmp_path / "wp_files_mock" / site_id / "wp-content"


def _write_theme_file(
    tmp_path: Path, site_id: str, slug: str, rel: str, content: str
) -> Path:
    target = _mock_content(tmp_path, site_id) / "themes" / slug / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    return target


def _write_plugin_file(
    tmp_path: Path, site_id: str, slug: str, rel: str, content: str
) -> Path:
    target = _mock_content(tmp_path, site_id) / "plugins" / slug / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))
    return target


def _tool_call(name: str, arguments: dict[str, Any] | None = None) -> str:
    return f'<tool_call>{json.dumps({"name": name, "arguments": arguments or {}})}</tool_call>'


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _passing_catalog(kind: str, files: list[tuple[str, str]], risk: str, *, artifact_dir=None):
    del artifact_dir
    needed = requirements(kind, files, risk)
    rows: list[dict[str, Any]] = []
    for job in JOBS:
        is_req = needed[job] == "required"
        row: dict[str, Any] = {
            "job": job,
            "status": "passed" if is_req else "not_applicable",
            "summary": f"{job} ok" if is_req else f"{job} not_applicable",
            "command": ["mock", job] if is_req else None,
            "started_at": "2026-11-24T00:00:00+00:00" if is_req else None,
            "finished_at": "2026-11-24T00:00:01+00:00" if is_req else None,
            "exit_code": 0 if is_req else None,
            "artifact_path": None,
            "executed": is_req,
            "simulated": False,
        }
        if job == "woo_static" and is_req:
            row["evidence_class"] = "static_precheck"
            row["runtime_proof"] = False
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 1. Foundation modules
# ---------------------------------------------------------------------------


def test_review_knowledge_template_candidates_contexts_and_traversal() -> None:
    assert len(TEMPLATE_CONTEXTS) == 21

    front = template_candidates("front_page", slug="hem", theme_slug="azom-child")
    assert front["context"] == "front_page"
    assert front["candidates"][:3] == ["front-page.php", "page-hem.php", "page.php"]
    assert "templates/front-page.html" in front["block_candidates"]
    assert "azom-child" in front["notes"]

    prod = template_candidates("single", post_type="product", slug="elcykel")
    assert prod["candidates"][:2] == ["single-product-elcykel.php", "single-product.php"]
    assert prod["block_candidates"][:2] == [
        "templates/single-product-elcykel.html",
        "templates/single-product.html",
    ]
    assert "woocommerce/single-product.php" in prod["woo_candidates"]

    cat = template_candidates("taxonomy", taxonomy="product_cat", slug="cyklar")
    assert cat["candidates"][:2] == [
        "taxonomy-product_cat-cyklar.php",
        "taxonomy-product_cat.php",
    ]
    assert "woocommerce/taxonomy-product-cat.php" in cat["woo_candidates"]

    for ctx in (
        "home",
        "page",
        "singular",
        "archive",
        "category",
        "tag",
        "author",
        "date",
        "search",
        "404",
        "attachment",
        "woo_shop",
        "woo_product",
        "woo_category",
        "woo_tag",
        "woo_cart",
        "woo_checkout",
        "woo_account",
    ):
        res = template_candidates(ctx)
        assert res["context"] == ctx
        assert res["candidates"]
        assert res["block_candidates"]
        if ctx.startswith("woo_"):
            assert res["woo_candidates"]

    for bad_token in ("../etc", "a/b", "foo..bar", "bad space", "foo.bar"):
        with pytest.raises(ValueError):
            template_candidates("page", slug=bad_token)
        with pytest.raises(ValueError):
            template_candidates("page", theme_slug=bad_token)
    with pytest.raises(ValueError):
        template_candidates("unsupported_ctx")


def test_code_index_extracts_symbols_filters_and_scrubs_secrets() -> None:
    secret_val = "sk_live_" + "IndexSecret998877"
    const_secret = "ConstSecretValueXYZ123"
    sources = {
        "style.css": (
            "/*\n"
            "Theme Name: Azom Child\n"
            "Template: twentytwentyfive\n"
            "Version: 1.4.2\n"
            "*/\n"
        ),
        "azom-site.php": (
            "<?php\n"
            "/**\n"
            " * Plugin Name: Azom Site Helper\n"
            " * WC requires at least: 8.5\n"
            " * WC tested up to: 9.6\n"
            " */\n"
            "define('AZOM_VER', '1.4.2');\n"
            f"define('AZOM_API_KEY', '{const_secret}');\n"
            "interface AzomRenderable { public function render(); }\n"
            "trait AzomLogTrait {}\n"
            "class AzomBadge {\n"
            f"    public function build($api_key = '{secret_val}') {{ return $api_key; }}\n"
            "}\n"
            "function azom_boot_badge() {}\n"
            "add_action('init', 'azom_boot_badge');\n"
            "add_filter('the_title', 'azom_filter_title');\n"
            "add_shortcode('azom_badge', 'azom_render_badge');\n"
            "register_rest_route('azom/v1', '/status', ['methods' => 'GET']);\n"
            "register_block_type('azom/php-card', []);\n"
        ),
        "blocks/hero/block.json": json.dumps(
            {"apiVersion": 3, "name": "azom/hero", "title": "Azom Hero"}
        ),
        "assets/main.js": (
            "export function initHero() {}\n"
            "export const mountWidget = () => {};\n"
            "registerBlockType('azom/js-block', {});\n"
            "addAction('azom.mounted', 'azom/hero', () => {});\n"
        ),
    }

    full = index_sources(sources)
    assert full["total_files"] == 4
    kinds = {entry["kind"] for entry in full["entries"]}
    assert {
        "header",
        "constant",
        "interface",
        "trait",
        "class",
        "method",
        "function",
        "hook",
        "shortcode",
        "rest_route",
        "block",
    }.issubset(kinds)
    names = {(entry["kind"], entry["name"]) for entry in full["entries"]}
    assert ("method", "AzomBadge::build") in names
    assert ("function", "azom_boot_badge") in names
    assert ("function", "initHero") in names
    assert ("hook", "init") in names
    assert ("shortcode", "azom_badge") in names
    assert ("rest_route", "azom/v1/status") in names
    assert ("block", "azom/hero") in names
    assert ("block", "azom/js-block") in names
    assert ("constant", "AZOM_API_KEY") in names
    assert ("header", "Theme Name") in names

    dumped = json.dumps(full)
    assert secret_val not in dumped
    assert const_secret not in dumped
    assert "***" in dumped

    filtered = index_sources(sources, query="azom/v1", symbols=["rest_routes"])
    assert filtered["matched"] == 1
    assert filtered["entries"][0]["name"] == "azom/v1/status"
    assert filtered["counts_by_kind"] == {"rest_route": 1}

    capped = index_sources(sources, max_results=3)
    assert capped["returned"] == 3
    assert capped["truncated"] is True


def test_static_checks_rules_and_secret_scrubbing() -> None:
    secret = "sk_live_" + "StaticSecret554433"
    get_var = "$_GET"
    post_var = "$_POST"
    ev_fn = "ev" + "al"
    ex_fn = "ex" + "ec"
    unser_fn = "un" + "serialize"
    dirty = {
        "bad-xss.php": (
            "<?php\n"
            "defined('ABSPATH') || exit;\n"
            f"echo {get_var}['q'];\n"
            f"echo esc_html({get_var}['safe']);\n"
            f"echo (int) {post_var}['safe_int'];\n"
        ),
        "bad-sql.php": (
            "<?php\n"
            "defined('ABSPATH') || exit;\n"
            "global $wpdb;\n"
            "$wpdb->query(\"SELECT * FROM wp_posts WHERE ID = $id\");\n"
            "$q = \"SELECT * FROM t WHERE a=\" . $x;\n"
            "$wpdb->get_results($q);\n"
            "$wpdb->get_var($wpdb->prepare(\"SELECT * FROM t WHERE id=%d\", $id));\n"
        ),
        "bad-exec.php": (
            "<?php\n"
            "defined('ABSPATH') || exit;\n"
            f"$api_key = '{secret}'; {ev_fn}($payload);\n"
            f"{ex_fn}($cmd);\n"
            f"{unser_fn}($raw);\n"
            f"{unser_fn}($safe, ['allowed_classes' => false]);\n"
        ),
        "bad-ajax.php": (
            "<?php\n"
            "defined('ABSPATH') || exit;\n"
            "add_action('wp_ajax_save_cfg', 'azom_save_cfg');\n"
        ),
        "bad-ajax-cap.php": (
            "<?php\n"
            "defined('ABSPATH') || exit;\n"
            "add_action('wp_ajax_save_cfg2', 'azom_save_cfg2');\n"
            "check_ajax_referer('nonce');\n"
        ),
        "bad-woo.php": (
            "<?php\n"
            "defined('ABSPATH') || exit;\n"
            "woocommerce_get_template('single-product.php');\n"
            "$p->get_related();\n"
        ),
        "standalone.php": "<?php\nfunction solo_fn() { return 1; }\n",
        "woocommerce/cart/cart.php": "<?php\n// template without ABSPATH is allowed\n",
        "theme.json": '{"version": 3,}\n',
    }
    res = run_static_checks(dirty)
    assert res["ok"] is False
    rules = {(item["rule"], item["severity"]) for item in res["findings"]}
    assert ("unescaped_superglobal", "high") in rules
    assert ("unsafe_sql", "high") in rules
    assert ("dangerous_exec", "high") in rules
    assert ("ajax_missing_guard", "high") in rules
    assert ("ajax_missing_guard", "medium") in rules
    assert ("deprecated_woo", "medium") in rules
    assert ("invalid_json", "high") in rules
    assert ("missing_abspath", "low") in rules
    assert not any(item["path"] == "woocommerce/cart/cart.php" for item in res["findings"])
    assert secret not in json.dumps(res)

    clean = run_static_checks(
        {
            "inc/safe.php": (
                "<?php\n"
                "defined('ABSPATH') || exit;\n"
                "add_action('wp_ajax_safe', function() {\n"
                "    check_ajax_referer('safe_nonce');\n"
                "    if (!current_user_can('manage_options')) { return; }\n"
                f"    echo esc_html({post_var}['label']);\n"
                "});\n"
            ),
            "theme.json": '{"version": 3, "settings": {}}\n',
        }
    )
    assert clean["ok"] is True
    assert clean["counts"] == {"high": 0, "medium": 0, "low": 0}


def test_woo_templates_parse_compare_and_audit_overrides() -> None:
    assert parse_template_version("<?php\n/**\n * @version 9.4.0\n */\n") == "9.4.0"
    assert parse_template_version(" * @version: v8.6.0\n") == "8.6.0"
    assert parse_template_version("<?php\n// no header\n") is None

    assert compare_versions("9.4", "9.4.0") == "current"
    assert compare_versions("7.8.0", "9.4.0") == "outdated"
    assert compare_versions("9.5.0", "9.4.0") == "newer"
    assert compare_versions(None, "9.4.0") == "unknown"

    files = {
        "style.css": "/* Theme Name: Shop */\n",
        "woocommerce/cart/cart.php": "<?php\n * @version 7.0.0\n",
        "woocommerce/checkout/thankyou.php": "<?php\n * @version 8.1.0\n",
        "woocommerce/single-product.php": "<?php\n * @version 1.6.4\n",
        "woocommerce/content-product.php": "<?php\n * @version 3.6.0\n",
        "woocommerce/custom/custom.php": "<?php\n// no version tag\n",
    }
    report9 = audit_overrides(files)
    assert report9["woo_version"] == "9.x"
    assert report9["total_overrides"] == 5
    assert report9["outdated"] == 2
    assert report9["current"] == 2
    assert report9["unknown"] == 1
    assert set(report9["critical_overrides"]) == {
        "woocommerce/cart/cart.php",
        "woocommerce/checkout/thankyou.php",
    }

    report8 = audit_overrides(files, woo_version="8.9")
    by_tpl8 = {item["template"]: item for item in report8["items"]}
    assert by_tpl8["content-product.php"]["status"] == "current"

    custom = audit_overrides(files, reference_versions={"custom/custom.php": "1.0.0"})
    by_custom = {item["template"]: item for item in custom["items"]}
    assert by_custom["custom/custom.php"]["status"] == "unknown"


def test_wporg_intel_fail_closed_mock_and_client(monkeypatch: pytest.MonkeyPatch) -> None:
    assert newer_available("9.4.0", "9.4.3") is True
    assert newer_available("9.4.3", "9.4.3") is False
    assert newer_available("1.0.0-beta", "1.0.0") is True
    assert newer_available("invalid", "1.0.0") is None

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("network must not be called")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WPORG_INTEL", raising=False)
    disabled = fetch_component_intel("plugin", "woocommerce")
    assert disabled["source"] == "disabled"
    assert disabled["intel_enabled"] is False
    assert disabled["latest_version"] is None
    with pytest.raises(WpOrgClientError):
        fetch_wporg_json("https://api.wordpress.org/plugins/info/1.2/?action=plugin_information")

    monkeypatch.setenv("AZOM_WPORG_INTEL", "0")
    disabled2 = fetch_component_intel("theme", "storefront")
    assert disabled2["source"] == "disabled"

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WPORG_INTEL", "1")
    mock_hit = fetch_component_intel("plugin", "woocommerce", installed_version="9.0.0")
    assert mock_hit["source"] == "mock"
    assert mock_hit["intel_enabled"] is False
    assert mock_hit["latest_version"] == "9.4.3"
    assert mock_hit["newer_available"] is True

    mock_miss = fetch_component_intel("plugin", "custom-missing-slug")
    assert mock_miss["source"] == "mock"
    assert mock_miss["latest_version"] is None

    with pytest.raises(WpOrgClientError):
        fetch_wporg_json("https://api.wordpress.org/plugins/info/1.2/?action=plugin_information")

    fetched = fetch_component_intel(
        "plugin",
        "woocommerce",
        installed_version="9.4.0",
        fetcher=lambda _url: {
            "version": "9.5.0",
            "requires_php": "7.4",
            "requires": "6.5",
            "tested": "6.7",
            "active_installs": 7_000_000,
            "security_advisories": [{"cve": "CVE-2026-0001", "api_key": "leak-me"}],
        },
    )
    assert fetched["source"] == "wporg"
    assert fetched["intel_enabled"] is True
    assert fetched["newer_available"] is True
    assert fetched["security_advisories"] == [{"cve": "CVE-2026-0001"}]

    not_found = fetch_component_intel("plugin", "nope", fetcher=lambda _u: {"error": "Not found"})
    assert not_found["error_class"] == "WpOrgNotFound"

    def raise_err(_u: str) -> dict[str, Any]:
        raise urllib.error.URLError("offline")

    unavail = fetch_component_intel("plugin", "nope", fetcher=raise_err)
    assert unavail["error_class"] == "WpOrgUnavailable"

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WPORG_INTEL", "1")
    with pytest.raises(ValueError):
        fetch_wporg_json("https://evil.example/api")

    class _FakeResp:
        def __init__(self, payload: bytes) -> None:
            self._buf = io.BytesIO(payload)

        def read(self, n: int = -1) -> bytes:
            return self._buf.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=5.0: _FakeResp(json.dumps({"version": "4.6.1"}).encode("utf-8")),
    )
    ok_json = fetch_wporg_json(
        "https://api.wordpress.org/themes/info/1.2/?action=theme_information&request[slug]=storefront"
    )
    assert ok_json["version"] == "4.6.1"

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=5.0: _FakeResp(b"x" * 2048),
    )
    with pytest.raises(WpOrgClientError):
        fetch_wporg_json(
            "https://api.wordpress.org/themes/info/1.2/?action=theme_information",
            max_bytes=1024,
        )


def test_theme_json_operations_v3_and_rejections() -> None:
    created = apply_theme_json_operations(
        None,
        [
            {
                "op": "set",
                "pointer": "/settings/color/palette",
                "value": [{"slug": "primary", "color": "#112233"}],
            },
            {
                "op": "set",
                "pointer": "/settings/color/palette/-",
                "value": {"slug": "accent", "color": "#445566"},
            },
            {"op": "set", "pointer": "/styles/color/background", "value": "#ffffff"},
            {"op": "set", "pointer": "/settings/custom/a~1b", "value": True},
        ],
    )
    assert created["version"] == 3
    parsed = json.loads(created["updated_json"])
    assert parsed["$schema"] == "https://schemas.wp.org/trunk/theme.json"
    assert len(parsed["settings"]["color"]["palette"]) == 2
    assert parsed["settings"]["custom"]["a/b"] is True

    removed = apply_theme_json_operations(
        created["updated_json"],
        [{"op": "remove", "pointer": "/settings/custom/a~1b"}],
    )
    parsed2 = json.loads(removed["updated_json"])
    assert "a/b" not in parsed2["settings"]["custom"]

    for bad_raw in ("{not json", "[]", '{"version": 1}', '{"version": true}'):
        with pytest.raises(ValueError):
            apply_theme_json_operations(bad_raw, [])

    for bad_ops in (
        [{"op": "set", "pointer": "/settings/__proto__/x", "value": 1}],
        [{"op": "set", "pointer": "/settings/constructor/x", "value": 1}],
        [{"op": "set", "pointer": "/settings/prototype", "value": 1}],
        [{"op": "set", "pointer": "/settings/nested", "value": {"__proto__": {"polluted": True}}}],
        [{"op": "set", "pointer": "/settings/num", "value": float("nan")}],
        [{"op": "remove", "pointer": "/settings/missing"}],
    ):
        with pytest.raises(ValueError):
            apply_theme_json_operations(created["updated_json"], bad_ops)


def test_playbooks_topics_selection_and_formatting() -> None:
    assert len(PLAYBOOK_TOPICS) == 10
    for topic in PLAYBOOK_TOPICS:
        pb = get_playbook(topic)
        assert pb["ok"] is True
        assert pb["topic"] == topic
        assert pb["title"]
        assert pb["summary"]
        assert pb["steps"]
        assert pb["tools"]
        assert pb["anti_patterns"]

    missing = get_playbook("nonexistent_topic")
    assert missing["ok"] is False
    assert missing["available_topics"] == list(PLAYBOOK_TOPICS)

    assert (
        select_playbooks("Uppdatera färger i theme.json med design tokens")[0]["topic"]
        == "theme_json_design"
    )
    assert (
        select_playbooks("Granska woocommerce overrides och @version")[0]["topic"]
        == "woo_template_overrides"
    )
    assert select_playbooks("Hur många öppna ärenden har vi i supporten?") == []
    assert select_playbooks("Kolla order 1001") == []

    hints = format_playbook_hints(
        "Gör en bulk_replace refaktorering i barntema", max_topics=2, max_chars=260
    )
    assert hints
    assert len(hints) <= 260
    assert format_playbook_hints("Kolla order 1001") == ""
    assert format_playbook_hints("theme.json", max_chars=0) == ""


# ---------------------------------------------------------------------------
# 2. All 15 new tools + wp_search_component enhancements + RBAC boundaries
# ---------------------------------------------------------------------------


def test_tool_schema_registration_and_wp_playbook_rbac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert len(EXTRA_FILE_TOOLS) == 20
    assert ALL_FILE_TOOLS == FILE_TOOL_NAMES | EXTRA_FILE_TOOLS
    for name in ALL_FILE_TOOLS:
        if name == "wp_read_theme_plugin_file":
            assert name in WP_TOOL_SCHEMAS
        else:
            assert name in FILE_TOOL_SCHEMAS

    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    secret = "sk_live_" + "RbacSecret12345"
    get_var = "$_GET"
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "style.css",
        "/*\nTheme Name: Twenty Twenty-Five\n*/\n",
    )
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: twentytwentyfive\n*/\n",
    )
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "functions.php",
        f"<?php\ndefined('ABSPATH') || exit;\n$api_key = '{secret}';\nadd_action('init', 'azom_child_init');\n",
    )
    _write_plugin_file(
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
    _setup_mock_env(tmp_path, monkeypatch)

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


def test_workspace_apply_changes_atomic_and_rollback_on_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/* Theme Name: Azom Child\nTemplate: parent\n*/\n",
    )
    _write_theme_file(tmp_path, "mock-staging", "azom-child", "a.php", "<?php\n// alpha\n")
    _write_theme_file(tmp_path, "mock-staging", "azom-child", "b.php", "<?php\n// beta\n")
    _write_theme_file(tmp_path, "mock-staging", "azom-child", "old.php", "<?php\n// delete me\n")

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
                    "base_sha256": _sha256_text("<?php\n// alpha\n"),
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

    direct_dry = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY).workspace_bulk_replace(
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

    _write_theme_file(
        tmp_path,
        "mock-staging",
        "twentytwentyfive",
        "style.css",
        "/* Theme Name: Twenty Twenty-Five */\n",
    )
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: twentytwentyfive\n*/\n",
    )
    _write_theme_file(
        tmp_path, "mock-staging", "azom-child", "front-page.php", "<?php\n// front\n"
    )
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "woocommerce/cart/cart.php",
        "<?php\n/**\n * @version 7.0.0\n */\n",
    )
    _write_plugin_file(
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


# ---------------------------------------------------------------------------
# 3. Audit bug fixes #1 - #10
# ---------------------------------------------------------------------------


def test_bug1_open_review_workspace_mock_local_tree_fallback_and_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    _write_theme_file(
        tmp_path, "mock-staging", "parent-base", "style.css", "/*\nTheme Name: Parent Base\n*/\n"
    )
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "functions.php",
        "<?php\n// populated from LocalTree\n",
    )
    _write_theme_file(
        tmp_path, "mock-staging", "solo-theme", "style.css", "/*\nTheme Name: Solo Theme\n*/\n"
    )
    _write_plugin_file(tmp_path, "mock-staging", "third-party", "third-party.php", "<?php\n")
    _write_plugin_file(tmp_path, "mock-staging", "woocommerce", "woocommerce.php", "<?php\n")

    child = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child"},
        actor="oscar",
    )
    assert child.data["ok"] is True
    assert child.data["status"] == "validated"
    assert child.data["theme_relationship"] == "child"
    ws_base = workspace_root(tmp_path, "mock-staging", child.data["plan_id"]) / "base"
    assert "populated from LocalTree" in (ws_base / "functions.php").read_text(encoding="utf-8")

    solo = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "solo-theme"},
        actor="oscar",
    )
    assert solo.data["ok"] is True
    assert solo.data["status"] == "validated"
    assert solo.data["theme_relationship"] == "no_child"

    parent = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "parent-base"},
        actor="oscar",
    )
    assert parent.data["ok"] is False
    assert parent.data["error_class"] == "ParentThemeBlocked"

    third = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin", "slug": "third-party"},
        actor="oscar",
    )
    assert third.data["ok"] is False
    assert third.data["error_class"] == "ThirdPartyPluginRefused"

    woo = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin", "slug": "woocommerce"},
        actor="oscar",
    )
    assert woo.data["ok"] is False
    assert woo.data["error_class"] == "WooCoreRefused"


def test_bug2_approve_staging_succeeds_on_created_and_parent_copied_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    _write_theme_file(
        tmp_path, "mock-staging", "parent-base", "archive.php", "<?php\n// parent archive\n"
    )
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )

    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]

    assert (
        plans.create_file(
            "mock-staging", plan_id, "includes/badge.php", "<?php\n// new badge\n", actor="oscar"
        ).ok
        is True
    )
    assert (
        plans.copy_from_parent(
            "mock-staging", plan_id, "archive.php", actor="oscar", parent_slug="parent-base"
        ).ok
        is True
    )

    monkeypatch.setattr(review_plan_mod, "run_catalog", _passing_catalog)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.ok is True
    assert checked.data["status"] == "tests_passed"

    row = plans.get(plan_id, site_id="mock-staging")
    assert row is not None
    staged = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=row["base_sha256"],
        work_sha256=row["work_sha256"],
    )
    assert staged.ok is True
    assert staged.data["status"] == "staged"
    assert staged.data["executed"] is True
    theme_dir = _mock_content(tmp_path, "mock-staging") / "themes" / "azom-child"
    assert (theme_dir / "includes" / "badge.php").read_text(encoding="utf-8") == (
        "<?php\n// new badge\n"
    )
    assert (theme_dir / "archive.php").read_text(encoding="utf-8") == "<?php\n// parent archive\n"


def test_bug3_workspace_run_checks_summary_findings_no_artifact_path_and_stage_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent\n*/\n",
    )
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child"},
        actor="oscar",
    )
    plan_id = opened.data["plan_id"]
    assert (
        tools.execute(
            "wp_workspace_create_file",
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "path": "includes/woo-badge.php",
                "content": "<?php\n// shipping helper without HPOS declaration\n",
            },
            actor="oscar",
        ).data["ok"]
        is True
    )

    incomplete = tools.execute(
        "wp_workspace_run_checks",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="oscar",
    )
    assert incomplete.data["ok"] is True
    assert incomplete.review_stage_plan_id is None
    assert incomplete.data["staging_ready"] is False
    assert incomplete.data["summary"]
    assert "wc_tested_up_to" in incomplete.data["findings"]
    assert "artifact_path" not in json.dumps(incomplete.data)

    opened2 = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Color tweak"},
        actor="oscar",
    )
    plan2 = opened2.data["plan_id"]
    assert (
        tools.execute(
            "wp_workspace_create_file",
            {
                "site_id": "mock-staging",
                "plan_id": plan2,
                "path": "includes/badge.php",
                "content": "<?php\n// badge\n",
            },
            actor="oscar",
        ).data["ok"]
        is True
    )

    monkeypatch.setattr(review_plan_mod, "run_catalog", _passing_catalog)
    passed = tools.execute(
        "wp_workspace_run_checks",
        {"site_id": "mock-staging", "plan_id": plan2},
        actor="oscar",
    )
    assert passed.data["ok"] is True
    assert passed.data["status"] == "tests_passed"
    assert passed.data["staging_ready"] is True
    assert passed.review_stage_plan_id == plan2
    assert "artifact_path" not in json.dumps(passed.data)
    assert passed.data["summary"].startswith("tests_passed:")


def test_bug4_list_entries_distinguishes_files_and_dotted_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    site_root = _mock_content(tmp_path, "mock-staging") / "themes" / "azom-child"
    (site_root / "inc").mkdir(parents=True)
    (site_root / "release.v2").mkdir(parents=True)
    for fname in ("Makefile", "style.scss", "README.md", "sv_SE.po", "theme.pot", "style.css"):
        (site_root / fname).write_text("ok\n", encoding="utf-8")

    local_tree = LocalTree("/var/www/mock-staging", tmp_path / "wp_files_mock" / "mock-staging")
    entries = dict(local_tree.list_entries("wp-content/themes/azom-child"))
    assert entries == {
        "Makefile": False,
        "README.md": False,
        "inc": True,
        "release.v2": True,
        "style.css": False,
        "style.scss": False,
        "sv_SE.po": False,
        "theme.pot": False,
    }

    listed = service.list_component_tree("mock-staging", "theme", "azom-child", "", actor="jonatan")
    assert listed.ok is True
    kinds = {row["name"]: row["kind"] for row in listed.data["entries"]}
    assert kinds["Makefile"] == "file"
    assert kinds["style.scss"] == "file"
    assert kinds["README.md"] == "file"
    assert kinds["sv_SE.po"] == "file"
    assert kinds["theme.pot"] == "file"
    assert kinds["inc"] == "directory"
    assert kinds["release.v2"] == "directory"

    site = parse_site(
        {
            "id": "stage",
            "display_name": "Stage",
            "canonical_url": "https://stage.example.test/",
            "environment": "staging",
            "business_owner": "customer",
            "hosting_provider": "unconfigured",
            "wordpress_version": "6.7",
            "php_version": "8.3",
            "woocommerce": False,
            "critical_urls": ["https://stage.example.test/"],
            "maintenance_window": "not agreed",
            "secret_ref": "WP_SECRET_STAGE",
            "alert_owner": "unassigned",
            "alert_confirmations": 2,
            "check_interval_seconds": 60,
            "timeout_seconds": 10,
            "dedupe_window_seconds": 30,
            "capabilities": {"uptime": "available"},
            "files_root": "/var/www/stage",
            "ssh_host": "stage.internal",
            "ssh_user": "azom-wp-reader",
        }
    )
    ssh_transport = SshFileTransport(site, tmp_path)

    class _FakeSftp:
        def listdir_attr(self, _path: str):
            return [
                SimpleNamespace(filename="Makefile", st_mode=stat.S_IFREG | 0o644),
                SimpleNamespace(filename="release.v2", st_mode=stat.S_IFDIR | 0o755),
                SimpleNamespace(filename="style.scss", st_mode=stat.S_IFREG | 0o644),
                SimpleNamespace(filename="sym", st_mode=stat.S_IFLNK | 0o777),
                SimpleNamespace(filename=".env", st_mode=stat.S_IFREG | 0o600),
            ]

    @contextmanager
    def _fake_session():
        yield _FakeSftp()

    monkeypatch.setattr(ssh_transport, "_active_session", _fake_session)
    monkeypatch.setattr(SshFileTransport, "_check_remote_path", staticmethod(lambda *_a, **_k: None))
    ssh_entries = ssh_transport.list_entries("wp-content/themes/azom-child")
    assert ssh_entries == [
        ("Makefile", False),
        ("release.v2", True),
        ("style.scss", False),
    ]


def test_bug5_search_redacts_secrets_in_excerpts_and_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "bound_site_cred_999")

    s1 = "sk_live_" + "SearchSecret8877"
    s2 = "pass1234secret"
    s3 = "Bearer bearerTok889900"
    res = search_texts(
        {
            "cfg.php": (
                f"$marker_uri = 'https://user:{s2}@api.example.test';\n"
                f"// azom_needle line with {s1} and {s3}\n"
                "define('DB_PASSWORD', 'bound_site_cred_999');\n"
            )
        },
        "azom_needle",
        context_lines=1,
        extra_secrets=["bound_site_cred_999"],
    )
    assert len(res["hits"]) == 1
    dumped = json.dumps(res)
    for secret in (s1, s2, s3, "bound_site_cred_999"):
        assert secret not in dumped

    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "inc/creds.php",
        f"<?php\n// azom_search_hit\n$api_key = '{s1}';\n$auth = '{s3}';\n",
    )
    found = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "query": "azom_search_hit",
            "context_lines": 2,
        },
        actor="oscar",
    )
    assert found.data["ok"] is True
    assert len(found.data["hits"]) == 1
    found_json = json.dumps(found.data)
    assert s1 not in found_json
    assert s3 not in found_json
    assert "[REDACTED]" in found_json


def test_bug6_file_tool_loop_caps_budget_complete_drift_reads_and_16000_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert FILE_LOOP_MAX_CALLS == 16
    assert FILE_LOOP_MAX_READS == 10
    assert FILE_LOOP_MAX_SEARCHES == 5
    assert FILE_LOOP_MAX_SECONDS == 60.0

    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)
    _write_theme_file(
        tmp_path, "mock-staging", "azom-child", "style.css", "/*\nTheme Name: Azom Child\n*/\n"
    )
    _write_theme_file(
        tmp_path, "mock-staging", "azom-child", "functions.php", "<?php\n// azom_func\n"
    )

    long_prefix = "A" * 5000
    initial = long_prefix + _tool_call(
        "wp_theme_overview", {"site_id": "mock-staging", "slug": "azom-child"}
    )
    captured_msgs: list[list[dict[str, str]]] = []
    followups = iter(
        [
            _tool_call("wp_site_status", {"site_id": "mock-staging"}),
            "Tema och sajtstatus hämtades.",
        ]
    )

    def complete_drift(msgs: list[dict[str, str]]) -> tuple[str, float]:
        captured_msgs.append([dict(m) for m in msgs])
        return next(followups), 0.001

    drift_res = run_bounded_file_tool_loop(
        initial,
        [{"role": "user", "content": "kolla temat och sajtens hälsa"}],
        complete=complete_drift,
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert drift_res.handled is True
    assert drift_res.calls == 2
    assert drift_res.results[0]["status"] == "overview"
    assert drift_res.results[1]["site_id"] == "mock-staging"
    assistant_msgs = [m["content"] for m in captured_msgs[0] if m["role"] == "assistant"]
    assert len(assistant_msgs[0]) > 4500

    huge = "B" * 17000 + _tool_call("wp_playbook", {"topic": "child_theme_edit"})
    huge_seen: list[list[dict[str, str]]] = []

    def complete_huge(msgs: list[dict[str, str]]) -> tuple[str, float]:
        huge_seen.append([dict(m) for m in msgs])
        return "Klart.", 0.0

    run_bounded_file_tool_loop(
        huge,
        [],
        complete=complete_huge,
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert len([m["content"] for m in huge_seen[0] if m["role"] == "assistant"][0]) == 16000

    budget_calls = {"n": 0}

    def complete_calls(msgs: list[dict[str, str]]) -> tuple[str, float]:
        budget_calls["n"] += 1
        if budget_calls["n"] <= FILE_LOOP_MAX_CALLS:
            return _tool_call("wp_playbook", {"topic": "child_theme_edit"}), 0.0
        assert any(
            "Verktygsbudgeten är nådd" in m["content"] for m in msgs if m["role"] == "system"
        )
        return "Sammanfattning av alla 16 anrop.", 0.0

    cap_res = run_bounded_file_tool_loop(
        _tool_call("wp_playbook", {"topic": "child_theme_edit"}),
        [],
        complete=complete_calls,
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert cap_res.handled is True
    assert cap_res.calls == FILE_LOOP_MAX_CALLS
    assert len(cap_res.results) == FILE_LOOP_MAX_CALLS
    assert budget_calls["n"] == FILE_LOOP_MAX_CALLS + 1
    assert "Sammanfattning av alla 16 anrop." in cap_res.content

    read_calls = {"n": 0}
    read_call = _tool_call(
        "wp_read_theme_plugin_file",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "path": "functions.php",
        },
    )

    def complete_reads(_msgs: list[dict[str, str]]) -> tuple[str, float]:
        read_calls["n"] += 1
        if read_calls["n"] <= FILE_LOOP_MAX_READS:
            return read_call, 0.0
        return "Sammanfattning efter läsgräns.", 0.0

    read_res = run_bounded_file_tool_loop(
        read_call,
        [],
        complete=complete_reads,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert read_res.calls == FILE_LOOP_MAX_READS
    assert read_calls["n"] == FILE_LOOP_MAX_READS + 1
    assert "läsgräns" in read_res.content

    search_calls = {"n": 0}
    search_call = _tool_call(
        "wp_search_component",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "query": "azom_func"},
    )

    def complete_searches(_msgs: list[dict[str, str]]) -> tuple[str, float]:
        search_calls["n"] += 1
        if search_calls["n"] <= FILE_LOOP_MAX_SEARCHES:
            return search_call, 0.0
        return "Sammanfattning efter sökgräns.", 0.0

    search_res = run_bounded_file_tool_loop(
        search_call,
        [],
        complete=complete_searches,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert search_res.calls == FILE_LOOP_MAX_SEARCHES
    assert search_calls["n"] == FILE_LOOP_MAX_SEARCHES + 1
    assert "sökgräns" in search_res.content


def test_bug7_and_bug8_chat_agent_and_site_content_wp_code_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    monkeypatch.delenv("AZOM_TELEGRAM_MODEL_ROUTE", raising=False)
    reset_model_router()

    assert site_content_turn("ta bort hook i temat", allow_wp_ops_fallthrough=True).consumed is False
    assert (
        site_content_turn("jämför header.php och footer.php", allow_wp_ops_fallthrough=True).consumed
        is False
    )
    refused_switch = site_content_turn("byt tema till storefront", allow_wp_ops_fallthrough=True)
    assert refused_switch.consumed is True
    assert "Temabyte" in refused_switch.results[0][1]

    for prompt in (
        "ta bort hook i temat",
        "jämför header.php och footer.php",
        "ändra hook på produktsidan i single-product.php",
    ):
        assert WP_CODE_INTENT_RE.search(prompt)
        pref = gather_tool_results(prompt, has_wp_site_context=True)
        assert pref.results == []
        assert pref.pending_action is None
        assert _use_read_tool_loop(prompt, pref, has_wp_site_context=True) is False

    for name in ALL_FILE_TOOLS:
        assert _use_file_tool_loop(_tool_call(name, {})) is True

    assert chat_agent.WP_CODE_INTENT_RE is WP_CODE_INTENT_RE
    assert hasattr(site_content, "_WP_CODE_FALLTHROUGH_RE")

    ctx = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        site_id="mock-staging",
        operation_ids=(),
    )
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            _tool_call("wp_playbook", {"topic": "child_theme_edit"}),
            "Vi ändrar detta säkert via barntemat i en wr-arbetsyta.",
        ]
    )

    def fake_chat(messages, **kwargs):
        calls.append({"messages": list(messages), **kwargs})
        return next(responses), 0.001

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)

    res = run_chat(
        "ta bort hook i temat",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        session={"wp_ops_context": ctx},
    )
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 4096
    assert calls[1]["max_tokens"] == 4096
    assert "barntemat" in res.text

    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.chat_completion",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("LLM must not run on theme switch")
        ),
    )
    refused_chat = run_chat(
        "byt tema till storefront",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        session={"wp_ops_context": ctx},
    )
    assert "Temabyte" in refused_chat.text


def test_bug9_rejected_review_open_leaves_no_dangling_workspace_or_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)

    for res in (
        plans.open("mock-staging", "plugin", "woocommerce", actor="oscar"),
        plans.open("mock-staging", "plugin", "hello-dolly", actor="oscar"),
        plans.open(
            "mock-staging",
            "theme",
            "twentytwentyfive",
            actor="oscar",
            verified_theme=VerifiedTheme(stylesheet="azom-child", template="twentytwentyfive"),
        ),
    ):
        assert res.ok is False

    assert not (tmp_path / "wp_workspaces").exists()
    assert not (tmp_path / "wp_review_plans.json").exists()


def test_bug10_drift_followup_completion_passes_wp_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    monkeypatch.delenv("AZOM_TELEGRAM_MODEL_ROUTE", raising=False)
    reset_model_router()

    ctx = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        site_id="mock-staging",
        operation_ids=(),
    )
    max_tokens_seen: list[int | None] = []
    responses = iter(
        [
            _tool_call("wp_site_status", {"site_id": "mock-staging"}),
            "mock-staging har status ok.",
        ]
    )

    def fake_chat(_messages, **kwargs):
        max_tokens_seen.append(kwargs.get("max_tokens"))
        return next(responses), 0.001

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)

    res = run_chat(
        "vad är sajtens status?",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        session={"wp_ops_context": ctx},
    )
    assert "mock-staging" in res.text
    assert max_tokens_seen == [2048, 2048]


# ---------------------------------------------------------------------------
# 4. Prompt & Telegram UX checks
# ---------------------------------------------------------------------------


def test_prompt_and_wp_tool_guide_budgets() -> None:
    assert len(WP_TOOL_GUIDE) <= 7000
    full_prompt, _version = _build_system_prompt()
    assert len(full_prompt) < 12986


def test_telegram_wpreview_callbacks_job_store_preflight_handlers_and_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42,77,88")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar,77:jonatan,88:agent")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.test")

    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )
    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        summary="Button tweak",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    assert (
        plans.create_file(
            "mock-staging", plan_id, "includes/tweak.php", "<?php\n// tweak\n", actor="oscar"
        ).ok
        is True
    )

    monkeypatch.setattr(review_plan_mod, "run_catalog", _passing_catalog)
    assert plans.run_checks("mock-staging", plan_id, actor="oscar").data["status"] == "tests_passed"

    stage_cb = f"wpreview:stage:{plan_id}"
    no_cb = f"wpreview:no:{plan_id}"
    assert job_store_mod._ACTION_CALLBACK.match(stage_cb) is not None
    assert job_store_mod._ACTION_CALLBACK.match(no_cb) is None
    assert bot_main._ACTION_CALLBACK.fullmatch(stage_cb) is not None
    assert bot_main._ACTION_CALLBACK.fullmatch(no_cb) is None

    conv_store = ConversationStore(path=tmp_path / "conv.json")
    handler = BotHandler(store=conv_store)
    monkeypatch.setattr(
        "ecom_ops.bot.handlers.run_chat",
        lambda *_a, **_k: ChatResult(text="Kontroller klara.", review_stage_plan_id=plan_id),
    )
    chat_reply = handler.handle(42, "kör kontroller")
    assert chat_reply.reply_markup is not None
    buttons = [btn for row in chat_reply.reply_markup["inline_keyboard"] for btn in row]
    assert any(btn.get("callback_data") == stage_cb for btn in buttons)
    assert any(btn.get("callback_data") == no_cb for btn in buttons)
    assert any(
        btn.get("url") == f"https://ops.example.test/wp/mock-staging/reviews/{plan_id}"
        for btn in buttons
    )
    cached_state = conv_store.get(42) or {}
    assert plan_id in (cached_state.get("session") or {}).get("wp_review_hashes", {})

    job_store = TelegramJobStore(path=tmp_path / "jobs.sqlite3")
    with job_store._connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_conversations(chat_id, state_json, updated_at) "
            "VALUES(?, ?, ?)",
            ("42", json.dumps(cached_state), time.time()),
        )

    accepted = job_store.accept_update(
        {
            "update_id": 1001,
            "callback_query": {
                "id": "cb1",
                "data": stage_cb,
                "message": {"message_id": 10, "chat": {"id": 42}},
            },
        }
    )
    assert accepted["disposition"] == "queued"
    job = job_store.get_job(accepted["job_id"])
    assert job is not None
    assert job["kind"] == "action"
    assert job["payload"]["approval_snapshot_required"] is True
    assert job["payload"]["approval_snapshot"] == TelegramJobStore.approval_snapshot(cached_state)
    assert bot_main._action_lock_key(job, handler) == "wp-file-edits"
    assert bot_main._preflight_action_job(handler, job) is None

    stale_job = {
        **job,
        "payload": {**job["payload"], "approval_snapshot": "0" * 64},
    }
    assert bot_main._preflight_action_job(handler, stale_job) is not None

    dup = job_store.accept_update(
        {
            "update_id": 1002,
            "callback_query": {
                "id": "cb2",
                "data": stage_cb,
                "message": {"message_id": 10, "chat": {"id": 42}},
            },
        }
    )
    assert dup["disposition"] == "duplicate_action"

    no_accepted = job_store.accept_update(
        {
            "update_id": 1003,
            "callback_query": {
                "id": "cb3",
                "data": no_cb,
                "message": {"message_id": 10, "chat": {"id": 42}},
            },
        }
    )
    no_job = job_store.get_job(no_accepted["job_id"])
    assert no_job is not None and no_job["kind"] == "callback"

    plans_oscar = handler.handle(42, "/wp plans mock-staging")
    assert plan_id in plans_oscar.text
    assert "Button tweak" in plans_oscar.text

    plans_jonatan = handler.handle(77, "/wp plans")
    assert plan_id in plans_jonatan.text
    assert "Button tweak" not in plans_jonatan.text

    plan_jonatan = handler.handle(77, f"/wp plan {plan_id}")
    assert plan_id in plan_jonatan.text
    assert plan_jonatan.reply_markup is None
    assert "kräver Oscar" in plan_jonatan.text

    plan_oscar = handler.handle(42, f"/wp plan {plan_id}")
    assert plan_oscar.reply_markup is not None
    assert any(
        btn.get("callback_data") == stage_cb
        for row in plan_oscar.reply_markup["inline_keyboard"]
        for btn in row
    )

    for denied_chat in (77, 88):
        denied_cb = handler.handle_callback(denied_chat, stage_cb)
        assert "kräver Oscar" in denied_cb.text
    assert not (
        _mock_content(tmp_path, "mock-staging")
        / "themes"
        / "azom-child"
        / "includes"
        / "tweak.php"
    ).exists()

    cancelled = handler.handle_callback(42, no_cb)
    assert "Avbrutet" in cancelled.text
    assert plan_id not in ((conv_store.get(42) or {}).get("session") or {}).get(
        "wp_review_hashes", {}
    )

    assert handler.handle(42, f"/wp plan {plan_id}").reply_markup is not None
    staged = handler.handle_callback(42, stage_cb)
    assert "skrivna i staging" in staged.text
    assert (
        _mock_content(tmp_path, "mock-staging")
        / "themes"
        / "azom-child"
        / "includes"
        / "tweak.php"
    ).read_text(encoding="utf-8") == "<?php\n// tweak\n"


# ---------------------------------------------------------------------------
# 5. Merge-compatibility regression tests (aa95eca x Etapp 1-4T)
# ---------------------------------------------------------------------------


def test_merge_compat_run_llm_chat_persists_wp_review_hashes_atomically_without_clobbering_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.test")

    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )
    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        summary="Header spacing fix",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    assert (
        plans.create_file(
            "mock-staging", plan_id, "includes/spacing.php", "<?php\n// spacing\n", actor="oscar"
        ).ok
        is True
    )
    monkeypatch.setattr(review_plan_mod, "run_catalog", _passing_catalog)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.data["status"] == "tests_passed"
    plan_row = plans.get(plan_id)
    assert plan_row is not None

    db_path = tmp_path / "jobs.sqlite3"
    conv_store = TelegramConversationStore(db_path)
    conv_store.set(
        42,
        {
            "flow": "confirm_action",
            "step": "await_confirm",
            "slots": {"pending": {"kind": "order_status", "order_id": "1001"}},
            "session": {"existing_key": "keep"},
            "messages": [{"role": "user", "content": "tidigare fråga"}],
        },
    )

    recorded_calls: list[dict[str, Any]] = []
    real_append = conv_store.append_turn_atomic

    def spy_append(chat_id: str | int, **kwargs: Any) -> dict[str, Any]:
        recorded_calls.append({"chat_id": chat_id, **kwargs})
        return real_append(chat_id, **kwargs)

    monkeypatch.setattr(conv_store, "append_turn_atomic", spy_append)
    monkeypatch.setattr(
        "ecom_ops.bot.handlers.run_chat",
        lambda *_a, **_k: ChatResult(
            text="Kontroller klara för staging.",
            messages=[
                {"role": "user", "content": "tidigare fråga"},
                {"role": "user", "content": "kör kontroller"},
                {"role": "assistant", "content": "Kontroller klara för staging."},
            ],
            review_stage_plan_id=plan_id,
        ),
    )

    handler = BotHandler(store=conv_store)
    reply = handler._run_llm_chat(42, "kör kontroller")
    assert reply.reply_markup is not None
    buttons = [btn for row in reply.reply_markup["inline_keyboard"] for btn in row]
    assert any(btn.get("callback_data") == f"wpreview:stage:{plan_id}" for btn in buttons)

    assert len(recorded_calls) == 1
    session_updates = recorded_calls[0]["session_updates"]
    assert isinstance(session_updates, dict)
    assert plan_id in session_updates["wp_review_hashes"]

    reopened = TelegramConversationStore(db_path)
    persisted = reopened.get(42)
    assert persisted is not None
    assert persisted["flow"] == "confirm_action"
    assert persisted["step"] == "await_confirm"
    assert persisted["slots"] == {"pending": {"kind": "order_status", "order_id": "1001"}}
    assert persisted["session"]["existing_key"] == "keep"
    assert persisted["session"]["wp_review_hashes"][plan_id] == {
        "actor": "oscar",
        "site_id": "mock-staging",
        "base_sha256": plan_row["base_sha256"],
        "work_sha256": plan_row["work_sha256"],
    }
    assert [m["content"] for m in persisted["messages"]] == [
        "tidigare fråga",
        "kör kontroller",
        "Kontroller klara för staging.",
    ]


def test_merge_compat_wpreview_stage_and_proposal_yes_coexist_across_job_store_and_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")

    db_path = tmp_path / "jobs.sqlite3"
    conv_store = TelegramConversationStore(db_path)
    job_store = TelegramJobStore(db_path)
    handler = BotHandler(store=conv_store)

    conv_store.set(
        42,
        {
            "session": {
                "wp_review_hashes": {
                    "wr-abcdef012345": {"actor": "oscar", "site_id": "mock-staging"}
                }
            }
        },
    )
    cached_state = conv_store.get(42)
    assert cached_state is not None

    proposal = job_store.create_proposal(
        42,
        kind="wp_content",
        payload={"site_id": "mock-staging", "action": "update_page"},
        site_id="mock-staging",
    )
    proposal_id = proposal["proposal_id"]

    wpreview_cb = "wpreview:stage:wr-abcdef012345"
    proposal_cb = f"proposal:yes:{proposal_id}"

    assert job_store_mod._ACTION_CALLBACK.match(wpreview_cb) is not None
    assert job_store_mod._ACTION_CALLBACK.match(proposal_cb) is not None
    assert bot_main._ACTION_CALLBACK.fullmatch(wpreview_cb) is not None
    assert bot_main._ACTION_CALLBACK.fullmatch(proposal_cb) is not None
    assert job_store_mod._ACTION_CALLBACK.match("wpreview:no:wr-abcdef012345") is None
    assert bot_main._ACTION_CALLBACK.fullmatch(f"proposal:no:{proposal_id}") is None

    wp_accepted = job_store.accept_update(
        {
            "update_id": 2001,
            "callback_query": {
                "id": "cb-wp",
                "data": wpreview_cb,
                "message": {"message_id": 11, "chat": {"id": 42}},
            },
        }
    )
    assert wp_accepted["disposition"] == "queued"
    wp_job = job_store.get_job(wp_accepted["job_id"])
    assert wp_job is not None
    assert wp_job["kind"] == "action"
    assert wp_job["payload"]["approval_snapshot_required"] is True
    assert wp_job["payload"]["approval_snapshot"] == TelegramJobStore.approval_snapshot(cached_state)
    assert "proposal_id" not in wp_job["payload"]
    assert bot_main._action_lock_key(wp_job, handler) == "wp-file-edits"
    assert bot_main._preflight_action_job(handler, wp_job, store=job_store) is None

    prop_accepted = job_store.accept_update(
        {
            "update_id": 2002,
            "callback_query": {
                "id": "cb-prop",
                "data": proposal_cb,
                "message": {"message_id": 12, "chat": {"id": 42}},
            },
        }
    )
    assert prop_accepted["disposition"] == "queued"
    prop_job = job_store.get_job(prop_accepted["job_id"])
    assert prop_job is not None
    assert prop_job["kind"] == "action"
    assert prop_job["payload"]["proposal_id"] == proposal_id
    assert "approval_snapshot_required" not in prop_job["payload"]
    assert bot_main._action_lock_key(prop_job, handler) == "approved-actions"
    assert bot_main._preflight_action_job(handler, prop_job, store=job_store) is None

    missing_snapshot_job = {
        **wp_job,
        "payload": {k: v for k, v in wp_job["payload"].items() if not k.startswith("approval_snapshot")},
    }
    assert bot_main._preflight_action_job(handler, missing_snapshot_job, store=job_store) is not None

    assert job_store.consume_proposal(proposal_id, chat_id=42) is not None
    assert bot_main._preflight_action_job(handler, prop_job, store=job_store) is not None


def test_merge_compat_attachments_roundtrip_send_photo_delivery_and_token_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    png_path = tmp_path / "wp_workspaces" / "wr-abcdef012345" / "artifacts" / "work-desktop.png"
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02\x03" * 4
    png_path.write_bytes(png_bytes)

    original = BotReply(
        text="Preview klar.",
        needs_typing=True,
        attachments=(str(png_path),),
    )
    serialized = bot_main._result_for_reply(original)
    assert serialized["attachments"] == [str(png_path)]
    restored = bot_main._reply_from_result(serialized)
    assert restored.text == "Preview klar."
    assert restored.attachments == (str(png_path),)

    secret_token = "123456:ABCDEF_secret_token_99"
    requests_seen: list[tuple[str, str, bytes]] = []

    class _FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._body = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    next_msg_id = iter(range(100, 120))

    def fake_urlopen(req: urllib.request.Request, timeout: float = 15.0) -> _FakeResponse:
        del timeout
        body = req.data if isinstance(req.data, bytes) else b""
        content_type = str(req.get_header("Content-type") or "")
        requests_seen.append((req.full_url, content_type, body))
        return _FakeResponse({"ok": True, "result": {"message_id": next(next_msg_id)}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    bot_main._send_reply(secret_token, 42, restored, reply_to_message_id=77)
    assert len(requests_seen) == 2
    assert requests_seen[0][0].endswith("/sendMessage")
    photo_url, photo_ctype, photo_body = requests_seen[1]
    assert photo_url.endswith("/sendPhoto")
    assert photo_ctype.startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"\r\n\r\n42\r\n' in photo_body
    assert b'name="reply_to_message_id"\r\n\r\n77\r\n' in photo_body
    assert b'name="allow_sending_without_reply"\r\n\r\ntrue\r\n' in photo_body
    assert b'filename="work-desktop.png"' in photo_body
    assert png_bytes in photo_body

    requests_seen.clear()
    job_store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    accepted = job_store.accept_update(
        {
            "update_id": 3001,
            "message": {"message_id": 88, "chat": {"id": 42}, "text": "visa preview"},
        }
    )
    job = job_store.claim_next()
    assert job is not None and job["job_id"] == accepted["job_id"]
    job_store.complete_job(job["job_id"], serialized)
    outbox_job = job_store.get_job(job["job_id"])
    assert outbox_job is not None

    turn = bot_main._LiveTelegramTurn(
        secret_token,
        42,
        reply_to_message_id=88,
        rate_limiter=PerChatRateLimiter(),
    )
    assert turn.finish(bot_main._reply_from_result(outbox_job["result"]), ok=True) is True
    assert len(requests_seen) == 2
    assert requests_seen[0][0].endswith("/sendMessage")
    assert requests_seen[1][0].endswith("/sendPhoto")
    assert b'name="reply_to_message_id"\r\n\r\n88\r\n' in requests_seen[1][2]
    assert png_bytes in requests_seen[1][2]

    requests_seen.clear()
    monkeypatch.setattr(bot_main, "_DEFAULT_RATE_LIMITER", PerChatRateLimiter())
    assert bot_main._deliver_job_result(secret_token, job_store, outbox_job) is True
    assert len(requests_seen) == 2
    assert requests_seen[1][0].endswith("/sendPhoto")
    assert b'name="reply_to_message_id"\r\n\r\n88\r\n' in requests_seen[1][2]
    delivered_job = job_store.get_job(job["job_id"])
    assert delivered_job is not None and delivered_job["delivery_state"] == "delivered"

    def failing_urlopen(req: urllib.request.Request, timeout: float = 15.0) -> Any:
        del timeout
        err_body = json.dumps(
            {
                "ok": False,
                "error_code": 400,
                "description": f"Bad Request via bot{secret_token}/sendPhoto",
            }
        ).encode("utf-8")
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(err_body),
        )

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(TelegramApiError) as exc_info:
        telegram_transport.send_photo(
            secret_token,
            42,
            png_path,
            caption="test",
            reply_to_message_id=88,
        )
    assert secret_token not in str(exc_info.value)
    assert secret_token not in exc_info.value.description
    assert "bot[REDACTED]" in exc_info.value.description
    assert exc_info.value.__cause__ is None


def test_merge_compat_file_tool_loop_progress_events_cancel_after_three_steers_and_budget_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    _write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )
    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        summary="Static check test",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    assert (
        plans.create_file(
            "mock-staging", plan_id, "includes/helper.php", "<?php\n// safe\n", actor="oscar"
        ).ok
        is True
    )

    assert {"wp_playbook", "wp_workspace_static_checks"}.issubset(EXTRA_FILE_TOOLS)
    assert _safe_tool_detail("wp_playbook", {"topic": "child_theme_edit"}, None) == "child_theme_edit"
    assert (
        _safe_tool_detail(
            "wp_workspace_static_checks",
            {"site_id": "mock-staging", "plan_id": plan_id},
            None,
        )
        == f"mock-staging · {plan_id}"
    )

    svc = WpOpsService(registry_path=REGISTRY, data_dir=tmp_path)
    tools = WpAgentTools(svc)
    events: list[ProgressEvent] = []

    @mark_event_consumer
    def collect(ev: ProgressEvent) -> None:
        events.append(ev)

    followup_answers = iter(
        [
            (
                _tool_call(
                    "wp_workspace_static_checks",
                    {"site_id": "mock-staging", "plan_id": plan_id},
                ),
                0.001,
            ),
            ("Guide och statisk kontroll är klara.", 0.001),
        ]
    )
    res = run_bounded_file_tool_loop(
        _tool_call("wp_playbook", {"topic": "child_theme_edit"}),
        [{"role": "user", "content": "kör guide och statisk kontroll"}],
        complete=lambda _msgs: next(followup_answers),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
        on_progress=collect,
    )
    assert res.handled is True
    assert res.calls == 2

    playbook_done = [
        ev for ev in events if ev.tool == "wp_playbook" and ev.phase == "completed"
    ]
    assert len(playbook_done) == 1
    assert playbook_done[0].label == _WP_TOOL_LABELS["wp_playbook"]
    assert playbook_done[0].detail == "child_theme_edit"
    assert playbook_done[0].outcome == "guide: child_theme_edit"

    static_done = [
        ev for ev in events if ev.tool == "wp_workspace_static_checks" and ev.phase == "completed"
    ]
    assert len(static_done) == 1
    assert static_done[0].label == _WP_TOOL_LABELS["wp_workspace_static_checks"]
    assert static_done[0].detail == f"mock-staging · {plan_id}"
    assert static_done[0].outcome is not None and "statiska fynd" in static_done[0].outcome

    # Verify before_tool() is still invoked on the 4th+ iteration so ChatCancelled interrupts after 3 steers
    steer_events: list[ProgressEvent] = []

    @mark_event_consumer
    def collect_steer(ev: ProgressEvent) -> None:
        steer_events.append(ev)

    before_calls = 0

    def steer_then_cancel() -> list[str]:
        nonlocal before_calls
        before_calls += 1
        if before_calls <= 3:
            return [f"följdfråga {before_calls}"]
        raise ChatCancelled()

    with pytest.raises(ChatCancelled):
        run_bounded_file_tool_loop(
            _tool_call("wp_playbook", {"topic": "child_theme_edit"}),
            [{"role": "user", "content": "testa avbrott"}],
            complete=lambda _msgs: (
                _tool_call("wp_playbook", {"topic": "child_theme_edit"}),
                0.001,
            ),
            actor="oscar",
            channel="telegram",
            conversation_id="telegram:42",
            tools=tools,
            on_progress=collect_steer,
            before_tool=steer_then_cancel,
        )
    assert before_calls == 4
    assert [ev.phase for ev in steer_events] == ["steer", "steer", "steer"]

    # Verify _finish_on_budget emits a failed ProgressEvent with ToolTimeBudgetExceeded on timeout
    timeout_events: list[ProgressEvent] = []

    @mark_event_consumer
    def collect_timeout(ev: ProgressEvent) -> None:
        timeout_events.append(ev)

    ticks = iter([100.0, 100.0 + FILE_LOOP_MAX_SECONDS + 5.0])
    monkeypatch.setattr(agent_tools_mod.time, "monotonic", lambda: next(ticks))

    budget_res = run_bounded_file_tool_loop(
        _tool_call("wp_playbook", {"topic": "child_theme_edit"}),
        [{"role": "user", "content": "testa tidsbudget"}],
        complete=lambda _msgs: ("Sammanfattning efter tidsgräns.", 0.001),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
        on_progress=collect_timeout,
    )
    assert budget_res.handled is True
    assert budget_res.calls == 1
    assert any(
        ev.step_id == "wp-file-budget-timeout"
        and ev.tool == "wp_file_loop"
        and ev.phase == "failed"
        and ev.error_class == "ToolTimeBudgetExceeded"
        for ev in timeout_events
    )


def test_merge_compat_page_review_loop_on_checkpoint_target_page_and_io_timeout_failed_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    events: list[ProgressEvent] = []

    @mark_event_consumer
    def collect(ev: ProgressEvent) -> None:
        events.append(ev)

    checkpoints_called = 0

    def checkpoint() -> list[str]:
        nonlocal checkpoints_called
        checkpoints_called += 1
        return ["fokusera på checkout"] if checkpoints_called == 1 else []

    class _TimeoutWpClient:
        timeout = 10.0

        def list_themes(self) -> list[dict[str, Any]]:
            raise TimeoutError("simulated REST timeout")

    outcome = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=_TimeoutWpClient(),
        registry_path=REGISTRY,
        data_dir=tmp_path,
        on_progress=collect,
        on_checkpoint=checkpoint,
        target_page="/kassa",
    )

    assert checkpoints_called >= 1
    assert any(
        ev.tool == "page_review" and ev.phase == "steer" and "Orderverktyg kördes inte" in (ev.outcome or "")
        for ev in events
    )

    home_step = next(row for row in outcome.steps if row["tool"] == "wp_page_review_home")
    assert home_step["status"] == "not_run"
    assert home_step["reason"] == "io_timeout"

    home_done = [
        ev
        for ev in events
        if ev.tool == "wp_page_review_home" and ev.phase != "started"
    ]
    assert len(home_done) == 1
    assert home_done[0].phase == "failed"
    assert home_done[0].error_class == "io_timeout"
    assert home_done[0].outcome == "tidsgräns nådd (timeout)"
