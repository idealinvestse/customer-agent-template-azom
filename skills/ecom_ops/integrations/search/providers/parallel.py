"""Parallel AI Search & Extract adapter (``route_id="parallel"``)."""

from __future__ import annotations

import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from ecom_ops.integrations.search.base import (
    FetchResult,
    SearchCapabilities,
    SearchHit,
    SearchRequest,
    SearchResult,
)
from ecom_ops.integrations.search.errors import (
    SearchConfigError,
    SearchPolicyError,
    SearchUnavailableError,
)
from ecom_ops.integrations.search.providers import (
    env_secret,
    pinned_json_request,
    require_api_key,
    scrub_query_for_provider,
)
from ecom_ops.research.budget import can_run_fetch
from ecom_ops.research.config import (
    is_mock_mode,
    kill_switch_active,
    load_research_config,
    web_fetch_enabled,
)
from ecom_ops.research.firewall import OutboundQueryFirewall, QueryTaintContext
from ecom_ops.research.sanitizer import sanitize_untrusted_text
from ecom_ops.telemetry import Telemetry, default_telemetry

_API_ORIGIN = "https://api.parallel.ai"
_SEARCH_URL = f"{_API_ORIGIN}/v1/search"
_EXTRACT_URL = f"{_API_ORIGIN}/v1/extract"
_ESTIMATED_COST_USD = 0.005


def _split_concise_queries(clean_query: str, *, max_len: int = 200, max_count: int = 5) -> list[str]:
    """Split a scrubbed query into 1..5 concise queries each <= 200 chars."""
    if len(clean_query) <= max_len:
        return [clean_query]
    words = clean_query.split()
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for word in words:
        piece = word[:max_len]
        add_len = len(piece) + (1 if cur else 0)
        if cur and cur_len + add_len > max_len:
            chunks.append(" ".join(cur))
            if len(chunks) >= max_count:
                break
            cur = [piece]
            cur_len = len(piece)
        else:
            cur.append(piece)
            cur_len += add_len
    if cur and len(chunks) < max_count:
        chunks.append(" ".join(cur))
    return chunks or [clean_query[:max_len]]


def _build_advanced_settings(request: SearchRequest) -> dict[str, Any]:
    adv: dict[str, Any] = {"max_results": int(request.max_results)}
    source_policy: dict[str, Any] = {}
    if request.include_domains:
        source_policy["include_domains"] = list(request.include_domains)
    if request.exclude_domains:
        source_policy["exclude_domains"] = list(request.exclude_domains)
    if request.freshness_days is not None:
        days = max(0, int(request.freshness_days))
        after_date = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        source_policy["after_date"] = after_date
    if source_policy:
        adv["source_policy"] = source_policy
    return adv


class ParallelSearchRoute:
    """Parallel Search API adapter (header-only ``x-api-key`` auth)."""

    route_id = "parallel"
    capabilities = SearchCapabilities(
        supports_country=False,
        supports_language=False,
        supports_freshness=True,
        supports_domain_filter=True,
        supports_content=True,
        supports_extract=True,
        supports_deep_mode=True,
    )

    def is_configured(self) -> bool:
        return bool(env_secret("PARALLEL_API_KEY"))

    def status(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "endpoint": _SEARCH_URL,
        }

    def search(self, request: SearchRequest) -> SearchResult:
        api_key = require_api_key(self.route_id, "PARALLEL_API_KEY")
        clean_query = scrub_query_for_provider(request.query, taint=request.taint)
        clean_objective = scrub_query_for_provider(
            request.objective or clean_query,
            taint=request.taint,
        )
        cfg = load_research_config()
        p_cfg = cfg.providers.get("parallel") or {}
        mode = (
            str(p_cfg.get("mode_deep") or "basic")
            if request.mode == "deep"
            else str(p_cfg.get("mode_quick") or "fast")
        )
        if mode not in {"fast", "basic", "advanced"}:
            mode = "fast"

        body = {
            "objective": clean_objective,
            "search_queries": _split_concise_queries(clean_query),
            "mode": mode,
            "advanced_settings": _build_advanced_settings(request),
        }

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
            raise SearchUnavailableError("parallel: invalid results field", code="invalid_json")

        hits: list[SearchHit] = []
        for idx, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            excerpts_raw = item.get("excerpts") or []
            excerpts = (
                tuple(str(e).strip() for e in excerpts_raw if str(e or "").strip())
                if isinstance(excerpts_raw, list)
                else ()
            )
            snippet = " ".join(excerpts)[:1200]
            content = "\n\n".join(excerpts) if (request.include_content and excerpts) else None
            score_val = item.get("score")
            score = float(score_val) if isinstance(score_val, (int, float)) else max(0.1, 1.0 - idx * 0.05)
            hits.append(
                SearchHit(
                    url=url,
                    title=str(item.get("title") or "").strip(),
                    snippet=snippet,
                    published_at=str(item.get("publish_date")).strip() if item.get("publish_date") else None,
                    provider="parallel",
                    score=score,
                    raw_excerpts=excerpts,
                    content=content,
                )
            )
            if len(hits) >= request.max_results:
                break

        return SearchResult(
            ok=True,
            route_id=self.route_id,
            provider="parallel",
            hits=tuple(hits),
            provenance="live_api",
            latency_ms=latency_ms,
            cached=False,
            estimated_cost_usd=_ESTIMATED_COST_USD,
        )

    def extract(
        self,
        url: str,
        *,
        taint: QueryTaintContext | None = None,
        objective: str | None = None,
        max_chars: int = 12_000,
        telemetry: Telemetry | None = None,
        actor: str | None = None,
        site: str = "azom",
        case_id: str | None = None,
    ) -> FetchResult:
        """Extract clean page content for a firewall-validated public HTTPS URL."""
        char_limit = int(max_chars)
        if char_limit <= 0:
            raise ValueError("max_chars must be > 0")

        cfg = load_research_config()
        tel = telemetry or default_telemetry
        fw = OutboundQueryFirewall()

        try:
            clean_url = fw.check_url(url, taint=taint)
        except SearchPolicyError as exc:
            tel.record(
                action="web_fetch_policy_blocked",
                site=site,
                cost_usd=0.0,
                case_id=case_id,
                actor=actor,
                meta={"error_class": "SearchPolicyError", "error_code": str(exc).split(":", 1)[-1].strip()},
            )
            raise

        domain = (urllib.parse.urlsplit(clean_url).hostname or "").lower()

        if is_mock_mode():
            mock_raw = (
                f"Mock Extract: {domain}\n\n"
                f"Deterministic offline mock extract for {clean_url}."
            )
            truncated = len(mock_raw) > char_limit
            clean_text = sanitize_untrusted_text(mock_raw, max_chars=char_limit)
            tel.record(
                action="web_fetch_page",
                site=site,
                units=1,
                unit_type="api_calls",
                cost_usd=0.0,
                case_id=case_id,
                actor=actor,
                meta={
                    "estimated_search_usd": 0.0,
                    "cost_kind": "mock",
                    "domain": domain,
                    "status_code": 200,
                    "content_type": "text/markdown",
                    "truncated": truncated,
                },
            )
            return FetchResult(
                ok=True,
                url=clean_url,
                final_url=clean_url,
                title=f"Mock Extract: {domain}",
                text=clean_text,
                content_type="text/markdown",
                status_code=200,
                latency_ms=1,
                truncated=truncated,
            )

        if kill_switch_active("fetch"):
            raise SearchConfigError("Web fetch disabled by kill switch", code="web_fetch_kill_switch")
        if not web_fetch_enabled():
            raise SearchConfigError(
                "Live web fetch is disabled (requires AZOM_USE_MOCK=0 and AZOM_WEB_FETCH_ENABLED=1)",
                code="web_fetch_disabled",
            )
        budget_ok, budget_reason = can_run_fetch(telemetry=tel, config=cfg)
        if not budget_ok:
            tel.record(
                action="web_fetch_budget_skip",
                site=site,
                cost_usd=0.0,
                case_id=case_id,
                actor=actor,
                meta={"domain": domain, "error_code": budget_reason},
            )
            raise SearchConfigError(f"Fetch budget exhausted ({budget_reason})", code=budget_reason)

        api_key = require_api_key(self.route_id, "PARALLEL_API_KEY")
        body: dict[str, Any] = {
            "urls": [clean_url],
            "advanced_settings": {"excerpts": True, "full_content": True},
        }
        if objective:
            body["objective"] = scrub_query_for_provider(objective, taint=taint)

        started = time.monotonic()
        data = pinned_json_request(
            self.route_id,
            _EXTRACT_URL,
            method="POST",
            headers={"x-api-key": api_key},
            json_body=body,
            allowed_auth_origins=(_API_ORIGIN,),
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        results = data.get("results") or []
        first = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else None
        if first is None:
            return FetchResult(
                ok=False,
                url=clean_url,
                final_url=clean_url,
                title="",
                text="",
                content_type="text/markdown",
                status_code=404,
                latency_ms=latency_ms,
                error_class="SearchUnavailableError",
                error_code="empty_extract",
            )

        raw_text = str(first.get("full_content") or "")
        if not raw_text:
            excerpts = first.get("excerpts") or []
            if isinstance(excerpts, list):
                raw_text = "\n\n".join(str(e) for e in excerpts if e)
        truncated = char_limit > 0 and len(raw_text) > char_limit
        clean_text = sanitize_untrusted_text(raw_text, max_chars=char_limit)
        clean_title = sanitize_untrusted_text(str(first.get("title") or ""), max_chars=300)
        final_url = str(first.get("url") or clean_url).strip() or clean_url
        final_domain = (urllib.parse.urlsplit(final_url).hostname or domain).lower()
        tel.record(
            action="web_fetch_page",
            site=site,
            units=1,
            unit_type="api_calls",
            cost_usd=0.0,
            case_id=case_id,
            actor=actor,
            meta={
                "estimated_search_usd": _ESTIMATED_COST_USD,
                "cost_kind": "estimated_external",
                "domain": final_domain,
                "status_code": 200,
                "content_type": "text/markdown",
                "truncated": truncated,
            },
        )
        return FetchResult(
            ok=True,
            url=clean_url,
            final_url=final_url,
            title=clean_title,
            text=clean_text,
            content_type="text/markdown",
            status_code=200,
            latency_ms=latency_ms,
            truncated=truncated,
        )


__all__ = ["ParallelSearchRoute"]
