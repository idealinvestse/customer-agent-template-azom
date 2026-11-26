"""Tests for read-only product discovery, sandbox product editing, preview URLs, and fallthrough."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _helpers import MOCK_REGISTRY as REGISTRY
from _helpers import REPO_ROOT as ROOT
from _helpers import write_theme_file as _write_theme

from ecom_ops.bot import dashboard_links
from ecom_ops.bot.site_content import (
    _WP_CODE_FALLTHROUGH_RE,
    _WP_WORKSPACE_INTENT_RE,
    site_content_turn,
)
from ecom_ops.rbac import ROLE_PERMISSIONS, Permission, clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import (
    ALL_FILE_TOOLS,
    EXTRA_FILE_TOOLS,
    FILE_TOOL_SCHEMAS,
    WP_TOOL_GUIDE,
    WpAgentTools,
    _review_notice,
    _safe_tool_detail,
    _with_review_notice,
    run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.review_workspace import workspace_root
from ecom_ops.wp_ops.service import WpOpsResult, WpOpsService

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "0")
    monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
    monkeypatch.setenv("XAI_GROK_BIN", "")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("WOO_CONSUMER_KEY", raising=False)
    monkeypatch.delenv("WOO_CONSUMER_SECRET", raising=False)
    clear_rbac_cache()


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpOpsService:
    _setup_env(tmp_path, monkeypatch)
    return WpOpsService(registry_path=REGISTRY, data_dir=tmp_path)


def _open_child_workspace(tmp_path: Path, tools: WpAgentTools, site_id: str = "mock-staging") -> str:
    _write_theme(tmp_path, site_id, "parent-theme", "style.css", "/*\nTheme Name: Parent\n*/\n")
    _write_theme(
        tmp_path,
        site_id,
        "child-theme",
        "style.css",
        "/*\nTheme Name: Child\nTemplate: parent-theme\n*/\n",
    )
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": site_id, "kind": "theme", "slug": "child-theme", "summary": "Product sandbox"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    return str(opened.data["plan_id"])


def test_product_tools_registered_and_guide_budget() -> None:
    expected_new = {"wp_list_products", "wp_get_product", "wp_workspace_product_edit"}
    assert expected_new.issubset(EXTRA_FILE_TOOLS)
    assert expected_new.issubset(ALL_FILE_TOOLS)
    for name in expected_new:
        assert name in FILE_TOOL_SCHEMAS
    assert len(WP_TOOL_GUIDE) <= 7000
    assert "wp_list_products" in WP_TOOL_GUIDE
    assert "wp_get_product" in WP_TOOL_GUIDE
    assert "wp_workspace_product_edit" in WP_TOOL_GUIDE
    assert "preview_url" in WP_TOOL_GUIDE


def test_list_and_get_products_mock_readonly_and_rbac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(tmp_path, monkeypatch)
    tools = WpAgentTools(svc)

    # Jonatan (WP_OPS_READ) can list & get products read-only in mock mode
    listed = tools.execute("wp_list_products", {"site_id": "mock-shop", "limit": 5}, actor="jonatan")
    assert listed.data["ok"] is True
    assert listed.data["source"] == "mock"
    assert listed.data["live_calls"] is False
    assert listed.data["count"] >= 1
    first = listed.data["products"][0]
    assert first["id"] == 501
    assert first["name"] == "Azom Pro Headset"
    assert first["slug"] == "azom-pro-headset"
    assert first["path"] == "/produkt/azom-pro-headset/"

    # Get by id, slug, and path all work
    for args in (
        {"site_id": "mock-shop", "product_id": 501},
        {"site_id": "mock-shop", "slug": "azom-pro-headset"},
        {"site_id": "mock-shop", "path": "/produkt/azom-pro-headset/"},
    ):
        detail = tools.execute("wp_get_product", args, actor="jonatan")
        assert detail.data["ok"] is True
        assert detail.data["live_calls"] is False
        assert detail.data["product"]["id"] == 501
        assert detail.data["product"]["name"] == "Azom Pro Headset"
        assert detail.data["product"]["path"] == "/produkt/azom-pro-headset/"

    # Missing identifier -> InvalidToolArguments with hint
    missing_id = tools.execute("wp_get_product", {"site_id": "mock-shop"}, actor="jonatan")
    assert missing_id.data["ok"] is False
    assert missing_id.data["error_class"] == "InvalidToolArguments"
    assert "hint" in missing_id.data

    # Blocked path refused on wp_get_product
    blocked_path = tools.execute(
        "wp_get_product", {"site_id": "mock-shop", "path": "/checkout/"}, actor="jonatan"
    )
    assert blocked_path.data["ok"] is False
    assert blocked_path.data["error_class"] == "PreviewPathRefused"

    # Unauthorized actor without WP_OPS_READ is denied
    monkeypatch.setitem(ROLE_PERMISSIONS, "viewer", frozenset({Permission.READ}))
    clear_rbac_cache()
    denied = tools.execute("wp_list_products", {"site_id": "mock-shop"}, actor="jonatan")
    assert denied.data["ok"] is False
    assert denied.data["error_class"] == "AccessDenied"


def test_live_products_fallback_html_and_secret_scrubbing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", "user:SuperSecretProdCred999")
    tools = WpAgentTools(svc)

    # No REST endpoints reachable -> falls back cleanly to read-only public HTML
    monkeypatch.setattr(tools, "_safe_site_get_json", lambda *_a, **_k: None)
    monkeypatch.setattr(tools, "_try_woo_client_read", lambda *_a, **_k: None)

    shop_html = """
    <html><body>
      <a href="https://mock-shop.example.test/produkt/el-sykkel-pro/">El-Sykkel Pro</a>
      <a href="/produkt/hjelm-nordic/">Hjelm Nordic</a>
      <a href="/checkout/">Kasse</a>
    </body></html>
    """
    product_html = """
    <html>
      <head>
        <title>El-Sykkel Pro - Azom</title>
        <script type="application/ld+json">
          {"@type": "Product", "name": "El-Sykkel Pro", "sku": "EL-001", "offers": {"price": "19990", "priceCurrency": "NOK"}}
        </script>
      </head>
      <body>
        <h1 class="product_title">El-Sykkel Pro</h1>
        <p class="price"><del>24 990 kr</del><ins>19 990 kr</ins></p>
        <div class="woocommerce-product-details__short-description">
          Kort tekst user:SuperSecretProdCred999
        </div>
        <div id="tab-description">Full produktbeskrivning för nordiskt klimat.</div>
        <button class="single_add_to_cart_button">Legg i handlekurv</button>
        <span class="posted_in"><a href="/kat/sykler/">Elsykler</a></span>
      </body>
    </html>
    """

    def fake_fetch(site: Any, path: str = "/") -> dict[str, Any]:
        if path in {"/butikk/", "/shop/", "/"}:
            return {"status": 200, "final_url": f"{site.canonical_url}{path}", "html": shop_html, "bytes": len(shop_html)}
        if path == "/produkt/el-sykkel-pro/":
            return {"status": 200, "final_url": f"{site.canonical_url}{path}", "html": product_html, "bytes": len(product_html)}
        raise AssertionError(f"Unexpected path: {path}")

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.fetch_live_html", fake_fetch)

    listed = tools.execute("wp_list_products", {"site_id": "mock-shop", "search": "sykkel"}, actor="oscar")
    assert listed.data["ok"] is True
    assert listed.data["source"] == "live_html"
    assert listed.data["live_calls"] is True
    assert len(listed.data["products"]) == 1
    assert listed.data["products"][0]["path"] == "/produkt/el-sykkel-pro/"

    detail = tools.execute(
        "wp_get_product",
        {"site_id": "mock-shop", "path": "/produkt/el-sykkel-pro/"},
        actor="oscar",
    )
    assert detail.data["ok"] is True
    assert detail.data["source"] == "live_html"
    prod = detail.data["product"]
    assert prod["name"] == "El-Sykkel Pro"
    assert prod["sku"] == "EL-001"
    assert prod["button_text"] == "Legg i handlekurv"
    assert prod["categories"] == ["Elsykler"]
    assert "SuperSecretProdCred999" not in json.dumps(detail.data)


def test_workspace_product_edit_writes_only_sandbox_merges_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(tmp_path, monkeypatch)
    tools = WpAgentTools(svc)
    plan_id = _open_child_workspace(tmp_path, tools)

    # Assert zero live WooCommerce writes can ever happen
    def boom_woo(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Live WooCommerce write must NEVER be called!")

    monkeypatch.setattr("ecom_ops.integrations.woocommerce.WooCommerceClient.update_product", boom_woo)
    monkeypatch.setattr("ecom_ops.integrations.woocommerce.WooCommerceClient.update_product_description", boom_woo)

    # Viewer (jonatan) cannot mutate workspace
    denied = tools.execute(
        "wp_workspace_product_edit",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "product_path": "/produkt/azom-pro-headset/",
            "title": "Blocked",
        },
        actor="jonatan",
    )
    assert denied.data["ok"] is False
    assert denied.data["error_class"] == "AccessDenied"

    # Checkout/admin/traversal paths refused
    for bad_path in ("/checkout/", "/cart", "/kasse/", "/wp-admin/", "/produkt/../secret"):
        bad = tools.execute(
            "wp_workspace_product_edit",
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "product_path": bad_path,
                "title": "Test",
            },
            actor="oscar",
        )
        assert bad.data["ok"] is False
        assert bad.data["error_class"] == "PreviewPathRefused", bad_path

    # Script / XSS payloads rejected
    for bad_field, bad_val in (
        ("title", "Test <script>alert(1)</script>"),
        ("description", '<img src=x onerror="evil()">'),
        ("short_description", "<iframe src='https://evil.test'></iframe>"),
        ("button_text", "javascript:alert(1)"),
    ):
        bad = tools.execute(
            "wp_workspace_product_edit",
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "product_path": "/produkt/azom-pro-headset/",
                bad_field: bad_val,
            },
            actor="oscar",
        )
        assert bad.data["ok"] is False
        assert bad.data["error_class"] == "InvalidToolArguments", bad_field

    # Empty edits rejected
    no_edits = tools.execute(
        "wp_workspace_product_edit",
        {"site_id": "mock-staging", "plan_id": plan_id, "product_path": "/produkt/azom-pro-headset/"},
        actor="oscar",
    )
    assert no_edits.data["ok"] is False
    assert no_edits.data["error_class"] == "InvalidToolArguments"

    # First valid edit creates work/product_edits.json cleanly inside wr-* sandbox
    first = tools.execute(
        "wp_workspace_product_edit",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "product_path": "/produkt/azom-pro-headset/",
            "product_id": 501,
            "title": "Testprodukt 123",
            "price": "399 kr",
            "replacements": [{"old_text": "Gammal rad", "new_text": "Ny rad"}],
        },
        actor="oscar",
    )
    assert first.data["ok"] is True
    assert first.data["executed"] is False
    assert first.data["live_calls"] is False
    assert first.data["product_path"] == "/produkt/azom-pro-headset/"
    assert first.data["product_id"] == 501
    assert set(first.data["updated_fields"]) == {"price", "replacements", "title"}
    assert "wp_preview_page" in first.data["hint"]
    assert "preview_url" in first.data["hint"]

    # Second edit on same product_path merges fields & updates work_sha256
    second = tools.execute(
        "wp_workspace_product_edit",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "product_path": "/produkt/azom-pro-headset/",
            "button_text": "Köp direkt",
            "badge_text": "Nyhet",
        },
        actor="oscar",
    )
    assert second.data["ok"] is True
    assert second.data["work_sha256"] != first.data["work_sha256"]

    edits_file = workspace_root(tmp_path, "mock-staging", plan_id) / "work" / "product_edits.json"
    assert edits_file.is_file()
    stored = json.loads(edits_file.read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1
    assert stored["product_path"] == "/produkt/azom-pro-headset/"
    assert stored["product_id"] == 501
    assert stored["title"] == "Testprodukt 123"
    assert stored["price"] == "399 kr"
    assert stored["button_text"] == "Köp direkt"
    assert stored["badge_text"] == "Nyhet"
    assert stored["replacements"] == [{"old_text": "Gammal rad", "new_text": "Ny rad"}]

    # _safe_tool_detail includes site_id, plan_id, product_path
    detail = _safe_tool_detail(
        "wp_workspace_product_edit",
        {"site_id": "mock-staging", "plan_id": plan_id, "product_path": "/produkt/azom-pro-headset/"},
        None,
    )
    assert detail == f"mock-staging · {plan_id} · /produkt/azom-pro-headset/"


def test_preview_page_auto_infers_product_path_and_surfaces_preview_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dash.azom.test")
    tools = WpAgentTools(svc)
    plan_id = _open_child_workspace(tmp_path, tools)

    edit_res = tools.execute(
        "wp_workspace_product_edit",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "product_path": "/produkt/azom-pro-headset/",
            "title": "Testprodukt 123",
        },
        actor="oscar",
    )
    assert edit_res.data["ok"] is True

    art_dir = workspace_root(tmp_path, "mock-staging", plan_id) / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    shot = art_dir / "change-desktop.png"
    shot.write_bytes(_PNG_BYTES)
    (art_dir / "preview.html").write_text("<html><body>Testprodukt 123</body></html>", encoding="utf-8")

    seen_paths: list[str] = []
    expected_preview_url = f"https://dash.azom.test/wp/mock-staging/reviews/{plan_id}/preview"

    def fake_live_preview(
        self: Any,
        site_id: str,
        pid: str,
        *,
        actor: str,
        path: str = "/",
        **_kwargs: Any,
    ) -> WpOpsResult:
        del self, actor
        seen_paths.append(path)
        return WpOpsResult(
            True,
            "Live-förhandsvisning skapad.",
            {
                "plan_id": pid,
                "site_id": site_id,
                "path": path,
                "screenshots": ["change-desktop.png"],
                "screenshot_paths": [str(shot)],
                "focus_matched": True,
                "focus_target": "text:Testprodukt 123",
                "preview_path": f"/wp/{site_id}/reviews/{pid}/preview",
                "preview_url": expected_preview_url,
                "preview_html_ready": True,
                "overlay": {"counts": {"applied": 1}, "highlight_token": "Testprodukt"},
            },
        )

    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.ReviewPlans.live_preview", fake_live_preview)

    # Call wp_preview_page WITHOUT path -> auto-uses product_edits.json's product_path!
    pv = tools.execute(
        "wp_preview_page",
        {"site_id": "mock-staging", "plan_id": plan_id, "focus_text": "Testprodukt 123"},
        actor="oscar",
    )
    assert pv.data["ok"] is True
    assert seen_paths == ["/produkt/azom-pro-headset/"]
    assert pv.data["path"] == "/produkt/azom-pro-headset/"
    assert pv.data["preview_path"] == f"/wp/mock-staging/reviews/{plan_id}/preview"
    assert pv.data["preview_url"] == expected_preview_url
    assert pv.data["preview_html_ready"] is True
    assert pv.attachments == (str(shot),)

    # _review_notice and _with_review_notice include the clickable sandbox preview URL
    notice = _review_notice([pv.data])
    assert expected_preview_url in notice
    assert f"Granskning {plan_id}" in notice

    full_reply = _with_review_notice("Ändringen är klar i sandlådan.", [pv.data])
    assert expected_preview_url in full_reply

    # Also test fallback when preview_url is absent in result dict but sandbox_preview_url resolves
    monkeypatch.setattr(
        dashboard_links,
        "sandbox_preview_url",
        lambda s, p: f"http://127.0.0.1:8080/wp/{s}/reviews/{p}/preview",
        raising=False,
    )
    fallback_notice = _review_notice(
        [
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "review_path": f"/wp/mock-staging/reviews/{plan_id}",
                "preview_path": f"/wp/mock-staging/reviews/{plan_id}/preview",
                "preview_html_ready": True,
            }
        ]
    )
    assert f"http://127.0.0.1:8080/wp/mock-staging/reviews/{plan_id}/preview" in fallback_notice


def test_file_loop_surfaces_sandbox_preview_url_after_product_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    tools = WpAgentTools(svc)
    plan_id = _open_child_workspace(tmp_path, tools)

    art_dir = workspace_root(tmp_path, "mock-staging", plan_id) / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    shot = art_dir / "change-desktop.png"
    shot.write_bytes(_PNG_BYTES)
    preview_url = f"https://dash.azom.test/wp/mock-staging/reviews/{plan_id}/preview"

    monkeypatch.setattr(
        "ecom_ops.wp_ops.review_plan.ReviewPlans.live_preview",
        lambda _self, site_id, pid, *, actor, path="/", **_k: WpOpsResult(
            True,
            "ok",
            {
                "plan_id": pid,
                "site_id": site_id,
                "path": path,
                "screenshots": ["change-desktop.png"],
                "screenshot_paths": [str(shot)],
                "preview_path": f"/wp/{site_id}/reviews/{pid}/preview",
                "preview_url": preview_url,
                "preview_html_ready": True,
            },
        ),
    )

    step1 = (
        "<tool_call>"
        + json.dumps(
            {
                "name": "wp_workspace_product_edit",
                "arguments": {
                    "site_id": "mock-staging",
                    "plan_id": plan_id,
                    "product_path": "/produkt/azom-pro-headset/",
                    "title": "Ny titel",
                },
            }
        )
        + "</tool_call>"
    )
    step2 = (
        "<tool_call>"
        + json.dumps(
            {
                "name": "wp_preview_page",
                "arguments": {
                    "site_id": "mock-staging",
                    "plan_id": plan_id,
                    "focus_text": "Ny titel",
                },
            }
        )
        + "</tool_call>"
    )
    replies = iter([(step2, 0.0), ("Klart i sandlådan!", 0.0)])

    loop_res = run_bounded_file_tool_loop(
        step1,
        [{"role": "user", "content": "Ändra produkten i sandlådan"}],
        complete=lambda _m: next(replies),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:77",
        tools=tools,
    )
    assert loop_res.handled is True
    assert loop_res.attachments == (str(shot),)
    assert preview_url in loop_res.content


def test_site_content_fallthrough_for_product_page_and_annotated_screenshot_edits() -> None:
    fallthrough_phrases = (
        "Redigera produktsidan i sandlådan och skicka print",
        "Ändra det inringade på produktsidan till Testprodukt 123",
        "Kolla bifogad bild och ändra köpknappen på /produkt/azom-pro/",
        "Uppdatera single-product.php i arbetsytan",
        "Testa sandboxsidan för produkten",
    )
    for phrase in fallthrough_phrases:
        assert _WP_CODE_FALLTHROUGH_RE.search(phrase), phrase
        turn = site_content_turn(phrase, allow_wp_ops_fallthrough=False)
        assert turn.consumed is False, phrase

    assert _WP_WORKSPACE_INTENT_RE.search("Ändra det inringade till Testprodukt 123")
    assert _WP_WORKSPACE_INTENT_RE.search("Redigera produktsidan")

    # Existing site_content behavior must remain intact
    refused = site_content_turn("ändra functions.php", allow_wp_ops_fallthrough=False)
    assert refused.consumed is True
    assert "inte" in refused.results[0][1].lower()


def test_end_to_end_product_edit_to_real_live_preview_html_and_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real ReviewPlans.live_preview + apply_product_edits_overlay + sandbox_preview_url integration."""
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    monkeypatch.setenv("AZOM_SANDBOX_PREVIEW_BASE_URL", "http://127.0.0.1:8080")
    tools = WpAgentTools(svc)
    plan_id = _open_child_workspace(tmp_path, tools)

    edit = tools.execute(
        "wp_workspace_product_edit",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "product_path": "/produkt/azom-pro-headset/",
            "product_id": 501,
            "title": "Testprodukt 123",
            "price": "499 kr",
            "button_text": "Köp i sandbox",
        },
        actor="oscar",
    )
    assert edit.data["ok"] is True

    live_html = (
        "<html><head><title>Azom Pro Headset</title></head><body>"
        "<h1 class='product_title'>Azom Pro Headset</h1>"
        "<p class='price'><span class='woocommerce-Price-amount'>599 kr</span></p>"
        "<button class='single_add_to_cart_button'>Legg i handlekurv</button>"
        "</body></html>"
    )
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/": {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": live_html,
            "bytes": len(live_html),
        },
    )

    def fake_capture(
        _site: Any,
        *,
        path: str = "/",
        artifact_dir: Path,
        revisions: list[tuple[str, str]],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        del path
        # Verify the overlay revision received the product edits
        overlay_html = revisions[-1][1]
        assert "Testprodukt 123" in overlay_html
        assert "499 kr" in overlay_html
        assert "Köp i sandbox" in overlay_html
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "change-desktop.png").write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {},
            "screenshots": ["change-desktop.png"],
            "focus_matched": True,
            "focus_target": "text:Testprodukt 123",
            "revisions": {"live": {"desktop": {}, "mobile": {}}, "preview": {"desktop": {}, "mobile": {}}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)

    pv = tools.execute(
        "wp_preview_page",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="oscar",
    )
    assert pv.data["ok"] is True
    assert pv.data["path"] == "/produkt/azom-pro-headset/"
    assert pv.data["preview_html_ready"] is True
    assert pv.data["preview_path"] == f"/wp/mock-staging/reviews/{plan_id}/preview"
    assert pv.data["preview_url"] == f"http://127.0.0.1:8080/wp/mock-staging/reviews/{plan_id}/preview"

    preview_html_file = workspace_root(tmp_path, "mock-staging", plan_id) / "artifacts" / "preview.html"
    assert preview_html_file.is_file()
    rendered_html = preview_html_file.read_text(encoding="utf-8")
    assert "Testprodukt 123" in rendered_html
    assert "AZOM SANDBOX" in rendered_html

    notice = _review_notice([pv.data])
    assert pv.data["preview_url"] in notice

