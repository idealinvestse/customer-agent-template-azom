"""Tests for Slice 1: Azom Ops Bridge v1.2.0 SEO module, WP/Woo Media & Category APIs,
Runtime Env Gates, and Change Vault SEO Rollback."""

from __future__ import annotations

import importlib.util
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient
from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient
from ecom_ops.integrations.wp.bridge_client import (
    InMemoryWpBridgeTransport,
    WpBridgeClient,
)
from ecom_ops.runtime_env import PROTECTED_OVERLAY_KEYS, overlay_key_allowed
from ecom_ops.security import SecurityError
from ecom_ops.wp_ops.change_ledger import ChangeLedger
from ecom_ops.wp_ops.rollback_service import ChangeRollbackService

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_DIR = _REPO_ROOT / "wordpress-plugin" / "azom-ops-bridge"


def _load_builder_module():
    spec = importlib.util.spec_from_file_location(
        "build_wp_bridge_plugin",
        _REPO_ROOT / "bin" / "build-wp-bridge-plugin.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_packager_includes_class_seo_and_passes_static_audit() -> None:
    mod = _load_builder_module()
    assert "includes/class-seo.php" in mod._REQUIRED_FILES

    meta = mod.validate_plugin_source(_PLUGIN_DIR)
    assert meta["ok"] is True
    assert meta["php_files"] >= 8
    assert meta["violations"] == []

    zip_bytes, report = mod.build_plugin_zip_bytes(_PLUGIN_DIR)
    assert report["ok"] is True
    assert "azom-ops-bridge/includes/class-seo.php" in report["files"]

    with zipfile.ZipFile(BytesIO(zip_bytes), "r") as zf:
        names = set(zf.namelist())
        assert "azom-ops-bridge/includes/class-seo.php" in names
        main_src = zf.read("azom-ops-bridge/azom-ops-bridge.php").decode("utf-8")
        assert "Version:     1.2.0" in main_src
        assert "AZOM_OPS_BRIDGE_VERSION', '1.2.0'" in main_src
        readme_src = zf.read("azom-ops-bridge/readme.txt").decode("utf-8")
        assert "Stable tag: 1.2.0" in readme_src


def test_php_plugin_v120_seo_routes_and_security_invariants() -> None:
    main_php = (_PLUGIN_DIR / "azom-ops-bridge.php").read_text(encoding="utf-8")
    seo_php = (_PLUGIN_DIR / "includes" / "class-seo.php").read_text(encoding="utf-8")

    # All 4 SEO route paths registered and gated by Azom_Ops_Bridge_Auth::verify_request
    for route in ("/seo/overview", "/seo/entities", "/seo/meta", "/seo/media"):
        assert f"'{route}'" in main_php
    assert "require_once AZOM_OPS_BRIDGE_DIR . 'includes/class-seo.php';" in main_php
    assert main_php.count("[Azom_Ops_Bridge_Auth::class, 'verify_request']") >= 19

    # Security & allowlist invariants in class-seo.php
    assert "defined('ABSPATH')" in seo_php
    assert "PLUGIN_META_MAP" in seo_php
    assert "ALLOWED_META_FIELDS" in seo_php
    assert "ALLOWED_MEDIA_FIELDS" in seo_php
    assert "expected_before_sha256" in seo_php
    assert "hash_equals" in seo_php
    assert "Azom_Ops_Bridge_Ops::flush_all_caches" in seo_php
    for plugin_marker in ("WPSEO_VERSION", "RANK_MATH_VERSION", "AIOSEO_VERSION", "SEOPRESS_VERSION"):
        assert plugin_marker in seo_php
    assert "_wp_attachment_image_alt" in seo_php


def test_bridge_client_seo_endpoints_fixtures_and_optimistic_locking() -> None:
    tr = InMemoryWpBridgeTransport(site_id="azom-no", base_url="https://azom.no")
    client = WpBridgeClient(site_id="azom-no", transport=tr)

    # 1. seo_overview
    ov = client.seo_overview()
    assert ov["ok"] is True
    assert ov["seo_plugin"] == "yoast"
    assert ov["blog_public"] is True
    assert ov["permalink_structure"] == "/%postname%/"
    assert ov["home_url"] == "https://azom.no"
    assert ov["robots_url"] == "https://azom.no/robots.txt"
    assert any("sitemap" in u for u in ov["sitemap_urls"])
    assert ov["counts"]["products"] >= 3
    assert ov["counts"]["pages"] >= 2
    assert ov["counts"]["posts"] >= 2
    assert ov["counts"]["media"] >= 4

    # 2. seo_entities (all + filtered + realistic audit issues)
    ent_all = client.seo_entities(object_type="all", limit=50, page=1)
    assert ent_all["ok"] is True
    assert ent_all["seo_plugin"] == "yoast"
    assert ent_all["total"] == len(ent_all["entities"]) >= 7

    # Rich Nordic CarPlay fixtures intentionally include actionable issues
    titles = [e["meta_title"] for e in ent_all["entities"]]
    assert any(len(t) > 65 for t in titles)  # long title
    assert len(titles) > len(set(titles))  # duplicate meta title
    assert any(e["meta_description"] == "" for e in ent_all["entities"])  # missing meta desc
    assert any(e["word_count"] < 100 for e in ent_all["entities"])  # thin product description
    assert any(e["images_missing_alt"] > 0 for e in ent_all["entities"])  # missing alt
    keyphrases = [e["focus_keyphrase"] for e in ent_all["entities"] if e["focus_keyphrase"]]
    assert keyphrases.count("trådlös carplay adapter") >= 2  # cannibalization cluster

    ent_prods = client.seo_entities(object_type="product", limit=10, page=1)
    assert all(e["object_type"] == "product" for e in ent_prods["entities"])
    prod_501 = next(e for e in ent_prods["entities"] if e["object_id"] == 501)
    assert len(prod_501["before_sha256"]) == 64

    # 3. seo_update_meta (disallowed field rejected, stale hash rejected, valid update succeeds)
    with pytest.raises(SecurityError, match="400"):
        client.seo_update_meta(
            change_id="chg-seo-bad",
            object_type="product",
            object_id=501,
            fields={"_arbitrary_evil_meta": "x"},
        )

    with pytest.raises(SecurityError, match="409"):
        client.seo_update_meta(
            change_id="chg-seo-stale",
            object_type="product",
            object_id=501,
            fields={"meta_title": "Azom CarPlay Pro | Trådlös Adapter"},
            expected_before_sha256="0" * 64,
        )

    flushes_before = tr.cache_flushes
    upd = client.seo_update_meta(
        change_id="chg-seo-0001",
        object_type="product",
        object_id=501,
        fields={
            "meta_title": "Azom CarPlay Pro | Trådlös Apple CarPlay Adapter",
            "meta_description": "Snabb trådlös Apple CarPlay adapter med 5 GHz WiFi och fri frakt i Norden.",
        },
        expected_before_sha256=prod_501["before_sha256"],
    )
    assert upd["ok"] is True
    assert upd["change_id"] == "chg-seo-0001"
    assert upd["before_sha256"] == prod_501["before_sha256"]
    assert upd["after_sha256"] != upd["before_sha256"]
    assert upd["after"]["meta_title"] == "Azom CarPlay Pro | Trådlös Apple CarPlay Adapter"
    assert tr.cache_flushes == flushes_before + 1

    # 4. seo_media_inventory
    inv = client.seo_media_inventory(limit=50, page=1)
    assert inv["ok"] is True
    assert inv["total"] >= 4
    img_101 = next(m for m in inv["media"] if m["media_id"] == 101)
    assert img_101["alt_text"] == ""
    assert img_101["filename"] == "IMG_4821.jpg"
    assert len(img_101["before_sha256"]) == 64

    # 5. seo_update_media (stale hash 409 + disallowed field 400 + valid update)
    with pytest.raises(SecurityError, match="400"):
        client.seo_update_media(
            change_id="chg-med-bad",
            media_id=101,
            fields={"post_status": "trash"},
        )
    with pytest.raises(SecurityError, match="409"):
        client.seo_update_media(
            change_id="chg-med-stale",
            media_id=101,
            fields={"alt_text": "Ny alt"},
            expected_before_sha256="f" * 64,
        )
    med_upd = client.seo_update_media(
        change_id="chg-med-0001",
        media_id=101,
        fields={
            "alt_text": "Azom CarPlay Trådlös Adapter Pro i bilens mittkonsol",
            "title": "Azom CarPlay Adapter Pro produktbild",
        },
        expected_before_sha256=img_101["before_sha256"],
    )
    assert med_upd["ok"] is True
    assert med_upd["before"]["alt_text"] == ""
    assert med_upd["after"]["alt_text"] == "Azom CarPlay Trådlös Adapter Pro i bilens mittkonsol"
    assert med_upd["after_sha256"] != med_upd["before_sha256"]


def test_wordpress_media_and_seo_post_page_updates() -> None:
    wp = WordPressClient(base_url="https://mock.local", transport=InMemoryWpTransport())

    m = wp.get_media(10)
    assert m["id"] == 10
    assert "alt_text" in m
    assert "caption" in m
    assert "description" in m
    assert m["media_details"]["width"] == 512

    updated_m = wp.update_media(
        10,
        alt_text="Azom varumärkeslogotyp",
        title="Azom Logo",
        caption="Officiell logotyp",
        description="Logotyp för Azom Norden",
    )
    assert updated_m["alt_text"] == "Azom varumärkeslogotyp"
    assert updated_m["title"]["rendered"] == "Azom Logo"
    assert updated_m["caption"]["rendered"] == "Officiell logotyp"
    assert updated_m["description"]["rendered"] == "Logotyp för Azom Norden"

    # Empty alt_text allowed (e.g. decorative image or rollback to empty)
    cleared = wp.update_media(10, alt_text="")
    assert cleared["alt_text"] == ""

    # No fields fails closed
    with pytest.raises(SecurityError, match="No media fields"):
        wp.update_media(10)

    # Post and page excerpt + slug updates
    p = wp.update_post(1, excerpt="Kort sammanfattning för SEO", slug="hej-fran-azom")
    assert p.raw["slug"] == "hej-fran-azom"
    assert p.raw["excerpt"]["rendered"] == "Kort sammanfattning för SEO"

    pg = wp.update_page(2, excerpt="Om Azom AB i Norden", slug="om-azom-ab")
    assert pg.raw["slug"] == "om-azom-ab"
    assert pg.raw["excerpt"]["rendered"] == "Om Azom AB i Norden"


def test_woocommerce_categories_and_product_slug_update() -> None:
    woo = WooCommerceClient(base_url="https://mock.local", transport=InMemoryWooTransport())

    cats = woo.list_product_categories(per_page=50, page=1)
    assert len(cats) >= 2
    assert any(c["slug"] == "tradlos-carplay" for c in cats)

    filtered = woo.list_product_categories(search="carplay")
    assert len(filtered) == 1 and filtered[0]["slug"] == "tradlos-carplay"

    updated = woo.update_product("501", {"slug": "Azom Pro Headset 2026!", "unknown_key": "drop"})
    assert updated["slug"] == "azom-pro-headset-2026"
    assert "unknown_key" not in updated


def test_runtime_env_protects_seo_gates() -> None:
    assert "AZOM_SEO_WRITE_ENABLED" in PROTECTED_OVERLAY_KEYS
    assert "AZOM_SEO_KILL" in PROTECTED_OVERLAY_KEYS
    assert overlay_key_allowed("AZOM_SEO_WRITE_ENABLED") is False
    assert overlay_key_allowed("AZOM_SEO_KILL") is False


def test_rollback_service_seo_meta_and_media_alt_and_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    # Verify zero banned tokens in rollback_service.py
    rb_src = (_REPO_ROOT / "skills" / "ecom_ops" / "wp_ops" / "rollback_service.py").read_text(encoding="utf-8")
    for banned in ("urllib.request", "urlopen", "http.client", "requests", "subprocess", "os.system", "integrations.wordpress"):
        assert banned not in rb_src

    tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    bc = WpBridgeClient(site_id="mock-staging", transport=tr)
    ledger = ChangeLedger(data_dir=tmp_path)
    svc = ChangeRollbackService(data_dir=tmp_path)

    # --- seo_meta rollback via Bridge ---
    orig_title = tr.seo_entities_store[("product", 501)]["meta_title"]
    orig_desc = tr.seo_entities_store[("product", 501)]["meta_description"]
    new_title = "Azom CarPlay Pro — Ny SEO Titel"
    new_desc = "Ny konverterande meta description för CarPlay Pro."
    bc.seo_update_meta(
        change_id="chg-seo-apply-1",
        object_type="product",
        object_id=501,
        fields={"meta_title": new_title, "meta_description": new_desc},
    )
    seo_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="seo_meta",
        transport="bridge",
        actor="oscar",
        summary="Uppdaterade Yoast SEO-meta för produkt 501",
        target_ref="product:501",
        before_data={"object_type": "product", "object_id": 501, "meta_title": orig_title, "meta_description": orig_desc},
        after_data={"object_type": "product", "object_id": 501, "meta_title": new_title, "meta_description": new_desc},
        metadata={"object_type": "product", "object_id": 501},
    )

    # Preview without drift
    prev = svc.preview_rollback("mock-staging", seo_row["change_id"], actor="oscar", bridge_client=bc)
    assert prev["ok"] is True
    assert prev["can_rollback"] is True
    assert prev["drift_detected"] is False

    # Simulate drift then confirm preview detects it
    tr.seo_entities_store[("product", 501)]["meta_title"] = "Manuellt ändrad i wp-admin"
    prev_drift = svc.preview_rollback("mock-staging", seo_row["change_id"], actor="oscar", bridge_client=bc)
    assert prev_drift["drift_detected"] is True
    assert "meta_title" in prev_drift["drift_note"]

    # AZOM_SEO_KILL blocks preview and execute_rollback
    monkeypatch.setenv("AZOM_SEO_KILL", "1")
    prev_killed = svc.preview_rollback("mock-staging", seo_row["change_id"], actor="oscar", bridge_client=bc)
    assert prev_killed["can_rollback"] is False
    assert prev_killed["block_reason"] == "SeoKillSwitchActive"
    rb_killed = svc.execute_rollback(
        "mock-staging",
        seo_row["change_id"],
        actor="oscar",
        expected_before_sha256=seo_row["before_sha256"],
        bridge_client=bc,
    )
    assert rb_killed.ok is False
    assert rb_killed.data["error_class"] == "SeoKillSwitchActive"
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)

    # Execute seo_meta rollback -> restores original values on bridge
    rb_seo = svc.execute_rollback(
        "mock-staging",
        seo_row["change_id"],
        actor="oscar",
        expected_before_sha256=seo_row["before_sha256"],
        bridge_client=bc,
    )
    assert rb_seo.ok is True
    assert rb_seo.data["transport"] == "bridge"
    assert tr.seo_entities_store[("product", 501)]["meta_title"] == orig_title
    assert tr.seo_entities_store[("product", 501)]["meta_description"] == orig_desc

    # --- media_alt rollback via Bridge ---
    bc.seo_update_media(
        change_id="chg-med-apply-1",
        media_id=101,
        fields={"alt_text": "Uppdaterad alt-text"},
    )
    med_bridge_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="media_alt",
        transport="bridge",
        actor="oscar",
        summary="Uppdaterade alt-text via Bridge",
        target_ref="media:101",
        before_data={"media_id": 101, "alt_text": ""},
        after_data={"media_id": 101, "alt_text": "Uppdaterad alt-text"},
        metadata={"media_id": 101},
    )
    rb_med_bridge = svc.execute_rollback(
        "mock-staging",
        med_bridge_row["change_id"],
        actor="oscar",
        expected_before_sha256=med_bridge_row["before_sha256"],
        bridge_client=bc,
    )
    assert rb_med_bridge.ok is True
    assert rb_med_bridge.data["transport"] == "bridge"
    assert tr.seo_media_store[101]["alt_text"] == ""

    # --- media_alt rollback via WP REST ---
    wp = WordPressClient(base_url="https://mock.local", transport=InMemoryWpTransport())
    wp.update_media(10, alt_text="Ny WP REST alt-text")
    med_wp_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="media_alt",
        transport="wp_rest",
        actor="oscar",
        summary="Uppdaterade alt-text via WP REST",
        target_ref="media:10",
        before_data={"media_id": 10, "alt_text": ""},
        after_data={"media_id": 10, "alt_text": "Ny WP REST alt-text"},
        metadata={"media_id": 10},
    )
    prev_wp = svc.preview_rollback("mock-staging", med_wp_row["change_id"], actor="oscar", wp_client=wp)
    assert prev_wp["drift_detected"] is False
    rb_med_wp = svc.execute_rollback(
        "mock-staging",
        med_wp_row["change_id"],
        actor="oscar",
        expected_before_sha256=med_wp_row["before_sha256"],
        wp_client=wp,
    )
    assert rb_med_wp.ok is True
    assert rb_med_wp.data["transport"] == "wp_rest"
    assert wp.get_media(10)["alt_text"] == ""


def test_b1_05_b1_08_php_seo_hardening_and_single_target_lookup() -> None:
    seo_php = (_PLUGIN_DIR / "includes" / "class-seo.php").read_text(encoding="utf-8")
    # B1-05 invariants: wp_slash, wp_update_post before postmeta writes, Yoast indexables + page schema + adv robots
    assert "wp_slash(" in seo_php
    assert "schema_type_meta_key" in seo_php
    assert "_yoast_wpseo_schema_page_type" in seo_php
    assert "_yoast_wpseo_meta-robots-adv" in seo_php
    assert "refresh_yoast_indexable" in seo_php
    assert "do_action('wp_insert_post'" in seo_php
    assert seo_php.index("// Phase 2: Run wp_update_post BEFORE postmeta writes") < seo_php.index(
        "// Phase 3: Apply postmeta and robots writes"
    )
    # B1-08: object_id / media_id single-target params
    assert "$request->get_param('object_id')" in seo_php
    assert "$request->get_param('media_id')" in seo_php

    # Mock parity: single-target lookup finds item beyond 100 items, invalid slug rejected without mutation
    tr = InMemoryWpBridgeTransport(site_id="azom-no", base_url="https://azom.no")
    for idx in range(600, 720):
        tr.seo_entities_store[("product", idx)] = {
            **dict(tr.seo_entities_store[("product", 501)]),
            "object_id": idx,
            "slug": f"prod-{idx}",
            "meta_title": f"Prod {idx}",
        }
        tr.seo_media_store[idx] = {
            **dict(tr.seo_media_store[101]),
            "media_id": idx,
            "filename": f"img-{idx}.jpg",
            "alt_text": f"Alt {idx}",
        }
    bc = WpBridgeClient(site_id="azom-no", transport=tr)
    found_ent = bc.seo_entities(object_type="product", object_id=715, limit=1, page=1)
    assert found_ent["total"] == 1
    assert found_ent["entities"][0]["object_id"] == 715

    found_med = bc.seo_media_inventory(media_id=715, limit=1, page=1)
    assert found_med["total"] == 1
    assert found_med["media"][0]["media_id"] == 715

    # Invalid slug rejected before mutating meta_title
    orig_title = tr.seo_entities_store[("product", 501)]["meta_title"]
    with pytest.raises(SecurityError, match="Invalid slug"):
        bc.seo_update_meta(
            change_id="chg-bad-slug",
            object_type="product",
            object_id=501,
            fields={"meta_title": "Should Not Persist", "slug": "!!!"},
        )
    assert tr.seo_entities_store[("product", 501)]["meta_title"] == orig_title


def test_b1_06_s1_04_s1_12_seo_rollback_diff_draft_slug_content_and_live_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    # Draft entity with empty slug
    tr.seo_entities_store[("page", 999)] = {
        **dict(tr.seo_entities_store[("page", 2)]),
        "object_id": 999,
        "slug": "",
        "status": "draft",
        "title": "Utkast",
        "meta_title": "Ny meta titel",
    }
    bc = WpBridgeClient(site_id="mock-staging", transport=tr)
    ledger = ChangeLedger(data_dir=tmp_path)
    svc = ChangeRollbackService(data_dir=tmp_path)

    # 1. Draft with empty slug rolls back ONLY changed meta_title (never sends empty slug, never clobbers title)
    draft_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="seo_meta",
        transport="bridge",
        actor="oscar",
        summary="Uppdaterade utkast meta_title",
        target_ref="page:999",
        before_data={"title": "Gammal titel", "slug": "", "meta_title": "Gammal meta titel", "robots": "index,follow"},
        after_data={"title": "Gammal titel", "slug": "", "meta_title": "Ny meta titel", "robots": "index,follow"},
        metadata={"object_type": "page", "object_id": 999},
    )
    # Concurrent edit to title in wp-admin must NOT be clobbered by rolling back meta_title
    tr.seo_entities_store[("page", 999)]["title"] = "Redigerad parallellt"
    rb_draft = svc.execute_rollback(
        "mock-staging",
        draft_row["change_id"],
        actor="oscar",
        expected_before_sha256=draft_row["before_sha256"],
        bridge_client=bc,
    )
    assert rb_draft.ok is True
    assert tr.seo_entities_store[("page", 999)]["meta_title"] == "Gammal meta titel"
    assert tr.seo_entities_store[("page", 999)]["title"] == "Redigerad parallellt"

    # 2. Refuses non-Bridge fallback when SEO-plugin-only field changed and Bridge is unavailable
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_RESTORE_ENABLED", "1")
    monkeypatch.delenv("AZOM_WP_BRIDGE_ENABLED", raising=False)
    no_bridge_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="seo_meta",
        transport="bridge",
        actor="oscar",
        summary="SEO meta utan bridge",
        target_ref="product:501",
        before_data={"slug": "carplay-adapter-pro", "meta_title": "Före"},
        after_data={"slug": "carplay-adapter-pro", "meta_title": "Efter"},
        metadata={"object_type": "product", "object_id": 501},
    )
    # S1-12: Live drift preview notes explicitly when Bridge is inactive
    prev_live = svc.preview_rollback("mock-staging", no_bridge_row["change_id"], actor="oscar")
    assert prev_live["drift_detected"] is False
    assert "Bridge" in prev_live["drift_note"]

    rb_no_bridge = svc.execute_rollback(
        "mock-staging",
        no_bridge_row["change_id"],
        actor="oscar",
        expected_before_sha256=no_bridge_row["before_sha256"],
    )
    assert rb_no_bridge.ok is False
    assert rb_no_bridge.data["error_class"] == "RollbackFailed"

    # 3. Content / product description rollback via matching WP/Woo REST transport
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    wp = WordPressClient(base_url="https://mock.local", transport=InMemoryWpTransport())
    wp.update_page(2, content="<p>Ny SEO text</p>")
    content_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="seo_meta",
        transport="wp_rest",
        actor="oscar",
        summary="Uppdaterade sidinnehåll",
        target_ref="page:2",
        before_data={"title": "Om oss", "content": "<p>Original</p>", "slug": "om-oss"},
        after_data={"title": "Om oss", "content": "<p>Ny SEO text</p>", "slug": "om-oss"},
        metadata={"object_type": "page", "object_id": 2},
    )
    rb_content = svc.execute_rollback(
        "mock-staging",
        content_row["change_id"],
        actor="oscar",
        expected_before_sha256=content_row["before_sha256"],
        wp_client=wp,
    )
    assert rb_content.ok is True
    assert rb_content.data["transport"] == "wp_rest"
    page_after_rb = wp.get_page(2)
    raw_content = page_after_rb.raw.get("content")
    assert (raw_content.get("rendered") if isinstance(raw_content, dict) else raw_content) == "<p>Original</p>"

    woo = WooCommerceClient(base_url="https://mock.local", transport=InMemoryWooTransport())
    woo.update_product("501", {"description": "<p>Ny produkttext</p>", "short_description": "Ny kort"})
    prod_row = ledger.record_change(
        site_id="mock-staging",
        environment="staging",
        category="seo_meta",
        transport="woo_rest",
        actor="oscar",
        summary="Uppdaterade produktbeskrivning",
        target_ref="product:501",
        before_data={"name": "Azom Pro", "description": "<p>Gammal</p>", "short_description": "Gammal kort"},
        after_data={"name": "Azom Pro", "description": "<p>Ny produkttext</p>", "short_description": "Ny kort"},
        metadata={"object_type": "product", "object_id": 501},
    )
    rb_prod = svc.execute_rollback(
        "mock-staging",
        prod_row["change_id"],
        actor="oscar",
        expected_before_sha256=prod_row["before_sha256"],
        woo_client=woo,
    )
    assert rb_prod.ok is True
    assert rb_prod.data["transport"] == "woo_rest"
    assert woo.get_product("501")["description"] == "<p>Gammal</p>"
    assert woo.get_product("501")["short_description"] == "Gammal kort"
