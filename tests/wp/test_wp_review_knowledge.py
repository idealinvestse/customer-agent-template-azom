"""Tests for WP review knowledge, code index, static checks, Woo templates, WP.org intel, theme.json, and playbooks."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from ecom_ops.integrations.wp_org_client import (
    WpOrgClientError,
    fetch_wporg_json,
)
from ecom_ops.wp_ops.code_index import index_sources
from ecom_ops.wp_ops.playbooks import (
    PLAYBOOK_TOPICS,
    format_playbook_hints,
    get_playbook,
    select_playbooks,
)
from ecom_ops.wp_ops.review_knowledge import (
    TEMPLATE_CONTEXTS,
    template_candidates,
)
from ecom_ops.wp_ops.static_checks import run_static_checks
from ecom_ops.wp_ops.theme_json import apply_theme_json_operations
from ecom_ops.wp_ops.woo_templates import (
    audit_overrides,
    compare_versions,
    parse_template_version,
)
from ecom_ops.wp_ops.wporg_intel import (
    fetch_component_intel,
    newer_available,
)


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
    assert len(PLAYBOOK_TOPICS) == 12
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
    assert (
        select_playbooks("Designa en ny tacksida med syntetisk exempeldata")[0]["topic"]
        == "new_page_and_sample_data"
    )
    assert (
        select_playbooks("Finjustera köpknappen med wp_inspect_element och A/B-variant")[0]["topic"]
        == "live_overlay_ui_iteration"
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
