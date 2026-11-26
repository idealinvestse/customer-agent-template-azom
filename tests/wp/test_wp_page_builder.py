"""Tests for net-new WordPress/WooCommerce page building, zero-PII sample data, and standalone live-shell preview."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.integrations.wp_live_preview import clear_live_html_cache
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import (
    _FILE_READ_MAX,
    WpAgentTools,
    run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.preview_overlay import (
    compose_standalone_page,
    extract_site_shell,
    sanitize_page_draft_html,
)
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import VerifiedTheme
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[2]
MOCK_REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"
LIVE_REGISTRY = ROOT / "config" / "wp_sites.yaml"

_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _setup_mock_wp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[WpOpsService, WpAgentTools]:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()
    clear_live_html_cache()
    theme_dir = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "azom-child"
    theme_dir.mkdir(parents=True, exist_ok=True)
    (theme_dir / "style.css").write_text(
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\nbody { font-family: Oswald, sans-serif; }\n",
        encoding="utf-8",
    )
    (theme_dir / "functions.php").write_text("<?php\n// child\n", encoding="utf-8")
    service = WpOpsService(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    tools = WpAgentTools(service)
    return service, tools


def _tool_call(name: str, arguments: dict[str, Any]) -> str:
    return f"<tool_call>{json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False)}</tool_call>"


def test_catalog_products_include_image_currency_permalink_and_upsells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    clear_rbac_cache()
    tools = WpAgentTools(WpOpsService(data_dir=tmp_path, registry_path=LIVE_REGISTRY))

    monkeypatch.setattr(tools, "_try_woo_client_read", lambda _site: None)
    monkeypatch.setattr(
        tools,
        "_safe_site_get_json",
        lambda _site, path, **_kw: [
            {
                "id": 42,
                "name": "Azom CarPlay Box Pro",
                "slug": "azom-carplay-pro",
                "permalink": "https://azom.no/produkt/azom-carplay-pro/",
                "prices": {"price": "249000", "currency_minor_unit": 2, "currency_code": "NOK"},
                "images": [{"src": "https://azom.no/wp-content/uploads/carplay.jpg"}],
                "sku": "AZ-CP-42",
                "upsell_ids": [101, 102],
                "cross_sell_ids": [205],
            },
            {
                "id": 99,
                "name": "Foreign Image Product",
                "slug": "foreign-img",
                "permalink": "https://azom.no/produkt/foreign-img/",
                "price": "590",
                "images": [{"src": "https://evil.example.org/tracker.png"}],
            },
        ]
        if "/wc/store/v1/products" in path
        else None,
    )

    listed = tools.execute("wp_list_products", {"site_id": "azom-no", "limit": 5}, actor="oscar")
    assert listed.data["ok"] is True
    p0, p1 = listed.data["products"]
    assert p0["image_url"] == "https://azom.no/wp-content/uploads/carplay.jpg"
    assert p0["currency"] == "NOK"
    assert p0["permalink"] == "https://azom.no/produkt/azom-carplay-pro/"
    assert p0["upsell_ids"] == [101, 102]
    assert p0["cross_sell_ids"] == [205]
    # Foreign non-allowlisted image host is stripped
    assert p1["image_url"] == ""


def test_wp_list_pages_and_get_page_discover_guides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    clear_rbac_cache()
    tools = WpAgentTools(WpOpsService(data_dir=tmp_path, registry_path=LIVE_REGISTRY))

    pages_fixture = [
        {
            "id": 77,
            "slug": "setup-guide",
            "title": {"rendered": "Monterings- og setup-guide"},
            "link": "https://azom.no/setup-guide/",
            "status": "publish",
            "excerpt": {"rendered": "<p>Steg-for-steg montering av bilstereo.</p>"},
            "content": {"rendered": "<h2>Steg 1</h2><p>Koble til kabelsettet. sk-or-v1-secret1234567890</p>"},
        }
    ]
    monkeypatch.setattr(
        tools,
        "_safe_site_get_json",
        lambda _site, ep, **_kw: pages_fixture if "/wp/v2/pages" in ep else [],
    )

    listed = tools.execute(
        "wp_list_pages",
        {"site_id": "azom-no", "search": "guide", "post_type": "page"},
        actor="oscar",
    )
    assert listed.data["ok"] is True
    assert listed.data["count"] == 1
    assert listed.data["pages"][0]["path"] == "/setup-guide/"

    fetched = tools.execute(
        "wp_get_page",
        {"site_id": "azom-no", "slug": "setup-guide"},
        actor="oscar",
    )
    assert fetched.data["ok"] is True
    assert fetched.data["page"]["slug"] == "setup-guide"
    assert "sk-or-v1-secret1234567890" not in json.dumps(fetched.data)
    assert "[REDACTED]" in json.dumps(fetched.data)


def test_wp_build_sample_data_pulls_real_products_guides_and_zero_pii_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)

    res = tools.execute(
        "wp_build_sample_data",
        {"site_id": "mock-staging", "scenario": "thankyou", "product_count": 2},
        actor="oscar",
    )
    assert res.data["ok"] is True
    assert res.data["synthetic_data"] is True
    assert res.data["contains_real_pii"] is False
    assert len(res.data["products"]) >= 1
    order = res.data["sample_order"]
    assert order["order_number"] == "AZ-TEST-1042"
    assert order["contains_real_pii"] is False
    assert len(order["items"]) >= 1
    assert res.data["sample_customer"]["email"] == "test@example.invalid"
    assert res.data["sample_customer"]["name"] == "Test Kund"


def test_extract_site_shell_sanitization_and_compose_standalone_page() -> None:
    webflow_shell = (
        "<!doctype html><html><head><title>Azom.no</title>"
        '<link rel="stylesheet" href="https://azom.no/style.css"></head>'
        '<body><header class="site-nav"><a href="/">Azom</a></header>'
        '<section class="hero"><h1>IKKE BARE EN VANLIG BILSTEREO</h1></section>'
        '<footer class="site-footer"><p>Azom AS</p></footer></body></html>'
    )
    shell = extract_site_shell(webflow_shell)
    assert shell.strategy == "header_footer"
    assert "site-nav" in shell.before_main
    assert "site-footer" in shell.after_main
    assert "IKKE BARE EN VANLIG BILSTEREO" not in shell.before_main
    assert "IKKE BARE EN VANLIG BILSTEREO" not in shell.after_main

    dirty_html = (
        '<section class="ty-card"><h1>Takk for bestillingen!</h1>'
        '<script>alert(1)</script><iframe src="https://evil.test"></iframe>'
        '<img src="https://azom.no/wp-content/uploads/box.jpg" alt="Box" onclick="evil()">'
        '<img src="https://evil.example.org/x.png" alt="Bad">'
        '<a href="javascript:alert(1)">Bad link</a>'
        '<a href="/setup-guide/">Setup-guide</a>'
        "</section>"
    )
    sanitized = sanitize_page_draft_html(dirty_html, allowed_hosts=frozenset({"azom.no"}))
    assert "<script" not in sanitized.lower()
    assert "<iframe" not in sanitized.lower()
    assert "onclick" not in sanitized.lower()
    assert "javascript:" not in sanitized.lower()
    assert "evil.example.org" not in sanitized
    assert "https://azom.no/wp-content/uploads/box.jpg" in sanitized
    assert 'href="/setup-guide/"' in sanitized

    composed = compose_standalone_page(
        webflow_shell,
        title="Takk for bestillingen — Azom",
        body_html=dirty_html,
        page_css=".ty-card { max-width: 960px; margin: 0 auto; }",
        extra_css_files=[("css/thankyou.css", ".ty-card h1 { text-transform: uppercase; }")],
        allowed_hosts=frozenset({"azom.no"}),
    )
    assert "<title>Takk for bestillingen — Azom</title>" in composed.html
    assert "site-nav" in composed.html and "site-footer" in composed.html
    assert "IKKE BARE EN VANLIG BILSTEREO" not in composed.html
    assert "css/thankyou.css" in composed.html
    assert ".ty-card { max-width: 960px; margin: 0 auto; }" in composed.html
    assert composed.injected_css_bytes > 0


def test_workspace_page_draft_pii_refusal_xss_refusal_and_risk_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Ny tacksida"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]

    # Rejects real email PII in synthetic_context
    bad_pii = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack",
            "title": "Tack",
            "page_type": "thankyou",
            "body_html": "<section><h1>Tack</h1></section>",
            "synthetic_context": {"customer_email": "kund@gmail.com"},
        },
        actor="oscar",
    )
    assert bad_pii.data["ok"] is False
    assert bad_pii.data["error_class"] == "RealPiiRefused"

    # Rejects real email PII in body_html
    bad_body_pii = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack",
            "title": "Tack",
            "body_html": "<p>Bekräftelse skickad till anna.svensson@hotmail.com</p>",
        },
        actor="oscar",
    )
    assert bad_body_pii.data["ok"] is False
    assert bad_body_pii.data["error_class"] == "RealPiiRefused"

    # Rejects XSS in body_html
    bad_xss = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack",
            "title": "Tack",
            "body_html": '<section onclick="alert(1)"><script>x()</script></section>',
        },
        actor="oscar",
    )
    assert bad_xss.data["ok"] is False
    assert bad_xss.data["error_class"] == "InvalidToolArguments"

    # Rejects blocked checkout path in shell_path
    bad_shell = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack",
            "title": "Tack",
            "shell_path": "/checkout/order-received/",
            "body_html": "<section><h1>Tack</h1></section>",
        },
        actor="oscar",
    )
    assert bad_shell.data["ok"] is False
    assert bad_shell.data["error_class"] == "PreviewPathRefused"

    # Valid zero-PII thank-you page draft stays low risk (even when text mentions order/frakt)
    ok_draft = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack-for-bestallningen",
            "title": "Tack för din beställning",
            "page_type": "thankyou",
            "body_html": "<section class='ty'><h1>Order #AZ-TEST-1042</h1><p>Fri frakt. test@example.invalid</p></section>",
            "page_css": ".ty { max-width: 960px; margin: 24px auto; }",
            "synthetic_context": {"order_number": "AZ-TEST-1042", "customer_email": "test@example.invalid"},
        },
        actor="oscar",
    )
    assert ok_draft.data["ok"] is True
    assert ok_draft.data["risk"] == "low"
    assert ok_draft.data["synthetic_data"] is True

    # Adding a checkout PHP template elevates workspace risk to high
    php_added = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "woocommerce/checkout/thankyou.php",
            "content": "<?php if ( ! defined( 'ABSPATH' ) ) { exit; } // checkout thankyou\n",
        },
        actor="oscar",
    )
    assert php_added.data["ok"] is True
    assert php_added.data["risk"] == "high"


def test_end_to_end_thankyou_standalone_preview_and_file_loop_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)

    # 1. Build sample data (mock mode)
    sample = tools.execute(
        "wp_build_sample_data",
        {"site_id": "mock-staging", "scenario": "thankyou", "product_count": 2},
        actor="oscar",
    )
    assert sample.data["ok"] is True

    # 2. Open workspace, create extra CSS + page_draft.json
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    opened = plans.open(
        "mock-staging",
        kind="theme",
        slug="azom-child",
        actor="oscar",
        summary="Tacksida förslag 1",
        variant_group="ty-v1",
        verified_theme=VerifiedTheme("azom-child", "parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]

    css_res = tools.execute(
        "wp_workspace_create_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "css/thankyou.css",
            "content": ".ty-wrap { padding: 32px; background: #111; color: #fff; }\n",
        },
        actor="oscar",
    )
    assert css_res.data["ok"] is True

    draft_res = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack-for-bestallningen",
            "title": "Tack för din beställning",
            "page_type": "thankyou",
            "body_html": (
                '<section class="ty-wrap"><h1>Tack för din beställning #AZ-TEST-1042</h1>'
                '<div class="ty-products"><p>Produkt 1</p></div>'
                '<a href="/setup-guide/">Monteringsguide</a></section>'
            ),
            "page_css": ".ty-products { display: grid; gap: 16px; }",
            "focus_selector": ".ty-wrap",
            "synthetic_context": {"order_number": "AZ-TEST-1042", "customer_email": "test@example.invalid"},
        },
        actor="oscar",
    )
    assert draft_res.data["ok"] is True

    # 3. Preview via wp_preview_page in live-shell standalone mode
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    shell_html = (
        "<!doctype html><html><head><title>Azom</title></head>"
        '<body><header class="site-header">Nav</header>'
        "<main><h1>Old Homepage Hero</h1></main>"
        '<footer class="site-footer">Footer</footer></body></html>'
    )
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/", **_kw: {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": shell_html,
            "bytes": len(shell_html),
        },
    )

    seen_revisions: list[tuple[list[str], str, str | None]] = []

    def fake_capture(
        site,
        *,
        path="/",
        artifact_dir,
        revisions,
        preview_kind_override=None,
        extra_preview_meta=None,
        **kwargs,
    ):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        rev_names = [lbl for lbl, _h in revisions]
        html_body = revisions[-1][1]
        seen_revisions.append((rev_names, html_body, preview_kind_override))
        shots = ("preview-desktop.png", "preview-mobile.png", "focus-desktop.png", "focus-mobile.png")
        for fname in shots:
            (artifact_dir / fname).write_bytes(_PNG_1X1)
        meta = {
            "preview_kind": preview_kind_override or "standalone_page",
            "path": path,
            "full_page": False,
            **(extra_preview_meta or {}),
        }
        (artifact_dir / "preview.json").write_text(json.dumps(meta), encoding="utf-8")
        return {
            "preview_kind": preview_kind_override or "standalone_page",
            "path": path,
            "screenshots": list(shots),
            "focus_matched": True,
            "visual_health": {"desktop": {"warnings": []}, "mobile": {"warnings": []}, "ok": True},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)

    steps = iter(["Tacksidan är renderad i sandlådan (desktop och mobil)."])
    loop_res = run_bounded_file_tool_loop(
        _tool_call("wp_preview_page", {"site_id": "mock-staging", "plan_id": plan_id}),
        [],
        complete=lambda _m: (next(steps), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert loop_res.handled is True
    # Only preview revision captured (no pointless homepage live-*.png)
    assert len(seen_revisions) == 1
    rev_names, rendered_html, kind_ov = seen_revisions[0]
    assert rev_names == ["preview"]
    assert kind_ov == "standalone_page"
    assert "Tack för din beställning #AZ-TEST-1042" in rendered_html
    assert "Old Homepage Hero" not in rendered_html
    assert "css/thankyou.css" in rendered_html
    # Both desktop AND mobile preview screenshots are preserved in attachments!
    basenames = [Path(p).name for p in loop_res.attachments]
    assert "preview-desktop.png" in basenames
    assert "preview-mobile.png" in basenames
    assert "focus-desktop.png" in basenames
    # _review_notice prints concrete risk (not "risk okänd")
    assert "risk okänd" not in loop_res.content
    assert "risk low" in loop_res.content
    # active_review_meta carries page_draft summary
    assert loop_res.active_review_meta is not None
    assert loop_res.active_review_meta["page_draft"]["slug"] == "tack-for-bestallningen"
    assert loop_res.active_review_meta["page_draft"]["page_type"] == "thankyou"
    assert loop_res.active_review_meta["page_draft"]["preview_kind"] == "standalone_page"
    # Interactive sandbox HTML has the standalone synthetic-data pill
    pv_html = (tmp_path / "wp_workspaces" / "mock-staging" / plan_id / "artifacts" / "preview.html").read_text(encoding="utf-8")
    assert "SANDBOX · NY SIDA (EXEMPELDATA)" in pv_html


def test_wc_template_overrides_audit_on_udesly_theme_without_woocommerce_dir_and_read_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)
    assert _FILE_READ_MAX == 20480

    # Simulate Webflow/Udesly compiled theme: NO woocommerce/ subdir, root wc-order-confirmation.php + >64KB CSS asset
    theme_dir = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "udesly-theme"
    (theme_dir / "assets").mkdir(parents=True, exist_ok=True)
    (theme_dir / "style.css").write_text("/*\nTheme Name: Udesly Shop\n*/\n", encoding="utf-8")
    (theme_dir / "wc-order-confirmation.php").write_text(
        "<?php\n// Udesly order confirmation template\n" + ("// line\n" * 2400),
        encoding="utf-8",
    )
    (theme_dir / "assets" / "webflow.css").write_text("body{}\n" * 15000, encoding="utf-8")  # > 90KB

    audit = tools.execute(
        "wc_template_overrides_audit",
        {"site_id": "mock-staging", "theme_slug": "udesly-theme"},
        actor="oscar",
    )
    assert audit.data["ok"] is True
    assert audit.data["has_woocommerce_dir"] is False
    assert "wc-order-confirmation.php" in audit.data["root_wc_templates"]

    # Reading an 18KB slice works within the raised 20480B limit without ToolResultTooLarge
    steps = iter(["Mallen lästes utan trunkeringsfel."])
    read_loop = run_bounded_file_tool_loop(
        _tool_call(
            "wp_read_theme_plugin_file",
            {
                "site_id": "mock-staging",
                "kind": "theme",
                "slug": "udesly-theme",
                "path": "wc-order-confirmation.php",
                "offset": 0,
                "limit": 18000,
            },
        ),
        [],
        complete=lambda _m: (next(steps), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert read_loop.handled is True
    assert read_loop.results[0]["ok"] is True
    assert len(read_loop.results[0]["content"]) >= 17000


def test_extract_site_shell_webflow_udesly_navbar_and_enclosing_main_div() -> None:
    from ecom_ops.wp_ops.preview_overlay import compose_standalone_page, extract_site_shell

    webflow_html = (
        "<!doctype html><html><head><title>Azom NO</title>"
        '<link rel="stylesheet" href="/assets/webflow.css"></head><body>'
        '<div data-collapse="medium" role="banner" class="nav02 w-nav">'
        '<div class="nav_inner"><a href="/">AZOM</a></div></div>'
        '<div class="main">'
        '<section class="hero"><h1>Old Homepage Hero</h1></section>'
        '<section class="products"><div>Featured</div></section>'
        '<footer class="f01"><div class="f_wrap">Azom Footer 2026</div></footer>'
        "</div>"
        '<script src="/assets/azom-no-woocommerce.js"></script>'
        "</body></html>"
    )
    shell = extract_site_shell(webflow_html)
    assert shell.strategy == "header_footer"
    assert 'class="nav02 w-nav"' in shell.before_main
    assert '<div class="main">' in shell.before_main
    assert "Old Homepage Hero" not in shell.before_main
    assert '<footer class="f01">' in shell.after_main
    assert "Azom Footer 2026" in shell.after_main

    composed = compose_standalone_page(
        webflow_html,
        title="Takk for bestillingen",
        body_html="<section><h1>Takk for bestillingen!</h1></section>",
    )
    assert "Old Homepage Hero" not in composed.html
    assert 'class="nav02 w-nav"' in composed.html
    assert "Takk for bestillingen!" in composed.html
    assert "Azom Footer 2026" in composed.html
    # Balanced <div class="main"> ... </div>
    assert composed.html.count("<div") == composed.html.count("</div")


def test_extract_site_shell_footer_only_and_classic_block_variants() -> None:
    from ecom_ops.wp_ops.preview_overlay import extract_site_shell

    # Classic theme with #masthead + role="contentinfo"
    classic_html = (
        "<!doctype html><html><head><title>Classic</title></head><body>"
        '<div id="masthead"><nav>Menu</nav></div>'
        '<div class="hero">Old</div>'
        '<div role="contentinfo">Copyright</div>'
        "</body></html>"
    )
    c_shell = extract_site_shell(classic_html)
    assert c_shell.strategy == "header_footer"
    assert 'id="masthead"' in c_shell.before_main
    assert 'role="contentinfo"' in c_shell.after_main
    assert "Old" not in c_shell.before_main

    # Footer-only fallback preserves footer chrome
    footer_only_html = (
        "<!doctype html><html><head><title>FooterOnly</title></head><body>"
        '<div class="wrap"><section>Old Hero</section>'
        "<footer>Footer Chrome</footer></div></body></html>"
    )
    f_shell = extract_site_shell(footer_only_html)
    assert f_shell.strategy == "footer_only"
    assert '<div class="wrap">' in f_shell.before_main
    assert "<footer>Footer Chrome</footer>" in f_shell.after_main
    assert "Old Hero" not in f_shell.before_main


def test_parse_numeric_price_nordic_thousands_and_negative_rejection() -> None:
    assert WpAgentTools._parse_numeric_price("1.490 NOK") == 1490.0
    assert WpAgentTools._parse_numeric_price("12.500 kr") == 12500.0
    assert WpAgentTools._parse_numeric_price("1.490,50 kr") == 1490.5
    assert WpAgentTools._parse_numeric_price("49,90 SEK") == 49.9
    assert WpAgentTools._parse_numeric_price("1490") == 1490.0
    assert WpAgentTools._parse_numeric_price("-100 SEK") is None
    assert WpAgentTools._parse_numeric_price("−49.00 NOK") is None


def test_fork_preserves_page_draft_and_inspect_element_defaults_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _service, tools = _setup_mock_wp(tmp_path, monkeypatch)
    opened = tools.execute(
        "wp_open_review_workspace",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "summary": "Thank you draft",
            "variant_group": "ty-iter",
        },
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_a = opened.data["plan_id"]

    drafted = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_a,
            "slug": "tack",
            "title": "Tack för din beställning",
            "page_type": "thankyou",
            "focus_selector": ".ty-hero",
            "body_html": '<section class="ty-hero"><h1>Tack!</h1></section>',
            "page_css": ".ty-hero { padding: 24px; }",
        },
        actor="oscar",
    )
    assert drafted.data["ok"] is True

    forked = tools.execute(
        "wp_workspace_fork",
        {
            "site_id": "mock-staging",
            "plan_id": plan_a,
            "summary": "Thank you variant B",
            "variant_group": "ty-iter",
        },
        actor="oscar",
    )
    assert forked.data["ok"] is True
    plan_b = forked.data["plan_id"]
    row_b = tools._review_plans().get(plan_b, site_id="mock-staging")
    assert row_b is not None
    assert isinstance(row_b.get("page_draft"), dict)
    assert row_b["page_draft"]["slug"] == "tack"

    # Passing a refused checkout path to wp_preview_page on a page_draft returns an actionable hint
    refused = tools.execute(
        "wp_preview_page",
        {"site_id": "mock-staging", "plan_id": plan_b, "path": "/checkout/order-received/"},
        actor="oscar",
    )
    assert refused.data["ok"] is False
    assert refused.data["error_class"] == "PreviewPathRefused"
    assert "page_draft.json" in str(refused.data.get("hint") or "")
