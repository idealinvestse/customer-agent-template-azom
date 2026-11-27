"""WordPress Shop Intelligence agent tools (run, status, query, security audit).

Read-only, zero-PII store learning and security/drift inspection backed by
``ecom_ops.shop_intel.ShopIntelService``. Zero direct network, socket, or shell
calls; never accesses customer/order PII or mutates WordPress/WooCommerce.
"""

from __future__ import annotations

from typing import Any

from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.shop_intel import (
    VALID_DOMAINS,
    VALID_DOMAINS_SET,
    VALID_MODES,
    VALID_PURPOSES,
    VALID_SEVERITIES,
    get_shop_intel_service,
)
from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.tools.registry import count_outcome, register_tool
from ecom_ops.wp_ops.tools.types import SITE_ID_RE, ToolContext, ToolOutput
from ecom_ops.wp_ops.tools.validation import (
    error_class_token,
    error_payload,
    safe_failure,
    short_text,
)

_VALID_SEVERITIES_SET = frozenset(VALID_SEVERITIES)


def _require_perm(actor: str, perm: Permission, site_id: str) -> ToolOutput | None:
    try:
        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, perm)
    except AccessDenied:
        denied = error_payload("AccessDenied")
        denied["status"] = "denied"
        return ToolOutput(denied, site_id=site_id)
    return None


def _require_any_perm(
    actor: str, perms: tuple[Permission, ...], site_id: str
) -> ToolOutput | None:
    try:
        actor_obj = resolve_actor(actor)
        if any(actor_obj.has(p) for p in perms):
            return None
        raise AccessDenied(f"Actor {actor_obj.name!r} lacks required Shop Intel permission")
    except AccessDenied:
        denied = error_payload("AccessDenied")
        denied["status"] = "denied"
        return ToolOutput(denied, site_id=site_id)


def _require_site(tools: Any, site_id: str, actor: str) -> tuple[Any, ToolOutput | None]:
    shown = tools.service.show_site(site_id, actor=actor)
    if not shown.ok:
        return None, ToolOutput(safe_failure(shown), site_id=site_id)
    site_obj = tools._site_model(site_id)
    if site_obj is None:
        return None, ToolOutput(error_payload("UnknownSiteOrDenied"), site_id=site_id)
    return site_obj, None


def _shop_intel_service(tools: Any) -> Any:
    data_dir = getattr(getattr(tools, "service", None), "data_dir", None)
    return get_shop_intel_service(data_dir=data_dir)


def _shop_intel_error(raw_err_cls: Any, default_cls: str, site_id: str) -> ToolOutput:
    cls_tok = error_class_token(raw_err_cls) or default_cls
    if cls_tok == "UnknownSite":
        cls_tok = "UnknownSiteOrDenied"
    err = error_payload(cls_tok)
    if cls_tok == "AccessDenied":
        err["status"] = "denied"
    return ToolOutput(err, site_id=site_id)


def _scrub_str(tools: Any, site_obj: Any, value: Any, *, max_len: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = redact_secret_text(value, redact_whole_lines=True)
    return tools._scrub_bounded(site_obj, cleaned, max_len=max_len)


def _scrub_obj(tools: Any, site_obj: Any, obj: Any, *, depth: int = 0) -> Any:
    if isinstance(obj, str):
        return _scrub_str(tools, site_obj, obj, max_len=140 if depth >= 4 else 280)
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if depth >= 6:
        return "[truncated]"
    if isinstance(obj, list):
        return [_scrub_obj(tools, site_obj, item, depth=depth + 1) for item in obj[:25]]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for idx, (k, v) in enumerate(obj.items()):
            if idx >= 40:
                break
            clean_k = _scrub_str(tools, site_obj, str(k), max_len=48)
            out[clean_k] = _scrub_obj(tools, site_obj, v, depth=depth + 1)
        return out
    return _scrub_str(tools, site_obj, str(obj), max_len=140)


def _compact_run_meta(tools: Any, site_obj: Any, run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return None
    return {
        "run_id": short_text(run.get("run_id"), 32),
        "site_id": short_text(run.get("site_id"), 64),
        "mode": short_text(run.get("mode"), 24),
        "provenance": short_text(run.get("provenance"), 24),
        "status": short_text(run.get("status"), 24),
        "security_score": run.get("security_score"),
        "findings_count": run.get("findings_count"),
        "diffs_count": run.get("diffs_count"),
        "domains_collected": _scrub_obj(tools, site_obj, run.get("domains_collected") or []),
        "created_at": short_text(run.get("created_at"), 40),
    }


def _run_outcome(data: dict[str, Any], _reads: int | None) -> str:
    rid = short_text(data.get("run_id"), 32)
    score = data.get("security_score")
    findings_n = data.get("findings_count")
    if rid and isinstance(score, int):
        f_txt = f", {findings_n} fynd" if isinstance(findings_n, int) else ""
        return f"Shop Intel {rid}: säkerhet {score}/100{f_txt}"
    return f"Shop Intel {rid} klar" if rid else "Shop Intel klar"


def _status_outcome(data: dict[str, Any], _reads: int | None) -> str:
    latest = data.get("latest_run")
    if isinstance(latest, dict) and latest.get("run_id"):
        rid = short_text(latest.get("run_id"), 32)
        score = latest.get("security_score")
        if isinstance(score, int):
            return f"senaste {rid} (säkerhet {score}/100)"
        return f"senaste {rid}"
    return "ingen Shop Intel-körning ännu"


def _security_outcome(data: dict[str, Any], _reads: int | None) -> str:
    score = data.get("security_score")
    findings = data.get("findings")
    diffs = data.get("diffs")
    f_n = len(findings) if isinstance(findings, list) else 0
    d_n = len(diffs) if isinstance(diffs, list) else 0
    if isinstance(score, int):
        return f"säkerhet {score}/100, {f_n} fynd, {d_n} driftändringar"
    return f"{f_n} fynd, {d_n} driftändringar"


@register_tool(
    name="wp_shop_intel_run",
    group="file",
    label="Kör Shop Intel-analys (noll-PII butiksinlärning över 7 domäner)",
    short_label="Kör Shop Intel",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "mode": {"type": "string", "enum": sorted(VALID_MODES)},
            "domains": {
                "type": "array",
                "items": {"type": "string", "enum": list(VALID_DOMAINS)},
            },
        },
        "additionalProperties": False,
    },
    outcome=_run_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="shop_intel",
)
def handle_shop_intel_run(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_READ, site_id)
    if denied is not None:
        return denied
    denied_run = _require_any_perm(
        ctx.actor,
        (Permission.RESEARCH_RUN, Permission.WP_OPS_OPERATE, Permission.WP_OPS_ADMIN),
        site_id,
    )
    if denied_run is not None:
        return denied_run
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    mode = arguments.get("mode", "full")
    if not isinstance(mode, str) or mode.strip().lower() not in VALID_MODES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    clean_mode = mode.strip().lower()

    raw_domains = arguments.get("domains")
    domains: list[str] | None = None
    if raw_domains is not None:
        if not isinstance(raw_domains, list) or not raw_domains or len(raw_domains) > len(VALID_DOMAINS):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        domains = []
        for item in raw_domains:
            if not isinstance(item, str) or item.strip().lower() not in VALID_DOMAINS_SET:
                return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
            dom = item.strip().lower()
            if dom not in domains:
                domains.append(dom)

    try:
        res = _shop_intel_service(tools).run_learning(
            site_id,
            actor=ctx.actor,
            mode=clean_mode,
            domains=domains,
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except ValueError:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    except Exception:
        return _shop_intel_error("ShopIntelRunFailed", "ShopIntelRunFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _shop_intel_error(raw_err, "ShopIntelRunFailed", site_id)

    run = res.get("run") if isinstance(res.get("run"), dict) else {}
    run_id = short_text(run.get("run_id"), 32)
    findings = res.get("findings") if isinstance(res.get("findings"), list) else []
    diffs = res.get("diffs") if isinstance(res.get("diffs"), list) else []

    return ToolOutput(
        {
            "ok": True,
            "status": short_text(run.get("status"), 24) or "completed",
            "site_id": site_id,
            "run_id": run_id,
            "mode": clean_mode,
            "provenance": short_text(run.get("provenance"), 24) or "mock",
            "security_score": res.get("security_score", run.get("security_score")),
            "facts_count": int(res.get("facts_count") or 0),
            "findings_count": len(findings),
            "diffs_count": len(diffs),
            "domains_collected": _scrub_obj(tools, site_obj, run.get("domains_collected") or []),
            "findings": _scrub_obj(tools, site_obj, findings[:12]),
            "diffs": _scrub_obj(tools, site_obj, diffs[:10]),
            "warnings": _scrub_obj(tools, site_obj, run.get("warnings") or []),
            "dashboard_path": f"/shop-intel/runs/{run_id}" if run_id else f"/shop-intel?site_id={site_id}",
            "live_calls": bool(res.get("live_calls")),
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_shop_intel_status",
    group="drift",
    label="Visa Shop Intel-status och senaste körning",
    short_label="Shop Intel-status",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
        },
        "additionalProperties": False,
    },
    outcome=_status_outcome,
    in_wp_schemas=True,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="shop_intel",
)
def handle_shop_intel_status(
    tools: Any, ctx: ToolContext, _arguments: dict[str, Any], **_kw: Any
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_any_perm(ctx.actor, (Permission.WP_OPS_READ, Permission.READ), site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    try:
        res = _shop_intel_service(tools).get_status(site_id, actor=ctx.actor)
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _shop_intel_error("ShopIntelStatusFailed", "ShopIntelStatusFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _shop_intel_error(raw_err, "ShopIntelStatusFailed", site_id)

    latest = _compact_run_meta(tools, site_obj, res.get("latest_run"))
    recent = [
        meta
        for r in (res.get("recent_runs") or [])[:5]
        if (meta := _compact_run_meta(tools, site_obj, r)) is not None
    ]
    return ToolOutput(
        {
            "ok": True,
            "status": "ok" if latest else "no_snapshot",
            "site_id": site_id,
            "gates": _scrub_obj(tools, site_obj, res.get("gates") or {}),
            "latest_run": latest,
            "schedule": _scrub_obj(tools, site_obj, res.get("schedule")),
            "recent_runs": recent,
            "dashboard_path": f"/shop-intel?site_id={site_id}",
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_shop_intel_query",
    group="drift",
    label="Fråga inlärd butikskunskap (Shop Intel)",
    short_label="Shop Intel-kunskap",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "domain": {"type": "string", "enum": list(VALID_DOMAINS)},
            "query": {"type": "string"},
            "purpose": {"type": "string", "enum": sorted(VALID_PURPOSES)},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 4000},
        },
        "additionalProperties": False,
    },
    outcome=count_outcome("facts", "fakta", "butikskunskap hämtad"),
    in_wp_schemas=True,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="shop_intel",
)
def handle_shop_intel_query(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any], **_kw: Any
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_any_perm(ctx.actor, (Permission.WP_OPS_READ, Permission.READ), site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    domain = arguments.get("domain")
    if domain is not None:
        if not isinstance(domain, str) or domain.strip().lower() not in VALID_DOMAINS_SET:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        domain = domain.strip().lower()

    query = arguments.get("query")
    if query is not None:
        if not isinstance(query, str) or len(query) > 200:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        query = query.strip()

    purpose = arguments.get("purpose")
    if purpose is not None:
        if not isinstance(purpose, str) or purpose.strip().lower() not in VALID_PURPOSES:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        purpose = purpose.strip().lower()

    max_chars = arguments.get("max_chars")
    if max_chars is not None:
        if type(max_chars) is not int or not (200 <= max_chars <= 4000):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    try:
        res = _shop_intel_service(tools).query_knowledge(
            site_id,
            domain=domain,
            query=query,
            purpose=purpose,
            max_chars=max_chars,
            actor=ctx.actor,
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except ValueError:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    except Exception:
        return _shop_intel_error("ShopIntelQueryFailed", "ShopIntelQueryFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _shop_intel_error(raw_err, "ShopIntelQueryFailed", site_id)

    facts = res.get("facts") if isinstance(res.get("facts"), list) else []
    payload: dict[str, Any] = {
        "ok": True,
        "status": "ok",
        "site_id": site_id,
        "run_id": short_text(res.get("run_id"), 32),
        "provenance": short_text(res.get("provenance"), 24) or "mock",
        "observed_at": short_text(res.get("observed_at"), 40),
        "facts": _scrub_obj(tools, site_obj, facts[:25]),
        "dossier": _scrub_obj(tools, site_obj, res.get("dossier") or {}),
        "live_calls": False,
    }
    if res.get("context"):
        payload["context"] = _scrub_str(tools, site_obj, str(res["context"]), max_len=3500)
    return ToolOutput(payload, site_id=site_id)


@register_tool(
    name="wp_shop_intel_security_audit",
    group="drift",
    label="Granska säkerhetsfynd och driftvarningar (Shop Intel)",
    short_label="Shop Intel-säkerhet",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "severity": {"type": "string", "enum": list(VALID_SEVERITIES)},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    },
    outcome=_security_outcome,
    in_wp_schemas=True,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="shop_intel",
)
def handle_shop_intel_security_audit(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any], **_kw: Any
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_any_perm(ctx.actor, (Permission.WP_OPS_READ, Permission.READ), site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    severity = arguments.get("severity")
    if severity is not None:
        if not isinstance(severity, str) or severity.strip().lower() not in _VALID_SEVERITIES_SET:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        severity = severity.strip().lower()

    limit = arguments.get("limit", 25)
    if type(limit) is not int or not (1 <= limit <= 50):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    svc = _shop_intel_service(tools)
    try:
        sec_res = svc.get_security_report(
            site_id, severity=severity, limit=limit, actor=ctx.actor
        )
        diff_res = svc.get_diffs(site_id, limit=min(limit, 20), actor=ctx.actor)
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except ValueError:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    except Exception:
        return _shop_intel_error("ShopIntelSecurityFailed", "ShopIntelSecurityFailed", site_id)

    if not isinstance(sec_res, dict) or sec_res.get("ok") is not True:
        raw_err = sec_res.get("error_class") if isinstance(sec_res, dict) else None
        return _shop_intel_error(raw_err, "ShopIntelSecurityFailed", site_id)

    run_id = short_text(sec_res.get("run_id"), 32)
    findings = sec_res.get("findings") if isinstance(sec_res.get("findings"), list) else []
    diffs = diff_res.get("diffs") if isinstance(diff_res, dict) and isinstance(diff_res.get("diffs"), list) else []

    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "site_id": site_id,
            "run_id": run_id,
            "security_score": sec_res.get("security_score", 100),
            "posture": _scrub_obj(tools, site_obj, sec_res.get("posture") or {}),
            "findings": _scrub_obj(tools, site_obj, findings[:limit]),
            "diffs": _scrub_obj(tools, site_obj, diffs[:20]),
            "dashboard_path": f"/shop-intel/runs/{run_id}" if run_id else f"/shop-intel?site_id={site_id}",
            "live_calls": False,
        },
        site_id=site_id,
    )


__all__ = [
    "handle_shop_intel_query",
    "handle_shop_intel_run",
    "handle_shop_intel_security_audit",
    "handle_shop_intel_status",
]
