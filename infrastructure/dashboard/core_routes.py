"""Home, onboarding, settings, interact, metrics, and JSON tails."""

from __future__ import annotations

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for

from auth import _auth_required
from settings_store import load_settings_view, save_settings, secrets_status
from status import health_probe, runtime_status
from support import (
    _dashboard_context,
    _data_dir,
    _flash,
    _flash_q,
    _load_probe_last,
    _normalize_escalation,
    _presence_integrations,
    _tail_jsonl,
)

_SECRET_META = ("token", "secret", "password", "authorization", "api_key", "apikey")
_SAFE_COUNTER_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "input_tokens",
        "output_tokens",
    }
)


def _is_safe_counter(key: str, value: object) -> bool:
    return (
        key in _SAFE_COUNTER_KEYS
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    )


def _telemetry_rows(
    rows: list,
    *,
    action_filter: str,
    route_filter: str,
    errors_only: bool,
) -> list[dict]:
    shown: list[dict] = []
    action_q = action_filter.lower()
    route_q = route_filter.lower()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        action = str(raw.get("action") or "")
        route_id = str(meta.get("route_id") or meta.get("selected_route") or "")
        error_class = str(meta.get("error_class") or meta.get("error") or "")
        if action_q and action_q not in action.lower():
            continue
        if route_q and route_q not in route_id.lower():
            continue
        is_error = bool(error_class) or "error" in action.lower()
        if errors_only and not is_error:
            continue
        safe_meta = {
            key: value
            for key, value in meta.items()
            if _is_safe_counter(str(key), value)
            or not any(part in str(key).lower() for part in _SECRET_META)
        }
        row = dict(raw)
        row["route_id"] = route_id
        row["model"] = meta.get("model") or ""
        row["prompt_tokens"] = meta.get("prompt_tokens")
        row["completion_tokens"] = meta.get("completion_tokens")
        row["error_class"] = error_class
        row["meta"] = safe_meta
        row["safe_meta"] = safe_meta
        shown.append(row)
    return shown


def _sanitize_escalation_json(raw: dict) -> dict:
    from ecom_ops.security import redact_secrets

    norm = _normalize_escalation(raw)
    if not getattr(g, "actor", {}).get("is_oscar"):
        norm["details"] = {}
    else:
        norm["details"] = redact_secrets(norm["details"])
    return norm


def register_core_routes(app: Flask) -> None:
    @app.route("/metrics")
    def prometheus_metrics():
        import hmac
        import os
        import sqlite3

        from flask import abort

        token = (os.environ.get("METRICS_SCRAPE_TOKEN") or "").strip()
        auth = (request.headers.get("Authorization") or "").strip()
        provided = ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if (
            not token
            or len(provided) != len(token)
            or not hmac.compare_digest(provided, token)
        ):
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
        section_errors: dict[str, str] = {}
        budget = None
        try:
            from ecom_ops.budget import budget_status
            from ecom_ops.telemetry import Telemetry

            budget = budget_status(telemetry=Telemetry(path=_data_dir() / "telemetry.jsonl"))
            if budget.get("used_usd") is not None:
                cost = float(budget["used_usd"])
                cost_pct = min(100, int((cost / cap) * 100)) if cap else 0
        except Exception as exc:
            budget = None
            section_errors["budget"] = f"Budgetstatus otillgänglig ({type(exc).__name__})"
        kpis = None
        try:
            from ecom_ops.kpis import support_kpis_last_days
            from ecom_ops.telemetry import Telemetry as _Tel

            kpis = support_kpis_last_days(
                telemetry=_Tel(path=_data_dir() / "telemetry.jsonl"),
                days=7,
            )
        except Exception as exc:
            kpis = None
            section_errors["kpis"] = f"Support-KPI otillgänglig ({type(exc).__name__})"
        readiness = None
        try:
            from ecom_ops.ops_status import readiness_from_last_poll

            readiness = readiness_from_last_poll()
        except Exception as exc:
            readiness = None
            section_errors["readiness"] = f"Poll-beredskap otillgänglig ({type(exc).__name__})"
        return render_template(
            "index.html",
            **_dashboard_context(
                telemetry=_telemetry_rows(
                    telemetry, action_filter="", route_filter="", errors_only=False
                ),
                escalations=[_normalize_escalation(e) for e in escalations],
                cost_usd=round(cost, 4),
                openrouter_cap=runtime.get("openrouter_cap", 100),
                budget_cap_llm=runtime.get("budget_cap_llm", 80),
                cost_pct=cost_pct,
                integrations=presence,
                last_probe=last_probe,
                budget=budget,
                support_kpis=kpis,
                readiness=readiness,
                section_errors=section_errors,
            ),
        )

    @app.route("/onboarding")
    @_auth_required
    def onboarding():
        secs = secrets_status()
        health = health_probe()
        return render_template(
            "onboarding.html",
            **_dashboard_context(
                secrets=secs,
                health=health,
                checks=health.get("checks") or [],
                health_ok=bool(health.get("ok")),
                secrets_missing=sum(1 for s in secs if not s.get("present")),
                secrets_total=len(secs),
            ),
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
                saved = save_settings(form)
                if saved.get("mock_mode_ignored"):
                    warn_msg = "Sparat, men mock-läge är låst i live-drift (AZOM_USE_MOCK=0)"
                    _flash(warn_msg, "warn")
                    return redirect(url_for("settings") + f"?{_flash_q(warn=warn_msg)}")
                _flash("Sparat", "ok")
                return redirect(url_for("settings") + f"?{_flash_q('Sparat')}")
            except (ValueError, TypeError) as exc:
                _flash(str(exc), "error")
                return redirect(url_for("settings") + f"?{_flash_q(err=str(exc))}")
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
                _flash("Välj minst en nyckel", "error")
                return redirect(url_for("secrets_page") + f"?{_flash_q(err='Välj minst en nyckel')}")
            from ecom_ops.escalation import EscalationService

            EscalationService().escalate_critical(
                "Dashboard secret update requested by Jonatan",
                details={
                    "requested_keys": keys,
                    "note": note,
                    "actor": g.actor["name"],
                },
            )
            _flash("Begäran skickad till Oscar", "ok")
            return redirect(url_for("secrets_page") + f"?{_flash_q('Begäran skickad till Oscar')}")
        return render_template(
            "secrets.html",
            **_dashboard_context(secrets=secrets_status()),
        )

    @app.route("/data/telemetry")
    @_auth_required
    def data_telemetry():
        action_filter = (request.args.get("action") or "").strip()[:80]
        route_filter = (request.args.get("route") or "").strip()[:80]
        errors_only = request.args.get("errors") == "1"
        raw_rows = _tail_jsonl(_data_dir() / "telemetry.jsonl", 200)
        rows = _telemetry_rows(
            raw_rows,
            action_filter=action_filter,
            route_filter=route_filter,
            errors_only=errors_only,
        )
        error_count = sum(
            1
            for r in rows
            if r.get("error_class") or "error" in str(r.get("action") or "").lower()
        )
        total_cost = 0.0
        total_tokens = 0
        for r in rows:
            try:
                total_cost += float(r.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
            for tk in ("prompt_tokens", "completion_tokens"):
                val = r.get(tk)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    total_tokens += int(val)
        summary = {
            "count": len(rows),
            "error_count": error_count,
            "total_cost_usd": round(total_cost, 4),
            "total_tokens": total_tokens,
            "cap": 200,
            "capped": len(raw_rows) >= 200,
        }
        return render_template(
            "data_telemetry.html",
            **_dashboard_context(
                rows=rows,
                action_filter=action_filter,
                route_filter=route_filter,
                errors_only=errors_only,
                summary=summary,
                total_cost_usd=summary["total_cost_usd"],
                error_count=error_count,
                total_tokens=total_tokens,
                row_cap=200,
                capped=summary["capped"],
            ),
        )

    @app.route("/data/audit")
    @_auth_required
    def data_audit():
        from ecom_ops.audit import read_audit_log

        actor_filter = (request.args.get("actor") or "").strip()[:80]
        action_filter = (request.args.get("action") or "").strip()[:80]
        only_failed = (
            request.args.get("only_failed") == "1" or request.args.get("failed") == "1"
        )
        raw_rows = read_audit_log(limit=200)
        rows = []
        actor_q = actor_filter.lower()
        action_q = action_filter.lower()
        for r in raw_rows:
            if not isinstance(r, dict):
                continue
            if actor_q and actor_q not in str(r.get("actor") or "").lower():
                continue
            if action_q and action_q not in str(r.get("action") or "").lower():
                continue
            if only_failed and bool(r.get("success", True)):
                continue
            rows.append(r)
        error_count = sum(1 for r in rows if not bool(r.get("success", True)))
        summary = {
            "count": len(rows),
            "error_count": error_count,
            "cap": 200,
            "capped": len(raw_rows) >= 200,
        }
        return render_template(
            "data_audit.html",
            **_dashboard_context(
                rows=rows,
                actor_filter=actor_filter,
                action_filter=action_filter,
                only_failed=only_failed,
                summary=summary,
                error_count=error_count,
                row_cap=200,
                capped=summary["capped"],
            ),
        )

    @app.route("/data/escalations")
    @_auth_required
    def data_escalations():
        show = (request.args.get("show") or "open").strip().lower()
        if show not in {"open", "resolved", "all"}:
            show = "open"
        raw_rows = _tail_jsonl(_data_dir() / "escalations.jsonl", 200)
        all_rows = [_normalize_escalation(e) for e in raw_rows if isinstance(e, dict)]
        open_count = sum(1 for e in all_rows if e["status"] == "open")
        resolved_count = sum(1 for e in all_rows if e["status"] == "resolved")
        if show == "open":
            rows = [e for e in all_rows if e["status"] == "open"]
        elif show == "resolved":
            rows = [e for e in all_rows if e["status"] == "resolved"]
        else:
            rows = all_rows
        return render_template(
            "data_escalations.html",
            **_dashboard_context(
                rows=rows,
                show_filter=show,
                open_count=open_count,
                resolved_count=resolved_count,
                all_count=len(all_rows),
                row_cap=200,
                capped=len(raw_rows) >= 200,
            ),
        )

    @app.route("/interact", methods=["GET", "POST"])
    @_auth_required
    def interact():
        result = None
        error = None
        message = ""
        if request.method == "POST":
            action = (request.form.get("action") or "draft").strip()
            if action == "escalate":
                draft = (request.form.get("draft") or "").strip()[:2000]
                category = (request.form.get("category") or "").strip()[:80]
                orig_message = (request.form.get("message") or "").strip()[:2000]
                from ecom_ops.escalation import EscalationService

                EscalationService().escalate_critical(
                    "Interact draft escalated from dashboard",
                    details={
                        "actor": g.actor["name"],
                        "category": category,
                        "message": orig_message,
                        "draft": draft,
                    },
                )
                _flash("Eskalerat till Oscar", "ok")
                return redirect(url_for("interact") + f"?{_flash_q('Eskalerat till Oscar')}")

            message = (
                request.form.get("message")
                or (request.get_json(silent=True) or {}).get("message")
                or ""
            ).strip()[:4000]
            if not message:
                if request.is_json:
                    return jsonify({"ok": False, "error": "message required"}), 400
                error = "Meddelande krävs"
            else:
                try:
                    from ecom_ops.actions.support import SupportService

                    out = SupportService().handle(message, actor="agent", language="sv")
                    result = out.to_dict()
                    result["note"] = (
                        "Sandlådeutkast — kundmail skickas aldrig härifrån."
                        if g.actor.get("is_oscar")
                        else "Draft only; send kräver operator/Oscar."
                    )
                    if request.is_json:
                        return jsonify(result)
                except Exception as exc:
                    safe_err = (
                        str(exc)[:200]
                        if g.actor.get("is_oscar")
                        else f"Kunde inte generera utkast ({type(exc).__name__})"
                    )
                    if request.is_json:
                        return jsonify({"ok": False, "error": safe_err}), 500
                    error = safe_err
        if request.method == "GET" and request.accept_mimetypes.best == "application/json":
            return jsonify({"hint": "POST message for support draft"})
        return render_template(
            "interact.html",
            **_dashboard_context(result=result, error=error, message=message),
        )

    @app.route("/logs")
    @_auth_required
    def logs():
        data = _data_dir()
        tel = _telemetry_rows(
            _tail_jsonl(data / "telemetry.jsonl", 100),
            action_filter="",
            route_filter="",
            errors_only=False,
        )
        esc = [
            _sanitize_escalation_json(e)
            for e in _tail_jsonl(data / "escalations.jsonl", 100)
            if isinstance(e, dict)
        ]
        return jsonify({"telemetry": tel, "escalations": esc})

    @app.route("/telemetry")
    @_auth_required
    def telemetry_json():
        rows = _telemetry_rows(
            _tail_jsonl(_data_dir() / "telemetry.jsonl", 200),
            action_filter="",
            route_filter="",
            errors_only=False,
        )
        return jsonify(rows)

    @app.route("/escalations")
    @_auth_required
    def escalations_json():
        rows = [
            _sanitize_escalation_json(e)
            for e in _tail_jsonl(_data_dir() / "escalations.jsonl", 200)
            if isinstance(e, dict)
        ]
        return jsonify(rows)

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
