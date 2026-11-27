"""WordPress SEO Studio agent tools (audit, meta/batch plans, drift digest, strategy, campaign, link packages).

Delegates all business logic and persistence to ``ecom_ops.seo.service.SeoStudioService``
and ``ecom_ops.seo.digest``. Zero direct network, socket, or shell calls; never executes
site writes or external link orders.
"""

from __future__ import annotations

import inspect
import math
import re
from typing import Any

from ecom_ops.rbac import AccessDenied, Permission, require_permission, resolve_actor
from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.tools.registry import plan_id_outcome, register_tool
from ecom_ops.wp_ops.tools.types import (
    RESEARCH_ID_RE,
    SITE_ID_RE,
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import (
    error_class_token,
    error_payload,
    safe_failure,
    short_text,
)

_SEO_SCOPES = frozenset(
    {
        "site",
        "technical",
        "page",
        "product",
        "media",
        "links",
        "cannibalization",
        "gsc",
        "parity",
        "deep_crawl",
    }
)
_SEO_FIX_TYPES = frozenset({"seo_meta", "media_alt", "product_seo", "page_seo", "schema_jsonld"})
_SEO_TARGET_TYPES = frozenset({"product", "page", "post", "media"})
_SEO_MARKETS = frozenset({"se", "no", "dk"})
_SEO_BATCH_SCOPES = frozenset({"all", "media_alt", "seo_meta"})
_SEO_LINK_MODES = frozenset({"create", "verify", "cumulative", "vendors"})
_SEO_GOAL_RE = re.compile(r"^[a-z0-9_-]{1,48}$")
_SEO_AUDIT_ID_RE = re.compile(r"^seoa-[a-f0-9]{12}$")
_SEO_STRATEGY_ID_RE = re.compile(r"^seos-[a-f0-9]{12}$")
_SEO_CAMPAIGN_ID_RE = re.compile(r"^seoc-[a-f0-9]{12}$")
_SEO_LINK_PKG_ID_RE = re.compile(r"^seol-[a-f0-9]{12}$")
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,120}$")


def _require_perm(actor: str, perm: Permission, site_id: str) -> ToolOutput | None:
    try:
        actor_obj = resolve_actor(actor)
        require_permission(actor_obj, perm)
    except AccessDenied:
        denied = error_payload("AccessDenied")
        denied["status"] = "denied"
        return ToolOutput(denied, site_id=site_id)
    return None


def _require_site(tools: Any, site_id: str, actor: str) -> tuple[Any, ToolOutput | None]:
    shown = tools.service.show_site(site_id, actor=actor)
    if not shown.ok:
        return None, ToolOutput(safe_failure(shown), site_id=site_id)
    site_obj = tools._site_model(site_id)
    if site_obj is None:
        return None, ToolOutput(error_payload("UnknownSiteOrDenied"), site_id=site_id)
    return site_obj, None


def _seo_error(raw_err_cls: Any, default_cls: str, site_id: str) -> ToolOutput:
    cls_tok = error_class_token(raw_err_cls) or default_cls
    if cls_tok == "UnknownSite":
        cls_tok = "UnknownSiteOrDenied"
    err = error_payload(cls_tok)
    if cls_tok == "AccessDenied":
        err["status"] = "denied"
    return ToolOutput(err, site_id=site_id)


def _seo_service(tools: Any) -> Any:
    from ecom_ops.seo.service import SeoStudioService

    data_dir = getattr(getattr(tools, "service", None), "data_dir", None)
    return SeoStudioService(data_dir=data_dir)


def _infer_market(site_id: str, raw_market: Any) -> str | None:
    if raw_market is not None:
        if not isinstance(raw_market, str):
            return None
        norm = raw_market.strip().lower()
        return norm if norm in _SEO_MARKETS else None
    low = site_id.strip().lower()
    if low.endswith(("-se", "_se", ".se")):
        return "se"
    if low.endswith(("-dk", "_dk", ".dk")):
        return "dk"
    return "no"


def _scrub_str(tools: Any, site_obj: Any, value: Any, *, max_len: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = redact_secret_text(value, redact_whole_lines=True)
    return tools._scrub_bounded(site_obj, cleaned, max_len=max_len)


def _scrub_obj(tools: Any, site_obj: Any, obj: Any, *, depth: int = 0) -> Any:
    if isinstance(obj, str):
        return _scrub_str(tools, site_obj, obj, max_len=120 if depth >= 4 else 240)
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
    return _scrub_str(tools, site_obj, str(obj), max_len=120)


def _audit_outcome(data: dict[str, Any], _reads: int | None) -> str:
    scope = short_text(data.get("scope"), 24)
    aid = short_text(data.get("audit_id"), 32)
    score = data.get("score")
    grade = short_text(data.get("grade"), 8)
    issues = data.get("issues")
    issue_n = len(issues) if isinstance(issues, list) else 0
    if isinstance(score, int):
        tag = f" {aid}" if aid else ""
        grade_part = f" ({grade})" if grade else ""
        return f"SEO-audit{tag}: {score}/100{grade_part}, {issue_n} fynd"
    if scope == "gsc":
        return "GSC quick-wins klar"
    if scope == "parity":
        return f"Nordisk SEO-paritet klar ({issue_n} fynd)" if issue_n else "Nordisk SEO-paritet klar"
    if scope == "deep_crawl":
        return f"SEO deep-crawl klar ({issue_n} fynd)" if issue_n else "SEO deep-crawl klar"
    return f"SEO-audit {aid} klar" if aid else "SEO-audit klar"


def _batch_plan_outcome(data: dict[str, Any], _reads: int | None) -> str:
    bid = short_text(data.get("batch_id"), 32)
    return f"SEO-batch {bid} skapad" if bid else "SEO-batch skapad"


def _drift_digest_outcome(data: dict[str, Any], _reads: int | None) -> str:
    days = data.get("days")
    if isinstance(days, int) and days > 0:
        return f"SEO+plugin-digest ({days}d) klar"
    return "SEO+plugin-digest klar"


def _strategy_outcome(data: dict[str, Any], _reads: int | None) -> str:
    sid = short_text(data.get("strategy_id"), 32)
    return f"SEO-strategi {sid} skapad" if sid else "SEO-strategi skapad"


def _campaign_outcome(data: dict[str, Any], _reads: int | None) -> str:
    cid = short_text(data.get("campaign_id"), 32)
    return f"SEO-kampanj {cid} skapad" if cid else "SEO-kampanj skapad"


def _link_pkg_outcome(data: dict[str, Any], _reads: int | None) -> str:
    mode = short_text(data.get("mode"), 20)
    pid = short_text(data.get("package_id"), 32)
    if mode == "verify":
        return f"länkpaket {pid} verifierat" if pid else "länkpaket verifierat"
    if mode == "cumulative":
        return "kumulativ ankartextmix beräknad"
    if mode == "vendors":
        cands = data.get("candidates")
        if isinstance(cands, list):
            return f"{len(cands)} SEO-butiker granskade"
        return "SEO-butiker granskade"
    return f"länkpaket {pid} skapat" if pid else "länkpaket skapat"


def _autopilot_outcome(data: dict[str, Any], _reads: int | None) -> str:
    pkg_id = short_text(data.get("package_id"), 32)
    vendor = short_text(data.get("selected_vendor_id"), 32)
    if pkg_id and vendor:
        return f"SEO-autopilot klar ({pkg_id} · {vendor})"
    if pkg_id:
        return f"SEO-autopilot klar ({pkg_id})"
    return "SEO-autopilot klar"


@register_tool(
    name="wp_seo_audit",
    group="file",
    label="Kör läsande SEO-audit (teknik, on-page, media, länkar, GSC, paritet, deep crawl)",
    short_label="SEO-audit",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "scope": {
                "type": "string",
                "enum": [
                    "site",
                    "technical",
                    "page",
                    "product",
                    "media",
                    "links",
                    "cannibalization",
                    "gsc",
                    "parity",
                    "deep_crawl",
                ],
            },
            "target_id": {"type": ["string", "integer"]},
            "path": {"type": "string"},
            "market": {"type": "string", "enum": ["se", "no", "dk"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "include_diff": {"type": "boolean"},
            "days": {"type": "integer", "minimum": 1, "maximum": 90},
            "max_pages": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    },
    outcome=_audit_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=False,
    untrusted_output=True,
    capability_group="seo",
)
def handle_seo_audit(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_READ, site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    scope = arguments.get("scope", "site")
    if not isinstance(scope, str) or scope not in _SEO_SCOPES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    market = _infer_market(site_id, arguments.get("market"))
    if market is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_limit = arguments.get("limit", 50)
    if type(raw_limit) is not int or not (1 <= raw_limit <= 100):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    include_diff = arguments.get("include_diff", True)
    if not isinstance(include_diff, bool):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_days = arguments.get("days", 28)
    if type(raw_days) is not int or not (1 <= raw_days <= 90):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_max_pages = arguments.get("max_pages", 10)
    if type(raw_max_pages) is not int or not (1 <= raw_max_pages <= 50):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_path = arguments.get("path", "/")
    if not isinstance(raw_path, str) or not raw_path.strip().startswith("/") or len(raw_path.strip()) > 200:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    path = raw_path.strip()

    raw_target = arguments.get("target_id")
    target_id: str | None = None
    if raw_target is not None:
        if isinstance(raw_target, bool):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        target_str = str(raw_target).strip()
        if not target_str or not _TARGET_ID_RE.fullmatch(target_str):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        target_id = target_str

    svc = _seo_service(tools)

    if scope == "gsc":
        try:
            res = svc.get_gsc_quick_wins(site_id, days=raw_days, actor=ctx.actor)
        except AccessDenied:
            denied_out = error_payload("AccessDenied")
            denied_out["status"] = "denied"
            return ToolOutput(denied_out, site_id=site_id)
        except Exception:
            return _seo_error("SeoGscFailed", "SeoGscFailed", site_id)
        if not isinstance(res, dict) or res.get("ok") is not True:
            raw_err = res.get("error_class") if isinstance(res, dict) else None
            return _seo_error(raw_err, "SeoGscFailed", site_id)
        return ToolOutput(
            {
                "ok": True,
                "status": "ok",
                "site_id": site_id,
                "market": market,
                "scope": "gsc",
                "days": raw_days,
                "gsc_status": _scrub_obj(tools, site_obj, res.get("gsc_status") or {}),
                "strike_distance": _scrub_obj(tools, site_obj, res.get("strike_distance") or []),
                "low_ctr": _scrub_obj(tools, site_obj, res.get("low_ctr") or []),
                "cannibalization": _scrub_obj(tools, site_obj, res.get("cannibalization") or []),
                "summary": _scrub_obj(tools, site_obj, res.get("summary") or {}),
                "dashboard_path": f"/seo?site_id={site_id}",
                "live_calls": bool(res.get("live_calls")),
            },
            site_id=site_id,
        )

    if scope == "parity":
        try:
            res = svc.run_nordic_parity(site_id, actor=ctx.actor)
        except AccessDenied:
            denied_out = error_payload("AccessDenied")
            denied_out["status"] = "denied"
            return ToolOutput(denied_out, site_id=site_id)
        except Exception:
            return _seo_error("SeoParityFailed", "SeoParityFailed", site_id)
        if not isinstance(res, dict) or res.get("ok") is not True:
            raw_err = res.get("error_class") if isinstance(res, dict) else None
            return _seo_error(raw_err, "SeoParityFailed", site_id)
        issues_raw = res.get("issues") if isinstance(res.get("issues"), list) else []
        return ToolOutput(
            {
                "ok": True,
                "status": "ok",
                "site_id": site_id,
                "market": market,
                "scope": "parity",
                "markets": _scrub_obj(tools, site_obj, res.get("markets") or []),
                "rows": _scrub_obj(tools, site_obj, res.get("rows") or []),
                "issues": [_scrub_obj(tools, site_obj, row) for row in issues_raw[:25]],
                "issues_total": len(issues_raw),
                "summary": _scrub_obj(tools, site_obj, res.get("summary") or {}),
                "dashboard_path": f"/seo/parity?site_id={site_id}",
                "live_calls": bool(res.get("live_calls")),
            },
            site_id=site_id,
        )

    if scope == "deep_crawl":
        try:
            res = svc.run_deep_crawl(site_id, max_pages=raw_max_pages, actor=ctx.actor)
        except AccessDenied:
            denied_out = error_payload("AccessDenied")
            denied_out["status"] = "denied"
            return ToolOutput(denied_out, site_id=site_id)
        except Exception:
            return _seo_error("SeoDeepCrawlFailed", "SeoDeepCrawlFailed", site_id)
        if not isinstance(res, dict) or res.get("ok") is not True:
            raw_err = res.get("error_class") if isinstance(res, dict) else None
            return _seo_error(raw_err, "SeoDeepCrawlFailed", site_id)
        issues_raw = res.get("issues") if isinstance(res.get("issues"), list) else []
        return ToolOutput(
            {
                "ok": True,
                "status": "ok",
                "site_id": site_id,
                "market": market,
                "scope": "deep_crawl",
                "sitemap_urls": _scrub_obj(tools, site_obj, res.get("sitemap_urls") or []),
                "crawled": _scrub_obj(tools, site_obj, res.get("crawled") or []),
                "issues": [_scrub_obj(tools, site_obj, row) for row in issues_raw[:25]],
                "issues_total": len(issues_raw),
                "summary": _scrub_obj(tools, site_obj, res.get("summary") or {}),
                "dashboard_path": f"/seo?site_id={site_id}",
                "live_calls": bool(res.get("live_calls")),
            },
            site_id=site_id,
        )

    try:
        res = svc.run_audit(
            site_id=site_id,
            actor=ctx.actor,
            scope=scope,
            target_id=target_id,
            path=path,
            market=market,
            limit=raw_limit,
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _seo_error("SeoAuditFailed", "SeoAuditFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _seo_error(raw_err, "SeoAuditFailed", site_id)

    audit_id = short_text(res.get("audit_id"), 32)
    issues_raw = res.get("issues") if isinstance(res.get("issues"), list) else []
    cannib_raw = res.get("cannibalization") if isinstance(res.get("cannibalization"), list) else []
    data_sources = [
        short_text(s, 48) for s in (res.get("data_sources") or [])[:12] if isinstance(s, str)
    ]

    diff_payload: dict[str, Any] | None = None
    if include_diff and hasattr(svc, "diff_audits"):
        try:
            diff_res = svc.diff_audits(
                site_id,
                latest_audit_id=audit_id or None,
                actor=ctx.actor,
            )
            if isinstance(diff_res, dict) and diff_res.get("ok") is True:
                diff_payload = {
                    "has_previous": bool(diff_res.get("has_previous")),
                    "latest_id": short_text(diff_res.get("latest_id"), 32),
                    "previous_id": short_text(diff_res.get("previous_id"), 32) if diff_res.get("previous_id") else None,
                    "score_delta": int(diff_res.get("score_delta") or 0),
                    "new_issues": _scrub_obj(tools, site_obj, diff_res.get("new_issues") or []),
                    "resolved_issues": _scrub_obj(tools, site_obj, diff_res.get("resolved_issues") or []),
                    "summary": _scrub_obj(tools, site_obj, diff_res.get("summary") or {}),
                }
        except Exception:
            diff_payload = None

    payload: dict[str, Any] = {
        "ok": True,
        "status": "ok",
        "site_id": site_id,
        "audit_id": audit_id,
        "market": short_text(res.get("market") or market, 8),
        "scope": short_text(res.get("scope") or scope, 24),
        "score": int(res.get("score") or 0),
        "grade": short_text(res.get("grade"), 8),
        "summary": _scrub_obj(tools, site_obj, res.get("summary") or {}),
        "issues": [_scrub_obj(tools, site_obj, row) for row in issues_raw[:25]],
        "issues_total": len(issues_raw),
        "cannibalization": [_scrub_obj(tools, site_obj, row) for row in cannib_raw[:10]],
        "data_sources": data_sources,
        "gsc_note": _scrub_str(tools, site_obj, str(res.get("gsc_note") or ""), max_len=200),
        "dashboard_path": f"/seo/audit/{audit_id}" if audit_id else "/seo",
        "live_calls": bool(res.get("live_calls")),
    }
    if diff_payload is not None:
        payload["diff"] = diff_payload

    return ToolOutput(payload, site_id=site_id)


@register_tool(
    name="wp_seo_plan_meta",
    group="file",
    label="Skapa låst SEO-ändringsplan för meta/alt/schema (förhandsvisning)",
    short_label="SEO-meta-plan",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "fix_type": {
                "type": "string",
                "enum": ["seo_meta", "media_alt", "product_seo", "page_seo", "schema_jsonld"],
            },
            "target_type": {
                "type": "string",
                "enum": ["product", "page", "post", "media"],
            },
            "target_id": {"type": ["string", "integer"]},
            "proposed": {"type": "object"},
            "summary": {"type": "string"},
            "market": {"type": "string", "enum": ["se", "no", "dk"]},
        },
        "required": ["fix_type", "target_type", "target_id", "proposed"],
        "additionalProperties": False,
    },
    outcome=plan_id_outcome("SEO-plan {pid} skapad", "SEO-plan skapad", nested_plan=True),
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="seo",
)
def handle_seo_plan_meta(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_OPERATE, site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err
    if site_obj.environment == "production":
        denied = _require_perm(ctx.actor, Permission.WP_OPS_ADMIN, site_id)
        if denied is not None:
            return denied

    fix_type = arguments.get("fix_type")
    target_type = arguments.get("target_type")
    raw_target_id = arguments.get("target_id")
    proposed = arguments.get("proposed")
    summary = arguments.get("summary", "")
    market = _infer_market(site_id, arguments.get("market"))

    if not isinstance(fix_type, str) or fix_type not in _SEO_FIX_TYPES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if not isinstance(target_type, str) or target_type not in _SEO_TARGET_TYPES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if raw_target_id is None or isinstance(raw_target_id, bool):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    target_str = str(raw_target_id).strip()
    if not target_str or not _TARGET_ID_RE.fullmatch(target_str):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if not isinstance(proposed, dict) or not proposed or len(proposed) > 25:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if not isinstance(summary, str) or len(summary) > 300:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    if market is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    try:
        res = _seo_service(tools).propose_fix(
            site_id=site_id,
            actor=ctx.actor,
            fix_type=fix_type,
            target_type=target_type,
            target_id=target_str,
            proposed=dict(proposed),
            summary=summary.strip(),
            market=market,
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _seo_error("SeoPlanFailed", "SeoPlanFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _seo_error(raw_err, "SeoPlanFailed", site_id)

    raw_plan = dict(res.get("plan") or {})
    plan_id = short_text(raw_plan.get("plan_id") or res.get("plan_id"), 32)
    plan_sha256 = short_text(raw_plan.get("plan_sha256"), 64)
    before_sha256 = short_text(raw_plan.get("before_sha256"), 64)
    safe_plan = _scrub_obj(tools, site_obj, raw_plan)
    if isinstance(safe_plan, dict):
        safe_plan["executed"] = False
        safe_plan["approval_required"] = True

    return ToolOutput(
        {
            "ok": True,
            "status": short_text(raw_plan.get("status"), 24) or "pending",
            "site_id": site_id,
            "plan_id": plan_id,
            "fix_type": fix_type,
            "target_type": target_type,
            "target_id": target_str,
            "before_sha256": before_sha256,
            "plan_sha256": plan_sha256,
            "executed": False,
            "approval_required": True,
            "plan": safe_plan,
            "dashboard_path": "/seo",
            "login_required": True,
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_seo_batch_plan",
    group="file",
    label="Skapa låst SEO-batchplan (seob-*) från audit (förhandsvisning)",
    short_label="SEO-batchplan",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "audit_id": {"type": "string", "pattern": _SEO_AUDIT_ID_RE.pattern},
            "scope": {"type": "string", "enum": ["all", "media_alt", "seo_meta"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["audit_id"],
        "additionalProperties": False,
    },
    outcome=_batch_plan_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="seo",
)
def handle_seo_batch_plan(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_OPERATE, site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err
    if site_obj.environment == "production":
        denied = _require_perm(ctx.actor, Permission.WP_OPS_ADMIN, site_id)
        if denied is not None:
            return denied

    audit_id = arguments.get("audit_id")
    if not isinstance(audit_id, str) or not _SEO_AUDIT_ID_RE.fullmatch(audit_id.strip()):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    audit_id = audit_id.strip()

    scope = arguments.get("scope", "all")
    if not isinstance(scope, str) or scope not in _SEO_BATCH_SCOPES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_limit = arguments.get("limit", 20)
    if type(raw_limit) is not int or not (1 <= raw_limit <= 50):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    try:
        res = _seo_service(tools).propose_batch_from_audit(
            site_id,
            audit_id,
            scope=scope,
            limit=raw_limit,
            actor=ctx.actor,
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _seo_error("SeoBatchPlanFailed", "SeoBatchPlanFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _seo_error(raw_err, "SeoBatchPlanFailed", site_id)

    raw_batch = dict(res.get("batch") or {})
    batch_id = short_text(raw_batch.get("batch_id") or res.get("batch_id"), 32)
    plan_sha256 = short_text(raw_batch.get("plan_sha256") or raw_batch.get("batch_sha256"), 64)
    items_raw = raw_batch.get("items") if isinstance(raw_batch.get("items"), list) else []
    safe_batch = _scrub_obj(tools, site_obj, raw_batch)
    if isinstance(safe_batch, dict):
        safe_batch["executed"] = False
        safe_batch["approval_required"] = True

    return ToolOutput(
        {
            "ok": True,
            "status": short_text(raw_batch.get("status"), 24) or "pending",
            "site_id": site_id,
            "audit_id": audit_id,
            "batch_id": batch_id,
            "scope": scope,
            "plan_sha256": plan_sha256,
            "item_count": len(items_raw),
            "executed": False,
            "approval_required": True,
            "batch": safe_batch,
            "dashboard_path": f"/seo?site_id={site_id}&batch_id={batch_id}" if batch_id else f"/seo?site_id={site_id}",
            "changes_path": f"/wp/{site_id}/changes?batch_id={batch_id}" if batch_id else f"/wp/{site_id}/changes",
            "login_required": True,
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_seo_drift_digest",
    group="file",
    label="Hämta veckovis SEO- och plugin-driftöversikt (läsande)",
    short_label="SEO-driftdigest",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "days": {"type": "integer", "minimum": 1, "maximum": 90},
        },
        "additionalProperties": False,
    },
    outcome=_drift_digest_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="seo",
)
def handle_seo_drift_digest(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_READ, site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    raw_days = arguments.get("days", 7)
    if type(raw_days) is not int or not (1 <= raw_days <= 90):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    try:
        from ecom_ops.seo.digest import build_seo_plugin_drift_digest

        kwargs: dict[str, Any] = {"actor": ctx.actor, "days": raw_days}
        data_dir = getattr(getattr(tools, "service", None), "data_dir", None)
        if data_dir is not None and "data_dir" in inspect.signature(build_seo_plugin_drift_digest).parameters:
            kwargs["data_dir"] = data_dir
        res = build_seo_plugin_drift_digest(site_id, **kwargs)
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _seo_error("SeoDigestFailed", "SeoDigestFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _seo_error(raw_err, "SeoDigestFailed", site_id)

    return ToolOutput(
        {
            "ok": True,
            "status": "ok",
            "site_id": site_id,
            "days": raw_days,
            "seo_trend": _scrub_obj(tools, site_obj, res.get("seo_trend") or {}),
            "gsc_summary": _scrub_obj(tools, site_obj, res.get("gsc_summary") or {}),
            "parity_summary": _scrub_obj(tools, site_obj, res.get("parity_summary") or {}),
            "plugin_summary": _scrub_obj(tools, site_obj, res.get("plugin_summary") or {}),
            "markdown": _scrub_str(tools, site_obj, str(res.get("markdown") or ""), max_len=3500),
            "dashboard_links": _scrub_obj(tools, site_obj, res.get("dashboard_links") or {}),
            "dashboard_path": f"/seo/digest?site_id={site_id}",
            "live_calls": bool(res.get("live_calls")),
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_seo_strategy",
    group="file",
    label="Skapa nordisk SEO-strategi (kluster & roadmap)",
    short_label="SEO-strategi",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "title": {"type": "string"},
            "market": {"type": "string", "enum": ["se", "no", "dk"]},
            "focus_topics": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            "goals": {"type": "string"},
            "research_brief_id": {"type": "string", "pattern": RESEARCH_ID_RE.pattern},
        },
        "required": ["title"],
        "additionalProperties": False,
    },
    outcome=_strategy_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="seo",
)
def handle_seo_strategy(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_READ, site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    title = arguments.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    market = _infer_market(site_id, arguments.get("market"))
    if market is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_topics = arguments.get("focus_topics")
    focus_topics: list[str] | None = None
    if raw_topics is not None:
        if (
            not isinstance(raw_topics, list)
            or len(raw_topics) > 12
            or any(not isinstance(t, str) or not t.strip() or len(t) > 120 for t in raw_topics)
        ):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        focus_topics = [t.strip() for t in raw_topics]

    goals = arguments.get("goals", "")
    if not isinstance(goals, str) or len(goals) > 600:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    rid = arguments.get("research_brief_id")
    if rid is not None and (not isinstance(rid, str) or not RESEARCH_ID_RE.fullmatch(rid.strip())):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    try:
        res = _seo_service(tools).create_strategy(
            site_id=site_id,
            actor=ctx.actor,
            title=title.strip(),
            market=market,
            focus_topics=focus_topics,
            goals=goals.strip(),
            research_brief_id=rid.strip() if isinstance(rid, str) else None,
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _seo_error("SeoStrategyFailed", "SeoStrategyFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _seo_error(raw_err, "SeoStrategyFailed", site_id)

    strat = dict(res.get("strategy") or {})
    strategy_id = short_text(strat.get("strategy_id") or res.get("strategy_id"), 32)
    return ToolOutput(
        {
            "ok": True,
            "status": "created",
            "site_id": site_id,
            "strategy_id": strategy_id,
            "market": market,
            "strategy": _scrub_obj(tools, site_obj, strat),
            "dashboard_path": "/seo/strategies",
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_seo_campaign",
    group="file",
    label="Skapa SEO-kampanjplan (sökord, URL:er & åtgärder)",
    short_label="SEO-kampanj",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "name": {"type": "string"},
            "market": {"type": "string", "enum": ["se", "no", "dk"]},
            "strategy_id": {"type": "string", "pattern": _SEO_STRATEGY_ID_RE.pattern},
            "target_keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 25},
            "target_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "campaign_type": {"type": "string"},
            "objective": {"type": "string"},
            "research_brief_id": {"type": "string", "pattern": RESEARCH_ID_RE.pattern},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    outcome=_campaign_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="seo",
)
def handle_seo_campaign(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_READ, site_id)
    if denied is not None:
        return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    name = arguments.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    market = _infer_market(site_id, arguments.get("market"))
    if market is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    strategy_id = arguments.get("strategy_id")
    if strategy_id is not None and (
        not isinstance(strategy_id, str) or not _SEO_STRATEGY_ID_RE.fullmatch(strategy_id.strip())
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_kws = arguments.get("target_keywords")
    target_keywords: list[str] | None = None
    if raw_kws is not None:
        if (
            not isinstance(raw_kws, list)
            or len(raw_kws) > 25
            or any(not isinstance(k, str) or not k.strip() or len(k) > 120 for k in raw_kws)
        ):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        target_keywords = [k.strip() for k in raw_kws]

    raw_urls = arguments.get("target_urls")
    target_urls: list[str] | None = None
    if raw_urls is not None:
        if (
            not isinstance(raw_urls, list)
            or len(raw_urls) > 20
            or any(not isinstance(u, str) or not u.strip() or len(u) > 240 for u in raw_urls)
        ):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        target_urls = [u.strip() for u in raw_urls]

    campaign_type = arguments.get("campaign_type", "commercial")
    if not isinstance(campaign_type, str) or not campaign_type.strip() or len(campaign_type) > 48:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    objective = arguments.get("objective", "")
    if not isinstance(objective, str) or len(objective) > 600:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    rid = arguments.get("research_brief_id")
    if rid is not None and (not isinstance(rid, str) or not RESEARCH_ID_RE.fullmatch(rid.strip())):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    try:
        res = _seo_service(tools).create_campaign(
            site_id=site_id,
            actor=ctx.actor,
            name=name.strip(),
            market=market,
            strategy_id=strategy_id.strip() if isinstance(strategy_id, str) else None,
            target_keywords=target_keywords,
            target_urls=target_urls,
            campaign_type=campaign_type.strip(),
            objective=objective.strip(),
            research_brief_id=rid.strip() if isinstance(rid, str) else None,
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _seo_error("SeoCampaignFailed", "SeoCampaignFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _seo_error(raw_err, "SeoCampaignFailed", site_id)

    camp = dict(res.get("campaign") or {})
    campaign_id = short_text(camp.get("campaign_id") or res.get("campaign_id"), 32)
    return ToolOutput(
        {
            "ok": True,
            "status": "created",
            "site_id": site_id,
            "campaign_id": campaign_id,
            "market": market,
            "campaign": _scrub_obj(tools, site_obj, camp),
            "dashboard_path": "/seo/campaigns",
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_seo_link_package",
    group="file",
    label="Skapa, verifiera, granska SEO-butiker eller kontrollera kumulativ ankartextmix för länkpaket (köper aldrig)",
    short_label="Länkpaket",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "mode": {"type": "string", "enum": ["create", "verify", "cumulative", "vendors"]},
            "package_id": {"type": "string", "pattern": _SEO_LINK_PKG_ID_RE.pattern},
            "name": {"type": "string"},
            "market": {"type": "string", "enum": ["se", "no", "dk"]},
            "campaign_id": {"type": "string", "pattern": _SEO_CAMPAIGN_ID_RE.pattern},
            "budget_sek": {"type": "number", "minimum": 1000, "maximum": 500000},
            "link_count": {"type": "integer", "minimum": 1, "maximum": 50},
            "goal": {"type": "string"},
            "include_web_research": {"type": "boolean"},
            "targets": {
                "type": "array",
                "maxItems": 25,
                "items": {"type": "object"},
            },
            "notes": {"type": "string"},
        },
        "additionalProperties": False,
    },
    outcome=_link_pkg_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="seo",
)
def handle_seo_link_package(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    mode = arguments.get("mode", "create")
    if not isinstance(mode, str) or mode not in _SEO_LINK_MODES:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    req_perms = (
        (Permission.WP_OPS_READ, Permission.MARKETING_SUGGEST)
        if mode == "create"
        else (Permission.WP_OPS_READ,)
    )
    for perm in req_perms:
        denied = _require_perm(ctx.actor, perm, site_id)
        if denied is not None:
            return denied
    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    raw_pkg_id = arguments.get("package_id")
    pkg_id: str | None = None
    if raw_pkg_id is not None:
        if not isinstance(raw_pkg_id, str) or not _SEO_LINK_PKG_ID_RE.fullmatch(raw_pkg_id.strip()):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        pkg_id = raw_pkg_id.strip()

    if mode == "vendors":
        market = _infer_market(site_id, arguments.get("market"))
        if market is None:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        raw_budget = arguments.get("budget_sek", 15000.0)
        if (
            isinstance(raw_budget, bool)
            or not isinstance(raw_budget, (int, float))
            or not math.isfinite(float(raw_budget))
            or not (1000 <= float(raw_budget) <= 500000)
        ):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        raw_count = arguments.get("link_count", 6)
        if type(raw_count) is not int or not (1 <= raw_count <= 50):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        raw_goal = arguments.get("goal", "organic_growth")
        if not isinstance(raw_goal, str) or not _SEO_GOAL_RE.fullmatch(raw_goal.strip().lower()):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        goal = raw_goal.strip().lower()
        include_web = arguments.get("include_web_research", True)
        if not isinstance(include_web, bool):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        try:
            res = _seo_service(tools).vet_link_vendors(
                market=market,
                budget_sek=float(raw_budget),
                link_count=raw_count,
                goal=goal,
                include_web_research=include_web,
                actor=ctx.actor,
            )
        except AccessDenied:
            denied_out = error_payload("AccessDenied")
            denied_out["status"] = "denied"
            return ToolOutput(denied_out, site_id=site_id)
        except Exception:
            return _seo_error("SeoVendorVettingFailed", "SeoVendorVettingFailed", site_id)
        if not isinstance(res, dict) or (res.get("ok") is False and not res.get("blocked")):
            raw_err = res.get("error_class") if isinstance(res, dict) else None
            return _seo_error(raw_err, "SeoVendorVettingFailed", site_id)

        raw_cands = res.get("candidates") if isinstance(res.get("candidates"), list) else []
        raw_cits = res.get("citations") if isinstance(res.get("citations"), list) else []
        raw_disc = res.get("disclosure_checklist") if isinstance(res.get("disclosure_checklist"), list) else []
        sel_vid = short_text(res.get("selected_vendor_id"), 48) if res.get("selected_vendor_id") else None
        return ToolOutput(
            {
                "ok": True,
                "status": "blocked" if res.get("blocked") else "ok",
                "mode": "vendors",
                "site_id": site_id,
                "market": market,
                "budget_sek": float(raw_budget),
                "link_count": raw_count,
                "goal": goal,
                "blocked": bool(res.get("blocked")),
                "block_reason": _scrub_str(tools, site_obj, str(res.get("block_reason") or ""), max_len=200),
                "budget_warning": _scrub_str(tools, site_obj, str(res.get("budget_warning") or ""), max_len=240),
                "recommended_tier": short_text(res.get("recommended_tier"), 48),
                "selected_vendor_id": sel_vid,
                "candidates": [_scrub_obj(tools, site_obj, row) for row in raw_cands[:3]],
                "disclosure_checklist": [_scrub_str(tools, site_obj, str(item), max_len=180) for item in raw_disc[:8]],
                "no_auto_purchase_notice": _scrub_str(
                    tools, site_obj, str(res.get("no_auto_purchase_notice") or ""), max_len=240
                ),
                "citations": [_scrub_obj(tools, site_obj, c) for c in raw_cits[:8]],
                "executed": False,
                "external_order_placed": False,
                "dashboard_path": f"/seo/vendors?market={market}",
                "live_calls": bool(res.get("live_calls")),
            },
            site_id=site_id,
        )

    if mode == "verify":
        if not pkg_id:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        try:
            res = _seo_service(tools).verify_link_package(pkg_id, actor=ctx.actor)
        except AccessDenied:
            denied_out = error_payload("AccessDenied")
            denied_out["status"] = "denied"
            return ToolOutput(denied_out, site_id=site_id)
        except Exception:
            return _seo_error("SeoLinkVerifyFailed", "SeoLinkVerifyFailed", site_id)
        if not isinstance(res, dict) or res.get("ok") is not True:
            raw_err = res.get("error_class") if isinstance(res, dict) else None
            return _seo_error(raw_err, "SeoLinkVerifyFailed", site_id)
        return ToolOutput(
            {
                "ok": True,
                "status": "verified",
                "mode": "verify",
                "site_id": site_id,
                "package_id": pkg_id,
                "verified_count": int(res.get("verified_count") or 0),
                "issue_count": int(res.get("issue_count") or 0),
                "results": _scrub_obj(tools, site_obj, res.get("results") or []),
                "external_order_placed": False,
                "dashboard_path": f"/seo/link-packages/{pkg_id}",
                "live_calls": bool(res.get("live_calls")),
            },
            site_id=site_id,
        )

    if mode == "cumulative":
        try:
            res = _seo_service(tools).evaluate_site_cumulative_anchor_mix(
                site_id,
                include_package_id=pkg_id,
                actor=ctx.actor,
            )
        except AccessDenied:
            denied_out = error_payload("AccessDenied")
            denied_out["status"] = "denied"
            return ToolOutput(denied_out, site_id=site_id)
        except Exception:
            return _seo_error("SeoCumulativeAnchorFailed", "SeoCumulativeAnchorFailed", site_id)
        if not isinstance(res, dict) or res.get("ok") is False:
            raw_err = res.get("error_class") if isinstance(res, dict) else None
            return _seo_error(raw_err, "SeoCumulativeAnchorFailed", site_id)
        return ToolOutput(
            {
                "ok": True,
                "status": "ok",
                "mode": "cumulative",
                "site_id": site_id,
                "package_id": pkg_id or "",
                "cumulative_anchor_mix": _scrub_obj(tools, site_obj, res),
                "external_order_placed": False,
                "dashboard_path": f"/seo/link-packages/{pkg_id}" if pkg_id else "/seo/link-packages",
                "live_calls": False,
            },
            site_id=site_id,
        )

    # mode == "create"
    name = arguments.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
    market = _infer_market(site_id, arguments.get("market"))
    if market is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    campaign_id = arguments.get("campaign_id")
    if campaign_id is not None and (
        not isinstance(campaign_id, str) or not _SEO_CAMPAIGN_ID_RE.fullmatch(campaign_id.strip())
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_budget = arguments.get("budget_sek", 15000.0)
    if (
        isinstance(raw_budget, bool)
        or not isinstance(raw_budget, (int, float))
        or not math.isfinite(float(raw_budget))
        or not (1000 <= float(raw_budget) <= 500000)
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_count = arguments.get("link_count", 6)
    if type(raw_count) is not int or not (1 <= raw_count <= 50):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_targets = arguments.get("targets")
    targets: list[dict[str, Any]] | None = None
    if raw_targets is not None:
        if (
            not isinstance(raw_targets, list)
            or len(raw_targets) > 25
            or any(not isinstance(t, dict) for t in raw_targets)
        ):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)
        targets = [dict(t) for t in raw_targets]

    notes = arguments.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 600:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    try:
        res = _seo_service(tools).create_link_package(
            site_id=site_id,
            actor=ctx.actor,
            name=name.strip(),
            market=market,
            campaign_id=campaign_id.strip() if isinstance(campaign_id, str) else None,
            budget_sek=float(raw_budget),
            link_count=raw_count,
            targets=targets,
            notes=notes.strip(),
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _seo_error("SeoLinkPackageFailed", "SeoLinkPackageFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _seo_error(raw_err, "SeoLinkPackageFailed", site_id)

    pkg = dict(res.get("package") or {})
    package_id = short_text(pkg.get("package_id") or res.get("package_id"), 32)
    safe_pkg = _scrub_obj(tools, site_obj, pkg)
    if isinstance(safe_pkg, dict):
        safe_pkg.pop("brief_markdown", None)

    return ToolOutput(
        {
            "ok": True,
            "status": short_text(pkg.get("status"), 32) or "draft",
            "mode": "create",
            "site_id": site_id,
            "package_id": package_id,
            "market": market,
            "executed": False,
            "approval_required": True,
            "external_order_placed": False,
            "guardrail_blocked": bool(pkg.get("guardrail_blocked")),
            "guardrail_warnings": [
                _scrub_str(tools, site_obj, str(w), max_len=200)
                for w in (pkg.get("guardrail_warnings") or [])[:12]
            ],
            "anchor_mix": _scrub_obj(tools, site_obj, pkg.get("anchor_mix") or {}),
            "package": safe_pkg,
            "dashboard_path": f"/seo/link-packages/{package_id}" if package_id else "/seo/link-packages",
            "login_required": True,
            "live_calls": False,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_seo_autopilot",
    group="file",
    label="Kör autonom SEO-pipeline (audit, SERP-research, strategi, kampanj, valda SEO-butiker & länkpaket-utkast)",
    short_label="SEO-autopilot",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string", "pattern": SITE_ID_RE.pattern},
            "topic": {"type": "string"},
            "market": {"type": "string", "enum": ["se", "no", "dk"]},
            "budget_sek": {"type": "number", "minimum": 1000, "maximum": 500000},
            "link_count": {"type": "integer", "minimum": 1, "maximum": 50},
            "include_web_research": {"type": "boolean"},
            "include_vendor_vetting": {"type": "boolean"},
            "include_batch_fixes": {"type": "boolean"},
            "start_deep_research": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    outcome=_autopilot_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="seo",
)
def handle_seo_autopilot(
    tools: Any, ctx: ToolContext, arguments: dict[str, Any]
) -> ToolOutput:
    site_id = str(ctx.site_id)
    denied = _require_perm(ctx.actor, Permission.WP_OPS_READ, site_id)
    if denied is not None:
        return denied
    try:
        actor_obj = resolve_actor(ctx.actor)
        if not (
            actor_obj.has(Permission.MARKETING_SUGGEST)
            or actor_obj.has(Permission.MARKETING_MUTATE)
        ):
            raise AccessDenied("wp_seo_autopilot requires MARKETING_SUGGEST or MARKETING_MUTATE")
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)

    site_obj, site_err = _require_site(tools, site_id, ctx.actor)
    if site_err is not None:
        return site_err

    topic = arguments.get("topic", "")
    if not isinstance(topic, str) or len(topic.strip()) > 200:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    market = _infer_market(site_id, arguments.get("market"))
    if market is None:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_budget = arguments.get("budget_sek", 15000.0)
    if (
        isinstance(raw_budget, bool)
        or not isinstance(raw_budget, (int, float))
        or not math.isfinite(float(raw_budget))
        or not (1000 <= float(raw_budget) <= 500000)
    ):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    raw_count = arguments.get("link_count", 6)
    if type(raw_count) is not int or not (1 <= raw_count <= 50):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    include_web = arguments.get("include_web_research", True)
    include_vendors = arguments.get("include_vendor_vetting", True)
    include_batch = arguments.get("include_batch_fixes", False)
    start_deep = arguments.get("start_deep_research", False)
    if not all(isinstance(v, bool) for v in (include_web, include_vendors, include_batch, start_deep)):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id=site_id)

    if include_batch:
        denied_op = _require_perm(ctx.actor, Permission.WP_OPS_OPERATE, site_id)
        if denied_op is not None:
            return denied_op
        if site_obj.environment == "production":
            denied_adm = _require_perm(ctx.actor, Permission.WP_OPS_ADMIN, site_id)
            if denied_adm is not None:
                return denied_adm

    try:
        res = _seo_service(tools).run_seo_autopilot(
            site_id,
            topic=topic.strip(),
            market=market,
            budget_sek=float(raw_budget),
            link_count=raw_count,
            include_web_research=include_web,
            include_vendor_vetting=include_vendors,
            include_batch_fixes=include_batch,
            start_deep_research=start_deep,
            actor=ctx.actor,
        )
    except AccessDenied:
        denied_out = error_payload("AccessDenied")
        denied_out["status"] = "denied"
        return ToolOutput(denied_out, site_id=site_id)
    except Exception:
        return _seo_error("SeoAutopilotFailed", "SeoAutopilotFailed", site_id)

    if not isinstance(res, dict) or res.get("ok") is not True:
        raw_err = res.get("error_class") if isinstance(res, dict) else None
        return _seo_error(raw_err, "SeoAutopilotFailed", site_id)

    raw_audit = dict(res.get("audit") or {})
    raw_strat = dict(res.get("strategy") or {})
    raw_camp = dict(res.get("campaign") or {})
    raw_vendors = dict(res.get("vendor_selection") or {})
    raw_pkg = dict(res.get("link_package") or {})
    raw_pkg.pop("brief_markdown", None)
    raw_batch = dict(res.get("batch_fix") or {}) if isinstance(res.get("batch_fix"), dict) else None

    audit_id = short_text(raw_audit.get("audit_id"), 32)
    strategy_id = short_text(raw_strat.get("strategy_id"), 32)
    campaign_id = short_text(raw_camp.get("campaign_id"), 32)
    package_id = short_text(raw_pkg.get("package_id"), 32)
    batch_id = short_text(raw_batch.get("batch_id"), 32) if raw_batch else ""
    sel_vendor_id = short_text(
        raw_vendors.get("selected_vendor_id") or raw_pkg.get("selected_vendor_id"),
        48,
    ) or None
    out_market = short_text(res.get("market") or market, 8) or market

    payload: dict[str, Any] = {
        "ok": True,
        "status": "draft_ready",
        "site_id": site_id,
        "market": out_market,
        "topic": _scrub_str(tools, site_obj, str(res.get("topic") or topic.strip()), max_len=200),
        "audit_id": audit_id,
        "strategy_id": strategy_id,
        "campaign_id": campaign_id,
        "package_id": package_id,
        "selected_vendor_id": sel_vendor_id,
        "executed": False,
        "external_order_placed": False,
        "approval_required": True,
        "audit": _scrub_obj(tools, site_obj, raw_audit),
        "serp_research": _scrub_obj(tools, site_obj, res.get("serp_research") or {}),
        "strategy": _scrub_obj(tools, site_obj, raw_strat),
        "campaign": _scrub_obj(tools, site_obj, raw_camp),
        "vendor_selection": _scrub_obj(tools, site_obj, raw_vendors),
        "link_package": _scrub_obj(tools, site_obj, raw_pkg),
        "batch_fix": _scrub_obj(tools, site_obj, raw_batch) if raw_batch is not None else None,
        "summary": _scrub_obj(tools, site_obj, res.get("summary") or {}),
        "dashboard_path": f"/seo/link-packages/{package_id}" if package_id else f"/seo?site_id={site_id}",
        "vendors_path": f"/seo/vendors?market={out_market}",
        "login_required": True,
        "live_calls": bool(res.get("live_calls")),
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return ToolOutput(payload, site_id=site_id)


__all__ = [
    "handle_seo_audit",
    "handle_seo_autopilot",
    "handle_seo_batch_plan",
    "handle_seo_campaign",
    "handle_seo_drift_digest",
    "handle_seo_link_package",
    "handle_seo_plan_meta",
    "handle_seo_strategy",
]
