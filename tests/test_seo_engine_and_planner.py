"""Comprehensive tests for Azom SEO Studio engine, planners, guardrails, store, and service."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient
from ecom_ops.rbac import AccessDenied
from ecom_ops.seo.content_audit import (
    audit_entities_on_page,
    audit_internal_links,
    audit_media_seo,
    build_breadcrumb_jsonld,
    build_faq_jsonld,
    build_product_jsonld,
    compute_seo_score,
    detect_keyword_cannibalization,
)
from ecom_ops.seo.models import (
    SEO_AUDIT_ID_RE,
    SEO_CAMPAIGN_ID_RE,
    SEO_FIX_ID_RE,
    SEO_LINK_PKG_ID_RE,
    SEO_STRATEGY_ID_RE,
)
from ecom_ops.seo.policy import (
    classify_anchor,
    evaluate_anchor_mix,
    is_generic_alt,
    is_generic_filename,
    recommend_natural_anchors,
    seo_kill_active,
    seo_write_allowed,
)
from ecom_ops.seo.service import SeoStudioService
from ecom_ops.seo.technical import (
    analyze_html_seo,
    analyze_robots_txt,
    analyze_sitemap_xml,
    audit_nordic_hreflang,
    fetch_public_seo_resource,
)
from ecom_ops.wp_ops.rollback_service import ChangeRollbackService


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AZOM_SEO_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. Policy gates, thresholds & Anchor-Mix Guardrails
# --------------------------------------------------------------------------- #


def test_seo_write_gates_and_kill_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("AZOM_SEO_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    assert seo_kill_active() is False
    assert seo_write_allowed() == (True, "mock")

    # Kill switch overrides even mock mode
    monkeypatch.setenv("AZOM_SEO_KILL", "1")
    assert seo_kill_active() is True
    assert seo_write_allowed() == (False, "seo_kill_switch")
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)

    monkeypatch.setenv("AZOM_WP_DEPLOY_KILL", "1")
    assert seo_kill_active() is True
    assert seo_write_allowed() == (False, "wp_deploy_kill_switch")
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    # Live mode requires explicit AZOM_SEO_WRITE_ENABLED=1
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    assert seo_write_allowed() == (False, "seo_write_disabled")

    monkeypatch.setenv("AZOM_SEO_WRITE_ENABLED", "1")
    assert seo_write_allowed() == (True, "live")

    monkeypatch.setenv("AZOM_SEO_KILL", "1")
    assert seo_write_allowed() == (False, "seo_kill_switch")


def test_filename_and_alt_heuristics() -> None:
    assert is_generic_filename("IMG_4821.jpg") is True
    assert is_generic_filename("DSC00412.PNG") is True
    assert is_generic_filename("Screenshot_2026_volvo.png") is True
    assert is_generic_filename("123456.jpg") is True
    assert is_generic_filename("azom-carplay-skarm-pro.webp") is False

    assert is_generic_alt("") is True
    assert is_generic_alt("bild") is True
    assert is_generic_alt("IMG_1234") is True
    assert is_generic_alt("Azom 9 tums trådlös Apple CarPlay-skärm monterad i Volvo") is False


def test_anchor_mix_auto_recommendation_and_guardrails() -> None:
    # Auto-generated natural anchors from target URLs + keywords -> healthy, not blocked
    raw_targets = [
        {"url": "https://azom.se/produkt/carplay-adapter-pro/", "keyword": "trådlös carplay adapter"},
        {"url": "https://azom.se/kategori/apple-carplay/", "keyword": "apple carplay bil"},
    ]
    recommended = recommend_natural_anchors(raw_targets, market="se", link_count=8)
    assert len(recommended) == 8
    rep = evaluate_anchor_mix(recommended, market="se", link_count=8)
    assert rep.guardrail_blocked is False
    assert rep.status == "healthy"
    assert rep.brand_url_generic_pct >= 60.0
    assert rep.exact_match_pct <= 10.0

    # Exact-match > 20% + low natural base -> hard blocked
    spammy_targets = [
        {"target_url": "https://azom.se/produkt/a/", "anchor_text": "trådlös carplay adapter", "keyword": "trådlös carplay adapter"},
        {"target_url": "https://azom.se/produkt/a/", "anchor_text": "trådlös carplay adapter", "keyword": "trådlös carplay adapter"},
        {"target_url": "https://azom.se/produkt/a/", "anchor_text": "trådlös carplay adapter", "keyword": "trådlös carplay adapter"},
        {"target_url": "https://azom.se/produkt/b/", "anchor_text": "Azom", "keyword": "trådlös carplay adapter"},
    ]
    blocked_rep = evaluate_anchor_mix(spammy_targets, market="se", link_count=4)
    assert blocked_rep.guardrail_blocked is True
    assert blocked_rep.status == "blocked"
    assert blocked_rep.exact_match_pct == 75.0
    assert any("Exaktmatchade" in r for r in blocked_rep.blocking_reasons)
    assert any("Överkoncentration" in w for w in blocked_rep.warnings)

    # Empty target URL -> hard blocked
    empty_rep = evaluate_anchor_mix(
        [{"target_url": "", "anchor_text": "Azom", "keyword": "carplay"}],
        market="no",
    )
    assert empty_rep.guardrail_blocked is True
    assert any("target_url" in r for r in empty_rep.blocking_reasons)

    # PBN / spam term -> hard blocked
    pbn_rep = evaluate_anchor_mix(
        [{"target_url": "https://azom.no/", "anchor_text": "Azom", "keyword": "carplay"}],
        market="no",
        notes="Buy 500 links from a private blog network PBN",
    )
    assert pbn_rep.guardrail_blocked is True
    assert any("PBN" in r for r in pbn_rep.blocking_reasons)

    # Classification checks
    assert classify_anchor("https://azom.no/produkt/x/") == "naked_url"
    assert classify_anchor("Azom", keyword="trådlös carplay") == "brand"
    assert classify_anchor("läs mer här", keyword="trådlös carplay") == "generic"
    assert classify_anchor("trådlös carplay", keyword="trådlös carplay") == "exact_match"
    assert classify_anchor("guide till trådlös carplay i Volvo", keyword="trådlös carplay") == "partial_match"


# --------------------------------------------------------------------------- #
# 2. Technical HTML, JSON-LD, Robots, Sitemap, Nordic Hreflang
# --------------------------------------------------------------------------- #


def test_html_jsonld_robots_sitemap_and_hreflang_analyzers(mock_env: Path) -> None:
    sample_html = """<!doctype html>
    <html lang="sv-SE">
    <head>
      <title>Kort</title>
      <meta name="robots" content="noindex, follow">
      <link rel="alternate" hreflang="se" href="https://azom.se/">
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Product","name":"CarPlay Pro","offers":{"@type":"Offer","price":"2490"}}
      </script>
      <script type="application/ld+json">{bad json</script>
    </head>
    <body>
      <h1>Rubrik 1</h1>
      <h1>Rubrik 2</h1>
      <img src="/uploads/IMG_1234.jpg" alt="">
      <a href="/produkt/carplay/">Klicka här</a>
    </body>
    </html>"""
    res = analyze_html_seo(sample_html, "https://azom.se/")
    codes = {i["code"] for i in res["issues"]}
    assert "title_too_short" in codes
    assert "missing_meta_description" in codes
    assert "page_noindex" in codes
    assert "missing_canonical" in codes
    assert "multiple_h1" in codes
    assert "invalid_jsonld" in codes
    assert "product_schema_incomplete" in codes
    assert "product_schema_missing_sku" in codes
    assert "product_offer_incomplete" in codes
    assert "html_images_missing_alt" in codes
    assert "html_images_missing_dimensions" in codes
    assert res["noindex"] is True

    # Robots.txt analyzer
    robots = analyze_robots_txt("User-agent: *\nDisallow: /\nDisallow: /wp-content/uploads/\n", site_url="https://azom.se")
    r_codes = {i["code"] for i in robots["issues"]}
    assert robots["blocks_entire_site"] is True
    assert "robots_blocks_all" in r_codes
    assert "robots_blocks_assets" in r_codes
    assert "robots_missing_sitemap" in r_codes

    # Sitemap XML analyzer (valid + non-HTTPS + duplicate)
    sm_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>http://azom.se/</loc></url>
      <url><loc>https://azom.se/produkt/a/</loc></url>
      <url><loc>https://azom.se/produkt/a/</loc></url>
    </urlset>"""
    sm = analyze_sitemap_xml(sm_xml, sitemap_url="https://azom.se/sitemap.xml")
    sm_codes = {i["code"] for i in sm["issues"]}
    assert sm["valid_xml"] is True
    assert "sitemap_non_https" in sm_codes
    assert "sitemap_duplicate_urls" in sm_codes
    assert "sitemap_missing_lastmod" in sm_codes

    # DTD refusal
    dtd_sm = analyze_sitemap_xml("<!DOCTYPE foo [<!ENTITY xxe 'bad'>]><urlset/>", sitemap_url="https://azom.se/s.xml")
    assert dtd_sm["valid_xml"] is False

    # Nordic hreflang reciprocity + noindex conflict + invalid code
    pages = {
        "https://azom.no/": {
            "noindex": False,
            "hreflang": [
                {"hreflang": "nb-NO", "href": "https://azom.no/"},
                {"hreflang": "se", "href": "https://azom.se/"},
                {"hreflang": "da-DK", "href": "https://azom.dk/"},
            ],
        },
        "https://azom.se/": {
            "noindex": True,
            "hreflang": [
                {"hreflang": "sv-SE", "href": "https://azom.se/"},
                {"hreflang": "nb-NO", "href": "https://azom.no/"},
            ],
        },
        "https://azom.dk/": {
            "noindex": False,
            "hreflang": [
                {"hreflang": "da-DK", "href": "https://azom.dk/"},
            ],
        },
    }
    hl = audit_nordic_hreflang(pages)
    hl_codes = {i["code"] for i in hl["issues"]}
    assert "hreflang_missing_x_default" in hl_codes
    assert "hreflang_missing_reciprocity" in hl_codes
    assert "hreflang_noindex_conflict" in hl_codes
    assert "hreflang_invalid_code" in hl_codes

    # Mock fetch wrapper never hits network
    fx = fetch_public_seo_resource("https://azom.no/", use_mock=True, market="no")
    assert fx["ok"] is True
    assert fx["provenance"] == "mock"
    assert "<title>" in fx["text"]


# --------------------------------------------------------------------------- #
# 3. Content, Media, Internal Links, Cannibalization & JSON-LD Builders
# --------------------------------------------------------------------------- #


def test_content_media_links_cannibalization_and_schema_builders() -> None:
    entities = [
        {
            "object_type": "product",
            "object_id": 501,
            "title": "CarPlay Adapter Pro",
            "slug": "carplay-adapter-pro",
            "url": "https://azom.no/produkt/carplay-adapter-pro/",
            "status": "publish",
            "word_count": 60,
            "meta_title": "Duplicate Title For Testing SEO Audits",
            "meta_description": "",
            "focus_keyphrase": "trådlös carplay adapter",
            "robots": "index, follow",
            "internal_links": [{"href": "/guide/carplay/", "anchor": "klicka här"}],
            "images_missing_alt": 1,
        },
        {
            "object_type": "post",
            "object_id": 10,
            "title": "Bästa CarPlay Adapter Guide",
            "slug": "basta-carplay-adapter-guide",
            "url": "https://azom.no/guide/carplay/",
            "status": "publish",
            "word_count": 450,
            "meta_title": "Duplicate Title For Testing SEO Audits",
            "meta_description": "En komplett köpguide för trådlösa CarPlay-adaptrar med test av uppstartstid och kompatibilitet för svenska och norska bilar.",
            "focus_keyphrase": "trådlös carplay adapter",
            "robots": "index, follow",
            "internal_links": [],
            "images_missing_alt": 0,
        },
    ]
    on_page = audit_entities_on_page(entities, categories=[{"id": 1, "name": "CarPlay", "slug": "carplay", "description": "Kort"}])
    op_codes = {i["code"] for i in on_page["issues"]}
    assert "thin_content" in op_codes
    assert "missing_meta_description" in op_codes
    assert "duplicate_meta_title" in op_codes
    assert "category_thin_description" in op_codes

    media = audit_media_seo(
        [
            {"media_id": 101, "filename": "IMG_9911.jpg", "mime_type": "image/jpeg", "alt_text": "", "width": 0, "height": 0, "filesize": 620_000, "attached_post_id": 501},
            {"media_id": 102, "filename": "azom-pro.webp", "mime_type": "image/webp", "alt_text": "bild", "width": 1200, "height": 800, "filesize": 80_000, "attached_post_id": 501},
        ]
    )
    m_codes = {i["code"] for i in media["issues"]}
    assert media["missing_alt_count"] == 1
    assert media["generic_alt_count"] == 1
    assert "media_missing_alt" in m_codes
    assert "media_generic_filename" in m_codes
    assert "media_oversized" in m_codes
    assert "media_missing_dimensions" in m_codes

    links = audit_internal_links(entities)
    assert links["orphan_count"] == 1  # product 501 has 0 inbound links
    assert len(links["generic_anchors"]) == 1
    assert len(links["link_opportunities"]) >= 1

    conflicts, c_issues = detect_keyword_cannibalization(entities)
    assert len(conflicts) >= 1
    assert conflicts[0]["match_type"] == "exact_focus_keyphrase"
    assert any(i["code"] == "keyword_cannibalization_exact" for i in c_issues)

    score, grade = compute_seo_score(on_page["issues"] + media["issues"] + c_issues)
    assert 0 <= score <= 100
    assert grade in {"A", "B", "C", "D", "F"}

    prod_ld = build_product_jsonld({"id": 501, "name": "Azom Pro", "price": "2490"}, market="se")
    assert prod_ld["@type"] == "Product"
    assert prod_ld["offers"]["priceCurrency"] == "SEK"
    faq_ld = build_faq_jsonld([{"question": "Passar den Volvo?", "answer": "Ja, alla Volvo med 12V."}])
    assert faq_ld["@type"] == "FAQPage" and len(faq_ld["mainEntity"]) == 1
    bc_ld = build_breadcrumb_jsonld([{"name": "Hem", "url": "https://azom.se/"}])
    assert bc_ld["@type"] == "BreadcrumbList" and bc_ld["itemListElement"][0]["position"] == 1


# --------------------------------------------------------------------------- #
# 4. SeoStudioService: Audit, Fix Propose/Apply, Change Vault, Rollback, RBAC
# --------------------------------------------------------------------------- #


def test_service_audit_fix_propose_apply_vault_and_rollback(
    mock_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_transport = InMemoryWpBridgeTransport(site_id="mock-shop", base_url="https://mock-shop.example.test")
    shared_bridge = WpBridgeClient(site_id="mock-shop", transport=shared_transport)
    svc = SeoStudioService(
        data_dir=mock_env,
        bridge_client_factory=lambda _site: shared_bridge,
    )

    # Full site audit (Jonatan has WP_OPS_READ / MARKETING_READ)
    audit = svc.run_audit(site_id="mock-shop", actor="jonatan", scope="site", market="se")
    assert audit["ok"] is True
    assert SEO_AUDIT_ID_RE.fullmatch(audit["audit_id"])
    assert 0 <= audit["score"] <= 100
    assert audit["grade"] in {"A", "B", "C", "D", "F"}
    assert "Google Search Console" in audit["gsc_note"]
    assert audit["on_page"]["entities_audited"] >= 5
    assert audit["media"]["total_media"] >= 4
    assert len(audit["cannibalization"]) >= 1
    assert svc.get_audit(audit["audit_id"]) is not None
    assert len(svc.list_audits(site_id="mock-shop")) == 1

    # Jonatan can propose SEO meta fix (MARKETING_SUGGEST)
    prop = svc.propose_fix(
        site_id="mock-shop",
        actor="jonatan",
        fix_type="seo_meta",
        target_type="product",
        target_id=502,
        proposed={
            "meta_title": "Azom CarPlay Mini Dongle – Trådlös Adapter | Azom",
            "meta_description": "Kompakt trådlös Apple CarPlay dongle med snabb 5 GHz anslutning och 2 års garanti. Fri frakt i hela Norden hos Azom.",
            "focus_keyphrase": "carplay mini dongle",
        },
        summary="Unik SEO-titel och beskrivning för Mini Dongle",
        market="se",
    )
    assert prop["ok"] is True
    plan = prop["plan"]
    assert SEO_FIX_ID_RE.fullmatch(plan["plan_id"])
    assert plan["executed"] is False
    assert plan["approval_required"] is True
    assert plan["status"] == "pending"
    assert svc.get_fix_plan(plan["plan_id"]) is not None

    # Jonatan CANNOT apply SEO fix (WP_OPS_ADMIN Oscar only!)
    denied = svc.apply_fix(
        site_id="mock-shop",
        plan_id=plan["plan_id"],
        actor="jonatan",
        expected_sha256=plan["plan_sha256"],
    )
    assert denied["ok"] is False
    assert denied["error_class"] == "AccessDenied"
    assert denied["executed"] is False

    # Wrong hash refused even for Oscar
    bad_hash = svc.apply_fix(
        site_id="mock-shop",
        plan_id=plan["plan_id"],
        actor="oscar",
        expected_sha256="0" * 64,
    )
    assert bad_hash["ok"] is False
    assert bad_hash["error_class"] == "PlanHashMismatch"

    # Kill switch blocks Oscar apply
    monkeypatch.setenv("AZOM_SEO_KILL", "1")
    killed = svc.apply_fix(
        site_id="mock-shop",
        plan_id=plan["plan_id"],
        actor="oscar",
        expected_sha256=plan["plan_sha256"],
    )
    assert killed["ok"] is False
    assert killed["error_class"] == "SeoKillSwitchActive"
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)

    # Live mode without AZOM_SEO_WRITE_ENABLED blocks Oscar apply
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    disabled = svc.apply_fix(
        site_id="mock-shop",
        plan_id=plan["plan_id"],
        actor="oscar",
        expected_sha256=plan["plan_sha256"],
    )
    assert disabled["ok"] is False
    assert disabled["error_class"] == "SeoWriteDisabled"
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    # Oscar applies fix -> executes on bridge + records Change Vault snapshot!
    applied = svc.apply_fix(
        site_id="mock-shop",
        plan_id=plan["plan_id"],
        actor="oscar",
        expected_sha256=plan["plan_sha256"],
    )
    assert applied["ok"] is True
    assert applied["executed"] is True
    assert applied["change_id"].startswith("chg-")
    assert shared_transport.seo_entities_store[("product", 502)]["focus_keyphrase"] == "carplay mini dongle"

    # Verify Change Vault record + rollback via ChangeRollbackService!
    chg = svc.ledger.get_change("mock-shop", applied["change_id"])
    assert chg is not None
    assert chg["category"] == "seo_meta"
    rb = ChangeRollbackService(data_dir=mock_env)
    rb_res = rb.execute_rollback(
        "mock-shop",
        applied["change_id"],
        actor="oscar",
        expected_before_sha256=chg["before_sha256"],
        bridge_client=shared_bridge,
    )
    assert rb_res.ok is True
    assert rb_res.data["executed"] is True
    assert shared_transport.seo_entities_store[("product", 502)]["focus_keyphrase"] == "trådlös carplay adapter"

    # Also test media_alt propose + apply + Change Vault category="media_alt" + rollback
    m_prop = svc.propose_fix(
        site_id="mock-shop",
        actor="jonatan",
        fix_type="media_alt",
        target_type="media",
        target_id=101,
        proposed={"alt_text": "Azom CarPlay Trådlös Adapter Pro monterad i Volvo XC60"},
        summary="Lägg till saknad alt-text på produktbild 101",
    )
    m_plan = m_prop["plan"]
    m_app = svc.apply_fix(
        site_id="mock-shop",
        plan_id=m_plan["plan_id"],
        actor="oscar",
        expected_sha256=m_plan["plan_sha256"],
    )
    assert m_app["ok"] is True
    m_chg = svc.ledger.get_change("mock-shop", m_app["change_id"])
    assert m_chg is not None and m_chg["category"] == "media_alt"
    assert shared_transport.seo_media_store[101]["alt_text"] == "Azom CarPlay Trådlös Adapter Pro monterad i Volvo XC60"
    m_rb = rb.execute_rollback(
        "mock-shop",
        m_app["change_id"],
        actor="oscar",
        expected_before_sha256=m_chg["before_sha256"],
        bridge_client=shared_bridge,
    )
    assert m_rb.ok is True
    assert shared_transport.seo_media_store[101]["alt_text"] == ""

    # Expired plan check
    exp_prop = svc.propose_fix(
        site_id="mock-shop",
        actor="jonatan",
        fix_type="seo_meta",
        target_type="page",
        target_id=2,
        proposed={"meta_title": "Om Azom – Nordisk CarPlay Specialist"},
    )
    exp_plan = exp_prop["plan"]
    exp_plan["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    svc.store.save_fix_plan(exp_plan)
    exp_res = svc.apply_fix(
        site_id="mock-shop",
        plan_id=exp_plan["plan_id"],
        actor="oscar",
        expected_sha256=exp_plan["plan_sha256"],
    )
    assert exp_res["ok"] is False
    assert exp_res["error_class"] == "PlanExpired"


# --------------------------------------------------------------------------- #
# 5. Strategy, Campaign & Link-Package Builder + State Machine + Exports
# --------------------------------------------------------------------------- #


def test_strategy_campaign_and_link_package_workflow(mock_env: Path) -> None:
    svc = SeoStudioService(data_dir=mock_env)

    # Seed an audit so strategy incorporates it
    svc.run_audit(site_id="mock-shop", actor="jonatan", scope="site", market="no")

    # Strategy
    s_res = svc.create_strategy(
        site_id="mock-shop",
        actor="jonatan",
        title="Q1 Nordisk CarPlay Tillväxtstrategi",
        market="no",
        focus_topics=["trådløs CarPlay skjerm", "CarPlay adapter Volvo"],
        goals="Öka organisk försäljning med 40% i Norge",
    )
    assert s_res["ok"] is True
    strat = s_res["strategy"]
    assert SEO_STRATEGY_ID_RE.fullmatch(strat["strategy_id"])
    assert len(strat["topic_clusters"]) >= 2
    assert set(strat["roadmap_90d"].keys()) == {"quick_wins_0_30d", "growth_30_60d", "authority_60_90d"}
    assert "Google Search Console" in strat["gsc_note"]
    assert svc.get_strategy(strat["strategy_id"]) is not None
    assert len(svc.list_strategies(site_id="mock-shop")) == 1

    # Campaign
    c_res = svc.create_campaign(
        site_id="mock-shop",
        actor="jonatan",
        name="Vårkampanj Trådlös CarPlay NO",
        market="no",
        strategy_id=strat["strategy_id"],
        target_keywords=["trådløs CarPlay skjerm", "montere CarPlay i eldre bil"],
        target_urls=["https://azom.no/produkt/carplay-skarm-pro/", "https://azom.no/guide/montera-carplay/"],
        campaign_type="seasonal",
        objective="Dominera säsongsökningar inför bilsemestern",
    )
    assert c_res["ok"] is True
    camp = c_res["campaign"]
    assert SEO_CAMPAIGN_ID_RE.fullmatch(camp["campaign_id"])
    assert camp["strategy_id"] == strat["strategy_id"]
    assert len(camp["keyword_clusters"]) == 2
    assert len(camp["url_keyword_map"]) == 2
    assert len(camp["content_briefs"]) == 2
    assert len(camp["schema_plan"]) == 2
    assert svc.get_campaign(camp["campaign_id"]) is not None
    assert len(svc.list_campaigns(site_id="mock-shop")) == 1

    # Link Package (healthy auto-designed anchor mix)
    lp_res = svc.create_link_package(
        site_id="mock-shop",
        actor="jonatan",
        name="Länkpaket Norge Q1",
        market="no",
        campaign_id=camp["campaign_id"],
        budget_sek=18000.0,
        link_count=6,
        targets=[
            {"url": "https://azom.no/produkt/carplay-skarm-pro/", "keyword": "trådløs CarPlay skjerm"},
            {"url": "https://azom.no/guide/montera-carplay/", "keyword": "montere CarPlay i bil"},
        ],
    )
    assert lp_res["ok"] is True
    pkg = lp_res["package"]
    pid = pkg["package_id"]
    assert SEO_LINK_PKG_ID_RE.fullmatch(pid)
    assert pkg["status"] == "draft"
    assert pkg["guardrail_blocked"] is False
    assert pkg["anchor_mix"]["brand_url_generic_pct"] >= 60.0
    assert len(pkg["targets"]) == 6
    assert len(pkg["drip_plan"]) >= 2
    assert "Länkpaket" in pkg["brief_markdown"]
    assert svc.get_link_package(pid) is not None
    assert len(svc.list_link_packages(site_id="mock-shop")) == 1

    # State machine: Jonatan submits draft -> ready_for_review
    sub = svc.transition_link_package(package_id=pid, actor="jonatan", new_status="ready_for_review", note="Redo för Oscar")
    assert sub["ok"] is True
    assert sub["package"]["status"] == "ready_for_review"

    # Jonatan CANNOT approve for manual order (Oscar MARKETING_MUTATE only!)
    j_app = svc.transition_link_package(package_id=pid, actor="jonatan", new_status="approved_for_manual_order")
    assert j_app["ok"] is False
    assert j_app["error_class"] == "AccessDenied"

    # Oscar approves -> ordered_external -> delivered
    o_app = svc.transition_link_package(package_id=pid, actor="oscar", new_status="approved_for_manual_order", note="Godkänd")
    assert o_app["ok"] is True
    assert o_app["package"]["status"] == "approved_for_manual_order"

    o_ord = svc.transition_link_package(package_id=pid, actor="oscar", new_status="ordered_external", note="Beställd manuellt")
    assert o_ord["ok"] is True
    assert o_ord["package"]["status"] == "ordered_external"

    o_del = svc.transition_link_package(package_id=pid, actor="oscar", new_status="delivered", note="Alla 6 länkar live")
    assert o_del["ok"] is True
    assert o_del["package"]["status"] == "delivered"

    # Terminal state cannot transition further
    term = svc.transition_link_package(package_id=pid, actor="oscar", new_status="cancelled")
    assert term["ok"] is False
    assert term["error_class"] == "InvalidTransition"

    # Exports: Markdown, JSON, CSV
    md_fn, md_ct, md_body = svc.export_link_package(pid, fmt="md")
    assert md_fn == f"{pid}.md" and "text/markdown" in md_ct and "Anti-PBN" in md_body

    js_fn, js_ct, js_body = svc.export_link_package(pid, fmt="json")
    assert js_fn == f"{pid}.json" and "application/json" in js_ct
    parsed_js = json.loads(js_body)
    assert parsed_js["package_id"] == pid and len(parsed_js["targets"]) == 6

    csv_fn, csv_ct, csv_body = svc.export_link_package(pid, fmt="csv")
    assert csv_fn == f"{pid}.csv" and "text/csv" in csv_ct
    csv_rows = list(csv.DictReader(io.StringIO(csv_body)))
    assert len(csv_rows) == 6
    assert csv_rows[0]["package_id"] == pid

    # Guardrail-blocked link package CANNOT be submitted or approved
    bad_lp = svc.create_link_package(
        site_id="mock-shop",
        actor="jonatan",
        name="Spammy Package",
        market="se",
        budget_sek=5000.0,
        link_count=4,
        targets=[
            {"url": "https://azom.se/produkt/a/", "keyword": "köp carplay", "anchor_text": "köp carplay"},
            {"url": "https://azom.se/produkt/a/", "keyword": "köp carplay", "anchor_text": "köp carplay"},
            {"url": "https://azom.se/produkt/a/", "keyword": "köp carplay", "anchor_text": "köp carplay"},
            {"url": "https://azom.se/produkt/a/", "keyword": "köp carplay", "anchor_text": "köp carplay"},
        ],
    )
    bad_pkg = bad_lp["package"]
    assert bad_pkg["guardrail_blocked"] is True
    blocked_sub = svc.transition_link_package(
        package_id=bad_pkg["package_id"],
        actor="jonatan",
        new_status="ready_for_review",
    )
    assert blocked_sub["ok"] is False
    assert blocked_sub["error_class"] == "GuardrailBlocked"

    # Unknown actor denied
    with pytest.raises(AccessDenied):
        svc.run_audit(site_id="mock-shop", actor="unknown-hacker")


# --------------------------------------------------------------------------- #
# 6. Stage 2 Regression Tests (U1-01/02/03/07, S1-01..S1-13)
# --------------------------------------------------------------------------- #


def test_u1_01_research_authorize_seo_target_refs() -> None:
    from ecom_ops.research.models import SearchPolicyError
    from ecom_ops.research.store import authorize_target_ref

    # Valid site and audit refs pass for jonatan & oscar (MARKETING_READ / WP_OPS_READ)
    authorize_target_ref("jonatan", "seo:mock-staging")
    authorize_target_ref("oscar", "seo:audit:seoa-0123456789ab")
    authorize_target_ref("agent", "seo:mock-shop")

    # Malformed seo: target refs fail closed with SearchPolicyError
    for bad_ref in (
        "seo:",
        "seo:UPPER-SITE",
        "seo:audit:not-valid",
        "seo:audit:seoa-123",
        "seo:site with spaces",
        "seo:../etc",
    ):
        with pytest.raises(SearchPolicyError):
            authorize_target_ref("jonatan", bad_ref)

    with pytest.raises(AccessDenied):
        authorize_target_ref("unknown-hacker", "seo:mock-staging")


def test_u1_02_u1_03_s1_05_link_package_bounds_and_export_sanitization(mock_env: Path) -> None:
    from ecom_ops.seo.planner import _csv_safe_cell, build_link_package, render_link_package_brief

    svc = SeoStudioService(data_dir=mock_env)

    # U1-02: reject non-finite and out-of-range budget_sek and link_count
    for bad_budget in (float("inf"), float("-inf"), float("nan"), 999.0, 500_001.0, -500.0, True):
        with pytest.raises(ValueError):
            build_link_package(site_id="mock-shop", actor="jonatan", name="Bad", budget_sek=bad_budget)
        with pytest.raises(ValueError):
            svc.create_link_package(site_id="mock-shop", actor="jonatan", name="Bad", budget_sek=bad_budget)

    for bad_count in (0, -3, 51, 100, True):
        with pytest.raises(ValueError):
            build_link_package(site_id="mock-shop", actor="jonatan", name="Bad", link_count=bad_count)
        with pytest.raises(ValueError):
            svc.create_link_package(site_id="mock-shop", actor="jonatan", name="Bad", link_count=bad_count)

    # U1-03 + S1-05: CSV formula injection + Markdown table injection + budget=None guard
    assert _csv_safe_cell("=SUM(1+1)") == "'=SUM(1+1)"
    assert _csv_safe_cell("  =HYPERLINK(\"https://evil.test\",\"click\")") == "'  =HYPERLINK(\"https://evil.test\",\"click\")"
    assert _csv_safe_cell("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
    assert _csv_safe_cell("-1+1") == "'-1+1"
    assert _csv_safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert _csv_safe_cell("\t=1+1") == "'\t=1+1"
    assert _csv_safe_cell("\r=1+1") == "'\r=1+1"
    assert _csv_safe_cell("Azom CarPlay") == "Azom CarPlay"

    pkg = build_link_package(
        site_id="mock-shop",
        actor="jonatan",
        name="Export Security Test",
        market="se",
        budget_sek=12000.0,
        link_count=2,
        targets=[
            {
                "url": "https://azom.se/",
                "keyword": "=SUM(1+1)",
                "anchor_text": '=HYPERLINK("https://evil.test","click")',
                "anchor_type": "brand",
            },
            {
                "url": "https://azom.se/produkt/pro/",
                "keyword": "carplay | volvo",
                "anchor_text": "CarPlay Pro | `Azom`\nRad 2",
                "anchor_type": "brand",
            },
        ],
    )
    _fn, _ct, csv_body = render_link_package_brief(pkg, fmt="csv")
    rows = list(csv.DictReader(io.StringIO(csv_body)))
    assert rows[0]["keyword"].startswith("'=")
    assert rows[0]["anchor_text"].startswith("'=")

    # Markdown table cell escaping + budget_sek=None guard
    pkg["budget_sek"] = None
    _mfn, _mct, md_body = render_link_package_brief(pkg, fmt="md")
    assert "Budget:** 0 SEK" in md_body
    assert r"CarPlay Pro \| \`Azom\` Rad 2" in md_body
    assert "footpring" not in md_body
    assert "footprint-nätverk" in md_body
    assert "länkleverantörer" in pkg["no_auto_purchase_notice"]


def test_u1_07_and_s1_13_campaign_types_dedup_and_localization(mock_env: Path) -> None:
    from ecom_ops.seo.models import VALID_CAMPAIGN_TYPES
    from ecom_ops.seo.planner import build_campaign

    assert VALID_CAMPAIGN_TYPES == (
        "commercial",
        "seasonal",
        "product_launch",
        "content_hub",
        "technical_sprint",
        "category",
        "informational",
        "technical",
    )
    for ctype in VALID_CAMPAIGN_TYPES:
        camp = build_campaign(
            site_id="mock-shop",
            actor="jonatan",
            name=f"Camp {ctype}",
            market="no",
            campaign_type=ctype,
        )
        assert camp["campaign_type"] == ctype

    with pytest.raises(ValueError, match="campaign_type"):
        build_campaign(
            site_id="mock-shop",
            actor="jonatan",
            name="Bad",
            campaign_type="nonexistent_type",
        )

    # S1-13: no duplicate URL briefs when len(kws) > len(urls) & NO/DK localization
    no_camp = build_campaign(
        site_id="mock-shop",
        actor="jonatan",
        name="NO Kampanj",
        market="no",
        target_keywords=["kw1", "kw2", "kw3", "kw4"],
        target_urls=["https://azom.no/produkt/a/", "https://azom.no/guide/b/"],
    )
    mapped_urls = [r["url"] for r in no_camp["url_keyword_map"]]
    brief_urls = [b["url"] for b in no_camp["content_briefs"]]
    assert len(mapped_urls) == len(set(mapped_urls)) == 2
    assert len(brief_urls) == len(set(brief_urls)) == 2
    assert "kw3" in no_camp["url_keyword_map"][0]["secondary_keywords"]
    assert "bästa" not in json.dumps(no_camp["url_keyword_map"], ensure_ascii=False).lower()
    assert "upptäck" not in json.dumps(no_camp["url_keyword_map"], ensure_ascii=False).lower()
    assert "Oppdag" in no_camp["url_keyword_map"][0]["recommended_meta_description"]

    dk_camp = build_campaign(site_id="mock-shop", actor="jonatan", name="DK", market="dk")
    assert "Opdag" in dk_camp["url_keyword_map"][0]["recommended_meta_description"]
    assert "bedste" in " ".join(dk_camp["url_keyword_map"][0]["secondary_keywords"])


def test_s1_01_unset_azom_use_mock_passes_explicit_use_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecom_ops.integrations import ga4 as ga4_mod
    from ecom_ops.integrations import google_ads as ads_mod
    from ecom_ops.integrations import woocommerce as woo_mod
    from ecom_ops.integrations import wordpress as wp_mod
    from ecom_ops.seo.planner import _collect_marketing_and_research_signals

    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    seen: dict[str, bool | None] = {}

    orig_wp = wp_mod.wp_client_from_env
    orig_woo = woo_mod.client_from_env
    orig_ga4 = ga4_mod.client_from_env
    orig_ads = ads_mod.client_from_env

    def spy_wp(*args, **kwargs):
        seen["wp"] = kwargs.get("use_mock")
        return orig_wp(*args, **kwargs)

    def spy_woo(*args, **kwargs):
        seen["woo"] = kwargs.get("use_mock")
        return orig_woo(*args, **kwargs)

    def spy_ga4(*args, **kwargs):
        seen["ga4"] = kwargs.get("use_mock")
        return orig_ga4(*args, **kwargs)

    def spy_ads(*args, **kwargs):
        seen["ads"] = kwargs.get("use_mock")
        return orig_ads(*args, **kwargs)

    monkeypatch.setattr(wp_mod, "wp_client_from_env", spy_wp)
    monkeypatch.setattr(woo_mod, "client_from_env", spy_woo)
    monkeypatch.setattr(ga4_mod, "client_from_env", spy_ga4)
    monkeypatch.setattr(ads_mod, "client_from_env", spy_ads)

    svc = SeoStudioService(data_dir=tmp_path)
    site = svc._resolve_site("mock-shop", market="se")
    svc._get_wp_client(site, market="se")
    svc._get_woo_client(site, market="se")
    _collect_marketing_and_research_signals(actor="jonatan", data_dir=tmp_path)

    assert seen == {"wp": True, "woo": True, "ga4": True, "ads": True}


def test_s1_02_propose_validation_and_apply_fail_closed_and_mixed_transports(
    mock_env: Path,
) -> None:
    from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient
    from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient

    shared_tr = InMemoryWpBridgeTransport(site_id="mock-shop", base_url="https://mock-shop.example.test")
    shared_bridge = WpBridgeClient(site_id="mock-shop", transport=shared_tr)
    woo = WooCommerceClient(base_url="https://mock-shop.example.test", transport=InMemoryWooTransport())
    wp = WordPressClient(base_url="https://mock-shop.example.test", transport=InMemoryWpTransport())

    svc = SeoStudioService(
        data_dir=mock_env,
        bridge_client_factory=lambda _s: shared_bridge,
        woo_client_factory=lambda **_kw: woo,
        wp_client_factory=lambda **_kw: wp,
    )

    # Non-numeric target_id rejected at propose time
    with pytest.raises(ValueError, match="positive integer"):
        svc.propose_fix(
            site_id="mock-shop",
            actor="jonatan",
            fix_type="media_alt",
            target_type="media",
            target_id="not-a-number",
            proposed={"alt_text": "Valid alt"},
        )

    # Disallowed proposed keys rejected at propose time
    with pytest.raises(ValueError, match="Unsupported proposed field"):
        svc.propose_fix(
            site_id="mock-shop",
            actor="jonatan",
            fix_type="seo_meta",
            target_type="product",
            target_id=501,
            proposed={"evil_field": "drop table"},
        )

    # Mixed meta + description on product writes BOTH bridge and Woo REST & captures before description
    mixed_prop = svc.propose_fix(
        site_id="mock-shop",
        actor="jonatan",
        fix_type="product_seo",
        target_type="product",
        target_id=501,
        proposed={
            "meta_title": "Azom CarPlay Pro | Bästa Trådlösa Adaptern",
            "description": "<p>Uppdaterad beskrivning för CarPlay Pro.</p>",
        },
        summary="Blandad meta + produktbeskrivning",
    )
    mixed_plan = mixed_prop["plan"]
    assert "description" in mixed_plan["before"]
    mixed_apply = svc.apply_fix(
        site_id="mock-shop",
        plan_id=mixed_plan["plan_id"],
        actor="oscar",
        expected_sha256=mixed_plan["plan_sha256"],
    )
    assert mixed_apply["ok"] is True
    assert mixed_apply["transport"] == "bridge+woo_rest"
    assert shared_tr.seo_entities_store[("product", 501)]["meta_title"] == "Azom CarPlay Pro | Bästa Trådlösa Adaptern"
    assert "Uppdaterad beskrivning" in woo.get_product(501)["description"]

    # Mixed meta + content on page writes BOTH bridge and WP REST
    page_prop = svc.propose_fix(
        site_id="mock-shop",
        actor="jonatan",
        fix_type="page_seo",
        target_type="page",
        target_id=2,
        proposed={
            "meta_title": "Om Azom – Trådlös CarPlay Expert",
            "content": "Ny sidtext om Azom Norden.",
        },
    )
    page_plan = page_prop["plan"]
    assert "content" in page_plan["before"]
    page_apply = svc.apply_fix(
        site_id="mock-shop",
        plan_id=page_plan["plan_id"],
        actor="oscar",
        expected_sha256=page_plan["plan_sha256"],
    )
    assert page_apply["ok"] is True
    assert page_apply["transport"] == "bridge+wp_rest"
    assert shared_tr.seo_entities_store[("page", 2)]["meta_title"] == "Om Azom – Trådlös CarPlay Expert"

    # Bridge=None + meta_title on product MUST FAIL CLOSED (never fake executed=True!)
    no_bridge_svc = SeoStudioService(
        data_dir=mock_env,
        bridge_client_factory=lambda _s: None,
        woo_client_factory=lambda **_kw: woo,
        wp_client_factory=lambda **_kw: None,
    )
    nb_prop = no_bridge_svc.propose_fix(
        site_id="mock-shop",
        actor="jonatan",
        fix_type="seo_meta",
        target_type="product",
        target_id=501,
        proposed={"meta_title": "Should not silently succeed"},
    )
    changes_before = len(no_bridge_svc.ledger.list_changes("mock-shop"))
    nb_apply = no_bridge_svc.apply_fix(
        site_id="mock-shop",
        plan_id=nb_prop["plan"]["plan_id"],
        actor="oscar",
        expected_sha256=nb_prop["plan"]["plan_sha256"],
    )
    assert nb_apply["ok"] is False
    assert nb_apply["executed"] is False
    assert nb_apply["error_class"] == "NoTransportAvailable"
    assert len(no_bridge_svc.ledger.list_changes("mock-shop")) == changes_before
    assert no_bridge_svc.get_fix_plan(nb_prop["plan"]["plan_id"])["status"] == "pending"

    # category target_type fails closed on apply_fix (never fake executed=True)
    cat_prop = svc.propose_fix(
        site_id="mock-shop",
        actor="jonatan",
        fix_type="seo_meta",
        target_type="category",
        target_id="12",
        proposed={"meta_title": "Kategori CarPlay | Azom"},
    )
    cat_apply = svc.apply_fix(
        site_id="mock-shop",
        plan_id=cat_prop["plan"]["plan_id"],
        actor="oscar",
        expected_sha256=cat_prop["plan"]["plan_sha256"],
    )
    assert cat_apply["ok"] is False
    assert cat_apply["executed"] is False
    assert cat_apply["error_class"] == "UnsupportedFields"


def _make_test_site(site_id: str = "live-shop", url: str = "https://azom.se") -> object:
    from ecom_ops.wp_ops.models import Site

    return Site(
        id=site_id,
        display_name=site_id,
        canonical_url=url,
        environment="production",
        business_owner="customer",
        hosting_provider="host",
        wordpress_version="6.7",
        php_version="8.3",
        woocommerce=True,
        critical_urls=(f"{url.rstrip('/')}/",),
        maintenance_window="night",
        secret_ref="WP_SECRET_LIVE",
        capabilities=(
            ("hosting_api", "unknown"),
            ("wp_cli", "unknown"),
            ("staging", "unknown"),
            ("backup", "unknown"),
            ("restore", "unknown"),
            ("uptime", "unknown"),
            ("security_scanner", "unknown"),
        ),
        alert_confirmations=2,
        check_interval_seconds=300,
        timeout_seconds=10.0,
        dedupe_window_seconds=30,
        retention_days=30,
        alert_owner="oscar",
        files_root="/var/www/live",
        ssh_host="azom.se",
    )


def test_s1_03_live_mode_no_mock_leak_and_unknown_site_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecom_ops.seo.service import TargetNotFound

    # T1-04: custom site_id in mock mode builds a complete valid Site (no TypeError)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    mock_svc = SeoStudioService(data_dir=tmp_path)
    custom_site = mock_svc._resolve_site("custom-mock-site", market="no")
    assert custom_site.id == "custom-mock-site"
    assert custom_site.ssh_host == "custom-mock-site.example.test"

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    # Unknown site_id fails closed in live mode
    svc = SeoStudioService(
        data_dir=tmp_path,
        bridge_client_factory=lambda _s: None,
        wp_client_factory=lambda **_kw: None,
        woo_client_factory=lambda **_kw: None,
    )
    with pytest.raises(ValueError, match="Unknown site_id"):
        svc._resolve_site("unregistered-ghost-site", market="se")

    # Known live site with missing target raises TargetNotFound (never leaks mock ID 501/302!)
    live_site = _make_test_site("live-shop", "https://azom.se")
    with pytest.raises(TargetNotFound):
        svc._fetch_current_target_state(
            live_site, fix_type="seo_meta", target_type="product", target_id="501", market="se"
        )
    with pytest.raises(TargetNotFound):
        svc._fetch_current_target_state(
            live_site, fix_type="media_alt", target_type="media", target_id="302", market="se"
        )

    # Paginated bridge lookup finds entity beyond item #100 (page 2)
    class _PaginatedBridge:
        def seo_entities(self, *, object_type="all", limit=100, page=1):
            if page == 1:
                return {
                    "ok": True,
                    "seo_plugin": "yoast",
                    "total": 105,
                    "entities": [{"object_type": "product", "object_id": i} for i in range(1, 101)],
                }
            return {
                "ok": True,
                "seo_plugin": "yoast",
                "total": 105,
                "entities": [
                    {
                        "object_type": "product",
                        "object_id": 999,
                        "title": "Page 2 Product",
                        "slug": "page-2-product",
                        "meta_title": "Page 2 Meta",
                    }
                ],
            }

    pag_svc = SeoStudioService(
        data_dir=tmp_path,
        bridge_client_factory=lambda _s: _PaginatedBridge(),
        wp_client_factory=lambda **_kw: None,
        woo_client_factory=lambda **_kw: None,
    )
    before, bsha = pag_svc._fetch_current_target_state(
        live_site, fix_type="seo_meta", target_type="product", target_id="999", market="se"
    )
    assert before["object_id"] == 999
    assert before["meta_title"] == "Page 2 Meta"
    assert len(bsha) == 64


def test_s1_06_strict_expires_at_and_atomic_cas_and_s1_13_store_corrupt_json(mock_env: Path) -> None:
    import sqlite3

    shared_tr = InMemoryWpBridgeTransport(site_id="mock-shop", base_url="https://mock-shop.example.test")
    shared_bridge = WpBridgeClient(site_id="mock-shop", transport=shared_tr)
    svc = SeoStudioService(data_dir=mock_env, bridge_client_factory=lambda _s: shared_bridge)

    for bad_exp in ("", "not-a-date", "2099-01-01T00:00:00"):  # missing, malformed, naive
        prop = svc.propose_fix(
            site_id="mock-shop",
            actor="jonatan",
            fix_type="seo_meta",
            target_type="product",
            target_id=501,
            proposed={"meta_title": "Valid Title For Expiry Test | Azom"},
        )
        plan = prop["plan"]
        plan["expires_at"] = bad_exp
        svc.store.save_fix_plan(plan)
        res = svc.apply_fix(
            site_id="mock-shop",
            plan_id=plan["plan_id"],
            actor="oscar",
            expected_sha256=plan["plan_sha256"],
        )
        assert res["ok"] is False
        assert res["error_class"] == "InvalidPlan"

    # Atomic SQLite CAS prevents double-transition on link package
    lp = svc.create_link_package(site_id="mock-shop", actor="jonatan", name="CAS Test", link_count=4)["package"]
    pid = lp["package_id"]
    lp_copy = dict(lp, status="ready_for_review")
    assert svc.store.compare_and_swap_link_package(pid, expected_status="draft", new_pkg=lp_copy) is True
    assert svc.store.compare_and_swap_link_package(pid, expected_status="draft", new_pkg=lp_copy) is False

    # Corrupt JSON in SQLite returns None (not {})
    with sqlite3.connect(str(svc.store.db_path)) as conn:
        conn.execute(
            "UPDATE seo_link_packages SET payload_json = '{corrupt' WHERE package_id = ?",
            (pid,),
        )
        conn.commit()
    assert svc.store.get_link_package(pid) is None


def test_s1_07_live_audit_fetch_failures_sitemap_fallback_and_unmatched_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ecom_ops.seo.service as seo_svc_mod

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    live_site = _make_test_site("live-shop", "https://azom.se")

    fetched_urls: list[str] = []

    def fake_fetch(url: str, **_kw):
        fetched_urls.append(url)
        if url.endswith("robots.txt"):
            return {"ok": False, "status": 404, "url": url, "final_url": url, "headers": {}, "text": "<!doctype html><html>404</html>", "provenance": "live", "message": "HTTP 404"}
        if url.endswith("sitemap_index.xml"):
            return {"ok": False, "status": 404, "url": url, "final_url": url, "headers": {}, "text": "<!doctype html><html>404</html>", "provenance": "live", "message": "HTTP 404"}
        if url.endswith("wp-sitemap.xml"):
            return {
                "ok": True,
                "status": 200,
                "url": url,
                "final_url": url,
                "headers": {"content-type": "application/xml"},
                "text": '<?xml version="1.0"?><urlset><url><loc>https://azom.se/</loc><lastmod>2026-11-20</lastmod></url></urlset>',
                "provenance": "live",
            }
        return {"ok": False, "status": 500, "url": url, "final_url": url, "headers": {}, "text": "Server Error", "provenance": "live", "message": "HTTP 500"}

    monkeypatch.setattr(seo_svc_mod, "fetch_public_seo_resource", fake_fetch)

    shared_tr = InMemoryWpBridgeTransport(site_id="live-shop", base_url="https://azom.se")
    shared_bridge = WpBridgeClient(site_id="live-shop", transport=shared_tr)
    svc = SeoStudioService(data_dir=tmp_path, bridge_client_factory=lambda _s: shared_bridge)
    monkeypatch.setattr(svc, "_resolve_site", lambda _sid, **_kw: live_site)

    # HTTP 500 emits critical technical_fetch_failed; 404 robots doesn't parse HTML; fallback wp-sitemap.xml succeeds!
    rep = svc.run_audit(site_id="live-shop", actor="jonatan", scope="technical", market="se")
    codes = {i["code"] for i in rep["issues"]}
    assert "technical_fetch_failed" in codes
    assert any(i["code"] == "technical_fetch_failed" and i["severity"] == "critical" for i in rep["issues"])
    assert "robots_fetch_failed" in codes
    assert "sitemap_dtd_refused" not in codes
    assert rep["technical"]["sitemap"]["valid_xml"] is True
    assert rep["technical"]["sitemap"]["sitemap_url"].endswith("wp-sitemap.xml")

    # Unmatched target_id returns explicit target_not_found instead of auditing all entities
    miss = svc.run_audit(site_id="live-shop", actor="jonatan", scope="product", target_id="999999", market="se")
    assert miss["summary"].get("target_not_found") is True
    assert miss["on_page"]["entities_audited"] == 0
    assert any(i["code"] == "target_not_found" for i in miss["issues"])


def test_s1_08_u1_05_draft_filtering_non_image_svg_and_missing_counts() -> None:
    entities = [
        {
            "object_type": "product",
            "object_id": 1,
            "title": "Publicerad Produkt",
            "slug": "pub-produkt",
            "url": "https://azom.se/produkt/pub-produkt/",
            "status": "publish",
            "word_count": 200,
            "meta_title": "Publicerad Produkt – Trådlös CarPlay | Azom",
            "meta_description": "",
            "focus_keyphrase": "",
            "internal_links": [
                {"href": "/produkt/pub-produkt/?utm_source=test#top", "anchor": "self"},
                {"href": "https://external.example/produkt/pub-produkt/", "anchor": "ext"},
            ],
        },
        {
            "object_type": "product",
            "object_id": 2,
            "title": "Utkast Kopia",
            "slug": "utkast-kopia",
            "url": "https://azom.se/produkt/utkast-kopia/",
            "status": "draft",
            "word_count": 10,
            "meta_title": "Publicerad Produkt – Trådlös CarPlay | Azom",
            "meta_description": "",
            "focus_keyphrase": "carplay",
            "internal_links": [],
        },
    ]
    op = audit_entities_on_page(entities)
    assert op["entities_audited"] == 1
    assert op["non_published_count"] == 1
    assert op["missing_meta_description"] == 1
    assert op["missing_focus_keyphrase"] == 1
    assert op["duplicate_titles_count"] == 0

    links = audit_internal_links(entities, base_url="https://azom.se")
    assert links["total_nodes"] == 1
    assert links["total_internal_links"] == 0

    conflicts, _ = detect_keyword_cannibalization(entities)
    assert conflicts == []

    # Media audit skips PDF/video and exempts SVG from WebP & dimension checks
    media = audit_media_seo(
        [
            {"media_id": 1, "filename": "manual.pdf", "mime_type": "application/pdf", "alt_text": "", "width": 0, "height": 0, "filesize": 900_000},
            {"media_id": 2, "filename": "demo.mp4", "mime_type": "video/mp4", "alt_text": "", "width": 0, "height": 0, "filesize": 2_000_000},
            {"media_id": 3, "filename": "logo.svg", "mime_type": "image/svg+xml", "alt_text": "Azom varumärkeslogotyp", "width": 0, "height": 0, "filesize": 4_000},
        ]
    )
    assert media["total_media"] == 1
    assert media["non_image_skipped"] == 2
    assert media["missing_dimensions_count"] == 0
    assert media["non_webp_count"] == 0
    assert media["issues"] == []


def test_s1_09_and_s1_13_technical_html_robots_sitemap_hreflang_and_charset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecom_ops.seo.technical as tech_mod

    # SVG <title> doesn't pollute page <title> or word_count; heading chunks join with spaces;
    # anchor text prefers visible text over title attr; lazy data-src replaces data: placeholder
    html_doc = """<!doctype html>
    <html lang="nb-NO">
    <head>
      <title>Azom Trådløs CarPlay Skjerm til Bil | Norge</title>
      <meta name="description" content="Oppgrader bilen med trådløs Apple CarPlay og Android Auto. Enkel montering på 5 minutter, 2 års garanti og rask levering i Norge.">
      <link rel="canonical" href="https://azom.no/">
    </head>
    <body>
      <svg><title>Shopping Cart Icon</title><text>HiddenSvgWord</text></svg>
      <h1><span>Apple</span><br><span>CarPlay</span></h1>
      <img src="data:image/svg+xml;base64,PHN2Zz4=" data-src="/uploads/carplay-hero.webp" alt="CarPlay skjerm i bil" width="800" height="600">
      <a href="/guide/" title="Läs mer">Läs mer</a>
    </body>
    </html>"""
    res = analyze_html_seo(html_doc, "https://azom.no/")
    assert res["title"] == "Azom Trådløs CarPlay Skjerm til Bil | Norge"
    assert res["headings"]["h1"] == ["Apple CarPlay"]
    assert res["images"][0]["src"] == "/uploads/carplay-hero.webp"
    assert res["images"][0]["filename"] == "carplay-hero.webp"
    assert res["internal_links"][0]["anchor"] == "Läs mer"
    assert res["internal_links"][0]["generic_anchor"] is True

    # Robots.txt consecutive User-agent accumulation + Googlebot + exact product matching
    r_accum = analyze_robots_txt("User-agent: *\nUser-agent: Googlebot\nDisallow: /\n")
    assert r_accum["blocks_entire_site"] is True

    r_gbot = analyze_robots_txt("User-agent: Googlebot\nDisallow: /product/\nSitemap: https://azom.no/s.xml\n")
    assert r_gbot["blocks_products"] is True

    r_safe_woo = analyze_robots_txt(
        "User-agent: *\nDisallow: /product-tag/\nDisallow: /shop/?add-to-cart=\nSitemap: https://azom.no/s.xml\n"
    )
    assert r_safe_woo["blocks_products"] is False

    # Sitemap BOM, non-sitemap XML rejection, empty <urlset/>, case-insensitive HTTP://
    bom_sm = analyze_sitemap_xml('\ufeff<?xml version="1.0"?><urlset><url><loc>HTTP://azom.se/</loc><lastmod>2026-01-01</lastmod></url></urlset>')
    assert bom_sm["valid_xml"] is True
    assert any(i["code"] == "sitemap_non_https" for i in bom_sm["issues"])

    rss_sm = analyze_sitemap_xml('<?xml version="1.0"?><rss><channel/></rss>')
    assert rss_sm["valid_xml"] is False
    assert rss_sm["kind"] == "invalid"

    empty_sm = analyze_sitemap_xml('<?xml version="1.0"?><urlset/>')
    assert any(i["code"] == "sitemap_empty" for i in empty_sm["issues"])

    # Hreflang URL percent-decoding, lowercase host, and nn-NO coverage
    hl = audit_nordic_hreflang(
        {
            "https://AZOM.SE/tr%C3%A5dl%C3%B6s/": {
                "noindex": False,
                "hreflang": [
                    {"hreflang": "sv-SE", "href": "https://azom.se/trådlös/"},
                    {"hreflang": "nn-NO", "href": "https://azom.no/tr%C3%A5dl%C3%B8s/"},
                    {"hreflang": "x-default", "href": "https://azom.se/trådlös/"},
                ],
            },
            "https://azom.no/trådløs/": {
                "noindex": False,
                "hreflang": [
                    {"hreflang": "nn-NO", "href": "https://azom.no/trådløs/"},
                    {"hreflang": "sv-SE", "href": "https://azom.se/tr%C3%A5dl%C3%B6s/"},
                    {"hreflang": "x-default", "href": "https://azom.se/trådlös/"},
                ],
            },
        }
    )
    assert hl["missing_self_ref"] == []
    assert hl["missing_return_links"] == []
    assert hl["reciprocal_pairs_count"] == 1
    assert "no" in hl["markets_covered"]
    assert "se" in hl["markets_covered"]

    # fetch_public_seo_resource: 3xx -> ok=False and charset= decode
    monkeypatch.setattr(
        tech_mod,
        "pinned_https_request",
        lambda *_a, **_kw: (302, {"content-type": "text/html"}, b"", "https://azom.se/"),
    )
    r302 = fetch_public_seo_resource("https://azom.se/", use_mock=False)
    assert r302["ok"] is False

    monkeypatch.setattr(
        tech_mod,
        "pinned_https_request",
        lambda *_a, **_kw: (
            200,
            {"content-type": "text/html; charset=iso-8859-1"},
            "Trådlös".encode("iso-8859-1"),
            "https://azom.se/",
        ),
    )
    r_latin = fetch_public_seo_resource("https://azom.se/", use_mock=False)
    assert r_latin["ok"] is True
    assert r_latin["text"] == "Trådlös"


def test_s1_10_s1_11_s1_13_anchor_mix_drip_jsonld_and_export_rbac(mock_env: Path) -> None:
    from ecom_ops.seo.planner import build_link_package

    # Default 6-link package passes HEALTHY (no partial_match > 25% warning!)
    pkg6 = build_link_package(site_id="mock-shop", actor="jonatan", name="Clean 6 Pack", market="no", link_count=6)
    assert pkg6["anchor_mix"]["status"] == "healthy"
    assert pkg6["anchor_mix"]["partial_match_pct"] <= 25.0
    assert pkg6["targets"][0]["target_week"] == 1

    # Single deep URL balanced with homepage so share <= 50%
    rec_single = recommend_natural_anchors(
        [{"url": "https://azom.no/produkt/carplay-pro/", "keyword": "trådløs carplay"}],
        market="no",
        link_count=6,
    )
    rep_single = evaluate_anchor_mix(rec_single, market="no", link_count=6)
    assert rep_single.status == "healthy"
    assert rep_single.max_single_url_share_pct <= 50.0

    # Drip schedule starts Week 1 even for 1-link and 2-link packages
    pkg1 = build_link_package(site_id="mock-shop", actor="jonatan", name="One Link", link_count=1)
    assert pkg1["targets"][0]["target_week"] == 1
    pkg2 = build_link_package(site_id="mock-shop", actor="jonatan", name="Two Links", link_count=2)
    assert pkg2["targets"][0]["target_week"] == 1

    # Normalized url_counter catches trailing-slash/query bypasses
    bypass_rep = evaluate_anchor_mix(
        [
            {"target_url": "https://azom.se/produkt/a", "anchor_text": "Azom", "keyword": "carplay"},
            {"target_url": "https://azom.se/produkt/a/", "anchor_text": "Azom Nordic", "keyword": "carplay"},
            {"target_url": "https://AZOM.SE/produkt/a/?ref=1", "anchor_text": "läs mer", "keyword": "carplay"},
            {"target_url": "https://azom.se/", "anchor_text": "https://azom.se/", "keyword": "carplay"},
        ],
        market="se",
        link_count=4,
    )
    assert bypass_rep.max_single_url_share_pct == 75.0
    assert any("Överkoncentration" in w for w in bypass_rep.warnings)

    # Spam/PBN detection on package name, hyphen/underscore URLs, and Nordic spam terms
    for spam_case in ("cheap-bulk-links", "pbn_network", "snabblån", "forbrukslån", "kasino", "lenkefarm", "betting"):
        srep = evaluate_anchor_mix(
            [{"target_url": "https://azom.se/", "anchor_text": "Azom", "keyword": "carplay"}],
            market="se",
            name=f"Kampanj {spam_case}",
        )
        assert srep.guardrail_blocked is True, spam_case

    # classify_anchor honors custom brand_terms
    assert classify_anchor("NordicAuto", brand_terms=frozenset({"nordicauto"})) == "brand"
    assert classify_anchor("Azom", brand_terms=frozenset({"nordicauto"})) != "brand"

    # safe_pct uses raw count sum (1+1+1/3 == 100.0, not 99.9)
    three_safe = evaluate_anchor_mix(
        [
            {"target_url": "https://azom.se/", "anchor_text": "Azom", "anchor_type": "brand"},
            {"target_url": "https://azom.se/", "anchor_text": "https://azom.se/", "anchor_type": "naked_url"},
            {"target_url": "https://azom.se/", "anchor_text": "läs mer", "anchor_type": "generic"},
        ],
        market="se",
    )
    assert three_safe.brand_url_generic_pct == 100.0

    # S1-11: build_product_jsonld omits missing price, strips HTML, maps onbackorder, supports images[0]['src']
    ld_no_price = build_product_jsonld(
        {
            "id": 77,
            "name": "CarPlay Box",
            "short_description": "<p>Enkel <strong>plug &amp; play</strong> montering.</p>",
            "stock_status": "onbackorder",
            "images": [{"src": "https://azom.no/img/box.webp"}],
        },
        market="no",
    )
    assert "price" not in ld_no_price["offers"]
    assert ld_no_price["description"] == "Enkel plug & play montering."
    assert ld_no_price["offers"]["availability"] == "https://schema.org/BackOrder"
    assert ld_no_price["image"] == "https://azom.no/img/box.webp"

    # S1-13: transition_link_package re-evaluates anchor mix & export_link_package enforces RBAC when actor passed
    svc = SeoStudioService(data_dir=mock_env)
    created = svc.create_link_package(site_id="mock-shop", actor="jonatan", name="Re-eval Pack", link_count=4)["package"]
    pid = created["package_id"]
    svc.transition_link_package(package_id=pid, actor="jonatan", new_status="ready_for_review")
    # Tamper stored package targets to spammy exact-match while guardrail_blocked=False
    stored = svc.store.get_link_package(pid)
    assert stored is not None
    stored["targets"] = [
        {"target_url": "https://azom.no/p/a/", "anchor_text": "buy carplay", "keyword": "buy carplay", "anchor_type": "exact_match"}
        for _ in range(4)
    ]
    stored["guardrail_blocked"] = False
    svc.store.save_link_package(stored)
    re_eval = svc.transition_link_package(package_id=pid, actor="oscar", new_status="approved_for_manual_order")
    assert re_eval["ok"] is False
    assert re_eval["error_class"] == "GuardrailBlocked"

    with pytest.raises(AccessDenied):
        svc.export_link_package(pid, fmt="md", actor="unknown-hacker")
    fn, _ct, _body = svc.export_link_package(pid, fmt="md", actor="jonatan")
    assert fn == f"{pid}.md"
