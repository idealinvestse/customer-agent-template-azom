"""Dashboard routes for Shop Intelligence (/shop-intel)."""

from __future__ import annotations

import re
from typing import Any

from flask import (
    Blueprint,
    Flask,
    Response,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import _auth_required, _dashboard_actor
from ecom_ops.rbac import AccessDenied, Permission
from ecom_ops.shop_intel import ShopIntelService, build_tailored_context
from ecom_ops.shop_intel.models import (
    SHOP_INTEL_RUN_ID_RE,
    VALID_DOMAINS,
    VALID_DOMAINS_SET,
    VALID_MODES,
    VALID_SEVERITIES_SET,
)
from support import _dashboard_context, _data_dir, _flash, _fmt_dt, _is_mock

shop_intel_bp = Blueprint("shop_intel", __name__)

SITE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_VALID_EXPORT_FMTS = frozenset({"md", "json"})
_CONTEXT_PREVIEW_PURPOSES: tuple[tuple[str, str], ...] = (
    ("support_draft", "Supportutkast (support_draft)"),
    ("product_copy", "Produkttext (product_copy)"),
    ("wp_coder", "WP/UI-kodare (wp_coder)"),
    ("security_audit", "Säkerhetsgranskning (security_audit)"),
)

_DOMAIN_META: tuple[dict[str, str], ...] = (
    {
        "id": "platform_rules",
        "title": "Plattform & Regler",
        "description": "Valuta, land, fraktzoner, fri frakt-gränser, betalmetoder, moms och plattformsversioner.",
    },
    {
        "id": "catalog_pricing",
        "title": "Katalog & Prissättning",
        "description": "Produkturval, kategorier, attribut, prisspann, lagersaldo och katalogluckor.",
    },
    {
        "id": "theme_architecture",
        "title": "Tema & Arkitektur",
        "description": "Aktivt tema, block/classic-läge, theme.json, WooCommerce-overrides och MU-plugins.",
    },
    {
        "id": "brand_voice_copy",
        "title": "Varumärkesröst & Copy",
        "description": "Språk, tilltal (du/ni), textstruktur, USP-fraser och CTA-vokabulär.",
    },
    {
        "id": "policies_legal",
        "title": "Policyer & Villkor",
        "description": "Retur, ångerrätt, frakt, garanti, köpvillkor, integritet och saknade policysidor.",
    },
    {
        "id": "plugins_ecosystem",
        "title": "Plugins & Ekosystem",
        "description": "Aktiva/inaktiva plugins, funktionsgrupper, REST-namnrymder och webhook-värdar.",
    },
    {
        "id": "security_posture",
        "title": "Säkerhet & Drift",
        "description": "Säkerhetspoäng, inaktiva plugins, föråldrade Woo-mallar, indexering och driftvarningar.",
    },
)


def _shop_intel_service() -> ShopIntelService:
    return ShopIntelService(data_dir=_data_dir())


def _can_read(actor_obj: Any) -> bool:
    return bool(actor_obj.has(Permission.WP_OPS_READ) or actor_obj.has(Permission.READ))


def _can_run(actor_obj: Any) -> bool:
    return bool(
        actor_obj.has(Permission.WP_OPS_READ)
        and (
            actor_obj.has(Permission.RESEARCH_RUN)
            or actor_obj.has(Permission.WP_OPS_OPERATE)
            or actor_obj.has(Permission.WP_OPS_ADMIN)
        )
    )


def _can_schedule(actor_obj: Any) -> bool:
    return bool(g.actor.get("is_oscar")) and bool(
        actor_obj.has(Permission.WP_OPS_ADMIN) or actor_obj.has(Permission.RESEARCH_ADMIN)
    )


def _parse_bool_flag(raw: Any, *, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _load_sites_list(actor_name: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        from ecom_ops.wp_ops import WpOpsService

        res = WpOpsService(data_dir=_data_dir()).list_sites(actor=actor_name)
        if getattr(res, "ok", False) and isinstance(getattr(res, "data", None), dict):
            for row in res.data.get("sites") or []:
                if isinstance(row, dict) and row.get("id"):
                    out.append(
                        {
                            "id": str(row["id"]),
                            "display_name": str(row.get("display_name") or row["id"]),
                            "environment": str(row.get("environment") or "staging"),
                            "canonical_url": str(row.get("canonical_url") or ""),
                        }
                    )
    except Exception:
        pass

    if not out:
        try:
            from ecom_ops.wp_ops.registry import load_sites

            for site in load_sites(data_dir=_data_dir(), apply_overlay=False):
                out.append(
                    {
                        "id": str(site.id),
                        "display_name": str(getattr(site, "display_name", "") or site.id),
                        "environment": str(getattr(site, "environment", "") or "staging"),
                        "canonical_url": str(getattr(site, "canonical_url", "") or ""),
                    }
                )
        except Exception:
            pass

    if not out and _is_mock():
        out.append(
            {
                "id": "mock-shop",
                "display_name": "Mock Shop",
                "environment": "production",
                "canonical_url": "https://mock-shop.example.test",
            }
        )
    return out


def _resolve_site_id(sites: list[dict[str, str]], requested: str | None) -> str:
    cand = str(requested or "").strip().lower()
    known = {s["id"] for s in sites}
    if cand and cand in known:
        return cand
    if cand and SITE_ID_RE.fullmatch(cand):
        return cand
    return sites[0]["id"] if sites else "mock-shop"


def _decorate_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return None
    item = dict(run)
    item["created_at_fmt"] = _fmt_dt(item.get("created_at"))
    domains = item.get("domains_collected")
    item["domains_count"] = len(domains) if isinstance(domains, list) else 0
    return item


def _decorate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if item.get("created_at"):
            item["created_at_fmt"] = _fmt_dt(item.get("created_at"))
        if item.get("observed_at"):
            item["observed_at_fmt"] = _fmt_dt(item.get("observed_at"))
        out.append(item)
    return out


def _decorate_schedule(sched: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sched, dict):
        return None
    item = dict(sched)
    item["last_run_at_fmt"] = _fmt_dt(item.get("last_run_at"))
    item["next_run_at_fmt"] = _fmt_dt(item.get("next_run_at"))
    item["updated_at_fmt"] = _fmt_dt(item.get("updated_at"))
    return item


def _domain_cards(
    *,
    dossier: dict[str, Any],
    facts: list[dict[str, Any]],
    domains_collected: list[str],
) -> list[dict[str, Any]]:
    facts_by_domain: dict[str, list[dict[str, Any]]] = {d: [] for d in VALID_DOMAINS}
    for fact in facts:
        dom = str(fact.get("domain") or "")
        if dom in facts_by_domain:
            facts_by_domain[dom].append(fact)

    collected_set = set(domains_collected)
    cards: list[dict[str, Any]] = []
    for meta in _DOMAIN_META:
        dom_id = meta["id"]
        dom_data = dossier.get(dom_id) if isinstance(dossier.get(dom_id), dict) else {}
        dom_facts = facts_by_domain.get(dom_id, [])
        cards.append(
            {
                "id": dom_id,
                "title": meta["title"],
                "description": meta["description"],
                "collected": dom_id in collected_set or bool(dom_data),
                "data": dom_data,
                "facts": dom_facts,
                "facts_count": len(dom_facts),
            }
        )
    return cards


@shop_intel_bp.route("/shop-intel", methods=["GET"])
@_auth_required
def shop_intel_index():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    actor_name = g.actor["name"]
    sites = _load_sites_list(actor_name)
    requested_site = request.args.get("site_id") or request.args.get("site")
    selected_site = _resolve_site_id(sites, requested_site)

    selected_domain = (request.args.get("domain") or "").strip().lower() or None
    if selected_domain and selected_domain not in VALID_DOMAINS_SET:
        selected_domain = None

    search_q = (request.args.get("q") or "").strip()[:200] or None

    selected_severity = (request.args.get("severity") or "").strip().lower() or None
    if selected_severity and selected_severity not in VALID_SEVERITIES_SET:
        selected_severity = None

    svc = _shop_intel_service()
    try:
        status_res = svc.get_status(selected_site, actor=actor_name)
    except AccessDenied:
        return Response("Saknar behörighet", 403)

    gates = status_res.get("gates") if isinstance(status_res.get("gates"), dict) else {}
    latest_run = _decorate_run(status_res.get("latest_run"))
    schedule = _decorate_schedule(status_res.get("schedule"))
    recent_runs = [
        r
        for r in (_decorate_run(item) for item in (status_res.get("recent_runs") or []))
        if r is not None
    ]

    all_facts: list[dict[str, Any]] = []
    filtered_facts: list[dict[str, Any]] = []
    dossier: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    all_findings_count = 0
    diffs: list[dict[str, Any]] = []

    if latest_run:
        q_all = svc.query_knowledge(selected_site, actor=actor_name)
        if q_all.get("ok"):
            all_facts = _decorate_rows(q_all.get("facts") or [])
            dossier = q_all.get("dossier") if isinstance(q_all.get("dossier"), dict) else {}

        if selected_domain or search_q:
            q_filt = svc.query_knowledge(
                selected_site,
                domain=selected_domain,
                query=search_q,
                actor=actor_name,
            )
            filtered_facts = _decorate_rows(q_filt.get("facts") or []) if q_filt.get("ok") else []
        else:
            filtered_facts = list(all_facts)

        sec_res = svc.get_security_report(
            selected_site,
            severity=selected_severity,
            limit=50,
            actor=actor_name,
        )
        if sec_res.get("ok"):
            findings = _decorate_rows(sec_res.get("findings") or [])
        all_findings_count = int(latest_run.get("findings_count") or len(findings))

        diffs_res = svc.get_diffs(selected_site, limit=20, actor=actor_name)
        if diffs_res.get("ok"):
            diffs = _decorate_rows(diffs_res.get("diffs") or [])

    domains_collected = (
        list(latest_run.get("domains_collected") or [])
        if isinstance(latest_run, dict)
        else []
    )
    coverage_pct = (
        round((len(domains_collected) / len(VALID_DOMAINS)) * 100)
        if latest_run and VALID_DOMAINS
        else 0
    )

    domain_cards = _domain_cards(
        dossier=dossier,
        facts=filtered_facts,
        domains_collected=domains_collected,
    )
    if selected_domain:
        visible_domain_cards = [c for c in domain_cards if c["id"] == selected_domain]
    else:
        visible_domain_cards = domain_cards

    return render_template(
        "shop_intel/index.html",
        **_dashboard_context(
            sites=sites,
            selected_site=selected_site,
            selected_domain=selected_domain or "",
            search_q=search_q or "",
            selected_severity=selected_severity or "",
            gates=gates,
            latest_run=latest_run,
            coverage_pct=coverage_pct,
            domains_collected_count=len(domains_collected),
            total_domains_count=len(VALID_DOMAINS),
            facts_count=len(all_facts),
            filtered_facts_count=len(filtered_facts),
            findings=findings,
            all_findings_count=all_findings_count,
            diffs=diffs,
            recent_runs=recent_runs,
            schedule=schedule,
            domain_cards=visible_domain_cards,
            domain_meta=_DOMAIN_META,
            can_run=_can_run(actor_obj),
            can_schedule=_can_schedule(actor_obj),
        ),
    )


@shop_intel_bp.route("/shop-intel/run", methods=["POST"])
@_auth_required
def shop_intel_run():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_run(actor_obj):
        return Response("Saknar behörighet", 403)

    actor_name = g.actor["name"]
    sites = _load_sites_list(actor_name)
    site_id = _resolve_site_id(sites, request.form.get("site_id"))
    mode = (request.form.get("mode") or "full").strip().lower()
    if mode not in VALID_MODES:
        _flash(f"Ogiltigt analysläge '{mode}'. Tillåtna: full, quick, security.", "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))

    raw_domains = [
        str(d).strip().lower()
        for d in request.form.getlist("domains")
        if str(d).strip()
    ]
    selected_domains: list[str] | None = None
    if raw_domains:
        invalid = [d for d in raw_domains if d not in VALID_DOMAINS_SET]
        if invalid:
            _flash(f"Okänd domän: {', '.join(invalid)}.", "error")
            return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))
        selected_domains = raw_domains

    try:
        res = _shop_intel_service().run_learning(
            site_id=site_id,
            actor=actor_name,
            mode=mode,
            domains=selected_domains,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except ValueError as exc:
        _flash(f"Butiksanalys kunde inte köras: {exc}", "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))
    except Exception as exc:
        _flash(f"Butiksanalys misslyckades: {exc}", "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))

    if not res.get("ok"):
        _flash(str(res.get("message") or "Butiksanalys blockerades."), "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))

    run = res.get("run") if isinstance(res.get("run"), dict) else {}
    run_id = str(run.get("run_id") or "")
    provenance = str(run.get("provenance") or ("live" if res.get("live_calls") else "mock"))
    prov_label = "live" if provenance == "live" else "mock"
    score = res.get("security_score", run.get("security_score", 100))
    facts_n = int(res.get("facts_count") or 0)
    findings_n = len(res.get("findings") or [])
    diffs_n = len(res.get("diffs") or [])
    _flash(
        f"Butiksanalys {run_id} klar för {site_id} ({prov_label}) — "
        f"säkerhetspoäng {score}/100, {facts_n} fakta, {findings_n} fynd, {diffs_n} driftvarningar.",
        "ok",
    )
    return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))


@shop_intel_bp.route("/shop-intel/runs/<run_id>", methods=["GET"])
@_auth_required
def shop_intel_run_detail(run_id: str):
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    clean_id = str(run_id or "").strip()
    if not SHOP_INTEL_RUN_ID_RE.fullmatch(clean_id):
        return Response("Ogiltigt körnings-ID", 404)

    actor_name = g.actor["name"]
    svc = _shop_intel_service()
    try:
        res = svc.get_run(clean_id, actor=actor_name)
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except ValueError:
        return Response("Ogiltigt körnings-ID", 404)

    if not res.get("ok") or not isinstance(res.get("run"), dict):
        return Response("Körningen hittades inte", 404)

    run = _decorate_run(res["run"]) or {}
    site_id = str(run.get("site_id") or "mock-shop")
    facts = _decorate_rows(res.get("facts") or [])
    findings = _decorate_rows(res.get("findings") or [])
    diffs = _decorate_rows(res.get("diffs") or [])
    dossier = run.get("dossier") if isinstance(run.get("dossier"), dict) else {}

    domain_titles = {item["id"]: item["title"] for item in _DOMAIN_META}
    facts_by_domain: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        dom = str(fact.get("domain") or "other")
        facts_by_domain.setdefault(dom, []).append(fact)

    context_previews: list[dict[str, str]] = []
    data_dir = _data_dir()
    for purpose, label in _CONTEXT_PREVIEW_PURPOSES:
        try:
            text = build_tailored_context(site_id, purpose=purpose, data_dir=data_dir)
        except Exception:
            text = None
        context_previews.append(
            {
                "purpose": purpose,
                "label": label,
                "content": text or "",
            }
        )

    return render_template(
        "shop_intel/run_detail.html",
        **_dashboard_context(
            run=run,
            site_id=site_id,
            dossier=dossier,
            facts=facts,
            facts_by_domain=facts_by_domain,
            domain_titles=domain_titles,
            findings=findings,
            diffs=diffs,
            context_previews=context_previews,
            can_run=_can_run(actor_obj),
        ),
    )


@shop_intel_bp.route("/shop-intel/export", methods=["GET"])
@_auth_required
def shop_intel_export():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_read(actor_obj):
        return Response("Saknar behörighet", 403)

    actor_name = g.actor["name"]
    sites = _load_sites_list(actor_name)
    site_id = _resolve_site_id(sites, request.args.get("site_id") or request.args.get("site"))
    fmt = (request.args.get("format") or request.args.get("fmt") or "json").strip().lower()
    if fmt not in _VALID_EXPORT_FMTS:
        return Response("Ogiltigt exportformat (tillåtna: md, json)", 400)

    try:
        res = _shop_intel_service().export_dossier(site_id, fmt=fmt, actor=actor_name)
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except ValueError:
        return Response("Ogiltig begäran", 400)

    if not res.get("ok"):
        return Response(
            str(res.get("message") or f"Ingen exporterbar dossier hittades för {site_id}."),
            404,
        )

    ext = "md" if fmt == "md" else "json"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", f"shop-intel-{site_id}.{ext}")
    mimetype = (
        "text/markdown; charset=utf-8" if ext == "md" else "application/json; charset=utf-8"
    )
    return Response(
        str(res.get("content") or ""),
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@shop_intel_bp.route("/shop-intel/schedule", methods=["POST"])
@_auth_required
def shop_intel_schedule():
    actor_obj = _dashboard_actor(g.actor)
    if not _can_schedule(actor_obj):
        return Response("Saknar behörighet", 403)

    actor_name = g.actor["name"]
    sites = _load_sites_list(actor_name)
    site_id = _resolve_site_id(sites, request.form.get("site_id"))
    enabled = _parse_bool_flag(request.form.get("enabled"), default=False)

    raw_hours = (request.form.get("interval_hours") or "24").strip()
    try:
        interval_hours = int(raw_hours)
    except ValueError:
        _flash("Intervall (timmar) måste vara ett heltal mellan 1 och 720.", "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))
    if interval_hours < 1 or interval_hours > 720:
        _flash("Intervall (timmar) måste vara mellan 1 och 720.", "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))

    mode = (request.form.get("mode") or "full").strip().lower()
    if mode not in VALID_MODES:
        _flash("Ogiltigt analysläge för schema (tillåtna: full, quick, security).", "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))

    try:
        res = _shop_intel_service().configure_schedule(
            site_id,
            enabled=enabled,
            interval_hours=interval_hours,
            mode=mode,
            actor=actor_name,
        )
    except AccessDenied:
        return Response("Saknar behörighet", 403)
    except ValueError as exc:
        _flash(f"Kunde inte spara schema: {exc}", "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))

    if not res.get("ok"):
        _flash(str(res.get("message") or "Kunde inte spara schema."), "error")
        return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))

    state_lbl = f"aktiverat (var {interval_hours}:e timme, läge {mode})" if enabled else "pausat"
    _flash(f"Återkommande butiksanalys för {site_id} {state_lbl}.", "ok")
    return redirect(url_for("shop_intel.shop_intel_index", site_id=site_id))


def register_shop_intel_routes(app: Flask) -> None:
    app.register_blueprint(shop_intel_bp)


__all__ = ["register_shop_intel_routes", "shop_intel_bp"]
