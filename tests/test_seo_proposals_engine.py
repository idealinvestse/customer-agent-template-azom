"""Tests for Proposals 1-5 SEO Studio backend engines, planners, store, service, and digest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient
from ecom_ops.rbac import AccessDenied
from ecom_ops.seo.content_audit import analyze_gsc_quick_wins
from ecom_ops.seo.digest import build_seo_plugin_drift_digest
from ecom_ops.seo.models import SEO_BATCH_ID_RE, VALID_AUDIT_SCOPES, SeoFixBatch
from ecom_ops.seo.planner import build_campaign, build_strategy, draft_batch_fix_items
from ecom_ops.seo.policy import evaluate_cumulative_anchor_mix, is_generic_alt
from ecom_ops.seo.service import SeoStudioService
from ecom_ops.seo.technical import (
    audit_nordic_parity_matrix,
    crawl_site_pages,
    expand_sitemap_index,
    verify_backlink_placement,
)


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AZOM_SEO_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)
    return tmp_path


# --------------------------------------------------------------------------- #
# Proposal 1: Batch Fix-Plan Workflows (seob-*)
# --------------------------------------------------------------------------- #


def test_p1_models_and_localized_batch_drafter() -> None:
    assert {"gsc", "parity", "deep_crawl"}.issubset(VALID_AUDIT_SCOPES)
    assert SEO_BATCH_ID_RE.fullmatch("seob-0123456789ab")
    assert not SEO_BATCH_ID_RE.fullmatch("seom-0123456789ab")

    sample_batch = SeoFixBatch(
        batch_id="seob-abcdef012345",
        site_id="mock-shop",
        audit_id="seoa-abcdef012345",
        market="no",
        scope="all",
        summary="Test",
        items=[],
        item_count=0,
        plan_sha256="a" * 64,
    )
    assert sample_batch.to_dict()["batch_id"] == "seob-abcdef012345"

    entities = [
        {
            "object_type": "product",
            "object_id": 501,
            "title": "Azom CarPlay Skjerm Pro 9\" med veldig lang produkttittel som maa kortes ned for Google SERP",
            "slug": "carplay-skarm-pro",
            "url": "https://azom.no/produkt/carplay-skarm-pro/",
            "status": "publish",
            "meta_title": "",
            "meta_description": "",
            "focus_keyphrase": "",
        }
    ]
    media = [
        {
            "media_id": 302,
            "filename": "IMG_4821.jpg",
            "mime_type": "image/jpeg",
            "alt_text": "",
            "title": "CarPlay Pro i Volvo XC60",
            "attached_post_id": 501,
            "url": "https://azom.no/wp-content/uploads/IMG_4821.jpg",
        }
    ]
    audit = {
        "issues": [
            {"code": "missing_meta_title", "target_type": "product", "target_id": "501", "url": "https://azom.no/produkt/carplay-skarm-pro/"},
            {"code": "missing_meta_description", "target_type": "product", "target_id": "501", "url": "https://azom.no/produkt/carplay-skarm-pro/"},
            {"code": "media_missing_alt", "target_type": "media", "target_id": "302", "url": "https://azom.no/wp-content/uploads/IMG_4821.jpg"},
        ]
    }

    for mkt, forbidden in (
        ("no", ("upptäck", "snabb leverans", "produktbillede")),
        ("dk", ("upptäck", "snabb leverans", "produktbilde")),
        ("se", ("oppdag", "rask levering", "produktbilde", "produktbillede")),
    ):
        drafted = draft_batch_fix_items(audit, entities, media, market=mkt, scope="all", limit=10)
        assert len(drafted) == 2
        meta_item = next(i for i in drafted if i["fix_type"] == "seo_meta")
        alt_item = next(i for i in drafted if i["fix_type"] == "media_alt")

        mt = meta_item["proposed"]["meta_title"]
        md = meta_item["proposed"]["meta_description"]
        alt = alt_item["proposed"]["alt_text"]

        assert 1 <= len(mt) <= 60
        assert 1 <= len(md) <= 155
        assert 5 <= len(alt) <= 125
        assert not is_generic_alt(alt)
        combined = f"{mt} {md} {alt}".lower()
        for word in forbidden:
            assert word not in combined


def test_p1_propose_apply_batch_rbac_gates_drift_and_vault(
    mock_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tr = InMemoryWpBridgeTransport(site_id="mock-shop", base_url="https://mock-shop.example.test")
    bridge = WpBridgeClient(site_id="mock-shop", transport=tr)
    svc = SeoStudioService(data_dir=mock_env, bridge_client_factory=lambda _s: bridge)

    audit = svc.run_audit(site_id="mock-shop", actor="jonatan", scope="site", market="no")
    prop = svc.propose_batch_from_audit(
        "mock-shop", audit["audit_id"], scope="all", limit=10, actor="jonatan"
    )
    assert prop["ok"] is True
    batch = prop["batch"]
    bid = batch["batch_id"]
    assert SEO_BATCH_ID_RE.fullmatch(bid)
    assert batch["item_count"] >= 2
    assert batch["status"] == "pending"
    assert batch["executed"] is False
    assert svc.get_fix_batch(bid) is not None
    assert len(svc.list_fix_batches("mock-shop")) == 1

    # Jonatan cannot apply batch
    denied = svc.apply_batch("mock-shop", bid, expected_sha256=batch["plan_sha256"], actor="jonatan")
    assert denied["ok"] is False
    assert denied["error_class"] == "AccessDenied"

    # Wrong hash refused
    bad_hash = svc.apply_batch("mock-shop", bid, expected_sha256="0" * 64, actor="oscar")
    assert bad_hash["ok"] is False
    assert bad_hash["error_class"] == "PlanHashMismatch"

    # Kill switch blocks
    monkeypatch.setenv("AZOM_SEO_KILL", "1")
    killed = svc.apply_batch("mock-shop", bid, expected_sha256=batch["plan_sha256"], actor="oscar")
    assert killed["ok"] is False
    assert killed["error_class"] == "SeoKillSwitchActive"
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)

    # Live without AZOM_SEO_WRITE_ENABLED blocks
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    disabled = svc.apply_batch("mock-shop", bid, expected_sha256=batch["plan_sha256"], actor="oscar")
    assert disabled["ok"] is False
    assert disabled["error_class"] == "SeoWriteDisabled"
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    # Oscar applies batch -> Change Vault records share batch_id
    applied = svc.apply_batch("mock-shop", bid, expected_sha256=batch["plan_sha256"], actor="oscar")
    assert applied["ok"] is True
    assert applied["batch_id"] == bid
    assert applied["applied_count"] == batch["item_count"]
    assert applied["failed_count"] == 0
    assert len(applied["change_ids"]) == batch["item_count"]
    for cid in applied["change_ids"]:
        chg = svc.ledger.get_change("mock-shop", cid)
        assert chg is not None
        assert chg["metadata"].get("batch_id") == bid
        assert chg.get("batch_id") == bid
    if hasattr(svc.ledger, "list_batch_changes"):
        batch_rows = svc.ledger.list_batch_changes(bid, site_id="mock-shop")
        assert len(batch_rows) == batch["item_count"]

    # Double apply refused
    again = svc.apply_batch("mock-shop", bid, expected_sha256=batch["plan_sha256"], actor="oscar")
    assert again["ok"] is False
    assert again["error_class"] == "BatchAlreadyApplied"

    # Per-item drift detection test on a second batch
    tr2 = InMemoryWpBridgeTransport(site_id="mock-shop", base_url="https://mock-shop.example.test")
    bridge2 = WpBridgeClient(site_id="mock-shop", transport=tr2)
    svc2 = SeoStudioService(data_dir=mock_env, bridge_client_factory=lambda _s: bridge2)
    audit2 = svc2.run_audit(site_id="mock-shop", actor="jonatan", scope="site", market="se")
    prop2 = svc2.propose_batch_from_audit("mock-shop", audit2["audit_id"], scope="all", limit=10, actor="jonatan")
    b2 = prop2["batch"]
    # Mutate first item target behind the scenes to trigger drift
    first = b2["items"][0]
    if first["target_type"] == "media":
        tr2.seo_media_store[int(first["target_id"])]["alt_text"] = "Drifted external edit"
    else:
        tr2.seo_entities_store[(first["target_type"], int(first["target_id"]))]["meta_title"] = "Drifted external title"

    res2 = svc2.apply_batch("mock-shop", b2["batch_id"], expected_sha256=b2["plan_sha256"], actor="oscar")
    assert res2["failed_count"] >= 1
    assert res2["applied_count"] >= 1
    assert res2["batch"]["status"] == "partial"
    assert "DriftDetected" in res2["batch"]["items"][0]["error"]

    # Expired batch refused
    prop3 = svc2.propose_batch_from_audit("mock-shop", audit2["audit_id"], scope="media_alt", limit=5, actor="jonatan")
    b3 = prop3["batch"]
    b3["expires_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    svc2.store.save_fix_batch(b3)
    exp_res = svc2.apply_batch("mock-shop", b3["batch_id"], expected_sha256=b3["plan_sha256"], actor="oscar")
    assert exp_res["ok"] is False
    assert exp_res["error_class"] == "BatchExpired"


# --------------------------------------------------------------------------- #
# Proposal 2: GSC Strike-Distance Quick-Win Engine
# --------------------------------------------------------------------------- #


def test_p2_gsc_quick_wins_audit_and_planner_wiring(mock_env: Path) -> None:
    rows = [
        {"query": "trådløs carplay skjerm", "page": "https://azom.no/produkt/pro/", "clicks": 35, "impressions": 1800, "ctr": 0.0194, "position": 6.2},
        {"query": "trådløs carplay skjerm", "page": "https://azom.no/guide/carplay/", "clicks": 10, "impressions": 700, "ctr": 0.0143, "position": 10.5},
        {"query": "apple carplay volvo xc60", "page": "https://azom.no/guide/volvo/", "clicks": 2, "impressions": 500, "ctr": 0.004, "position": 4.1},
        {"query": "azom", "page": "https://azom.no/", "clicks": 400, "impressions": 1000, "ctr": 0.40, "position": 1.1},
        {"query": "carplay adapter test", "page": "https://azom.no/produkt/mini/", "clicks": 5, "impressions": 2500, "ctr": 0.002, "position": 14.0},
    ]
    entities = [{"object_type": "product", "object_id": 501, "title": "Azom Pro", "url": "https://azom.no/produkt/pro/"}]
    qw = analyze_gsc_quick_wins(rows, entities=entities)

    # Strike distance: pos 4.0-15.0 sorted by impressions desc (2500 first, 1800 second...)
    assert len(qw["strike_distance"]) == 4
    assert qw["strike_distance"][0]["query"] == "carplay adapter test"
    assert qw["strike_distance"][1]["object_id"] == "501"
    # Low CTR: impressions >= 200 and ctr below benchmark
    assert any(r["query"] == "apple carplay volvo xc60" for r in qw["low_ctr"])
    assert not any(r["query"] == "azom" for r in qw["low_ctr"])
    # Cannibalization: same query on >= 2 distinct URLs
    assert len(qw["cannibalization"]) == 1
    assert qw["cannibalization"][0]["query"] == "trådløs carplay skjerm"
    assert qw["cannibalization"][0]["total_impressions"] == 2500

    # Service get_gsc_quick_wins & audit enrichment
    svc = SeoStudioService(data_dir=mock_env)
    res = svc.get_gsc_quick_wins("mock-shop", days=28, actor="jonatan")
    assert res["ok"] is True
    assert "gsc_status" in res
    assert res["summary"]["strike_distance_count"] >= 1

    gsc_audit = svc.run_audit(site_id="mock-shop", actor="jonatan", scope="gsc", market="no")
    assert gsc_audit["ok"] is True
    assert "Google Search Console" in gsc_audit["gsc_note"]
    assert gsc_audit["gsc_quick_wins"]["summary"]["strike_distance_count"] >= 1

    # Planner wiring into strategy & campaign
    strat = build_strategy(
        site_id="mock-shop",
        actor="jonatan",
        title="GSC-driven strategi",
        market="no",
        gsc_quick_wins=qw,
    )
    assert "google_search_console" in strat["data_sources"]
    assert "carplay adapter test" in strat["focus_topics"]

    camp = build_campaign(
        site_id="mock-shop",
        actor="jonatan",
        name="GSC-kampanj",
        market="no",
        gsc_quick_wins=qw,
    )
    assert "google_search_console" in camp["data_sources"]
    assert any(c["primary_keyword"] == "carplay adapter test" for c in camp["keyword_clusters"])


# --------------------------------------------------------------------------- #
# Proposal 3: Deep Crawl, Nordic Parity Matrix & Audit Diff
# --------------------------------------------------------------------------- #


def test_p3_sitemap_expand_deep_crawl_nordic_parity_and_audit_diff(mock_env: Path) -> None:
    index_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://azom.se/product-sitemap.xml</loc></sitemap>
      <sitemap><loc>https://azom.se/page-sitemap.xml</loc></sitemap>
    </sitemapindex>"""

    def fake_sm_fetch(url: str) -> dict[str, Any]:
        if "product-sitemap" in url:
            return {
                "ok": True,
                "text": "<urlset><url><loc>https://azom.se/produkt/a/</loc></url><url><loc>https://azom.se/404-dead/</loc></url></urlset>",
            }
        return {
            "ok": True,
            "text": "<urlset><url><loc>https://azom.se/old-redirect/</loc></url><url><loc>https://azom.se/noindex-page/</loc></url></urlset>",
        }

    expanded = expand_sitemap_index(index_xml, max_children=2, fetcher=fake_sm_fetch)
    assert expanded["ok"] is True
    assert expanded["kind"] == "sitemapindex"
    assert len(expanded["urls"]) == 4

    def fake_page_fetch(url: str) -> dict[str, Any]:
        if "404-dead" in url:
            return {"ok": False, "status": 404, "url": url, "final_url": url, "headers": {}, "text": ""}
        if "500-err" in url:
            return {"ok": False, "status": 500, "url": url, "final_url": url, "headers": {}, "text": ""}
        if "old-redirect" in url:
            return {
                "ok": True,
                "status": 200,
                "url": url,
                "final_url": "https://azom.se/produkt/a/",
                "redirect_chain": [url, "https://azom.se/mid/", "https://azom.se/produkt/a/"],
                "headers": {},
                "text": "<html><head><title>Produkt A | Azom</title><link rel='canonical' href='https://azom.se/produkt/a/'></head><body><h1>A</h1></body></html>",
            }
        if "noindex-page" in url:
            return {
                "ok": True,
                "status": 200,
                "url": url,
                "final_url": url,
                "headers": {},
                "text": "<html><head><title>Noindex</title><meta name='robots' content='noindex'><link rel='canonical' href='https://azom.se/'></head><body><h1>X</h1></body></html>",
            }
        return {
            "ok": True,
            "status": 200,
            "url": url,
            "final_url": url,
            "headers": {},
            "text": "<html><head><title>Produkt A – Trådlös CarPlay | Azom</title><link rel='canonical' href='https://azom.se/produkt/a/'></head><body><h1>A</h1></body></html>",
        }

    crawl = crawl_site_pages(
        [*expanded["urls"], "https://azom.se/500-err/"],
        max_pages=10,
        fetcher=fake_page_fetch,
    )
    c_codes = {i["code"] for i in crawl["issues"]}
    assert "crawl_http_404" in c_codes
    assert "crawl_http_5xx" in c_codes
    assert "crawl_redirect_chain" in c_codes
    assert "crawl_noindex_canonical_conflict" in c_codes

    # Nordic parity matrix
    parity = audit_nordic_parity_matrix(
        {
            "se": [
                {
                    "key": "/produkt/carplay-pro",
                    "url": "https://azom.se/produkt/carplay-pro/",
                    "title": "Azom CarPlay Skärm Pro",
                    "price_currency": "SEK",
                    "hreflang": [
                        {"hreflang": "sv-SE", "href": "https://azom.se/produkt/carplay-pro/"},
                        {"hreflang": "nb-NO", "href": "https://azom.no/produkt/carplay-pro/"},
                        {"hreflang": "x-default", "href": "https://azom.no/produkt/carplay-pro/"},
                    ],
                }
            ],
            "no": [
                {
                    "key": "/produkt/carplay-pro",
                    "url": "https://azom.no/produkt/carplay-pro/",
                    "title": "Azom CarPlay Skjerm Pro",
                    "text": "Köp idag med fraktfritt erbjudande och svensk kundtjänst!",
                    "price_currency": "SEK",  # wrong for NO!
                    "hreflang": [
                        {"hreflang": "nb-NO", "href": "https://azom.no/produkt/carplay-pro/"},
                    ],  # missing return link to SE!
                }
            ],
            "dk": [],  # missing translation for DK!
        }
    )
    p_codes = {i["code"] for i in parity["issues"]}
    assert "parity_missing_translation" in p_codes
    assert "parity_swedish_leak" in p_codes
    assert "parity_wrong_currency" in p_codes
    assert "hreflang_missing_reciprocity" in p_codes

    # Service methods: run_deep_crawl, run_nordic_parity, audit scopes & diff_audits
    svc = SeoStudioService(data_dir=mock_env)
    dc_res = svc.run_deep_crawl("mock-shop", max_pages=5, actor="jonatan")
    assert dc_res["ok"] is True
    assert len(dc_res["crawled"]) >= 1

    np_res = svc.run_nordic_parity("mock-shop", actor="jonatan")
    assert np_res["ok"] is True
    assert set(np_res["markets"]) == {"se", "no", "dk"}
    assert np_res["summary"]["total_issues"] >= 1

    # Audit scopes parity & deep_crawl
    a_parity = svc.run_audit(site_id="mock-shop", actor="jonatan", scope="parity", market="no")
    assert a_parity["ok"] is True and a_parity["parity"]["summary"]["total_issues"] >= 1
    a_crawl = svc.run_audit(site_id="mock-shop", actor="jonatan", scope="deep_crawl", market="no")
    assert a_crawl["ok"] is True and len(a_crawl["deep_crawl"]["crawled"]) >= 1

    # Audit diff trend: first site audit -> has_previous=False, second -> has_previous=True with delta
    a1 = svc.run_audit(site_id="mock-shop", actor="jonatan", scope="site", market="no")
    d1 = svc.diff_audits("mock-shop", latest_audit_id=a1["audit_id"], actor="jonatan")
    assert d1["ok"] is True
    assert d1["has_previous"] is False

    # Persist an improved second audit to test resolved_issues & positive score_delta
    a2 = dict(a1)
    a2["audit_id"] = "seoa-ffffffffffff"
    a2["created_at"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    a2["issues"] = a1["issues"][2:] + [
        {"code": "brand_new_test_issue", "severity": "low", "category": "on_page", "title": "New", "detail": "New", "target_type": "page", "target_id": "99", "url": "https://azom.no/new/"}
    ]
    a2["score"] = min(100, int(a1["score"]) + 10)
    svc.store.save_audit(a2)

    d2 = svc.diff_audits("mock-shop", actor="jonatan")
    assert d2["ok"] is True
    assert d2["has_previous"] is True
    assert d2["latest_id"] == "seoa-ffffffffffff"
    assert d2["previous_id"] == a1["audit_id"]
    assert d2["score_delta"] == a2["score"] - a1["score"]
    assert any(i["code"] == "brand_new_test_issue" for i in d2["new_issues"])
    assert len(d2["resolved_issues"]) == 2


# --------------------------------------------------------------------------- #
# Proposal 4: Link-Package Lifecycle (Edit Draft, Delivery, Verification, Cumulative)
# --------------------------------------------------------------------------- #


def test_p4_link_package_edit_delivery_verify_and_cumulative_anchor_mix(mock_env: Path) -> None:
    # Cumulative anchor mix across historical delivered + current targets
    hist = [
        {
            "package_id": "seol-111111111111",
            "status": "delivered",
            "targets": [
                {"target_url": "https://azom.no/produkt/a/", "anchor_text": "trådløs carplay", "keyword": "trådløs carplay"},
                {"target_url": "https://azom.no/produkt/a/", "anchor_text": "Azom", "keyword": "trådløs carplay"},
            ],
        },
        {
            "package_id": "seol-222222222222",
            "status": "cancelled",  # must be ignored!
            "targets": [
                {"target_url": "https://azom.no/produkt/a/", "anchor_text": "trådløs carplay", "keyword": "trådløs carplay"}
                for _ in range(10)
            ],
        },
    ]
    cur = [
        {"target_url": "https://azom.no/produkt/a/", "anchor_text": "trådløs carplay", "keyword": "trådløs carplay"},
        {"target_url": "https://azom.no/produkt/a/", "anchor_text": "Azom Nordic", "keyword": "trådløs carplay"},
    ]
    cum = evaluate_cumulative_anchor_mix(cur, hist, base_url="https://azom.no")
    assert cum["historical_links"] == 2
    assert cum["current_links"] == 2
    assert cum["total_links"] == 4
    assert cum["exact_match_pct"] == 50.0
    assert cum["guardrail_blocked"] is True
    assert any("Överkoncentration" in w or "överkoncentration" in w.lower() for w in cum["warnings"])

    # Backlink placement verifier
    ok_v = verify_backlink_placement(
        "https://motorblogg.no/carplay-guide/",
        expected_target_url="https://azom.no/produkt/carplay-skarm-pro/",
        expected_anchor="Azom",
        use_mock=True,
    )
    assert ok_v["ok"] is True
    assert ok_v["verified"] is True
    assert ok_v["rel_type"] == "dofollow"
    assert ok_v["indexable"] is True

    v_404 = verify_backlink_placement("https://motorblogg.no/404-missing/", expected_target_url="https://azom.no/", use_mock=True)
    assert v_404["ok"] is False and v_404["http_status"] == 404

    v_noindex = verify_backlink_placement("https://motorblogg.no/noindex-post/", expected_target_url="https://azom.no/", use_mock=True)
    assert v_noindex["ok"] is False and v_noindex["indexable"] is False

    v_nofollow = verify_backlink_placement("https://motorblogg.no/nofollow-post/", expected_target_url="https://azom.no/", expected_anchor="Azom", use_mock=True)
    assert v_nofollow["ok"] is True and v_nofollow["rel_type"] == "nofollow"

    # Full service lifecycle
    svc = SeoStudioService(data_dir=mock_env)
    created = svc.create_link_package(
        site_id="mock-shop",
        actor="jonatan",
        name="Draft Pack NO",
        market="no",
        budget_sek=12000.0,
        link_count=4,
    )
    assert "cumulative_anchor_mix" in created
    pid = created["package"]["package_id"]
    fetched = svc.get_link_package(pid)
    assert fetched is not None and "cumulative_anchor_mix" in fetched

    # Edit draft allowed while status == draft
    upd = svc.update_link_package_draft(
        pid,
        name="Updated Draft Pack NO",
        budget_sek=16000.0,
        notes="Uppdaterade noter",
        actor="jonatan",
    )
    assert upd["ok"] is True
    assert upd["link_package"]["name"] == "Updated Draft Pack NO"
    assert upd["link_package"]["budget_sek"] == 16000.0
    assert "cumulative_anchor_mix" in upd

    # Submit -> edit draft now refused
    svc.transition_link_package(package_id=pid, actor="jonatan", new_status="ready_for_review")
    refused_edit = svc.update_link_package_draft(pid, name="Blocked Edit", actor="jonatan")
    assert refused_edit["ok"] is False
    assert refused_edit["error_class"] == "InvalidStatus"

    # Record link delivery: Jonatan denied, Oscar allowed
    svc.transition_link_package(package_id=pid, actor="oscar", new_status="approved_for_manual_order")
    j_del = svc.record_link_delivery(pid, target_index=1, published_url="https://bilguide.no/artikkel-1/", actor="jonatan")
    assert j_del["ok"] is False and j_del["error_class"] == "AccessDenied"

    o_del = svc.record_link_delivery(pid, target_index=1, published_url="https://bilguide.no/artikkel-1/", actor="oscar")
    assert o_del["ok"] is True
    assert o_del["link_package"]["targets"][0]["published_url"] == "https://bilguide.no/artikkel-1/"

    # Verify link package
    ver = svc.verify_link_package(pid, actor="jonatan")
    assert ver["ok"] is True
    assert ver["verified_count"] == 1
    assert ver["issue_count"] == 0
    assert len(ver["results"]) == 1

    # Cumulative anchor mix across site
    site_cum = svc.evaluate_site_cumulative_anchor_mix("mock-shop", actor="jonatan")
    assert site_cum["total_links"] == 4


# --------------------------------------------------------------------------- #
# Proposal 5: Weekly SEO & Plugin Drift Digest
# --------------------------------------------------------------------------- #


def test_p5_weekly_seo_plugin_drift_digest(mock_env: Path) -> None:
    svc = SeoStudioService(data_dir=mock_env)
    svc.run_audit(site_id="mock-shop", actor="jonatan", scope="site", market="no")

    class _FakePluginSvc:
        def plugin_inventory(self, site_id: str, *, actor: str, include_updates: bool = True):
            return {
                "ok": True,
                "data": {
                    "plugins": [
                        {"slug": "woocommerce", "version": "9.3.0", "new_version": "9.4.1", "update_available": True, "is_critical": True, "risk_class": "minor"},
                        {"slug": "wordpress-seo", "version": "23.0", "new_version": "23.1", "update_available": True, "is_critical": False, "risk_class": "patch"},
                    ],
                    "summary": {"total": 12, "updates_available": 2, "critical_updates": 1},
                },
            }

    digest = build_seo_plugin_drift_digest(
        "mock-shop",
        actor="jonatan",
        days=7,
        data_dir=mock_env,
        seo_service=svc,
        plugin_service=_FakePluginSvc(),
    )
    assert digest["ok"] is True
    assert digest["site_id"] == "mock-shop"
    assert digest["seo_trend"]["latest_score"] is not None
    assert digest["gsc_summary"]["strike_distance_count"] >= 1
    assert digest["parity_summary"]["total_issues"] >= 1
    assert digest["plugin_summary"]["available"] is True
    assert digest["plugin_summary"]["updates_available"] == 2
    assert digest["plugin_summary"]["critical_updates"] == 1
    assert digest["dashboard_links"] == {
        "seo": "/seo?site=mock-shop",
        "plugins": "/wp/mock-shop/plugins",
        "changes": "/wp/mock-shop/changes?category=seo_meta",
    }
    md = digest["markdown"]
    assert "/seo?site=mock-shop" in md
    assert "/wp/mock-shop/plugins" in md
    assert "/wp/mock-shop/changes?category=seo_meta" in md

    with pytest.raises(AccessDenied):
        build_seo_plugin_drift_digest("mock-shop", actor="unknown-hacker", data_dir=mock_env)
