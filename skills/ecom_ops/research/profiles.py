"""Situation-adaptive search profiles and YAML override merging.

Leaf module: no ``integrations.*``, ``urllib``, ``http.client``, ``requests``,
or ``subprocess`` imports.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ecom_ops.research.config import load_research_config
from ecom_ops.research.models import SearchProfile

_BUILTIN_PROFILES: dict[str, SearchProfile] = {
    "design_inspiration": SearchProfile(
        id="design_inspiration",
        description="UX/UI inspiration, conversion patterns, and layout benchmarks for WP pages.",
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
        description="WordPress, WooCommerce, PHP, CSS, and official developer documentation.",
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
        description="Nordic e-commerce and car-multimedia positioning, USP, and market research.",
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
        description="Carrier outages, Nordic consumer law, and OEM hardware/fitment verification.",
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
        description="Google Ads/GA4 updates, Consent Mode, seasonality, and keyword disambiguation.",
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
        description="Open-ended multi-round strategic and operational research.",
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
}


def _merge_profile(base: SearchProfile, override: dict[str, Any] | None) -> SearchProfile:
    if not override:
        return base
    kwargs: dict[str, Any] = {}
    if "description" in override and override["description"]:
        kwargs["description"] = str(override["description"])
    if isinstance(override.get("preferred_providers"), (list, tuple)):
        provs = tuple(str(p).strip().lower() for p in override["preferred_providers"] if str(p).strip())
        if provs:
            kwargs["preferred_providers"] = provs
    if isinstance(override.get("market_defaults"), (list, tuple)):
        mkts = tuple(str(m).strip() for m in override["market_defaults"] if str(m).strip())
        if mkts:
            kwargs["market_defaults"] = mkts
    if "freshness_days" in override and override["freshness_days"] is not None:
        kwargs["freshness_days"] = max(1, int(override["freshness_days"]))
    if override.get("corroboration_mode"):
        kwargs["corroboration_mode"] = str(override["corroboration_mode"]).strip()
    if isinstance(override.get("authority_domains"), (list, tuple)):
        doms = tuple(str(d).strip().lower().lstrip(".") for d in override["authority_domains"] if str(d).strip())
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


__all__ = [
    "get_profile",
    "list_profiles",
]
