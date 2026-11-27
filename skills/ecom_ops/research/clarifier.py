"""Research scoping & precision-clarification helpers (Layer 4).

Leaf module: no ``integrations.*``, ``urllib``, ``http.client``, ``requests``,
``subprocess``, or ``bot.*`` imports.
"""

from __future__ import annotations

import re
from typing import Any

from ecom_ops.research.models import ResearchSpec
from ecom_ops.research.profiles import get_profile
from ecom_ops.research.sanitizer import sanitize_untrusted_text

RESEARCH_CLARIFY_CATEGORIES: tuple[str, ...] = (
    "research_design",
    "research_market",
    "research_tech",
    "research_general",
)

_CATEGORY_TO_PROFILE: dict[str, str] = {
    "research_design": "design_inspiration",
    "research_market": "competitor_market",
    "research_tech": "technical_docs",
    "research_general": "deep_general",
}

_VALID_DEPTHS = frozenset({"quick", "standard", "deep"})
_VALID_OUTPUT_FORMATS = frozenset(
    {"brief", "comparison", "checklist", "ux_patterns", "matrix", "spec_sheet", "risk_brief"}
)

_SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_WR_PLAN_RE = re.compile(r"^wr-[0-9a-f]{12}$")
_VALID_TARGET_REF_RE = re.compile(
    r"^(?:"
    r"wp:[a-z0-9][a-z0-9_-]{0,63}:wr-[0-9a-f]{12}|"
    r"wp_site:[a-z0-9][a-z0-9_-]{0,63}|"
    r"case:[A-Za-z0-9][A-Za-z0-9_-]{0,63}|"
    r"marketing:[A-Za-z0-9._:-]{1,64}|"
    r"product:[A-Za-z0-9._-]{1,64}|"
    r"chat:[a-z0-9_-]{1,32}:[A-Za-z0-9._:-]{1,64}"
    r")$"
)

_RESEARCH_CMD_PREFIX_RE = re.compile(r"^\s*/research(?:@[a-z0-9_]+)?\b", re.I)
_RESEARCH_SUBCOMMAND_RE = re.compile(
    r"^(?:list|ls|status|show|get|cancel|stop|steer|help|\?|res-[0-9a-f]{12})\b",
    re.I,
)
_EXPLICIT_FLAGS_RE = re.compile(
    r"(?:--(?:depth|profile|market|locale|country|focus|format)\b|"
    r"\b(?:depth|profile|market)\s*=\s*\S+)",
    re.I,
)
_SHORT_ACK_RE = re.compile(
    r"^\s*(?:"
    r"hej+|tjena|hall[åa]|tja|god\s+(?:morgon|dag|kv[äa]ll)|"
    r"tack(?:ar)?|ok(?:ej)?|fine|yes|no|ja+|japp|jo|nej+|n[äa]|"
    r"status|l[äa]ge(?:t)?|bra|perfekt|toppen|avbryt|skippa"
    r")\s*[.!?🙏👍👌✅✖️]*\s*$",
    re.I,
)
_NARROW_SEARCH_RE = re.compile(
    r"\b(?:"
    r"snabbs[öo]k|webbs[öo]k|s[öo]k\s+(?:p[åa]\s+webben|upp)|googla|"
    r"kolla\s+om\s+(?:postnord|dhl|bring)|"
    r"vad\s+s[äa]ger\s+(?:woocommerce|wordpress|konsumentverket|docs|dokumentationen)|"
    r"sl[åa]\s+upp|h[äa]mta\s+https?://"
    r")\b",
    re.I,
)
_URL_RE = re.compile(r"https?://\S+", re.I)
_BROAD_RESEARCH_RE = re.compile(
    r"\b(?:"
    r"konkurrentanalys(?:er)?|marknadsanalys(?:er)?|branschanalys(?:er)?|"
    r"djupanalys(?:er)?|deep\s+research|djup\s+research|"
    r"researcha|g[öo]r\s+(?:en\s+)?(?:djup(?:are)?\s+|bred\s+)?research|"
    r"g[öo]r\s+(?:en\s+)?(?:konkurrent|marknads|ux|design)?analys|"
    r"g[öo]r\s+en\s+utredning|kartl[äa]gg(?:ning)?|"
    r"unders[öo]k\s+(?:hur|vad|vilka|marknad\w*|konkurrent\w*|b[äa]sta\s+praxis)"
    r")\b",
    re.I,
)

_DEPTH_QUICK_RE = re.compile(r"\b(?:quick|snabb(?:t|are)?|kort(?:fattad)?|snabbt\s+sp[åa]r)\b", re.I)
_DEPTH_DEEP_RE = re.compile(r"\b(?:deep|djup(?:are)?|grundlig(?:t)?|omfattande)\b", re.I)
_DEPTH_STANDARD_RE = re.compile(r"\b(?:standard|normal|balanserad)\b", re.I)

_MARKET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SE", re.compile(r"\b(?:SE|sverige|svensk(?:a|t)?|azom\.se)\b", re.I)),
    ("NO", re.compile(r"\b(?:NO|norge|norsk(?:a|t)?|azom\.no)\b", re.I)),
    ("DK", re.compile(r"\b(?:DK|danmark|dansk(?:a|t)?|azom\.dk)\b", re.I)),
    ("EN", re.compile(r"\b(?:EN|engelsk(?:a|t)?|internationell(?:a|t)?|global(?:t|a)?)\b", re.I)),
)
_MARKET_TO_LOCALE: dict[str, str] = {
    "SE": "sv",
    "NO": "nb",
    "DK": "da",
    "EN": "en",
}

_DESIGN_KW_RE = re.compile(
    r"\b(?:design\w*|ux|ui|layout\w*|tacksid\w*|thank[- ]?you|takkeside\w*|takside\w*|"
    r"landningssid\w*|produktsid\w*|kassa(?:sid\w*)?|checkout|konvertering\w*|"
    r"inspiration|redesign\w*|kundres\w*)\b",
    re.I,
)
_MARKET_KW_RE = re.compile(
    r"\b(?:konkurrent\w*|competitor\w*|marknad\w*|market\w*|prisj[äa]mf[öo]relse|"
    r"positionering|usp|bransch\w*|nordisk(?:a|t)?)\b",
    re.I,
)
_TECH_KW_RE = re.compile(
    r"\b(?:woocommerce|wordpress|hook(?:s)?|filter|hpos|php|css|theme\.json|"
    r"api|dokumentation|docs|block\s+theme|kod(?:bas)?|plugin)\b",
    re.I,
)
_SUPPORT_KW_RE = re.compile(
    r"\b(?:postnord|bring|dhl|konsumentverk\w*|forbrukerr[åa]d\w*|lovdata|"
    r"konsumentlag\w*|garanti|reklamation|carplay|dab\+)\b",
    re.I,
)
_MARKETING_KW_RE = re.compile(
    r"\b(?:google\s+ads|ga4|consent\s+mode|roas|seo|s[öo]kord|adwords|kampanj\w*)\b",
    re.I,
)
_WP_CONTEXT_RE = re.compile(
    r"\b(?:wp|wordpress|woocommerce|sajt\w*|sid(?:a|an|or)|tacksid\w*|landningssid\w*|"
    r"tema\w*|theme|layout|ux|ui|checkout|wr-[0-9a-f]{12})\b",
    re.I,
)
_PUBLIC_DOMAIN_RE = re.compile(r"\b([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:se|no|dk|com|org|net|io|dev|eu|nu))\b", re.I)
_INTERNAL_DOMAIN_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home.arpa", ".invalid", ".example", ".test")


def is_research_clarify_category(category: str | None) -> bool:
    """Return True when ``category`` is one of the 4 research clarify categories."""
    return str(category or "").strip().lower() in _CATEGORY_TO_PROFILE


def category_to_profile(category: str | None) -> str:
    """Map a research clarification category to its canonical ``SearchProfile`` id."""
    norm = str(category or "").strip().lower()
    return _CATEGORY_TO_PROFILE.get(norm, "deep_general")


def _extract_markets_from_text(text: str) -> list[str]:
    found: list[str] = []
    for code, pat in _MARKET_PATTERNS:
        if pat.search(text) and code not in found:
            found.append(code)
    return found


def _extract_public_domains(text: str) -> list[str]:
    out: list[str] = []
    for match in _PUBLIC_DOMAIN_RE.finditer(text or ""):
        dom = match.group(1).strip().lower().lstrip(".")
        if not dom or dom.endswith(_INTERNAL_DOMAIN_SUFFIXES):
            continue
        if dom not in out:
            out.append(dom)
    return out


def _normalize_pick_strings(picks: list[Any] | tuple[Any, ...]) -> list[str]:
    out: list[str] = []
    for item in picks or ():
        if isinstance(item, dict):
            parts = [
                str(item.get("label") or "").strip(),
                str(item.get("summary") or "").strip(),
                str(item.get("action_hint") or "").strip(),
                str(item.get("id") or "").strip(),
            ]
            joined = " ".join(p for p in parts if p)
            cleaned = sanitize_untrusted_text(joined, max_chars=400)
            if cleaned:
                out.append(cleaned)
        else:
            cleaned = sanitize_untrusted_text(str(item or ""), max_chars=400)
            if cleaned:
                out.append(cleaned)
    return out


def should_trigger_research_clarify(message: str) -> bool:
    """Return True for broad/ambiguous research asks that benefit from scoping."""
    raw = (message or "").strip()
    if not raw:
        return False

    if raw.startswith("/"):
        first, _, rest = raw.partition(" ")
        cmd = first[1:].split("@", 1)[0].strip().lower()
        if cmd != "research":
            return False
        args = rest.strip()
        if not args:
            return True
        if _RESEARCH_SUBCOMMAND_RE.match(args):
            return False
        if _EXPLICIT_FLAGS_RE.search(args) or _URL_RE.search(args):
            return False
        words = args.split()
        has_market = bool(_extract_markets_from_text(args))
        has_depth = bool(
            _DEPTH_QUICK_RE.search(args)
            or _DEPTH_DEEP_RE.search(args)
            or _DEPTH_STANDARD_RE.search(args)
        )
        has_domain = bool(_extract_public_domains(args))
        scope_signals = int(has_market) + int(has_depth) + int(has_domain)
        if scope_signals >= 2 and len(words) >= 4:
            return False
        return len(words) <= 6 or scope_signals == 0

    if _SHORT_ACK_RE.match(raw):
        return False
    if _NARROW_SEARCH_RE.search(raw) or _URL_RE.search(raw):
        return False
    if not _BROAD_RESEARCH_RE.search(raw):
        return False

    has_market = bool(_extract_markets_from_text(raw))
    has_depth = bool(
        _DEPTH_QUICK_RE.search(raw)
        or _DEPTH_DEEP_RE.search(raw)
        or _DEPTH_STANDARD_RE.search(raw)
    )
    has_domain = bool(_extract_public_domains(raw))
    scope_signals = int(has_market) + int(has_depth) + int(has_domain)
    return scope_signals < 2


_ADDITIVE_NOTE_RE = re.compile(
    r"\b(?:ocks[åa]|även|dessutom|l[äa]gg\s+till|samt|inklusive|plus)\b",
    re.I,
)
_REPLACE_NOTE_RE = re.compile(
    r"\b(?:ist[äa]llet|i\s+st[äa]llet|bara|endast|enbart|byt\s+till)\b",
    re.I,
)


_EXTRA_PROFILE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("regulatory_compliance", re.compile(r"\b(?:gdpr|imy|eur-lex|ce-m[äa]rk\w*|compliance|regelv\w*)\b", re.I)),
    ("conversion_teardown", re.compile(r"\b(?:cro|teardown|friktion)\b", re.I)),
    ("product_spec_deep", re.compile(r"\b(?:can[- ]?bus|passform|kablage|oem\s+spec)\b", re.I)),
    ("metasearch_triangulation", re.compile(r"\b(?:triangul\w*|metas[öo]k|searxng)\b", re.I)),
    ("quick_pulse", re.compile(r"\b(?:snabbpuls|nul[äa]ge\s+idag|release\s+notes)\b", re.I)),
    ("seo_content_radar", re.compile(r"\b(?:inneh[åa]llsluck\w*|schema\.org|strukturerad\s+data)\b", re.I)),
)


def _is_known_profile(name: str) -> bool:
    norm = (name or "").strip().lower()
    if not norm:
        return False
    try:
        get_profile(norm)
        return True
    except KeyError:
        return False


def _match_profile_kw(text: str) -> str | None:
    if not text:
        return None
    if _DESIGN_KW_RE.search(text):
        return "design_inspiration"
    if _TECH_KW_RE.search(text):
        return "technical_docs"
    if _SUPPORT_KW_RE.search(text):
        return "support_fact_check"
    if _MARKETING_KW_RE.search(text):
        return "marketing_intel"
    if _MARKET_KW_RE.search(text):
        return "competitor_market"
    for pid, pat in _EXTRA_PROFILE_RULES:
        if pat.search(text):
            return pid
    return None


def _infer_profile(combined: str, spec_dict: dict[str, Any], *, note_text: str = "") -> str:
    note_profile = _match_profile_kw(note_text)
    if note_profile:
        return note_profile
    raw_profile = str(spec_dict.get("profile_id") or spec_dict.get("profile") or "").strip().lower()
    if _is_known_profile(raw_profile):
        return raw_profile
    raw_cat = str(spec_dict.get("category") or "").strip().lower()
    if is_research_clarify_category(raw_cat):
        return category_to_profile(raw_cat)
    return _match_profile_kw(combined) or "deep_general"


def _match_depth_kw(text: str) -> str | None:
    if not text:
        return None
    if _DEPTH_QUICK_RE.search(text):
        return "quick"
    if _DEPTH_DEEP_RE.search(text):
        return "deep"
    if _DEPTH_STANDARD_RE.search(text):
        return "standard"
    return None


def _infer_depth(combined: str, spec_dict: dict[str, Any], *, note_text: str = "") -> str:
    note_depth = _match_depth_kw(note_text)
    if note_depth:
        return note_depth
    raw_depth = str(spec_dict.get("depth") or "").strip().lower()
    if raw_depth in _VALID_DEPTHS:
        return raw_depth
    return _match_depth_kw(combined) or "standard"


def _parse_markets(
    combined: str,
    spec_dict: dict[str, Any],
    *,
    site_id: str | None,
    note_text: str = "",
) -> tuple[list[str], str, str]:
    prior_markets: list[str] = []
    raw_markets = spec_dict.get("markets") if "markets" in spec_dict else spec_dict.get("market")
    if isinstance(raw_markets, str):
        for token in raw_markets.split(","):
            norm = token.strip().upper()
            if norm and norm not in prior_markets:
                prior_markets.append(norm)
    elif isinstance(raw_markets, (list, tuple)):
        for item in raw_markets:
            norm = str(item or "").strip().upper()
            if norm and norm not in prior_markets:
                prior_markets.append(norm)

    note_markets = _extract_markets_from_text(note_text) if note_text else []
    markets: list[str] = list(prior_markets)
    markets_overridden = False
    if note_markets and prior_markets:
        is_additive = bool(_ADDITIVE_NOTE_RE.search(note_text)) and not bool(
            _REPLACE_NOTE_RE.search(note_text)
        )
        if is_additive:
            for code in note_markets:
                if code not in markets:
                    markets.append(code)
        else:
            markets = list(note_markets)
            markets_overridden = markets != prior_markets
    else:
        for code in _extract_markets_from_text(combined):
            if code not in markets:
                markets.append(code)

    if not markets and site_id:
        low_site = site_id.strip().lower()
        if low_site.endswith(("_no", "-no", ".no")):
            markets.append("NO")
        elif low_site.endswith(("_dk", "-dk", ".dk")):
            markets.append("DK")
        elif low_site.endswith(("_se", "-se", ".se")):
            markets.append("SE")

    if not markets:
        markets = ["SE"]

    raw_country = str(spec_dict.get("country") or "").strip().upper()
    if not markets_overridden and len(raw_country) == 2 and raw_country.isalpha():
        country = raw_country
    else:
        country = next((m for m in markets if m in {"SE", "NO", "DK"}), "SE")

    raw_locale = str(spec_dict.get("locale") or "").strip().lower()
    if not markets_overridden and raw_locale:
        locale = raw_locale
    else:
        primary_mkt = markets[0] if markets else country
        locale = _MARKET_TO_LOCALE.get(primary_mkt, _MARKET_TO_LOCALE.get(country, "sv"))

    return markets, locale, country


def build_research_spec(
    original_request: str,
    picks: list[str],
    notes: list[str],
    *,
    spec: dict[str, Any] | None = None,
    site_id: str | None = None,
    active_review_plan_id: str | None = None,
) -> ResearchSpec:
    """Build a clean, bounded ``ResearchSpec`` from clarification picks and notes."""
    spec_dict = dict(spec) if isinstance(spec, dict) else {}
    pick_texts = _normalize_pick_strings(picks)
    note_texts = [
        cleaned
        for n in (notes or ())
        if (cleaned := sanitize_untrusted_text(str(n or ""), max_chars=400))
    ]

    raw_topic = str(spec_dict.get("topic") or "").strip()
    if not raw_topic:
        raw_topic = _RESEARCH_CMD_PREFIX_RE.sub("", str(original_request or "")).strip()
    clean_topic = " ".join(sanitize_untrusted_text(raw_topic, max_chars=500).split())
    if not clean_topic:
        fallback_parts = note_texts or pick_texts
        clean_topic = " ".join(fallback_parts[:2]).strip()[:500] or "Allmän research"

    note_joined = " ".join(note_texts)
    combined = "\n".join([clean_topic, *pick_texts, *note_texts])
    profile_id = _infer_profile(combined, spec_dict, note_text=note_joined)
    depth = _infer_depth(
        " ".join([*pick_texts, *note_texts, clean_topic]),
        spec_dict,
        note_text=note_joined,
    )
    markets, locale, country = _parse_markets(
        combined, spec_dict, site_id=site_id, note_text=note_joined
    )

    focus_domains: list[str] = []
    queries_seed: list[str] = []

    def _add_focus_item(val: str) -> None:
        s = sanitize_untrusted_text(val, max_chars=240).strip()
        if not s:
            return
        if "." in s and " " not in s:
            dom = s.lower().lstrip(".")
            if not dom.endswith(_INTERNAL_DOMAIN_SUFFIXES) and dom not in focus_domains:
                focus_domains.append(dom)
        elif s not in queries_seed:
            queries_seed.append(s)

    raw_focus = spec_dict.get("focus_domains") if "focus_domains" in spec_dict else spec_dict.get("focus")
    if isinstance(raw_focus, str):
        for part in raw_focus.split(","):
            _add_focus_item(part)
    elif isinstance(raw_focus, (list, tuple)):
        for part in raw_focus:
            _add_focus_item(str(part or ""))

    for dom in _extract_public_domains(" ".join([*pick_texts, *note_texts])):
        if dom not in focus_domains:
            focus_domains.append(dom)

    raw_seeds = spec_dict.get("queries_seed")
    if isinstance(raw_seeds, (list, tuple)):
        for item in raw_seeds:
            cleaned = " ".join(sanitize_untrusted_text(str(item or ""), max_chars=240).split())
            if cleaned and cleaned not in queries_seed:
                queries_seed.append(cleaned)

    for note in note_texts:
        cleaned = " ".join(note.split())
        if cleaned and cleaned.lower() != clean_topic.lower() and cleaned not in queries_seed:
            queries_seed.append(cleaned[:240])

    exclude_domains: list[str] = []
    raw_excl = spec_dict.get("exclude_domains")
    if isinstance(raw_excl, str):
        raw_excl = [p.strip() for p in raw_excl.split(",") if p.strip()]
    if isinstance(raw_excl, (list, tuple)):
        for d in raw_excl:
            dom = str(d or "").strip().lower().lstrip(".")
            if dom and dom not in exclude_domains:
                exclude_domains.append(dom)

    raw_fmt = str(spec_dict.get("output_format") or "brief").strip().lower()
    output_format = raw_fmt if raw_fmt in _VALID_OUTPUT_FORMATS else "brief"

    target_ref: str | None = None
    raw_target = str(spec_dict.get("target_ref") or "").strip()
    if raw_target and _VALID_TARGET_REF_RE.fullmatch(raw_target):
        target_ref = raw_target
    else:
        clean_site = str(site_id or "").strip().lower()
        clean_plan = str(active_review_plan_id or "").strip().lower()
        if clean_site and _SITE_ID_RE.fullmatch(clean_site):
            if clean_plan and _WR_PLAN_RE.fullmatch(clean_plan):
                target_ref = f"wp:{clean_site}:{clean_plan}"
            elif (
                profile_id in {"design_inspiration", "technical_docs"}
                or bool(spec_dict.get("wp_context"))
                or bool(_WP_CONTEXT_RE.search(combined))
            ):
                target_ref = f"wp_site:{clean_site}"

    return ResearchSpec(
        topic=clean_topic,
        profile_id=profile_id,
        depth=depth,
        markets=markets,
        locale=locale,
        country=country,
        focus_domains=focus_domains[:10],
        exclude_domains=exclude_domains[:10],
        target_ref=target_ref,
        output_format=output_format,
        queries_seed=queries_seed[:6],
    )


__all__ = [
    "RESEARCH_CLARIFY_CATEGORIES",
    "build_research_spec",
    "category_to_profile",
    "is_research_clarify_category",
    "should_trigger_research_clarify",
]
