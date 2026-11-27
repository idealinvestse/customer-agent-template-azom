"""Multi-provider web search router with fail-closed gates, firewall, and breaker."""

from __future__ import annotations

import hashlib
import os
import time
import urllib.parse
from dataclasses import replace
from typing import Any

from ecom_ops.integrations import _http_guard
from ecom_ops.integrations.search import circuit_breaker
from ecom_ops.integrations.search.base import SearchHit, SearchRequest, SearchResult
from ecom_ops.integrations.search.errors import (
    SearchAuthError,
    SearchConfigError,
    SearchPolicyError,
    SearchRateLimitError,
    SearchUnavailableError,
)
from ecom_ops.integrations.search.providers.brave import BraveSearchRoute
from ecom_ops.integrations.search.providers.exa import ExaSearchRoute
from ecom_ops.integrations.search.providers.mock import MockSearchRoute
from ecom_ops.integrations.search.providers.parallel import ParallelSearchRoute
from ecom_ops.integrations.search.providers.searxng import SearxngSearchRoute
from ecom_ops.integrations.search.providers.tavily import TavilySearchRoute
from ecom_ops.research.budget import can_run_search
from ecom_ops.research.config import (
    ResearchConfig,
    effective_allowlist_domains,
    is_mock_mode,
    kill_switch_active,
    load_research_config,
    web_search_enabled,
)
from ecom_ops.research.firewall import OutboundQueryFirewall
from ecom_ops.research.profiles import get_profile
from ecom_ops.telemetry import Telemetry, default_telemetry

LIVE_ROUTE_IDS = ("parallel", "exa", "brave", "tavily", "searxng")


def _query_hash(query: str) -> str:
    return hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:16]


def _policy_code(exc: SearchPolicyError) -> str:
    msg = str(exc)
    if ":" in msg:
        return msg.split(":", 1)[1].strip()[:64] or "policy"
    return "policy"


def _host_matches_any(host: str, domains: tuple[str, ...]) -> bool:
    clean_host = host.strip().lower().rstrip(".")
    for d in domains:
        dom = str(d or "").strip().lower().lstrip(".")
        if dom and (clean_host == dom or clean_host.endswith(f".{dom}")):
            return True
    return False


def _dashboard_host() -> str:
    raw = str(os.environ.get("AZOM_DASHBOARD_PUBLIC_URL") or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw if "://" in raw else f"https://{raw}")
        return (parsed.hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""


def _validate_domain_tokens(
    domains: tuple[str, ...],
    *,
    denylist: tuple[str, ...],
    allowlist: tuple[str, ...],
) -> tuple[str, ...]:
    """Validate requested ``include_domains`` against SSRF/internal rules, denylist, and allowlist."""
    validated: list[str] = []
    for raw_dom in domains:
        dom = str(raw_dom or "").strip().lower().lstrip(".")
        if not dom:
            continue
        ok, clean_dom, reason = _http_guard._validate_public_dns_hostname(
            dom,
            block_example_test_tlds=True,
        )
        if not ok:
            raise SearchPolicyError(f"OutboundQueryBlocked: {reason or 'internal_host'}")
        if _host_matches_any(clean_dom, denylist):
            raise SearchPolicyError("OutboundQueryBlocked: domain_denylisted")
        if allowlist and not _host_matches_any(clean_dom, allowlist):
            continue
        if clean_dom not in validated:
            validated.append(clean_dom)
    if domains and allowlist and not validated:
        raise SearchPolicyError("OutboundQueryBlocked: domain_not_allowlisted")
    if not domains and allowlist:
        return allowlist
    return tuple(validated)


def _filter_hits(
    hits: tuple[SearchHit, ...],
    *,
    denylist: tuple[str, ...],
    allowlist: tuple[str, ...],
    exclude_domains: tuple[str, ...],
) -> tuple[SearchHit, ...]:
    """Filter returned hits so denylist, allowlist, and public-HTTPS rules always hold."""
    kept: list[SearchHit] = []
    combined_deny = tuple(dict.fromkeys((*denylist, *exclude_domains)))
    for hit in hits:
        try:
            parsed = _http_guard.safe_https_parsed(hit.url, allow_non_443=False)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if combined_deny and _host_matches_any(host, combined_deny):
            continue
        if allowlist and not _host_matches_any(host, allowlist):
            continue
        kept.append(hit)
    return tuple(kept)


class SearchRouter:
    """Fail-closed search router with firewall, circuit breaker, and failover."""

    def __init__(self) -> None:
        self._routes: dict[str, Any] = {
            "mock": MockSearchRoute(),
            "parallel": ParallelSearchRoute(),
            "exa": ExaSearchRoute(),
            "brave": BraveSearchRoute(),
            "tavily": TavilySearchRoute(),
            "searxng": SearxngSearchRoute(),
        }

    def ordered_route_ids(
        self,
        profile_id: str | None = None,
        *,
        config: ResearchConfig | None = None,
    ) -> list[str]:
        """Return live route priority honoring ``AZOM_SEARCH_ROUTE_ORDER`` and profile preferences."""
        cfg = config or load_research_config()
        raw_env_order = str(os.environ.get("AZOM_SEARCH_ROUTE_ORDER") or "").strip()
        if raw_env_order:
            env_ids: list[str] = []
            for part in raw_env_order.split(","):
                norm = part.strip().lower()
                if norm and norm != "mock" and norm in self._routes and norm not in env_ids:
                    env_ids.append(norm)
            if env_ids:
                return env_ids

        preferred: tuple[str, ...] = ()
        if profile_id:
            try:
                preferred = get_profile(profile_id).preferred_providers
            except KeyError:
                preferred = ()

        merged: list[str] = []
        for rid in (*preferred, *cfg.default_route_order):
            norm = str(rid or "").strip().lower()
            if norm and norm != "mock" and norm in self._routes and norm not in merged:
                merged.append(norm)
        return merged

    def routes(self) -> dict[str, dict[str, Any]]:
        """Return safe non-secret status for every search route."""
        return {rid: route.status() for rid, route in self._routes.items()}

    def any_configured(self) -> bool:
        """Return True if at least one live search provider is configured."""
        return any(
            self._routes[rid].is_configured() for rid in LIVE_ROUTE_IDS
        )

    def _is_route_enabled(self, route_id: str, cfg: ResearchConfig) -> bool:
        if route_id == "searxng":
            # Opt-in strictly via process-env SEARXNG_BASE_URL.
            return self._routes["searxng"].is_configured()
        p_cfg = cfg.providers.get(route_id)
        if isinstance(p_cfg, dict) and p_cfg.get("enabled") is False:
            return False
        return True

    def _prepare_request(
        self,
        request: SearchRequest,
        cfg: ResearchConfig,
    ) -> tuple[SearchRequest, tuple[str, ...], tuple[str, ...]]:
        fw = OutboundQueryFirewall()
        clean_query = fw.check_query(request.query, taint=request.taint)
        clean_objective = (
            fw.check_query(request.objective, taint=request.taint)
            if request.objective
            else None
        )

        dash_host = _dashboard_host()
        denylist = tuple(
            dict.fromkeys(
                d
                for d in (*cfg.egress.denylist_domains, dash_host)
                if str(d or "").strip()
            )
        )
        allowlist = effective_allowlist_domains(cfg)

        clean_include = _validate_domain_tokens(
            request.include_domains,
            denylist=denylist,
            allowlist=allowlist,
        )
        clean_exclude = tuple(dict.fromkeys((*request.exclude_domains, *denylist)))

        clean_req = replace(
            request,
            query=clean_query,
            objective=clean_objective,
            include_domains=clean_include,
            exclude_domains=clean_exclude,
        )
        return clean_req, denylist, allowlist

    def search(
        self,
        request: SearchRequest,
        *,
        telemetry: Telemetry | None = None,
        route_override: str | None = None,
    ) -> SearchResult:
        """Execute a search request enforcing precedence: gates > firewall > denylist > allowlist."""
        tel = telemetry or default_telemetry
        cfg = load_research_config()
        q_hash = _query_hash(request.query)
        mock_mode = is_mock_mode()

        # 1. Kill switches & live gates
        if not mock_mode:
            if kill_switch_active("search"):
                tel.record(
                    action="web_search_error",
                    site=request.site,
                    cost_usd=0.0,
                    case_id=request.case_id,
                    actor=request.actor,
                    meta={
                        "profile_id": request.profile_id,
                        "query_hash": q_hash,
                        "error_class": "SearchConfigError",
                        "error_code": "web_search_kill_switch",
                    },
                )
                raise SearchConfigError(
                    "Web search is disabled by kill switch",
                    code="web_search_kill_switch",
                )
            if not web_search_enabled():
                tel.record(
                    action="web_search_error",
                    site=request.site,
                    cost_usd=0.0,
                    case_id=request.case_id,
                    actor=request.actor,
                    meta={
                        "profile_id": request.profile_id,
                        "query_hash": q_hash,
                        "error_class": "SearchConfigError",
                        "error_code": "web_search_disabled",
                    },
                )
                raise SearchConfigError(
                    "Live web search is disabled (requires AZOM_USE_MOCK=0 and AZOM_WEB_SEARCH_ENABLED=1)",
                    code="web_search_disabled",
                )
            budget_ok, budget_reason = can_run_search(telemetry=tel, config=cfg)
            if not budget_ok:
                tel.record(
                    action="web_search_budget_skip",
                    site=request.site,
                    cost_usd=0.0,
                    case_id=request.case_id,
                    actor=request.actor,
                    meta={
                        "profile_id": request.profile_id,
                        "query_hash": q_hash,
                        "error_class": "SearchConfigError",
                        "error_code": budget_reason,
                    },
                )
                raise SearchConfigError(
                    f"Search budget exhausted ({budget_reason})",
                    code=budget_reason,
                )

        # 2. OutboundQueryFirewall & SSRF > 3. domain_denylist > 4. domain_allowlist
        try:
            clean_request, denylist, allowlist = self._prepare_request(request, cfg)
        except SearchPolicyError as exc:
            tel.record(
                action="web_search_policy_blocked",
                site=request.site,
                cost_usd=0.0,
                case_id=request.case_id,
                actor=request.actor,
                meta={
                    "profile_id": request.profile_id,
                    "query_hash": q_hash,
                    "error_class": "SearchPolicyError",
                    "error_code": _policy_code(exc),
                },
            )
            raise

        # Mock mode executes mock fixture route exclusively (zero sockets)
        if mock_mode:
            raw_res = self._routes["mock"].search(clean_request)
            filtered_hits = _filter_hits(
                raw_res.hits,
                denylist=denylist,
                allowlist=allowlist,
                exclude_domains=clean_request.exclude_domains,
            )
            res = replace(raw_res, hits=filtered_hits)
            tel.record(
                action="web_search_selected",
                site=clean_request.site,
                units=1,
                unit_type="api_calls",
                cost_usd=0.0,
                case_id=clean_request.case_id,
                actor=clean_request.actor,
                meta={
                    "estimated_search_usd": 0.0,
                    "cost_kind": "mock",
                    "query_hash": q_hash,
                    "profile_id": clean_request.profile_id,
                    "provider": res.provider,
                    "route_id": res.route_id,
                    "hits_count": len(res.hits),
                    "latency_ms": res.latency_ms,
                    "cached": False,
                },
            )
            return res

        # Live mode route selection and failover
        if route_override is not None:
            rid_norm = str(route_override).strip().lower()
            if rid_norm == "mock":
                raise SearchConfigError("mock route is forbidden in live mode", code="mock_disabled")
            if rid_norm not in self._routes:
                raise SearchConfigError(f"Unknown search route: {route_override}", code="unknown_route")
            candidate_ids = [rid_norm]
        else:
            candidate_ids = self.ordered_route_ids(clean_request.profile_id, config=cfg)

        errors: list[str] = []
        skipped_transient: list[str] = []
        auth_blocked: list[str] = []
        configured_count = 0
        attempts = 0

        def _execute_route(rid: str, *, bypass_breaker: bool = False) -> SearchResult | None:
            nonlocal attempts, configured_count
            route = self._routes.get(rid)
            if route is None or rid == "mock":
                errors.append(f"{rid}: unknown")
                return None
            if not self._is_route_enabled(rid, cfg):
                errors.append(f"{rid}: disabled")
                return None
            if not route.is_configured():
                errors.append(f"{rid}: not_configured")
                return None

            if not bypass_breaker:
                configured_count += 1

            allowed, br_state, rem_s = circuit_breaker.allow_attempt(
                rid,
                bypass=(bypass_breaker or bool(route_override)),
            )
            if not allowed:
                errors.append(f"{rid}: circuit_{br_state}")
                tel.record(
                    action="web_search_circuit_skipped",
                    site=clean_request.site,
                    cost_usd=0.0,
                    case_id=clean_request.case_id,
                    actor=clean_request.actor,
                    meta={
                        "route_id": rid,
                        "profile_id": clean_request.profile_id,
                        "query_hash": q_hash,
                        "breaker_state": br_state,
                        "cooldown_remaining_s": round(rem_s, 1),
                    },
                )
                if br_state == "open":
                    skipped_transient.append(rid)
                elif br_state == "open_auth":
                    auth_blocked.append(rid)
                return None

            attempts += 1
            try:
                raw_res = route.search(clean_request)
            except SearchPolicyError as exc:
                tel.record(
                    action="web_search_policy_blocked",
                    site=clean_request.site,
                    cost_usd=0.0,
                    case_id=clean_request.case_id,
                    actor=clean_request.actor,
                    meta={
                        "route_id": rid,
                        "profile_id": clean_request.profile_id,
                        "query_hash": q_hash,
                        "error_class": "SearchPolicyError",
                        "error_code": _policy_code(exc),
                    },
                )
                raise
            except (
                SearchAuthError,
                SearchRateLimitError,
                SearchUnavailableError,
                SearchConfigError,
            ) as exc:
                is_rl = isinstance(exc, SearchRateLimitError)
                tel.record(
                    action="web_search_rate_limited" if is_rl else "web_search_error",
                    site=clean_request.site,
                    cost_usd=0.0,
                    case_id=clean_request.case_id,
                    actor=clean_request.actor,
                    meta={
                        "route_id": rid,
                        "profile_id": clean_request.profile_id,
                        "query_hash": q_hash,
                        "error_class": type(exc).__name__,
                        "error_code": getattr(exc, "code", "") or "",
                        "http_status": getattr(exc, "http_status", None),
                    },
                )
                circuit_breaker.record_failure(rid, exc)
                errors.append(f"{rid}: {type(exc).__name__}")
                if route_override is not None:
                    raise
                return None

            circuit_breaker.record_success(rid)
            filtered_hits = _filter_hits(
                raw_res.hits,
                denylist=denylist,
                allowlist=allowlist,
                exclude_domains=clean_request.exclude_domains,
            )
            res = replace(raw_res, hits=filtered_hits)
            tel.record(
                action="web_search_selected",
                site=clean_request.site,
                units=1,
                unit_type="api_calls",
                cost_usd=0.0,
                case_id=clean_request.case_id,
                actor=clean_request.actor,
                meta={
                    "estimated_search_usd": float(res.estimated_cost_usd),
                    "cost_kind": "estimated_external",
                    "query_hash": q_hash,
                    "profile_id": clean_request.profile_id,
                    "provider": res.provider,
                    "route_id": res.route_id,
                    "hits_count": len(res.hits),
                    "latency_ms": res.latency_ms,
                    "cached": False,
                },
            )
            return res

        for rid in candidate_ids:
            res = _execute_route(rid)
            if res is not None:
                return res

        if attempts == 0 and skipped_transient and route_override is None:
            res = _execute_route(skipped_transient[0], bypass_breaker=True)
            if res is not None:
                return res

        if configured_count == 0:
            raise SearchConfigError(
                "No configured live search route available: " + "; ".join(errors),
                code="no_configured_route",
            )
        if attempts == 0:
            if auth_blocked and len(auth_blocked) == configured_count:
                raise SearchAuthError(
                    "All configured search routes blocked by auth breaker: " + "; ".join(errors),
                    code="circuit_open_auth",
                )
            raise SearchUnavailableError(
                "All configured search routes skipped by circuit breaker: " + "; ".join(errors),
                code="circuit_open",
            )
        raise SearchUnavailableError(
            "All configured search routes failed: " + "; ".join(errors),
            code="all_routes_failed",
        )

    def test_route(self, route_id: str) -> dict[str, Any]:
        """Isolated single-route credential probe (zero fallback, bypasses breaker)."""
        rid = str(route_id or "").strip().lower()
        started = time.monotonic()
        route = self._routes.get(rid)
        if route is None:
            return {
                "ok": False,
                "route_id": rid,
                "status": "error",
                "error_class": "SearchConfigError",
                "error_code": "unknown_route",
                "latency_ms": 0,
            }

        if is_mock_mode():
            if rid == "mock" or route.is_configured():
                return {
                    "ok": True,
                    "route_id": rid,
                    "status": "ok",
                    "error_class": None,
                    "error_code": None,
                    "latency_ms": 0,
                }
            return {
                "ok": False,
                "route_id": rid,
                "status": "missing",
                "error_class": "SearchConfigError",
                "error_code": "missing_key",
                "latency_ms": 0,
            }

        if rid == "mock":
            return {
                "ok": False,
                "route_id": rid,
                "status": "error",
                "error_class": "SearchConfigError",
                "error_code": "mock_disabled",
                "latency_ms": 0,
            }

        if not route.is_configured():
            return {
                "ok": False,
                "route_id": rid,
                "status": "missing",
                "error_class": "SearchConfigError",
                "error_code": "missing_key",
                "latency_ms": 0,
            }

        probe_req = SearchRequest(
            query="woocommerce documentation",
            profile_id="technical_docs",
            max_results=1,
            mode="quick",
        )
        try:
            res = route.search(probe_req)
            latency_ms = int((time.monotonic() - started) * 1000)
            return {
                "ok": bool(res.ok),
                "route_id": rid,
                "status": "ok" if res.ok else "error",
                "error_class": res.error_class,
                "error_code": res.error_code,
                "latency_ms": res.latency_ms or latency_ms,
            }
        except (
            SearchAuthError,
            SearchRateLimitError,
            SearchUnavailableError,
            SearchConfigError,
            SearchPolicyError,
        ) as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return {
                "ok": False,
                "route_id": rid,
                "status": "error",
                "error_class": type(exc).__name__,
                "error_code": getattr(exc, "code", None) or "error",
                "latency_ms": latency_ms,
            }


_router: SearchRouter | None = None


def get_search_router() -> SearchRouter:
    """Return the process-wide ``SearchRouter`` singleton."""
    global _router
    if _router is None:
        _router = SearchRouter()
    return _router


def reset_search_router() -> None:
    """Reset the ``SearchRouter`` singleton and breaker read cache (for tests)."""
    global _router
    _router = None
    circuit_breaker.reset_breaker_cache_for_tests()


__all__ = [
    "LIVE_ROUTE_IDS",
    "SearchRouter",
    "get_search_router",
    "reset_search_router",
]
