"""Oscar-only admin routes."""

from __future__ import annotations

from urllib.parse import quote

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

from auth import _oscar_required
from secret_probes import run_all_probes, run_probe
from settings_store import (
    EDITABLE_SECRET_KEYS,
    apply_env_overlays,
    editable_secret_groups,
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
    _flash,
    _flash_q,
    _fmt_dt,
    _load_probe_last,
    _merge_probe_last,
    _normalize_escalation,
    _save_probe_last,
    _tail_jsonl,
)

_ERROR_CATEGORY_MAP = {
    "ModelAuthError": ("auth", "Autentisering (auth)"),
    "ModelConfigError": ("config", "Konfiguration (config)"),
    "ModelRateLimitError": ("rate_limit", "Hastighetsgräns (rate_limit)"),
    "ModelUnavailableError": ("unavailable", "Otillgänglig (unavailable)"),
    "ModelSchemaError": ("schema", "Schemafel (schema)"),
}


def _model_panel() -> dict:
    from ecom_ops.model_router import circuit_breaker
    from ecom_ops.model_router.profiles import load_profiles
    from ecom_ops.model_router.router import ROUTE_IDS, get_model_router, load_last_test
    from ecom_ops.model_router.xai_device_login import device_login_status

    router = get_model_router()
    catalog_profiles = load_profiles()
    query = (request.args.get("model_q") or "").strip()[:80]
    catalog_provider = (request.args.get("catalog") or "openrouter").strip().lower()
    catalog_rows: list[dict[str, str]] = []
    catalog_error = ""
    if query:
        from ecom_ops.model_router.catalog import search_models

        found = search_models(catalog_provider, query)
        catalog_rows = list(found.get("models") or [])
        catalog_error = str(found.get("error") or "")

    raw_routes = router.routes()
    formatted_routes: dict[str, dict] = {}
    for rid, info in raw_routes.items():
        row = dict(info) if isinstance(info, dict) else {}
        row["expires_at_fmt"] = _fmt_dt(row.get("expires_at"))
        formatted_routes[rid] = row

    breaker_snapshot = circuit_breaker.status_snapshot()
    route_breakers = {
        k: v for k, v in breaker_snapshot.items() if str(k).startswith("route:")
    }
    slot_breakers = []
    for k, v in breaker_snapshot.items():
        if str(k).startswith("slot:"):
            parts = str(k).split(":", 2)
            slot_breakers.append(
                {
                    "key": k,
                    "route_id": parts[1] if len(parts) > 1 else "",
                    "model_id": parts[2] if len(parts) > 2 else "",
                    **(v if isinstance(v, dict) else {}),
                }
            )
    open_breakers_count = sum(
        1
        for v in breaker_snapshot.values()
        if isinstance(v, dict) and v.get("state", "closed") != "closed"
    )

    last_test_raw = load_last_test()
    last_test: dict = {}
    if last_test_raw:
        last_test = dict(last_test_raw)
        err_cls = str(last_test.get("error_class") or "")
        cat_key, cat_label = _ERROR_CATEGORY_MAP.get(
            err_cls, ("error", err_cls) if err_cls else ("", "")
        )
        last_test.setdefault("model", "")
        last_test.setdefault("provider", "")
        last_test.setdefault("at", "")
        last_test["at_fmt"] = _fmt_dt(last_test.get("at"))
        last_test["error_category"] = cat_key
        last_test["error_category_sv"] = cat_label

    return {
        "model_routes": formatted_routes,
        "model_all_routes": list(ROUTE_IDS),
        "model_order": router.ordered_route_ids(),
        "model_preferred": router.preferred_route_id(),
        "model_last_test": last_test,
        "model_breaker": breaker_snapshot,
        "model_route_breakers": route_breakers,
        "model_slot_breakers": slot_breakers,
        "open_breakers_count": open_breakers_count,
        "model_profiles": catalog_profiles.profiles,
        "model_recommended": catalog_profiles.recommended_models,
        "reasoning_effort_options": ["", "none", "minimal", "low", "medium", "high", "xhigh"],
        "xai_login": device_login_status(),
        "model_query": query,
        "model_catalog": catalog_rows,
        "model_catalog_error": catalog_error,
        "model_catalog_provider": (
            catalog_provider if catalog_provider in {"openrouter", "xai", "openai"} else "openrouter"
        ),
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
            _flash("Okänd modellroute", "error")
            return redirect(url_for("oscar_home") + f"?{_flash_q(err='Okänd modellroute')}")
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
            _flash("Modelltest misslyckades", "error")
            return redirect(url_for("oscar_home") + f"?{_flash_q(err='Modelltest misslyckades')}")
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

            default_telemetry.record(
                action="llm_model_test",
                site="azom",
                cost_usd=float(result.cost_usd),
                actor="oscar",
                meta={
                    "route_id": result.route_id,
                    "provider": result.provider,
                    "model": result.model,
                    "cost_kind": "estimated",
                },
            )
        _flash("Modelltest ok utan fallback", "ok")
        return redirect(url_for("oscar_home") + f"?{_flash_q('Modelltest ok utan fallback')}")

    @app.route("/oscar/models/xai/login", methods=["POST"])
    @_oscar_required
    def oscar_xai_login():
        from ecom_ops.model_router.xai_device_login import start_device_login

        start_device_login()
        return redirect(url_for("oscar_home") + "#xai-login")

    @app.route("/oscar/models/xai/login/status", methods=["GET"])
    @_oscar_required
    def oscar_xai_login_status():
        from ecom_ops.model_router.xai_device_login import device_login_status
        from ecom_ops.runtime_env import env_flag

        status = device_login_status()
        return jsonify(
            {
                "status": status["status"],
                "verification_uri": status["verification_uri"],
                "user_code": status["user_code"],
                "detail": status["detail"],
                "enabled": env_flag("AZOM_XAI_GROK_BUILD"),
            }
        )

    @app.route("/oscar/models/chatgpt/disconnect", methods=["POST"])
    @_oscar_required
    def oscar_chatgpt_disconnect():
        from ecom_ops.audit import log_action
        from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

        OpenAIChatGPTOAuthRoute().disconnect()
        log_action(
            actor=g.actor["name"],
            action="chatgpt_oauth_disconnect",
            target="chatgpt_oauth",
            details={},
        )
        _flash("ChatGPT frånkopplad", "ok")
        return redirect(url_for("oscar_home") + f"?{_flash_q('ChatGPT frånkopplad')}")

    @app.route("/oscar/models/settings", methods=["POST"])
    @_oscar_required
    def oscar_model_settings():
        from ecom_ops.audit import log_action

        try:
            saved = save_model_runtime(request.form, grok_build_posted=True)
        except ValueError as exc:
            _flash(str(exc), "error")
            return redirect(url_for("oscar_home", err=str(exc)))
        if saved:
            log_action(
                actor=g.actor["name"],
                action="model_settings_save",
                target="runtime.env",
                details={"keys": saved},
            )
        msg = "Modellval sparat" if saved else "Inga ändringar"
        _flash(msg, "ok" if saved else "info")
        return redirect(url_for("oscar_home", msg=msg))

    @app.route("/oscar/models/profiles/save", methods=["POST"])
    @_oscar_required
    def oscar_model_profile_save():
        from ecom_ops.audit import log_action
        from ecom_ops.model_router.profiles import save_profile_override

        profile_id = (request.form.get("profile_id") or "").strip()
        slot_ids = request.form.getlist("slot_id")
        route_ids = request.form.getlist("route_id")
        model_ids = request.form.getlist("model_id")
        mounts = []
        for idx, (sid, rid, mid) in enumerate(zip(slot_ids, route_ids, model_ids, strict=False)):
            enabled_val = request.form.get(f"enabled_{idx}")
            mounts.append(
                {
                    "slot_id": sid,
                    "route_id": rid,
                    "model_id": mid,
                    "enabled": enabled_val in {"1", "true", "on", "yes"},
                }
            )
        payload: dict = {
            "prompt_ref": (request.form.get("prompt_ref") or "").strip(),
            "generation": {
                "max_output_tokens": request.form.get("max_output_tokens"),
                "temperature": request.form.get("temperature"),
                "reasoning_effort": (request.form.get("reasoning_effort") or "").strip(),
            },
            "timeouts": {
                "idle_s": request.form.get("idle_s"),
                "hard_s": request.form.get("hard_s"),
            },
        }
        if mounts:
            payload["mounts"] = mounts
        try:
            saved = save_profile_override(profile_id, payload)
        except (ValueError, TypeError) as exc:
            _flash(str(exc), "error")
            return redirect(url_for("oscar_home", err=str(exc)) + "#model-profiles")
        log_action(
            actor=g.actor["name"],
            action="model_profile_save",
            target=f"profile:{saved.profile_id}",
            details={
                "prompt_ref": saved.prompt_ref,
                "slots": [f"{m.slot_id}={m.route_id}:{m.model_id}" for m in saved.mounts],
            },
        )
        msg = f"Profil {saved.profile_id} sparad"
        _flash(msg, "ok")
        return redirect(url_for("oscar_home", msg=msg) + "#model-profiles")

    @app.route("/oscar/models/profiles/test", methods=["POST"])
    @_oscar_required
    def oscar_model_profile_test():
        from ecom_ops.model_router.base import ModelRequest
        from ecom_ops.model_router.errors import ModelRouteError
        from ecom_ops.model_router.router import TEST_PROMPT, get_model_router, save_last_test

        profile_id = (request.form.get("profile_id") or "").strip()
        slot_id = (request.form.get("slot_id") or "").strip()
        router = get_model_router()
        try:
            result = router.complete_slot(
                profile_id,
                slot_id,
                ModelRequest(
                    messages=[{"role": "user", "content": TEST_PROMPT}],
                    max_output_tokens=512,
                    temperature=0.0,
                    timeout=30,
                    actor="oscar",
                ),
            )
        except (KeyError, ModelRouteError) as exc:
            save_last_test(
                route_id=f"{profile_id}/{slot_id}",
                ok=False,
                error_class=type(exc).__name__,
                error_code=getattr(exc, "code", "") or "",
                model="",
                provider="",
            )
            _flash("Slottest misslyckades", "error")
            return redirect(url_for("oscar_home", err="Slottest misslyckades") + "#model-profiles")
        save_last_test(
            route_id=result.route_id,
            ok=True,
            error_class="",
            error_code="",
            model=result.model,
            provider=result.provider,
        )
        if result.cost_usd:
            from ecom_ops.telemetry import default_telemetry

            default_telemetry.record(
                action="llm_model_test",
                site="azom",
                cost_usd=float(result.cost_usd),
                actor="oscar",
                meta={
                    "route_id": result.route_id,
                    "provider": result.provider,
                    "model": result.model,
                    "profile_id": profile_id,
                    "slot_id": slot_id,
                    "cost_kind": "estimated",
                },
            )
        msg = f"Slot {profile_id}/{slot_id} ok utan fallback"
        _flash(msg, "ok")
        return redirect(url_for("oscar_home", msg=msg) + "#model-profiles")

    @app.route("/oscar/models/profiles/reset", methods=["POST"])
    @_oscar_required
    def oscar_model_profile_reset():
        from ecom_ops.audit import log_action
        from ecom_ops.model_router.profiles import reset_profile_override

        profile_id = (request.form.get("profile_id") or "").strip()
        target = None if profile_id in {"", "all"} else profile_id
        reset_profile_override(target)
        log_action(
            actor=g.actor["name"],
            action="model_profile_reset",
            target=f"profile:{target or 'all'}",
            details={},
        )
        _flash("Profilstandard återställd", "ok")
        return redirect(url_for("oscar_home", msg="Profilstandard återställd") + "#model-profiles")

    @app.route("/oscar/models/breaker/reset", methods=["POST"])
    @_oscar_required
    def oscar_model_breaker_reset():
        from ecom_ops.audit import log_action
        from ecom_ops.model_router.circuit_breaker import reset_breaker

        key = (request.form.get("breaker_key") or "").strip() or None
        reset_breaker(key)
        log_action(
            actor=g.actor["name"],
            action="model_breaker_reset",
            target=key or "all",
            details={},
        )
        _flash("Circuit breaker nollställd", "ok")
        return redirect(url_for("oscar_home", msg="Circuit breaker nollställd"))

    @app.route("/oscar/behavior", methods=["GET", "POST"])
    @_oscar_required
    def oscar_behavior():
        from ecom_ops.audit import log_action
        from ecom_ops.prompts import get_prompt, save_prompt_texts

        error = None
        submitted_drafts: dict[str, str] = {}
        if request.method == "POST":
            updates = {
                name: request.form.get(name, "")
                for name in ("chat", "classify", "draft", "product_desc", "wp_coder")
                if name in request.form
            }
            try:
                changed = save_prompt_texts(updates)
            except ValueError as exc:
                error = str(exc)
                submitted_drafts = updates
            else:
                if changed:
                    log_action(
                        actor=g.actor["name"],
                        action="prompt_save",
                        target="prompts.yaml",
                        details={"versions": changed},
                    )
                msg = "Beteende sparat" if changed else "Inga ändringar"
                _flash(msg, "ok" if changed else "info")
                return redirect(url_for("oscar_behavior", msg=msg))
        prompts = {}
        for name in ("chat", "classify", "draft", "product_desc", "wp_coder"):
            system, version = get_prompt(name)
            prompts[name] = {
                "system": submitted_drafts[name] if name in submitted_drafts else system,
                "default_system": system,
                "version": version,
            }
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
        if single.status == "error":
            msg = f"Telegram-probe fel: {single.message}"
            _flash(msg, "error")
            return redirect(url_for("oscar_telegram") + f"?{_flash_q(err=msg)}")
        if single.status in {"warn", "missing"}:
            msg = f"Telegram-probe ({single.status}): {single.message}"
            _flash(msg, "warn")
            return redirect(url_for("oscar_telegram") + f"?{_flash_q(warn=msg)}")
        _flash("Telegram-probe körd", "ok")
        return redirect(url_for("oscar_telegram") + f"?{_flash_q('Telegram-probe körd')}")

    @app.route("/oscar/secrets", methods=["GET", "POST"])
    @_oscar_required
    def oscar_secrets():
        if request.method == "POST":
            updates = {}
            for key in EDITABLE_SECRET_KEYS:
                if key in request.form:
                    updates[key] = request.form.get(key, "")
            try:
                saved = save_secrets(updates)
            except ValueError as exc:
                _flash(str(exc), "error")
                return redirect(url_for("oscar_secrets") + f"?{_flash_q(err=str(exc))}")
            if saved:
                from ecom_ops.audit import log_action

                log_action(
                    actor=g.actor["name"],
                    action="secrets_save",
                    target="secrets.env",
                    details={"keys": saved},
                )
            msg = f"Sparade {len(saved)} nycklar" if saved else "Inga ändringar"
            _flash(msg, "ok" if saved else "info")
            return redirect(url_for("oscar_secrets") + f"?{_flash_q(msg)}")
        last = _load_probe_last()
        results = list((last or {}).get("results") or [])
        if not results:
            live = run_all_probes()
            _save_probe_last(live)
            results = [r.to_dict() for r in live]
        return render_template(
            "oscar_secrets.html",
            **_dashboard_context(
                secrets=secrets_status(),
                secret_groups=editable_secret_groups(),
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
            display_results = [r.to_dict() for r in results]
        else:
            single = run_probe(probe)
            results = [single]
            display_results = _merge_probe_last(single)
        ok_n = sum(1 for r in results if r.status == "ok")
        warn_n = sum(1 for r in results if r.status == "warn")
        err_n = sum(1 for r in results if r.status in {"error", "missing"})
        if err_n > 0:
            level = "error"
            msg = f"Test klart: {ok_n} ok, {err_n} problem" + (
                f", {warn_n} varning" if warn_n else ""
            )
        elif warn_n > 0:
            level = "warn"
            msg = f"Test klart: {ok_n} ok, {warn_n} varning"
        else:
            level = "ok"
            msg = f"Test klart: {ok_n} ok, 0 problem"
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
                secret_groups=editable_secret_groups(),
                editable_keys=EDITABLE_SECRET_KEYS,
                probe_results=display_results,
                flash=f"{level}:{msg}",
            ),
        )

    @app.route("/oscar/escalations", methods=["GET", "POST"])
    @_oscar_required
    def oscar_escalations():
        if request.method == "POST":
            ticket_id = request.form.get("ticket_id", "").strip()
            show = (request.args.get("show") or "open").strip().lower()
            if show not in {"open", "resolved", "all"}:
                show = "open"
            if ticket_id and resolve_escalation(ticket_id, resolved_by=g.actor["name"]):
                from ecom_ops.audit import log_action

                log_action(
                    actor=g.actor["name"],
                    action="resolve_escalation",
                    target="escalation",
                    target_id=ticket_id,
                    details={},
                )
                _flash("Markerad som löst", "ok")
                return redirect(
                    url_for("oscar_escalations")
                    + f"?show={quote(show, safe='')}&{_flash_q('Markerad som löst')}"
                )
            _flash("Ticket hittades inte", "error")
            return redirect(
                url_for("oscar_escalations")
                + f"?show={quote(show, safe='')}&{_flash_q(err='Ticket hittades inte')}"
            )
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
            "oscar_escalations.html",
            **_dashboard_context(
                rows=rows,
                show_filter=show,
                open_count=open_count,
                resolved_count=resolved_count,
                all_count=len(all_rows),
            ),
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
