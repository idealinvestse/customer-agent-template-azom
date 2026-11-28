"""Deterministic search result normalization, multi-factor scoring, and citation verification.

Layer 2 domain module: pure stdlib + leaf research models/profiles/sanitizer.
No ``integrations.*``, ``urllib.request``, ``http.client``, ``requests``, or ``subprocess``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ecom_ops.research.models import (
    Citation,
    ClaimKind,
    ConflictNote,
    EvaluatedSource,
    EvidenceStatus,
    ResearchBrief,
    SearchProfile,
    SourceType,
)
from ecom_ops.research.sanitizer import sanitize_untrusted_text

if TYPE_CHECKING:
    from ecom_ops.integrations.search.base import SearchHit

_STRIP_QUERY_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
_CITATION_RE = re.compile(r"\[(\s*[sS]\d+(?:\s*[,;]\s*[sS]\d+)*\s*)\]")

_GOV_DOMAINS = frozenset(
    {
        "konsumentverket.se",
        "hallakonsument.se",
        "skatteverket.se",
        "tullverket.se",
        "imy.se",
        "riksdagen.se",
        "regeringen.se",
        "forbrukerradet.no",
        "lovdata.no",
        "datatilsynet.no",
        "regjeringen.no",
        "forbrug.dk",
        "datatilsynet.dk",
        "borger.dk",
        "europa.eu",
        "eur-lex.europa.eu",
    }
)

_CARRIER_DOMAINS = frozenset(
    {
        "postnord.se",
        "postnord.no",
        "postnord.dk",
        "postnord.com",
        "bring.se",
        "bring.no",
        "bring.dk",
        "bring.com",
        "dhl.com",
        "dhl.se",
        "dhl.no",
        "dhl.dk",
        "ups.com",
        "fedex.com",
        "schenker.se",
        "dbschenker.com",
        "budbee.com",
        "instabox.io",
        "earlybird.se",
    }
)

_UX_DOMAINS = frozenset(
    {
        "baymard.com",
        "nngroup.com",
        "smashingmagazine.com",
        "alistapart.com",
        "uxdesign.cc",
        "uxplanet.org",
        "cxl.com",
        "conversionxl.com",
        "goodui.org",
        "builtformars.com",
        "web.dev",
    }
)

_OFFICIAL_DOCS_DOMAINS = frozenset(
    {
        "developer.wordpress.org",
        "wordpress.org",
        "woocommerce.com",
        "developer.woocommerce.com",
        "developer.mozilla.org",
        "php.net",
        "support.google.com",
        "developers.google.com",
        "w3.org",
        "schema.org",
        "learn.microsoft.com",
        "docs.python.org",
        "github.com",
    }
)

_FORUM_DOMAINS = frozenset(
    {
        "reddit.com",
        "old.reddit.com",
        "stackoverflow.com",
        "stackexchange.com",
        "wordpress.stackexchange.com",
        "quora.com",
        "flashback.org",
        "sweclockers.com",
        "byggahus.se",
    }
)

_TRADE_PRESS_DOMAINS = frozenset(
    {
        "ehandel.se",
        "ehandel.no",
        "ehandel.dk",
        "dagenshandel.se",
        "breakit.se",
        "di.se",
        "digitalcommerce360.com",
        "searchengineland.com",
        "searchenginejournal.com",
        "techcrunch.com",
        "theverge.com",
        "blog.google",
        "martech.org",
        "emarketer.com",
        "retaildive.com",
    }
)

_STORE_DOMAINS = frozenset(
    {
        "brl.se",
        "cdon.se",
        "kjell.com",
        "biltema.se",
        "mekonomen.se",
        "jula.se",
        "elgiganten.se",
        "elkjop.no",
        "elgiganten.dk",
        "komplett.se",
        "komplett.no",
        "komplett.dk",
        "power.se",
        "power.no",
        "power.dk",
        "autohifi.no",
        "bilstereo.no",
        "carsound.dk",
        "bleken.no",
        "stereospesialisten.no",
        "webhallen.com",
        "netonnet.se",
        "dustin.se",
        "azom.se",
        "azom.no",
        "azom.dk",
    }
)

_LOW_QUALITY_DOMAIN_HINTS = (
    "pinterest.",
    "coupon",
    "promo-code",
    "discount-code",
    "top10best",
    "bestreview",
    "ezinearticles",
    "slideshare.net",
)

_BOILERPLATE_HINTS = (
    "subscribe to read",
    "enable javascript",
    "403 forbidden",
    "access denied",
    "sign in to continue",
    "accept cookies to view",
)

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "how",
        "what",
        "when",
        "where",
        "your",
        "into",
        "about",
        "och",
        "att",
        "det",
        "som",
        "för",
        "med",
        "till",
        "av",
        "på",
        "är",
        "en",
        "ett",
        "hur",
        "vad",
        "när",
        "var",
        "från",
        "om",
        "og",
        "til",
        "fra",
        "hvordan",
    }
)

_NEGATIVE_SIGNAL_RE = re.compile(
    r"(?i)\b(?:outage|störning|driftstörning|avbrott|delayed|försenad|forsinket|"
    r"not supported|unsupported|incompatible|ej kompatibel|deprecated|removed|"
    r"discontinued|unavailable|saknar|fungerar inte|does not work|no longer)\b"
)
_POSITIVE_SIGNAL_RE = re.compile(
    r"(?i)\b(?:operational|normal drift|inga störningar|no outage|no known issues|"
    r"resolved|åtgärdad|supported|compatible|kompatibel|included|ingår|fungerar|"
    r"works out of the box|officially supported)\b"
)
_UNIT_VALUE_RE = re.compile(
    r"(?i)\b(\d+(?:[.,]\d+)?)\s*(dagar|days|dag|day|veckor|weeks|månader|months|år|years|%|kr|sek|nok|dkk)\b"
)


def _domain_matches(domain: str, candidates: frozenset[str] | tuple[str, ...]) -> bool:
    if not domain:
        return False
    for cand in candidates:
        c = cand.strip().lower().lstrip(".")
        if c and (domain == c or domain.endswith(f".{c}")):
            return True
    return False


def _extract_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""


def canonicalize_url(url: str) -> str:
    """Normalize scheme/host, strip tracking params and fragment, and normalize trailing slash."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""

    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not scheme or not host:
        return ""

    try:
        port = parsed.port
    except ValueError:
        return ""

    host_part = f"[{host}]" if ":" in host else host
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        netloc = host_part
    else:
        netloc = f"{host_part}:{port}"

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    filtered = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _STRIP_QUERY_PARAMS
    ]
    filtered.sort(key=lambda kv: (kv[0].lower(), kv[0], kv[1]))
    clean_query = urlencode(filtered, doseq=True)
    return urlunsplit((scheme, netloc, path, clean_query, ""))


def classify_source_type(url: str, domain: str, profile: SearchProfile) -> SourceType:
    """Classify a search hit into a structured ``SourceType``."""
    dom = (domain or "").strip().lower().lstrip(".") or _extract_host(url)
    try:
        path = (urlsplit(url).path or "/").lower()
    except ValueError:
        path = "/"

    if (
        _domain_matches(dom, _GOV_DOMAINS)
        or dom.endswith((".gov", ".europa.eu", ".kommune.no"))
        or ".gov." in dom
    ):
        return SourceType.REGULATORY_GOV

    if _domain_matches(dom, _CARRIER_DOMAINS):
        return SourceType.CARRIER_STATUS

    if (
        _domain_matches(dom, _FORUM_DOMAINS)
        or dom.startswith(("forum.", "forums.", "community.", "discourse."))
        or any(tok in path for tok in ("/forum/", "/forums/", "/community/", "/threads/", "/discussions/"))
        or (dom == "github.com" and ("/issues" in path or "/discussions" in path))
        or (dom == "wordpress.org" and "/support/topic/" in path)
    ):
        return SourceType.FORUM_COMMUNITY

    if _domain_matches(dom, _UX_DOMAINS):
        return SourceType.UX_RESEARCH

    if (
        _domain_matches(dom, _OFFICIAL_DOCS_DOMAINS)
        or dom.startswith(("docs.", "developer.", "developers."))
        or path.startswith(("/docs/", "/documentation/", "/reference/", "/manual/", "/api/"))
    ):
        return SourceType.OFFICIAL_DOCS

    if (
        _domain_matches(dom, _TRADE_PRESS_DOMAINS)
        or (dom == "shopify.com" and "/blog" in path)
        or (dom.startswith(("news.", "press.")) and any(tok in path for tok in ("/news/", "/article/", "/press/")))
    ):
        return SourceType.TRADE_PRESS

    if (
        _domain_matches(dom, _STORE_DOMAINS)
        or any(
            tok in path
            for tok in (
                "/product/",
                "/produkt/",
                "/produkter/",
                "/shop/",
                "/butik/",
                "/checkout",
                "/kassa",
                "/varukorg",
                "/thank-you",
                "/tack",
                "/takkeside",
            )
        )
        or (
            profile.id == "competitor_market"
            and dom.endswith((".se", ".no", ".dk", ".com", ".nu", ".fi"))
            and not _domain_matches(dom, profile.authority_domains)
        )
    ):
        return SourceType.COMPETITOR_STORE

    if _domain_matches(dom, profile.authority_domains):
        if profile.id == "design_inspiration":
            return SourceType.UX_RESEARCH
        return SourceType.OFFICIAL_DOCS

    return SourceType.UNKNOWN


def classify_claim_kind(source: EvaluatedSource, profile: SearchProfile) -> ClaimKind:
    """Tag whether a source backs factual claims, best-practice heuristics, inspiration, or opinion."""
    if profile.corroboration_mode == "inspiration_and_heuristics":
        if source.source_type in {SourceType.UX_RESEARCH, SourceType.OFFICIAL_DOCS}:
            return ClaimKind.BEST_PRACTICE
        if source.source_type == SourceType.FORUM_COMMUNITY:
            return ClaimKind.OPINION
        return ClaimKind.INSPIRATION

    if source.source_type in {
        SourceType.REGULATORY_GOV,
        SourceType.CARRIER_STATUS,
        SourceType.OFFICIAL_DOCS,
    }:
        return ClaimKind.FACT
    if source.source_type in {SourceType.UX_RESEARCH, SourceType.TRADE_PRESS}:
        return ClaimKind.BEST_PRACTICE
    if source.source_type == SourceType.COMPETITOR_STORE:
        if profile.id == "competitor_market":
            return ClaimKind.FACT
        return ClaimKind.INSPIRATION
    if source.source_type == SourceType.FORUM_COMMUNITY:
        return ClaimKind.OPINION
    return ClaimKind.BEST_PRACTICE if source.trust_tier != "low" else ClaimKind.OPINION


def _hit_attr(hit: SearchHit | dict[str, Any], name: str, default: Any = None) -> Any:
    if isinstance(hit, dict):
        return hit.get(name, default)
    return getattr(hit, name, default)


def _tokenize(text: str) -> set[str]:
    tokens = set()
    for tok in re.findall(r"[0-9a-zA-ZåäöøæÅÄÖØÆ]{2,}", text.lower()):
        if tok not in _STOPWORDS:
            tokens.add(tok)
    return tokens


def _score_relevance(query: str, title: str, snippet: str, extracted_text: str, provider_score: Any) -> float:
    q_tokens = _tokenize(query)
    doc_text = f"{title} {snippet} {extracted_text[:1500]}"
    d_tokens = _tokenize(doc_text)
    if q_tokens:
        exact_hits = sum(1 for qt in q_tokens if qt in d_tokens)
        partial_hits = sum(
            0.5 for qt in q_tokens if qt not in d_tokens and len(qt) >= 5 and any(dt.startswith(qt[:5]) for dt in d_tokens)
        )
        lexical = min(1.0, (exact_hits + partial_hits) / len(q_tokens))
    else:
        lexical = 0.5

    prov_norm = 0.0
    try:
        ps = float(provider_score or 0.0)
        if 0.0 < ps <= 1.0:
            prov_norm = ps
        elif ps > 1.0:
            prov_norm = min(1.0, ps / 10.0)
    except (TypeError, ValueError):
        prov_norm = 0.0

    combined = (0.75 * lexical + 0.25 * prov_norm) if prov_norm > 0 else lexical
    return round(max(0.0, min(1.0, combined)), 4)


def _score_authority(domain: str, source_type: SourceType, profile: SearchProfile) -> tuple[float, str]:
    is_profile_auth = _domain_matches(domain, profile.authority_domains)
    if any(hint in domain for hint in _LOW_QUALITY_DOMAIN_HINTS):
        return 0.2, "low"

    if source_type in {SourceType.REGULATORY_GOV, SourceType.CARRIER_STATUS}:
        base = 0.95
        tier = "high"
    elif source_type in {SourceType.OFFICIAL_DOCS, SourceType.UX_RESEARCH}:
        base = 0.88 if is_profile_auth else 0.82
        tier = "high"
    elif source_type == SourceType.TRADE_PRESS:
        base = 0.72
        tier = "standard"
    elif source_type == SourceType.COMPETITOR_STORE:
        base = 0.68 if profile.id in {"competitor_market", "design_inspiration"} else 0.55
        tier = "standard"
    elif source_type == SourceType.FORUM_COMMUNITY:
        base = 0.35
        tier = "low"
    else:
        base = 0.5
        tier = "standard"

    if is_profile_auth:
        base = max(base, 0.88)
        tier = "high"
    return round(max(0.0, min(1.0, base)), 4), tier


def _parse_published_at(raw: str | None) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text = f"{text}T00:00:00+00:00"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _score_freshness(published_at: str | None, profile: SearchProfile, now: datetime) -> float:
    dt = _parse_published_at(published_at)
    if dt is None:
        if profile.id == "support_fact_check":
            return 0.25
        if profile.id == "technical_docs":
            return 0.45
        return 0.55
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    horizon = max(1.0, float(profile.freshness_days))
    score = math.exp(-0.693147 * (age_days / horizon))
    return round(max(0.05, min(1.0, score)), 4)


def _score_signal_density(title: str, snippet: str, extracted_text: str) -> float:
    combined = " ".join(part for part in (title, snippet, extracted_text) if part).strip()
    if not combined:
        return 0.0
    lower = combined.lower()
    if any(hint in lower for hint in _BOILERPLATE_HINTS) and len(combined) < 300:
        return 0.15

    char_len = len(snippet) + len(extracted_text)
    if char_len < 30:
        length_score = 0.2
    elif char_len < 120:
        length_score = 0.45
    elif char_len < 400:
        length_score = 0.72
    else:
        length_score = 0.9

    info_bonus = 0.1 if re.search(r"\b\d+(?:\.\d+)?\b", combined) else 0.0
    return round(max(0.0, min(1.0, length_score + info_bonus)), 4)


def _weighted_score(
    *,
    relevance: float,
    authority: float,
    freshness: float,
    signal_density: float,
    weights: dict[str, float],
) -> float:
    w_rel = max(0.0, float(weights.get("relevance", 0.35)))
    w_auth = max(0.0, float(weights.get("authority", 0.35)))
    w_fresh = max(0.0, float(weights.get("freshness", 0.15)))
    w_sig = max(0.0, float(weights.get("signal_density", 0.15)))
    total_w = w_rel + w_auth + w_fresh + w_sig
    if total_w <= 0:
        total_w = 1.0
        w_rel, w_auth, w_fresh, w_sig = 0.35, 0.35, 0.15, 0.15
    raw = (
        w_rel * relevance
        + w_auth * authority
        + w_fresh * freshness
        + w_sig * signal_density
    ) / total_w
    return round(max(0.0, min(1.0, raw)), 4)


def _detect_conflicts(sources: list[EvaluatedSource], query: str) -> list[ConflictNote]:
    del query
    conflicts: list[ConflictNote] = []
    seen_keys: set[tuple[str, tuple[str, ...]]] = set()

    # 1. Cross-source polarity / status contradiction
    pos_sources: list[EvaluatedSource] = []
    neg_sources: list[EvaluatedSource] = []
    for src in sources:
        if src.score < 0.3:
            continue
        blob = f"{src.title} {src.snippet} {src.extracted_text[:1200]}"
        has_neg = bool(_NEGATIVE_SIGNAL_RE.search(blob))
        has_pos = bool(_POSITIVE_SIGNAL_RE.search(blob))
        if has_neg and not has_pos:
            neg_sources.append(src)
        elif has_pos and not has_neg:
            pos_sources.append(src)

    if pos_sources and neg_sources:
        for p_src in pos_sources:
            for n_src in neg_sources:
                if p_src.domain == n_src.domain:
                    continue
                pair = tuple(sorted((p_src.source_id, n_src.source_id)))
                key = ("status_polarity", pair)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                severity = "major" if (p_src.trust_tier == "high" or n_src.trust_tier == "high") else "minor"
                conflicts.append(
                    ConflictNote(
                        topic="status_polarity",
                        summary=(
                            f"{p_src.source_id} ({p_src.domain}) and {n_src.source_id} ({n_src.domain}) "
                            "report conflicting operational/compatibility status."
                        ),
                        source_ids=list(pair),
                        severity=severity,
                    )
                )
                break
            if conflicts:
                break

    # 2. Numeric-with-unit divergence on overlapping content
    source_units: list[tuple[EvaluatedSource, dict[str, set[str]], set[str]]] = []
    for src in sources:
        if src.score < 0.35:
            continue
        blob = f"{src.title} {src.snippet}"
        unit_map: dict[str, set[str]] = {}
        for num, unit in _UNIT_VALUE_RE.findall(blob):
            norm_u = unit.lower()
            unit_map.setdefault(norm_u, set()).add(num.replace(",", "."))
        if unit_map:
            source_units.append((src, unit_map, _tokenize(blob)))

    for idx, (s1, u1, t1) in enumerate(source_units):
        for s2, u2, t2 in source_units[idx + 1 :]:
            if s1.domain == s2.domain:
                continue
            shared_units = set(u1) & set(u2)
            for unit in sorted(shared_units):
                if u1[unit] != u2[unit] and len(t1 & t2) >= 2:
                    pair = tuple(sorted((s1.source_id, s2.source_id)))
                    key = (f"numeric_{unit}", pair)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    vals1 = "/".join(sorted(u1[unit]))
                    vals2 = "/".join(sorted(u2[unit]))
                    conflicts.append(
                        ConflictNote(
                            topic=f"numeric_{unit}",
                            summary=(
                                f"{s1.source_id} ({s1.domain}: {vals1} {unit}) diverges from "
                                f"{s2.source_id} ({s2.domain}: {vals2} {unit})."
                            ),
                            source_ids=list(pair),
                            severity="minor",
                        )
                    )
                    if len(conflicts) >= 4:
                        return conflicts
    return conflicts


def _determine_evidence_status(
    sources: list[EvaluatedSource],
    conflicts: list[ConflictNote],
    profile: SearchProfile,
    provenance: str,
) -> EvidenceStatus:
    if not sources:
        return EvidenceStatus.INSUFFICIENT

    if provenance in {"mock", "mock_fixture"}:
        return EvidenceStatus.MOCK

    if all(s.score < 0.2 for s in sources):
        return EvidenceStatus.INSUFFICIENT

    if any(c.severity == "major" for c in conflicts) or (
        conflicts and sum(1 for s in sources if s.score >= 0.45) >= 2
    ):
        return EvidenceStatus.CONFLICTING

    claim_kinds = {classify_claim_kind(s, profile) for s in sources}
    if profile.corroboration_mode == "inspiration_and_heuristics" or (
        claim_kinds <= {ClaimKind.INSPIRATION, ClaimKind.BEST_PRACTICE, ClaimKind.OPINION}
        and not any(
            s.source_type in {SourceType.REGULATORY_GOV, SourceType.CARRIER_STATUS, SourceType.OFFICIAL_DOCS}
            for s in sources
        )
    ):
        return EvidenceStatus.INSPIRATION_ONLY

    credible_domains = {
        s.domain for s in sources if s.domain and s.score >= 0.4 and s.trust_tier != "low"
    }
    authoritative_primary = [
        s
        for s in sources
        if (
            s.source_type in {SourceType.REGULATORY_GOV, SourceType.CARRIER_STATUS, SourceType.OFFICIAL_DOCS}
            or _domain_matches(s.domain, profile.authority_domains)
        )
        and s.score >= 0.5
    ]

    mode = profile.corroboration_mode
    if mode in {"strict_factual", "authoritative_primary"}:
        if authoritative_primary or len(credible_domains) >= 2:
            return EvidenceStatus.GROUNDED
        return EvidenceStatus.PARTIAL

    if len(credible_domains) >= 2 or (authoritative_primary and sources[0].score >= 0.65):
        return EvidenceStatus.GROUNDED
    return EvidenceStatus.PARTIAL


def _hit_providers(hit: SearchHit | dict[str, Any]) -> list[str]:
    seen: list[str] = []
    raw_prov = str(_hit_attr(hit, "provider", "") or "")
    for part in raw_prov.split("+"):
        p = part.strip().lower()
        if p and p not in seen:
            seen.append(p)
    corr = _hit_attr(hit, "corroborated_by", None) or ()
    if isinstance(corr, (list, tuple, set)):
        for item in corr:
            p = str(item or "").strip().lower()
            if p and p not in seen:
                seen.append(p)
    return seen


def evaluate_search_hits(
    hits: Sequence[SearchHit | dict[str, Any]],
    *,
    query: str,
    profile: SearchProfile,
    provenance: str = "live",
) -> tuple[list[EvaluatedSource], EvidenceStatus, list[ConflictNote]]:
    """Sanitize, deduplicate, score (with RRF corroboration), and evaluate search hits."""
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    deduped: dict[str, dict[str, Any]] = {}
    provider_ranks: dict[str, int] = {}

    for hit in hits:
        raw_url = str(_hit_attr(hit, "url", "") or "").strip()
        canon = canonicalize_url(raw_url)
        if not canon:
            continue
        domain = _extract_host(canon)
        title = sanitize_untrusted_text(str(_hit_attr(hit, "title", "") or ""), max_chars=300)
        snippet = sanitize_untrusted_text(str(_hit_attr(hit, "snippet", "") or ""), max_chars=2000)

        raw_content = _hit_attr(hit, "content", None)
        raw_excerpts = _hit_attr(hit, "raw_excerpts", ()) or ()
        if raw_content:
            extracted_raw = str(raw_content)
        elif raw_excerpts:
            extracted_raw = "\n".join(str(x) for x in raw_excerpts if x)
        else:
            extracted_raw = ""
        extracted_text = sanitize_untrusted_text(extracted_raw, max_chars=8000)
        published_at = _hit_attr(hit, "published_at", None)
        if published_at is not None:
            published_at = sanitize_untrusted_text(str(published_at), max_chars=64) or None
        hit_provs = _hit_providers(hit)
        provider_score = _hit_attr(hit, "score", 0.0)
        try:
            pre_rrf = float(_hit_attr(hit, "rrf_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            pre_rrf = 0.0

        rrf_contrib = 0.0
        for p in hit_provs or [""]:
            rank = provider_ranks.get(p, 0) + 1
            provider_ranks[p] = rank
            rrf_contrib += 1.0 / (60.0 + rank)
        effective_rrf = max(pre_rrf, rrf_contrib)

        existing = deduped.get(canon)
        if existing is not None:
            if len(snippet) > len(existing["snippet"]):
                existing["snippet"] = snippet
            if len(extracted_text) > len(existing["extracted_text"]):
                existing["extracted_text"] = extracted_text
            if not existing["title"] and title:
                existing["title"] = title
            if not existing["published_at"] and published_at:
                existing["published_at"] = published_at
            new_prov_added = False
            for p in hit_provs:
                if p not in existing["providers"]:
                    existing["providers"].append(p)
                    new_prov_added = True
            if new_prov_added:
                existing["rrf_sum"] = max(existing["rrf_sum"] + rrf_contrib, pre_rrf)
            elif pre_rrf > existing["rrf_sum"]:
                existing["rrf_sum"] = pre_rrf
            try:
                if float(provider_score or 0.0) > float(existing["provider_score"] or 0.0):
                    existing["provider_score"] = provider_score
            except (TypeError, ValueError):
                pass
            continue

        deduped[canon] = {
            "url": raw_url,
            "canonical_url": canon,
            "domain": domain,
            "title": title,
            "snippet": snippet,
            "extracted_text": extracted_text,
            "published_at": published_at,
            "providers": list(hit_provs),
            "rrf_sum": effective_rrf,
            "provider_score": provider_score,
        }

    evaluated: list[EvaluatedSource] = []
    for item in deduped.values():
        source_type = classify_source_type(item["canonical_url"], item["domain"], profile)
        rel = _score_relevance(
            query,
            item["title"],
            item["snippet"],
            item["extracted_text"],
            item["provider_score"],
        )
        auth, trust_tier = _score_authority(item["domain"], source_type, profile)
        fresh = _score_freshness(item["published_at"], profile, now)
        sig = _score_signal_density(item["title"], item["snippet"], item["extracted_text"])
        base_overall = _weighted_score(
            relevance=rel,
            authority=auth,
            freshness=fresh,
            signal_density=sig,
            weights=profile.weights,
        )
        prov_count = len(item["providers"])
        corr_bonus = (
            min(0.12, 0.04 * (prov_count - 1) + min(0.04, float(item["rrf_sum"]) * 1.2))
            if prov_count >= 2
            else 0.0
        )
        overall = round(max(0.0, min(1.0, base_overall + corr_bonus)), 4)
        evaluated.append(
            EvaluatedSource(
                source_id="",
                url=item["url"],
                canonical_url=item["canonical_url"],
                domain=item["domain"],
                title=item["title"],
                snippet=item["snippet"],
                extracted_text=item["extracted_text"],
                source_type=source_type,
                trust_tier=trust_tier,
                published_at=item["published_at"],
                accessed_at=now_iso,
                provider="+".join(item["providers"]),
                provenance=provenance,
                relevance_score=rel,
                authority_score=auth,
                freshness_score=fresh,
                signal_density=sig,
                score=overall,
                corroborated_by=tuple(item["providers"]),
                provider_count=max(1, prov_count),
                rrf_score=round(float(item["rrf_sum"]), 6),
            )
        )

    evaluated.sort(key=lambda s: (-s.score, -s.authority_score, s.canonical_url))
    for idx, src in enumerate(evaluated, start=1):
        src.source_id = f"S{idx}"

    conflicts = _detect_conflicts(evaluated, query)
    status = _determine_evidence_status(evaluated, conflicts, profile, provenance)
    return evaluated, status, conflicts


def build_citations(
    sources: Sequence[EvaluatedSource],
    profile: SearchProfile | None = None,
) -> list[Citation]:
    """Convert evaluated sources into structured ``Citation`` objects."""
    citations: list[Citation] = []
    for src in sources:
        claim_kind = classify_claim_kind(src, profile) if profile is not None else ClaimKind.FACT
        citations.append(
            Citation(
                id=src.source_id,
                title=src.title,
                url=src.canonical_url or src.url,
                domain=src.domain,
                published_at=src.published_at,
                accessed_at=src.accessed_at,
                source_type=src.source_type,
                trust_tier=src.trust_tier,
                score=src.score,
                claim_kind=claim_kind,
            )
        )
    return citations


def _scrub_citations_in_text(
    text: str,
    valid_ids: set[str],
    removed_ids: list[str],
    kept_ids: set[str],
) -> str:
    if not text:
        return ""

    def _replace(match: re.Match[str]) -> str:
        raw_group = match.group(1)
        parts = [p.strip().upper() for p in re.split(r"[,;]", raw_group) if p.strip()]
        kept_group: list[str] = []
        for cid in parts:
            if cid in valid_ids:
                kept_ids.add(cid)
                if cid not in kept_group:
                    kept_group.append(cid)
            elif cid not in removed_ids:
                removed_ids.append(cid)
        if not kept_group:
            return ""
        return "[" + ", ".join(kept_group) + "]"

    cleaned = _CITATION_RE.sub(_replace, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return cleaned.strip()


def _sync_verified_markdown(
    markdown: str,
    new_citations: list[Citation],
    new_gaps: list[str],
    added_gap_notes: list[str],
) -> str:
    if not markdown:
        return ""
    lines = markdown.splitlines()

    def _find_section(heading_prefixes: tuple[str, ...]) -> tuple[int, int] | None:
        start_idx: int | None = None
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if start_idx is None:
                if any(stripped.startswith(p) for p in heading_prefixes):
                    start_idx = idx
            elif stripped.startswith("## "):
                return start_idx, idx
        if start_idx is not None:
            return start_idx, len(lines)
        return None

    # Update ## Kunskapsluckor & begränsningar if structured brief and new gaps were added
    if added_gap_notes:
        gap_span = _find_section(("## Kunskapsluckor", "## Gaps"))
        if gap_span is not None:
            _g_start, g_end = gap_span
            existing_block = set(lines[_g_start + 1 : g_end])
            inserts = [f"- {g}" for g in added_gap_notes if f"- {g}" not in existing_block]
            if inserts:
                lines = lines[:g_end] + inserts + lines[g_end:]
        elif _find_section(("## Sammanfattning", "## Summary")) is not None:
            src_span = _find_section(("## Källor", "## Sources"))
            block = ["", "## Kunskapsluckor & begränsningar", *(f"- {g}" for g in new_gaps if g)]
            if src_span is not None:
                lines = lines[: src_span[0]] + block + [""] + lines[src_span[0] :]
            else:
                lines.extend(block)

    # Re-sync ## Källor / ## Sources against verified new_citations
    src_span = _find_section(("## Källor", "## Sources"))
    if src_span is not None:
        s_start, s_end = src_span
        if new_citations:
            heading = lines[s_start]
            cit_lines = [heading]
            for cit in new_citations:
                pub = f" · {cit.published_at}" if cit.published_at else ""
                cit_lines.append(f"- [{cit.id}] {cit.title} ({cit.domain}{pub}) — {cit.url}")
            lines = lines[:s_start] + cit_lines + lines[s_end:]
        else:
            # Drop trailing blank line before removed section if any
            trim_start = s_start - 1 if s_start > 0 and not lines[s_start - 1].strip() else s_start
            lines = lines[:trim_start] + lines[s_end:]

    return "\n".join(lines).strip() + ("\n" if markdown.endswith("\n") else "")


def verify_citations(brief: ResearchBrief, sources: list[EvaluatedSource]) -> ResearchBrief:
    """Verify every ``[S#]`` citation against ``sources`` and remove unbacked citation IDs."""
    valid_map: dict[str, EvaluatedSource] = {
        src.source_id.strip().upper(): src
        for src in sources
        if src.source_id and src.source_id.strip()
    }
    valid_ids = set(valid_map)
    removed_ids: list[str] = []
    kept_ids: set[str] = set()

    new_summary = _scrub_citations_in_text(brief.summary, valid_ids, removed_ids, kept_ids)
    new_markdown = _scrub_citations_in_text(brief.markdown, valid_ids, removed_ids, kept_ids)
    new_recommendations = [
        _scrub_citations_in_text(rec, valid_ids, removed_ids, kept_ids)
        for rec in brief.recommendations
    ]

    new_findings: list[Any] = []
    for item in brief.findings:
        if isinstance(item, dict):
            copied = dict(item)
            for key in ("claim", "text", "summary", "detail", "title", "description"):
                if isinstance(copied.get(key), str):
                    copied[key] = _scrub_citations_in_text(
                        copied[key], valid_ids, removed_ids, kept_ids
                    )
            for list_key in ("citations", "source_ids"):
                raw_list = copied.get(list_key)
                if isinstance(raw_list, list):
                    valid_list: list[str] = []
                    for entry in raw_list:
                        norm = str(entry or "").strip().strip("[]").upper()
                        if norm in valid_ids:
                            kept_ids.add(norm)
                            if norm not in valid_list:
                                valid_list.append(norm)
                        elif norm and norm not in removed_ids:
                            removed_ids.append(norm)
                    copied[list_key] = valid_list
            new_findings.append(copied)
        elif isinstance(item, str):
            new_findings.append(_scrub_citations_in_text(item, valid_ids, removed_ids, kept_ids))
        else:
            new_findings.append(item)

    new_citations: list[Citation] = []
    seen_cids: set[str] = set()
    for cit in brief.citations:
        cid = (cit.id or "").strip().upper()
        if cid in valid_ids:
            if cid not in seen_cids:
                seen_cids.add(cid)
                new_citations.append(replace(cit, id=cid))
        elif cid and cid not in removed_ids:
            removed_ids.append(cid)

    new_gaps = list(brief.gaps)
    added_gap_notes: list[str] = []
    new_status = brief.evidence_status
    if removed_ids:
        note = "Removed unverified citation(s): " + ", ".join(f"[{rid}]" for rid in removed_ids)
        if note not in new_gaps:
            new_gaps.append(note)
            added_gap_notes.append(note)
        if (
            new_status == EvidenceStatus.GROUNDED
            and not kept_ids
            and not new_citations
        ):
            new_status = EvidenceStatus.PARTIAL

    synced_markdown = _sync_verified_markdown(
        new_markdown,
        new_citations=new_citations,
        new_gaps=new_gaps,
        added_gap_notes=added_gap_notes,
    )

    return replace(
        brief,
        evidence_status=new_status,
        summary=new_summary,
        findings=new_findings,
        recommendations=new_recommendations,
        gaps=new_gaps,
        citations=new_citations,
        markdown=synced_markdown,
    )


__all__ = [
    "build_citations",
    "canonicalize_url",
    "classify_claim_kind",
    "classify_source_type",
    "evaluate_search_hits",
    "verify_citations",
]
