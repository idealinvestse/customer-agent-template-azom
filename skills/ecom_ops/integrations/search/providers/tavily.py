"""Tavily Search REST adapter (``route_id="tavily"``)."""

from __future__ import annotations

import time
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
from ecom_ops.research.config import load_research_config

_API_ORIGIN = "https://api.tavily.com"
_SEARCH_URL = f"{_API_ORIGIN}/search"
_ESTIMATED_COST_QUICK_USD = 0.008
_ESTIMATED_COST_DEEP_USD = 0.016


class TavilySearchRoute:
    """Tavily Search API adapter (header-only ``Authorization: Bearer`` auth)."""

    route_id = "tavily"
    capabilities = SearchCapabilities(
        supports_country=False,
        supports_language=False,
        supports_freshness=True,
        supports_domain_filter=True,
        supports_content=True,
        supports_extract=False,
        supports_deep_mode=True,
    )

    def is_configured(self) -> bool:
        return bool(env_secret("TAVILY_API_KEY"))

    def status(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "endpoint": _SEARCH_URL,
        }

    def search(self, request: SearchRequest) -> SearchResult:
        api_key = require_api_key(self.route_id, "TAVILY_API_KEY")
        clean_query = scrub_query_for_provider(request.query, taint=request.taint)
        cfg = load_research_config()
        t_cfg = cfg.providers.get("tavily") or {}
        search_depth = (
            str(t_cfg.get("search_depth_deep") or "advanced")
            if request.mode == "deep"
            else str(t_cfg.get("search_depth_quick") or "basic")
        )
        if search_depth not in {"basic", "advanced"}:
            search_depth = "basic"

        body: dict[str, Any] = {
            "query": clean_query,
            "search_depth": search_depth,
            "max_results": int(request.max_results),
            "include_raw_content": bool(request.include_content),
        }
        if request.include_domains:
            body["include_domains"] = list(request.include_domains)
        if request.exclude_domains:
            body["exclude_domains"] = list(request.exclude_domains)
        if request.freshness_days is not None:
            if request.freshness_days <= 1:
                body["time_range"] = "day"
            elif request.freshness_days <= 7:
                body["time_range"] = "week"
            elif request.freshness_days <= 31:
                body["time_range"] = "month"
            else:
                body["time_range"] = "year"

        started = time.monotonic()
        data = pinned_json_request(
            self.route_id,
            _SEARCH_URL,
            method="POST",
            headers={"Authorization": f"Bearer {api_key}"},
            json_body=body,
            allowed_auth_origins=(_API_ORIGIN,),
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        raw_results = data.get("results")
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise SearchUnavailableError("tavily: invalid results field", code="invalid_json")

        hits: list[SearchHit] = []
        for idx, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            snippet_text = str(item.get("content") or "").strip()
            raw_content = str(item.get("raw_content") or "").strip()
            content = (raw_content or snippet_text) if request.include_content else None
            score_val = item.get("score")
            score = float(score_val) if isinstance(score_val, (int, float)) else max(0.1, 1.0 - idx * 0.05)
            hits.append(
                SearchHit(
                    url=url,
                    title=str(item.get("title") or "").strip(),
                    snippet=snippet_text[:1200],
                    published_at=str(item.get("published_date")).strip() if item.get("published_date") else None,
                    provider="tavily",
                    score=score,
                    raw_excerpts=(snippet_text,) if snippet_text else (),
                    content=content,
                )
            )
            if len(hits) >= request.max_results:
                break

        est_cost = _ESTIMATED_COST_DEEP_USD if search_depth == "advanced" else _ESTIMATED_COST_QUICK_USD
        return SearchResult(
            ok=True,
            route_id=self.route_id,
            provider="tavily",
            hits=tuple(hits),
            provenance="live_api",
            latency_ms=latency_ms,
            cached=False,
            estimated_cost_usd=est_cost,
        )


__all__ = ["TavilySearchRoute"]
