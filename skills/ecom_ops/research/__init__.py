"""Universal web search and async deep research package.

Leaf exports remain acyclic and fast at import time; heavy service/engine/worker/tool
helpers import lazily on demand.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ecom_ops.research.budget import (
    can_run_fetch,
    can_run_search,
    research_budget_status,
)
from ecom_ops.research.config import (
    DepthTierConfig,
    ResearchBudgetsConfig,
    ResearchCacheConfig,
    ResearchConfig,
    ResearchEgressConfig,
    ResearchRetentionConfig,
    clear_research_config_cache,
    effective_allowlist_domains,
    effective_route_order,
    is_mock_mode,
    kill_switch_active,
    load_research_config,
    research_enabled,
    web_fetch_enabled,
    web_search_enabled,
)
from ecom_ops.research.firewall import (
    OutboundQueryFirewall,
    QueryTaintContext,
)
from ecom_ops.research.models import (
    RESEARCH_ID_RE,
    Citation,
    ClaimKind,
    ConflictNote,
    EvaluatedSource,
    EvidenceStatus,
    ResearchBrief,
    ResearchJob,
    ResearchSpec,
    SearchPolicyError,
    SearchProfile,
    SourceType,
    new_research_id,
)
from ecom_ops.research.profiles import get_profile, list_profiles
from ecom_ops.research.sanitizer import (
    frame_untrusted_web_data,
    sanitize_untrusted_text,
)

_VALID_DEPTHS = frozenset({"quick", "standard", "deep"})
_SERVICE_LOCK = threading.Lock()
_SERVICES: dict[str, ResearchService] = {}


def _host_matches_any(host: str, domains: Sequence[str]) -> bool:
    clean = host.strip().lower().rstrip(".")
    if not clean:
        return False
    for dom in domains:
        norm = str(dom or "").strip().lower().lstrip(".")
        if norm and (clean == norm or clean.endswith(f".{norm}")):
            return True
    return False


def _filter_cached_sources(
    sources: Sequence[Any],
    *,
    allowlist: Sequence[str],
    denylist: Sequence[str],
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for src in sources:
        if not isinstance(src, dict):
            continue
        raw_url = str(src.get("canonical_url") or src.get("url") or "").strip()
        host = str(src.get("domain") or "").strip().lower()
        if not host and raw_url:
            host = (urlsplit(raw_url).hostname or "").strip().lower()
        if not host:
            continue
        if denylist and _host_matches_any(host, denylist):
            continue
        if allowlist and not _host_matches_any(host, allowlist):
            continue
        kept.append(src)
    return kept


def _resolve_locale_country(
    locale: str | None,
    country: str | None,
    profile: SearchProfile,
) -> tuple[str | None, str | None]:
    lang: str | None = None
    ctry: str | None = (country or "").strip().upper() or None
    raw_loc = (locale or "").strip()
    if raw_loc:
        if len(raw_loc) == 2 and raw_loc.isupper():
            ctry = ctry or raw_loc
            lang = {"SE": "sv", "NO": "nb", "DK": "da"}.get(raw_loc, "en")
        elif "-" in raw_loc or "_" in raw_loc:
            sep = "-" if "-" in raw_loc else "_"
            p_lang, p_ctry = raw_loc.split(sep, 1)
            lang = p_lang.strip().lower() or None
            if len(p_ctry.strip()) == 2:
                ctry = ctry or p_ctry.strip().upper()
        else:
            lang = raw_loc.lower()
            if not ctry:
                ctry = {"sv": "SE", "nb": "NO", "no": "NO", "da": "DK"}.get(lang)

    if not ctry:
        env_c = (os.environ.get("AZOM_RESEARCH_DEFAULT_COUNTRY") or "").strip().upper()
        if env_c:
            ctry = env_c
        else:
            for item in profile.market_defaults:
                if len(item) == 2 and item.isupper():
                    ctry = item
                    break
    if not lang:
        env_l = (os.environ.get("AZOM_RESEARCH_DEFAULT_LANG") or "").strip().lower()
        if env_l:
            lang = env_l
        else:
            for item in profile.market_defaults:
                if len(item) == 2 and item.islower():
                    lang = item
                    break
    return lang, ctry


class ResearchService:
    """High-level facade for sync search, safe fetch, and async Deep Research."""

    def __init__(
        self,
        *,
        data_dir: Path | str | None = None,
        store: Any | None = None,
        cache: Any | None = None,
        search_router: Any | None = None,
        fetcher: Any | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._store = store
        self._cache = cache
        self._search_router = search_router
        self._fetcher = fetcher
        self._telemetry = telemetry

    def _base_dir(self) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))

    def _store_data_dir(self) -> Path:
        store_path = getattr(self.store, "path", None)
        return store_path.parent if store_path is not None else self._base_dir()

    @property
    def store(self):
        if self._store is None:
            from ecom_ops.research.store import ResearchStore

            base = self._base_dir()
            self._store = ResearchStore(
                path=base / "research_jobs.sqlite3",
                artifacts_dir=base / "research_artifacts",
            )
        return self._store

    @property
    def cache(self):
        if self._cache is None:
            from ecom_ops.research.store import SearchCache

            base = self._base_dir()
            self._cache = SearchCache(path=base / "web_search_cache.sqlite3")
        return self._cache

    @property
    def search_router(self):
        if self._search_router is None:
            from ecom_ops.integrations.search.router import get_search_router

            self._search_router = get_search_router()
        return self._search_router

    @property
    def fetcher(self):
        if self._fetcher is None:
            from ecom_ops.integrations.search.fetcher import SafePublicHttpClient

            self._fetcher = SafePublicHttpClient()
        return self._fetcher

    @property
    def telemetry(self):
        if self._telemetry is None:
            from ecom_ops.telemetry import default_telemetry

            self._telemetry = default_telemetry
        return self._telemetry

    @staticmethod
    def is_mock() -> bool:
        return is_mock_mode()

    def quick_search(
        self,
        query: str,
        *,
        actor: str = "agent",
        profile_id: str = "deep_general",
        locale: str | None = None,
        country: str | None = None,
        freshness_days: int | None = None,
        max_results: int = 5,
        include_domains: Sequence[str] | None = None,
        exclude_domains: Sequence[str] | None = None,
        taint: QueryTaintContext | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Run a bounded synchronous web search with RBAC, firewall, evaluation, and optional cache."""
        from ecom_ops.integrations.search.base import SearchRequest
        from ecom_ops.integrations.search.errors import SearchConfigError
        from ecom_ops.rbac import Permission, require_permission, resolve_actor
        from ecom_ops.research.evaluator import evaluate_search_hits

        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)

        profile = get_profile(profile_id)
        cfg = load_research_config()
        fw = OutboundQueryFirewall()
        clean_query = fw.check_query(query, taint=taint)

        if not is_mock_mode():
            if kill_switch_active("search"):
                raise SearchConfigError("Web search is disabled by kill switch", code="web_search_kill_switch")
            if not web_search_enabled():
                raise SearchConfigError(
                    "Live web search is disabled (requires AZOM_USE_MOCK=0 and AZOM_WEB_SEARCH_ENABLED=1)",
                    code="web_search_disabled",
                )

        lang, ctry = _resolve_locale_country(locale, country, profile)
        eff_freshness = int(freshness_days) if freshness_days is not None else profile.freshness_days
        clamped_max = max(1, min(10, int(max_results)))
        inc_doms = tuple(str(d).strip().lower().lstrip(".") for d in (include_domains or ()) if str(d).strip())
        exc_doms = tuple(str(d).strip().lower().lstrip(".") for d in (exclude_domains or ()) if str(d).strip())
        allowlist = effective_allowlist_domains(cfg)
        denylist = cfg.egress.denylist_domains
        combined_deny = tuple(dict.fromkeys((*denylist, *exc_doms)))

        cache_key = ""
        if use_cache and cfg.cache.enabled and not is_mock_mode():
            cache_key = self.cache.make_key(
                "search",
                clean_query,
                profile_id=profile.id,
                locale=f"{ctry or ''}:{lang or ''}",
                filters={
                    "freshness_days": eff_freshness,
                    "max_results": clamped_max,
                    "include_domains": inc_doms,
                    "exclude_domains": exc_doms,
                    "allowlist_domains": allowlist,
                    "denylist_domains": denylist,
                },
            )
            cached = self.cache.get(cache_key)
            if isinstance(cached, dict):
                filtered_sources = _filter_cached_sources(
                    cached.get("sources") or [],
                    allowlist=allowlist,
                    denylist=combined_deny,
                )
                if filtered_sources:
                    q_hash = hashlib.sha256(clean_query.encode("utf-8")).hexdigest()[:16]
                    self.telemetry.record(
                        action="web_search_selected",
                        site="azom",
                        units=1,
                        unit_type="api_calls",
                        cost_usd=0.0,
                        actor=actor_obj.name,
                        meta={
                            "estimated_search_usd": 0.0,
                            "cost_kind": "cached",
                            "query_hash": q_hash,
                            "profile_id": profile.id,
                            "provider": cached.get("provider", ""),
                            "route_id": cached.get("route_id", ""),
                            "hits_count": len(filtered_sources),
                            "cached": True,
                        },
                    )
                    return {
                        **cached,
                        "hits_count": len(filtered_sources),
                        "sources": filtered_sources,
                        "cached": True,
                    }

        req = SearchRequest(
            query=clean_query,
            profile_id=profile.id,
            country=ctry,
            language=lang,
            freshness_days=eff_freshness,
            max_results=clamped_max,
            include_domains=inc_doms,
            exclude_domains=exc_doms,
            include_content=False,
            mode="quick",
            taint=taint,
            actor=actor_obj.name,
            site="azom",
        )
        result = self.search_router.search(req, telemetry=self.telemetry)
        provenance = "mock" if result.provenance in {"mock", "mock_fixture"} else "live"
        sources, evidence_status, conflicts = evaluate_search_hits(
            result.hits,
            query=clean_query,
            profile=profile,
            provenance=provenance,
        )
        payload = {
            "ok": True,
            "profile_id": profile.id,
            "evidence_status": str(evidence_status),
            "provenance": provenance,
            "route_id": result.route_id,
            "provider": result.provider,
            "cached": False,
            "hits_count": len(sources),
            "sources": [s.to_dict() for s in sources],
            "conflicts": [c.to_dict() for c in conflicts],
        }
        if cache_key and cfg.cache.enabled and not is_mock_mode() and sources:
            ttl_hours = (
                cfg.cache.news_ttl_hours
                if profile.id == "support_fact_check"
                else (cfg.cache.docs_ttl_hours if profile.id == "technical_docs" else cfg.cache.default_ttl_hours)
            )
            self.cache.set(cache_key, payload, ttl_seconds=ttl_hours * 3600, kind="search")
        return payload

    def fetch_extract(
        self,
        url: str,
        *,
        actor: str = "agent",
        profile_id: str = "deep_general",
        max_chars: int = 5000,
        taint: QueryTaintContext | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Fetch and extract clean text from one public HTTPS URL."""
        from ecom_ops.integrations.search.errors import SearchConfigError
        from ecom_ops.rbac import Permission, require_permission, resolve_actor

        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)

        profile = get_profile(profile_id)
        cfg = load_research_config()
        fw = OutboundQueryFirewall()
        clean_url = fw.check_url(url, taint=taint)

        if not is_mock_mode():
            if kill_switch_active("fetch"):
                raise SearchConfigError("Web fetch disabled by kill switch", code="web_fetch_kill_switch")
            if not web_fetch_enabled():
                raise SearchConfigError(
                    "Live web fetch is disabled (requires AZOM_USE_MOCK=0 and AZOM_WEB_FETCH_ENABLED=1)",
                    code="web_fetch_disabled",
                )

        char_limit = max(200, min(cfg.egress.max_extracted_chars, int(max_chars)))
        cache_key = ""
        if use_cache and cfg.cache.enabled and not is_mock_mode():
            cache_key = self.cache.make_key(
                "fetch",
                clean_url,
                profile_id=profile.id,
                filters={"max_chars": char_limit},
            )
            cached = self.cache.get(cache_key)
            if isinstance(cached, dict):
                return {**cached, "cached": True}

        fres = self.fetcher.fetch(
            clean_url,
            taint=taint,
            telemetry=self.telemetry,
            max_extracted_chars=char_limit,
            actor=actor_obj.name,
            site="azom",
        )
        domain = clean_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        payload = {
            "ok": fres.ok,
            "url": fres.url,
            "final_url": fres.final_url,
            "domain": domain,
            "title": fres.title,
            "text": fres.text,
            "content_type": fres.content_type,
            "status_code": fres.status_code,
            "latency_ms": fres.latency_ms,
            "truncated": fres.truncated,
            "cached": False,
            "provenance": "mock" if is_mock_mode() else "live",
            "error_class": fres.error_class,
            "error_code": fres.error_code,
        }
        if fres.ok and cache_key and cfg.cache.enabled and not is_mock_mode():
            ttl_hours = cfg.cache.docs_ttl_hours if profile.id == "technical_docs" else cfg.cache.default_ttl_hours
            self.cache.set(cache_key, payload, ttl_seconds=ttl_hours * 3600, kind="fetch")
        return payload

    def start_research(
        self,
        topic: str,
        *,
        actor: str = "agent",
        profile_id: str = "deep_general",
        depth: str = "standard",
        target_ref: str | None = None,
        markets: Sequence[str] | None = None,
        locale: str | None = None,
        country: str | None = None,
        focus_domains: Sequence[str] | None = None,
        exclude_domains: Sequence[str] | None = None,
        queries_seed: Sequence[str] | None = None,
        visibility: str = "private",
        channel: str = "",
        peer_id: str = "",
        taint: QueryTaintContext | None = None,
        start_worker: bool = False,
    ) -> ResearchJob:
        """Validate RBAC/firewall and enqueue an async Deep Research job (<1s)."""
        from ecom_ops.audit import log_action
        from ecom_ops.integrations.search.errors import SearchConfigError
        from ecom_ops.rbac import Permission, require_permission, resolve_actor
        from ecom_ops.research.store import authorize_target_ref

        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.RESEARCH_RUN)
        authorize_target_ref(actor_obj.name, target_ref)

        if not is_mock_mode():
            if kill_switch_active("research"):
                raise SearchConfigError(
                    "Deep research is disabled by kill switch",
                    code="research_kill_switch",
                )
            if not research_enabled():
                raise SearchConfigError(
                    "Live deep research is disabled (requires AZOM_USE_MOCK=0 and AZOM_RESEARCH_ENABLED=1)",
                    code="research_disabled",
                )

        profile = get_profile(profile_id)
        norm_depth = (depth or "standard").strip().lower()
        if norm_depth not in _VALID_DEPTHS:
            raise ValueError(f"Invalid research depth: {depth}")

        clean_topic = " ".join(sanitize_untrusted_text(topic, max_chars=500).split())
        if not clean_topic:
            raise SearchPolicyError("OutboundQueryBlocked: empty_topic")
        if taint is None and target_ref:
            from ecom_ops.research.engine import resolve_taint_for_target_ref

            taint = resolve_taint_for_target_ref(target_ref, self._store_data_dir())
        fw = OutboundQueryFirewall()
        clean_topic = fw.check_text(clean_topic, max_chars=500, taint=taint)

        clean_seeds: list[str] = []
        for raw_seed in (queries_seed or ()):
            seed = str(raw_seed or "").strip()
            if seed:
                clean_seeds.append(fw.check_query(seed, taint=taint))

        lang, ctry = _resolve_locale_country(locale, country, profile)
        spec = ResearchSpec(
            topic=clean_topic,
            profile_id=profile.id,
            depth=norm_depth,
            markets=[str(m).strip() for m in (markets or ()) if str(m).strip()],
            locale=lang or "sv",
            country=ctry or "SE",
            focus_domains=[str(d).strip().lower().lstrip(".") for d in (focus_domains or ()) if str(d).strip()],
            exclude_domains=[str(d).strip().lower().lstrip(".") for d in (exclude_domains or ()) if str(d).strip()],
            target_ref=target_ref.strip() if target_ref else None,
            queries_seed=clean_seeds,
        )
        job = self.store.enqueue_job(
            spec,
            actor=actor_obj.name,
            channel=channel,
            peer_id=peer_id,
            target_ref=spec.target_ref,
            visibility=visibility,
        )
        log_action(
            actor=actor_obj.name,
            action="research_job_enqueued",
            target="research",
            target_id=job.research_id,
            success=True,
            details={
                "research_id": job.research_id,
                "profile_id": profile.id,
                "depth": norm_depth,
                "visibility": job.visibility,
            },
        )
        if start_worker:
            from ecom_ops.research.worker import ensure_research_worker_started

            ensure_research_worker_started(data_dir=self._base_dir())
        return job

    def get_status(
        self,
        research_id: str,
        *,
        actor: str,
        since_seq: int = 0,
    ) -> dict[str, Any]:
        """Return job status and ordered progress events (RBAC-enforced)."""
        from ecom_ops.rbac import Permission, require_permission, resolve_actor

        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)
        job = self.store.get_job(research_id, actor=actor_obj.name)
        if job is None:
            return {
                "ok": False,
                "status": "not_found",
                "research_id": research_id,
            }
        events = self.store.list_events(research_id, since_seq=since_seq)
        return {
            "ok": True,
            "research_id": job.research_id,
            "status": job.status,
            "job": job.to_dict(),
            "events": events,
        }

    def get_brief(self, research_id: str, *, actor: str) -> dict[str, Any]:
        """Return persisted ``brief.json``, ``sources.json``, and ``meta.json`` (RBAC-enforced)."""
        from ecom_ops.rbac import Permission, require_permission, resolve_actor

        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)
        return self.store.read_brief(research_id, actor=actor_obj.name)

    def list_jobs(
        self,
        *,
        actor: str,
        limit: int = 50,
        status: str | None = None,
        target_ref: str | None = None,
    ) -> list[ResearchJob]:
        """List research jobs visible to ``actor``."""
        from ecom_ops.rbac import Permission, require_permission, resolve_actor

        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)
        return self.store.list_jobs(
            actor=actor_obj.name,
            limit=limit,
            status=status,
            target_ref=target_ref,
        )

    def steer_research(
        self,
        research_id: str,
        note: str,
        *,
        actor: str,
        taint: QueryTaintContext | None = None,
    ) -> dict[str, Any]:
        """Queue a follow-up steering note for ``research_id`` (owner or ``RESEARCH_ADMIN``)."""
        from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor

        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.RESEARCH_RUN)
        if taint is None:
            job = self.store.get_job(research_id)
            if job is not None:
                from ecom_ops.research.store import authorize_research_access

                if not authorize_research_access(actor_obj.name, job, "steer"):
                    raise AccessDenied(f"Actor {actor_obj.name!r} cannot steer research job {research_id}")
                from ecom_ops.research.engine import _resolve_taint_for_job

                taint = _resolve_taint_for_job(job, self._store_data_dir())
        clean_note = OutboundQueryFirewall().check_query(note, taint=taint)
        steer_id = self.store.add_steer(research_id, actor_obj.name, clean_note, taint=taint)
        return {"ok": True, "research_id": research_id, "steer_id": steer_id}

    def cancel_research(self, research_id: str, *, actor: str) -> dict[str, Any]:
        """Request cooperative cancellation (owner or ``RESEARCH_ADMIN``)."""
        from ecom_ops.rbac import Permission, require_permission, resolve_actor

        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, Permission.RESEARCH_RUN)
        result = self.store.request_cancel(research_id, actor_obj.name)
        return {"ok": result != "not_found", "research_id": research_id, "result": result}


def get_research_service(data_dir: Path | str | None = None) -> ResearchService:
    """Return the per-``AZOM_DATA_DIR`` singleton ``ResearchService``."""
    base = Path(data_dir or os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    key = str(base.resolve())
    with _SERVICE_LOCK:
        svc = _SERVICES.get(key)
        if svc is None:
            svc = ResearchService(data_dir=base)
            _SERVICES[key] = svc
        return svc


def reset_research_service() -> None:
    """Clear cached ``ResearchService`` singletons (intended for tests)."""
    with _SERVICE_LOCK:
        _SERVICES.clear()


def __getattr__(name: str) -> Any:
    if name == "ResearchEngine":
        from ecom_ops.research.engine import ResearchEngine

        return ResearchEngine
    if name in {
        "ResearchWorkerPool",
        "ensure_research_worker_started",
        "get_research_worker_pool",
        "shutdown_research_workers",
    }:
        from ecom_ops.research import worker

        return getattr(worker, name)
    if name in {
        "RESEARCH_TOOL_NAMES",
        "RESEARCH_TOOL_SCHEMAS",
        "WEB_RESEARCH_TOOL_GUIDE",
        "execute_research_tool",
    }:
        from ecom_ops.research import tools

        return getattr(tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "RESEARCH_ID_RE",
    "Citation",
    "ClaimKind",
    "ConflictNote",
    "EvaluatedSource",
    "EvidenceStatus",
    "OutboundQueryFirewall",
    "QueryTaintContext",
    "ResearchBrief",
    "ResearchBudgetsConfig",
    "ResearchCacheConfig",
    "ResearchConfig",
    "ResearchEgressConfig",
    "ResearchJob",
    "ResearchRetentionConfig",
    "ResearchService",
    "ResearchSpec",
    "SearchPolicyError",
    "SearchProfile",
    "SourceType",
    "DepthTierConfig",
    "can_run_fetch",
    "can_run_search",
    "clear_research_config_cache",
    "effective_allowlist_domains",
    "effective_route_order",
    "frame_untrusted_web_data",
    "get_profile",
    "get_research_service",
    "is_mock_mode",
    "kill_switch_active",
    "list_profiles",
    "load_research_config",
    "new_research_id",
    "research_budget_status",
    "research_enabled",
    "reset_research_service",
    "sanitize_untrusted_text",
    "web_fetch_enabled",
    "web_search_enabled",
]
