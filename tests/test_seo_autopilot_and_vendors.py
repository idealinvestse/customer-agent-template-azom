"""Comprehensive tests for Autonomous SEO Pipeline, SERP Research Bridge, and Vetted Nordic Link-Shop Engine."""

from __future__ import annotations

import ast
import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.integrations.search.router import reset_search_router
from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient
from ecom_ops.research import (
    SearchPolicyError,
    clear_research_config_cache,
    get_profile,
    providers_for_round,
    recommend_profile,
)
from ecom_ops.seo.models import (
    SEO_AUDIT_ID_RE,
    SEO_BATCH_ID_RE,
    SEO_CAMPAIGN_ID_RE,
    SEO_LINK_PKG_ID_RE,
    SEO_STRATEGY_ID_RE,
)
from ecom_ops.seo.planner import render_link_package_brief
from ecom_ops.seo.policy import contains_spam_or_pbn
from ecom_ops.seo.research_bridge import (
    gather_seo_serp_context,
    vet_link_vendors_via_web,
)
from ecom_ops.seo.service import SeoStudioService
from ecom_ops.seo.vendors import (
    MIN_WHITE_HAT_UNIT_SEK,
    NORDIC_LINK_VENDORS,
    determine_campaign_archetype,
    match_vendors_for_link_package,
    vet_vendor_candidate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AZOM_WEB_SEARCH_ENABLED", raising=False)
    monkeypatch.delenv("AZOM_WEB_SEARCH_KILL", raising=False)
    monkeypatch.delenv("AZOM_RESEARCH_ENABLED", raising=False)
    monkeypatch.delenv("AZOM_RESEARCH_KILL", raising=False)
    monkeypatch.delenv("AZOM_SEO_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    clear_research_config_cache()
    reset_search_router()
    return tmp_path / "data"


# --------------------------------------------------------------------------- #
# 1. Profile #13, Leaf Purity & Public Spam/PBN Helper
# --------------------------------------------------------------------------- #


def test_seo_vendor_vetting_profile_and_module_purity() -> None:
    prof = get_profile("seo_vendor_vetting")
    assert prof.ambition == "standard"
    assert prof.combination_mode == "staged"
    assert prof.market_defaults == ("SE", "NO", "DK")
    assert prof.pii_strictness == "high"
    assert prof.skill_doc == "skills/research-profiles/SKILL.md"
    r1 = providers_for_round(prof, 1)
    r2 = providers_for_round(prof, 2)
    assert r1[:2] != r2[:2]

    assert recommend_profile("Granska nordisk länkleverantör Bazoom och WhitePress anti-PBN").id == "seo_vendor_vetting"

    # Leaf module purity: vendors.py must not import research or HTTP libs
    vendors_src = (REPO_ROOT / "skills/ecom_ops/seo/vendors.py").read_text(encoding="utf-8")
    tree = ast.parse(vendors_src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in ("urllib", "http", "requests", "httpx", "ecom_ops.research", "ecom_ops.integrations"):
        assert not any(m == forbidden or m.startswith(f"{forbidden}.") for m in imported), f"Forbidden import: {forbidden}"

    # research_bridge.py must NOT import ecom_ops.research at top level
    bridge_src = (REPO_ROOT / "skills/ecom_ops/seo/research_bridge.py").read_text(encoding="utf-8")
    bridge_tree = ast.parse(bridge_src)
    top_imports: set[str] = set()
    for stmt in bridge_tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                top_imports.add(alias.name)
        elif isinstance(stmt, ast.ImportFrom) and stmt.module:
            top_imports.add(stmt.module)
    assert not any(m.startswith("ecom_ops.research") for m in top_imports)

    # Public contains_spam_or_pbn helper
    hit, terms = contains_spam_or_pbn("Buy 500 links from a Private Blog Network and casino link farm")
    assert hit is True
    assert "private blog network" in terms
    assert "casino" in terms
    assert "link farm" in terms
    assert contains_spam_or_pbn("White-hat editorial guest article on Swedish car tech magazine") == (False, [])


# --------------------------------------------------------------------------- #
# 2. Vendor Vetting Rubric & All 7 Hard-Reject Anti-PBN Rules
# --------------------------------------------------------------------------- #


def test_curated_vendors_pass_rubric_and_all_7_hard_rejects_score_zero() -> None:
    assert len(NORDIC_LINK_VENDORS) == 6
    assert {v.vendor_id for v in NORDIC_LINK_VENDORS} == {
        "bazoom",
        "whitepress",
        "linkhouse",
        "prnews",
        "getfluence",
        "seeders",
    }

    for v in NORDIC_LINK_VENDORS:
        for mkt in ("se", "no", "dk"):
            rep = vet_vendor_candidate(v, market=mkt)
            assert rep["hard_reject"] is False
            assert rep["eligible"] is True
            assert rep["vetting_score"] >= 75
            assert rep["vetting_status"] in {"preferred", "conditional"}
            assert sum(rep["rubric_breakdown"].values()) == rep["vetting_score"]

    # All 7 hard-reject scenarios -> forced score 0 & status "rejected"
    hard_reject_cases: list[tuple[str, dict[str, Any]]] = [
        (
            "hidden_inventory_blind_bundle",
            {
                "vendor_id": "shady_blind",
                "name": "BlindLinks",
                "domain": "blindlinks.example",
                "pre_payment_url_preview": False,
                "description": "10x DR50 links for 299 EUR blind bundle",
            },
        ),
        (
            "pbn_or_link_farm",
            {
                "vendor_id": "shady_pbn",
                "name": "PbnBlast",
                "domain": "pbnblast.example",
                "description": "Owned private blog network and cheap bulk links",
            },
        ),
        (
            "guaranteed_ranking_claims",
            {
                "vendor_id": "shady_rank",
                "name": "Top1SEO",
                "domain": "top1seo.example",
                "claims": "Guaranteed #1 Google ranking in 14 days!",
            },
        ),
        (
            "toxic_neighborhood",
            {
                "vendor_id": "shady_casino",
                "name": "SlotsLinks",
                "domain": "slotslinks.example",
                "description": "Links alongside online casino, gambling, and snabblån portals",
            },
        ),
        (
            "raw_machine_translation",
            {
                "vendor_id": "shady_mt",
                "name": "AutoTranslatePR",
                "domain": "autotrans.example",
                "forces_machine_translation": True,
                "description": "Raw machine translation into Swedish and Norwegian",
            },
        ),
        (
            "anchor_lockout_or_undisclosed_ads",
            {
                "vendor_id": "shady_anchor",
                "name": "LockedAnchor",
                "domain": "lockedanchor.example",
                "anchor_lockout": True,
                "description": "Undisclosed advertorial with no anchor control",
            },
        ),
        (
            "no_warranty_or_coercive_lockin",
            {
                "vendor_id": "shady_nowarranty",
                "name": "ZeroWarranty",
                "domain": "zerowarranty.example",
                "guarantee_months": 1,
                "coercive_lockin": True,
            },
        ),
    ]
    for expected_code, payload in hard_reject_cases:
        res = vet_vendor_candidate(payload, market="se")
        assert res["hard_reject"] is True, f"Expected hard_reject for {expected_code}"
        assert res["vetting_score"] == 0
        assert res["vetting_status"] == "rejected"
        assert res["eligible"] is False
        assert any(expected_code in r for r in res["hard_reject_reasons"])

    # Web-vetting confirmation of PBN also hard-rejects
    web_pbn = vet_vendor_candidate(
        {"vendor_id": "suspect", "name": "SuspectShop", "domain": "suspectshop.example"},
        market="no",
        web_signals=[
            {
                "url": "https://review.example/suspectshop",
                "title": "SuspectShop exposed as PBN link farm",
                "snippet": "Independent audit confirmed suspectshop.example operates a private blog network.",
            }
        ],
    )
    assert web_pbn["hard_reject"] is True
    assert web_pbn["vetting_score"] == 0


# --------------------------------------------------------------------------- #
# 3. Budget Realism Floor, Archetypes & SE/NO/DK Ranking Differences
# --------------------------------------------------------------------------- #


def test_budget_floor_warning_archetypes_and_nordic_market_rankings() -> None:
    assert MIN_WHITE_HAT_UNIT_SEK == {"se": 1200.0, "dk": 1200.0, "no": 1800.0}

    # Budget too low for NO (6000 / 6 = 1000 SEK < 1800 SEK floor) -> warns & recommends 3 links, never PBN
    low_no = match_vendors_for_link_package(
        "no",
        6000.0,
        6,
        extra_candidates=[
            {
                "vendor_id": "cheap_pbn",
                "name": "CheapPBN",
                "domain": "cheappbn.example",
                "typical_price_per_link_sek": (200, 400),
                "description": "Cheap bulk links private blog network",
            }
        ],
    )
    assert low_no["ok"] is True
    assert low_no["blocked"] is False
    assert "1800" in low_no["budget_warning"]
    assert low_no["recommended_link_count"] == 3
    assert all(c["vendor_id"] != "cheap_pbn" for c in low_no["candidates"])
    assert any(r["vendor_id"] == "cheap_pbn" for r in low_no["rejected_candidates"])
    assert any("Annonse" in item for item in low_no["disclosure_checklist"])
    assert "aldrig automatiska köp" in low_no["no_auto_purchase_notice"]

    # Budget floor for SE (5000 / 6 = 833 SEK < 1200 SEK) -> recommends 4 links
    low_se = match_vendors_for_link_package("se", 5000.0, 6)
    assert "1200" in low_se["budget_warning"]
    assert low_se["recommended_link_count"] == 4
    assert any("Annons" in item for item in low_se["disclosure_checklist"])

    # Fail-closed when anchor guardrails are blocked
    blocked = match_vendors_for_link_package("se", 15000.0, 6, anchor_guardrail_blocked=True)
    assert blocked["ok"] is False
    assert blocked["blocked"] is True
    assert blocked["candidates"] == []
    assert blocked["selected_vendor_id"] is None

    # Archetype classification
    assert determine_campaign_archetype(15000.0, 6) == "self_serve_marketplace"
    assert determine_campaign_archetype(35000.0, 8, brand_url_generic_pct=78.0) == "hybrid_marketplace_pr"
    assert determine_campaign_archetype(75000.0, 12) == "managed_nordic_pr"

    # Market-specific ranking differences across SE, NO, DK
    se_agile = match_vendors_for_link_package("se", 15000.0, 6)
    dk_agile = match_vendors_for_link_package("dk", 15000.0, 6)
    no_managed = match_vendors_for_link_package("no", 75000.0, 12, goal="market_entry_pr")

    assert se_agile["recommended_tier"] == "self_serve_marketplace"
    assert se_agile["candidates"][0]["vendor_id"] == "bazoom"
    assert se_agile["candidates"][1]["vendor_id"] == "whitepress"

    # DK has strongest Bazoom & Linkhouse footprint (~5,500 & ~2,200 .dk)
    assert dk_agile["candidates"][0]["vendor_id"] == "bazoom"
    assert any("Annonce" in item for item in dk_agile["disclosure_checklist"])
    assert dk_agile["candidates"][0]["market_fit"] > se_agile["candidates"][0]["market_fit"]

    # Managed NO launch elevates Seeders to #1 (dedicated NO outreach team for scarce .no inventory)
    assert no_managed["recommended_tier"] == "managed_nordic_pr"
    assert no_managed["selected_vendor_id"] == "seeders"
    assert no_managed["candidates"][0]["vendor_id"] == "seeders"
    assert len(no_managed["candidates"]) == 3


# --------------------------------------------------------------------------- #
# 4. SERP Research Bridge, OutboundQueryFirewall & Graceful Live Degradation
# --------------------------------------------------------------------------- #


def test_serp_research_bridge_firewall_and_live_degrade(
    mock_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mock-first SERP context via seo_content_radar
    ctx = gather_seo_serp_context(
        "mock-shop",
        topic="trådlös Apple CarPlay skärm",
        market="se",
        actor="jonatan",
        run_deep_research=True,
        data_dir=mock_env,
    )
    assert ctx["ok"] is True
    assert ctx["degraded"] is False
    assert ctx["profile_id"] == "seo_content_radar"
    assert len(ctx["competitor_domains"]) >= 2
    assert any("brl.se" in d or "bilradiospes.no" in d for d in ctx["competitor_domains"])
    assert len(ctx["content_gaps"]) >= 1
    assert len(ctx["recommended_angles"]) >= 1
    assert len(ctx["citations"]) >= 2
    assert ctx["research_job_id"] and ctx["research_job_id"].startswith("res-")

    # OutboundQueryFirewall MUST fail closed on PII in topic
    with pytest.raises(SearchPolicyError):
        gather_seo_serp_context(
            "mock-shop",
            topic="CarPlay för kund anna.svensson@example.se order #99182",
            market="se",
            actor="jonatan",
            data_dir=mock_env,
        )

    # Web-based vendor vetting in mock mode uses seo_vendor_vetting fixtures
    vet_res = vet_link_vendors_via_web(
        "se",
        actor="jonatan",
        budget_sek=18000.0,
        link_count=6,
        data_dir=mock_env,
    )
    assert vet_res["ok"] is True
    assert vet_res["web_vetting_used"] is True
    assert vet_res["candidates"][0]["provenance"] == "curated+web_vetted"
    assert len(vet_res["citations"]) >= 1

    # Graceful degrade in live mode when AZOM_WEB_SEARCH_ENABLED is unset
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    clear_research_config_cache()
    reset_search_router()

    degraded_serp = gather_seo_serp_context(
        "mock-shop",
        topic="trådlös Apple CarPlay",
        market="se",
        actor="jonatan",
        data_dir=mock_env,
    )
    assert degraded_serp["ok"] is True
    assert degraded_serp["degraded"] is True
    assert degraded_serp["status"] == "web_search_disabled"

    degraded_vendors = vet_link_vendors_via_web(
        "se",
        actor="jonatan",
        budget_sek=18000.0,
        link_count=6,
        data_dir=mock_env,
    )
    assert degraded_vendors["ok"] is True
    assert degraded_vendors["web_vetting_used"] is False
    assert degraded_vendors["web_vetting_status"].startswith("degraded:")
    assert len(degraded_vendors["candidates"]) == 3


# --------------------------------------------------------------------------- #
# 5. Full Autonomous SEO Pipeline E2E, Draft Vendor Selection & Brief Exports
# --------------------------------------------------------------------------- #


def test_autonomous_seo_pipeline_e2e_vendor_selection_and_brief_exports(
    mock_env: Path,
) -> None:
    transport = InMemoryWpBridgeTransport(site_id="mock-shop", base_url="https://mock-shop.example.test")
    bridge = WpBridgeClient(site_id="mock-shop", transport=transport)
    svc = SeoStudioService(data_dir=mock_env, bridge_client_factory=lambda _s: bridge)

    res = svc.run_seo_autopilot(
        "mock-shop",
        topic="trådlös Apple CarPlay skärm",
        market="se",
        budget_sek=18000.0,
        link_count=6,
        include_web_research=True,
        include_vendor_vetting=True,
        include_batch_fixes=True,
        actor="jonatan",
    )
    assert res["ok"] is True
    assert res["executed"] is False
    assert res["external_order_placed"] is False
    assert res["approval_required"] is True

    # Chained artifacts all persisted and cross-linked
    audit = res["audit"]
    strategy = res["strategy"]
    campaign = res["campaign"]
    pkg = res["link_package"]
    batch = res["batch_fix"]

    assert SEO_AUDIT_ID_RE.fullmatch(audit["audit_id"])
    assert SEO_STRATEGY_ID_RE.fullmatch(strategy["strategy_id"])
    assert SEO_CAMPAIGN_ID_RE.fullmatch(campaign["campaign_id"])
    assert SEO_LINK_PKG_ID_RE.fullmatch(pkg["package_id"])
    assert batch is not None and SEO_BATCH_ID_RE.fullmatch(batch["batch_id"])
    assert batch["executed"] is False
    assert batch["status"] == "pending"

    assert campaign["strategy_id"] == strategy["strategy_id"]
    assert pkg["campaign_id"] == campaign["campaign_id"]
    assert pkg["status"] == "draft"
    assert pkg["selected_vendor_id"] == res["vendor_selection"]["selected_vendor_id"]
    assert len(pkg["recommended_vendors"]) == 3
    assert "cumulative_anchor_mix" in pkg
    assert "web_serp_research:seo_content_radar" in strategy["data_sources"]
    assert "web_serp_research:seo_content_radar" in campaign["data_sources"]
    assert strategy["serp_research"]["competitor_domains"]
    assert campaign["serp_research"]["content_gaps"]

    # Second autopilot run within 6h reuses the recent site audit
    res2 = svc.run_seo_autopilot(
        "mock-shop",
        topic="trådlös CarPlay adapter",
        market="se",
        budget_sek=15000.0,
        link_count=6,
        include_batch_fixes=False,
        actor="jonatan",
    )
    assert res2["summary"]["audit_reused"] is True
    assert res2["audit"]["audit_id"] == audit["audit_id"]
    assert res2["batch_fix"] is None

    # Service-level read-only vendor vetting
    vet_direct = svc.vet_link_vendors(market="no", budget_sek=24000.0, link_count=8, actor="jonatan")
    assert vet_direct["ok"] is True
    assert len(vet_direct["candidates"]) == 3

    # Draft vendor selection: switch to WhitePress on draft package
    sel_ok = svc.select_link_package_vendor(
        pkg["package_id"], vendor_id="whitepress", actor="jonatan"
    )
    assert sel_ok["ok"] is True
    assert sel_ok["link_package"]["selected_vendor_id"] == "whitepress"
    assert "WhitePress" in sel_ok["link_package"]["brief_markdown"]

    # Refuses unknown vendor
    sel_unknown = svc.select_link_package_vendor(
        pkg["package_id"], vendor_id="not_a_real_vendor", actor="jonatan"
    )
    assert sel_unknown["ok"] is False
    assert sel_unknown["error_class"] == "VendorNotFound"

    # Transition draft -> ready_for_review, then verify vendor selection is refused on non-draft
    trans = svc.transition_link_package(
        package_id=pkg["package_id"], actor="jonatan", new_status="ready_for_review"
    )
    assert trans["ok"] is True
    sel_locked = svc.select_link_package_vendor(
        pkg["package_id"], vendor_id="bazoom", actor="jonatan"
    )
    assert sel_locked["ok"] is False
    assert sel_locked["error_class"] == "InvalidStatus"

    # Brief rendering: MD, JSON, CSV (including vendor comparison, disclosure checklist, and formula-injection safety)
    md_fn, md_ct, md_body = svc.export_link_package(pkg["package_id"], fmt="md", actor="jonatan")
    assert md_fn.endswith(".md") and "markdown" in md_ct
    assert "Rekommenderade granskade länkleverantörer" in md_body
    assert "WhitePress (VALD)" in md_body
    assert "Nordisk annonsmärkning" in md_body
    assert "Annons" in md_body

    js_fn, js_ct, js_body = svc.export_link_package(pkg["package_id"], fmt="json", actor="jonatan")
    assert js_fn.endswith(".json") and "json" in js_ct
    parsed_json = json.loads(js_body)
    assert parsed_json["selected_vendor_id"] == "whitepress"
    assert len(parsed_json["recommended_vendors"]) == 3
    assert parsed_json["disclosure_checklist"]

    csv_fn, csv_ct, csv_body = svc.export_link_package(pkg["package_id"], fmt="csv", actor="jonatan")
    assert csv_fn.endswith(".csv") and "csv" in csv_ct
    rows = list(csv.DictReader(io.StringIO(csv_body)))
    assert len(rows) == 6
    assert all(r["selected_vendor_id"] == "whitepress" for r in rows)
    assert all(r["selected_vendor_name"] == "WhitePress" for r in rows)

    # Formula injection neutralization & markdown pipe/backtick escaping on vendor fields
    poisoned_pkg = dict(parsed_json)
    poisoned_pkg["recommended_vendors"] = [
        {
            "vendor_id": "whitepress",
            "name": "=CMD|'/C calc'!A0",
            "domain": "whitepress.com`evil",
            "rank": 1,
            "rank_score": 90.0,
            "vetting_score": 95,
            "vetting_status": "preferred",
            "estimated_unit_sek_range": [1200, 5000],
            "guarantee_months": 12,
            "order_workflow_notes": "note|pipe",
        }
    ]
    _, _, safe_csv = render_link_package_brief(poisoned_pkg, fmt="csv")
    safe_rows = list(csv.DictReader(io.StringIO(safe_csv)))
    assert safe_rows[0]["selected_vendor_name"].startswith("'=CMD")

    _, _, safe_md = render_link_package_brief(poisoned_pkg, fmt="md")
    assert "\\|" in safe_md
    assert "\\`evil" in safe_md
