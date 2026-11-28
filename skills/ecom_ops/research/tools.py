"""Canonical 5-tool LLM specifications, compact prompt guide, and bounded executor."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.integrations.search.errors import SearchRouteError
from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.research import get_research_service
from ecom_ops.research.firewall import QueryTaintContext
from ecom_ops.research.models import RESEARCH_ID_RE, SearchPolicyError
from ecom_ops.research.profiles import recommend_profile
from ecom_ops.research.sanitizer import frame_untrusted_web_data, sanitize_untrusted_text
from ecom_ops.research.store import authorize_research_access, authorize_target_ref

WEB_SEARCH_MAX_BYTES = 5 * 1024
WEB_FETCH_MAX_BYTES = 7 * 1024
WEB_RESEARCH_GET_MAX_BYTES = 8 * 1024
_TOOL_RESULT_ENVELOPE_HEADROOM_BYTES = 32

RESEARCH_TOOL_NAMES: tuple[str, ...] = (
    "web_search",
    "web_fetch_extract",
    "web_research_start",
    "web_research_status",
    "web_research_get",
)

RESEARCH_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "web_search": {
        "name": "web_search",
        "description": (
            "Run a fast bounded public HTTPS search for quick facts, carrier/docs status, or "
            "URL discovery (never include PII, order IDs, or secrets). Supports profile='auto' "
            "or a named SearchProfile; returns nonce-framed hits and evidence status."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 250},
                "profile": {"type": "string", "default": "auto"},
                "locale": {"type": ["string", "null"]},
                "freshness_days": {"type": ["integer", "null"], "minimum": 1, "maximum": 3650},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "domain_hints": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
            },
        },
    },
    "web_fetch_extract": {
        "name": "web_fetch_extract",
        "description": (
            "Deeply inspect ONE specific public HTTPS URL via Parallel Extract / Exa / DNS-pinned "
            "SSRF guard (use after web_search or on an explicit URL; pass focus_question when targeted)."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 2048},
                "profile": {"type": "string", "default": "auto"},
                "focus_question": {"type": ["string", "null"], "maxLength": 250},
                "max_chars": {"type": "integer", "minimum": 200, "maximum": 12000, "default": 5000},
            },
        },
    },
    "web_research_start": {
        "name": "web_research_start",
        "description": (
            "Enqueue a non-blocking async multi-source Deep Research job for comparisons, market "
            "audits, or multi-page synthesis (returns res-<12hex> immediately; do NOT poll status in a loop)."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["topic"],
            "properties": {
                "topic": {"type": "string", "minLength": 1, "maxLength": 500},
                "profile": {"type": "string", "default": "auto"},
                "depth": {
                    "type": "string",
                    "enum": ["auto", "quick", "standard", "deep"],
                    "default": "standard",
                },
                "target_ref": {"type": ["string", "null"]},
                "market": {"type": ["string", "array", "null"]},
                "focus": {"type": ["string", "array", "null"]},
            },
        },
    },
    "web_research_status": {
        "name": "web_research_status",
        "description": "Check progress and evidence status for an existing res-<12hex> job when asked.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["research_id"],
            "properties": {
                "research_id": {"type": "string", "pattern": r"^res-[0-9a-f]{12}$"},
            },
        },
    },
    "web_research_get": {
        "name": "web_research_get",
        "description": (
            "Read a scrubbed, nonce-framed section of a completed or partial res-<12hex> brief."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["research_id"],
            "properties": {
                "research_id": {"type": "string", "pattern": r"^res-[0-9a-f]{12}$"},
                "section": {
                    "type": "string",
                    "enum": [
                        "summary",
                        "findings",
                        "recommendations",
                        "gaps",
                        "conflicts",
                        "citations",
                        "markdown",
                        "all",
                    ],
                    "default": "summary",
                },
            },
        },
    },
}

WEB_RESEARCH_TOOL_GUIDE = (
    "Webb & research (publik HTTPS; aldrig kund-PII/hemligheter):\n"
    "- Samma svar (synk): web_search(query, profile='auto'|..., max_results?) + web_fetch_extract(url, focus_question?)\n"
    "- Bakgrundsbrief (endast asynk): web_research_start(topic, target_ref='wp_site:<site_id>', depth?) -> res-*; polla ej\n"
    "- Jobb: web_research_status(research_id) / web_research_get(research_id, section?)"
)


def _utf8_len(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _bounded_framed_output(
    payload: dict[str, Any],
    *,
    raw_text: str,
    label: str,
    max_bytes: int,
    text_key: str = "content",
) -> dict[str, Any]:
    """Attach nonce-framed ``raw_text`` while guaranteeing total JSON <= ``max_bytes``."""
    effective_max = max(512, int(max_bytes) - _TOOL_RESULT_ENVELOPE_HEADROOM_BYTES)
    budget = max(512, effective_max - _utf8_len(payload) - 160)
    char_cap = max(200, budget)
    for _ in range(5):
        framed = frame_untrusted_web_data(raw_text, label=label, max_chars=char_cap)
        candidate = dict(payload)
        candidate[text_key] = framed
        if _utf8_len(candidate) <= effective_max:
            return candidate
        char_cap = max(120, int(char_cap * 0.65))
    framed = frame_untrusted_web_data(raw_text, label=label, max_chars=120)
    candidate = dict(payload)
    candidate[text_key] = framed
    return candidate


def _policy_code(exc: SearchPolicyError) -> str:
    msg = str(exc)
    if ":" in msg:
        return msg.split(":", 1)[1].strip()[:64] or "policy_blocked"
    return "policy_blocked"


def _error_result(
    error_class: str,
    error_code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error_class": error_class,
        "error_code": error_code,
        "message": sanitize_untrusted_text(message, max_chars=240),
    }


def _opaque_not_found_or_denied(research_id: str) -> dict[str, Any]:
    return {
        "ok": False,
        "research_id": research_id,
        "error_class": "NotFoundOrDenied",
        "error_code": "not_found_or_denied",
        "message": "Research job not found or access denied.",
    }


def _format_search_hits_text(sources: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for src in sources:
        sid = src.get("source_id") or src.get("id") or ""
        title = src.get("title") or ""
        url = src.get("canonical_url") or src.get("url") or ""
        domain = src.get("domain") or ""
        snippet = src.get("snippet") or ""
        extracted = src.get("extracted_text") or ""
        body = snippet or extracted[:400]
        lines.append(f"[{sid}] {title} ({domain})\nURL: {url}\n{body}")
    if conflicts:
        lines.append("Conflicts: " + "; ".join(str(c.get("summary") or "") for c in conflicts[:3]))
    return "\n\n".join(lines) if lines else "No matching external sources."


def _format_brief_section(brief: dict[str, Any], section: str) -> str:
    sec = (section or "summary").strip().lower()
    if sec == "summary":
        parts = [str(brief.get("summary") or "")]
        cits = brief.get("citations") or []
        if cits:
            parts.append(
                "Sources: "
                + "; ".join(f"[{c.get('id')}] {c.get('title')} ({c.get('url')})" for c in cits[:8])
            )
        return "\n\n".join(p for p in parts if p)
    if sec == "findings":
        lines = []
        for f in brief.get("findings") or []:
            if isinstance(f, dict):
                lines.append(f"- ({f.get('claim_kind', 'fact')}) {f.get('claim', '')}")
            else:
                lines.append(f"- {f}")
        return "\n".join(lines) or "No findings recorded."
    if sec == "recommendations":
        recs = [f"- {r}" for r in (brief.get("recommendations") or []) if r]
        return "\n".join(recs) or "No recommendations recorded."
    if sec == "gaps":
        gaps = [f"- {g}" for g in (brief.get("gaps") or []) if g]
        return "\n".join(gaps) or "No gaps recorded."
    if sec == "conflicts":
        confs = [
            f"- [{c.get('severity', 'minor')}] {c.get('summary', '')}"
            for c in (brief.get("conflicts") or [])
            if isinstance(c, dict)
        ]
        return "\n".join(confs) or "No source conflicts detected."
    if sec == "citations":
        cits = [
            f"[{c.get('id')}] {c.get('title')} ({c.get('domain')}) — {c.get('url')}"
            for c in (brief.get("citations") or [])
            if isinstance(c, dict)
        ]
        return "\n".join(cits) or "No citations recorded."
    if sec in {"markdown", "all"}:
        return str(brief.get("markdown") or brief.get("summary") or "")
    return str(brief.get("summary") or "")


def _resolve_tool_profile(
    raw_profile: Any,
    topic_or_query: str,
    *,
    default_ambition: str | None = None,
) -> str:
    prof = str(raw_profile or "").strip().lower()
    if prof and prof != "auto":
        return prof
    return recommend_profile(topic_or_query, ambition=default_ambition).id


def _derive_locale_country_from_markets(markets: list[str]) -> tuple[str | None, str | None]:
    if not markets:
        return None, None
    first = markets[0].strip().upper()
    if first == "SE":
        return "sv", "SE"
    if first == "NO":
        return "nb", "NO"
    if first == "DK":
        return "da", "DK"
    if first == "EN":
        return "en", None
    return None, (first if len(first) == 2 and first.isalpha() else None)


def execute_research_tool(
    name: str,
    args: dict[str, Any] | None,
    *,
    actor: str,
    channel: str = "cli",
    peer_id: str = "local",
    taint: QueryTaintContext | None = None,
    on_enqueue: Callable[[Any], None] | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Execute one canonical research tool with strict RBAC, firewall, and size caps."""
    tool_name = str(name or "").strip()
    if tool_name not in RESEARCH_TOOL_NAMES:
        return _error_result("UnknownTool", "unknown_tool", f"Unknown research tool: {tool_name}")

    arguments = dict(args or {})
    service = get_research_service(data_dir)

    try:
        actor_obj = resolve_actor(actor)
    except AccessDenied as exc:
        return _error_result("AccessDenied", "access_denied", str(exc))

    try:
        if tool_name == "web_search":
            require_permission(actor_obj, Permission.WEB_SEARCH)
            query = str(arguments.get("query") or "").strip()
            profile = _resolve_tool_profile(
                arguments.get("profile"),
                query,
                default_ambition="quick",
            )
            locale = arguments.get("locale")
            freshness_days = arguments.get("freshness_days")
            max_results = int(arguments.get("max_results") or 5)
            domain_hints = arguments.get("domain_hints")
            include_domains = (
                [str(d) for d in domain_hints]
                if isinstance(domain_hints, (list, tuple))
                else ([str(domain_hints)] if isinstance(domain_hints, str) and domain_hints.strip() else None)
            )
            res = service.quick_search(
                query,
                actor=actor_obj.name,
                profile_id=profile,
                locale=str(locale) if locale else None,
                freshness_days=int(freshness_days) if freshness_days is not None else None,
                max_results=max(1, min(10, max_results)),
                include_domains=include_domains,
                taint=taint,
            )
            sources = list(res.get("sources") or [])
            conflicts = list(res.get("conflicts") or [])
            raw_text = _format_search_hits_text(sources, conflicts)
            payload = {
                "ok": True,
                "tool": "web_search",
                "profile_id": res.get("profile_id"),
                "evidence_status": res.get("evidence_status"),
                "provenance": res.get("provenance"),
                "route_id": res.get("route_id"),
                "provider": res.get("provider"),
                "cached": bool(res.get("cached")),
                "hits_count": len(sources),
            }
            out = _bounded_framed_output(
                payload,
                raw_text=raw_text,
                label="WEB_SEARCH_HITS",
                max_bytes=WEB_SEARCH_MAX_BYTES,
                text_key="content",
            )
            log_action(
                actor=actor_obj.name,
                action="research_tool_web_search",
                target="web_search",
                success=True,
                details={
                    "profile_id": res.get("profile_id"),
                    "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
                    "result_count": len(sources),
                    "evidence_status": res.get("evidence_status"),
                    "provenance": res.get("provenance"),
                },
            )
            return out

        if tool_name == "web_fetch_extract":
            require_permission(actor_obj, Permission.WEB_SEARCH)
            url = str(arguments.get("url") or "").strip()
            focus_question = arguments.get("focus_question")
            raw_prof = str(arguments.get("profile") or "").strip()
            if not raw_prof or raw_prof.lower() == "auto":
                profile = (
                    recommend_profile(str(focus_question)).id
                    if focus_question
                    else "deep_general"
                )
            else:
                profile = raw_prof
            max_chars = max(200, min(12_000, int(arguments.get("max_chars") or 5000)))
            fetch_kwargs: dict[str, Any] = {
                "actor": actor_obj.name,
                "profile_id": profile,
                "max_chars": max_chars,
                "taint": taint,
            }
            if focus_question:
                try:
                    sig = inspect.signature(service.fetch_extract)
                    if "objective" in sig.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig.parameters.values()
                    ):
                        fetch_kwargs["objective"] = str(focus_question).strip()
                except (TypeError, ValueError):
                    pass
            res = service.fetch_extract(url, **fetch_kwargs)
            if not res.get("ok"):
                return _error_result(
                    str(res.get("error_class") or "SearchUnavailableError"),
                    str(res.get("error_code") or "fetch_failed"),
                    f"Failed to fetch URL ({res.get('error_code') or 'error'}).",
                )
            header = f"Title: {res.get('title') or ''}\nURL: {res.get('final_url') or res.get('url') or ''}"
            if focus_question:
                header += f"\nFocus: {sanitize_untrusted_text(str(focus_question), max_chars=200)}"
            raw_text = f"{header}\n\n{res.get('text') or ''}".strip()
            payload = {
                "ok": True,
                "tool": "web_fetch_extract",
                "url": res.get("final_url") or res.get("url"),
                "domain": res.get("domain"),
                "status_code": res.get("status_code"),
                "truncated": bool(res.get("truncated")),
                "cached": bool(res.get("cached")),
                "provenance": res.get("provenance"),
            }
            out = _bounded_framed_output(
                payload,
                raw_text=raw_text,
                label="WEB_FETCH_EXTRACT",
                max_bytes=WEB_FETCH_MAX_BYTES,
                text_key="content",
            )
            log_action(
                actor=actor_obj.name,
                action="research_tool_web_fetch_extract",
                target="web_fetch",
                success=True,
                details={
                    "profile_id": profile,
                    "domain": res.get("domain"),
                    "provenance": res.get("provenance"),
                },
            )
            return out

        if tool_name == "web_research_start":
            require_permission(actor_obj, Permission.RESEARCH_RUN)
            topic = str(arguments.get("topic") or "").strip()
            raw_depth = str(arguments.get("depth") or "standard").strip().lower()
            depth_auto = raw_depth in {"", "auto"}
            profile = _resolve_tool_profile(
                arguments.get("profile"),
                topic,
                default_ambition=None if depth_auto else raw_depth,
            )
            if depth_auto:
                from ecom_ops.research.profiles import get_profile

                try:
                    prof_amb = get_profile(profile).ambition
                    depth = prof_amb if prof_amb in {"quick", "standard", "deep"} else "standard"
                except KeyError:
                    depth = "standard"
            else:
                depth = raw_depth
            target_ref = arguments.get("target_ref")
            authorize_target_ref(actor_obj.name, str(target_ref) if target_ref else None)

            raw_market = arguments.get("market")
            markets: list[str] = []
            if isinstance(raw_market, str) and raw_market.strip():
                markets = [m.strip() for m in raw_market.split(",") if m.strip()]
            elif isinstance(raw_market, (list, tuple)):
                markets = [str(m).strip() for m in raw_market if str(m).strip()]
            derived_locale, derived_country = _derive_locale_country_from_markets(markets)

            raw_focus = arguments.get("focus")
            focus_domains: list[str] = []
            queries_seed: list[str] = []
            if isinstance(raw_focus, (list, tuple)):
                for item in raw_focus:
                    s = str(item or "").strip()
                    if "." in s and " " not in s:
                        focus_domains.append(s)
                    elif s:
                        queries_seed.append(s)
            elif isinstance(raw_focus, str) and raw_focus.strip():
                s = raw_focus.strip()
                if "." in s and " " not in s:
                    focus_domains.append(s)
                else:
                    queries_seed.append(s)

            job = service.start_research(
                topic,
                actor=actor_obj.name,
                profile_id=profile,
                depth=depth,
                target_ref=str(target_ref).strip() if target_ref else None,
                markets=markets,
                locale=derived_locale,
                country=derived_country,
                focus_domains=focus_domains,
                queries_seed=queries_seed,
                channel=channel,
                peer_id=peer_id,
                taint=taint,
                start_worker=False,
            )
            if on_enqueue is not None:
                try:
                    on_enqueue(job)
                except Exception:
                    pass
            return {
                "ok": True,
                "tool": "web_research_start",
                "research_id": job.research_id,
                "status": job.status,
                "profile_id": job.spec.profile_id if job.spec else profile,
                "depth": job.spec.depth if job.spec else depth,
                "provenance": "mock" if service.is_mock() else "live",
            }

        if tool_name == "web_research_status":
            require_permission(actor_obj, Permission.WEB_SEARCH)
            rid = str(arguments.get("research_id") or "").strip()
            if not RESEARCH_ID_RE.fullmatch(rid):
                return _opaque_not_found_or_denied(rid)
            job = service.store.get_job(rid)
            if job is None or not authorize_research_access(actor_obj.name, job, "view"):
                return _opaque_not_found_or_denied(rid)
            events = service.store.list_events(rid, limit=10)
            return {
                "ok": True,
                "tool": "web_research_status",
                "research_id": job.research_id,
                "status": job.status,
                "evidence_status": str(job.evidence_status) if job.evidence_status else None,
                "current_round": job.current_round,
                "queries_used": job.queries_used,
                "fetches_used": job.fetches_used,
                "error_class": job.error_class,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "completed_at": job.completed_at,
                "recent_events": events[-5:],
            }

        if tool_name == "web_research_get":
            require_permission(actor_obj, Permission.WEB_SEARCH)
            rid = str(arguments.get("research_id") or "").strip()
            section = str(arguments.get("section") or "summary").strip().lower()
            if not RESEARCH_ID_RE.fullmatch(rid):
                return _opaque_not_found_or_denied(rid)
            job = service.store.get_job(rid)
            if job is None or not authorize_research_access(actor_obj.name, job, "view"):
                return _opaque_not_found_or_denied(rid)
            loaded = service.store.read_brief(rid, actor=actor_obj.name)
            if not loaded.get("ok"):
                return _error_result(
                    str(loaded.get("error_class") or "artifact_unavailable"),
                    str(loaded.get("status") or "artifact_unavailable"),
                    str(loaded.get("message") or "Research brief is unavailable."),
                )
            brief = loaded["brief"]
            raw_text = _format_brief_section(brief, section)
            payload = {
                "ok": True,
                "tool": "web_research_get",
                "research_id": rid,
                "section": section,
                "profile_id": brief.get("profile_id"),
                "evidence_status": brief.get("evidence_status"),
                "provenance": brief.get("provenance"),
                "citations_count": len(brief.get("citations") or []),
            }
            return _bounded_framed_output(
                payload,
                raw_text=raw_text,
                label="RESEARCH_BRIEF",
                max_bytes=WEB_RESEARCH_GET_MAX_BYTES,
                text_key="content",
            )

    except AccessDenied:
        if tool_name in {"web_research_status", "web_research_get"}:
            return _opaque_not_found_or_denied(str(arguments.get("research_id") or ""))
        return _error_result("AccessDenied", "access_denied", "Permission denied for research tool.")
    except SearchPolicyError as exc:
        return _error_result("SearchPolicyError", _policy_code(exc), str(exc))
    except SearchRouteError as exc:
        return _error_result(
            type(exc).__name__,
            getattr(exc, "code", None) or "route_error",
            str(exc),
        )
    except KeyError as exc:
        return _error_result("InvalidToolArguments", "unknown_profile", str(exc))
    except ValueError as exc:
        return _error_result("InvalidToolArguments", "invalid_arguments", str(exc))

    return _error_result("UnknownTool", "unknown_tool", f"Unhandled research tool: {tool_name}")


__all__ = [
    "RESEARCH_TOOL_NAMES",
    "RESEARCH_TOOL_SCHEMAS",
    "WEB_FETCH_MAX_BYTES",
    "WEB_RESEARCH_GET_MAX_BYTES",
    "WEB_RESEARCH_TOOL_GUIDE",
    "WEB_SEARCH_MAX_BYTES",
    "execute_research_tool",
]
