"""Oscar-only admin routes."""

from __future__ import annotations

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

from auth import _oscar_required
from secret_probes import run_all_probes, run_probe
from settings_store import (
    EDITABLE_SECRET_KEYS,
    apply_env_overlays,
    load_settings_view,
    resolve_escalation,
    save_model_runtime,
    save_secrets,
    secrets_file_has_key,
    secrets_status,
)
from support import (
    _dashboard_context,
    _data_dir,
    _load_probe_last,
    _merge_probe_last,
    _save_probe_last,
    _tail_jsonl,
)


def _model_panel() -> dict:
    from ecom_ops.model_router.router import get_model_router, load_last_test

    router = get_model_router()
    return {
        "model_routes": router.routes(),
        "model_order": router.ordered_route_ids(),
        "model_preferred": router.preferred_route_id(),
        "model_last_test": load_last_test(),
    }


def register_oscar_routes(app: Flask) -> None:
    @app.route("/oscar")
    @_oscar_required
    def oscar_home():
        esc = _tail_jsonl(_data_dir() / "escalations.jsonl", 100)
        open_esc_rows = [e for e in esc if e.get("status", "open") == "open"]
        secs = secrets_status()
        present = sum(1 for s in secs if s["present"])
        missing = sum(1 for s in secs if not s["present"])
        from ecom_ops.oauth.gmail import GmailOAuthStore

        store = GmailOAuthStore()
        return render_template(
            "oscar.html",
            **_dashboard_context(
                open_escalation_tickets=open_esc_rows,
                open_count=len(open_esc_rows),
                secrets_present=present,
                secrets_missing=missing,
                gmail_connected=store.has_tokens(),
                settings=load_settings_view(),
                **_model_panel(),
            ),
        )

    @app.route("/oscar/models/test", methods=["POST"])
    @_oscar_required
    def oscar_model_test():
        from ecom_ops.model_router.base import ModelRequest
        from ecom_ops.model_router.errors import ModelRouteError
        from ecom_ops.model_router.router import (
            ROUTE_IDS,
            TEST_PROMPT,
            get_model_router,
            save_last_test,
        )

        route_id = (request.form.get("route_id") or "").strip()
        if route_id not in ROUTE_IDS:
            return redirect(url_for("oscar_home") + "?err=Okand+modellroute")
        router = get_model_router()
        try:
            result = router.complete_route(
                route_id,
                ModelRequest(
                    messages=[{"role": "user", "content": TEST_PROMPT}],
                    max_output_tokens=512,
                    temperature=0.0,
                    timeout=30,
                    actor="oscar",
                ),
            )
        except ModelRouteError as exc:
            save_last_test(
                route_id=route_id,
                ok=False,
                error_class=type(exc).__name__,
                error_code=exc.code,
                model="",
                provider="",
            )
            return redirect(url_for("oscar_home") + "?err=Modelltest+misslyckades")
        save_last_test(
            route_id=route_id,
            ok=True,
            error_class="",
            error_code="",
            model=result.model,
            provider=result.provider,
        )
        if result.cost_usd:
            from ecom_ops.telemetry import default_telemetry

            default_telemetry().record(
                action="llm_model_test",
                cost_usd=float(result.cost_usd),
                actor="oscar",
                meta={
                    "route_id": result.route_id,
                    "provider": result.provider,
                    "model": result.model,
                    "cost_kind": "estimated",
                },
            )
        return redirect(url_for("oscar_home") + "?msg=Modelltest+ok+utan+fallback")

    @app.route("/oscar/models/chatgpt/disconnect", methods=["POST"])
    @_oscar_required
    def oscar_chatgpt_disconnect():
        from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

        OpenAIChatGPTOAuthRoute().disconnect()
        return redirect(url_for("oscar_home") + "?msg=ChatGPT+fran+kopplad")

    @app.route("/oscar/models/settings", methods=["POST"])
    @_oscar_required
    def oscar_model_settings():
        from ecom_ops.audit import log_action

        try:
            saved = save_model_runtime(request.form, grok_build_posted=True)
        except ValueError as exc:
            return redirect(url_for("oscar_home", err=str(exc)))
        if saved:
            log_action(
                actor=g.actor["name"],
                action="model_settings_save",
                target="runtime.env",
                details={"keys": saved},
            )
        msg = "Modellval sparat" if saved else "Inga ändringar"
        return redirect(url_for("oscar_home", msg=msg))

    @app.route("/oscar/behavior", methods=["GET", "POST"])
    @_oscar_required
    def oscar_behavior():
        from ecom_ops.audit import log_action
        from ecom_ops.prompts import get_prompt, save_prompt_texts

        error = None
        if request.method == "POST":
            try:
                changed = save_prompt_texts(
                    {
                        "chat": request.form.get("chat", ""),
                        "classify": request.form.get("classify", ""),
                        "draft": request.form.get("draft", ""),
                    }
                )
            except ValueError as exc:
                error = str(exc)
            else:
                if changed:
                    log_action(
                        actor=g.actor["name"],
                        action="prompt_save",
                        target="prompts.yaml",
                        details={"versions": changed},
                    )
                msg = "Beteende sparat" if changed else "Inga ändringar"
                return redirect(url_for("oscar_behavior", msg=msg))
        prompts = {}
        for name in ("chat", "classify", "draft"):
            system, version = get_prompt(name)
            prompts[name] = {"system": system, "version": version}
        return render_template(
            "oscar_behavior.html",
            **_dashboard_context(prompts=prompts, error=error),
        )

    @app.route("/oscar/telegram")
    @_oscar_required
    def oscar_telegram():
        import os

        apply_env_overlays()
        allow_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        actor_raw = os.environ.get("TELEGRAM_ACTOR_MAP", "")
        last = _load_probe_last()
        probe = None
        for row in (last or {}).get("results") or []:
            if isinstance(row, dict) and row.get("id") == "telegram":
                probe = dict(row)
                message = str(probe.get("message") or "")
                if "api.telegram.org/bot" in message:
                    probe["message"] = "getMe failed"
                break

        def _count(raw: str) -> int:
            return len([part for part in str(raw).replace(";", ",").split(",") if part.strip()])

        return render_template(
            "oscar_telegram.html",
            **_dashboard_context(
                token_present=bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
                allow_count=_count(allow_raw),
                actor_count=_count(actor_raw),
                allow_in_secrets_file=secrets_file_has_key("TELEGRAM_ALLOWED_CHAT_IDS"),
                actor_in_secrets_file=secrets_file_has_key("TELEGRAM_ACTOR_MAP"),
                probe=probe,
            ),
        )

    @app.route("/oscar/telegram/probe", methods=["POST"])
    @_oscar_required
    def oscar_telegram_probe():
        single = run_probe("telegram")
        _merge_probe_last(single)
        return redirect(url_for("oscar_telegram", msg="Telegram-probe körd"))

    @app.route("/oscar/secrets", methods=["GET", "POST"])
    @_oscar_required
    def oscar_secrets():
        if request.method == "POST":
            updates = {}
            for key in EDITABLE_SECRET_KEYS:
                if key in request.form:
                    updates[key] = request.form.get(key, "")
            saved = save_secrets(updates)
            msg = f"Sparade+{len(saved)}+nycklar" if saved else "Inga+ändringar"
            return redirect(url_for("oscar_secrets") + f"?msg={msg}")
        last = _load_probe_last()
        results = (last or {}).get("results") or [r.to_dict() for r in run_all_probes()]
        if not last:
            live = run_all_probes()
            _save_probe_last(live)
            results = [r.to_dict() for r in live]
        return render_template(
            "oscar_secrets.html",
            **_dashboard_context(
                secrets=secrets_status(),
                editable_keys=EDITABLE_SECRET_KEYS,
                probe_results=results,
            ),
        )

    @app.route("/oscar/secrets/test", methods=["POST"])
    @_oscar_required
    def oscar_secrets_test():
        apply_env_overlays()
        probe = (request.form.get("probe") or "all").strip().lower()
        if probe in {"", "all"}:
            results = run_all_probes()
            _save_probe_last(results)
        else:
            single = run_probe(probe)
            results = [single]
            _merge_probe_last(single)
        ok_n = sum(1 for r in results if r.status == "ok")
        err_n = sum(1 for r in results if r.status in {"error", "missing"})
        msg = f"Test+klart:+{ok_n}+ok,+{err_n}+problem"
        if request.headers.get("Accept") == "application/json" or request.is_json:
            return jsonify(
                {
                    "ok": err_n == 0,
                    "results": [r.to_dict() for r in results],
                }
            )
        return render_template(
            "oscar_secrets.html",
            **_dashboard_context(
                secrets=secrets_status(),
                editable_keys=EDITABLE_SECRET_KEYS,
                probe_results=[r.to_dict() for r in results],
                flash=f"ok:{msg.replace('+', ' ')}",
            ),
        )

    @app.route("/oscar/escalations", methods=["GET", "POST"])
    @_oscar_required
    def oscar_escalations():
        if request.method == "POST":
            ticket_id = request.form.get("ticket_id", "").strip()
            show = request.args.get("show", "open")
            if ticket_id and resolve_escalation(ticket_id):
                return redirect(
                    url_for("oscar_escalations", show=show) + "?msg=Markerad+som+löst"
                )
            return redirect(
                url_for("oscar_escalations", show=show) + "?err=Ticket+hittades+inte"
            )
        show = request.args.get("show", "open")
        rows = _tail_jsonl(_data_dir() / "escalations.jsonl", 200)
        if show != "all":
            rows = [e for e in rows if e.get("status", "open") == "open"]
        return render_template(
            "oscar_escalations.html",
            **_dashboard_context(rows=rows, show_filter=show),
        )

    @app.route("/oscar/gdpr/delete", methods=["POST"])
    @_oscar_required
    def oscar_gdpr_delete():
        email = (request.form.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "message": "email required"}), 400
        from ecom_ops.audit import log_action
        from ecom_ops.cases.retention import gdpr_delete

        result = gdpr_delete(email=email, db_path=_data_dir() / "cases.db")
        if not result.get("ok"):
            status = 400 if "Invalid email" in str(result.get("message") or "") else 404
            if "not found" in str(result.get("message") or ""):
                status = 404
            return jsonify(result), status
        log_action(
            actor=g.actor["name"],
            action="gdpr_delete",
            target="cases",
            target_id=email,
            details={"deleted_count": result.get("deleted", 0)},
        )
        return jsonify(result)

    @app.route("/oscar/gdpr/export", methods=["GET"])
    @_oscar_required
    def oscar_gdpr_export():
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "message": "email required"}), 400
        from ecom_ops.audit import log_action
        from ecom_ops.cases.retention import gdpr_export

        result = gdpr_export(email=email, db_path=_data_dir() / "cases.db")
        if not result.get("ok"):
            status = 400 if "Invalid email" in str(result.get("message") or "") else 404
            return jsonify(result), status
        log_action(
            actor=g.actor["name"],
            action="gdpr_export",
            target="cases",
            target_id=email,
            details={"case_count": len(result.get("cases") or [])},
        )
        return jsonify(result)
