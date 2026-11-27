"""SEO Strategy designer, SEO Campaign designer, and white-hat Link-Package (länkpaket) builder."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from ecom_ops.seo.models import (
    GSC_NOT_CONNECTED_NOTE,
    VALID_CAMPAIGN_TYPES,
    KeywordCluster,
    LinkPackageOrder,
    LinkTargetSpec,
    SeoCampaign,
    SeoStrategy,
    new_seo_id,
    normalize_market,
    utc_now_iso,
)
from ecom_ops.seo.policy import (
    ALT_TEXT_MAX,
    evaluate_anchor_mix,
    is_generic_alt,
    is_generic_filename,
    recommend_natural_anchors,
)
from ecom_ops.seo.vendors import NORDIC_DISCLOSURE_CHECKLIST
from ecom_ops.wp_ops.config import is_mock_mode

LINK_PACKAGE_MIN_BUDGET_SEK = 1000.0
LINK_PACKAGE_MAX_BUDGET_SEK = 500_000.0
LINK_PACKAGE_MIN_COUNT = 1
LINK_PACKAGE_MAX_COUNT = 50

_CSV_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe_cell(val: Any) -> str:
    """Neutralize spreadsheet formula injection (DDE/Excel/Sheets) in CSV cells."""
    if val is None:
        return ""
    text = str(val)
    if not text:
        return ""
    stripped = text.lstrip(" ")
    if text[0] in _CSV_FORMULA_PREFIXES or (stripped and stripped[0] in _CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def _md_table_cell(val: Any) -> str:
    """Escape pipes/backticks and collapse newlines for safe Markdown table cells."""
    if val is None:
        return ""
    text = " ".join(str(val).replace("\r", " ").replace("\n", " ").split())
    return text.replace("|", "\\|").replace("`", "\\`")


def validate_link_package_inputs(budget_sek: Any, link_count: Any) -> tuple[float, int]:
    """Strictly validate finite budget (1000..500000 SEK) and integer link_count (1..50)."""
    if isinstance(budget_sek, bool):
        raise ValueError("budget_sek måste vara ett tal mellan 1000 och 500000 SEK.")
    try:
        budget = float(budget_sek)
    except (TypeError, ValueError) as exc:
        raise ValueError("budget_sek måste vara ett giltigt tal.") from exc
    if not math.isfinite(budget) or budget < LINK_PACKAGE_MIN_BUDGET_SEK or budget > LINK_PACKAGE_MAX_BUDGET_SEK:
        raise ValueError(
            f"budget_sek måste vara ändligt och ligga mellan {int(LINK_PACKAGE_MIN_BUDGET_SEK)} "
            f"och {int(LINK_PACKAGE_MAX_BUDGET_SEK)} SEK."
        )

    if isinstance(link_count, bool):
        raise ValueError("link_count måste vara ett heltal mellan 1 och 50.")
    if isinstance(link_count, float):
        if not math.isfinite(link_count) or not link_count.is_integer():
            raise ValueError("link_count måste vara ett heltal mellan 1 och 50.")
        count = int(link_count)
    else:
        try:
            count = int(link_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("link_count måste vara ett giltigt heltal.") from exc
    if count < LINK_PACKAGE_MIN_COUNT or count > LINK_PACKAGE_MAX_COUNT:
        raise ValueError(
            f"link_count måste vara mellan {LINK_PACKAGE_MIN_COUNT} och {LINK_PACKAGE_MAX_COUNT}."
        )
    return budget, count


_MARKET_META: dict[str, dict[str, str]] = {
    "no": {
        "name": "Norge",
        "domain": "azom.no",
        "language": "nb-NO",
        "language_name": "Norsk (bokmål)",
        "tld": ".no",
        "currency": "NOK",
        "carplay_term": "trådløs Apple CarPlay skjerm",
        "adapter_term": "trådløs CarPlay adapter",
        "android_term": "Android Auto skjerm bil",
        "install_term": "montere CarPlay i eldre bil",
        "best_prefix": "beste",
        "discover_verb": "Oppdag",
        "desc_suffix": "Enkel montering på 5 minutter, 2 års garanti og rask levering. Se priser og modeller her!",
        "h2_what_is": "Hva er {kw} og hvordan fungerer det?",
        "h2_compat": "Kompatible bilmodeller og årsmodeller",
        "h2_steps": "Steg-for-steg: montering på 5 minutter",
        "h2_faq": "Ofte stilte spørsmål (FAQ)",
        "cta_tpl": "Se hele utvalget hos Azom ({domain})",
    },
    "se": {
        "name": "Sverige",
        "domain": "azom.se",
        "language": "sv-SE",
        "language_name": "Svenska",
        "tld": ".se",
        "currency": "SEK",
        "carplay_term": "trådlös Apple CarPlay skärm",
        "adapter_term": "trådlös CarPlay adapter",
        "android_term": "Android Auto skärm bil",
        "install_term": "montera CarPlay i äldre bil",
        "best_prefix": "bästa",
        "discover_verb": "Upptäck",
        "desc_suffix": "Enkel montering på 5 minuter, 2 års garanti och snabb leverans. Se priser och modeller här!",
        "h2_what_is": "Vad är {kw} och hur fungerar det?",
        "h2_compat": "Kompatibla bilmodeller och årsmodeller",
        "h2_steps": "Steg-för-steg: montering på 5 minuter",
        "h2_faq": "Vanliga frågor (FAQ)",
        "cta_tpl": "Se hela sortimentet hos Azom ({domain})",
    },
    "dk": {
        "name": "Danmark",
        "domain": "azom.dk",
        "language": "da-DK",
        "language_name": "Dansk",
        "tld": ".dk",
        "currency": "DKK",
        "carplay_term": "trådløs Apple CarPlay skærm",
        "adapter_term": "trådløs CarPlay adapter",
        "android_term": "Android Auto skærm bil",
        "install_term": "montere CarPlay i ældre bil",
        "best_prefix": "bedste",
        "discover_verb": "Opdag",
        "desc_suffix": "Nem montering på 5 minutter, 2 års garanti og hurtig levering. Se priser og modeller her!",
        "h2_what_is": "Hvad er {kw}, og hvordan fungerer det?",
        "h2_compat": "Kompatible bilmodeller og årgange",
        "h2_steps": "Trin-for-trin: montering på 5 minutter",
        "h2_faq": "Ofte stillede spørgsmål (FAQ)",
        "cta_tpl": "Se hele udvalget hos Azom ({domain})",
    },
}


def _collect_marketing_and_research_signals(
    *,
    actor: str,
    research_brief_id: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Safely read mock/live GA4 landing pages, Ads search terms, and optional Research brief."""
    signals: dict[str, Any] = {
        "ga4_landing_pages": [],
        "ads_waste_terms": [],
        "ads_final_urls": [],
        "research_brief": None,
        "research_summary": "",
        "data_sources": [],
    }
    mock = is_mock_mode()
    try:
        from ecom_ops.integrations import ga4 as ga4_mod

        ga = ga4_mod.client_from_env(use_mock=mock)
        landings = ga.landing_pages(days=30)
        if landings:
            signals["ga4_landing_pages"] = landings[:10]
            signals["data_sources"].append("ga4_landing_pages")
    except Exception:
        pass

    try:
        from ecom_ops.integrations import google_ads as ads_mod

        ads = ads_mod.client_from_env(use_mock=mock)
        waste = ads.waste_report(days=30)
        if waste:
            signals["ads_waste_terms"] = waste[:10]
            signals["data_sources"].append("google_ads_search_terms")
        final_urls = ads.final_urls()
        if final_urls:
            signals["ads_final_urls"] = final_urls[:10]
    except Exception:
        pass

    if research_brief_id:
        try:
            from ecom_ops.research import get_research_service

            svc = get_research_service(data_dir=data_dir)
            brief_bundle = svc.get_brief(str(research_brief_id).strip(), actor=actor)
            brief = brief_bundle.get("brief") if isinstance(brief_bundle, dict) else None
            if isinstance(brief, dict):
                signals["research_brief"] = brief
                summary = str(brief.get("executive_summary") or brief.get("summary") or "").strip()
                signals["research_summary"] = summary[:600]
                signals["data_sources"].append(f"web_research:{research_brief_id}")
        except Exception:
            pass

    return signals


def _classify_search_intent(keyword: str) -> tuple[str, str]:
    low = str(keyword or "").lower()
    if any(w in low for w in ("köp", "kjøp", "køb", "pris", "erbjudande", "tilbud", "beställ", "skärm", "skjerm", "skærm", "adapter")):
        return "transactional", "bofu"
    if any(w in low for w in ("bäst", "bästa", "beste", "bedste", "test", "jämför", "sammenlign", "recension", "volvo", "bmw", "vw", "toyota")):
        return "commercial", "mofu"
    if any(w in low for w in ("azom", "login", "kontakt", "leverans", "garanti")):
        return "navigational", "bofu"
    return "informational", "tofu"


# --------------------------------------------------------------------------- #
# 1. SEO Strategy Designer
# --------------------------------------------------------------------------- #


def build_strategy(
    *,
    site_id: str,
    actor: str,
    title: str,
    market: str = "no",
    focus_topics: list[str] | None = None,
    goals: str = "",
    latest_audit: dict[str, Any] | None = None,
    research_brief_id: str | None = None,
    data_dir: Path | None = None,
    gsc_quick_wins: dict[str, Any] | None = None,
    serp_research: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a structured Swedish/Nordic SEO Strategy combining audit + GSC + GA4/Ads + Research."""
    mkt = normalize_market(market)
    mm = _MARKET_META[mkt]
    clean_title = " ".join(str(title or "").strip().split()) or f"Nordisk SEO-strategi – {mm['name']}"
    gsc_qw = gsc_quick_wins or (
        latest_audit.get("gsc_quick_wins") if isinstance(latest_audit, dict) else None
    )
    gsc_strike = list((gsc_qw or {}).get("strike_distance") or [])
    gsc_low_ctr = list((gsc_qw or {}).get("low_ctr") or [])

    topics = [str(t).strip() for t in (focus_topics or []) if str(t).strip()]
    if not topics:
        gsc_seed = [
            str(r.get("query") or "").strip()
            for r in gsc_strike[:3]
            if str(r.get("query") or "").strip()
        ]
        topics = list(
            dict.fromkeys(
                [
                    *gsc_seed,
                    mm["carplay_term"],
                    mm["adapter_term"],
                    mm["android_term"],
                    mm["install_term"],
                ]
            )
        )[:5]

    signals = _collect_marketing_and_research_signals(
        actor=actor,
        research_brief_id=research_brief_id,
        data_dir=data_dir,
    )
    data_sources = list(dict.fromkeys(["wp_woo_catalog", *signals["data_sources"]]))
    if latest_audit:
        data_sources.insert(0, f"seo_audit:{latest_audit.get('audit_id', 'latest')}")
    if gsc_strike or gsc_low_ctr:
        data_sources.append("google_search_console")
        data_sources = list(dict.fromkeys(data_sources))

    serp_ctx = dict(serp_research or {})
    serp_competitors = list(serp_ctx.get("competitor_domains") or [])
    serp_gaps = list(serp_ctx.get("content_gaps") or [])
    if serp_competitors or serp_gaps or serp_ctx.get("citations"):
        data_sources.append(f"web_serp_research:{serp_ctx.get('profile_id', 'seo_content_radar')}")
        data_sources = list(dict.fromkeys(data_sources))

    audit_score = int(latest_audit.get("score") or 0) if latest_audit else None
    audit_grade = str(latest_audit.get("grade") or "") if latest_audit else ""
    audit_summary = (latest_audit.get("summary") or {}) if latest_audit else {}
    crit_count = int(audit_summary.get("critical", 0))
    high_count = int(audit_summary.get("high", 0))
    missing_alt = int((latest_audit.get("media") or {}).get("missing_alt_count", 0)) if latest_audit else 0

    ga4_lps = signals["ga4_landing_pages"]
    lp_note = (
        f"GA4 visar {len(ga4_lps)} aktiva landningssidor (t.ex. {ga4_lps[0].get('landing_page', '/')}). "
        if ga4_lps
        else ""
    )
    ads_note = (
        f"Google Ads-söktermer ({len(signals['ads_waste_terms'])} identifierade termer) hjälper oss "
        "filtrera bort irrelevant intent och prioritera köpstarka sökord. "
        if signals["ads_waste_terms"]
        else ""
    )
    gsc_summary_note = (
        f"Google Search Console visar {len(gsc_strike)} slagläges-sökord (pos 4–15) "
        f"och {len(gsc_low_ctr)} låg-CTR-möjligheter som prioriteras dag 1–30. "
        if (gsc_strike or gsc_low_ctr)
        else ""
    )
    audit_note = (
        f"Senaste SEO-auditen ({latest_audit.get('audit_id')}) gav poängen {audit_score}/100 ({audit_grade}) "
        f"med {crit_count} kritiska, {high_count} höga fynd och {missing_alt} bilder utan alt-text. "
        if latest_audit
        else "Ingen tidigare SEO-audit hittades ännu — kör en helhetsaudit som dag-1-baslinje. "
    )
    research_note = (
        f"Integrerad Web Research-brief ({research_brief_id}): {signals['research_summary']}"
        if signals["research_summary"]
        else ""
    )
    serp_note = (
        f"SERP-analys ({', '.join(serp_competitors[:3]) or mm['name']}): "
        f"{len(serp_gaps)} identifierade innehållsluckor prioriteras i kluster och köpguider. "
        if (serp_competitors or serp_gaps)
        else ""
    )

    executive_summary = (
        f"Denna 90-dagars SEO-strategi för {mm['name']} ({mm['domain']}) fokuserar på att driva "
        f"lönsam organisk e-handelstrafik inom {', '.join(topics[:3])}. "
        f"{audit_note}{gsc_summary_note}{serp_note}{lp_note}{ads_note}"
        f"Målet är att kombinera teknisk spets (hreflang NO/SE/DK, Product/FAQ JSON-LD), "
        f"konverteringsstarka produkt- och kategorisidor samt naturlig auktoritetstillväxt. "
        f"{research_note}"
    ).strip()

    # Build topic clusters
    clusters: list[dict[str, Any]] = []
    pillar_templates = [
        ("/kategori/apple-carplay/", "P1", "Hög kommersiell konvertering"),
        ("/kategori/carplay-adapter/", "P1", "Hög sökvolym & låg friktion"),
        ("/kategori/android-auto/", "P2", "Breddar målgruppen utanför iOS"),
        ("/guide/montera-carplay/", "P2", "Bygger topikal auktoritet & internlänkar till produkter"),
    ]
    for idx, topic in enumerate(topics[:5]):
        pillar_url, prio, impact = pillar_templates[idx % len(pillar_templates)]
        intent, funnel = _classify_search_intent(topic)
        clusters.append(
            KeywordCluster(
                cluster_name=f"Kluster {idx + 1}: {topic}",
                pillar_url=pillar_url,
                primary_keyword=topic,
                secondary_keywords=[
                    f"{topic} {mm['name'].lower()}",
                    f"{topic} pris",
                    f"{topic} test",
                    f"{mm['best_prefix']} {topic}",
                ],
                search_intent=intent,
                funnel_stage=funnel,
                market=mkt,
                priority=prio,
                estimated_impact=impact,
                content_action=(
                    "Optimera kategorisida + topp-3 produktsidor med H2-köpguide, FAQPage JSON-LD och internlänkar."
                    if funnel in {"bofu", "mofu"}
                    else "Publicera djupgående monterings-/kompatibilitetsguide med produktkort och internlänkar."
                ),
            ).to_dict()
        )

    roadmap_90d: dict[str, list[dict[str, Any]]] = {
        "quick_wins_0_30d": [
            {
                "task": "Åtgärda alla kritiska tekniska fynd (noindex, canonical, robots.txt, hreflang NO/SE/DK)",
                "owner": "Oscar (WP_OPS_ADMIN)",
                "impact": "Hög",
            },
            {
                "task": "Sätt unika SEO-titlar (30–60 tecken), meta descriptions (110–158 tecken) och fokusnyckelord på topp-10 produkter",
                "owner": "Jonatan föreslår -> Oscar godkänner i Change Vault",
                "impact": "Hög",
            },
            {
                "task": "Komplettera saknade & generiska alt-texter i Mediabiblioteket samt aktivera WebP-leverans",
                "owner": "Jonatan föreslår -> Oscar applicerar",
                "impact": "Medel–Hög",
            },
        ],
        "growth_30_60d": [
            {
                "task": "Rulla ut komplett Product + BreadcrumbList + FAQPage JSON-LD med SKU och lagerstatus",
                "owner": "Oscar",
                "impact": "Hög",
            },
            {
                "task": "Utöka tunna produktsidor (<150 ord) och kategorisidor med bilmodell-kompatibilitet och FAQ",
                "owner": "Jonatan + SEO Studio",
                "impact": "Hög",
            },
            {
                "task": "Åtgärda föräldralösa sidor och lösa identifierad sökordskannibalisering via tydlig internlänkning",
                "owner": "Jonatan + Oscar",
                "impact": "Medel–Hög",
            },
        ],
        "authority_60_90d": [
            {
                "task": f"Publicera bilmärkesspecifika guider (Volvo, VW, Toyota, BMW) för {mm['name']} som länkar till köpsidor",
                "owner": "Jonatan",
                "impact": "Hög",
            },
            {
                "task": "Genomför 1–2 white-hat länkpaket med naturlig ankartext-mix (>=60% varumärke/URL/generisk) och månatlig droppplan",
                "owner": "Jonatan designar -> Oscar godkänner beställningsunderlag",
                "impact": "Hög",
            },
            {
                "task": "Koppla Google Search Console (GSC) för full CTR-/positionsuppföljning per marknad",
                "owner": "Oscar",
                "impact": "Medel",
            },
        ],
    }

    action_tracks = {
        "technical": [
            "Säkerställ ömsesidiga hreflang-länkar mellan nb-NO, sv-SE, da-DK samt x-default.",
            "Validera XML-sitemap och robots.txt så inga viktiga produktsidor blockeras.",
            "Övervaka Core Web Vitals (LCP/CLS) genom explicita bilddimensioner och WebP.",
        ],
        "on_page": [
            "Unik SEO-titel (30–60 tecken) och säljande meta description (110–158 tecken) på 100% av publicerade produkter/kategorier.",
            "Minst 250–400 ord unikt, köpstarkt innehåll per huvudprodukt inkl. kompatibilitet och montering.",
            "Eliminera sökordskannibalisering genom 1-sida-per-sökintention-principen.",
        ],
        "media_seo": [
            "100% alt-texttäckning på produkt- och guidebilder med beskrivande bil-/produktkontext.",
            "Byt ut generiska kameranamn (IMG_*, DSC_*) vid nya uppladdningar.",
        ],
        "schema_structured_data": [
            "Product JSON-LD med name, image, sku, brand, offers (price, priceCurrency, availability).",
            "BreadcrumbList på alla kategorier/produkter och FAQPage på köpguider.",
        ],
        "internal_linking": [
            "Noll föräldralösa produkter: varje produkt ska ha minst 3 kontextuella internlänkar.",
            "Ersätt generiska ankartexter ('läs mer') med beskrivande nyckelordsfraser.",
        ],
        "off_page_authority": [
            "Redaktionella gästinlägg och nischguider på nordiska motor-, teknik- och livsstilssajter.",
            "Strikt anti-spam/anti-PBN-policy och Oscar-godkännande av varje länkpaket före manuell beställning.",
        ],
    }

    kpi_targets = [
        {"metric": "Teknisk & On-page SEO-poäng", "baseline": f"{audit_score}/100" if audit_score is not None else "Ej mätt", "target_90d": ">= 88/100 (Betyg B+/A)"},
        {"metric": "SEO-titel & meta description-täckning", "baseline": f"{(latest_audit.get('on_page') or {}).get('meta_title_coverage_pct', 0)}%" if latest_audit else "Ej mätt", "target_90d": "100% på topp-50 URL:er"},
        {"metric": "Alt-texttäckning (Mediabiblioteket)", "baseline": f"{(latest_audit.get('media') or {}).get('alt_coverage_pct', 0)}%" if latest_audit else "Ej mätt", "target_90d": ">= 98%"},
        {"metric": "Föräldralösa produktsidor", "baseline": str((latest_audit.get("links") or {}).get("orphan_count", "Ej mätt")) if latest_audit else "Ej mätt", "target_90d": "0"},
        {"metric": "Organiska landningssidessessioner (GA4)", "baseline": f"{sum(int(x.get('sessions') or 0) for x in ga4_lps)} / 30d" if ga4_lps else "GA4-baslinje", "target_90d": "+35% på 90 dagar"},
    ]

    strategy = SeoStrategy(
        strategy_id=new_seo_id("seos"),
        site_id=site_id,
        market=mkt,
        title=clean_title,
        goals=str(goals or f"Öka lönsam organisk försäljning och synlighet i {mm['name']} inom 90 dagar.").strip(),
        focus_topics=topics,
        executive_summary=executive_summary,
        market_positioning={
            "market_code": mkt,
            "market_name": mm["name"],
            "primary_domain": mm["domain"],
            "language": mm["language"],
            "currency": mm["currency"],
            "nordic_cross_market_note": (
                "Säkerställ konsekvent lokalisering (NO bokmål / SE svenska / DK danska) "
                "och ömsesidiga hreflang-kluster mellan azom.no, azom.se och azom.dk."
            ),
        },
        topic_clusters=clusters,
        roadmap_90d=roadmap_90d,
        action_tracks=action_tracks,
        kpi_targets=kpi_targets,
        data_sources=data_sources,
        gsc_note=GSC_NOT_CONNECTED_NOTE,
        research_brief_id=research_brief_id or None,
        research_summary=signals["research_summary"],
        serp_research=serp_ctx,
        created_by=actor,
    )
    return strategy.to_dict()


# --------------------------------------------------------------------------- #
# 2. SEO Campaign Designer
# --------------------------------------------------------------------------- #


def build_campaign(
    *,
    site_id: str,
    actor: str,
    name: str,
    market: str = "no",
    strategy_id: str | None = None,
    target_keywords: list[str] | None = None,
    target_urls: list[str] | None = None,
    campaign_type: str = "commercial",
    objective: str = "",
    research_brief_id: str | None = None,
    data_dir: Path | None = None,
    gsc_quick_wins: dict[str, Any] | None = None,
    serp_research: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Design an actionable SEO Campaign with intent clusters, URL map, checklists, and briefs."""
    mkt = normalize_market(market)
    mm = _MARKET_META[mkt]
    clean_name = " ".join(str(name or "").strip().split()) or f"SEO-kampanj {mm['name']}"
    ctype = str(campaign_type or "commercial").strip().lower()
    if ctype not in VALID_CAMPAIGN_TYPES:
        raise ValueError(
            f"Unsupported campaign_type {campaign_type!r}. Allowed: {list(VALID_CAMPAIGN_TYPES)}"
        )

    gsc_strike = list((gsc_quick_wins or {}).get("strike_distance") or [])
    kws = list(dict.fromkeys(str(k).strip() for k in (target_keywords or []) if str(k).strip()))
    if not kws:
        gsc_kws = [
            str(r.get("query") or "").strip()
            for r in gsc_strike[:4]
            if str(r.get("query") or "").strip()
        ]
        kws = list(dict.fromkeys([*gsc_kws, mm["carplay_term"], mm["adapter_term"], mm["install_term"]]))[:5]

    urls = list(dict.fromkeys(str(u).strip() for u in (target_urls or []) if str(u).strip()))
    if not urls:
        gsc_urls = [
            str(r.get("page") or "").strip()
            for r in gsc_strike[:3]
            if str(r.get("page") or "").strip()
        ]
        urls = list(
            dict.fromkeys(
                [
                    *gsc_urls,
                    f"https://{mm['domain']}/kategori/apple-carplay/",
                    f"https://{mm['domain']}/produkt/carplay-skarm-pro/",
                    f"https://{mm['domain']}/guide/montera-carplay/",
                ]
            )
        )[:4]

    signals = _collect_marketing_and_research_signals(
        actor=actor,
        research_brief_id=research_brief_id,
        data_dir=data_dir,
    )
    data_sources = list(dict.fromkeys(["wp_woo_catalog", *signals["data_sources"]]))
    if strategy_id:
        data_sources.insert(0, f"seo_strategy:{strategy_id}")
    if gsc_strike:
        data_sources.append("google_search_console")
        data_sources = list(dict.fromkeys(data_sources))

    serp_ctx = dict(serp_research or {})
    serp_gaps = list(serp_ctx.get("content_gaps") or [])
    serp_competitors = list(serp_ctx.get("competitor_domains") or [])
    if serp_gaps or serp_competitors or serp_ctx.get("citations"):
        data_sources.append(f"web_serp_research:{serp_ctx.get('profile_id', 'seo_content_radar')}")
        data_sources = list(dict.fromkeys(data_sources))

    keyword_clusters: list[dict[str, Any]] = []
    url_keyword_map: list[dict[str, Any]] = []
    schema_plan: list[dict[str, Any]] = []
    content_briefs: list[dict[str, Any]] = []
    internal_linking_plan: list[dict[str, Any]] = []
    url_map_index: dict[str, int] = {}

    for idx, kw in enumerate(kws):
        mapped_url = urls[idx % len(urls)]
        intent, funnel = _classify_search_intent(kw)
        rec_title = f"{kw.capitalize()} – Plug & Play | Azom"[:60]
        rec_desc = f"{mm['discover_verb']} {kw} hos Azom ({mm['name']}). {mm['desc_suffix']}"[:158]

        keyword_clusters.append(
            {
                "primary_keyword": kw,
                "secondary_keywords": [f"{kw} pris", f"{kw} test", f"{kw} bil"],
                "search_intent": intent,
                "funnel_stage": funnel,
                "mapped_url": mapped_url,
                "priority": "P1" if idx < 2 else "P2",
            }
        )
        # Avoid duplicate URL entries/briefs (keyword cannibalization) when len(kws) > len(urls)
        if mapped_url in url_map_index:
            existing_entry = url_keyword_map[url_map_index[mapped_url]]
            if kw not in existing_entry["secondary_keywords"] and kw != existing_entry["primary_keyword"]:
                existing_entry["secondary_keywords"].append(kw)
            continue

        url_map_index[mapped_url] = len(url_keyword_map)
        url_keyword_map.append(
            {
                "url": mapped_url,
                "primary_keyword": kw,
                "secondary_keywords": [f"{kw} {mm['name'].lower()}", f"{mm['best_prefix']} {kw}"],
                "intent": intent,
                "recommended_title": rec_title,
                "recommended_meta_description": rec_desc,
            }
        )
        is_product = "/produkt/" in mapped_url or "/product/" in mapped_url
        schema_types = (
            ["Product", "BreadcrumbList", "FAQPage"]
            if is_product
            else ["BreadcrumbList", "FAQPage", "Article" if "/guide/" in mapped_url else "CollectionPage"]
        )
        schema_plan.append(
            {
                "url": mapped_url,
                "required_schemas": schema_types,
                "notes": "Inkludera SKU, pris i " + mm["currency"] + " och lagerstatus." if is_product else "Inkludera minst 3–5 vanliga kundfrågor i FAQPage.",
            }
        )
        content_briefs.append(
            {
                "url": mapped_url,
                "target_keyword": kw,
                "h1": kw.capitalize(),
                "h2_outline": [
                    mm["h2_what_is"].format(kw=kw),
                    mm["h2_compat"],
                    mm["h2_steps"],
                    mm["h2_faq"],
                ],
                "target_word_count": 450 if is_product else 900,
                "cta": mm["cta_tpl"].format(domain=mm["domain"]),
                **({"serp_content_gaps": serp_gaps[:3], "competitor_references": serp_competitors[:4]} if (serp_gaps or serp_competitors) else {}),
            }
        )

    # Internal linking plan linking guides/categories -> product URLs
    if len(urls) >= 2:
        for i in range(len(urls)):
            src = urls[i]
            dst = urls[(i + 1) % len(urls)]
            if src != dst:
                internal_linking_plan.append(
                    {
                        "from_url": src,
                        "to_url": dst,
                        "recommended_anchor": kws[(i + 1) % len(kws)],
                        "placement": "Kontextuell länk i brödtext / relaterad sektion",
                    }
                )

    on_page_checklist = [
        {"step": "Uppdatera SEO-titel (30–60 tecken) & meta description (110–158 tecken) via seo_meta-fixplan", "area": "on_page", "status": "todo"},
        {"step": "Säkerställ exakt en H1 + tydlig H2/H3-struktur som täcker kompatibilitet & montering", "area": "content", "status": "todo"},
        {"step": "Komplettera beskrivande alt-texter (5–125 tecken) för samtliga kampanjbilder", "area": "media", "status": "todo"},
        {"step": "Validera Product/BreadcrumbList/FAQPage JSON-LD efter publicering", "area": "schema", "status": "todo"},
        {"step": "Lägg in 2–4 kontextuella internlänkar från relaterade guider och kategorier", "area": "links", "status": "todo"},
        {"step": "Verifiera hreflang-reciprocitet mellan NO, SE och DK", "area": "technical", "status": "todo"},
    ]

    kpi_milestones = [
        {"horizon": "Dag 14", "target": "Alla on-page-, alt-text- och schema-fixplaner godkända och applicerade via Change Vault."},
        {"horizon": "Dag 30", "target": "100% indexeringsstatus utan tekniska fel; +15% organiska sidvisningar på kampanj-URL:er."},
        {"horizon": "Dag 60", "target": "Minst 3–5 nya white-hat auktoritetslänkar live enligt droppplan; +25% organisk trafik."},
        {"horizon": "Dag 90", "target": "Topp-5-placeringar på primära långsvanssökord och mätbar ökning av organiska köp i GA4/Woo."},
    ]

    campaign = SeoCampaign(
        campaign_id=new_seo_id("seoc"),
        site_id=site_id,
        market=mkt,
        name=clean_name,
        campaign_type=ctype,
        objective=str(objective or f"Öka synlighet och konvertering för {', '.join(kws[:3])} i {mm['name']}.").strip(),
        strategy_id=strategy_id or None,
        research_brief_id=research_brief_id or None,
        keyword_clusters=keyword_clusters,
        url_keyword_map=url_keyword_map,
        on_page_checklist=on_page_checklist,
        internal_linking_plan=internal_linking_plan,
        schema_plan=schema_plan,
        content_briefs=content_briefs,
        kpi_milestones=kpi_milestones,
        data_sources=data_sources,
        gsc_note=GSC_NOT_CONNECTED_NOTE,
        serp_research=serp_ctx,
        status="draft",
        created_by=actor,
    )
    return campaign.to_dict()


# --------------------------------------------------------------------------- #
# 3. Link-Package (Länkpaket) Order Builder & Exporter
# --------------------------------------------------------------------------- #


def build_link_package(
    *,
    site_id: str,
    actor: str,
    name: str,
    market: str = "no",
    campaign_id: str | None = None,
    budget_sek: float = 15000.0,
    link_count: int = 6,
    targets: list[dict[str, Any]] | None = None,
    notes: str = "",
    base_url: str = "",
    recommended_vendors: list[dict[str, Any]] | None = None,
    selected_vendor_id: str | None = None,
    vendor_vetting_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a white-hat, supplier-ready Link-Package order brief with anchor-mix guardrails."""
    mkt = normalize_market(market)
    mm = _MARKET_META[mkt]
    clean_name = " ".join(str(name or "").strip().split()) or f"Länkpaket {mm['name']}"
    budget, count = validate_link_package_inputs(budget_sek, link_count)

    resolved_targets = recommend_natural_anchors(
        targets,
        market=mkt,
        link_count=count,
        brand_name="Azom",
        base_url=base_url or f"https://{mm['domain']}",
    )
    mix_report = evaluate_anchor_mix(
        resolved_targets,
        market=mkt,
        link_count=len(resolved_targets),
        notes=notes,
        name=clean_name,
    )

    actual_count = len(resolved_targets)
    avg_cost = round(budget / actual_count, 2) if actual_count else 0.0
    # Tier DR target reasonably by budget per link
    base_min_dr = 40 if avg_cost >= 4000 else (30 if avg_cost >= 2200 else 25)
    total_weeks = max(4, min(12, math.ceil(actual_count / 1.5) * 2))

    article_angles = {
        "no": (
            "Slik oppgraderer du en eldre bil med trådløs Apple CarPlay uten verksted",
            "Test og kjøpsguide: beste biltilbehør for tryggere navigasjon på norske veier",
            "Smart biltur i Norge – teknologien som gjør kjøreturen enklere",
            "Android Auto vs Apple CarPlay i ettermontert skjerm – hva bør du velge?",
        ),
        "se": (
            "Så uppgraderar du en äldre bil med trådlös Apple CarPlay utan verkstad",
            "Köpguide: bästa biltillbehören för smartare navigering och handsfree",
            "Bilsemester i Sverige – tekniken som lyfter körupplevelsen",
            "Android Auto vs Apple CarPlay i eftermonterad skärm – stor jämförelse",
        ),
        "dk": (
            "Sådan opgraderer du en ældre bil med trådløs Apple CarPlay på 5 minutter",
            "Købsguide: det bedste biltilbehør til sikker navigation i Danmark",
            "Kør-selv-ferie – bilteknologi der gør turen nemmere og sikrere",
            "Android Auto vs Apple CarPlay i eftermonteret skærm – hvad skal du vælge?",
        ),
    }[mkt]

    placements: list[dict[str, Any]] = []
    drip_buckets: dict[int, list[int]] = {}
    for idx, row in enumerate(resolved_targets, start=1):
        if actual_count <= 1:
            week = 1
        else:
            week = 1 + math.floor(((idx - 1) / (actual_count - 1)) * (total_weeks - 1))
        week = max(1, min(total_weeks, week))
        drip_buckets.setdefault(week, []).append(idx)
        placement_type = (
            "editorial_guest_article"
            if row["anchor_type"] in {"partial_match", "exact_match"}
            else ("digital_pr_guide" if idx % 2 == 0 else "niche_resource_mention")
        )
        rel_guidance = (
            "dofollow (redaktionell kontextuell länk i brödtext)"
            if idx % 5 != 0
            else "natural mix (dofollow eller rel='sponsored'/nofollow om publicisten kräver det)"
        )
        spec = LinkTargetSpec(
            index=idx,
            target_url=row["target_url"],
            anchor_text=row["anchor_text"],
            anchor_type=row["anchor_type"],
            keyword=row["keyword"],
            article_angle=article_angles[(idx - 1) % len(article_angles)],
            target_week=week,
            min_dr=base_min_dr + (5 if row["anchor_type"] in {"partial_match", "exact_match"} else 0),
            language=mm["language"],
            tld_preference=mm["tld"],
            rel_guidance=rel_guidance,
            placement_type=placement_type,
            estimated_cost_sek=avg_cost,
        )
        placements.append(spec.to_dict())

    drip_plan = [
        {
            "week": wk,
            "links_scheduled": len(idxs),
            "placement_indices": idxs,
            "velocity_note": "Jämn, naturlig publiceringstakt utan länkspikar.",
        }
        for wk, idxs in sorted(drip_buckets.items())
    ]

    quality_criteria = {
        "min_dr": base_min_dr,
        "min_organic_traffic_monthly": 500,
        "required_language": mm["language_name"],
        "preferred_tld": mm["tld"],
        "topical_relevance": ["bil & motor", "teknik & prylar", "konsument & livsstil", "e-handel & resor"],
        "placement_rules": [
            "Endast kontextuell länk inbäddad i unik, välskriven redaktionell artikel (>= 600 ord).",
            "Max 1–2 utgående kommersiella länkar per artikel.",
            "Inga footer-, sidebar-, bloggroll- eller sitewide-länkar.",
            "Artikeln ska vara indexerad (index, follow) och permanent (minst 12–24 månaders garanti).",
        ],
        "anti_pbn_checklist": [
            "Ingen PBN (Private Blog Network) eller delade IP-/footprint-nätverk.",
            "Verklig organisk trafik i Ahrefs/Semrush (ej döda eller spammade utgångna domäner).",
            "Inga kasino-, lån-, vuxen- eller masspublicerade länkfarm-kategorier på publicistdomänen.",
            "Unik design, tydlig redaktionell avsändare och lokal nordisk relevans.",
        ],
    }

    cost_breakdown = {
        "total_budget_sek": round(budget, 2),
        "link_count": actual_count,
        "avg_cost_per_link_sek": avg_cost,
        "currency": "SEK",
        "note": "Endast estimat och beställningsunderlag — inga automatiska köp eller betalningar utförs.",
    }

    now = utc_now_iso()
    vet_summary = dict(vendor_vetting_summary or {})
    rec_vendors = [dict(v) for v in (recommended_vendors or []) if isinstance(v, dict)]
    sel_vendor_id = str(selected_vendor_id or "").strip().lower() or None
    if not sel_vendor_id and rec_vendors:
        sel_vendor_id = str(rec_vendors[0].get("vendor_id") or "").strip().lower() or None

    warnings_list = list(mix_report.warnings)
    budget_warn = str(vet_summary.get("budget_warning") or "").strip()
    if budget_warn and budget_warn not in warnings_list:
        warnings_list.append(budget_warn)

    pkg = LinkPackageOrder(
        package_id=new_seo_id("seol"),
        site_id=site_id,
        market=mkt,
        name=clean_name,
        campaign_id=campaign_id or None,
        status="draft",
        budget_sek=round(budget, 2),
        link_count=actual_count,
        targets=placements,
        anchor_mix=mix_report.to_dict(),
        quality_criteria=quality_criteria,
        drip_plan=drip_plan,
        cost_breakdown=cost_breakdown,
        guardrail_warnings=warnings_list,
        guardrail_blocked=bool(mix_report.guardrail_blocked),
        blocking_reasons=list(mix_report.blocking_reasons),
        brief_markdown="",
        notes=str(notes or "").strip(),
        recommended_vendors=rec_vendors,
        selected_vendor_id=sel_vendor_id,
        vendor_vetting_summary=vet_summary,
        history=[
            {
                "from_status": None,
                "to_status": "draft",
                "actor": actor,
                "note": "Länkpaket skapat i SEO Studio",
                "at": now,
            }
        ],
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    pkg_dict = pkg.to_dict()
    _fn, _ct, md_body = render_link_package_brief(pkg_dict, fmt="md")
    pkg_dict["brief_markdown"] = md_body
    return pkg_dict


def render_link_package_brief(
    pkg: dict[str, Any],
    *,
    fmt: str = "md",
) -> tuple[str, str, str]:
    """Render a supplier-ready link-package brief as ``(filename, content_type, body)``."""
    norm_fmt = str(fmt or "md").strip().lower()
    pid = str(pkg.get("package_id") or "seol-brief")
    raw_mkt = str(pkg.get("market") or "no").strip().lower()
    mkt = raw_mkt if raw_mkt in NORDIC_DISCLOSURE_CHECKLIST else "no"
    vet_summary = pkg.get("vendor_vetting_summary") if isinstance(pkg.get("vendor_vetting_summary"), dict) else {}
    rec_vendors = [v for v in (pkg.get("recommended_vendors") or []) if isinstance(v, dict)]
    sel_vid = str(pkg.get("selected_vendor_id") or "").strip().lower()
    selected_vendor = next(
        (v for v in rec_vendors if str(v.get("vendor_id") or "").strip().lower() == sel_vid),
        rec_vendors[0] if rec_vendors else None,
    )
    sel_vendor_name = str((selected_vendor or {}).get("name") or "")
    disclosure_checklist = list(
        vet_summary.get("disclosure_checklist") or NORDIC_DISCLOSURE_CHECKLIST[mkt]
    )

    if norm_fmt == "json":
        export_obj = {k: v for k, v in pkg.items() if k != "brief_markdown"}
        export_obj.setdefault("disclosure_checklist", disclosure_checklist)
        return (
            f"{pid}.json",
            "application/json; charset=utf-8",
            json.dumps(export_obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        )

    if norm_fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "package_id",
                "market",
                "selected_vendor_id",
                "selected_vendor_name",
                "index",
                "target_url",
                "anchor_text",
                "anchor_type",
                "keyword",
                "article_angle",
                "target_week",
                "min_dr",
                "language",
                "tld_preference",
                "placement_type",
                "rel_guidance",
                "estimated_cost_sek",
            ]
        )
        for t in pkg.get("targets") or []:
            writer.writerow(
                [
                    _csv_safe_cell(pid),
                    _csv_safe_cell(pkg.get("market", "")),
                    _csv_safe_cell(sel_vid),
                    _csv_safe_cell(sel_vendor_name),
                    _csv_safe_cell(t.get("index", "")),
                    _csv_safe_cell(t.get("target_url", "")),
                    _csv_safe_cell(t.get("anchor_text", "")),
                    _csv_safe_cell(t.get("anchor_type", "")),
                    _csv_safe_cell(t.get("keyword", "")),
                    _csv_safe_cell(t.get("article_angle", "")),
                    _csv_safe_cell(t.get("target_week", "")),
                    _csv_safe_cell(t.get("min_dr", "")),
                    _csv_safe_cell(t.get("language", "")),
                    _csv_safe_cell(t.get("tld_preference", "")),
                    _csv_safe_cell(t.get("placement_type", "")),
                    _csv_safe_cell(t.get("rel_guidance", "")),
                    _csv_safe_cell(t.get("estimated_cost_sek", "")),
                ]
            )
        return (f"{pid}.csv", "text/csv; charset=utf-8", buf.getvalue())

    if norm_fmt != "md":
        raise ValueError(f"Unsupported export format {fmt!r}. Allowed: md, json, csv")

    mix = pkg.get("anchor_mix") or {}
    pcts = mix.get("percentages") or {}
    qc = pkg.get("quality_criteria") or {}
    raw_budget = pkg.get("budget_sek")
    try:
        safe_budget = float(raw_budget) if raw_budget is not None else 0.0
        if not math.isfinite(safe_budget):
            safe_budget = 0.0
    except (TypeError, ValueError):
        safe_budget = 0.0
    lines: list[str] = [
        f"# Länkpaket – Beställningsunderlag: {pkg.get('name', pid)}",
        "",
        f"- **Paket-ID:** `{pid}`",
        f"- **Webbplats:** `{pkg.get('site_id', '')}` · **Marknad:** `{str(pkg.get('market', '')).upper()}`",
        f"- **Status:** `{pkg.get('status', 'draft')}`",
        f"- **Budget:** {safe_budget:.0f} SEK · **Antal länkar:** {pkg.get('link_count', 0)}",
        f"- **Guardrail-status:** `{mix.get('status', 'unknown')}`"
        + (" (BLOCKERAD – åtgärda innan godkännande)" if pkg.get("guardrail_blocked") else ""),
    ]
    if selected_vendor:
        lines.append(
            f"- **Vald granskad länkleverantör:** {selected_vendor.get('name', sel_vid)} "
            f"(`{selected_vendor.get('domain', '')}`, poäng {selected_vendor.get('rank_score', '')}/100, "
            f"status `{selected_vendor.get('vetting_status', 'preferred')}`)"
        )
    lines.extend(
        [
            "",
            "> **Viktigt:** Detta är endast ett manuellt beställningsunderlag för leverantör. "
            "Inga externa köp eller betalningar utförs automatiskt.",
            "",
            "## Ankartext-mix (Naturlig profil)",
            "",
            f"- **Varumärke (`brand`):** {pcts.get('brand', 0)}%",
            f"- **Naken URL (`naked_url`):** {pcts.get('naked_url', 0)}%",
            f"- **Generisk (`generic`):** {pcts.get('generic', 0)}%",
            f"- **Naturlig bas totalt:** {mix.get('brand_url_generic_pct', 0)}% (mål >= 60%, hård gräns >= 35%)",
            f"- **Delvis match (`partial_match`):** {pcts.get('partial_match', 0)}% (mål <= 25%)",
            f"- **Exakt match (`exact_match`):** {pcts.get('exact_match', 0)}% (mål <= 10%, hård gräns <= 20%)",
            "",
        ]
    )
    if pkg.get("blocking_reasons"):
        lines.append("### Blockerande guardrails")
        for r in pkg["blocking_reasons"]:
            lines.append(f"- [BLOCK] {r}")
        lines.append("")
    if pkg.get("guardrail_warnings"):
        lines.append("### Varningar")
        for w in pkg["guardrail_warnings"]:
            lines.append(f"- [VARNING] {w}")
        lines.append("")

    if rec_vendors:
        tier_label = str(vet_summary.get("recommended_tier") or "self_serve_marketplace")
        lines.extend(
            [
                "## Rekommenderade granskade länkleverantörer (Topp 3)",
                "",
                f"- **Rekommenderad kampanjnivå:** `{tier_label}`",
            ]
        )
        if selected_vendor:
            lines.append(
                f"- **Primärt val för manuell beställning:** **{_md_table_cell(selected_vendor.get('name'))}** "
                f"(`{_md_table_cell(selected_vendor.get('domain'))}`) — "
                f"{_md_table_cell(selected_vendor.get('order_workflow_notes'))}"
            )
        lines.extend(
            [
                "",
                "| Rank | Leverantör | Domän | Status | Rankpoäng | Vetting | Est. SEK/länk | Garanti |",
                "|------|------------|-------|--------|-----------|---------|---------------|---------|",
            ]
        )
        for v in rec_vendors[:3]:
            unit_rng = v.get("estimated_unit_sek_range") or [0, 0]
            rng_txt = f"{unit_rng[0]}–{unit_rng[1]}" if len(unit_rng) >= 2 else ""
            marker = " (VALD)" if str(v.get("vendor_id") or "").lower() == sel_vid else ""
            lines.append(
                f"| #{_md_table_cell(v.get('rank', ''))} | {_md_table_cell(v.get('name'))}{marker} | "
                f"`{_md_table_cell(v.get('domain'))}` | `{_md_table_cell(v.get('vetting_status'))}` | "
                f"{_md_table_cell(v.get('rank_score'))} | {_md_table_cell(v.get('vetting_score'))}/100 | "
                f"{_md_table_cell(rng_txt)} SEK | {_md_table_cell(v.get('guarantee_months', 12))} mån |"
            )
        lines.append("")

    lines.extend(
        [
            "## Länkspecifikation per placering",
            "",
            "| # | Vecka | Mål-URL | Ankartext | Typ | Min DR | Artikelvinkel | Est. SEK |",
            "|---|-------|---------|-----------|-----|--------|---------------|----------|",
        ]
    )
    for t in pkg.get("targets") or []:
        lines.append(
            f"| {_md_table_cell(t.get('index'))} | v.{_md_table_cell(t.get('target_week'))} | "
            f"{_md_table_cell(t.get('target_url'))} | {_md_table_cell(t.get('anchor_text'))} | "
            f"`{_md_table_cell(t.get('anchor_type'))}` | DR {_md_table_cell(t.get('min_dr'))}+ | "
            f"{_md_table_cell(t.get('article_angle'))} | {_md_table_cell(t.get('estimated_cost_sek'))} |"
        )
    lines.extend(
        [
            "",
            "## Kvalitets- & Anti-PBN-krav",
            "",
            f"- **Språk / TLD:** {qc.get('required_language', '')} (`{qc.get('preferred_tld', '')}`)",
            f"- **Min DR:** {qc.get('min_dr', 25)}+ · **Min organisk trafik:** {qc.get('min_organic_traffic_monthly', 500)}/mån",
            f"- **Relevanta nischer:** {', '.join(qc.get('topical_relevance') or [])}",
        ]
    )
    for rule in qc.get("placement_rules") or []:
        lines.append(f"- {rule}")
    lines.append("")
    lines.append("### Anti-PBN-checklista")
    for chk in qc.get("anti_pbn_checklist") or []:
        lines.append(f"- [ ] {chk}")
    lines.append("")
    lines.append("### Nordisk annonsmärkning & juridisk compliance")
    for item in disclosure_checklist:
        lines.append(f"- [ ] {item}")
    if pkg.get("notes"):
        lines.extend(["", "## Anteckningar", "", str(pkg["notes"])])
    lines.append("")
    return (f"{pid}.md", "text/markdown; charset=utf-8", "\n".join(lines))


# --------------------------------------------------------------------------- #
# 4. Batch Fix-Plan Proposal Drafter (Proposal 1 — Localized SE/NO/DK)
# --------------------------------------------------------------------------- #

_BATCH_META_LOCALE: dict[str, dict[str, str]] = {
    "no": {
        "brand_suffix": " | Azom",
        "title_addon": " – Trådløs CarPlay",
        "desc_tpl": "Oppdag {title} hos Azom. Enkel plug & play-montering på 5 min, 2 års garanti og rask levering i Norge.",
        "alt_suffix": " – Azom produktbilde",
        "alt_fallback": "Azom trådløs Apple CarPlay-skjerm montert i bil",
    },
    "se": {
        "brand_suffix": " | Azom",
        "title_addon": " – Trådlös CarPlay",
        "desc_tpl": "Upptäck {title} hos Azom. Enkel plug & play-montering på 5 min, 2 års garanti och snabb leverans i Sverige.",
        "alt_suffix": " – Azom produktbild",
        "alt_fallback": "Azom trådlös Apple CarPlay-skärm monterad i bil",
    },
    "dk": {
        "brand_suffix": " | Azom",
        "title_addon": " – Trådløs CarPlay",
        "desc_tpl": "Opdag {title} hos Azom. Nem plug & play-montering på 5 min, 2 års garanti og hurtig levering i Danmark.",
        "alt_suffix": " – Azom produktbillede",
        "alt_fallback": "Azom trådløs Apple CarPlay-skærm monteret i bil",
    },
}


def _fit_text(text: str, max_len: int) -> str:
    clean = " ".join(str(text or "").strip().split())
    if len(clean) <= max_len:
        return clean
    clipped = clean[:max_len].rstrip(" -–|,.:;")
    if " " in clipped and len(clipped) > max_len - 12:
        clipped = clipped.rsplit(" ", 1)[0].rstrip(" -–|,.:;")
    return clipped[:max_len]


def _localized_meta_title(ent: dict[str, Any], *, market: str) -> str:
    loc = _BATCH_META_LOCALE[market]
    base_title = " ".join(str(ent.get("title") or ent.get("slug") or "Azom CarPlay").strip().split())
    brand = loc["brand_suffix"]
    addon = loc["title_addon"]
    # Try title + addon + brand if <= 60 and base_title is short
    if len(base_title) < 30 and len(f"{base_title}{addon}{brand}") <= 60:
        return f"{base_title}{addon}{brand}"
    if brand.strip().lower() in base_title.lower():
        return _fit_text(base_title, 60)
    max_base = 60 - len(brand)
    return f"{_fit_text(base_title, max_base)}{brand}"


def _localized_meta_desc(ent: dict[str, Any], *, market: str) -> str:
    loc = _BATCH_META_LOCALE[market]
    title = " ".join(str(ent.get("title") or "Azom CarPlay").strip().split())
    excerpt = " ".join(re.sub(r"<[^>]+>", " ", str(ent.get("excerpt") or "")).split()).strip()
    if excerpt and len(excerpt) >= 60:
        candidate = f"{excerpt.rstrip('.')} – {_fit_text(loc['desc_tpl'].format(title=title), 90)}"
        if len(candidate) <= 155:
            return candidate
    return _fit_text(loc["desc_tpl"].format(title=title), 155)


def _localized_media_alt(
    media_item: dict[str, Any],
    *,
    market: str,
    entity_by_id: dict[str, dict[str, Any]],
) -> str:
    loc = _BATCH_META_LOCALE[market]
    title = " ".join(str(media_item.get("title") or "").strip().split())
    attached_id = str(media_item.get("attached_post_id") or "").strip()
    attached_ent = entity_by_id.get(attached_id)
    attached_title = " ".join(str((attached_ent or {}).get("title") or "").strip().split())
    filename = str(media_item.get("filename") or "").rsplit(".", 1)[0]
    cleaned_file = " ".join(re.sub(r"[-_]+", " ", filename).split()).strip()

    if title and not is_generic_alt(title) and not is_generic_filename(title):
        alt = f"{title}{loc['alt_suffix']}"
    elif attached_title and not is_generic_alt(attached_title):
        alt = f"{attached_title}{loc['alt_suffix']}"
    elif cleaned_file and not is_generic_filename(filename) and not is_generic_alt(cleaned_file):
        alt = f"{cleaned_file.capitalize()}{loc['alt_suffix']}"
    else:
        alt = loc["alt_fallback"]

    alt = _fit_text(alt, ALT_TEXT_MAX)
    if is_generic_alt(alt) or len(alt) < 5:
        alt = _fit_text(loc["alt_fallback"], ALT_TEXT_MAX)
    return alt


def draft_batch_fix_items(
    audit: dict[str, Any],
    entities: list[dict[str, Any]] | None,
    media: list[dict[str, Any]] | None,
    *,
    market: str = "no",
    scope: str = "all",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Draft localized SE/NO/DK ``media_alt`` and ``seo_meta`` fix items from an audit report."""
    mkt = normalize_market(market)
    clean_scope = str(scope or "all").strip().lower()
    if clean_scope == "media":
        clean_scope = "media_alt"
    elif clean_scope in {"on_page", "meta"}:
        clean_scope = "seo_meta"
    if clean_scope not in {"all", "media_alt", "seo_meta"}:
        raise ValueError(f"Unsupported batch scope {scope!r}. Allowed: all, media_alt, seo_meta")
    cap = max(1, min(int(limit or 20), 100))

    ent_map: dict[tuple[str, str], dict[str, Any]] = {}
    ent_by_id: dict[str, dict[str, Any]] = {}
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        otype = str(ent.get("object_type") or "page").strip().lower()
        oid = str(ent.get("object_id") or "").strip()
        if oid:
            ent_map[(otype, oid)] = ent
            ent_by_id.setdefault(oid, ent)

    media_map: dict[str, dict[str, Any]] = {}
    for m in media or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("media_id") or m.get("id") or "").strip()
        if mid:
            media_map[mid] = m

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _ensure_item(
        fix_type: str,
        target_type: str,
        target_id: str,
        url: str,
        issue_code: str,
    ) -> dict[str, Any]:
        key = (fix_type, target_type, target_id)
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "fix_type": fix_type,
                "target_type": target_type,
                "target_id": target_id,
                "url": url,
                "summary": "",
                "issue_codes": [],
                "proposed": {},
            }
            grouped[key] = entry
        if issue_code and issue_code not in entry["issue_codes"]:
            entry["issue_codes"].append(issue_code)
        if url and not entry["url"]:
            entry["url"] = url
        return entry

    for iss in (audit or {}).get("issues") or []:
        if not isinstance(iss, dict):
            continue
        code = str(iss.get("code") or "").strip()
        ttype = str(iss.get("target_type") or "").strip().lower()
        tid = str(iss.get("target_id") or "").strip()
        url = str(iss.get("url") or "").strip()
        if not tid.isdigit() or int(tid) <= 0:
            continue

        # Media alt fixes
        if clean_scope in {"all", "media_alt"} and ttype == "media" and code in {
            "media_missing_alt",
            "media_generic_alt",
            "media_alt_length",
            "media_duplicate_alt",
        }:
            m_obj = media_map.get(tid, {"media_id": int(tid), "filename": "", "title": ""})
            alt_text = _localized_media_alt(m_obj, market=mkt, entity_by_id=ent_by_id)
            item = _ensure_item("media_alt", "media", tid, url or str(m_obj.get("url") or ""), code)
            item["proposed"]["alt_text"] = alt_text
            item["summary"] = f"Alt-text ({mkt.upper()}) för bild #{tid}"
            continue

        # Entity SEO meta fixes
        if clean_scope in {"all", "seo_meta"} and ttype in {"product", "page", "post"} and code in {
            "missing_meta_title",
            "meta_title_too_short",
            "meta_title_too_long",
            "missing_meta_description",
            "meta_desc_length",
            "missing_focus_keyphrase",
            "published_noindex",
        }:
            ent = ent_map.get((ttype, tid)) or ent_by_id.get(tid) or {"title": f"{ttype} #{tid}", "slug": ""}
            item = _ensure_item("seo_meta", ttype, tid, url or str(ent.get("url") or ""), code)
            if code in {"missing_meta_title", "meta_title_too_short", "meta_title_too_long"}:
                item["proposed"]["meta_title"] = _localized_meta_title(ent, market=mkt)
            elif code in {"missing_meta_description", "meta_desc_length"}:
                item["proposed"]["meta_description"] = _localized_meta_desc(ent, market=mkt)
            elif code == "missing_focus_keyphrase":
                kw_base = " ".join(str(ent.get("title") or ent.get("slug") or "").replace("-", " ").split()).lower()
                kw_words = kw_base.split()[:4]
                if kw_words:
                    item["proposed"]["focus_keyphrase"] = " ".join(kw_words)[:60]
            elif code == "published_noindex":
                item["proposed"]["robots"] = "index, follow"
            item["summary"] = f"SEO-meta ({mkt.upper()}) för {ttype} #{tid}"

    # Fallback sweep directly over entities/media if audit had few/no targeted issues
    if len(grouped) < cap and clean_scope in {"all", "media_alt"}:
        for mid, m_obj in media_map.items():
            if len(grouped) >= cap:
                break
            if not mid.isdigit() or int(mid) <= 0:
                continue
            mime = str(m_obj.get("mime_type") or "image/jpeg").lower()
            if not mime.startswith("image/"):
                continue
            cur_alt = str(m_obj.get("alt_text") or "").strip()
            if not cur_alt or is_generic_alt(cur_alt):
                item = _ensure_item(
                    "media_alt",
                    "media",
                    mid,
                    str(m_obj.get("url") or ""),
                    "media_missing_alt" if not cur_alt else "media_generic_alt",
                )
                item["proposed"]["alt_text"] = _localized_media_alt(m_obj, market=mkt, entity_by_id=ent_by_id)
                item["summary"] = f"Alt-text ({mkt.upper()}) för bild #{mid}"

    if len(grouped) < cap and clean_scope in {"all", "seo_meta"}:
        for (otype, oid), ent in ent_map.items():
            if len(grouped) >= cap:
                break
            if otype not in {"product", "page", "post"} or not oid.isdigit() or int(oid) <= 0:
                continue
            if str(ent.get("status") or "publish").lower() != "publish":
                continue
            mt = str(ent.get("meta_title") or "").strip()
            md = str(ent.get("meta_description") or "").strip()
            if not mt or len(mt) > 60:
                item = _ensure_item("seo_meta", otype, oid, str(ent.get("url") or ""), "meta_title_fix")
                item["proposed"]["meta_title"] = _localized_meta_title(ent, market=mkt)
                item["summary"] = f"SEO-meta ({mkt.upper()}) för {otype} #{oid}"
            if not md or len(md) > 155:
                item = _ensure_item("seo_meta", otype, oid, str(ent.get("url") or ""), "meta_description_fix")
                item["proposed"]["meta_description"] = _localized_meta_desc(ent, market=mkt)
                item["summary"] = f"SEO-meta ({mkt.upper()}) för {otype} #{oid}"

    items = [entry for entry in grouped.values() if entry.get("proposed")]
    return items[:cap]


__all__ = [
    "LINK_PACKAGE_MAX_BUDGET_SEK",
    "LINK_PACKAGE_MAX_COUNT",
    "LINK_PACKAGE_MIN_BUDGET_SEK",
    "LINK_PACKAGE_MIN_COUNT",
    "_csv_safe_cell",
    "build_campaign",
    "build_link_package",
    "build_strategy",
    "draft_batch_fix_items",
    "render_link_package_brief",
    "validate_link_package_inputs",
]
