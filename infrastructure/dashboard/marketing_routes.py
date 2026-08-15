"""Marketing digest + HITL suggest routes."""

from __future__ import annotations

from flask import Flask, g, redirect, render_template, url_for

from auth import _auth_required, _validate_csrf
from support import _dashboard_context, _flash_q, _is_mock


def register_marketing_routes(app: Flask) -> None:
    @app.route("/marketing")
    @_auth_required
    def marketing_home():
        from ecom_ops.actions.marketing import MarketingService

        svc = MarketingService(use_mock=_is_mock() or None)
        actor = g.actor["name"]
        dig = svc.digest(actor=actor)
        health = svc.health(actor=actor)
        cons = svc.consistency(actor=actor)
        sug = svc.list_suggests(status="open", actor=actor)
        err = None
        if not dig.ok:
            err = dig.message
        return render_template(
            "marketing.html",
            **_dashboard_context(
                digest=dig.data if dig.ok else None,
                health=health.data if health.ok else None,
                consistency=cons.data if cons.ok else None,
                suggests=(sug.data or {}).get("suggests") if sug.ok else [],
                error=err,
            ),
        )

    @app.route("/marketing/suggests/build", methods=["POST"])
    @_auth_required
    def marketing_suggests_build():
        failed = _validate_csrf()
        if failed:
            return failed
        from ecom_ops.actions.marketing import MarketingService

        result = MarketingService(use_mock=_is_mock() or None).build_waste_suggests(
            actor=g.actor["name"]
        )
        frag = _flash_q(result.message) if result.ok else _flash_q(err=result.message)
        return redirect(url_for("marketing_home") + f"?{frag}")

    @app.route("/marketing/suggests/<suggest_id>/deny", methods=["POST"])
    @_auth_required
    def marketing_suggest_deny(suggest_id: str):
        failed = _validate_csrf()
        if failed:
            return failed
        from ecom_ops.actions.marketing import MarketingService

        result = MarketingService(use_mock=_is_mock() or None).deny_suggest(
            suggest_id, actor=g.actor["name"]
        )
        frag = _flash_q(result.message) if result.ok else _flash_q(err=result.message)
        return redirect(url_for("marketing_home") + f"?{frag}")

    @app.route("/marketing/suggests/<suggest_id>/approve", methods=["POST"])
    @_auth_required
    def marketing_suggest_approve(suggest_id: str):
        failed = _validate_csrf()
        if failed:
            return failed
        from ecom_ops.actions.marketing import MarketingService

        result = MarketingService(use_mock=_is_mock() or None).approve_and_mutate(
            suggest_id, actor=g.actor["name"]
        )
        frag = _flash_q(result.message) if result.ok else _flash_q(err=result.message)
        return redirect(url_for("marketing_home") + f"?{frag}")
