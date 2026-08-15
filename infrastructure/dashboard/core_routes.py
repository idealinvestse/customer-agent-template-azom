"""Home, onboarding, settings, interact, metrics, and JSON tails."""

from __future__ import annotations

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for

from auth import _auth_required
from settings_store import load_settings_view, save_settings, secrets_status
from status import health_probe, runtime_status
from support import (
    _dashboard_context,
    _data_dir,
    _load_probe_last,
    _presence_integrations,
    _tail_jsonl,
)


def register_core_routes(app: Flask) -> None:
    @app.route("/metrics")
    def prometheus_metrics():
        import os
        import sqlite3

        from flask import abort

        token = (os.environ.get("METRICS_SCRAPE_TOKEN") or "").strip()
        remote = (request.remote_addr or "").strip()
        localhost = remote in {"127.0.0.1", "::1", "localhost"}
        auth = (request.headers.get("Authorization") or "").strip()
        bearer_ok = bool(token) and auth == f"Bearer {token}"
        if not localhost and not bearer_ok:
            abort(403)

        from ecom_ops.budget import budget_status
        from ecom_ops.kpis import support_kpis_last_days

        lines: list[str] = []
        bs = budget_status()
        lines.append("# TYPE azom_budget_used_usd gauge")
        lines.append(f"azom_budget_used_usd {bs.get('used_usd', 0)}")
        lines.append(f"azom_budget_cap_usd {bs.get('cap_usd', 0)}")
        lines.append(f"azom_budget_used_ratio {bs.get('used_ratio', 0)}")
        kpis = support_kpis_last_days(days=7)
        lines.append("# TYPE azom_case_approved_total gauge")
        lines.append(f"azom_case_approved_total {kpis.get('n_case_approved', 0)}")
        if kpis.get("median_time_to_approve_sec") is not None:
            lines.append(f"azom_median_time_to_approve_sec {kpis['median_time_to_approve_sec']}")
        if kpis.get("mean_draft_edit_distance") is not None:
            lines.append(f"azom_mean_draft_edit_distance {kpis['mean_draft_edit_distance']}")
        db_path = _data_dir() / "cases.db"
        if db_path.is_file():
            try:
                conn = sqlite3.connect(str(db_path))
                for status in ("open", "escalated", "replied", "closed"):
                    count = conn.execute(
                        "SELECT COUNT(*) FROM cases WHERE status = ?", (status,)
                    ).fetchone()[0]
                    lines.append(f'azom_cases{{status="{status}"}} {count}')
                conn.close()
            except Exception:
                pass
        probe_path = _data_dir() / "probe_last.json"
        if probe_path.is_file():
            try:
                import json as _json

                data = _json.loads(probe_path.read_text(encoding="utf-8"))
                lines.append(f"azom_probe_last_ts {data.get('checked_at', '')!r}")
            except Exception:
                pass
        return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")

    @app.route("/")
    @_auth_required
    def index():
        data = _data_dir()
        telemetry = _tail_jsonl(data / "telemetry.jsonl", 20)
        escalations = _tail_jsonl(data / "escalations.jsonl", 20)
        cost = 0.0
        for ev in telemetry:
            try:
                cost += float(ev.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
        runtime = runtime_status()
        cap = float(runtime.get("openrouter_cap") or 100)
        cost_pct = min(100, int((cost / cap) * 100)) if cap else 0
        presence = _presence_integrations(runtime)
        last_probe = _load_probe_last()
        budget = None
        try:
            from ecom_ops.budget import budget_status
            from ecom_ops.telemetry import Telemetry

            budget = budget_status(telemetry=Telemetry(path=_data_dir() / "telemetry.jsonl"))
            if budget.get("used_usd") is not None:
                cost = float(budget["used_usd"])
                cost_pct = min(100, int((cost / cap) * 100)) if cap else 0
        except Exception:
            budget = None
        kpis = None
        try:
            from ecom_ops.kpis import support_kpis_last_days
            from ecom_ops.telemetry import Telemetry as _Tel

            kpis = support_kpis_last_days(
                telemetry=_Tel(path=_data_dir() / "telemetry.jsonl"),
                days=7,
            )
        except Exception:
            kpis = None
        readiness = None
        try:
            from ecom_ops.ops_status import readiness_from_last_poll

            readiness = readiness_from_last_poll()
        except Exception:
            readiness = None
        return render_template(
            "index.html",
            **_dashboard_context(
                telemetry=telemetry,
                escalations=escalations,
                cost_usd=round(cost, 4),
                openrouter_cap=runtime.get("openrouter_cap", 100),
                budget_cap_llm=runtime.get("budget_cap_llm", 80),
                cost_pct=cost_pct,
                integrations=presence,
                last_probe=last_probe,
                budget=budget,
                support_kpis=kpis,
                readiness=readiness,
            ),
        )

    @app.route("/onboarding")
    @_auth_required
    def onboarding():
        return render_template(
            "onboarding.html",
            **_dashboard_context(secrets=secrets_status(), health=health_probe()),
        )

    @app.route("/onboarding/status")
    @_auth_required
    def onboarding_status():
        return jsonify(
            {
                "runtime": runtime_status(),
                "secrets": secrets_status(),
                "health": health_probe(),
            }
        )

    @app.route("/settings", methods=["GET", "POST"])
    @_auth_required
    def settings():
        if request.method == "POST":
            form = request.form.to_dict()
            for flag in (
                "email_enabled",
                "email_smtp",
                "email_imap",
                "email_pop3",
                "mailcow",
                "order_api",
                "selenium",
                "woocommerce_api",
                "wordpress_api",
                "smart_handling",
                "full_agent_tools",
            ):
                form[flag] = "1" if flag in request.form else "0"
            if g.actor.get("is_oscar"):
                form["mock_mode"] = "1" if "mock_mode" in request.form else "0"
            else:
                current = load_settings_view()
                form["mock_mode"] = "1" if current.get("mock_mode") else "0"
            try:
                save_settings(form)
                return redirect(url_for("settings") + "?msg=Sparat")
            except ValueError as exc:
                return redirect(url_for("settings") + f"?err={exc}")
        return render_template(
            "settings.html",
            **_dashboard_context(settings=load_settings_view()),
        )

    @app.route("/secrets", methods=["GET", "POST"])
    @_auth_required
    def secrets_page():
        if request.method == "POST":
            if g.actor.get("is_oscar"):
                return redirect(url_for("oscar_secrets"))
            keys_raw = request.form.get("keys", "")
            note = request.form.get("note", "").strip()[:500]
            keys = [k.strip() for k in keys_raw.replace(";", ",").split(",") if k.strip()]
            if not keys:
                keys = [k for k in request.form.getlist("key") if k]
            if not keys:
                return redirect(url_for("secrets_page") + "?err=Välj+minst+en+nyckel")
            from ecom_ops.escalation import EscalationService

            EscalationService().escalate_critical(
                "Dashboard secret update requested by Jonatan",
                details={
                    "requested_keys": keys,
                    "note": note,
                    "actor": g.actor["name"],
                },
            )
            return redirect(url_for("secrets_page") + "?msg=Begäran+skickad+till+Oscar")
        return render_template(
            "secrets.html",
            **_dashboard_context(secrets=secrets_status()),
        )

    @app.route("/data/telemetry")
    @_auth_required
    def data_telemetry():
        rows = _tail_jsonl(_data_dir() / "telemetry.jsonl", 200)
        return render_template(
            "data_telemetry.html",
            **_dashboard_context(rows=rows),
        )

    @app.route("/data/escalations")
    @_auth_required
    def data_escalations():
        rows = _tail_jsonl(_data_dir() / "escalations.jsonl", 200)
        return render_template(
            "data_escalations.html",
            **_dashboard_context(rows=rows),
        )

    @app.route("/interact", methods=["GET", "POST"])
    @_auth_required
    def interact():
        result = None
        error = None
        if request.method == "POST":
            action = request.form.get("action", "draft")
            if action == "escalate":
                draft = (request.form.get("draft") or "").strip()[:2000]
                category = (request.form.get("category") or "").strip()[:80]
                from ecom_ops.escalation import EscalationService

                EscalationService().escalate_critical(
                    "Interact draft escalated from dashboard",
                    details={
                        "actor": g.actor["name"],
                        "category": category,
                        "draft": draft,
                    },
                )
                return redirect(url_for("interact") + "?msg=Eskalerat+till+Oscar")

            message = (
                request.form.get("message")
                or (request.get_json(silent=True) or {}).get("message")
                or ""
            ).strip()
            if not message:
                if request.is_json:
                    return jsonify({"ok": False, "error": "message required"}), 400
                error = "Meddelande krävs"
            else:
                try:
                    from ecom_ops.actions.support import SupportService

                    out = SupportService().handle(message, actor="agent", language="sv")
                    result = out.to_dict()
                    result["note"] = "Draft only; send kräver operator/Oscar."
                    if request.is_json:
                        return jsonify(result)
                except Exception as exc:
                    if request.is_json:
                        return jsonify({"ok": False, "error": str(exc)}), 500
                    error = str(exc)
        if request.method == "GET" and request.accept_mimetypes.best == "application/json":
            return jsonify({"hint": "POST message for support draft"})
        return render_template(
            "interact.html",
            **_dashboard_context(result=result, error=error),
        )

    @app.route("/logs")
    @_auth_required
    def logs():
        data = _data_dir()
        return jsonify(
            {
                "telemetry": _tail_jsonl(data / "telemetry.jsonl", 100),
                "escalations": _tail_jsonl(data / "escalations.jsonl", 100),
            }
        )

    @app.route("/telemetry")
    @_auth_required
    def telemetry_json():
        return jsonify(_tail_jsonl(_data_dir() / "telemetry.jsonl", 200))

    @app.route("/escalations")
    @_auth_required
    def escalations_json():
        return jsonify(_tail_jsonl(_data_dir() / "escalations.jsonl", 200))

    @app.route("/manage")
    @_auth_required
    def manage():
        if g.actor.get("is_oscar"):
            return jsonify(
                {
                    "message": "Oscar full_admin — use /oscar for admin boxes",
                    "allowed": ["settings", "secrets", "escalations", "all"],
                }
            )
        return jsonify(
            {
                "message": "Manage is limited for Jonatan. Escalate secret writes to Oscar.",
                "allowed": ["view_health", "view_logs", "view_telemetry", "edit_settings"],
                "denied": ["order_update", "product_publish", "mail_send", "ssh_write", "set_secrets"],
            }
        )
