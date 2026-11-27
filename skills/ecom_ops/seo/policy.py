"""SEO Studio policy gates, quality thresholds, RBAC helpers, and anchor-mix guardrails."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from ecom_ops.rbac import AccessDenied, Actor, Permission, require_permission, resolve_actor
from ecom_ops.runtime_env import env_flag
from ecom_ops.seo.models import AnchorMixReport, normalize_market
from ecom_ops.wp_ops.config import is_mock_mode

# --------------------------------------------------------------------------- #
# On-page, meta, media, and thin-content thresholds
# --------------------------------------------------------------------------- #

SEO_TITLE_MIN = 30
SEO_TITLE_MAX = 60
META_DESC_MIN = 110
META_DESC_MAX = 158
ALT_TEXT_MIN = 5
ALT_TEXT_MAX = 125
THIN_CONTENT_PRODUCT_WORDS = 150
THIN_CONTENT_PAGE_WORDS = 300
IMAGE_OVERSIZE_WARN_BYTES = 250_000
IMAGE_OVERSIZE_HIGH_BYTES = 500_000
FIX_PLAN_TTL_HOURS = 12

GENERIC_FILENAME_RE = re.compile(
    r"^(?:"
    r"img[_-]?\d+|"
    r"dsc[_-]?\d+|"
    r"screenshot[\s_-]*.*|"
    r"sk[aä]rmavbild[\s_-]*.*|"
    r"skjermbilde[\s_-]*.*|"
    r"image[_-]?\d*|"
    r"photo[_-]?\d*|"
    r"bild[_-]?\d*|"
    r"untitled[\s_-]*.*|"
    r"p\d{5,}|"
    r"\d{3,}"
    r")(?:\.[a-z0-9]{2,5})?$",
    re.I,
)

GENERIC_ALT_PHRASES: frozenset[str] = frozenset(
    {
        "image",
        "img",
        "photo",
        "picture",
        "bild",
        "bilde",
        "billede",
        "foto",
        "produkt",
        "product",
        "produktbild",
        "produktbilde",
        "banner",
        "logo",
        "thumbnail",
        "placeholder",
        "untitled",
        "screenshot",
        "skärmavbild",
    }
)

GENERIC_ANCHOR_PHRASES: frozenset[str] = frozenset(
    {
        "klicka här",
        "läs mer",
        "läs mer här",
        "här",
        "se mer",
        "se utbudet",
        "se produkter",
        "besök hemsidan",
        "denna guide",
        "denna sida",
        "länk",
        "hemsida",
        "köp här",
        "les mer",
        "les mer her",
        "klikk her",
        "her",
        "se utvalget",
        "besøk nettsiden",
        "denne siden",
        "læs mere",
        "læs mere her",
        "klik her",
        "se udvalget",
        "besøg hjemmesiden",
        "click here",
        "read more",
        "here",
        "visit site",
        "learn more",
        "this page",
        "website",
        "link",
        "more info",
    }
)

SPAM_PBN_RE = re.compile(
    r"\b(?:"
    r"pbn|"
    r"private\s+blog\s+network|"
    r"link\s*farm|l[aä]nkfarm|lenkefarm|"
    r"link\s*wheel|"
    r"comment\s+spam|forum\s+spam|"
    r"xrumer|gsa\s+ser|"
    r"cloaking|doorway\s+pages?|"
    r"hidden\s+links?|"
    r"casino|kasino|gambling|betting|slots|poker|"
    r"payday\s+loan|snabbl[aå]n|forbruksl[aå]n|"
    r"viagra|cialis|porn|adult\s+links?|"
    r"\d{3,}\s+(?:backlinks?|links?)\s+blast|"
    r"cheap\s+bulk\s+links?"
    r")\b",
    re.I,
)


def contains_spam_or_pbn(text: str) -> tuple[bool, list[str]]:
    """Return ``(detected, matched_terms)`` when spam, PBN, or toxic-niche signals appear."""
    raw = str(text or "")
    if not raw:
        return False, []
    found: list[str] = []
    for candidate in (raw, re.sub(r"[-_]+", " ", raw)):
        for match in SPAM_PBN_RE.finditer(candidate):
            term = " ".join(match.group(0).strip().lower().split())
            if term and term not in found:
                found.append(term)
    return bool(found), found


def _contains_spam_or_pbn(text: str) -> bool:
    return contains_spam_or_pbn(text)[0]

_MARKET_BRAND_DOMAINS: dict[str, str] = {
    "no": "azom.no",
    "se": "azom.se",
    "dk": "azom.dk",
}

_MARKET_GENERIC_ANCHORS: dict[str, tuple[str, ...]] = {
    "no": ("les mer her", "se utvalget", "besøk nettsiden"),
    "se": ("läs mer här", "se utbudet", "besök hemsidan"),
    "dk": ("læs mere her", "se udvalget", "besøg hjemmesiden"),
}

_MARKET_PARTIAL_TEMPLATES: dict[str, tuple[str, ...]] = {
    "no": (
        "guide til {kw}",
        "oppgradere bilen med {kw}",
        "beste valg for {kw}",
        "slik fungerer {kw}",
    ),
    "se": (
        "guide till {kw}",
        "uppgradera bilen med {kw}",
        "bästa val för {kw}",
        "så fungerar {kw}",
    ),
    "dk": (
        "guide til {kw}",
        "opgrader bilen med {kw}",
        "bedste valg til {kw}",
        "sådan fungerer {kw}",
    ),
}

_BRAND_TERMS: frozenset[str] = frozenset(
    {
        "azom",
        "azom.no",
        "azom.se",
        "azom.dk",
        "azom nordic",
        "azom bilstereo",
        "azom butik",
        "azom nettbutikk",
        "azom shop",
    }
)


# --------------------------------------------------------------------------- #
# Runtime write gates & kill switches
# --------------------------------------------------------------------------- #


def seo_kill_active() -> bool:
    """True when ``AZOM_SEO_KILL`` or ``AZOM_WP_DEPLOY_KILL`` is enabled."""
    return env_flag("AZOM_SEO_KILL") or env_flag("AZOM_WP_DEPLOY_KILL")


def seo_write_allowed() -> tuple[bool, str]:
    """Fail-closed write gate for SEO mutations (``seo_meta``, ``media_alt``, etc.).

    - Kill switches (``AZOM_SEO_KILL`` / ``AZOM_WP_DEPLOY_KILL``) ALWAYS block.
    - In mock mode (``AZOM_USE_MOCK=1`` or unset) with kill switches off: ``(True, "mock")``.
    - In live mode (``AZOM_USE_MOCK=0``): requires ``AZOM_SEO_WRITE_ENABLED=1`` -> ``(True, "live")``.
    """
    if env_flag("AZOM_SEO_KILL"):
        return False, "seo_kill_switch"
    if env_flag("AZOM_WP_DEPLOY_KILL"):
        return False, "wp_deploy_kill_switch"
    if is_mock_mode():
        return True, "mock"
    if not env_flag("AZOM_SEO_WRITE_ENABLED"):
        return False, "seo_write_disabled"
    return True, "live"


# --------------------------------------------------------------------------- #
# RBAC enforcement helpers (reuse existing Permissions — zero new roles)
# --------------------------------------------------------------------------- #


def require_seo_read(actor_name: str | Actor) -> Actor:
    actor = actor_name if isinstance(actor_name, Actor) else resolve_actor(actor_name)
    if actor.has(Permission.MARKETING_READ) or actor.has(Permission.WP_OPS_READ):
        return actor
    raise AccessDenied(
        f"Actor {actor.name!r} (role={actor.role}) lacks MARKETING_READ or WP_OPS_READ"
    )


def require_seo_propose(actor_name: str | Actor) -> Actor:
    actor = actor_name if isinstance(actor_name, Actor) else resolve_actor(actor_name)
    if (
        actor.has(Permission.MARKETING_SUGGEST)
        or actor.has(Permission.WP_OPS_OPERATE)
        or actor.has(Permission.WP_OPS_ADMIN)
    ):
        return actor
    raise AccessDenied(
        f"Actor {actor.name!r} (role={actor.role}) lacks MARKETING_SUGGEST or WP_OPS_OPERATE"
    )


def require_seo_apply(actor_name: str | Actor) -> Actor:
    """Apply SEO writes to site — Oscar ``WP_OPS_ADMIN`` ONLY."""
    actor = actor_name if isinstance(actor_name, Actor) else resolve_actor(actor_name)
    require_permission(actor, Permission.WP_OPS_ADMIN)
    return actor


def require_link_package_draft(actor_name: str | Actor) -> Actor:
    """Create/submit link packages — ``MARKETING_SUGGEST`` (Jonatan + Oscar)."""
    actor = actor_name if isinstance(actor_name, Actor) else resolve_actor(actor_name)
    require_permission(actor, Permission.MARKETING_SUGGEST)
    return actor


def require_link_package_approve(actor_name: str | Actor) -> Actor:
    """Approve/order/deliver/cancel link packages — Oscar ``MARKETING_MUTATE`` ONLY."""
    actor = actor_name if isinstance(actor_name, Actor) else resolve_actor(actor_name)
    require_permission(actor, Permission.MARKETING_MUTATE)
    return actor


# --------------------------------------------------------------------------- #
# Quality helper predicates
# --------------------------------------------------------------------------- #


def is_generic_filename(filename: str) -> bool:
    leaf = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not leaf:
        return True
    return bool(GENERIC_FILENAME_RE.match(leaf))


def is_generic_alt(alt_text: str) -> bool:
    clean = " ".join(str(alt_text or "").strip().lower().split())
    if not clean:
        return True
    if clean in GENERIC_ALT_PHRASES:
        return True
    if GENERIC_FILENAME_RE.match(clean):
        return True
    return False


def is_generic_anchor(anchor_text: str) -> bool:
    clean = " ".join(str(anchor_text or "").strip().lower().split()).strip(" .,:;!?→»")
    return clean in GENERIC_ANCHOR_PHRASES


def score_to_grade(score: int) -> str:
    s = max(0, min(100, int(score)))
    if s >= 90:
        return "A"
    if s >= 80:
        return "B"
    if s >= 70:
        return "C"
    if s >= 55:
        return "D"
    return "F"


# --------------------------------------------------------------------------- #
# Natural Anchor-Text Mix & Anti-Spam Validator
# --------------------------------------------------------------------------- #


def _norm_tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9åäöøæ]+", str(text or "").lower()) if len(t) >= 2]


def _normalize_url_path(raw_url: str) -> str:
    val = str(raw_url or "").strip()
    if not val:
        return ""
    parsed = urlparse(val)
    path = parsed.path or "/"
    return ("/" + path.lstrip("/")).rstrip("/") or "/"


def _normalize_url_identity(raw_url: str) -> tuple[str, str]:
    val = str(raw_url or "").strip()
    if not val:
        return ("", "")
    parsed = urlparse(val)
    host = (parsed.hostname or "").lower()
    path = ("/" + (parsed.path or "/").lstrip("/")).rstrip("/") or "/"
    return (host, path)


def classify_anchor(
    anchor_text: str,
    *,
    target_url: str = "",
    keyword: str = "",
    market: str = "no",
    brand_terms: frozenset[str] = _BRAND_TERMS,
) -> str:
    """Classify an anchor into ``brand|naked_url|generic|partial_match|exact_match``."""
    raw = " ".join(str(anchor_text or "").strip().split())
    low = raw.lower().strip(" .,:;!?→»")
    if not low:
        return "generic"

    norm_brand_terms = frozenset(
        " ".join(str(b).strip().lower().split()) for b in brand_terms if str(b).strip()
    )
    brand_root_tokens: set[str] = set()
    for bt in norm_brand_terms:
        stem = bt.split(".", 1)[0] if ("." in bt and " " not in bt) else bt
        toks = _norm_tokens(stem)
        if len(toks) == 1:
            brand_root_tokens.add(toks[0])

    if low.startswith(("http://", "https://", "www.")):
        return "naked_url"
    if re.fullmatch(r"[a-z0-9-]+\.(?:no|se|dk|com|net|org)(?:/[^\s]*)?", low):
        return "naked_url"

    if low in GENERIC_ANCHOR_PHRASES:
        return "generic"

    kw_clean = " ".join(str(keyword or "").strip().lower().split())
    kw_tokens = set(_norm_tokens(kw_clean))
    anchor_tokens = set(_norm_tokens(low))

    if kw_clean:
        if low == kw_clean:
            return "exact_match"
        if kw_tokens and anchor_tokens == kw_tokens:
            return "exact_match"
        if len(kw_tokens) >= 2 and kw_tokens.issubset(anchor_tokens):
            return "partial_match"

    if low in norm_brand_terms or (
        bool(anchor_tokens & brand_root_tokens)
        and len(anchor_tokens) <= 2
        and not (anchor_tokens & kw_tokens)
    ):
        return "brand"

    if kw_tokens and (anchor_tokens & kw_tokens):
        return "partial_match"

    # Check against target URL slug tokens when keyword wasn't explicitly passed
    path_tokens = set(_norm_tokens(_normalize_url_path(target_url).replace("-", " "))) - {
        "produkt",
        "product",
        "kategori",
        "category",
        "guide",
        "blogg",
        "no",
        "se",
        "dk",
    }
    if path_tokens and anchor_tokens:
        overlap = anchor_tokens & path_tokens
        if overlap == anchor_tokens and len(anchor_tokens) >= 2:
            return "exact_match"
        if overlap:
            return "partial_match"

    if anchor_tokens & brand_root_tokens:
        return "brand"
    return "partial_match"


def recommend_natural_anchors(
    targets: list[dict[str, Any]] | None,
    *,
    market: str = "no",
    link_count: int = 6,
    brand_name: str = "Azom",
    base_url: str = "",
) -> list[dict[str, Any]]:
    """Auto-design a white-hat, natural anchor distribution for ``link_count`` placements.

    Targets safe proportions:
    - brand (~35%) + naked_url (~15%) + generic (~15%) >= 65%
    - partial_match (<= 25%)
    - exact_match (<= 10%)
    """
    mkt = normalize_market(market)
    count = max(1, min(50, int(link_count)))
    domain = _MARKET_BRAND_DOMAINS[mkt]
    origin = (base_url or f"https://{domain}").rstrip("/")

    # Normalize supplied targets
    cleaned: list[dict[str, Any]] = []
    for row in targets or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("target_url") or "").strip()
        kw = str(row.get("keyword") or row.get("primary_keyword") or "").strip()
        anchor = str(row.get("anchor_text") or row.get("anchor") or "").strip()
        atype = str(row.get("anchor_type") or "").strip().lower()
        cleaned.append(
            {
                "target_url": url,
                "keyword": kw,
                "anchor_text": anchor,
                "anchor_type": atype,
            }
        )

    # If user provided explicit anchor_text on all entries, preserve them while honoring link_count
    if cleaned and all(item["anchor_text"] for item in cleaned):
        out: list[dict[str, Any]] = []
        for idx in range(count):
            src = cleaned[idx % len(cleaned)]
            resolved_type = src["anchor_type"] or classify_anchor(
                src["anchor_text"],
                target_url=src["target_url"],
                keyword=src["keyword"],
                market=mkt,
            )
            out.append(
                {
                    "target_url": src["target_url"],
                    "keyword": src["keyword"],
                    "anchor_text": src["anchor_text"],
                    "anchor_type": resolved_type,
                }
            )
        return out

    # Default Nordic target pool when user passed no targets
    if not cleaned:
        default_kw = {
            "no": "trådløs CarPlay-skjerm",
            "se": "trådlös CarPlay-skärm",
            "dk": "trådløs CarPlay-skærm",
        }[mkt]
        cleaned = [
            {"target_url": f"{origin}/", "keyword": f"{brand_name} {default_kw}", "anchor_text": "", "anchor_type": ""},
            {"target_url": f"{origin}/produkt/carplay-skarm-pro/", "keyword": default_kw, "anchor_text": "", "anchor_type": ""},
            {"target_url": f"{origin}/kategori/apple-carplay/", "keyword": "Apple CarPlay bil", "anchor_text": "", "anchor_type": ""},
        ]

    # Preserve any mixed explicit-anchor entries cleanly once, then auto-fill remaining slots
    explicit_rows: list[dict[str, Any]] = []
    for item in cleaned:
        if item["anchor_text"] and len(explicit_rows) < count:
            target_url = item["target_url"]
            if target_url and not target_url.startswith(("http://", "https://")):
                target_url = f"{origin}/{target_url.lstrip('/')}"
            kw = item["keyword"] or "CarPlay-skärm"
            resolved_type = item["anchor_type"] or classify_anchor(
                item["anchor_text"], target_url=target_url, keyword=kw, market=mkt
            )
            explicit_rows.append(
                {
                    "target_url": target_url,
                    "keyword": kw,
                    "anchor_text": item["anchor_text"],
                    "anchor_type": resolved_type,
                }
            )

    auto_count = count - len(explicit_rows)
    if auto_count <= 0:
        return explicit_rows[:count]

    # Avoid single non-homepage URL > 50% concentration when count >= 4
    unique_deep_ids = {
        _normalize_url_identity(c["target_url"])
        for c in cleaned
        if c["target_url"] and _normalize_url_path(c["target_url"]) != "/"
    }
    has_homepage = any(_normalize_url_path(c["target_url"]) == "/" for c in cleaned if c["target_url"])
    if count >= 4 and len(unique_deep_ids) == 1 and not has_homepage:
        primary = cleaned[0]
        cleaned.append(
            {
                "target_url": f"{origin}/",
                "keyword": primary["keyword"] or f"{brand_name} CarPlay",
                "anchor_text": "",
                "anchor_type": "",
            }
        )

    # Compute exact slot counts matching natural white-hat mix (partial <= 25%, exact <= 10%)
    exact_n = math.floor(auto_count * 0.10)
    partial_n = math.floor(auto_count * 0.25)
    naked_n = max(1, round(auto_count * 0.15)) if auto_count >= 3 else 0
    generic_n = max(1, round(auto_count * 0.15)) if auto_count >= 4 else 0
    brand_n = max(1, auto_count - exact_n - partial_n - naked_n - generic_n)
    # Re-balance if sum != auto_count
    while brand_n + naked_n + generic_n + partial_n + exact_n > auto_count:
        if partial_n > 0:
            partial_n -= 1
        elif generic_n > 0:
            generic_n -= 1
        elif naked_n > 0:
            naked_n -= 1
        else:
            brand_n -= 1
    while brand_n + naked_n + generic_n + partial_n + exact_n < auto_count:
        brand_n += 1

    # Interleave slots so drip schedule feels natural week by week
    ordered_slots: list[str] = []
    buckets = {
        "brand": ["brand"] * brand_n,
        "partial_match": ["partial_match"] * partial_n,
        "naked_url": ["naked_url"] * naked_n,
        "generic": ["generic"] * generic_n,
        "exact_match": ["exact_match"] * exact_n,
    }
    cycle = ("brand", "partial_match", "naked_url", "brand", "generic", "exact_match")
    while len(ordered_slots) < auto_count:
        progressed = False
        for k in cycle:
            if buckets[k]:
                ordered_slots.append(buckets[k].pop())
                progressed = True
        if not progressed:
            break

    generics = _MARKET_GENERIC_ANCHORS[mkt]
    partials = _MARKET_PARTIAL_TEMPLATES[mkt]
    brand_variants = (brand_name, f"{brand_name} Nordic", f"{brand_name} Bilstereo")

    # Separate homepage vs deep targets for clean placement pairing
    homepage_targets = [c for c in cleaned if _normalize_url_path(c["target_url"]) == "/"]
    deep_targets = [c for c in cleaned if _normalize_url_path(c["target_url"]) != "/"]
    has_distinct_home = bool(homepage_targets and deep_targets)
    if not deep_targets:
        deep_targets = cleaned
    if not homepage_targets:
        homepage_targets = cleaned

    # Track per-deep-URL placements so no single deep URL exceeds 50% when count >= 4
    deep_cap = count // 2 if count >= 4 else count
    deep_url_counts: Counter[tuple[str, str]] = Counter()
    for er in explicit_rows:
        if er["target_url"] and _normalize_url_path(er["target_url"]) != "/":
            deep_url_counts[_normalize_url_identity(er["target_url"])] += 1

    result: list[dict[str, Any]] = list(explicit_rows)
    for idx, atype in enumerate(ordered_slots):
        candidate_deep = min(
            deep_targets,
            key=lambda c: deep_url_counts[_normalize_url_identity(c["target_url"])],
        )
        cand_deep_key = _normalize_url_identity(candidate_deep["target_url"])
        deep_at_cap = has_distinct_home and deep_url_counts[cand_deep_key] >= deep_cap

        if atype in {"partial_match", "exact_match"} and not deep_at_cap:
            src = candidate_deep
        elif has_distinct_home and (deep_at_cap or (atype in {"brand", "naked_url", "generic"} and idx % 2 == 0)):
            src = homepage_targets[idx % len(homepage_targets)]
        else:
            src = candidate_deep

        target_url = src["target_url"]
        if target_url and not target_url.startswith(("http://", "https://")):
            target_url = f"{origin}/{target_url.lstrip('/')}"
        if target_url and _normalize_url_path(target_url) != "/":
            deep_url_counts[_normalize_url_identity(target_url)] += 1
        kw = src["keyword"] or "CarPlay-skärm"

        if atype == "brand":
            anchor_text = brand_variants[idx % len(brand_variants)]
            final_type = "brand"
        elif atype == "naked_url":
            anchor_text = target_url or f"https://{domain}/"
            final_type = "naked_url"
        elif atype == "generic":
            anchor_text = generics[idx % len(generics)]
            final_type = "generic"
        elif atype == "partial_match":
            anchor_text = partials[idx % len(partials)].format(kw=kw)
            final_type = "partial_match"
        else:
            anchor_text = kw
            final_type = "exact_match"

        result.append(
            {
                "target_url": target_url,
                "keyword": kw,
                "anchor_text": anchor_text,
                "anchor_type": final_type,
            }
        )
    return result


def evaluate_anchor_mix(
    targets: list[dict[str, Any]],
    market: str = "no",
    link_count: int | None = None,
    *,
    notes: str = "",
    name: str = "",
) -> AnchorMixReport:
    """Evaluate natural anchor-text mix & anti-spam/anti-PBN guardrails.

    Hard-blocks (``guardrail_blocked=True``) when:
    - ``exact_match > 20%``
    - ``(brand + naked_url + generic) < 35%``
    - any empty target URL
    - spam/PBN terms detected in anchors, URLs, keywords, package name, or notes
    - zero targets provided

    Warns when:
    - ``exact_match > 10%``
    - ``partial_match > 25%``
    - ``(brand + naked_url + generic) < 60%``
    - single non-homepage URL share ``> 50%`` when ``total_links >= 4``
    """
    mkt = normalize_market(market)
    rows = [r for r in (targets or []) if isinstance(r, dict)]
    total = len(rows)

    counts: dict[str, int] = {
        "brand": 0,
        "naked_url": 0,
        "generic": 0,
        "partial_match": 0,
        "exact_match": 0,
    }
    warnings: list[str] = []
    blocking: list[str] = []

    if total == 0:
        blocking.append("Inga mållänkar angivna i länkpaketet.")
        return AnchorMixReport(
            total_links=0,
            counts=counts,
            percentages={k: 0.0 for k in counts},
            brand_url_generic_pct=0.0,
            partial_match_pct=0.0,
            exact_match_pct=0.0,
            max_single_url_share_pct=0.0,
            max_single_url="",
            warnings=warnings,
            blocking_reasons=blocking,
            guardrail_blocked=True,
            status="blocked",
        )

    if notes and _contains_spam_or_pbn(notes):
        blocking.append("Anti-spam/PBN-spärr utlöst i anteckningar (otillåten länkmetod/nisch).")
    if name and _contains_spam_or_pbn(name):
        blocking.append("Anti-spam/PBN-spärr utlöst i paketnamnet (otillåten länkmetod/nisch).")

    url_counter: Counter[tuple[str, str]] = Counter()
    url_display: dict[tuple[str, str], str] = {}
    empty_url_count = 0
    spam_hit = False

    for item in rows:
        url = str(item.get("target_url") or item.get("url") or "").strip()
        anchor = str(item.get("anchor_text") or item.get("anchor") or "").strip()
        kw = str(item.get("keyword") or item.get("primary_keyword") or "").strip()
        explicit_type = str(item.get("anchor_type") or "").strip().lower()

        if not url:
            empty_url_count += 1
        else:
            url_id = _normalize_url_identity(url)
            if url_id[1] != "/":
                url_counter[url_id] += 1
                url_display.setdefault(url_id, url)

        combined_text = f"{anchor} {kw} {url}"
        if _contains_spam_or_pbn(combined_text):
            spam_hit = True

        # Always verify classification (do not let spammy exact-match masquerade as brand)
        inferred = classify_anchor(anchor, target_url=url, keyword=kw, market=mkt)
        if explicit_type in counts and (
            explicit_type == inferred
            or (explicit_type in {"brand", "naked_url", "generic"} and inferred in {"brand", "naked_url", "generic"})
        ):
            atype = explicit_type
        else:
            atype = inferred
        counts[atype] += 1

    if empty_url_count > 0:
        blocking.append(f"{empty_url_count} länk(ar) saknar mål-URL (target_url).")
    if spam_hit:
        blocking.append(
            "Anti-spam/PBN-spärr: otillåtna termer (PBN, länkfarm, casino/spam m.m.) upptäckta."
        )

    percentages = {k: round((v / total) * 100.0, 1) for k, v in counts.items()}
    safe_count = counts["brand"] + counts["naked_url"] + counts["generic"]
    safe_pct = round((safe_count / total) * 100.0, 1)
    partial_pct = percentages["partial_match"]
    exact_pct = percentages["exact_match"]

    max_url = ""
    max_url_share = 0.0
    if url_counter:
        max_key, max_hits = url_counter.most_common(1)[0]
        max_url = url_display.get(max_key, max_key[1])
        max_url_share = round((max_hits / total) * 100.0, 1)

    if exact_pct > 20.0:
        blocking.append(
            f"Exaktmatchade ankartexter är {exact_pct}% (max tillåtet 20% — hög risk för överoptimering)."
        )
    elif exact_pct > 10.0:
        warnings.append(
            f"Exaktmatchade ankartexter är {exact_pct}% (rekommenderat <= 10%)."
        )

    if safe_pct < 35.0:
        blocking.append(
            f"Naturlig bas (varumärke + naken URL + generisk) är bara {safe_pct}% (minst 35% krävs, mål >= 60%)."
        )
    elif safe_pct < 60.0:
        warnings.append(
            f"Naturlig bas (varumärke + naken URL + generisk) är {safe_pct}% (rekommenderat >= 60%)."
        )

    if partial_pct > 25.0:
        warnings.append(
            f"Delvis matchade ankartexter är {partial_pct}% (rekommenderat <= 25%)."
        )

    effective_count = max(total, int(link_count)) if link_count is not None else total
    if effective_count >= 4 and max_url_share > 50.0:
        warnings.append(
            f"Överkoncentration: {max_url_share}% av länkarna pekar på samma undersida ({max_url}); "
            "sprid länkarna över flera målsidor + startsidan (mål <= 50%)."
        )

    blocked = bool(blocking)
    status = "blocked" if blocked else ("warn" if warnings else "healthy")
    return AnchorMixReport(
        total_links=total,
        counts=counts,
        percentages=percentages,
        brand_url_generic_pct=safe_pct,
        partial_match_pct=partial_pct,
        exact_match_pct=exact_pct,
        max_single_url_share_pct=max_url_share,
        max_single_url=max_url,
        warnings=warnings,
        blocking_reasons=blocking,
        guardrail_blocked=blocked,
        status=status,
    )


_HISTORICAL_LINK_STATUSES: frozenset[str] = frozenset(
    {"approved_for_manual_order", "ordered_external", "delivered"}
)


def evaluate_cumulative_anchor_mix(
    current_targets: list[dict[str, Any]] | None,
    historical_packages: list[dict[str, Any]] | None,
    *,
    brand_terms: frozenset[str] | list[str] | set[str] | None = None,
    base_url: str = "",
    market: str = "no",
) -> dict[str, Any]:
    """Evaluate combined anchor-text mix across current targets and historical ordered/delivered packages.

    Flags cumulative exact-match over-optimization (>10% warn, >20% block), low natural base,
    and single non-homepage URL over-concentration across the site's link history.
    """
    mkt = normalize_market(market)
    resolved_brand: set[str] = set(brand_terms) if brand_terms is not None else set(_BRAND_TERMS)
    if base_url:
        host = (urlparse(str(base_url)).hostname or "").lower().removeprefix("www.")
        if host:
            resolved_brand.add(host)
            stem = host.split(".", 1)[0]
            if len(stem) >= 2:
                resolved_brand.add(stem)
    brand_fs = frozenset(resolved_brand)

    cur_rows = [r for r in (current_targets or []) if isinstance(r, dict)]
    hist_rows: list[dict[str, Any]] = []
    hist_pkg_count = 0
    for pkg in historical_packages or []:
        if not isinstance(pkg, dict):
            continue
        st = str(pkg.get("status") or "").strip().lower()
        if st and st not in _HISTORICAL_LINK_STATUSES:
            continue
        pkg_targets = [t for t in (pkg.get("targets") or []) if isinstance(t, dict)]
        if pkg_targets:
            hist_pkg_count += 1
            hist_rows.extend(pkg_targets)

    combined = hist_rows + cur_rows
    total = len(combined)

    counts: dict[str, int] = {
        "brand": 0,
        "naked_url": 0,
        "generic": 0,
        "partial_match": 0,
        "exact_match": 0,
    }
    warnings: list[str] = []
    blocking: list[str] = []

    if total == 0:
        return {
            "total_links": 0,
            "historical_links": 0,
            "current_links": 0,
            "historical_packages_count": 0,
            "counts": counts,
            "percentages": {k: 0.0 for k in counts},
            "brand_url_generic_pct": 0.0,
            "partial_match_pct": 0.0,
            "exact_match_pct": 0.0,
            "max_single_url_share_pct": 0.0,
            "max_single_url": "",
            "warnings": [],
            "blocking_reasons": [],
            "guardrail_blocked": False,
            "status": "healthy",
        }

    url_counter: Counter[tuple[str, str]] = Counter()
    url_display: dict[tuple[str, str], str] = {}

    for item in combined:
        url = str(item.get("target_url") or item.get("url") or "").strip()
        anchor = str(item.get("anchor_text") or item.get("anchor") or "").strip()
        kw = str(item.get("keyword") or item.get("primary_keyword") or "").strip()
        explicit_type = str(item.get("anchor_type") or "").strip().lower()

        if url:
            url_id = _normalize_url_identity(url)
            if url_id[1] != "/":
                url_counter[url_id] += 1
                url_display.setdefault(url_id, url)

        inferred = classify_anchor(
            anchor,
            target_url=url,
            keyword=kw,
            market=mkt,
            brand_terms=brand_fs,
        )
        if explicit_type in counts and (
            explicit_type == inferred
            or (
                explicit_type in {"brand", "naked_url", "generic"}
                and inferred in {"brand", "naked_url", "generic"}
            )
        ):
            atype = explicit_type
        else:
            atype = inferred
        counts[atype] += 1

    percentages = {k: round((v / total) * 100.0, 1) for k, v in counts.items()}
    safe_count = counts["brand"] + counts["naked_url"] + counts["generic"]
    safe_pct = round((safe_count / total) * 100.0, 1)
    partial_pct = percentages["partial_match"]
    exact_pct = percentages["exact_match"]

    max_url = ""
    max_url_share = 0.0
    if url_counter:
        max_key, max_hits = url_counter.most_common(1)[0]
        max_url = url_display.get(max_key, max_key[1])
        max_url_share = round((max_hits / total) * 100.0, 1)

    if exact_pct > 20.0:
        blocking.append(
            f"Kumulativ exaktmatch-överoptimering: {exact_pct}% över {total} länkar "
            "(max tillåtet 20%)."
        )
    elif exact_pct > 10.0:
        warnings.append(
            f"Kumulativ exaktmatch-andel är {exact_pct}% över {total} länkar "
            "(rekommenderat <= 10%)."
        )

    if safe_pct < 35.0:
        blocking.append(
            f"Kumulativ naturlig bas (varumärke + URL + generisk) är bara {safe_pct}% "
            "(minst 35% krävs)."
        )
    elif safe_pct < 60.0:
        warnings.append(
            f"Kumulativ naturlig bas är {safe_pct}% (rekommenderat >= 60%)."
        )

    if partial_pct > 25.0:
        warnings.append(
            f"Kumulativ delvis match är {partial_pct}% (rekommenderat <= 25%)."
        )

    if total >= 4 and max_url_share > 50.0:
        warnings.append(
            f"Kumulativ URL-överkoncentration: {max_url_share}% av alla {total} länkar "
            f"pekar på {max_url} (mål <= 50%)."
        )

    blocked = bool(blocking)
    status = "blocked" if blocked else ("warn" if warnings else "healthy")
    return {
        "total_links": total,
        "historical_links": len(hist_rows),
        "current_links": len(cur_rows),
        "historical_packages_count": hist_pkg_count,
        "counts": counts,
        "percentages": percentages,
        "brand_url_generic_pct": safe_pct,
        "partial_match_pct": partial_pct,
        "exact_match_pct": exact_pct,
        "max_single_url_share_pct": max_url_share,
        "max_single_url": max_url,
        "warnings": warnings,
        "blocking_reasons": blocking,
        "guardrail_blocked": blocked,
        "status": status,
    }


__all__ = [
    "ALT_TEXT_MAX",
    "ALT_TEXT_MIN",
    "FIX_PLAN_TTL_HOURS",
    "GENERIC_ALT_PHRASES",
    "GENERIC_ANCHOR_PHRASES",
    "GENERIC_FILENAME_RE",
    "IMAGE_OVERSIZE_HIGH_BYTES",
    "IMAGE_OVERSIZE_WARN_BYTES",
    "META_DESC_MAX",
    "META_DESC_MIN",
    "SEO_TITLE_MAX",
    "SEO_TITLE_MIN",
    "SPAM_PBN_RE",
    "THIN_CONTENT_PAGE_WORDS",
    "THIN_CONTENT_PRODUCT_WORDS",
    "classify_anchor",
    "contains_spam_or_pbn",
    "evaluate_anchor_mix",
    "evaluate_cumulative_anchor_mix",
    "is_generic_alt",
    "is_generic_anchor",
    "is_generic_filename",
    "recommend_natural_anchors",
    "require_link_package_approve",
    "require_link_package_draft",
    "require_seo_apply",
    "require_seo_propose",
    "require_seo_read",
    "score_to_grade",
    "seo_kill_active",
    "seo_write_allowed",
]
