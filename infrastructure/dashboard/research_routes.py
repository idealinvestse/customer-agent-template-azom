"""Dashboard routes for web search, async Deep Research, status polling, and export."""

from __future__ import annotations

import json
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from flask import (
    Blueprint,
    Flask,
    Response,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import _auth_required, _dashboard_actor, _oscar_required
from ecom_ops.audit import log_action
from ecom_ops.integrations._http_guard import safe_public_https_url
from ecom_ops.integrations.search.errors import SearchRouteError
from ecom_ops.integrations.search.router import (
    LIVE_ROUTE_IDS,
    get_search_router,
    reset_search_router,
)
from ecom_ops.ops_status import web_search_readiness
from ecom_ops.rbac import AccessDenied, Permission, require_permission
from ecom_ops.research import ResearchService, list_profiles, research_budget_status
from ecom_ops.research.config import is_mock_mode, load_research_config
from ecom_ops.research.evaluator import classify_claim_kind
from ecom_ops.research.models import (
    RESEARCH_ID_RE,
    EvaluatedSource,
    ResearchJob,
    SearchPolicyError,
)
from ecom_ops.research.profiles import get_profile, recommend_profile
from ecom_ops.research.store import (
    ResearchStore,
    authorize_research_access,
    authorize_target_ref,
)
from ecom_ops.runtime_env import env_flag
from ecom_ops.telemetry import Telemetry
from settings_store import (
    load_research_settings_view,
    save_research_settings,
    save_secrets,
)
from support import _dashboard_context, _data_dir, _flash, _flash_q, _fmt_dt

research_bp = Blueprint("research", __name__)

_VALID_DEPTHS = frozenset({"quick", "standard", "deep"})
_RESEARCH_API_KEYS: tuple[str, ...] = (
    "PARALLEL_API_KEY",
    "EXA_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "SEARXNG_API_KEY",
)
_PROVIDER_CARD_META: tuple[dict[str, str], ...] = (
    {
        "id": "parallel",
        "label": "Parallel",
        "env_key": "PARALLEL_API_KEY",
        "role_hint": "Webbsökning + sidextraktion (primary)",
    },
    {
        "id": "exa",
        "label": "Exa",
        "env_key": "EXA_API_KEY",
        "role_hint": "Semantisk UX-, CRO- & research-sökning",
    },
    {
        "id": "brave",
        "label": "Brave Search",
        "env_key": "BRAVE_SEARCH_API_KEY",
        "role_hint": "Snabbt nordiskt webbindex & nyheter",
    },
    {
        "id": "tavily",
        "label": "Tavily",
        "env_key": "TAVILY_API_KEY",
        "role_hint": "Djupwebb & strukturerat sidinnehåll (valfri fallback)",
    },
    {
        "id": "searxng",
        "label": "SearXNG",
        "env_key": "SEARXNG_API_KEY",
        "role_hint": "Egen instans (valfri, kräver SEARXNG_BASE_URL i processmiljö)",
    },
)
_QUICK_PRESETS: tuple[dict[str, str], ...] = (
    {
        "id": "thankyou_ux",
        "label": "Tacksida UX",
        "profile_id": "design_inspiration",
        "depth": "standard",
        "mode": "research",
        "hint": "UX- & konverteringsmönster för tacksida och checkout",
        "placeholder": "T.ex. WooCommerce thank you page UX best practices order tracking",
    },
    {
        "id": "product_spec_carplay",
        "label": "Produkt-spec / CarPlay",
        "profile_id": "product_spec_deep",
        "depth": "standard",
        "mode": "research",
        "hint": "OEM-passform, CAN-bus, kablage och tekniska begränsningar",
        "placeholder": "T.ex. Volvo XC60 2016 wireless CarPlay CAN-bus compatibility specs",
    },
    {
        "id": "support_postnord",
        "label": "PostNord / Support faktakoll",
        "profile_id": "support_fact_check",
        "depth": "quick",
        "mode": "search",
        "hint": "Färska fraktstörningar, ombud och konsumentregler utan kund-PII",
        "placeholder": "T.ex. PostNord driftstörning paketombud Sverige",
    },
    {
        "id": "ads_ga4",
        "label": "Google Ads / GA4",
        "profile_id": "marketing_intel",
        "depth": "standard",
        "mode": "search",
        "hint": "Consent Mode v2, GA4-nyheter och säsongssökord",
        "placeholder": "T.ex. Google Ads Consent Mode v2 ecommerce GA4 update",
    },
    {
        "id": "seo_gaps",
        "label": "SEO innehållsluckor",
        "profile_id": "seo_content_radar",
        "depth": "standard",
        "mode": "research",
        "hint": "Sökintention och innehållsluckor på SE/NO/DK",
        "placeholder": "T.ex. trådlös CarPlay Volvo innehållsluckor köpguide SE",
    },
    {
        "id": "competitor_nordic",
        "label": "Konkurrentanalys SE/NO/DK",
        "profile_id": "competitor_market",
        "depth": "deep",
        "mode": "research",
        "hint": "Nordiska konkurrenter, prisnivåer, garanti och USP",
        "placeholder": "T.ex. nordiska konkurrenter bilstereo CarPlay adapter SE NO DK",
    },
    {
        "id": "gdpr_consumer_law",
        "label": "GDPR / Konsumenträtt",
        "profile_id": "regulatory_compliance",
        "depth": "deep",
        "mode": "research",
        "hint": "Konsumentköplagen, ångerrätt, IMY/GDPR och nordisk juridik",
        "placeholder": "T.ex. konsumentköplagen reklamation bilelektronik Sverige",
    },
    {
        "id": "triangulated_deep",
        "label": "Triangulerad djupresearch",
        "profile_id": "metasearch_triangulation",
        "depth": "deep",
        "mode": "research",
        "hint": "Maximal bredd och källtriangulering över flera sökmotorer",
        "placeholder": "T.ex. eftermarknad CarPlay Android Auto trender Norden 2026",
    },
)


def _research_store() -> ResearchStore:
    base = _data_dir()
    return ResearchStore(
        path=base / "research_jobs.sqlite3",
        artifacts_dir=base / "research_artifacts",
    )


def _research_service() -> ResearchService:
    return ResearchService(data_dir=_data_dir())


def _maybe_start_research_worker() -> None:
    """Start background research worker ONLY for live non-test dashboard enqueues."""
    if current_app.config.get("TESTING"):
        return
    if is_mock_mode():
        return
    if env_flag("AZOM_DISABLE_BACKGROUND_WORKERS", default=False):
        return
    from ecom_ops.research.worker import ensure_research_worker_started

    ensure_research_worker_started(data_dir=_data_dir())


def _safe_next_url(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 or ch == "\\" for ch in text):
        return None
    text = text.split("#", 1)[0].strip()
    if not text or not text.startswith("/") or text.startswith("//"):
        return None
    return text


def _format_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        item = dict(ev)
        created = item.get("created_at")
        item["created_at_fmt"] = _fmt_dt(created) or str(created or "")
        out.append(item)
    return out


def _case_taint_for_target(target_ref: str | None):
    if not target_ref or not target_ref.startswith("case:"):
        return None
    case_id = target_ref.split(":", 1)[1].strip()
    if not case_id:
        return None
    from ecom_ops.cases.service import CaseService

    case_svc = CaseService()
    case_obj = case_svc.store.get(case_id)
    if case_obj is None:
        return None
    case_msgs = case_svc.store.messages(case_id)
    return case_svc._build_case_taint_context(case_obj, case_msgs)


def _job_view(job: ResearchJob) -> dict[str, Any]:
    data = job.to_dict()
    spec = data.get("spec") or {}
    data["id"] = job.research_id
    data["topic"] = spec.get("topic") or ""
    data["profile_id"] = spec.get("profile_id") or "deep_general"
    data["depth"] = spec.get("depth") or "standard"
    data["locale"] = spec.get("locale") or "sv"
    data["country"] = spec.get("country") or "SE"
    data["markets"] = spec.get("markets") or []
    data["rounds_completed"] = job.current_round
    data["evidence_status"] = str(job.evidence_status) if job.evidence_status else ""
    data["created_at_fmt"] = _fmt_dt(job.created_at)
    data["updated_at_fmt"] = _fmt_dt(job.updated_at)
    data["completed_at_fmt"] = _fmt_dt(job.completed_at)
    return data


def _annotate_source(
    raw: dict[str, Any],
    profile_id: str,
    *,
    evidence_status: str | None = None,
) -> dict[str, Any]:
    item = dict(raw)
    url = str(item.get("url") or "")
    item["safe_url"] = safe_public_https_url(url)
    if evidence_status and not item.get("evidence_status"):
        item["evidence_status"] = evidence_status
    if not item.get("claim_kind"):
        try:
            profile = get_profile(profile_id)
            item["claim_kind"] = str(
                classify_claim_kind(EvaluatedSource.from_dict(item), profile)
            )
        except Exception:
            item["claim_kind"] = "fact"
    return item


def _annotate_brief(raw_brief: dict[str, Any]) -> dict[str, Any]:
    brief = dict(raw_brief)
    cits_out: list[dict[str, Any]] = []
    for cit in brief.get("citations") or []:
        if isinstance(cit, dict):
            c = dict(cit)
            c["safe_url"] = safe_public_https_url(str(c.get("url") or ""))
            cits_out.append(c)
    brief["citations"] = cits_out
    return brief


def _list_steers(store: ResearchStore, job_id: str) -> list[dict[str, Any]]:
    with store._connection() as conn:
        rows = conn.execute(
            "SELECT id, job_id, actor, note, created_at, consumed_at "
            "FROM research_steers WHERE job_id=? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        created_iso = (
            datetime.fromtimestamp(float(row["created_at"]), tz=UTC).isoformat()
            if row["created_at"]
            else ""
        )
        consumed_iso = (
            datetime.fromtimestamp(float(row["consumed_at"]), tz=UTC).isoformat()
            if row["consumed_at"]
            else None
        )
        out.append(
            {
                "id": int(row["id"]),
                "job_id": str(row["job_id"]),
                "actor": str(row["actor"]),
                "note": str(row["note"]),
                "created_at": created_iso,
                "created_at_fmt": _fmt_dt(created_iso),
                "consumed_at": consumed_iso,
            }
        )
    return out


def _build_search_status() -> dict[str, Any]:
    readiness = web_search_readiness()
    budget = research_budget_status(telemetry=Telemetry(path=_data_dir() / "telemetry.jsonl"))
    return {
        **readiness,
        "mode": readiness.get("status") or ("mock" if readiness.get("mock_mode") else "disabled"),
        "kill_switch": bool(readiness.get("kill_switch_search")),
        "budget": budget,
        "queries_today": budget["queries_today"],
        "max_queries_per_day": budget["max_queries_per_day"],
        "fetches_today": budget["fetches_today"],
        "max_fetches_per_day": budget["max_fetches_per_day"],
        "monthly_external_usd": budget["monthly_external_usd"],
        "monthly_external_usd_cap": budget["monthly_external_usd_cap"],
        "near_cap": budget["near_cap"],
        "at_cap": budget["at_cap"],
        "search_allowed": budget["search_allowed"],
        "fetch_allowed": budget["fetch_allowed"],
        "message": budget["message"],
        "daily_queries_used": budget["queries_today"],
        "daily_queries_cap": budget["max_queries_per_day"],
    }


def _build_provider_cards() -> tuple[list[dict[str, Any]], int]:
    router = get_search_router()
    routes_map = router.routes()
    cfg = load_research_config()
    cards: list[dict[str, Any]] = []
    configured_count = 0
    for meta in _PROVIDER_CARD_META:
        rid = meta["id"]
        route_info = routes_map.get(rid) or {}
        configured = bool(route_info.get("configured"))
        if rid == "searxng":
            enabled = configured
        else:
            p_cfg = cfg.providers.get(rid)
            enabled = not (isinstance(p_cfg, dict) and p_cfg.get("enabled") is False)
        if configured:
            configured_count += 1
        cards.append(
            {
                "id": rid,
                "label": meta["label"],
                "env_key": meta["env_key"],
                "role_hint": meta["role_hint"],
                "configured": configured,
                "enabled": enabled,
            }
        )
    return cards, configured_count


def _hub_context(
    *,
    quick_result: dict[str, Any] | None = None,
    quick_query: str = "",
    selected_profile: str = "auto",
    selected_locale: str = "SE",
    flash: str | None = None,
) -> dict[str, Any]:
    actor_name = g.actor["name"]
    actor_obj = _dashboard_actor(g.actor)
    research_settings = load_research_settings_view()
    store = _research_store()
    jobs = [_job_view(j) for j in store.list_jobs(actor=actor_name, limit=50)]
    profiles = [p.to_dict() for p in list_profiles()]
    provider_cards, configured_provider_count = _build_provider_cards()
    raw_loc = (selected_locale or "").strip()
    norm_locale = "SE" if raw_loc.lower() in {"", "sv"} else raw_loc
    extra: dict[str, Any] = {
        "jobs": jobs,
        "profiles": profiles,
        "search_status": _build_search_status(),
        "provider_cards": provider_cards,
        "configured_provider_count": configured_provider_count,
        "research_settings": research_settings,
        "quick_presets": [dict(p) for p in _QUICK_PRESETS],
        "quick_result": quick_result,
        "quick_query": quick_query,
        "quick_locale": norm_locale,
        "selected_profile": selected_profile,
        "selected_locale": norm_locale,
        "can_admin": actor_obj.has(Permission.RESEARCH_ADMIN),
    }
    if flash is not None:
        extra["flash"] = flash
    return _dashboard_context(**extra)


def _authorize_job_or_error(
    res_id: str,
    *,
    action: str,
    as_json: bool = False,
) -> tuple[ResearchStore, ResearchJob | None, Response | tuple[Response, int] | None]:
    if RESEARCH_ID_RE.fullmatch(str(res_id or "")) is None:
        if as_json:
            return _research_store(), None, (jsonify({"ok": False, "error": "not_found"}), 404)
        return _research_store(), None, Response("Okänd research", 404)
    store = _research_store()
    job = store.get_job(res_id)
    if job is None:
        if as_json:
            return store, None, (jsonify({"ok": False, "error": "not_found"}), 404)
        return store, None, Response("Research hittades inte", 404)
    if not authorize_research_access(g.actor["name"], job, action):  # type: ignore[arg-type]
        if as_json:
            return store, None, (jsonify({"ok": False, "error": "forbidden"}), 403)
        return store, None, Response("Saknar behörighet", 403)
    return store, job, None


def load_linked_research_jobs(
    *,
    actor: str,
    target_ref: str | None = None,
    target_ref_prefix: str | None = None,
    extra_ids: Sequence[str] = (),
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return RBAC-filtered research job view dicts linked to a case/marketing/WP entity."""
    store = _research_store()
    seen: set[str] = set()
    matched: list[dict[str, Any]] = []

    if target_ref:
        jobs = store.list_jobs(actor=actor, target_ref=target_ref, limit=limit)
    else:
        jobs = store.list_jobs(actor=actor, limit=max(limit * 4, 40))

    for job in jobs:
        ref = str(job.target_ref or "")
        if target_ref and ref != target_ref:
            continue
        if target_ref_prefix and not ref.startswith(target_ref_prefix):
            continue
        if job.research_id in seen:
            continue
        seen.add(job.research_id)
        matched.append(_job_view(job))
        if len(matched) >= limit:
            return matched

    for rid in extra_ids:
        clean_id = str(rid or "").strip()
        if not clean_id or clean_id in seen or RESEARCH_ID_RE.fullmatch(clean_id) is None:
            continue
        try:
            job = store.get_job(clean_id, actor=actor)
        except AccessDenied:
            continue
        if job is None:
            continue
        seen.add(job.research_id)
        matched.append(_job_view(job))
        if len(matched) >= limit:
            break

    return matched


@research_bp.route("/research", methods=["GET"])
@_auth_required
def research_hub():
    actor_obj = _dashboard_actor(g.actor)
    if not (actor_obj.has(Permission.WEB_SEARCH) or actor_obj.has(Permission.RESEARCH_RUN)):
        return Response("Saknar behörighet", 403)
    selected_profile = (request.args.get("profile") or "auto").strip() or "auto"
    return render_template(
        "research/hub.html",
        **_hub_context(selected_profile=selected_profile),
    )


@research_bp.route("/research/recommend", methods=["GET"])
@_auth_required
def research_recommend():
    actor_obj = _dashboard_actor(g.actor)
    if not (actor_obj.has(Permission.WEB_SEARCH) or actor_obj.has(Permission.RESEARCH_RUN)):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    q = (request.args.get("q") or "").strip()[:500]
    raw_ambition = (request.args.get("ambition") or "auto").strip().lower()
    ambition = raw_ambition if raw_ambition in {"", "auto", "quick", "standard", "deep"} else "auto"
    resolved = recommend_profile(
        q,
        ambition=None if ambition in {"", "auto"} else ambition,
    )
    resolved_depth = (
        resolved.ambition if resolved.ambition in _VALID_DEPTHS else "standard"
    ) if ambition in {"", "auto"} else ambition

    router = get_search_router()
    routes_info = router.routes()
    configured_providers = [
        rid for rid in LIVE_ROUTE_IDS if bool((routes_info.get(rid) or {}).get("configured"))
    ]
    search_status = web_search_readiness()
    return jsonify(
        {
            "ok": True,
            "profile": resolved.to_dict(),
            "resolved_depth": resolved_depth,
            "configured_providers": configured_providers,
            "live_ready": bool(search_status.get("ready")),
        }
    )


@research_bp.route("/research/config/keys", methods=["POST"])
@_oscar_required
def research_config_keys():
    updates: dict[str, str] = {}
    for key in _RESEARCH_API_KEYS:
        raw_val = request.form.get(key, "")
        if raw_val.strip():
            updates[key] = raw_val

    try:
        saved = save_secrets(updates)
    except ValueError:
        err_msg = "Ogiltig API-nyckel (kontrolltecken är inte tillåtna)."
        _flash(err_msg, "error")
        return redirect(url_for("research.research_hub") + f"?{_flash_q(err=err_msg)}#research-setup")

    if saved:
        reset_search_router()
        log_action(
            actor=g.actor["name"],
            action="research_keys_updated",
            target="research_config",
            details={"keys": sorted(saved)},
        )
        msg = f"Sparade {len(saved)} API-nycklar ({', '.join(sorted(saved))})"
        _flash(msg, "ok")
        q = _flash_q(msg)
    else:
        msg = "Inga API-nycklar angivna"
        _flash(msg, "info")
        q = _flash_q(info=msg)
    return redirect(url_for("research.research_hub") + f"?{q}#research-setup")


@research_bp.route("/research/config/settings", methods=["POST"])
@_oscar_required
def research_config_settings():
    form_data = dict(request.form)
    cache_vals = request.form.getlist("cache_enabled")
    form_data["cache_enabled"] = (
        "1"
        if any(str(v).strip().lower() in {"1", "true", "yes", "on"} for v in cache_vals)
        else "0"
    )
    try:
        saved_view = save_research_settings(form_data)
    except ValueError as exc:
        err_msg = str(exc) or "Ogiltiga research-inställningar"
        _flash(err_msg, "error")
        return redirect(url_for("research.research_hub") + f"?{_flash_q(err=err_msg)}#research-setup")

    reset_search_router()
    log_action(
        actor=g.actor["name"],
        action="research_settings_updated",
        target="research_config",
        details={
            "route_order": saved_view.get("route_order", ""),
            "default_country": saved_view.get("default_country"),
            "default_lang": saved_view.get("default_lang"),
            "max_queries_per_day": saved_view.get("max_queries_per_day"),
            "max_fetches_per_day": saved_view.get("max_fetches_per_day"),
            "monthly_external_usd_cap": saved_view.get("monthly_external_usd_cap"),
            "cache_enabled": saved_view.get("cache_enabled"),
        },
    )
    msg = "Research-inställningar sparade"
    _flash(msg, "ok")
    return redirect(url_for("research.research_hub") + f"?{_flash_q(msg)}#research-setup")


@research_bp.route("/research/config/test", methods=["POST"])
@_oscar_required
def research_config_test():
    route_id = (request.form.get("route_id") or "all").strip().lower()
    if route_id not in set(LIVE_ROUTE_IDS) | {"all"}:
        err_msg = "Okänd sökmotor för test."
        _flash(err_msg, "error")
        return redirect(url_for("research.research_hub") + f"?{_flash_q(err=err_msg)}#research-setup")

    router = get_search_router()
    targets = list(LIVE_ROUTE_IDS) if route_id == "all" else [route_id]
    results_by_id: dict[str, dict[str, Any]] = {}

    def _probe_one(rid: str) -> dict[str, Any]:
        try:
            return dict(router.test_route(rid))
        except Exception as exc:
            return {
                "ok": False,
                "status": "error",
                "error_class": type(exc).__name__,
            }

    if len(targets) > 1:
        with ThreadPoolExecutor(max_workers=min(5, len(targets))) as pool:
            futs = [(rid, pool.submit(_probe_one, rid)) for rid in targets]
            for rid, fut in futs:
                results_by_id[rid] = fut.result()
    else:
        for rid in targets:
            results_by_id[rid] = _probe_one(rid)

    parts: list[str] = []
    has_error = False
    has_missing = False
    for rid in targets:
        res = results_by_id.get(rid) or {"ok": False, "status": "error"}
        ok = bool(res.get("ok"))
        status = str(res.get("status") or ("ok" if ok else "error")).strip().lower()
        raw_code = str(res.get("error_code") or res.get("error_class") or "").strip()
        safe_code = "".join(ch for ch in raw_code if ch.isalnum() or ch in {"_", "-"})[:40]
        if ok:
            parts.append(f"{rid}: ok")
        else:
            if status == "missing":
                has_missing = True
            else:
                has_error = True
            if safe_code:
                parts.append(f"{rid}: {status} ({safe_code})")
            else:
                parts.append(f"{rid}: {status}")

    summary = "Söktest: " + ", ".join(parts)
    if has_error:
        _flash(summary, "error")
        q = _flash_q(err=summary)
    elif has_missing:
        _flash(summary, "warn")
        q = _flash_q(warn=summary)
    else:
        _flash(summary, "ok")
        q = _flash_q(summary)
    return redirect(url_for("research.research_hub") + f"?{q}#research-setup")


@research_bp.route("/research/search", methods=["POST"])
@_auth_required
def research_search():
    try:
        require_permission(_dashboard_actor(g.actor), Permission.WEB_SEARCH)
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    query = (request.form.get("query") or "").strip()
    raw_profile = (request.form.get("profile_id") or request.form.get("profile") or "auto").strip()
    auto_selected = raw_profile.lower() in {"", "auto"}
    selected_profile = "auto" if auto_selected else raw_profile
    raw_locale = (request.form.get("locale") or "").strip()
    locale = ("en" if raw_locale.upper() == "EN" else raw_locale) or None

    if not query:
        err_msg = "Ange en sökfråga."
        _flash(err_msg, "error")
        return render_template(
            "research/hub.html",
            **_hub_context(
                quick_result=None,
                quick_query="",
                selected_profile=selected_profile,
                selected_locale=locale or "SE",
                flash=f"error:{err_msg}",
            ),
        )

    resolved_profile = recommend_profile(query) if auto_selected else None
    effective_profile_id = resolved_profile.id if resolved_profile is not None else raw_profile

    try:
        raw_result = _research_service().quick_search(
            query,
            actor=g.actor["name"],
            profile_id=effective_profile_id,
            locale=locale,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except SearchPolicyError as exc:
        err_msg = f"Sökningen blockerades av säkerhetspolicy ({exc})"
        _flash(err_msg, "error")
        return render_template(
            "research/hub.html",
            **_hub_context(
                quick_result=None,
                quick_query=query,
                selected_profile=selected_profile,
                selected_locale=locale or "SE",
                flash=f"error:{err_msg}",
            ),
        )
    except SearchRouteError as exc:
        code = getattr(exc, "code", "") or "error"
        err_msg = f"Webbsökning misslyckades ({type(exc).__name__}: {code})"
        _flash(err_msg, "error")
        return render_template(
            "research/hub.html",
            **_hub_context(
                quick_result=None,
                quick_query=query,
                selected_profile=selected_profile,
                selected_locale=locale or "SE",
                flash=f"error:{err_msg}",
            ),
        )
    except (KeyError, ValueError) as exc:
        err_msg = f"Ogiltig sökparameter: {exc}"
        _flash(err_msg, "error")
        return render_template(
            "research/hub.html",
            **_hub_context(
                quick_result=None,
                quick_query=query,
                selected_profile=selected_profile,
                selected_locale=locale or "SE",
                flash=f"error:{err_msg}",
            ),
        )

    quick_result = dict(raw_result)
    if auto_selected and resolved_profile is not None:
        quick_result["auto_selected"] = True
        quick_result["resolved_profile_id"] = resolved_profile.id
        quick_result["resolved_profile_title"] = resolved_profile.title or resolved_profile.id
    ev_status = str(quick_result.get("evidence_status") or "")
    quick_result["sources"] = [
        _annotate_source(
            src,
            str(quick_result.get("profile_id") or effective_profile_id),
            evidence_status=ev_status,
        )
        for src in (quick_result.get("sources") or [])
        if isinstance(src, dict)
    ]
    return render_template(
        "research/hub.html",
        **_hub_context(
            quick_result=quick_result,
            quick_query=query,
            selected_profile=selected_profile,
            selected_locale=locale or "SE",
        ),
    )


@research_bp.route("/research/jobs", methods=["POST"])
@_auth_required
def research_jobs_create():
    try:
        require_permission(_dashboard_actor(g.actor), Permission.RESEARCH_RUN)
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    topic = (request.form.get("topic") or request.form.get("query") or "").strip()
    raw_profile = (request.form.get("profile_id") or request.form.get("profile") or "auto").strip()
    raw_depth = (request.form.get("depth") or "auto").strip().lower()
    profile_auto = raw_profile.lower() in {"", "auto"}
    depth_auto = raw_depth in {"", "auto"}

    if profile_auto:
        resolved = recommend_profile(topic, ambition=None if depth_auto else raw_depth)
        profile_id = resolved.id
        if depth_auto:
            depth = resolved.ambition if resolved.ambition in _VALID_DEPTHS else "standard"
        else:
            depth = raw_depth
    else:
        profile_id = raw_profile
        if depth_auto:
            try:
                prof_amb = get_profile(profile_id).ambition
                depth = prof_amb if prof_amb in _VALID_DEPTHS else "standard"
            except KeyError:
                depth = "standard"
        else:
            depth = raw_depth

    market = (request.form.get("market") or "").strip().upper()
    raw_locale = (request.form.get("locale") or "").strip()
    locale = ("en" if raw_locale.upper() == "EN" else raw_locale) or None
    target_ref = (request.form.get("target_ref") or "").strip() or None
    next_url = _safe_next_url(request.form.get("next_url") or request.form.get("next"))

    try:
        authorize_target_ref(g.actor["name"], target_ref)
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except SearchPolicyError as exc:
        err_msg = f"Ogiltig målreferens ({exc})"
        _flash(err_msg, "error")
        dest = next_url or url_for("research.research_hub")
        sep = "&" if "?" in dest else "?"
        return redirect(f"{dest}{sep}{_flash_q(err=err_msg)}")

    is_wp = bool(target_ref and target_ref.startswith(("wp:", "wp_site:")))
    visibility = "team" if (target_ref and not is_wp) else "private"
    markets = [market] if market else None
    country = market if (len(market) == 2 and market != "EN") else None
    if market == "EN" and not locale:
        locale = "en"
    taint = _case_taint_for_target(target_ref)

    try:
        job = _research_service().start_research(
            topic,
            actor=g.actor["name"],
            profile_id=profile_id,
            depth=depth,
            target_ref=target_ref,
            markets=markets,
            locale=locale,
            country=country,
            visibility=visibility,
            channel="dashboard",
            taint=taint,
            start_worker=False,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except (SearchPolicyError, SearchRouteError, KeyError, ValueError) as exc:
        err_msg = f"Kunde inte starta research ({exc})"
        _flash(err_msg, "error")
        dest = next_url or url_for("research.research_hub")
        sep = "&" if "?" in dest else "?"
        return redirect(f"{dest}{sep}{_flash_q(err=err_msg)}")

    try:
        _maybe_start_research_worker()
    except SearchRouteError as exc:
        err_msg = f"Research köad men kunde inte startas ({exc})"
        _flash(err_msg, "error")
        dest = next_url or url_for("research.research_detail", res_id=job.research_id)
        sep = "&" if "?" in dest else "?"
        return redirect(f"{dest}{sep}{_flash_q(err=err_msg)}")
    auto_suffix = f" (auto: {profile_id}/{depth})" if (profile_auto or depth_auto) else ""
    msg = f"Research startad ({job.research_id}){auto_suffix}"
    _flash(msg, "ok")
    if next_url:
        sep = "&" if "?" in next_url else "?"
        return redirect(f"{next_url}{sep}{_flash_q(msg)}")
    return redirect(url_for("research.research_detail", res_id=job.research_id))


@research_bp.route("/research/<res_id>", methods=["GET"])
@_auth_required
def research_detail(res_id: str):
    try:
        require_permission(_dashboard_actor(g.actor), Permission.WEB_SEARCH)
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    store, job, err_resp = _authorize_job_or_error(res_id, action="view", as_json=False)
    if err_resp is not None or job is None:
        return err_resp

    actor_name = g.actor["name"]
    actor_obj = _dashboard_actor(g.actor)
    brief_res = store.read_brief(res_id, actor=actor_name)
    profile_id = job.spec.profile_id if job.spec is not None else "deep_general"
    try:
        profile_meta = get_profile(profile_id).to_dict()
    except KeyError:
        profile_meta = get_profile("deep_general").to_dict()

    brief: dict[str, Any] | None = None
    sources: list[dict[str, Any]] = []
    if brief_res.get("ok"):
        raw_brief = brief_res.get("brief")
        if isinstance(raw_brief, dict):
            brief = _annotate_brief(raw_brief)
        ev_status = str((brief or {}).get("evidence_status") or job.evidence_status or "")
        sources = [
            _annotate_source(s, profile_id, evidence_status=ev_status)
            for s in (brief_res.get("sources") or [])
            if isinstance(s, dict)
        ]

    events = _format_events(store.list_events(res_id, since_seq=0, limit=200))
    steers = _list_steers(store, res_id)
    can_mutate = authorize_research_access(actor_name, job, "steer")
    return render_template(
        "research/detail.html",
        **_dashboard_context(
            job=_job_view(job),
            profile_meta=profile_meta,
            brief=brief,
            sources=sources,
            events=events,
            steers=steers,
            can_mutate=can_mutate,
            can_steer=can_mutate,
            can_cancel=authorize_research_access(actor_name, job, "cancel"),
            can_admin=actor_obj.has(Permission.RESEARCH_ADMIN),
        ),
    )


@research_bp.route("/research/<res_id>/status", methods=["GET"])
@_auth_required
def research_status(res_id: str):
    try:
        require_permission(_dashboard_actor(g.actor), Permission.WEB_SEARCH)
    except AccessDenied:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    store, job, err_resp = _authorize_job_or_error(res_id, action="view", as_json=True)
    if err_resp is not None or job is None:
        return err_resp

    try:
        since_seq = max(0, int(request.args.get("since_seq") or 0))
    except (TypeError, ValueError):
        since_seq = 0
    events = _format_events(store.list_events(res_id, since_seq=since_seq, limit=200))
    return jsonify(
        {
            "ok": True,
            "id": job.research_id,
            "status": job.status,
            "rounds_completed": job.current_round,
            "queries_used": job.queries_used,
            "fetches_used": job.fetches_used,
            "evidence_status": str(job.evidence_status) if job.evidence_status else None,
            "events": events,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
            "error_code": job.error_class,
            "error_message": job.error_message,
        }
    )


@research_bp.route("/research/<res_id>/steer", methods=["POST"])
@_auth_required
def research_steer(res_id: str):
    try:
        require_permission(_dashboard_actor(g.actor), Permission.RESEARCH_RUN)
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    _, job, err_resp = _authorize_job_or_error(res_id, action="steer", as_json=False)
    if err_resp is not None or job is None:
        return err_resp

    if job.status in {"completed", "partial", "cancelled", "failed"}:
        err_msg = "Kan inte styra ett redan avslutat research-jobb."
        _flash(err_msg, "error")
        return redirect(url_for("research.research_detail", res_id=res_id) + f"?{_flash_q(err=err_msg)}")

    note = (request.form.get("note") or "").strip()
    try:
        taint = _case_taint_for_target(job.target_ref)
        if taint is not None and note:
            from ecom_ops.research.firewall import OutboundQueryFirewall

            fw = OutboundQueryFirewall()
            norm_note = " ".join(note.split())
            for offset in range(0, len(norm_note), 125):
                chunk = norm_note[offset : offset + 250]
                if chunk:
                    fw.check_query(chunk, taint=taint)
                if offset + 250 >= len(norm_note):
                    break
        _research_service().steer_research(res_id, note, actor=g.actor["name"])
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except KeyError:
        return Response("Research hittades inte", 404)
    except (SearchPolicyError, ValueError) as exc:
        err_msg = f"Ogiltig styrning ({exc})"
        _flash(err_msg, "error")
        return redirect(url_for("research.research_detail", res_id=res_id) + f"?{_flash_q(err=err_msg)}")

    msg = "Styrningsnotering tillagd"
    _flash(msg, "ok")
    return redirect(url_for("research.research_detail", res_id=res_id) + f"?{_flash_q(msg)}")


@research_bp.route("/research/<res_id>/cancel", methods=["POST"])
@_auth_required
def research_cancel(res_id: str):
    try:
        require_permission(_dashboard_actor(g.actor), Permission.RESEARCH_RUN)
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    _, job, err_resp = _authorize_job_or_error(res_id, action="cancel", as_json=False)
    if err_resp is not None or job is None:
        return err_resp

    try:
        res = _research_service().cancel_research(res_id, actor=g.actor["name"])
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    result = res.get("result")
    if result == "cancelled":
        msg = "Research avbruten"
        _flash(msg, "ok")
        q = _flash_q(msg)
    elif result == "already_terminal":
        msg = "Research-jobbet är redan avslutat"
        _flash(msg, "info")
        q = _flash_q(info=msg)
    else:
        msg = "Avbrytning begärd"
        _flash(msg, "ok")
        q = _flash_q(msg)
    return redirect(url_for("research.research_detail", res_id=res_id) + f"?{q}")


@research_bp.route("/research/<res_id>/export", methods=["GET"])
@_auth_required
def research_export(res_id: str):
    try:
        require_permission(_dashboard_actor(g.actor), Permission.WEB_SEARCH)
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    store, job, err_resp = _authorize_job_or_error(res_id, action="export", as_json=False)
    if err_resp is not None or job is None:
        return err_resp

    fmt = (request.args.get("format") or "md").strip().lower()
    if fmt not in {"md", "json"}:
        return Response(
            "Ogiltigt exportformat (tillåtna: md, json)",
            400,
            mimetype="text/plain; charset=utf-8",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    brief_res = store.read_brief(res_id, actor=g.actor["name"])
    if brief_res.get("ok"):
        brief_data = dict(brief_res.get("brief") or {})
        sources_data = list(brief_res.get("sources") or [])
        meta_data = dict(brief_res.get("meta") or {})
    else:
        job_view = _job_view(job)
        brief_data = {
            "research_id": job.research_id,
            "topic": job_view["topic"],
            "profile_id": job_view["profile_id"],
            "evidence_status": job_view["evidence_status"] or "insufficient",
            "summary": "",
            "findings": [],
            "recommendations": [],
            "gaps": [],
            "conflicts": [],
            "citations": [],
            "markdown": "",
            "provenance": "system",
            "created_at": job.created_at,
        }
        sources_data = []
        meta_data = {
            "research_id": job.research_id,
            "actor": job.actor,
            "topic": job_view["topic"],
            "profile_id": job_view["profile_id"],
            "depth": job_view["depth"],
            "status": job.status,
            "visibility": job.visibility,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
        }

    if fmt == "json":
        payload = json.dumps(
            {
                "research_id": res_id,
                "brief": brief_data,
                "sources": sources_data,
                "meta": meta_data,
            },
            ensure_ascii=False,
            indent=2,
        )
        return Response(
            payload,
            200,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Disposition": f'attachment; filename="research-{res_id}.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    md_text = str(brief_data.get("markdown") or "").strip()
    if not md_text:
        topic = str(brief_data.get("topic") or res_id)
        profile_id = str(brief_data.get("profile_id") or "deep_general")
        ev_status = str(brief_data.get("evidence_status") or job.status)
        summary = str(brief_data.get("summary") or "_Inget underlag genererat ännu._")
        lines = [
            f"# {topic}",
            "",
            f"- ID: `{res_id}`",
            f"- Profile: `{profile_id}`",
            f"- Evidence: `{ev_status}`",
            "",
            "## Summary",
            summary,
        ]
        cits = brief_data.get("citations") or []
        if cits:
            lines.extend(["", "## Sources"])
            for cit in cits:
                if isinstance(cit, dict):
                    lines.append(
                        f"- [{cit.get('id', '')}] {cit.get('title', '')} "
                        f"({cit.get('domain', '')}) — {cit.get('url', '')}"
                    )
        md_text = "\n".join(lines).strip() + "\n"

    return Response(
        md_text,
        200,
        headers={
            "Content-Type": "text/markdown; charset=utf-8",
            "Content-Disposition": f'attachment; filename="research-{res_id}.md"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def register_research_routes(app: Flask) -> None:
    """Register the research Blueprint on ``app`` without starting worker threads."""
    app.register_blueprint(research_bp)
