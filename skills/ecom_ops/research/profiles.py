"""Goal- and ambition-adaptive search profiles, provider combinations, and YAML overrides.

Leaf module: no ``integrations.*``, ``urllib``, ``http.client``, ``requests``,
or ``subprocess`` imports.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from ecom_ops.research.config import load_research_config
from ecom_ops.research.models import SearchProfile

_VALID_AMBITIONS = frozenset({"quick", "standard", "deep"})
_VALID_COMBINATION_MODES = frozenset({"failover", "ensemble", "staged"})
_VALID_FETCH_STRATEGIES = frozenset(
    {"snippet_only", "direct", "parallel_extract_first", "hybrid"}
)

_BUILTIN_PROFILES: dict[str, SearchProfile] = {
    "design_inspiration": SearchProfile(
        id="design_inspiration",
        title="UX & Design Inspiration",
        description="UX/UI inspiration, conversion patterns, and layout benchmarks for WP pages.",
        goal="Hämta modern UX/UI-inspiration, konverteringsmönster och branschbenchmarks före sidbygge.",
        ambition="standard",
        combination_mode="ensemble",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        fetch_strategy="parallel_extract_first",
        output_format_default="ux_patterns",
        when_to_use=(
            "Vid redesign eller nybygge av tacksida, produktsida, landningssida eller checkout."
        ),
        query_templates=(
            "{topic} ecommerce UX patterns best practices",
            "{topic} {market} design benchmark examples",
            "{topic} mobile conversion layout study",
        ),
        deliverable_sections=(
            "Visuella mönster",
            "Konverteringsprinciper",
            "Nordisk anpassning",
            "Källbelägg [S#]",
        ),
        recommended_tools=(
            "web_search",
            "web_fetch_extract",
            "web_research_start",
            "wp_workspace_attach_research",
        ),
        preferred_providers=("exa", "parallel", "tavily", "brave"),
        market_defaults=("en", "sv", "nb", "da"),
        freshness_days=730,
        corroboration_mode="inspiration_and_heuristics",
        authority_domains=(
            "baymard.com",
            "nngroup.com",
            "smashingmagazine.com",
            "web.dev",
            "wordpress.org",
        ),
        pii_strictness="standard",
        weights={
            "relevance": 0.35,
            "authority": 0.30,
            "freshness": 0.15,
            "signal_density": 0.20,
        },
        max_fetch_default=3,
    ),
    "technical_docs": SearchProfile(
        id="technical_docs",
        title="Teknisk Primärdokumentation",
        description="WordPress, WooCommerce, PHP, CSS, and official developer documentation.",
        goal="Verifiera exakta hooks, block-API:er, HPOS-signaturer, PHP/CSS-standarder mot officiell dokumentation.",
        ambition="standard",
        combination_mode="failover",
        fanout_by_depth={"quick": 1, "standard": 1, "deep": 2},
        fetch_strategy="parallel_extract_first",
        output_format_default="checklist",
        when_to_use=(
            "När kod, teman eller plugins behöver exakta WooCommerce-/WordPress-hooks eller säker CSS/PHP."
        ),
        query_templates=(
            "{topic} official developer documentation",
            "{topic} hooks API signature deprecation notes",
        ),
        deliverable_sections=(
            "Verifierad API/Hook-spec",
            "Kompatibilitetskrav",
            "Implementationschecklista",
        ),
        recommended_tools=("web_search", "web_fetch_extract", "web_research_start"),
        preferred_providers=("parallel", "exa", "brave", "tavily"),
        market_defaults=("en",),
        freshness_days=540,
        corroboration_mode="authoritative_primary",
        authority_domains=(
            "developer.wordpress.org",
            "woocommerce.com",
            "developer.mozilla.org",
            "php.net",
            "github.com",
        ),
        pii_strictness="standard",
        weights={
            "relevance": 0.30,
            "authority": 0.45,
            "freshness": 0.15,
            "signal_density": 0.10,
        },
        max_fetch_default=3,
    ),
    "competitor_market": SearchProfile(
        id="competitor_market",
        title="Nordisk Konkurrent- & Marknadsanalys",
        description="Nordic e-commerce and car-multimedia positioning, USP, and market research.",
        goal="Kartlägg nordiska konkurrenter, positionering, prisnivåer, garanti-normer och USP:ar.",
        ambition="deep",
        combination_mode="staged",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        round_providers={
            1: ("parallel", "exa"),
            2: ("brave", "tavily"),
            3: ("parallel", "exa", "brave"),
        },
        fetch_strategy="hybrid",
        output_format_default="comparison",
        when_to_use=(
            "Vid nordiska marknads-, konkurrent- eller sortimentsanalyser över SE/NO/DK."
        ),
        query_templates=(
            "{topic} {market} marknad konkurrenter positionering",
            "{topic} {market} pris garanti leverans villkor",
            "{topic} Nordic aftermarket trends trade comparison",
        ),
        deliverable_sections=(
            "Marknadsöversikt",
            "Konkurrentmatris",
            "Pris & värdeerbjudande",
            "Differentieringsmöjligheter",
        ),
        recommended_tools=("web_research_start", "web_search", "web_fetch_extract"),
        preferred_providers=("parallel", "exa", "brave", "tavily"),
        market_defaults=("SE", "NO", "DK"),
        freshness_days=365,
        corroboration_mode="triangulated_market",
        authority_domains=(),
        pii_strictness="high",
        weights={
            "relevance": 0.35,
            "authority": 0.25,
            "freshness": 0.20,
            "signal_density": 0.20,
        },
        max_fetch_default=4,
    ),
    "support_fact_check": SearchProfile(
        id="support_fact_check",
        title="Faktakoll: Frakt, Drift & Support",
        description="Carrier outages, Nordic consumer law, and OEM hardware/fitment verification.",
        goal="Faktagranska färska fraktstörningar, konsumentregler och hårdvarupassform utan kund-PII.",
        ambition="quick",
        combination_mode="failover",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 2},
        fetch_strategy="direct",
        output_format_default="brief",
        when_to_use=(
            "Snabb, strikt källbelagd faktakoll för kundärenden, PostNord/Bring/DHL-drift eller garanti."
        ),
        query_templates=(
            "{topic} {market} officiell driftinformation status",
            "{topic} {market} konsumenträtt officiell vägledning",
        ),
        deliverable_sections=(
            "Faktastatus",
            "Primärkällor",
            "Risker & osäkerhet",
        ),
        recommended_tools=("web_search", "web_fetch_extract"),
        preferred_providers=("parallel", "brave", "exa", "tavily"),
        market_defaults=("SE", "NO", "DK"),
        freshness_days=30,
        corroboration_mode="strict_factual",
        authority_domains=(
            "postnord.se",
            "postnord.no",
            "postnord.dk",
            "bring.se",
            "bring.no",
            "dhl.com",
            "konsumentverket.se",
            "forbrukerradet.no",
            "lovdata.no",
            "forbrug.dk",
        ),
        pii_strictness="strict",
        weights={
            "relevance": 0.25,
            "authority": 0.45,
            "freshness": 0.20,
            "signal_density": 0.10,
        },
        max_fetch_default=3,
    ),
    "marketing_intel": SearchProfile(
        id="marketing_intel",
        title="Marketing & Ads Intelligence",
        description="Google Ads/GA4 updates, Consent Mode, seasonality, and keyword disambiguation.",
        goal="Fånga plattformsuppdateringar i Google Ads/GA4, Consent Mode v2, säsongseffekter och sökordsavgränsning.",
        ambition="standard",
        combination_mode="ensemble",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        fetch_strategy="direct",
        output_format_default="brief",
        when_to_use=(
            "Vid kampanjplanering, negativa sökord, Consent Mode eller policy-/GA4-förändringar."
        ),
        query_templates=(
            "{topic} Google Ads GA4 official update",
            "{topic} {market} seasonal demand search intent",
        ),
        deliverable_sections=(
            "Plattformsändringar",
            "Sökords- & intentionstolkning",
            "Rekommenderade nästa steg (HITL)",
        ),
        recommended_tools=("web_search", "web_research_start", "web_fetch_extract"),
        preferred_providers=("exa", "parallel", "brave", "tavily"),
        market_defaults=("SE", "NO", "DK", "en"),
        freshness_days=365,
        corroboration_mode="triangulated_market",
        authority_domains=(
            "support.google.com",
            "developers.google.com",
            "blog.google",
        ),
        pii_strictness="high",
        weights={
            "relevance": 0.30,
            "authority": 0.35,
            "freshness": 0.25,
            "signal_density": 0.10,
        },
        max_fetch_default=3,
    ),
    "deep_general": SearchProfile(
        id="deep_general",
        title="Djup Strategisk Research",
        description="Open-ended multi-round strategic and operational research.",
        goal="Bred och djup triangulering över flera sökmotorer och rundor när frågan saknar smal specialprofil.",
        ambition="deep",
        combination_mode="staged",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        round_providers={
            1: ("parallel", "exa"),
            2: ("tavily", "brave"),
            3: ("parallel", "exa", "tavily"),
        },
        fetch_strategy="hybrid",
        output_format_default="brief",
        when_to_use=(
            "Standardprofil för öppna eller tvärfunktionella research-frågor i chat, CLI och Dashboard."
        ),
        query_templates=(
            "{topic} {market} overview evidence sources",
            "{topic} primary sources comparison risks",
        ),
        deliverable_sections=(
            "Sammanfattning",
            "Viktiga fynd [S#]",
            "Rekommendationer",
            "Källkonflikter & luckor",
        ),
        recommended_tools=("web_research_start", "web_search", "web_fetch_extract"),
        preferred_providers=("parallel", "exa", "tavily", "brave"),
        market_defaults=("SE", "en"),
        freshness_days=365,
        corroboration_mode="triangulated_factual",
        authority_domains=(),
        pii_strictness="high",
        weights={
            "relevance": 0.35,
            "authority": 0.35,
            "freshness": 0.15,
            "signal_density": 0.15,
        },
        max_fetch_default=4,
    ),
    "quick_pulse": SearchProfile(
        id="quick_pulse",
        title="Snabbpuls & Färska Händelser",
        description="Ultra-fast low-latency news, carrier disruption, and status pulse (snippet-first).",
        goal="Leverera snabbast möjliga nulägeskoll på färska händelser och driftstörningar med minimal budget.",
        ambition="quick",
        combination_mode="failover",
        fanout_by_depth={"quick": 1, "standard": 1, "deep": 2},
        fetch_strategy="snippet_only",
        output_format_default="brief",
        when_to_use=(
            "Akuta statuskollar, senaste 14 dagarnas nyheter, release notes eller prisförändringar utan helsideshämtning."
        ),
        query_templates=(
            "{topic} {market} senaste nyheter status idag",
            "{topic} changelog release notice",
        ),
        deliverable_sections=("Nulägesbild", "Källor med datum"),
        recommended_tools=("web_search",),
        preferred_providers=("brave", "parallel", "tavily", "exa"),
        market_defaults=("SE", "NO", "DK", "en"),
        freshness_days=14,
        corroboration_mode="strict_factual",
        authority_domains=(),
        pii_strictness="strict",
        weights={
            "relevance": 0.25,
            "authority": 0.25,
            "freshness": 0.40,
            "signal_density": 0.10,
        },
        max_fetch_default=0,
    ),
    "product_spec_deep": SearchProfile(
        id="product_spec_deep",
        title="Produkt- & Hårdvaruspecifikation",
        description="OEM fitment, CarPlay/Android Auto/CAN-bus specs, wiring & product copy facts.",
        goal="Verifiera exakta tekniska data, bilmodells-kompatibilitet, kontakter, spänning och OEM-begränsningar.",
        ambition="standard",
        combination_mode="ensemble",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        fetch_strategy="parallel_extract_first",
        output_format_default="spec_sheet",
        when_to_use=(
            "Före produktbeskrivning (`product-desc --research`), passformskontroll eller CAN-bus/OEM-verifiering."
        ),
        query_templates=(
            "{topic} technical specifications compatibility manual",
            "{topic} CAN-bus wiring harness OEM pinout limitations",
            "{topic} {market} passform specifikation",
        ),
        deliverable_sections=(
            "Tekniska specifikationer",
            "Kompatibilitet & krav",
            "Kända begränsningar",
            "Verifierade källor [S#]",
        ),
        recommended_tools=("web_research_start", "web_search", "web_fetch_extract"),
        preferred_providers=("parallel", "exa", "brave", "tavily"),
        market_defaults=("en", "SE"),
        freshness_days=730,
        corroboration_mode="authoritative_primary",
        authority_domains=(
            "developer.apple.com",
            "android.com",
            "crutchfield.com",
            "github.com",
        ),
        pii_strictness="standard",
        weights={
            "relevance": 0.35,
            "authority": 0.35,
            "freshness": 0.10,
            "signal_density": 0.20,
        },
        max_fetch_default=4,
    ),
    "seo_content_radar": SearchProfile(
        id="seo_content_radar",
        title="SEO, Sökintention & Innehållsluckor",
        description="Nordic search intent, PAA questions, schema markup, and copy differentiation angles.",
        goal="Identifiera nordiska sökintentioner, vanliga köparfrågor, innehållsluckor och SEO-struktur.",
        ambition="standard",
        combination_mode="ensemble",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        fetch_strategy="direct",
        output_format_default="checklist",
        when_to_use=(
            "Inför nya kategorisidor, landningssidor, produkttexter eller SEO/strukturerad data-lyft."
        ),
        query_templates=(
            "{topic} {market} köpguide vanliga frågor",
            "{topic} SEO structured data schema ecommerce best practices",
        ),
        deliverable_sections=(
            "Sökintention & kluster",
            "Vanliga kundfrågor (FAQ-uppslag)",
            "Innehållsluckor & copy-vinklar",
        ),
        recommended_tools=("web_search", "web_research_start", "web_fetch_extract"),
        preferred_providers=("exa", "brave", "tavily", "parallel"),
        market_defaults=("SE", "NO", "DK"),
        freshness_days=365,
        corroboration_mode="triangulated_market",
        authority_domains=(
            "developers.google.com",
            "ahrefs.com",
            "semrush.com",
            "moz.com",
        ),
        pii_strictness="high",
        weights={
            "relevance": 0.35,
            "authority": 0.25,
            "freshness": 0.20,
            "signal_density": 0.20,
        },
        max_fetch_default=3,
    ),
    "seo_vendor_vetting": SearchProfile(
        id="seo_vendor_vetting",
        title="SEO Länkleverantör & Anti-PBN Granskning",
        description=(
            "Vetting Nordic & European white-hat SEO link marketplaces and digital PR "
            "suppliers (SE/NO/DK) against anti-PBN and Nordic ad-disclosure standards."
        ),
        goal=(
            "Granska nordiska länkleverantörer, publicistkvalitet, pristransparens, "
            "anti-PBN-skydd och Konsumentverkets/Forbrukertilsynets annonsmärkning."
        ),
        ambition="standard",
        combination_mode="staged",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        round_providers={
            1: ("parallel", "brave"),
            2: ("exa", "tavily"),
            3: ("parallel", "exa", "brave"),
        },
        fetch_strategy="direct",
        output_format_default="comparison",
        when_to_use=(
            "Vid urval, ryktes- och anti-PBN-granskning av nordiska länkleverantörer "
            "och digital PR-plattformar inför länkpaket."
        ),
        query_templates=(
            "{topic} {market} white-hat link building marketplace reviews transparency",
            "{topic} anti-PBN publisher preview sponsored disclosure {market}",
        ),
        deliverable_sections=(
            "Granskade länkleverantörer",
            "Anti-PBN & transparensbedömning",
            "Nordisk marknadstäckning (SE/NO/DK)",
            "Rekommendation & annonsmärkning [S#]",
        ),
        recommended_tools=("web_search", "web_research_start", "web_fetch_extract"),
        preferred_providers=("parallel", "brave", "exa", "tavily"),
        market_defaults=("SE", "NO", "DK"),
        freshness_days=365,
        corroboration_mode="triangulated_market",
        authority_domains=(
            "konsumentverket.se",
            "forbrukertilsynet.no",
            "forbrug.dk",
            "ahrefs.com",
            "semrush.com",
        ),
        pii_strictness="high",
        weights={
            "relevance": 0.30,
            "authority": 0.35,
            "freshness": 0.15,
            "signal_density": 0.20,
        },
        max_fetch_default=3,
        skill_doc="skills/research-profiles/SKILL.md",
    ),
    "regulatory_compliance": SearchProfile(
        id="regulatory_compliance",
        title="Regelverk, Juridik & Compliance",
        description="Nordic consumer law, GDPR, Consent Mode legality, warranty, and CE/RoHS checks.",
        goal="Säkerställ spårbara primärkällor inom nordisk/EU konsumentlag, GDPR, ångerrätt och CE-krav.",
        ambition="deep",
        combination_mode="staged",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        round_providers={
            1: ("parallel", "brave"),
            2: ("tavily", "exa"),
            3: ("parallel", "brave", "searxng"),
        },
        fetch_strategy="direct",
        output_format_default="risk_brief",
        when_to_use=(
            "Vid juridiska villkor, reklamation/garanti på SE/NO/DK, GDPR-beslut eller produktsäkerhet."
        ),
        query_templates=(
            "{topic} {market} lag föreskrift officiell myndighet",
            "{topic} EU directive regulation compliance requirements",
        ),
        deliverable_sections=(
            "Gällande regelverk & jurisdiktion",
            "Officiella primärkällor [S#]",
            "Tolkningsskillnader & riskbedömning",
            "Kräv mänsklig eskalering/godkännande",
        ),
        recommended_tools=("web_research_start", "web_search", "web_fetch_extract"),
        preferred_providers=("parallel", "brave", "tavily", "exa", "searxng"),
        market_defaults=("SE", "NO", "DK"),
        freshness_days=180,
        corroboration_mode="strict_factual",
        authority_domains=(
            "konsumentverket.se",
            "imy.se",
            "forbrukerradet.no",
            "lovdata.no",
            "forbrug.dk",
            "eur-lex.europa.eu",
        ),
        pii_strictness="strict",
        weights={
            "relevance": 0.20,
            "authority": 0.55,
            "freshness": 0.15,
            "signal_density": 0.10,
        },
        max_fetch_default=4,
    ),
    "conversion_teardown": SearchProfile(
        id="conversion_teardown",
        title="CRO & Konverteringsanalys",
        description="Deep conversion audit combing Exa UX studies, Parallel extraction, and Brave/Tavily stores.",
        goal="Kombinera semantiska UX-studier, djupt innehållsutdrag och live-e-handelsexempel för maximerad konvertering.",
        ambition="deep",
        combination_mode="staged",
        fanout_by_depth={"quick": 1, "standard": 2, "deep": 3},
        round_providers={
            1: ("exa", "parallel"),
            2: ("tavily", "brave"),
            3: ("parallel", "exa"),
        },
        fetch_strategy="parallel_extract_first",
        output_format_default="ux_patterns",
        when_to_use=(
            "Djup CRO-genomgång av tacksida, checkout, varukorg eller högtrafik-landningssidor i WordPress."
        ),
        query_templates=(
            "{topic} conversion rate optimization empirical research",
            "{topic} {market} ecommerce checkout teardown friction",
            "{topic} accessibility WCAG trust signals mobile UX",
        ),
        deliverable_sections=(
            "Friktionspunkter & CRO-hypoteser",
            "Evidensbaserade UX-mönster [S#]",
            "Prioriterad åtgärdsplan för WP-staging",
        ),
        recommended_tools=(
            "web_research_start",
            "web_fetch_extract",
            "web_search",
            "wp_workspace_attach_research",
        ),
        preferred_providers=("exa", "parallel", "tavily", "brave"),
        market_defaults=("en", "SE", "NO", "DK"),
        freshness_days=540,
        corroboration_mode="inspiration_and_heuristics",
        authority_domains=(
            "baymard.com",
            "nngroup.com",
            "cxl.com",
            "smashingmagazine.com",
            "web.dev",
        ),
        pii_strictness="standard",
        weights={
            "relevance": 0.30,
            "authority": 0.35,
            "freshness": 0.15,
            "signal_density": 0.20,
        },
        max_fetch_default=4,
    ),
    "metasearch_triangulation": SearchProfile(
        id="metasearch_triangulation",
        title="Maximal Metasök & Triangulering",
        description="All-engine ensemble fusion across Parallel, Exa, Brave, Tavily, and opt-in SearXNG.",
        goal="Korsverifiera komplexa påståenden över så många oberoende sökindex och extraheringsvägar som möjligt.",
        ambition="deep",
        combination_mode="ensemble",
        fanout_by_depth={"quick": 2, "standard": 3, "deep": 4},
        fetch_strategy="hybrid",
        output_format_default="matrix",
        when_to_use=(
            "När högsta tillförlitlighet, källbredd och konfliktupptäckt är viktigare än latens och sökkvot."
        ),
        query_templates=(
            "{topic} {market} independent verification",
            "{topic} contradicting claims primary evidence",
        ),
        deliverable_sections=(
            "Konsensusmatris",
            "Källkonflikter & avvikelser",
            "Fullständig citattabell [S#]",
        ),
        recommended_tools=("web_research_start", "web_search", "web_fetch_extract"),
        preferred_providers=("parallel", "exa", "brave", "tavily", "searxng"),
        market_defaults=("SE", "NO", "DK", "en"),
        freshness_days=365,
        corroboration_mode="triangulated_factual",
        authority_domains=(),
        pii_strictness="high",
        weights={
            "relevance": 0.30,
            "authority": 0.35,
            "freshness": 0.15,
            "signal_density": 0.20,
        },
        max_fetch_default=5,
    ),
}

_RECOMMEND_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "regulatory_compliance",
        re.compile(
            r"\b(?:gdpr|konsumentlag\w*|konsumentverk\w*|forbrukerr[åa]d\w*|lovdata|juridik|"
            r"regelv\w*|compliance|ce-m[äa]rk\w*|rohs|[åa]ngerr[äa]tt|imy|policykrav)\b",
            re.I,
        ),
        6,
    ),
    (
        "conversion_teardown",
        re.compile(
            r"\b(?:cro|konvertering\w*|teardown|friktion|checkout|varukorg|"
            r"tacksid\w*|thank[- ]?you|funnel|a/b[- ]?test)\b",
            re.I,
        ),
        5,
    ),
    (
        "product_spec_deep",
        re.compile(
            r"\b(?:carplay|android\s*auto|can[- ]?bus|dab\+|oem|specifikation\w*|"
            r"kompatibilitet|passform|kablage|h[åa]rdvara|produktbeskrivning|sku|montering)\b",
            re.I,
        ),
        6,
    ),
    (
        "seo_vendor_vetting",
        re.compile(
            r"\b(?:l[äa]nkleverant[öo]r\w*|l[äa]nkshop\w*|link\s*vendor\w*|link\s*building\s+marketplace|"
            r"bazoom|whitepress|linkhouse|prnews\.io|getfluence|seeders|anti[- ]?pbn|"
            r"l[äa]nkmarknadsplats\w*|digital\s+pr\s+leverant[öo]r\w*)\b",
            re.I,
        ),
        7,
    ),
    (
        "seo_content_radar",
        re.compile(
            r"\b(?:seo|s[öo]kintention|schema\.org|strukturerad\s+data|inneh[åa]llsluck\w*|"
            r"kategorisid\w*|copy|meta[- ]?beskrivning|keyword\s+gap)\b",
            re.I,
        ),
        5,
    ),
    (
        "metasearch_triangulation",
        re.compile(
            r"\b(?:triangul\w*|metas[öo]k|alla\s+s[öo]kmotorer|searxng|"
            r"korsverifier\w*|max(?:imal)?\s+k[äa]llbredd)\b",
            re.I,
        ),
        6,
    ),
    (
        "quick_pulse",
        re.compile(
            r"\b(?:snabbkoll|snabbpuls|nul[äa]ge|senaste\s+nytt|nyheter|release\s+notes|"
            r"idag|akut|status\s+just\s+nu)\b",
            re.I,
        ),
        5,
    ),
    (
        "support_fact_check",
        re.compile(
            r"\b(?:postnord|bring|dhl|paketombud|driftst[öo]rning|frakt\w*|"
            r"reklamation|garanti|faktagransk\w*)\b",
            re.I,
        ),
        5,
    ),
    (
        "marketing_intel",
        re.compile(
            r"\b(?:google\s+ads|ga4|consent\s+mode|roas|adwords|kampanj\w*|"
            r"negativa\s+s[öo]kord|s[äa]songseffekt)\b",
            re.I,
        ),
        5,
    ),
    (
        "technical_docs",
        re.compile(
            r"\b(?:woocommerce|wordpress|hook(?:s)?|filter|hpos|php|css|theme\.json|"
            r"block\s+theme|api|dokumentation|docs|plugin)\b",
            re.I,
        ),
        4,
    ),
    (
        "design_inspiration",
        re.compile(
            r"\b(?:design\w*|ux|ui|layout\w*|inspiration|landningssid\w*|"
            r"produktsid\w*|redesign\w*|visuell)\b",
            re.I,
        ),
        4,
    ),
    (
        "competitor_market",
        re.compile(
            r"\b(?:konkurrent\w*|competitor\w*|marknad\w*|market\w*|prisj[äa]mf[öo]relse|"
            r"positionering|usp|bransch\w*)\b",
            re.I,
        ),
        4,
    ),
)


def _str_tuple(raw: Any, *, lower: bool = True) -> tuple[str, ...]:
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = [str(x or "") for x in raw]
    else:
        return ()
    out: list[str] = []
    for item in items:
        clean = item.strip()
        if lower:
            clean = clean.lower()
        if clean and clean not in out:
            out.append(clean)
    return tuple(out)


def _merge_profile(base: SearchProfile, override: dict[str, Any] | None) -> SearchProfile:
    if not override:
        return base
    kwargs: dict[str, Any] = {}
    if override.get("title"):
        kwargs["title"] = str(override["title"]).strip()
    if override.get("description"):
        kwargs["description"] = str(override["description"]).strip()
    if override.get("goal"):
        kwargs["goal"] = str(override["goal"]).strip()
    if override.get("ambition"):
        amb = str(override["ambition"]).strip().lower()
        if amb in _VALID_AMBITIONS:
            kwargs["ambition"] = amb
    if override.get("combination_mode"):
        cmode = str(override["combination_mode"]).strip().lower()
        if cmode in _VALID_COMBINATION_MODES:
            kwargs["combination_mode"] = cmode
    if isinstance(override.get("fanout_by_depth"), dict):
        merged_fanout = dict(base.fanout_by_depth)
        for k, v in override["fanout_by_depth"].items():
            depth_k = str(k).strip().lower()
            if depth_k in _VALID_AMBITIONS:
                try:
                    merged_fanout[depth_k] = max(1, min(5, int(v)))
                except (TypeError, ValueError):
                    continue
        kwargs["fanout_by_depth"] = merged_fanout
    if isinstance(override.get("round_providers"), dict):
        parsed_rounds: dict[int, tuple[str, ...]] = dict(base.round_providers)
        for k, v in override["round_providers"].items():
            try:
                rnum = max(1, int(k))
            except (TypeError, ValueError):
                continue
            provs = _str_tuple(v, lower=True)
            if provs:
                parsed_rounds[rnum] = provs
        kwargs["round_providers"] = parsed_rounds
    if override.get("fetch_strategy"):
        fstrat = str(override["fetch_strategy"]).strip().lower()
        if fstrat in _VALID_FETCH_STRATEGIES:
            kwargs["fetch_strategy"] = fstrat
    if override.get("output_format_default"):
        kwargs["output_format_default"] = str(override["output_format_default"]).strip().lower()
    if override.get("when_to_use"):
        kwargs["when_to_use"] = str(override["when_to_use"]).strip()
    if isinstance(override.get("query_templates"), (list, tuple)):
        qtpl = tuple(str(x).strip() for x in override["query_templates"] if str(x or "").strip())
        if qtpl:
            kwargs["query_templates"] = qtpl
    if isinstance(override.get("deliverable_sections"), (list, tuple)):
        dsec = tuple(str(x).strip() for x in override["deliverable_sections"] if str(x or "").strip())
        if dsec:
            kwargs["deliverable_sections"] = dsec
    if isinstance(override.get("recommended_tools"), (list, tuple)):
        rtools = tuple(str(x).strip() for x in override["recommended_tools"] if str(x or "").strip())
        if rtools:
            kwargs["recommended_tools"] = rtools
    if override.get("skill_doc"):
        kwargs["skill_doc"] = str(override["skill_doc"]).strip()
    if isinstance(override.get("preferred_providers"), (list, tuple)):
        provs = _str_tuple(override["preferred_providers"], lower=True)
        if provs:
            kwargs["preferred_providers"] = provs
    if isinstance(override.get("market_defaults"), (list, tuple)):
        mkts = tuple(str(m).strip() for m in override["market_defaults"] if str(m or "").strip())
        if mkts:
            kwargs["market_defaults"] = mkts
    if "freshness_days" in override and override["freshness_days"] is not None:
        kwargs["freshness_days"] = max(1, int(override["freshness_days"]))
    if override.get("corroboration_mode"):
        kwargs["corroboration_mode"] = str(override["corroboration_mode"]).strip()
    if isinstance(override.get("authority_domains"), (list, tuple)):
        doms = tuple(
            str(d).strip().lower().lstrip(".")
            for d in override["authority_domains"]
            if str(d or "").strip()
        )
        kwargs["authority_domains"] = doms
    if override.get("pii_strictness"):
        kwargs["pii_strictness"] = str(override["pii_strictness"]).strip().lower()
    if isinstance(override.get("weights"), dict):
        merged_w = dict(base.weights)
        for k, v in override["weights"].items():
            try:
                merged_w[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        kwargs["weights"] = merged_w
    if "max_fetch_default" in override and override["max_fetch_default"] is not None:
        kwargs["max_fetch_default"] = max(0, int(override["max_fetch_default"]))
    return replace(base, **kwargs)


def _merged_catalog() -> dict[str, SearchProfile]:
    cfg = load_research_config()
    out: dict[str, SearchProfile] = {}
    for pid, base in _BUILTIN_PROFILES.items():
        out[pid] = _merge_profile(base, cfg.profiles.get(pid))
    for pid, raw in cfg.profiles.items():
        if pid not in out and isinstance(raw, dict):
            out[pid] = _merge_profile(SearchProfile(id=pid), raw)
    return out


def get_profile(name: str) -> SearchProfile:
    """Return the resolved ``SearchProfile`` for ``name`` (with YAML overrides)."""
    key = (name or "").strip().lower()
    catalog = _merged_catalog()
    if key not in catalog:
        raise KeyError(f"Unknown research profile: {name}")
    return catalog[key]


def list_profiles() -> list[SearchProfile]:
    """Return all configured ``SearchProfile`` objects in stable order."""
    return list(_merged_catalog().values())


def providers_for_round(profile: SearchProfile, round_num: int) -> tuple[str, ...]:
    """Return ordered provider IDs for a given research round (honoring staged schedules)."""
    ordered: list[str] = []
    if profile.combination_mode == "staged" and profile.round_providers:
        r_idx = max(1, int(round_num))
        chosen = profile.round_providers.get(r_idx)
        if chosen is None:
            lower_keys = sorted(k for k in profile.round_providers if k <= r_idx)
            if lower_keys:
                chosen = profile.round_providers[lower_keys[-1]]
            else:
                chosen = profile.round_providers[min(profile.round_providers)]
        for p in chosen:
            if p not in ordered:
                ordered.append(p)
    for p in profile.preferred_providers:
        if p not in ordered:
            ordered.append(p)
    return tuple(ordered)


def recommend_profile(
    goal: str,
    *,
    ambition: str | None = None,
) -> SearchProfile:
    """Select the best matching ``SearchProfile`` for a free-text goal and optional ambition."""
    text = (goal or "").strip()
    norm_amb = (ambition or "").strip().lower()
    if norm_amb not in _VALID_AMBITIONS:
        norm_amb = ""

    catalog = _merged_catalog()
    scores: dict[str, int] = {pid: 0 for pid in catalog}
    for pid, pat, weight in _RECOMMEND_RULES:
        if pid in scores and pat.search(text):
            scores[pid] += weight

    if norm_amb:
        for pid, prof in catalog.items():
            if prof.ambition == norm_amb:
                scores[pid] += 2

    best_id = max(scores, key=lambda pid: scores[pid])
    if scores[best_id] <= 0 or (
        norm_amb and scores[best_id] <= 2 and not any(pat.search(text) for _, pat, _ in _RECOMMEND_RULES)
    ):
        if norm_amb == "quick" and "quick_pulse" in catalog:
            return catalog["quick_pulse"]
        return catalog.get("deep_general") or next(iter(catalog.values()))
    return catalog[best_id]


__all__ = [
    "get_profile",
    "list_profiles",
    "providers_for_round",
    "recommend_profile",
]
