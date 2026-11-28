"""Gmail + Google marketing OAuth start/callback/status."""

from __future__ import annotations

from flask import Flask, Response, flash, jsonify, redirect, request, url_for
from flask import session as flask_session

from auth import _auth_required, _oscar_required
from support import _flash, _flash_q, _is_mock

_OPENAI_ERR_MSG_MAP = {
    "incomplete_registration": "ChatGPT OAuth saknar utfärdat client_id — prova Ny registrering",
    "client_mismatch": "ChatGPT OAuth client_id matchar inte — prova Ny registrering",
    "invalid_grant": "ChatGPT OAuth-koden har löpt ut eller redan använts — starta om inloggningen",
    "missing_callback_params": "ChatGPT OAuth saknar code eller state",
    "invalid_client": "ChatGPT OAuth client_id avvisades av OpenAI — prova Ny registrering",
    "invalid_request": "ChatGPT OAuth-begäran var ogiltig — kontrollera redirect_uri",
    "unauthorized_client": "ChatGPT OAuth-klienten har inte behörighet — prova Ny registrering",
    "access_denied": "ChatGPT OAuth nekades eller avbröts i webbläsaren",
    "state_expired": "ChatGPT OAuth-sessionen löpte ut (över 10 min) — starta om inloggningen",
    "persist_failed": "Kunde inte spara ChatGPT OAuth-sessionen till disk (behörighetsfel)",
    "token_endpoint_unavailable": "OpenAI token-endpoint svarar inte just nu — försök igen",
    "invalid_scope": "ChatGPT OAuth saknar nödvändig behörighet (scope)",
    "lock_timeout": "Kunde inte låsa ChatGPT OAuth-filen (timeout) — försök igen",
}


def _oscar_error_redirect(message: str):
    _flash(message, "error")
    return redirect(url_for("oscar_home") + f"?{_flash_q(err=message)}")


def _openai_error_message(exc: Exception) -> str:
    from ecom_ops.model_router.errors import ModelAuthError, ModelRouteError

    if isinstance(exc, ModelRouteError):
        mapped = _OPENAI_ERR_MSG_MAP.get(exc.code)
        if mapped:
            return mapped
        return "ChatGPT OAuth avvisades" if isinstance(exc, ModelAuthError) else "ChatGPT OAuth misslyckades"
    return _OPENAI_ERR_MSG_MAP["persist_failed"]


def register_oauth_routes(app: Flask) -> None:
    @app.route("/oauth/openai/start")
    @_oscar_required
    def oauth_openai_start():
        from ecom_ops.model_router.errors import ModelConfigError, ModelRouteError
        from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

        fresh = (request.args.get("fresh") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            route = OpenAIChatGPTOAuthRoute()
            url, _state = route.start_authorization(force_fresh_registration=fresh)
        except ModelConfigError:
            return _oscar_error_redirect("ChatGPT OAuth saknar konfiguration")
        except ModelRouteError as exc:
            return _oscar_error_redirect(_openai_error_message(exc))
        except OSError:
            return _oscar_error_redirect(
                "Kunde inte spara ChatGPT OAuth-state till disk (behörighetsfel)"
            )
        return redirect(url)

    @app.route("/oauth/openai/callback")
    def oauth_openai_callback():
        from ecom_ops.model_router.errors import ModelRouteError
        from ecom_ops.model_router.openai_chatgpt import (
            OpenAIChatGPTOAuthRoute,
            _safe_error_code,
        )

        err = (request.args.get("error") or "").strip()
        if err:
            safe = _safe_error_code(err) or "oauth_error"
            msg = _OPENAI_ERR_MSG_MAP.get(safe, f"ChatGPT OAuth avvisades ({safe})")
            return _oscar_error_redirect(msg)
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        issued_client_id = request.args.get("client_id", "").strip()
        if not code or not state:
            return _oscar_error_redirect(_OPENAI_ERR_MSG_MAP["missing_callback_params"])
        try:
            oauth_session = OpenAIChatGPTOAuthRoute().exchange_authorization_code(
                code=code,
                state=state,
                issued_client_id=issued_client_id or None,
            )
        except (ModelRouteError, OSError) as exc:
            return _oscar_error_redirect(_openai_error_message(exc))
        msg = (
            f"ChatGPT kopplad — ChatGPT ansluten (gen {oauth_session.generation}) "
            "— klicka Testa modell"
        )
        _flash(msg, "ok")
        return redirect(url_for("oscar_home") + f"?{_flash_q(msg)}")

    @app.route("/oauth/openai/complete-manual", methods=["POST"])
    @_oscar_required
    def oauth_openai_complete_manual():
        from flask import g

        from ecom_ops.audit import log_action
        from ecom_ops.model_router.errors import ModelRouteError
        from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

        callback_url = (request.form.get("callback_url") or "").strip()
        if not callback_url:
            return _oscar_error_redirect("Klistra in hel callback-URL")
        try:
            oauth_session = OpenAIChatGPTOAuthRoute().exchange_callback_url(callback_url)
        except (ModelRouteError, OSError) as exc:
            return _oscar_error_redirect(_openai_error_message(exc))
        log_action(
            actor=g.actor["name"],
            action="chatgpt_oauth_manual_connect",
            target="chatgpt_oauth",
            details={
                "generation": oauth_session.generation,
                "plan_usage": "chatgpt.tokens.use.direct" in oauth_session.scopes,
            },
        )
        msg = (
            f"ChatGPT kopplad — ChatGPT ansluten (gen {oauth_session.generation}) "
            "— klicka Testa modell"
        )
        _flash(msg, "ok")
        return redirect(url_for("oscar_home") + f"?{_flash_q(msg)}")

    @app.route("/oauth/google/start")
    @_oscar_required
    def oauth_google_marketing_start():
        from ecom_ops.oauth.google_marketing import (
            build_authorize_url,
            exchange_code,
            google_marketing_oauth_configured,
        )

        if (request.args.get("next") or "").strip() == "oscar_google":
            flask_session["google_oauth_next"] = "oscar_google"
        else:
            flask_session.pop("google_oauth_next", None)

        if _is_mock():
            exchange_code("mock")
            _flash("Google marketing kopplad (mock)", "ok")
            if flask_session.pop("google_oauth_next", "") == "oscar_google":
                return redirect("/oscar/google")
            return redirect(url_for("marketing_home"))
        if not google_marketing_oauth_configured():
            _flash("GOOGLE_OAUTH_CLIENT_ID/SECRET saknas", "error")
            if flask_session.pop("google_oauth_next", "") == "oscar_google":
                return redirect("/oscar/google")
            return redirect(url_for("oscar_secrets"))
        url, _state = build_authorize_url()
        return redirect(url)

    @app.route("/oauth/google/callback")
    def oauth_google_marketing_callback():
        from ecom_ops.oauth.google_marketing import (
            GoogleMarketingOAuthStore,
            exchange_code,
        )

        error = request.args.get("error")
        if error:
            return Response("OAuth error", 400, mimetype="text/plain")
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        if not code or not state:
            return Response("Missing code or state", 400)
        store = GoogleMarketingOAuthStore()
        if not store.consume_state(state):
            return Response("Invalid or expired OAuth state", 400)
        try:
            exchange_code(code)
        except Exception:
            return Response("Token exchange failed", 502, mimetype="text/plain")
        _flash("Google marketing kopplad", "ok")
        if flask_session.pop("google_oauth_next", "") == "oscar_google":
            return redirect("/oscar/google")
        return redirect(url_for("marketing_home"))

    @app.route("/oauth/google/status")
    @_oscar_required
    def oauth_google_marketing_status():
        from settings_store import apply_env_overlays

        apply_env_overlays()
        from ecom_ops.oauth.google_marketing import (
            GoogleMarketingOAuthStore,
            google_marketing_oauth_configured,
            google_marketing_redirect_uri,
        )

        store = GoogleMarketingOAuthStore()
        bundle = store.load_tokens()
        return jsonify(
            {
                "configured": google_marketing_oauth_configured(),
                "connected": store.has_tokens(),
                "email": bundle.email if bundle else None,
                "redirect_uri": google_marketing_redirect_uri(),
                "mock_mode": _is_mock(),
            }
        )

    @app.route("/oauth/gmail/start")
    @_auth_required
    def oauth_gmail_start():
        from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured

        store = GmailOAuthStore()
        if _is_mock():
            store.mock_connect()
            flash("Gmail kopplad (mock)", "ok")
            return redirect(url_for("onboarding"))
        if not gmail_oauth_configured():
            flash("MAIL_OAUTH_CLIENT_ID/SECRET saknas", "error")
            return redirect(url_for("secrets_page"))
        state = store.create_state()
        return redirect(store.build_authorize_url(state=state))

    @app.route("/oauth/gmail/callback")
    def oauth_gmail_callback():
        from ecom_ops.oauth.gmail import GmailOAuthStore

        error = request.args.get("error")
        if error:
            return Response("OAuth error", 400, mimetype="text/plain")
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        if not code or not state:
            return Response("Missing code or state", 400)
        store = GmailOAuthStore()
        if not store.validate_state(state):
            return Response("Invalid or expired OAuth state", 400)
        try:
            store.exchange_code(code)
        except Exception:
            return Response("Token exchange failed", 502, mimetype="text/plain")
        finally:
            store.clear_state()
        flash("Gmail kopplad", "ok")
        return redirect(url_for("onboarding"))

    @app.route("/oauth/gmail/status")
    @_auth_required
    def oauth_gmail_status():
        from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured

        store = GmailOAuthStore()
        bundle = store.load_tokens()
        return jsonify(
            {
                "configured": gmail_oauth_configured(),
                "connected": store.has_tokens(),
                "email": bundle.email if bundle else None,
                "mock_mode": _is_mock(),
            }
        )
