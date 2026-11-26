"""Dashboard routes for web search, async Deep Research, status polling, and export."""

from __future__ import annotations

import json
from collections.abc import Sequence
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

from auth import _auth_required, _dashboard_actor
from ecom_ops.integrations._http_guard import safe_public_https_url
from ecom_ops.integrations.search.errors import SearchRouteError
from ecom_ops.ops_status import web_search_readiness
from ecom_ops.rbac import AccessDenied, Permission, require_permission
from ecom_ops.research import ResearchService, list_profiles, research_budget_status
from ecom_ops.research.config import is_mock_mode
from ecom_ops.research.evaluator import classify_claim_kind
from ecom_ops.research.models import (
    RESEARCH_ID_RE,
    EvaluatedSource,
    ResearchJob,
    SearchPolicyError,
)
from ecom_ops.research.profiles import get_profile
from ecom_ops.research.store import (
    ResearchStore,
    authorize_research_access,
    authorize_target_ref,
)
from ecom_ops.runtime_env import env_flag
from ecom_ops.telemetry import Telemetry
from support import _dashboard_context, _data_dir, _flash, _flash_q, _fmt_dt

research_bp = Blueprint("research", __name__)


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
    if not text or not text.startswith("/") or text.startswith("//"):
        return None
    if any(ch in text for ch in ("\r", "\n", "\t", "\\")):
        return None
    return text


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


def _hub_context(
    *,
    quick_result: dict[str, Any] | None = None,
    quick_query: str = "",
    selected_profile: str = "deep_general",
    selected_locale: str = "sv",
    flash: str | None = None,
) -> dict[str, Any]:
    actor_name = g.actor["name"]
    actor_obj = _dashboard_actor(g.actor)
    store = _research_store()
    jobs = [_job_view(j) for j in store.list_jobs(actor=actor_name, limit=50)]
    profiles = [p.to_dict() for p in list_profiles()]
    extra: dict[str, Any] = {
        "jobs": jobs,
        "profiles": profiles,
        "search_status": _build_search_status(),
        "quick_result": quick_result,
        "quick_query": quick_query,
        "quick_locale": selected_locale,
        "selected_profile": selected_profile,
        "selected_locale": selected_locale,
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
    selected_profile = (request.args.get("profile") or "deep_general").strip() or "deep_general"
    return render_template(
        "research/hub.html",
        **_hub_context(selected_profile=selected_profile),
    )


@research_bp.route("/research/search", methods=["POST"])
@_auth_required
def research_search():
    try:
        require_permission(_dashboard_actor(g.actor), Permission.WEB_SEARCH)
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    query = (request.form.get("query") or "").strip()
    profile_id = (
        request.form.get("profile_id") or request.form.get("profile") or "deep_general"
    ).strip() or "deep_general"
    locale = (request.form.get("locale") or "").strip() or None

    if not query:
        err_msg = "Ange en sökfråga."
        _flash(err_msg, "error")
        return render_template(
            "research/hub.html",
            **_hub_context(
                quick_result=None,
                quick_query="",
                selected_profile=profile_id,
                selected_locale=locale or "sv",
                flash=f"error:{err_msg}",
            ),
        )

    try:
        raw_result = _research_service().quick_search(
            query,
            actor=g.actor["name"],
            profile_id=profile_id,
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
                selected_profile=profile_id,
                selected_locale=locale or "sv",
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
                selected_profile=profile_id,
                selected_locale=locale or "sv",
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
                selected_profile=profile_id,
                selected_locale=locale or "sv",
                flash=f"error:{err_msg}",
            ),
        )

    quick_result = dict(raw_result)
    ev_status = str(quick_result.get("evidence_status") or "")
    quick_result["sources"] = [
        _annotate_source(
            src,
            str(quick_result.get("profile_id") or profile_id),
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
            selected_profile=profile_id,
            selected_locale=locale or "sv",
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
    profile_id = (
        request.form.get("profile_id") or request.form.get("profile") or "deep_general"
    ).strip() or "deep_general"
    depth = (request.form.get("depth") or "standard").strip().lower() or "standard"
    market = (request.form.get("market") or "").strip().upper()
    locale = (request.form.get("locale") or "").strip() or None
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
    country = market if len(market) == 2 else None

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
            start_worker=False,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except (SearchPolicyError, KeyError, ValueError) as exc:
        err_msg = f"Kunde inte starta research ({exc})"
        _flash(err_msg, "error")
        dest = next_url or url_for("research.research_hub")
        sep = "&" if "?" in dest else "?"
        return redirect(f"{dest}{sep}{_flash_q(err=err_msg)}")

    _maybe_start_research_worker()
    msg = f"Research startad ({job.research_id})"
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

    events = store.list_events(res_id, since_seq=0, limit=200)
    steers = _list_steers(store, res_id)
    can_mutate = authorize_research_access(actor_name, job, "steer")
    return render_template(
        "research/detail.html",
        **_dashboard_context(
            job=_job_view(job),
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
    events = store.list_events(res_id, since_seq=since_seq, limit=200)
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

    note = (request.form.get("note") or "").strip()
    try:
        _research_service().steer_research(res_id, note, actor=g.actor["name"])
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except KeyError:
        return Response("Research hittades inte", 404)
    except SearchPolicyError as exc:
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

    msg = "Research avbruten" if res.get("result") == "cancelled" else "Avbrytning begärd"
    _flash(msg, "ok")
    return redirect(url_for("research.research_detail", res_id=res_id) + f"?{_flash_q(msg)}")


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
