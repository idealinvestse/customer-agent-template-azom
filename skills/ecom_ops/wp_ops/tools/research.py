"""WordPress-loop adapters for bounded web search, safe fetch, and async Deep Research.

All HTTP I/O stays inside ``ecom_ops.integrations.search`` via lazy calls to
``ecom_ops.research``; this module contains zero HTTP or network imports.
"""

from __future__ import annotations

from typing import Any

from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.tools.registry import register_tool
from ecom_ops.wp_ops.tools.scrub import site_bound_secrets
from ecom_ops.wp_ops.tools.types import RESEARCH_ID_RE, SITE_ID_RE, ToolContext, ToolOutput
from ecom_ops.wp_ops.tools.validation import error_payload, short_text


def _web_search_outcome(data: dict[str, Any], _reads: int | None) -> str:
    hits = data.get("hits_count")
    prov = short_text(data.get("provenance"), 16) or "mock"
    if isinstance(hits, int):
        return f"{hits} träffar ({prov})"
    return f"webbsök klar ({prov})"


def _web_fetch_outcome(data: dict[str, Any], _reads: int | None) -> str:
    domain = short_text(data.get("domain"), 48)
    return f"webbsida hämtad ({domain})" if domain else "webbsida hämtad"


def _research_start_outcome(data: dict[str, Any], _reads: int | None) -> str:
    rid = short_text(data.get("research_id"), 24)
    return f"research {rid} startad" if rid else "research startad"


def _research_status_outcome(data: dict[str, Any], _reads: int | None) -> str:
    st = short_text(data.get("status"), 24) or "okänd"
    return f"research-status: {st}"


def _research_get_outcome(data: dict[str, Any], _reads: int | None) -> str:
    rid = short_text(data.get("research_id"), 24)
    section = short_text(data.get("section"), 20)
    if rid and section:
        return f"brief {rid} ({section})"
    return f"brief {rid}" if rid else "research-brief hämtad"


def _attach_research_outcome(data: dict[str, Any], _reads: int | None) -> str:
    rid = short_text(data.get("research_id"), 24)
    pid = short_text(data.get("plan_id"), 24)
    if rid and pid:
        return f"{rid} kopplad till {pid}"
    return "research kopplad till arbetsyta"


_RESEARCH_OUTCOME_FORMATTERS = {
    "web_search": _web_search_outcome,
    "web_fetch_extract": _web_fetch_outcome,
    "web_research_start": _research_start_outcome,
    "web_research_status": _research_status_outcome,
    "web_research_get": _research_get_outcome,
    "wp_workspace_attach_research": _attach_research_outcome,
}


def _denied_output(site_id: str | None = None) -> ToolOutput:
    denied = error_payload("AccessDenied")
    denied["status"] = "denied"
    return ToolOutput(denied, site_id=site_id)


def _taint_for_ctx(tools: Any, ctx: ToolContext) -> Any:
    from ecom_ops.research.firewall import QueryTaintContext

    site_id = ctx.site_id or ctx.sticky_site_id
    extra_secrets: list[str] = []
    if isinstance(site_id, str) and SITE_ID_RE.fullmatch(site_id):
        site_obj = tools._site_model(site_id)
        extra_secrets = site_bound_secrets(site_obj)
    return QueryTaintContext(secrets=tuple(extra_secrets)) if extra_secrets else None


def _run_research_facade(
    name: str,
    tools: Any,
    ctx: ToolContext,
    arguments: dict[str, Any],
) -> ToolOutput:
    from ecom_ops.research.tools import execute_research_tool

    res = execute_research_tool(
        name,
        arguments,
        actor=ctx.actor,
        channel=ctx.channel or "wp_chat",
        peer_id=ctx.conversation_id or "local",
        taint=_taint_for_ctx(tools, ctx),
        on_enqueue=None,
    )
    payload = dict(res) if isinstance(res, dict) else error_payload("ToolFailed")
    payload.setdefault("live_calls", payload.get("provenance") == "live")
    if not payload.get("ok") and "status" not in payload:
        payload["status"] = "denied" if payload.get("error_class") == "AccessDenied" else "unknown"
    return ToolOutput(payload)


@register_tool(
    name="web_search",
    group="file",
    label="Sök på webben",
    short_label="Webbsökning",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 250},
            "profile": {"type": "string", "default": "deep_general"},
            "locale": {"type": ["string", "null"]},
            "freshness_days": {"type": ["integer", "null"], "minimum": 1, "maximum": 3650},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            "domain_hints": {
                "type": ["array", "null"],
                "items": {"type": "string"},
            },
        },
    },
    outcome=_RESEARCH_OUTCOME_FORMATTERS["web_search"],
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=False,
    untrusted_output=True,
    web_search_budget=True,
    capability_group="web_research",
)
def handle_web_search(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)
    except AccessDenied:
        return _denied_output()
    return _run_research_facade("web_search", tools, ctx, arguments)


@register_tool(
    name="web_fetch_extract",
    group="file",
    label="Hämta och extrahera webbsida",
    short_label="Hämta webbsida",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "minLength": 1, "maxLength": 2048},
            "profile": {"type": "string", "default": "deep_general"},
            "focus_question": {"type": ["string", "null"], "maxLength": 250},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 12000, "default": 5000},
        },
    },
    outcome=_RESEARCH_OUTCOME_FORMATTERS["web_fetch_extract"],
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=False,
    untrusted_output=True,
    web_fetch_budget=True,
    capability_group="web_research",
)
def handle_web_fetch_extract(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)
    except AccessDenied:
        return _denied_output()
    return _run_research_facade("web_fetch_extract", tools, ctx, arguments)


@register_tool(
    name="web_research_start",
    group="file",
    label="Starta asynkron research",
    short_label="Starta research",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["topic"],
        "properties": {
            "topic": {"type": "string", "minLength": 1, "maxLength": 500},
            "profile": {"type": "string", "default": "deep_general"},
            "depth": {
                "type": "string",
                "enum": ["quick", "standard", "deep"],
                "default": "standard",
            },
            "target_ref": {"type": ["string", "null"]},
            "market": {"type": ["string", "array", "null"]},
            "focus": {"type": ["string", "array", "null"]},
        },
    },
    outcome=_RESEARCH_OUTCOME_FORMATTERS["web_research_start"],
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=False,
    capability_group="web_research",
)
def handle_web_research_start(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    from ecom_ops.research.models import SearchPolicyError
    from ecom_ops.research.store import authorize_target_ref

    target_ref = arguments.get("target_ref")
    if target_ref is not None and not isinstance(target_ref, str):
        return ToolOutput(error_payload("InvalidToolArguments"))
    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.RESEARCH_RUN)
        authorize_target_ref(actor_obj.name, target_ref)
    except AccessDenied:
        return _denied_output()
    except SearchPolicyError as exc:
        err = error_payload("SearchPolicyError")
        err["message"] = str(exc)
        return ToolOutput(err)
    return _run_research_facade("web_research_start", tools, ctx, arguments)


@register_tool(
    name="web_research_status",
    group="file",
    label="Kontrollera research-status",
    short_label="Research-status",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["research_id"],
        "properties": {
            "research_id": {"type": "string", "pattern": r"^res-[0-9a-f]{12}$"},
        },
    },
    outcome=_RESEARCH_OUTCOME_FORMATTERS["web_research_status"],
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=False,
    capability_group="web_research",
)
def handle_web_research_status(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)
    except AccessDenied:
        return _denied_output()
    rid = arguments.get("research_id")
    if not isinstance(rid, str) or not RESEARCH_ID_RE.fullmatch(rid.strip()):
        return ToolOutput(error_payload("InvalidToolArguments"))
    return _run_research_facade("web_research_status", tools, ctx, arguments)


@register_tool(
    name="web_research_get",
    group="file",
    label="Hämta research-brief",
    short_label="Hämta research",
    schema={
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
    outcome=_RESEARCH_OUTCOME_FORMATTERS["web_research_get"],
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=False,
    untrusted_output=True,
    capability_group="web_research",
)
def handle_web_research_get(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.WEB_SEARCH)
    except AccessDenied:
        return _denied_output()
    rid = arguments.get("research_id")
    if not isinstance(rid, str) or not RESEARCH_ID_RE.fullmatch(rid.strip()):
        return ToolOutput(error_payload("InvalidToolArguments"))
    return _run_research_facade("web_research_get", tools, ctx, arguments)


def _build_research_snapshot(
    research_id: str,
    job: Any,
    loaded_brief: dict[str, Any],
) -> dict[str, Any]:
    from ecom_ops.research.sanitizer import sanitize_untrusted_text

    if loaded_brief.get("ok") and isinstance(loaded_brief.get("brief"), dict):
        brief = loaded_brief["brief"]
        citations = []
        for cit in (brief.get("citations") or [])[:10]:
            if isinstance(cit, dict):
                citations.append(
                    {
                        "id": str(cit.get("id") or "")[:16],
                        "title": sanitize_untrusted_text(str(cit.get("title") or ""), max_chars=200),
                        "url": str(cit.get("url") or "")[:400],
                        "domain": str(cit.get("domain") or "")[:120],
                    }
                )
        return {
            "research_id": research_id,
            "topic": sanitize_untrusted_text(str(brief.get("topic") or ""), max_chars=400),
            "profile_id": str(brief.get("profile_id") or "deep_general")[:64],
            "evidence_status": str(brief.get("evidence_status") or "partial")[:32],
            "provenance": str(brief.get("provenance") or "mock")[:32],
            "summary": sanitize_untrusted_text(str(brief.get("summary") or ""), max_chars=4000),
            "recommendations": [
                sanitize_untrusted_text(str(r), max_chars=300)
                for r in (brief.get("recommendations") or [])[:8]
                if r
            ],
            "gaps": [
                sanitize_untrusted_text(str(g), max_chars=240)
                for g in (brief.get("gaps") or [])[:6]
                if g
            ],
            "citations": citations,
        }

    spec = getattr(job, "spec", None)
    return {
        "research_id": research_id,
        "status": str(getattr(job, "status", "queued") or "queued"),
        "topic": sanitize_untrusted_text(str(getattr(spec, "topic", "") or ""), max_chars=400),
        "profile_id": str(getattr(spec, "profile_id", "deep_general") or "deep_general")[:64],
        "evidence_status": (
            str(job.evidence_status) if getattr(job, "evidence_status", None) is not None else None
        ),
        "provenance": "mock",
        "summary": "",
        "recommendations": [],
        "gaps": [],
        "citations": [],
    }


@register_tool(
    name="wp_workspace_attach_research",
    group="workspace",
    label="Koppla research till arbetsyta",
    short_label="Koppla research",
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["plan_id", "research_id"],
        "properties": {
            "site_id": {"type": "string"},
            "plan_id": {"type": "string"},
            "research_id": {"type": "string", "pattern": r"^res-[0-9a-f]{12}$"},
        },
    },
    outcome=_RESEARCH_OUTCOME_FORMATTERS["wp_workspace_attach_research"],
    in_file_schemas=True,
    in_extra_file_tools=True,
    requires_site=True,
    plan_kind="wr",
    workspace_tool=True,
    mutates_workspace=False,
    capability_group="web_research",
)
def handle_workspace_attach_research(
    tools: Any,
    ctx: ToolContext,
    arguments: dict[str, Any],
    *,
    ops: Any,
) -> ToolOutput:
    from ecom_ops.research import get_research_service
    from ecom_ops.research.models import SearchPolicyError
    from ecom_ops.research.store import authorize_research_access, authorize_target_ref

    site_id = str(ctx.site_id)
    plan_id = str(arguments["plan_id"])
    raw_rid = arguments.get("research_id")
    if not isinstance(raw_rid, str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    research_id = raw_rid.strip().strip("`")
    if not RESEARCH_ID_RE.fullmatch(research_id):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    try:
        actor_obj = resolve_actor(ctx.actor)
        require_permission(actor_obj, Permission.WP_OPS_OPERATE)
        require_permission(actor_obj, Permission.WEB_SEARCH)
        authorize_target_ref(actor_obj.name, f"wp:{site_id}:{plan_id}")
    except AccessDenied:
        return _denied_output(site_id)
    except SearchPolicyError:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    svc = get_research_service(getattr(tools.service, "data_dir", None))
    job = svc.store.get_job(research_id)
    if job is None or not authorize_research_access(actor_obj.name, job, "view"):
        return ToolOutput(
            {
                "ok": False,
                "status": "unknown",
                "error_class": "NotFoundOrDenied",
                "research_id": research_id,
                "site_id": site_id,
                "plan_id": plan_id,
                "live_calls": False,
            },
            site_id=site_id,
        )

    loaded_brief = svc.store.read_brief(research_id, actor=actor_obj.name)
    snapshot = _build_research_snapshot(research_id, job, loaded_brief)
    attached = ops.attach_research(
        site_id,
        plan_id,
        research_id,
        actor=actor_obj.name,
        summary_snapshot=snapshot,
    )
    if isinstance(attached, dict):
        out_payload = dict(attached)
        out_payload.setdefault("live_calls", False)
        if out_payload.get("ok") is True:
            out_payload.setdefault("status", "attached")
        return ToolOutput(out_payload, site_id=site_id)
    return tools._file_result(attached, site_id)


__all__ = [
    "handle_web_fetch_extract",
    "handle_web_research_get",
    "handle_web_research_start",
    "handle_web_research_status",
    "handle_web_search",
    "handle_workspace_attach_research",
]
