"""Exa AI semantic search adapter (``route_id="exa"``)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
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

_API_ORIGIN = "https://api.exa.ai"
_SEARCH_URL = f"{_API_ORIGIN}/search"
_ESTIMATED_COST_USD = 0.005


class ExaSearchRoute:
    """Exa Search API adapter (header-only ``x-api-key`` auth)."""

    route_id = "exa"
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
        return bool(env_secret("EXA_API_KEY"))

    def status(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "endpoint": _SEARCH_URL,
        }

    def search(self, request: SearchRequest) -> SearchResult:
        api_key = require_api_key(self.route_id, "EXA_API_KEY")
        clean_query = scrub_query_for_provider(request.query, taint=request.taint)
        cfg = load_research_config()
        e_cfg = cfg.providers.get("exa") or {}
        search_type = (
            str(e_cfg.get("search_type_deep") or "deep")
            if request.mode == "deep"
            else str(e_cfg.get("search_type_quick") or "auto")
        )
        if search_type not in {"auto", "deep", "neural", "keyword"}:
            search_type = "auto"

        contents: dict[str, Any] = {"highlights": True}
        if request.include_content:
            contents["text"] = {"maxCharacters": 4000}

        body: dict[str, Any] = {
            "query": clean_query,
            "type": search_type,
            "numResults": int(request.max_results),
            "contents": contents,
        }
        if request.include_domains:
            body["includeDomains"] = list(request.include_domains)
        if request.exclude_domains:
            body["excludeDomains"] = list(request.exclude_domains)
        if request.freshness_days is not None:
            start_dt = datetime.now(UTC) - timedelta(days=int(request.freshness_days))
            body["startPublishedDate"] = start_dt.strftime("%Y-%m-%d")

        started = time.monotonic()
        data = pinned_json_request(
            self.route_id,
            _SEARCH_URL,
            method="POST",
            headers={"x-api-key": api_key},
            json_body=body,
            allowed_auth_origins=(_API_ORIGIN,),
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        raw_results = data.get("results")
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            raise SearchUnavailableError("exa: invalid results field", code="invalid_json")

        hits: list[SearchHit] = []
        for idx, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            highlights_raw = item.get("highlights") or []
            highlights = (
                tuple(str(h).strip() for h in highlights_raw if str(h or "").strip())
                if isinstance(highlights_raw, list)
                else ()
            )
            text_val = str(item.get("text") or "").strip()
            snippet = " ".join(highlights)[:1200] if highlights else text_val[:600]
            content = text_val if (request.include_content and text_val) else None
            score_val = item.get("score")
            score = float(score_val) if isinstance(score_val, (int, float)) else max(0.1, 1.0 - idx * 0.05)
            hits.append(
                SearchHit(
                    url=url,
                    title=str(item.get("title") or "").strip(),
                    snippet=snippet,
                    published_at=str(item.get("publishedDate")).strip() if item.get("publishedDate") else None,
                    provider="exa",
                    score=score,
                    author=str(item.get("author")).strip() if item.get("author") else None,
                    raw_excerpts=highlights,
                    content=content,
                )
            )
            if len(hits) >= request.max_results:
                break

        return SearchResult(
            ok=True,
            route_id=self.route_id,
            provider="exa",
            hits=tuple(hits),
            provenance="live_api",
            latency_ms=latency_ms,
            cached=False,
            estimated_cost_usd=_ESTIMATED_COST_USD,
        )


__all__ = ["ExaSearchRoute"]
