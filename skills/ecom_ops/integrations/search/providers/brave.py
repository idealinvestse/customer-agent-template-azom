"""Brave Search REST adapter (``route_id="brave"``)."""

from __future__ import annotations

import time
import urllib.parse
from typing import Any

from ecom_ops.integrations.search.base import (
    SearchCapabilities,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from ecom_ops.integrations.search.errors import SearchUnavailableError
from ecom_ops.integrations.search.providers import (
    env_secret,
    pinned_json_request,
    require_api_key,
    scrub_query_for_provider,
)

_API_ORIGIN = "https://api.search.brave.com"
_SEARCH_PATH = "/res/v1/web/search"
_ESTIMATED_COST_USD = 0.005

_BRAVE_SEARCH_LANGS: frozenset[str] = frozenset(
    {
        "ar",
        "eu",
        "bn",
        "bg",
        "ca",
        "zh-hans",
        "zh-hant",
        "hr",
        "cs",
        "da",
        "nl",
        "en",
        "en-gb",
        "et",
        "fi",
        "fr",
        "gl",
        "de",
        "el",
        "gu",
        "he",
        "hi",
        "hu",
        "is",
        "it",
        "jp",
        "kn",
        "ko",
        "lv",
        "lt",
        "ms",
        "ml",
        "mr",
        "nb",
        "pl",
        "pt-br",
        "pt-pt",
        "pa",
        "ro",
        "ru",
        "sr",
        "sk",
        "sl",
        "es",
        "sv",
        "ta",
        "te",
        "th",
        "tr",
        "uk",
        "vi",
    }
)
_BRAVE_LANG_ALIASES: dict[str, str] = {
    "no": "nb",
    "nb": "nb",
    "nn": "nb",
    "nob": "nb",
    "nno": "nb",
    "nor": "nb",
    "sv": "sv",
    "se": "sv",
    "swe": "sv",
    "da": "da",
    "dk": "da",
    "dan": "da",
    "en": "en",
    "eng": "en",
    "en-gb": "en-gb",
    "zh": "zh-hans",
    "zh-cn": "zh-hans",
    "zh-hans": "zh-hans",
    "zh-tw": "zh-hant",
    "zh-hant": "zh-hant",
    "pt": "pt-pt",
    "pt-pt": "pt-pt",
    "pt-br": "pt-br",
    "ja": "jp",
    "jp": "jp",
}
_BRAVE_COUNTRY_ALIASES: dict[str, str] = {
    "UK": "GB",
}
_NON_COUNTRY_LANGUAGE_TOKENS: frozenset[str] = frozenset({"EN", "SV", "NB", "NN", "DA"})


def _normalize_brave_country(raw_country: str | None) -> str | None:
    raw = str(raw_country or "").strip().upper()
    if not raw:
        return None
    if raw == "ALL":
        return "ALL"
    mapped = _BRAVE_COUNTRY_ALIASES.get(raw, raw)
    if mapped in _NON_COUNTRY_LANGUAGE_TOKENS:
        return None
    if len(mapped) == 2 and mapped.isalpha():
        return mapped
    return None


def _normalize_brave_search_lang(raw_language: str | None) -> str | None:
    raw = str(raw_language or "").strip().lower().replace("_", "-")
    if not raw:
        return None
    if raw in _BRAVE_LANG_ALIASES:
        cand = _BRAVE_LANG_ALIASES[raw]
        return cand if cand in _BRAVE_SEARCH_LANGS else None
    if raw in _BRAVE_SEARCH_LANGS:
        return raw
    base = raw.split("-", 1)[0]
    cand = _BRAVE_LANG_ALIASES.get(base, base)
    return cand if cand in _BRAVE_SEARCH_LANGS else None


def _brave_freshness(freshness_days: int | None) -> str | None:
    if freshness_days is None:
        return None
    if freshness_days <= 1:
        return "pd"
    if freshness_days <= 7:
        return "pw"
    if freshness_days <= 31:
        return "pm"
    return "py"


class BraveSearchRoute:
    """Brave Web Search API adapter (header-only ``X-Subscription-Token`` auth)."""

    route_id = "brave"
    capabilities = SearchCapabilities(
        supports_country=True,
        supports_language=True,
        supports_freshness=True,
        supports_domain_filter=False,
        supports_content=False,
        supports_extract=False,
        supports_deep_mode=False,
    )

    def is_configured(self) -> bool:
        return bool(env_secret("BRAVE_SEARCH_API_KEY"))

    def status(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "endpoint": f"{_API_ORIGIN}{_SEARCH_PATH}",
        }

    def search(self, request: SearchRequest) -> SearchResult:
        api_key = require_api_key(self.route_id, "BRAVE_SEARCH_API_KEY")
        clean_query = scrub_query_for_provider(request.query, taint=request.taint)

        params: dict[str, str] = {
            "q": clean_query,
            "count": str(min(20, int(request.max_results))),
            "extra_snippets": "true",
        }
        country = _normalize_brave_country(request.country)
        if country:
            params["country"] = country
        search_lang = _normalize_brave_search_lang(request.language)
        if search_lang:
            params["search_lang"] = search_lang
        freshness = _brave_freshness(request.freshness_days)
        if freshness:
            params["freshness"] = freshness

        url = f"{_API_ORIGIN}{_SEARCH_PATH}?{urllib.parse.urlencode(params)}"

        started = time.monotonic()
        data = pinned_json_request(
            self.route_id,
            url,
            method="GET",
            headers={
                "X-Subscription-Token": api_key,
                "Cache-Control": "no-cache",
            },
            allowed_auth_origins=(_API_ORIGIN,),
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        web_block = data.get("web")
        if web_block is None:
            web_block = {}
        if not isinstance(web_block, dict):
            raise SearchUnavailableError("brave: invalid web field", code="invalid_json")
        raw_results = web_block.get("results")
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise SearchUnavailableError("brave: invalid web.results field", code="invalid_json")

        hits: list[SearchHit] = []
        for idx, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue
            hit_url = str(item.get("url") or "").strip()
            if not hit_url:
                continue
            desc = str(item.get("description") or "").strip()
            extra_raw = item.get("extra_snippets") or []
            extras = (
                tuple(str(s).strip() for s in extra_raw if str(s or "").strip())
                if isinstance(extra_raw, list)
                else ()
            )
            excerpts = tuple(x for x in (desc, *extras) if x)
            snippet = " ".join(excerpts)[:1200] if excerpts else desc
            hits.append(
                SearchHit(
                    url=hit_url,
                    title=str(item.get("title") or "").strip(),
                    snippet=snippet,
                    published_at=str(item.get("page_age")).strip() if item.get("page_age") else None,
                    provider="brave",
                    score=max(0.1, 1.0 - idx * 0.05),
                    raw_excerpts=excerpts,
                    content=None,
                )
            )
            if len(hits) >= request.max_results:
                break

        return SearchResult(
            ok=True,
            route_id=self.route_id,
            provider="brave",
            hits=tuple(hits),
            provenance="live_api",
            latency_ms=latency_ms,
            cached=False,
            estimated_cost_usd=_ESTIMATED_COST_USD,
        )


__all__ = ["BraveSearchRoute"]
