"""Research Bridge & Autonomous SEO Pipeline connecting SEO Studio to Web Search & Vetted Vendors.

Lazy-imports ``ecom_ops.research`` strictly inside functions so ``ecom_ops.seo``
never introduces circular imports at module load time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ecom_ops.audit import log_action
from ecom_ops.seo.models import normalize_market
from ecom_ops.seo.planner import validate_link_package_inputs
from ecom_ops.seo.policy import (
    require_link_package_draft,
    require_seo_propose,
    require_seo_read,
)
from ecom_ops.seo.vendors import (
    NO_AUTO_PURCHASE_NOTICE,
    NORDIC_DISCLOSURE_CHECKLIST,
    match_vendors_for_link_package,
)

_OWN_BRAND_DOMAINS = frozenset({"azom.no", "azom.se", "azom.dk", "mock-shop.example.test"})
_AUTHORITY_DOC_DOMAINS = frozenset(
    {
        "developers.google.com",
        "support.google.com",
        "konsumentverket.se",
        "forbrukertilsynet.no",
        "forbrug.dk",
        "ahrefs.com",
        "semrush.com",
        "moz.com",
    }
)
_MARKET_DEFAULT_TOPIC: dict[str, str] = {
    "no": "trådløs Apple CarPlay skjerm bil",
    "se": "trådlös Apple CarPlay skärm bil",
    "dk": "trådløs Apple CarPlay skærm bil",
}
_MARKET_LOCALE: dict[str, str] = {
    "no": "nb-NO",
    "se": "sv-SE",
    "dk": "da-DK",
}
_RECENT_AUDIT_MAX_AGE = timedelta(hours=6)


def _infer_market_for_site(site_id: str, explicit_market: str | None) -> str:
    if explicit_market is not None and str(explicit_market).strip():
        return normalize_market(explicit_market)
    sid = str(site_id or "").strip().lower()
    if sid.endswith(("-se", "_se", ".se")) or "azom-se" in sid or "sweden" in sid:
        return "se"
    if sid.endswith(("-dk", "_dk", ".dk")) or "azom-dk" in sid or "denmark" in sid:
        return "dk"
    return "no"


def _extract_domain(url: str) -> str:
    host = (urlsplit(str(url or "").strip()).hostname or "").lower().removeprefix("www.")
    return host


def _extract_serp_gaps_and_angles(
    sources: list[dict[str, Any]],
    *,
    topic: str,
    market: str,
) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    competitors: list[str] = []
    gaps: list[str] = []
    angles: list[str] = []
    citations: list[dict[str, Any]] = []

    for src in sources:
        if not isinstance(src, dict):
            continue
        url = str(src.get("canonical_url") or src.get("url") or "").strip()
        domain = str(src.get("domain") or _extract_domain(url)).lower().removeprefix("www.")
        title = str(src.get("title") or "").strip()
        snippet = str(src.get("snippet") or src.get("excerpt") or "").strip()
        provider = str(src.get("provider") or "web_search")

        if url:
            citations.append(
                {
                    "title": title or domain,
                    "url": url,
                    "domain": domain,
                    "snippet": snippet[:240],
                    "provider": provider,
                }
            )
        if domain and domain not in _OWN_BRAND_DOMAINS and domain not in _AUTHORITY_DOC_DOMAINS:
            if domain not in competitors:
                competitors.append(domain)

        combined = f"{title} {snippet}".lower()
        if any(k in combined for k in ("faq", "fråg", "spørsmål", "spørgsmål", "structured data", "json-ld")):
            g = "Lägg till strukturerad FAQPage + komplett Product JSON-LD (SKU, pris, lagerstatus) för rika sökresultat."
            if g not in gaps:
                gaps.append(g)
        if any(k in combined for k in ("canbus", "volvo", "bmw", "passform", "årsmodell", "kompatibilitet", "bilmodel")):
            g = "Bygg tydlig bilmodells- & årsmodellskompatibilitetstabell (Volvo, VW, BMW, Toyota) med 5-minuters DIY-guide."
            if g not in gaps:
                gaps.append(g)
        if any(k in combined for k in ("dab+", "kulde", "ryggekamera", "bakkamera", "garanti")):
            g = "Lyft nordiska köpargument: DAB+/backkamerastöd, stabilitet i nordiskt klimat och tydlig 2–3 års garanti."
            if g not in gaps:
                gaps.append(g)
        if any(k in combined for k in ("2-din", "indbygning", "verksted", "verkstad", "værksted", "plug-and-play")):
            a = f"Jämförelsevinkel ({market.upper()}): {topic} – 5-minuters plug & play vs dyr verkstadsmontering."
            if a not in angles:
                angles.append(a)

    if not gaps:
        gaps.append(
            f"Differentiera {topic} ({market.upper()}) med kompatibilitetsguide, FAQPage JSON-LD och tydlig nordisk garanti."
        )
    if not angles:
        angles.append(
            f"Redaktionell köpguide ({market.upper()}): bästa {topic} med fokus på snabb DIY-montering utan verkstad."
        )
    if len(angles) < 2:
        angles.append(
            f"Teknisk jämförelse ({market.upper()}): trådlös CarPlay-skärm vs adapter – vad passar din bil?"
        )

    return competitors[:8], gaps[:6], angles[:5], citations[:8]


def gather_seo_serp_context(
    site_id: str,
    *,
    topic: str,
    market: str = "no",
    actor: str = "jonatan",
    focus_keywords: list[str] | None = None,
    run_deep_research: bool = False,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Gather PII-free SERP competitor/gap context via ``seo_content_radar`` (mock-first, live-gated).

    - Always runs ``OutboundQueryFirewall.check_query`` fail-closed (PII/secrets raise ``SearchPolicyError``).
    - Gracefully degrades when live web search is disabled or kill-switched.
    - Optionally enqueues an async ``ResearchJob`` with ``target_ref=f"seo:{site_id}"``.
    """
    from ecom_ops.integrations.search.errors import SearchConfigError, SearchRouteError
    from ecom_ops.research import (
        OutboundQueryFirewall,
        SearchPolicyError,
        get_research_service,
    )

    mkt = normalize_market(market)
    clean_topic = " ".join(str(topic or "").strip().split()) or _MARKET_DEFAULT_TOPIC[mkt]
    extra_kws = [
        " ".join(str(k).strip().split())
        for k in (focus_keywords or [])
        if str(k or "").strip()
    ]
    kw_part = f" {' '.join(extra_kws[:2])}" if extra_kws else ""
    market_label = {"no": "Norge", "se": "Sverige", "dk": "Danmark"}[mkt]
    raw_query = f"{clean_topic}{kw_part} {market_label} köpguide SEO".strip()

    fw = OutboundQueryFirewall()
    # Fail-closed on PII, secrets, internal IDs/hosts — NEVER swallow SearchPolicyError
    clean_query = fw.check_query(raw_query)

    svc = get_research_service(data_dir=data_dir)
    try:
        res = svc.quick_search(
            clean_query,
            actor=actor,
            profile_id="seo_content_radar",
            locale=_MARKET_LOCALE[mkt],
            country=mkt.upper(),
            max_results=5,
        )
    except SearchPolicyError:
        raise
    except (SearchConfigError, SearchRouteError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        return {
            "ok": True,
            "degraded": True,
            "status": str(code),
            "profile_id": "seo_content_radar",
            "query": clean_query,
            "market": mkt,
            "evidence_status": "provider_unavailable",
            "provenance": "degraded",
            "provider": "none",
            "competitor_domains": [],
            "content_gaps": [],
            "recommended_angles": [],
            "citations": [],
            "research_job_id": None,
            "note": f"Web SERP research hoppades över ({code}): {str(exc)[:180]}",
        }

    sources = list(res.get("sources") or [])
    competitors, gaps, angles, citations = _extract_serp_gaps_and_angles(
        sources, topic=clean_topic, market=mkt
    )

    research_job_id: str | None = None
    research_note = ""
    if run_deep_research:
        try:
            job = svc.start_research(
                clean_query,
                actor=actor,
                profile_id="seo_content_radar",
                depth="standard",
                target_ref=f"seo:{site_id}",
                markets=[mkt.upper()],
                locale=_MARKET_LOCALE[mkt],
                country=mkt.upper(),
                start_worker=False,
            )
            research_job_id = str(getattr(job, "research_id", "") or "") or None
        except SearchPolicyError:
            raise
        except Exception as exc:
            research_note = f"Async deep research kunde inte köas ({type(exc).__name__}: {str(exc)[:120]})."

    return {
        "ok": True,
        "degraded": False,
        "status": "ok",
        "profile_id": "seo_content_radar",
        "query": clean_query,
        "market": mkt,
        "evidence_status": str(res.get("evidence_status") or "mock"),
        "provenance": str(res.get("provenance") or "mock_fixture"),
        "provider": str(res.get("provider") or "mock"),
        "competitor_domains": competitors,
        "content_gaps": gaps,
        "recommended_angles": angles,
        "citations": citations,
        "research_job_id": research_job_id,
        "note": research_note,
    }


def vet_link_vendors_via_web(
    market: str,
    *,
    actor: str = "jonatan",
    budget_sek: float = 15000.0,
    link_count: int = 6,
    goal: str = "organic_growth",
    brand_url_generic_pct: float = 70.0,
    anchor_guardrail_blocked: bool = False,
    include_web_research: bool = True,
    extra_candidates: list[dict[str, Any]] | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Run bounded ``seo_vendor_vetting`` web search and rank white-hat Nordic link shops."""
    from ecom_ops.integrations.search.errors import SearchConfigError, SearchRouteError
    from ecom_ops.research import (
        OutboundQueryFirewall,
        SearchPolicyError,
        get_research_service,
    )

    mkt = normalize_market(market)
    web_signals: list[dict[str, Any]] = []
    web_vetting_used = False
    web_vetting_status = "curated_only"
    web_provenance = "curated_catalog"

    if include_web_research and not anchor_guardrail_blocked:
        market_label = {"no": "Norway NO", "se": "Sweden SE", "dk": "Denmark DK"}[mkt]
        raw_query = (
            f"white-hat link building marketplace {market_label} Bazoom WhitePress Linkhouse PRNEWS Getfluence Seeders"
        )
        fw = OutboundQueryFirewall()
        clean_query = fw.check_query(raw_query)

        svc = get_research_service(data_dir=data_dir)
        try:
            res = svc.quick_search(
                clean_query,
                actor=actor,
                profile_id="seo_vendor_vetting",
                locale=_MARKET_LOCALE[mkt],
                country=mkt.upper(),
                max_results=7,
            )
            for src in res.get("sources") or []:
                if isinstance(src, dict):
                    web_signals.append(
                        {
                            "url": str(src.get("canonical_url") or src.get("url") or ""),
                            "title": str(src.get("title") or ""),
                            "snippet": str(src.get("snippet") or src.get("excerpt") or ""),
                            "provider": str(src.get("provider") or res.get("provider") or "mock"),
                        }
                    )
            web_vetting_used = bool(web_signals)
            web_vetting_status = "ok"
            web_provenance = str(res.get("provenance") or "mock_fixture")
        except SearchPolicyError:
            raise
        except (SearchConfigError, SearchRouteError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            web_vetting_used = False
            web_vetting_status = f"degraded:{code}"
            web_provenance = "curated_catalog_fallback"

    matched = match_vendors_for_link_package(
        mkt,
        budget_sek,
        link_count,
        goal=goal,
        brand_url_generic_pct=brand_url_generic_pct,
        anchor_guardrail_blocked=anchor_guardrail_blocked,
        web_signals=web_signals or None,
        extra_candidates=extra_candidates,
    )
    matched["web_vetting_used"] = web_vetting_used
    matched["web_vetting_status"] = web_vetting_status
    matched["web_provenance"] = web_provenance
    return matched


def _find_reusable_recent_audit(
    service: Any,
    *,
    site_id: str,
    market: str,
) -> dict[str, Any] | None:
    try:
        recent = service.list_audits(site_id=site_id, limit=5)
    except Exception:
        return None
    now = datetime.now(UTC)
    for row in recent:
        if not isinstance(row, dict):
            continue
        if str(row.get("scope") or "").lower() != "site":
            continue
        if str(row.get("market") or "").lower() != market:
            continue
        raw_ts = str(row.get("created_at") or "").strip()
        if not raw_ts:
            continue
        try:
            dt = datetime.fromisoformat(raw_ts)
            if dt.tzinfo is None:
                continue
            if now - dt.astimezone(UTC) <= _RECENT_AUDIT_MAX_AGE:
                return row
        except ValueError:
            continue
    return None


def run_autonomous_seo_pipeline(
    service: Any,
    *,
    site_id: str,
    actor: str = "jonatan",
    market: str | None = None,
    topic: str = "",
    budget_sek: float = 15000.0,
    link_count: int = 6,
    include_web_research: bool = True,
    include_vendor_vetting: bool = True,
    include_batch_fixes: bool = False,
    start_deep_research: bool = False,
) -> dict[str, Any]:
    """Chain Audit -> SERP Research -> Strategy -> Campaign -> Vetted Link Shops -> Draft Link Package -> Optional Batch Fix Preview.

    STRICT HITL INVARIANTS:
    - Never executes WP/Woo writes (``executed=False``).
    - Never places external link orders or payments (``external_order_placed=False``).
    - Produces an order-ready ``status="draft"`` link package requiring Oscar ``MARKETING_MUTATE`` approval.
    """
    actor_obj = require_seo_read(actor)
    require_seo_propose(actor_obj)
    require_link_package_draft(actor_obj)

    budget, count = validate_link_package_inputs(budget_sek, link_count)
    mkt = _infer_market_for_site(site_id, market)
    site = service._resolve_site(site_id, market=mkt)

    # 1. Site Audit + GSC quick-wins (reuse recent <6h audit or run fresh)
    reused_audit = _find_reusable_recent_audit(service, site_id=site.id, market=mkt)
    if reused_audit is not None:
        audit = dict(reused_audit)
        audit_reused = True
    else:
        audit = service.run_audit(site_id=site.id, actor=actor_obj.name, scope="site", market=mkt)
        audit_reused = False

    gsc_qw = audit.get("gsc_quick_wins") or {}
    gsc_strike = list(gsc_qw.get("strike_distance") or [])
    gsc_keywords = [
        str(r.get("query") or "").strip()
        for r in gsc_strike[:4]
        if str(r.get("query") or "").strip()
    ]

    clean_topic = " ".join(str(topic or "").strip().split())
    if not clean_topic:
        clean_topic = gsc_keywords[0] if gsc_keywords else _MARKET_DEFAULT_TOPIC[mkt]

    # 2. SERP Research Context (seo_content_radar — PII-free via OutboundQueryFirewall)
    if include_web_research:
        serp_ctx = gather_seo_serp_context(
            site.id,
            topic=clean_topic,
            market=mkt,
            actor=actor_obj.name,
            focus_keywords=gsc_keywords[:2],
            run_deep_research=start_deep_research,
            data_dir=service.data_dir,
        )
    else:
        serp_ctx = {
            "ok": True,
            "degraded": False,
            "status": "skipped",
            "profile_id": "seo_content_radar",
            "query": "",
            "market": mkt,
            "evidence_status": "skipped",
            "provenance": "skipped",
            "provider": "none",
            "competitor_domains": [],
            "content_gaps": [],
            "recommended_angles": [],
            "citations": [],
            "research_job_id": None,
            "note": "Web SERP research avstängd via include_web_research=False.",
        }

    # 3. Automated Strategy (seos-*) enriched with audit + GSC + SERP context
    focus_topics = list(dict.fromkeys([clean_topic, *gsc_keywords[:3]]))[:5]
    strat_title = f"Autopilot SEO-strategi ({mkt.upper()}): {clean_topic[:60]}"
    strat_res = service.create_strategy(
        site_id=site.id,
        actor=actor_obj.name,
        title=strat_title,
        market=mkt,
        focus_topics=focus_topics,
        goals=f"Driva lönsam organisk tillväxt för {clean_topic} i {mkt.upper()} (Audit {audit.get('score')}/100).",
        serp_research=serp_ctx,
    )
    strategy = strat_res["strategy"]

    # 4. Automated Campaign (seoc-*) linked to strategy + enriched with SERP context
    camp_name = f"Autopilot SEO-kampanj ({mkt.upper()}): {clean_topic[:55]}"
    camp_res = service.create_campaign(
        site_id=site.id,
        actor=actor_obj.name,
        name=camp_name,
        market=mkt,
        strategy_id=strategy["strategy_id"],
        target_keywords=focus_topics,
        campaign_type="commercial",
        objective=f"Ta topp-3-positioner och öka konvertering för {clean_topic} i {mkt.upper()}.",
        serp_research=serp_ctx,
    )
    campaign = camp_res["campaign"]

    # 5. Vetted Nordic Link-Shop Selection (seo_vendor_vetting)
    if include_vendor_vetting:
        vendor_sel = vet_link_vendors_via_web(
            mkt,
            actor=actor_obj.name,
            budget_sek=budget,
            link_count=count,
            goal="organic_growth",
            include_web_research=include_web_research,
            data_dir=service.data_dir,
        )
    else:
        vendor_sel = {
            "ok": True,
            "blocked": False,
            "block_reason": "",
            "skipped": True,
            "market": mkt,
            "budget_sek": round(budget, 2),
            "link_count": count,
            "unit_sek": round(budget / count, 2),
            "budget_warning": "",
            "recommended_link_count": count,
            "recommended_tier": "",
            "selected_vendor_id": None,
            "candidates": [],
            "rejected_candidates": [],
            "disclosure_checklist": list(NORDIC_DISCLOSURE_CHECKLIST[mkt]),
            "no_auto_purchase_notice": NO_AUTO_PURCHASE_NOTICE,
            "citations": [],
            "web_vetting_used": False,
            "web_vetting_status": "skipped",
            "web_provenance": "skipped",
        }

    # 6. Order-Ready Draft Link Package (seol-*, status="draft", pre-matched to #1 vetted shop)
    campaign_targets: list[dict[str, Any]] = []
    for row in (campaign.get("url_keyword_map") or [])[:4]:
        if isinstance(row, dict) and str(row.get("url") or "").strip():
            campaign_targets.append(
                {
                    "url": str(row["url"]).strip(),
                    "keyword": str(row.get("primary_keyword") or clean_topic).strip(),
                }
            )

    selected_vid = vendor_sel.get("selected_vendor_id")
    recommended_vendors = list(vendor_sel.get("candidates") or [])
    vetting_summary = {
        "recommended_tier": vendor_sel.get("recommended_tier", ""),
        "unit_sek": vendor_sel.get("unit_sek", round(budget / count, 2)),
        "min_white_hat_unit_sek": vendor_sel.get("min_white_hat_unit_sek"),
        "budget_warning": vendor_sel.get("budget_warning", ""),
        "recommended_link_count": vendor_sel.get("recommended_link_count", count),
        "web_vetting_used": bool(vendor_sel.get("web_vetting_used", False)),
        "web_vetting_status": str(vendor_sel.get("web_vetting_status") or ""),
        "disclosure_checklist": list(vendor_sel.get("disclosure_checklist") or NORDIC_DISCLOSURE_CHECKLIST[mkt]),
        "citations": list(vendor_sel.get("citations") or []),
    }

    safe_topic_note = re.sub(r"\s+", " ", clean_topic)[:120]
    pkg_res = service.create_link_package(
        site_id=site.id,
        actor=actor_obj.name,
        name=f"Autopilot Länkpaket ({mkt.upper()}): {safe_topic_note[:50]}",
        market=mkt,
        campaign_id=campaign["campaign_id"],
        budget_sek=budget,
        link_count=count,
        targets=campaign_targets or None,
        notes=(
            f"Genererat av SEO Autopilot från audit {audit['audit_id']}, strategi {strategy['strategy_id']} "
            f"och kampanj {campaign['campaign_id']}. Kräver Oscar-godkännande före manuell beställning."
        ),
        recommended_vendors=recommended_vendors,
        selected_vendor_id=selected_vid,
        vendor_vetting_summary=vetting_summary,
    )
    link_pkg = pkg_res["package"]

    # 7. Optional Batch Fix Preview (seob-*, executed=False — NEVER executes!)
    batch_fix: dict[str, Any] | None = None
    batch_note = ""
    if include_batch_fixes:
        try:
            bres = service.propose_batch_from_audit(
                site.id,
                audit["audit_id"],
                scope="all",
                limit=15,
                actor=actor_obj.name,
            )
            batch_fix = bres.get("batch")
        except ValueError as exc:
            batch_note = str(exc)

    selected_vendor_name = ""
    for cand in recommended_vendors:
        if cand.get("vendor_id") == selected_vid:
            selected_vendor_name = str(cand.get("name") or "")
            break

    log_action(
        actor=actor_obj.name,
        action="seo_autopilot_run",
        target=site.id,
        target_id=link_pkg["package_id"],
        success=True,
        details={
            "market": mkt,
            "audit_id": audit["audit_id"],
            "audit_reused": audit_reused,
            "strategy_id": strategy["strategy_id"],
            "campaign_id": campaign["campaign_id"],
            "package_id": link_pkg["package_id"],
            "selected_vendor_id": selected_vid,
            "batch_id": batch_fix["batch_id"] if isinstance(batch_fix, dict) else None,
            "executed": False,
            "external_order_placed": False,
        },
    )

    return {
        "ok": True,
        "site_id": site.id,
        "market": mkt,
        "topic": clean_topic,
        "executed": False,
        "external_order_placed": False,
        "approval_required": True,
        "audit": audit,
        "serp_research": serp_ctx,
        "strategy": strategy,
        "campaign": campaign,
        "vendor_selection": vendor_sel,
        "link_package": link_pkg,
        "batch_fix": batch_fix,
        "summary": {
            "audit_id": audit["audit_id"],
            "audit_reused": audit_reused,
            "audit_score": audit.get("score"),
            "audit_grade": audit.get("grade"),
            "strategy_id": strategy["strategy_id"],
            "campaign_id": campaign["campaign_id"],
            "package_id": link_pkg["package_id"],
            "package_status": link_pkg["status"],
            "selected_vendor_id": selected_vid,
            "selected_vendor_name": selected_vendor_name,
            "recommended_tier": vendor_sel.get("recommended_tier", ""),
            "budget_warning": vendor_sel.get("budget_warning", ""),
            "batch_id": batch_fix["batch_id"] if isinstance(batch_fix, dict) else None,
            "batch_note": batch_note,
            "serp_competitors_count": len(serp_ctx.get("competitor_domains") or []),
            "serp_gaps_count": len(serp_ctx.get("content_gaps") or []),
            "research_job_id": serp_ctx.get("research_job_id"),
            "executed": False,
            "external_order_placed": False,
            "no_auto_purchase_notice": NO_AUTO_PURCHASE_NOTICE,
        },
    }


__all__ = [
    "gather_seo_serp_context",
    "run_autonomous_seo_pipeline",
    "vet_link_vendors_via_web",
]
