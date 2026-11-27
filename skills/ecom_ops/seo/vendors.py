"""Vetted Nordic White-Hat SEO Link-Shop Engine (SE / NO / DK).

Pure leaf module: imports ONLY stdlib + ``ecom_ops.seo.models`` + ``ecom_ops.seo.policy``.
Never imports ``ecom_ops.research``, never makes HTTP calls, and NEVER executes
external link purchases or payments.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any

from ecom_ops.seo.models import normalize_market
from ecom_ops.seo.policy import contains_spam_or_pbn

NO_AUTO_PURCHASE_NOTICE = (
    "Endast manuellt beställningsunderlag — Azom utför aldrig automatiska köp, "
    "registreringar eller betalningar hos externa länkleverantörer. "
    "Oscar (MARKETING_MUTATE) godkänner och lägger eventuell beställning manuellt."
)

MIN_WHITE_HAT_UNIT_SEK: dict[str, float] = {
    "se": 1200.0,
    "dk": 1200.0,
    "no": 1800.0,
}

NORDIC_DISCLOSURE_CHECKLIST: dict[str, tuple[str, ...]] = {
    "se": (
        "Sverige (Konsumentverket / Marknadsföringslagen): märk betalda redaktionella placeringar tydligt med 'Annons' eller 'Reklam'.",
        "Google spam-policy: använd transparent rel='sponsored' / naturlig länkattribut-mix och undvik dolda nätverk.",
        "Språk & kvalitet: endast idiomatisk svenska skriven/redigerad av modersmålsskribent (aldrig rå maskinöversättning).",
        "Manuell domängranskning: verifiera verklig organisk trafik >= 1 000/mån och ren utlänksprofil före betalning.",
    ),
    "no": (
        "Norge (Forbrukertilsynet / Markedsføringsloven): merk betalte redaksjonelle plasseringer tydelig med 'Annonse' eller 'Reklame'.",
        "Google spam-policy: bruk transparent rel='sponsored' / naturlig lenkeattributt-miks og unngå skjulte nettverk.",
        "Språk & kvalitet: kun idiomatisk norsk (bokmål) skrevet/redigert av morsmålsskribent (aldri rå maskinoversettelse).",
        "Manuell domenesjekk: verifiser reell organisk trafikk >= 1 000/mnd og ren utlenkeprofil før bestilling.",
    ),
    "dk": (
        "Danmark (Forbrugerombudsmanden / Markedsføringsloven): markér betalte redaktionelle placeringer tydeligt med 'Annonce' eller 'Reklame'.",
        "Google spam-policy: brug transparent rel='sponsored' / naturlig linkattribut-miks og undgå skjulte netværk.",
        "Sprog & kvalitet: kun idiomatisk dansk skrevet/redigeret af modersmålsskribent (aldrig rå maskinoversættelse).",
        "Manuel domænekontrol: verificér reel organisk trafik >= 1.000/md og ren udgående linkprofil før bestilling.",
    ),
}

_BLIND_BUNDLE_RE = re.compile(
    r"(?:\bblind\s+bundle\b|\bhidden\s+inventory\b|\bopaque\s+package\b|"
    r"\bno\s+url\s+preview\b|\bdolda\s+dom[äa]ner\b|\bhemliga\s+sajter\b|"
    r"\b\d+x\s*dr\s*\d+\s+(?:links?|backlinks?)\s+for\b)",
    re.I,
)
_GUARANTEED_RANKING_RE = re.compile(
    r"(?:\bguaranteed\s+(?:#1|number\s*1|first\s+page|google\s+ranking|top\s*1|serp)\b|"
    r"\bgaranterad\s+(?:#1|topp[- ]?1|f[öo]rstasida|google[- ]?ranking|ranking)\b|"
    r"\b100%\s*(?:#1|first\s+page|ranking)\b)",
    re.I,
)
_TOXIC_EXTRA_RE = re.compile(
    r"(?:\bcrypto[- ]?spam\b|\bunregulated\s+pharma\b|\btoxic\s+neighborhood\b|"
    r"\badult\s+casino\b|\bgambling\s+network\b)",
    re.I,
)
_MACHINE_TRANSLATION_RE = re.compile(
    r"(?:\braw\s+machine\s+translation\b|\bgoogle\s+translate\s+only\b|"
    r"\bonly\s+machine\s+translated\b|\bmaskin[öo]versatt\s+utan\s+redakt[öo]r\b|"
    r"\bunedited\s+ai\s+translation\b)",
    re.I,
)
_ANCHOR_LOCKOUT_RE = re.compile(
    r"(?:\bno\s+anchor\s+control\b|\banchor\s+lockout\b|\bcannot\s+choose\s+anchor\b|"
    r"\bundisclosed\s+advertorial\b|\bdold\s+annons\b|\bingen\s+annonsm[äa]rkning\b)",
    re.I,
)
_NO_WARRANTY_LOCKIN_RE = re.compile(
    r"(?:\bno\s+warranty\b|\bno\s+(?:live\s+)?guarantee\b|\bingen\s+garanti\b|"
    r"\bcoercive\s+(?:lock-?in|subscription)\b|\bmandatory\s+12[- ]?month\s+lock-?in\b)",
    re.I,
)
_WEB_NEGATIVE_RE = re.compile(
    r"\b(?:scam|fraud|penalized|google\s+penalty|de-?indexed|pbn\s+network|link\s*farm)\b",
    re.I,
)
_WEB_POSITIVE_RE = re.compile(
    r"\b(?:transparent|pre-?payment|url\s+preview|anti-?pbn|escrow|guarantee|"
    r"native\s+(?:nordic|copywriter|swedish|norwegian|danish)|konsumentverket|forbrukertilsynet)\b",
    re.I,
)


@dataclass(frozen=True)
class LinkVendor:
    """Curated Nordic/European white-hat SEO link marketplace or digital PR supplier."""

    vendor_id: str
    name: str
    domain: str
    url: str
    business_model: str  # self_serve_marketplace | digital_pr_marketplace | premium_editorial_pr | managed_nordic_agency
    markets: tuple[str, ...]
    market_inventory: dict[str, int]  # 0-100 inventory fit per market (se/no/dk)
    market_publisher_counts: dict[str, str]
    min_budget_sek: float
    typical_price_per_link_sek: tuple[int, int]
    dr_range: tuple[int, int]
    placement_types: tuple[str, ...]
    native_copywriting: bool
    editorial_copy_notes: str
    pre_payment_url_preview: bool
    verified_metrics_sources: tuple[str, ...]
    explicit_rel_disclosure: bool
    anti_pbn_moderation: bool
    organic_traffic_filter: bool
    clean_outbound_ratio: bool
    nordic_legal_disclosure_compliant: bool
    plagiarism_editorial_bar: bool
    guarantee_months: int
    escrow_or_pay_per_publish: bool
    duplicate_domain_guard: bool
    transparent_sek_eur_invoicing: bool
    automotive_tech_inventory: bool
    pbn_risk_flags: tuple[str, ...] = ()
    supported_exports: tuple[str, ...] = ("md", "json", "csv")
    order_workflow_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


NORDIC_LINK_VENDORS: tuple[LinkVendor, ...] = (
    LinkVendor(
        vendor_id="bazoom",
        name="Bazoom",
        domain="bazoom.com",
        url="https://bazoom.com/marketplace/nordics/",
        business_model="self_serve_marketplace",
        markets=("se", "no", "dk"),
        market_inventory={"se": 95, "no": 90, "dk": 98},
        market_publisher_counts={"se": "~3,500", "no": "~1,000", "dk": "~5,500"},
        min_budget_sek=3000.0,
        typical_price_per_link_sek=(1500, 5500),
        dr_range=(25, 75),
        placement_types=("editorial_guest_article", "niche_resource_mention", "digital_pr_guide"),
        native_copywriting=True,
        editorial_copy_notes="Bundled self-supplied, AI-draft, or native SE/NO/DK copywriters.",
        pre_payment_url_preview=True,
        verified_metrics_sources=("Ahrefs DR", "Semrush AS", "Moz DA", "Sistrix SI"),
        explicit_rel_disclosure=True,
        anti_pbn_moderation=True,
        organic_traffic_filter=True,
        clean_outbound_ratio=True,
        nordic_legal_disclosure_compliant=True,
        plagiarism_editorial_bar=True,
        guarantee_months=12,
        escrow_or_pay_per_publish=True,
        duplicate_domain_guard=True,
        transparent_sek_eur_invoicing=True,
        automotive_tech_inventory=True,
        pbn_risk_flags=(),
        order_workflow_notes=(
            "Danskgrundad självbetjäningsmarknadsplats utan abonnemang; filtrera på organisk trafik "
            ">= 1 000/mån, motor/teknik-kategori och aktivera dubblettskydd före beställning (3–4 d leverans)."
        ),
    ),
    LinkVendor(
        vendor_id="whitepress",
        name="WhitePress",
        domain="whitepress.com",
        url="https://www.whitepress.com/en/nordic-content-marketing",
        business_model="self_serve_marketplace",
        markets=("se", "no", "dk"),
        market_inventory={"se": 90, "no": 84, "dk": 86},
        market_publisher_counts={"se": "SE regional & nisch", "no": "NO regional & nisch", "dk": "DK regional & nisch"},
        min_budget_sek=2500.0,
        typical_price_per_link_sek=(1200, 5000),
        dr_range=(25, 70),
        placement_types=("editorial_guest_article", "niche_resource_mention", "digital_pr_guide"),
        native_copywriting=True,
        editorial_copy_notes="Separat modersmålsskribent/journalist (Basic -> Premium Plus) för SE/NO/DK eller egen text.",
        pre_payment_url_preview=True,
        verified_metrics_sources=("Ahrefs", "Semrush", "Majestic", "Moz", "Traffic"),
        explicit_rel_disclosure=True,
        anti_pbn_moderation=True,
        organic_traffic_filter=True,
        clean_outbound_ratio=True,
        nordic_legal_disclosure_compliant=True,
        plagiarism_editorial_bar=True,
        guarantee_months=12,
        escrow_or_pay_per_publish=True,
        duplicate_domain_guard=True,
        transparent_sek_eur_invoicing=True,
        automotive_tech_inventory=True,
        pbn_risk_flags=(),
        order_workflow_notes=(
            "Gratis annonsörskonto med escrow-betalning och 50+ SEO-/trafikfilter; välj modersmålsskribent "
            "(sv/nb/da) och 12–36 månaders övervakad publiceringsgaranti."
        ),
    ),
    LinkVendor(
        vendor_id="linkhouse",
        name="Linkhouse",
        domain="linkhouse.net",
        url="https://linkhouse.net/marketplace/scandinavia/",
        business_model="self_serve_marketplace",
        markets=("se", "no", "dk"),
        market_inventory={"se": 85, "no": 75, "dk": 88},
        market_publisher_counts={"se": "~1,100 .se", "no": "~760 .no", "dk": "~2,200 .dk"},
        min_budget_sek=2000.0,
        typical_price_per_link_sek=(700, 2600),
        dr_range=(20, 65),
        placement_types=("editorial_guest_article", "niche_resource_mention"),
        native_copywriting=True,
        editorial_copy_notes="Egen text eller valfritt lokalt skribent-/översättningstillägg per order.",
        pre_payment_url_preview=True,
        verified_metrics_sources=("Ahrefs DR/UR", "Majestic TF/CF", "Semrush", "Senuto"),
        explicit_rel_disclosure=True,
        anti_pbn_moderation=True,
        organic_traffic_filter=True,
        clean_outbound_ratio=False,
        nordic_legal_disclosure_compliant=True,
        plagiarism_editorial_bar=True,
        guarantee_months=12,
        escrow_or_pay_per_publish=True,
        duplicate_domain_guard=False,
        transparent_sek_eur_invoicing=True,
        automotive_tech_inventory=True,
        pbn_risk_flags=("low_cost_tier_requires_manual_traffic_and_tf_check",),
        order_workflow_notes=(
            "Starkt .dk/.se-utbud till lägre enhetspris; ställ alltid manuellt filter på organisk trafik "
            ">= 1 000/mån och balanserad TF/DR-kvot för att undvika svaga lågprisdomäner."
        ),
    ),
    LinkVendor(
        vendor_id="prnews",
        name="PRNEWS.IO",
        domain="prnews.io",
        url="https://prnews.io/sites/nordics/",
        business_model="digital_pr_marketplace",
        markets=("se", "no", "dk"),
        market_inventory={"se": 82, "no": 80, "dk": 80},
        market_publisher_counts={"se": "Regional & nationell nyhetsmedia", "no": "Regional & nationell nyhetsmedia", "dk": "Regional & nationell nyhetsmedia"},
        min_budget_sek=4000.0,
        typical_price_per_link_sek=(1600, 6500),
        dr_range=(35, 85),
        placement_types=("digital_pr_guide", "sponsored_news_feature"),
        native_copywriting=False,
        editorial_copy_notes="Egen redaktionell text (~48h granskning) rekommenderas starkt för idiomatisk SE/NO/DK.",
        pre_payment_url_preview=True,
        verified_metrics_sources=("Ahrefs DR", "Semrush AS", "Monthly Traffic & Geo Split"),
        explicit_rel_disclosure=True,
        anti_pbn_moderation=True,
        organic_traffic_filter=True,
        clean_outbound_ratio=True,
        nordic_legal_disclosure_compliant=True,
        plagiarism_editorial_bar=True,
        guarantee_months=12,
        escrow_or_pay_per_publish=True,
        duplicate_domain_guard=False,
        transparent_sek_eur_invoicing=True,
        automotive_tech_inventory=True,
        pbn_risk_flags=(),
        order_workflow_notes=(
            "Fastpris-katalog för nordisk nyhets- och branschmedia med full förhandsvisning av geo-trafik, "
            "länkattribut och tydlig Annons/Annonse/Annonce-märkning. Bäst för varumärkes-/URL-ankare."
        ),
    ),
    LinkVendor(
        vendor_id="getfluence",
        name="Getfluence",
        domain="getfluence.com",
        url="https://getfluence.com/en/managed-digital-pr/",
        business_model="premium_editorial_pr",
        markets=("se", "no", "dk"),
        market_inventory={"se": 76, "no": 72, "dk": 74},
        market_publisher_counts={"se": "Kuraterade Tier-1/2 medier", "no": "Kuraterade Tier-1/2 medier", "dk": "Kuraterade Tier-1/2 medier"},
        min_budget_sek=10000.0,
        typical_price_per_link_sek=(3500, 20000),
        dr_range=(45, 90),
        placement_types=("digital_pr_guide", "sponsored_news_feature", "editorial_guest_article"),
        native_copywriting=True,
        editorial_copy_notes="Egen text eller redaktörsgranskad flerspråkig copy med Copyscape-kontroll.",
        pre_payment_url_preview=True,
        verified_metrics_sources=("Spot Finder Traffic", "Ahrefs", "Authority Metrics"),
        explicit_rel_disclosure=True,
        anti_pbn_moderation=True,
        organic_traffic_filter=True,
        clean_outbound_ratio=True,
        nordic_legal_disclosure_compliant=True,
        plagiarism_editorial_bar=True,
        guarantee_months=12,
        escrow_or_pay_per_publish=True,
        duplicate_domain_guard=True,
        transparent_sek_eur_invoicing=True,
        automotive_tech_inventory=True,
        pbn_risk_flags=(),
        order_workflow_notes=(
            "Premium redaktionell marknadsplats + Getfluence+ (från ~11 500 SEK/mån + media); "
            "strikt anti-PBN-policy där avindexerade domäner fryses."
        ),
    ),
    LinkVendor(
        vendor_id="seeders",
        name="Seeders",
        domain="seeders.com",
        url="https://seeders.com/nordics-link-building/",
        business_model="managed_nordic_agency",
        markets=("se", "no", "dk"),
        market_inventory={"se": 88, "no": 92, "dk": 86},
        market_publisher_counts={"se": "Dedikerat SE outreach-team", "no": "Dedikerat NO outreach-team (stark på .no)", "dk": "Dedikerat DK outreach-team"},
        min_budget_sek=23000.0,
        typical_price_per_link_sek=(3000, 9000),
        dr_range=(35, 80),
        placement_types=("digital_pr_guide", "editorial_guest_article", "earned_outreach_pr"),
        native_copywriting=True,
        editorial_copy_notes="Ingår: lokala SE/NO/DK-outreachskribenter & journalistpitch (aldrig maskinöversättning).",
        pre_payment_url_preview=True,
        verified_metrics_sources=("Pre-approved Domain List", "Ahrefs DR", "Traffic", "Indexation Report"),
        explicit_rel_disclosure=True,
        anti_pbn_moderation=True,
        organic_traffic_filter=True,
        clean_outbound_ratio=True,
        nordic_legal_disclosure_compliant=True,
        plagiarism_editorial_bar=True,
        guarantee_months=12,
        escrow_or_pay_per_publish=True,
        duplicate_domain_guard=True,
        transparent_sek_eur_invoicing=True,
        automotive_tech_inventory=True,
        pbn_risk_flags=(),
        order_workflow_notes=(
            "Hanterad nordisk outreach & digital PR med förhandsgodkända domänlistor; "
            "starkast val för svåråtkomliga .no-domäner och större lanseringspaket (>= 55 000 SEK)."
        ),
    ),
)

_VENDOR_BY_ID: dict[str, LinkVendor] = {v.vendor_id: v for v in NORDIC_LINK_VENDORS}


def list_vendors() -> list[LinkVendor]:
    """Return all curated Nordic link vendors in canonical order."""
    return list(NORDIC_LINK_VENDORS)


def get_vendor(vendor_id: str) -> LinkVendor | None:
    """Lookup a curated vendor by ID or domain."""
    key = str(vendor_id or "").strip().lower()
    if not key:
        return None
    if key in _VENDOR_BY_ID:
        return _VENDOR_BY_ID[key]
    for v in NORDIC_LINK_VENDORS:
        if v.domain.lower() == key or v.name.lower() == key:
            return v
    return None


def _vendor_to_dict(candidate: LinkVendor | dict[str, Any]) -> dict[str, Any]:
    if isinstance(candidate, LinkVendor):
        return candidate.to_dict()
    return dict(candidate or {})


def _match_web_signals(
    domain: str,
    name: str,
    web_signals: list[dict[str, Any]] | None,
) -> tuple[float, list[str], list[dict[str, Any]], bool]:
    """Inspect plain-dict web signals for a vendor and return ``(delta, notes, citations, severe_pbn)``."""
    if not web_signals:
        return 0.0, [], [], False

    dom_low = str(domain or "").strip().lower().removeprefix("www.")
    name_low = str(name or "").strip().lower()
    matched_citations: list[dict[str, Any]] = []
    notes: list[str] = []
    pos_hits = 0
    neg_hits = 0
    severe_pbn = False

    for sig in web_signals:
        if not isinstance(sig, dict):
            continue
        url = str(sig.get("url") or "").strip()
        title = str(sig.get("title") or "").strip()
        snippet = str(sig.get("snippet") or sig.get("content") or "").strip()
        blob = f"{url} {title} {snippet}".lower()
        if not ((dom_low and dom_low in blob) or (name_low and len(name_low) >= 4 and name_low in blob)):
            continue

        matched_citations.append(
            {
                "title": title or name or domain,
                "url": url,
                "snippet": snippet[:240],
                "provider": str(sig.get("provider") or "web_vetting"),
            }
        )
        # Check for severe negative/scam/PBN evidence (exclude positive "anti-PBN" mentions)
        sanitized_blob = re.sub(r"\banti[- ]?pbn\b", "", blob, flags=re.I)
        spam_flag, spam_terms = contains_spam_or_pbn(sanitized_blob)
        if spam_flag or _WEB_NEGATIVE_RE.search(sanitized_blob):
            neg_hits += 1
            if spam_flag:
                severe_pbn = True
                notes.append(f"Webbvarning ({domain}): spam/PBN-signaler i extern källa ({', '.join(spam_terms[:3])}).")
            else:
                notes.append(f"Webbvarning ({domain}): negativ ryktessignal i extern källa.")
        elif _WEB_POSITIVE_RE.search(blob):
            pos_hits += 1

    if severe_pbn:
        delta = -25.0
    elif neg_hits > 0:
        delta = max(-15.0, -7.5 * neg_hits)
    elif pos_hits > 0:
        delta = min(4.0, 2.0 * pos_hits)
    else:
        delta = 0.0
    return delta, notes, matched_citations[:3], severe_pbn


def vet_vendor_candidate(
    candidate: LinkVendor | dict[str, Any],
    *,
    market: str = "no",
    web_signals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply the 0–100 weighted white-hat vetting rubric and 7 hard-reject anti-PBN rules."""
    mkt = normalize_market(market)
    data = _vendor_to_dict(candidate)

    vendor_id = str(data.get("vendor_id") or data.get("domain") or "custom").strip().lower()
    name = str(data.get("name") or vendor_id).strip()
    domain = str(data.get("domain") or "").strip().lower().removeprefix("www.")
    text_blob = " ".join(
        str(data.get(k) or "")
        for k in (
            "name",
            "domain",
            "description",
            "claims",
            "notes",
            "editorial_copy_notes",
            "order_workflow_notes",
        )
    )
    risk_flags = [str(f).strip().lower() for f in (data.get("pbn_risk_flags") or ()) if str(f).strip()]
    full_text = f"{text_blob} {' '.join(risk_flags)}".strip()

    hard_reject_reasons: list[str] = []

    # Rule 1: Hidden inventory / blind bundles
    if data.get("pre_payment_url_preview") is False or data.get("blind_bundle") is True or _BLIND_BUNDLE_RE.search(full_text):
        hard_reject_reasons.append(
            "hard_reject:hidden_inventory_blind_bundle — dolda publicist-URL:er eller blinda länkpaket före betalning."
        )

    # Rule 2: PBN / link-farm footprint
    sanitized_text = re.sub(r"\banti[- ]?pbn\b", "", full_text, flags=re.I)
    spam_hit, spam_terms = contains_spam_or_pbn(sanitized_text)
    pbn_terms = [t for t in spam_terms if t in {"pbn", "private blog network", "link farm", "linkfarm", "länkfarm", "lenkefarm", "link wheel", "cheap bulk links", "hidden links", "cloaking", "doorway pages", "doorway page", "xrumer", "gsa ser", "comment spam", "forum spam"}]
    toxic_terms = [t for t in spam_terms if t not in pbn_terms]
    if data.get("anti_pbn_moderation") is False or data.get("is_pbn") is True or pbn_terms:
        detail = f" ({', '.join(pbn_terms)})" if pbn_terms else ""
        hard_reject_reasons.append(
            f"hard_reject:pbn_or_link_farm — PBN/länkfarm-fotavtryck eller saknad anti-PBN-moderering{detail}."
        )

    # Rule 3: Guaranteed-ranking claims
    if data.get("guaranteed_rankings") is True or _GUARANTEED_RANKING_RE.search(full_text):
        hard_reject_reasons.append(
            "hard_reject:guaranteed_ranking_claims — oseriösa löften om garanterad #1-ranking i Google."
        )

    # Rule 4: Toxic neighborhoods (casino, payday loans, adult, crypto-spam, unregulated pharma)
    if data.get("toxic_neighborhood") is True or toxic_terms or _TOXIC_EXTRA_RE.search(sanitized_text):
        detail = f" ({', '.join(toxic_terms)})" if toxic_terms else ""
        hard_reject_reasons.append(
            f"hard_reject:toxic_neighborhood — toxiska länkgrannar (kasino/snabblån/vuxen/spam){detail}."
        )

    # Rule 5: Raw machine-translated / non-native Nordic copy forced
    if data.get("forces_machine_translation") is True or _MACHINE_TRANSLATION_RE.search(full_text):
        hard_reject_reasons.append(
            "hard_reject:raw_machine_translation — tvingar rå maskinöversättning utan nordisk modersmålsredaktör."
        )

    # Rule 6: Anchor-guardrail lockout or undisclosed illegal advertorials
    if (
        data.get("anchor_lockout") is True
        or data.get("blocks_anchor_control") is True
        or data.get("requires_undisclosed_advertorial") is True
        or data.get("nordic_legal_disclosure_compliant") is False
        or _ANCHOR_LOCKOUT_RE.search(full_text)
    ):
        hard_reject_reasons.append(
            "hard_reject:anchor_lockout_or_undisclosed_ads — låser ankartexter eller bryter mot nordisk annonsmärkning."
        )

    # Rule 7: No warranty (<6 months) or coercive lock-in
    raw_guarantee = data.get("guarantee_months")
    guarantee_months = int(raw_guarantee) if raw_guarantee is not None else 12
    if (
        guarantee_months < 6
        or data.get("coercive_lockin") is True
        or _NO_WARRANTY_LOCKIN_RE.search(full_text)
    ):
        hard_reject_reasons.append(
            f"hard_reject:no_warranty_or_coercive_lockin — live-garanti ({guarantee_months} mån) < 6 mån eller tvingande inlåsning."
        )

    # Evaluate optional web vetting signals
    web_delta, web_notes, citations, web_severe_pbn = _match_web_signals(domain, name, web_signals)
    if web_severe_pbn:
        hard_reject_reasons.append(
            "hard_reject:web_vetting_pbn_confirmed — extern webbgranskning rapporterar spam/PBN-signaler."
        )

    if hard_reject_reasons:
        return {
            "vendor_id": vendor_id,
            "name": name,
            "domain": domain,
            "vetting_score": 0,
            "vetting_status": "rejected",
            "eligible": False,
            "hard_reject": True,
            "hard_reject_reasons": hard_reject_reasons,
            "rubric_breakdown": {
                "transparency_25": 0,
                "anti_pbn_traffic_20": 0,
                "nordic_depth_20": 0,
                "editorial_compliance_15": 0,
                "guarantees_ops_10": 0,
                "price_authority_fit_10": 0,
            },
            "cautions": web_notes,
            "web_signal_adjustment": web_delta,
            "citations": citations,
        }

    # --- Weighted 0-100 Rubric -------------------------------------------- #
    # 1. Pre-Payment Domain & Metric Transparency (25 pts)
    metrics_sources = tuple(data.get("verified_metrics_sources") or ())
    metrics_pts = 10 if len(metrics_sources) >= 3 else (7 if len(metrics_sources) >= 1 else 0)
    transparency_25 = (
        (10 if data.get("pre_payment_url_preview", True) else 0)
        + metrics_pts
        + (5 if data.get("explicit_rel_disclosure", True) else 0)
    )

    # 2. Anti-PBN & Organic Traffic Integrity (20 pts)
    anti_pbn_traffic_20 = (
        (8 if data.get("anti_pbn_moderation", True) else 0)
        + (7 if data.get("organic_traffic_filter", True) else 0)
        + (5 if data.get("clean_outbound_ratio", True) else 2)
    )

    # 3. Nordic Market Depth & Native Localization (20 pts)
    inv_map = data.get("market_inventory") or {}
    raw_inv = int(inv_map.get(mkt, 70 if mkt in (data.get("markets") or ()) else 30))
    tld_pts = round(max(0, min(100, raw_inv)) * 0.10)
    nordic_depth_20 = (
        tld_pts
        + (6 if data.get("native_copywriting", False) else 2)
        + (4 if data.get("automotive_tech_inventory", True) else 1)
    )

    # 4. Editorial & Legal Compliance (15 pts)
    editorial_compliance_15 = (
        (6 if data.get("explicit_rel_disclosure", True) else 0)
        + (5 if data.get("nordic_legal_disclosure_compliant", True) else 0)
        + (4 if data.get("plagiarism_editorial_bar", True) else 1)
    )

    # 5. Guarantees, Escrow & Operations (10 pts)
    guar_pts = 5 if guarantee_months >= 12 else (3 if guarantee_months >= 6 else 0)
    guarantees_ops_10 = (
        guar_pts
        + (3 if data.get("escrow_or_pay_per_publish", True) else 1)
        + (2 if data.get("duplicate_domain_guard", False) else 1)
    )

    # 6. Price-to-Authority Fit for Azom (10 pts)
    price_range = data.get("typical_price_per_link_sek") or (1500, 5000)
    low_p = float(price_range[0]) if len(price_range) >= 1 else 1500.0
    high_p = float(price_range[1]) if len(price_range) >= 2 else 5500.0
    if low_p <= 3500 and high_p <= 10000:
        unit_pts = 6
    elif low_p <= 5000:
        unit_pts = 5
    else:
        unit_pts = 4
    price_authority_fit_10 = unit_pts + (4 if data.get("transparent_sek_eur_invoicing", True) else 1)

    total_score = (
        transparency_25
        + anti_pbn_traffic_20
        + nordic_depth_20
        + editorial_compliance_15
        + guarantees_ops_10
        + price_authority_fit_10
    )
    total_score = max(0, min(100, int(total_score)))

    cautions: list[str] = list(web_notes)
    if "low_cost_tier_requires_manual_traffic_and_tf_check" in risk_flags:
        cautions.append(
            "Kräver manuell kontroll av organisk trafik >= 1 000/mån och TF/DR-kvot på lägre prisnivåer."
        )
    if not data.get("native_copywriting", False):
        cautions.append(
            "Leverera egen idiomatisk nordisk artikeltext (SE/NO/DK) för att säkra publicistgodkännande."
        )

    if total_score >= 85:
        status = "preferred"
    elif total_score >= 70:
        status = "conditional"
    else:
        status = "rejected"

    return {
        "vendor_id": vendor_id,
        "name": name,
        "domain": domain,
        "vetting_score": total_score,
        "vetting_status": status,
        "eligible": total_score >= 70,
        "hard_reject": False,
        "hard_reject_reasons": [],
        "rubric_breakdown": {
            "transparency_25": transparency_25,
            "anti_pbn_traffic_20": anti_pbn_traffic_20,
            "nordic_depth_20": nordic_depth_20,
            "editorial_compliance_15": editorial_compliance_15,
            "guarantees_ops_10": guarantees_ops_10,
            "price_authority_fit_10": price_authority_fit_10,
        },
        "cautions": cautions,
        "web_signal_adjustment": web_delta,
        "citations": citations,
    }


def determine_campaign_archetype(
    budget_sek: float,
    link_count: int,
    *,
    goal: str = "organic_growth",
    brand_url_generic_pct: float = 70.0,
) -> str:
    """Classify a link package into ``self_serve_marketplace | hybrid_marketplace_pr | managed_nordic_pr``."""
    g = str(goal or "organic_growth").strip().lower()
    if budget_sek >= 55000.0 or (link_count >= 10 and budget_sek >= 45000.0) or g in {"managed_launch", "market_entry_pr"}:
        return "managed_nordic_pr"
    if (
        budget_sek >= 25000.0
        or (g in {"brand_launch_pr", "brand_pr", "digital_pr", "authority_pr"} and budget_sek >= 15000.0)
        or (link_count >= 6 and brand_url_generic_pct >= 75.0 and budget_sek >= 20000.0)
    ):
        return "hybrid_marketplace_pr"
    return "self_serve_marketplace"


def _budget_tier_fit(
    vendor: dict[str, Any],
    *,
    budget_sek: float,
    unit_sek: float,
    link_count: int,
    tier: str,
) -> float:
    min_b = float(vendor.get("min_budget_sek") or 2000.0)
    low_u, high_u = vendor.get("typical_price_per_link_sek") or (1200, 5000)
    model = str(vendor.get("business_model") or "self_serve_marketplace")

    # Start from unit price compatibility
    if low_u <= unit_sek <= high_u:
        score = 92.0
    elif unit_sek < low_u:
        ratio = unit_sek / max(float(low_u), 1.0)
        score = max(25.0, 82.0 * ratio)
    else:
        # Budget higher than typical range -> still good for higher-DR spots, slight preference for premium/PR tiers
        score = 84.0 if model in {"digital_pr_marketplace", "premium_editorial_pr", "managed_nordic_agency"} else 76.0

    # Retainer / minimum budget penalty when package budget is below vendor minimum
    if budget_sek < min_b:
        deficit = (min_b - budget_sek) / max(min_b, 1.0)
        score -= min(45.0, deficit * 55.0)

    # Archetype alignment
    if tier == "managed_nordic_pr":
        if model == "managed_nordic_agency":
            score += 18.0
        elif model == "premium_editorial_pr":
            score += 12.0
        elif model == "digital_pr_marketplace":
            score += 6.0
        else:
            score -= 8.0
    elif tier == "hybrid_marketplace_pr":
        if model in {"digital_pr_marketplace", "self_serve_marketplace"}:
            score += 8.0
        elif model == "premium_editorial_pr":
            score += 5.0
    else:  # self_serve_marketplace
        if model == "self_serve_marketplace":
            score += 8.0
        elif model == "managed_nordic_agency":
            score -= 18.0
        elif model == "premium_editorial_pr":
            score -= 10.0

    if link_count >= 10 and model == "managed_nordic_agency" and budget_sek >= min_b:
        score += 5.0
    return max(0.0, min(100.0, round(score, 1)))


def _goal_anchor_fit(
    vendor: dict[str, Any],
    *,
    goal: str,
    brand_url_generic_pct: float,
    link_count: int,
    native_copy_needed: bool,
    tier: str,
) -> float:
    vid = str(vendor.get("vendor_id") or "").lower()
    g = str(goal or "organic_growth").strip().lower()
    score = 74.0

    if brand_url_generic_pct >= 75.0 or g in {"brand_launch_pr", "brand_pr", "digital_pr", "authority_pr"}:
        if vid in {"prnews", "getfluence", "seeders"}:
            score += 18.0
        elif vid in {"bazoom", "whitepress"}:
            score += 6.0
    else:
        if vid in {"bazoom", "whitepress", "linkhouse"}:
            score += 12.0

    if native_copy_needed and link_count <= 6:
        if vid in {"bazoom", "whitepress"}:
            score += 10.0
        elif vid == "prnews":
            score -= 6.0

    if tier == "managed_nordic_pr" and vid in {"seeders", "getfluence"}:
        score += 10.0
    if tier == "hybrid_marketplace_pr" and vid in {"bazoom", "prnews", "whitepress"}:
        score += 6.0

    return max(0.0, min(100.0, round(score, 1)))


def match_vendors_for_link_package(
    market: str,
    budget_sek: float,
    link_count: int,
    *,
    goal: str = "organic_growth",
    brand_url_generic_pct: float = 70.0,
    anchor_guardrail_blocked: bool = False,
    web_signals: list[dict[str, Any]] | None = None,
    native_copy_needed: bool = True,
    extra_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministically vet, score, and rank top 2–3 Nordic white-hat SEO link shops.

    Fail-closed on anchor-mix guardrail blocks and enforces Nordic white-hat unit budget
    floors (SE/DK >= 1,200 SEK/link; NO >= 1,800 SEK/link) without ever downgrading to PBNs.
    """
    mkt = normalize_market(market)
    budget = float(budget_sek)
    count = max(1, int(link_count))
    unit_sek = round(budget / count, 2)
    min_unit = MIN_WHITE_HAT_UNIT_SEK[mkt]
    disclosure = list(NORDIC_DISCLOSURE_CHECKLIST[mkt])

    if anchor_guardrail_blocked:
        return {
            "ok": False,
            "blocked": True,
            "block_reason": (
                "Leverantörsurval stoppat (fail-closed): länkpaketets ankartext-mix eller "
                "anti-spam-guardrail är blockerad. Justera ankartexter till >= 60% varumärke/URL/generisk "
                "och <= 10% exaktmatch innan leverantör väljs."
            ),
            "market": mkt,
            "budget_sek": round(budget, 2),
            "link_count": count,
            "unit_sek": unit_sek,
            "min_white_hat_unit_sek": min_unit,
            "budget_warning": "",
            "recommended_link_count": count,
            "recommended_tier": "",
            "selected_vendor_id": None,
            "candidates": [],
            "rejected_candidates": [],
            "disclosure_checklist": disclosure,
            "no_auto_purchase_notice": NO_AUTO_PURCHASE_NOTICE,
            "citations": [],
        }

    budget_warning = ""
    recommended_link_count = count
    if unit_sek < min_unit:
        recommended_link_count = max(1, math.floor(budget / min_unit))
        budget_warning = (
            f"Budgetvarning ({mkt.upper()}): {unit_sek:.0f} SEK/länk understiger white-hat-golvet "
            f"({min_unit:.0f} SEK/länk). Minska antalet länkar till {recommended_link_count} st "
            f"(eller höj budgeten till minst {int(count * min_unit)} SEK) — nedgradera aldrig till "
            f"lågpris-PBN eller länkfarmar."
        )

    tier = determine_campaign_archetype(
        budget,
        count,
        goal=goal,
        brand_url_generic_pct=brand_url_generic_pct,
    )

    pool: list[LinkVendor | dict[str, Any]] = list(NORDIC_LINK_VENDORS)
    if extra_candidates:
        pool.extend(extra_candidates)

    ranked: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    all_citations: list[dict[str, Any]] = []
    seen_citation_urls: set[str] = set()

    for raw_cand in pool:
        vdict = _vendor_to_dict(raw_cand)
        vet = vet_vendor_candidate(raw_cand, market=mkt, web_signals=web_signals)
        for cit in vet.get("citations") or []:
            cu = str(cit.get("url") or "")
            if cu and cu not in seen_citation_urls:
                seen_citation_urls.add(cu)
                all_citations.append(cit)

        if not vet["eligible"] or vet["hard_reject"]:
            rejected.append(
                {
                    "vendor_id": vet["vendor_id"],
                    "name": vet["name"],
                    "domain": vet["domain"],
                    "vetting_score": vet["vetting_score"],
                    "vetting_status": vet["vetting_status"],
                    "hard_reject": vet["hard_reject"],
                    "reasons": vet["hard_reject_reasons"] or [f"Vetting-poäng {vet['vetting_score']} < 70"],
                }
            )
            continue

        inv_map = vdict.get("market_inventory") or {}
        market_fit = float(inv_map.get(mkt, 70))
        budget_fit = _budget_tier_fit(
            vdict,
            budget_sek=budget,
            unit_sek=unit_sek,
            link_count=count,
            tier=tier,
        )
        goal_fit = _goal_anchor_fit(
            vdict,
            goal=goal,
            brand_url_generic_pct=brand_url_generic_pct,
            link_count=count,
            native_copy_needed=native_copy_needed,
            tier=tier,
        )
        web_adj = float(vet.get("web_signal_adjustment") or 0.0)

        rank_score = (
            0.40 * float(vet["vetting_score"])
            + 0.25 * market_fit
            + 0.20 * budget_fit
            + 0.15 * goal_fit
            + web_adj
        )
        rank_score = round(max(0.0, min(100.0, rank_score)), 1)

        low_u, high_u = vdict.get("typical_price_per_link_sek") or (1500, 5000)
        est_low_total = int(low_u) * recommended_link_count
        est_high_total = int(high_u) * recommended_link_count
        provenance = "curated+web_vetted" if vet.get("citations") else "curated_catalog"

        ranked.append(
            {
                "vendor_id": vet["vendor_id"],
                "name": vet["name"],
                "domain": vet["domain"],
                "url": str(vdict.get("url") or f"https://{vet['domain']}/"),
                "business_model": str(vdict.get("business_model") or "self_serve_marketplace"),
                "rank_score": rank_score,
                "fit_score": rank_score,
                "vetting_score": vet["vetting_score"],
                "vetting_status": vet["vetting_status"],
                "market_fit": round(market_fit, 1),
                "budget_fit": round(budget_fit, 1),
                "goal_anchor_fit": round(goal_fit, 1),
                "web_signal_adjustment": web_adj,
                "estimated_unit_sek_range": [int(low_u), int(high_u)],
                "estimated_total_sek_range": [est_low_total, est_high_total],
                "market_publishers": str((vdict.get("market_publisher_counts") or {}).get(mkt, "")),
                "native_copywriting": bool(vdict.get("native_copywriting", False)),
                "guarantee_months": int(vdict.get("guarantee_months") or 12),
                "rubric_breakdown": vet["rubric_breakdown"],
                "cautions": vet["cautions"],
                "order_workflow_notes": str(vdict.get("order_workflow_notes") or ""),
                "provenance": provenance,
                "citations": vet["citations"],
            }
        )

    ranked.sort(key=lambda r: (-r["rank_score"], -r["vetting_score"], r["vendor_id"]))
    top_candidates = ranked[:3]
    for idx, cand in enumerate(top_candidates, start=1):
        cand["rank"] = idx
        if idx == 1:
            cand["recommended_role"] = "Primär rekommenderad leverantör"
        elif idx == 2:
            cand["recommended_role"] = "Alternativ / komplement för bredare publicistmix"
        else:
            cand["recommended_role"] = "Tredjehandsval / specialiserad komplettering"

    # Also add Konsumentverket / disclosure citation if present in web_signals
    for sig in web_signals or []:
        if not isinstance(sig, dict):
            continue
        u = str(sig.get("url") or "").strip()
        if u and u not in seen_citation_urls and any(
            k in u.lower() for k in ("konsumentverket", "forbrukertilsynet", "forbrug.dk")
        ):
            seen_citation_urls.add(u)
            all_citations.append(
                {
                    "title": str(sig.get("title") or "Nordic Sponsored Disclosure Rules"),
                    "url": u,
                    "snippet": str(sig.get("snippet") or "")[:240],
                    "provider": str(sig.get("provider") or "web_vetting"),
                }
            )

    return {
        "ok": True,
        "blocked": False,
        "block_reason": "",
        "market": mkt,
        "budget_sek": round(budget, 2),
        "link_count": count,
        "unit_sek": unit_sek,
        "min_white_hat_unit_sek": min_unit,
        "budget_warning": budget_warning,
        "recommended_link_count": recommended_link_count,
        "recommended_tier": tier,
        "selected_vendor_id": top_candidates[0]["vendor_id"] if top_candidates else None,
        "candidates": top_candidates,
        "rejected_candidates": rejected,
        "disclosure_checklist": disclosure,
        "no_auto_purchase_notice": NO_AUTO_PURCHASE_NOTICE,
        "citations": all_citations,
    }


__all__ = [
    "LinkVendor",
    "MIN_WHITE_HAT_UNIT_SEK",
    "NORDIC_DISCLOSURE_CHECKLIST",
    "NORDIC_LINK_VENDORS",
    "NO_AUTO_PURCHASE_NOTICE",
    "determine_campaign_archetype",
    "get_vendor",
    "list_vendors",
    "match_vendors_for_link_package",
    "vet_vendor_candidate",
]
