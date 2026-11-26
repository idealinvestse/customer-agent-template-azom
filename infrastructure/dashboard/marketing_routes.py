"""Marketing digest + HITL suggest routes."""

from __future__ import annotations

from flask import Flask, g, redirect, render_template, request, url_for

from auth import _auth_required, _dashboard_actor
from support import _dashboard_context, _flash, _flash_q, _is_mock

_ALLOWED_DAYS = (7, 14, 30)


def register_marketing_routes(app: Flask) -> None:
    @app.route("/marketing")
    @_auth_required
    def marketing_home():
        from ecom_ops.actions.marketing import MarketingService
        from ecom_ops.marketing.kill_switch import (
            ads_mutate_allowed,
            ga_mutate_allowed,
            merchant_write_allowed,
            mp_allowed,
        )
        from ecom_ops.oauth.google_marketing import (
            GoogleMarketingOAuthStore,
            google_marketing_oauth_configured,
        )
        from ecom_ops.rbac import Permission

        try:
            days = int(request.args.get("days", "7"))
        except ValueError:
            days = 7
        if days not in _ALLOWED_DAYS:
            days = 7

        svc = MarketingService(use_mock=_is_mock() or None)
        actor = g.actor["name"]
        actor_obj = _dashboard_actor(g.actor)

        section_errors: dict[str, str] = {}

        def _call(name: str, fn):
            try:
                res = fn()
            except Exception as exc:
                section_errors[name] = f"{name} misslyckades ({type(exc).__name__})"
                return None
            if not res.ok:
                section_errors[name] = res.message or f"{name} misslyckades"
                return None
            return res.data

        dig_data = _call("digest", lambda: svc.digest(days=days, actor=actor))
        health_data = _call("health", lambda: svc.health(actor=actor))
        cons_data = _call("consistency", lambda: svc.consistency(days=days, actor=actor))
        sug_data = _call("suggests", lambda: svc.list_suggests(status="open", actor=actor))

        mer = None
        if isinstance(dig_data, dict):
            ads = dig_data.get("ads") or {}
            ga4 = dig_data.get("ga4") or {}
            try:
                ads_cost = float(ads.get("cost") or 0)
                ga_rev = float(ga4.get("purchase_revenue") or 0)
                if ads_cost > 0:
                    mer = round(ga_rev / ads_cost, 2)
            except (TypeError, ValueError):
                mer = None

        ads_ok, ads_reason = ads_mutate_allowed()
        ga_ok, ga_reason = ga_mutate_allowed()
        mp_ok, mp_reason = mp_allowed()
        merch_ok, merch_reason = merchant_write_allowed()
        kill_switches = {
            "ads_mutate_allowed": ads_ok,
            "ads_mutate_reason": ads_reason,
            "ga_mutate_allowed": ga_ok,
            "ga_mutate_reason": ga_reason,
            "mp_allowed": mp_ok,
            "mp_reason": mp_reason,
            "merchant_write_allowed": merch_ok,
            "merchant_write_reason": merch_reason,
        }

        oauth_store = GoogleMarketingOAuthStore()
        err = section_errors.get("digest") or (
            next(iter(section_errors.values())) if section_errors else None
        )
        marketing_research_jobs: list[dict] = []
        try:
            from research_routes import load_linked_research_jobs

            marketing_research_jobs = load_linked_research_jobs(
                actor=actor,
                target_ref_prefix="marketing:",
                limit=10,
            )
        except Exception:
            marketing_research_jobs = []
        return render_template(
            "marketing.html",
            **_dashboard_context(
                digest=dig_data,
                health=health_data,
                consistency=cons_data,
                suggests=(sug_data or {}).get("suggests") or [],
                error=err,
                section_errors=section_errors,
                days=days,
                days_options=list(_ALLOWED_DAYS),
                mer=mer,
                kill_switches=kill_switches,
                google_oauth_connected=oauth_store.has_tokens(),
                google_oauth_configured=google_marketing_oauth_configured(),
                can_suggest=actor_obj.has(Permission.MARKETING_SUGGEST),
                can_mutate=actor_obj.has(Permission.MARKETING_MUTATE),
                marketing_research_jobs=marketing_research_jobs,
            ),
        )

    @app.route("/marketing/suggests/build", methods=["POST"])
    @_auth_required
    def marketing_suggests_build():
        from ecom_ops.actions.marketing import MarketingService

        result = MarketingService(use_mock=_is_mock() or None).build_waste_suggests(
            actor=g.actor["name"]
        )
        _flash(result.message, "ok" if result.ok else "error")
        frag = _flash_q(result.message) if result.ok else _flash_q(err=result.message)
        return redirect(url_for("marketing_home") + f"?{frag}")

    @app.route("/marketing/suggests/<suggest_id>/deny", methods=["POST"])
    @_auth_required
    def marketing_suggest_deny(suggest_id: str):
        from ecom_ops.actions.marketing import MarketingService

        result = MarketingService(use_mock=_is_mock() or None).deny_suggest(
            suggest_id, actor=g.actor["name"]
        )
        _flash(result.message, "ok" if result.ok else "error")
        frag = _flash_q(result.message) if result.ok else _flash_q(err=result.message)
        return redirect(url_for("marketing_home") + f"?{frag}")

    @app.route("/marketing/suggests/<suggest_id>/approve", methods=["POST"])
    @_auth_required
    def marketing_suggest_approve(suggest_id: str):
        from ecom_ops.actions.marketing import MarketingService

        result = MarketingService(use_mock=_is_mock() or None).approve_and_mutate(
            suggest_id, actor=g.actor["name"]
        )
        _flash(result.message, "ok" if result.ok else "error")
        frag = _flash_q(result.message) if result.ok else _flash_q(err=result.message)
        return redirect(url_for("marketing_home") + f"?{frag}")
