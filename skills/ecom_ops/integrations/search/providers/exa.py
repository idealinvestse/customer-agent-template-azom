"""Exa AI semantic search & contents adapter (``route_id="exa"``)."""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
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
    scrub_objective_for_provider,
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

_API_ORIGIN = "https://api.exa.ai"
_SEARCH_URL = f"{_API_ORIGIN}/search"
_CONTENTS_URL = f"{_API_ORIGIN}/contents"
_ESTIMATED_COST_USD = 0.005
_MAX_CONTENTS_BATCH = 20
_VALID_SEARCH_TYPES = frozenset({"auto", "deep", "neural", "keyword", "fast", "lite"})


def _match_url_key(url: str) -> str:
    raw = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}?{parts.query}" if parts.query else f"{host}{path}"


class ExaSearchRoute:
    """Exa Search & Contents API adapter (header-only ``x-api-key`` auth)."""

    route_id = "exa"
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
        return bool(env_secret("EXA_API_KEY"))

    def status(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "endpoint": _SEARCH_URL,
            "contents_endpoint": _CONTENTS_URL,
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
        ).strip().lower()
        if search_type not in _VALID_SEARCH_TYPES:
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

    def _build_contents_result(
        self,
        clean_url: str,
        item: dict[str, Any] | None,
        *,
        char_limit: int,
        latency_ms: int,
        tel: Telemetry,
        actor: str | None,
        site: str,
        case_id: str | None,
        error_code: str = "empty_contents",
        record_cost: float = _ESTIMATED_COST_USD,
    ) -> FetchResult:
        domain = (urllib.parse.urlsplit(clean_url).hostname or "").lower()
        if item is None:
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
                error_code=error_code,
                provider="exa",
            )

        raw_text = str(item.get("text") or "").strip()
        if not raw_text:
            highlights = item.get("highlights") or []
            if isinstance(highlights, list):
                raw_text = "\n\n".join(str(h).strip() for h in highlights if str(h or "").strip())
        if not raw_text:
            return FetchResult(
                ok=False,
                url=clean_url,
                final_url=str(item.get("url") or clean_url).strip() or clean_url,
                title=sanitize_untrusted_text(str(item.get("title") or ""), max_chars=300),
                text="",
                content_type="text/markdown",
                status_code=204,
                latency_ms=latency_ms,
                error_class="SearchUnavailableError",
                error_code="empty_contents",
                provider="exa",
            )

        truncated = char_limit > 0 and len(raw_text) > char_limit
        clean_text = sanitize_untrusted_text(raw_text, max_chars=char_limit)
        clean_title = sanitize_untrusted_text(str(item.get("title") or ""), max_chars=300)
        final_url = str(item.get("url") or clean_url).strip() or clean_url
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
                "estimated_search_usd": record_cost,
                "cost_kind": "estimated_external",
                "provider": "exa",
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
            provider="exa",
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
        """Extract clean page content for a firewall-validated public HTTPS URL via ``/contents``."""
        char_limit = int(max_chars)
        if char_limit <= 0:
            raise ValueError("max_chars must be > 0")

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

        results = self.extract_many(
            [clean_url],
            taint=taint,
            objective=objective,
            max_chars=char_limit,
            telemetry=tel,
            actor=actor,
            site=site,
            case_id=case_id,
        )
        return results[0]

    def extract_many(
        self,
        urls: Sequence[str],
        *,
        taint: QueryTaintContext | None = None,
        objective: str | None = None,
        max_chars: int = 12_000,
        telemetry: Telemetry | None = None,
        actor: str | None = None,
        site: str = "azom",
        case_id: str | None = None,
    ) -> list[FetchResult]:
        """Batch-extract page contents via ``POST https://api.exa.ai/contents``."""
        raw_urls = [str(u or "") for u in urls]
        if not raw_urls:
            return []
        char_limit = int(max_chars)
        if char_limit <= 0:
            raise ValueError("max_chars must be > 0")

        tel = telemetry or default_telemetry
        cfg = load_research_config()
        fw = OutboundQueryFirewall()

        cleaned_slots: list[str | None] = []
        blocked_results: dict[int, FetchResult] = {}
        unique_valid: list[str] = []
        seen_valid: set[str] = set()

        for idx, raw_u in enumerate(raw_urls):
            try:
                cu = fw.check_url(raw_u, taint=taint)
            except SearchPolicyError as exc:
                code = str(exc).split(":", 1)[-1].strip()
                tel.record(
                    action="web_fetch_policy_blocked",
                    site=site,
                    cost_usd=0.0,
                    case_id=case_id,
                    actor=actor,
                    meta={"error_class": "SearchPolicyError", "error_code": code},
                )
                cleaned_slots.append(None)
                blocked_results[idx] = FetchResult(
                    ok=False,
                    url=raw_u,
                    final_url=raw_u,
                    title="",
                    text="",
                    content_type="text/markdown",
                    status_code=0,
                    latency_ms=0,
                    error_class="SearchPolicyError",
                    error_code=code,
                    provider="exa",
                )
                continue
            cleaned_slots.append(cu)
            if cu not in seen_valid:
                seen_valid.add(cu)
                unique_valid.append(cu)

        if not unique_valid:
            return [blocked_results[i] for i in range(len(raw_urls))]

        by_url: dict[str, FetchResult] = {}
        if is_mock_mode():
            for cu in unique_valid:
                domain = (urllib.parse.urlsplit(cu).hostname or "").lower()
                mock_raw = (
                    f"Mock Exa Contents: {domain}\n\n"
                    f"Deterministic offline mock contents for {cu}."
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
                        "provider": "mock",
                        "domain": domain,
                        "status_code": 200,
                        "content_type": "text/markdown",
                        "truncated": truncated,
                    },
                )
                by_url[cu] = FetchResult(
                    ok=True,
                    url=cu,
                    final_url=cu,
                    title=f"Mock Exa Contents: {domain}",
                    text=clean_text,
                    content_type="text/markdown",
                    status_code=200,
                    latency_ms=1,
                    truncated=truncated,
                    provider="mock",
                )
        else:
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
                    meta={"error_code": budget_reason},
                )
                raise SearchConfigError(f"Fetch budget exhausted ({budget_reason})", code=budget_reason)

            api_key = require_api_key(self.route_id, "EXA_API_KEY")
            clean_obj = (
                scrub_objective_for_provider(objective, taint=taint, max_out_chars=240)
                if objective
                else None
            )
            batches = [
                unique_valid[i : i + _MAX_CONTENTS_BATCH]
                for i in range(0, len(unique_valid), _MAX_CONTENTS_BATCH)
            ]

            def _run_batch(batch_urls: list[str]) -> dict[str, FetchResult]:
                body: dict[str, Any] = {
                    "urls": batch_urls,
                    "text": {"maxCharacters": min(20_000, char_limit)},
                    "highlights": {"query": clean_obj} if clean_obj else True,
                }
                started = time.monotonic()
                data = pinned_json_request(
                    self.route_id,
                    _CONTENTS_URL,
                    method="POST",
                    headers={"x-api-key": api_key},
                    json_body=body,
                    allowed_auth_origins=(_API_ORIGIN,),
                )
                latency_ms = int((time.monotonic() - started) * 1000)

                raw_results = data.get("results") or []
                raw_statuses = data.get("statuses") or []
                matched_items: dict[str, dict[str, Any]] = {}
                if isinstance(raw_results, list):
                    for item in raw_results:
                        if not isinstance(item, dict):
                            continue
                        u = str(item.get("url") or item.get("id") or "").strip()
                        if u:
                            matched_items[u] = item
                            matched_items[_match_url_key(u)] = item

                status_errors: dict[str, str] = {}
                if isinstance(raw_statuses, list):
                    for st in raw_statuses:
                        if not isinstance(st, dict):
                            continue
                        if str(st.get("status") or "").lower() == "error":
                            sid = str(st.get("id") or "").strip()
                            err_tag = str(st.get("error") or "contents_error").strip()
                            if sid:
                                status_errors[sid] = err_tag
                                status_errors[_match_url_key(sid)] = err_tag

                per_url_cost = round(_ESTIMATED_COST_USD / max(1, len(batch_urls)), 6)
                out_map: dict[str, FetchResult] = {}
                for idx_b, bu in enumerate(batch_urls):
                    item = matched_items.get(bu) or matched_items.get(_match_url_key(bu))
                    if (
                        item is None
                        and isinstance(raw_results, list)
                        and len(raw_results) == len(batch_urls)
                        and isinstance(raw_results[idx_b], dict)
                    ):
                        item = raw_results[idx_b]
                    err_code = (
                        status_errors.get(bu)
                        or status_errors.get(_match_url_key(bu))
                        or "empty_contents"
                    )
                    out_map[bu] = self._build_contents_result(
                        bu,
                        item,
                        char_limit=char_limit,
                        latency_ms=latency_ms,
                        tel=tel,
                        actor=actor,
                        site=site,
                        case_id=case_id,
                        error_code=err_code,
                        record_cost=per_url_cost,
                    )
                return out_map

            if len(batches) == 1:
                by_url.update(_run_batch(batches[0]))
            else:
                with ThreadPoolExecutor(max_workers=min(3, len(batches))) as pool:
                    for batch_map in pool.map(_run_batch, batches):
                        by_url.update(batch_map)

        final_results: list[FetchResult] = []
        for idx, cu in enumerate(cleaned_slots):
            if cu is None:
                final_results.append(blocked_results[idx])
            else:
                final_results.append(by_url[cu])
        return final_results


__all__ = ["ExaSearchRoute"]
