"""Gmail + Google marketing OAuth start/callback/status."""

from __future__ import annotations

from flask import Flask, Response, jsonify, redirect, request, url_for

from auth import _auth_required, _oscar_required
from support import _is_mock


def register_oauth_routes(app: Flask) -> None:
    @app.route("/oauth/openai/start")
    @_oscar_required
    def oauth_openai_start():
        from ecom_ops.model_router.errors import ModelConfigError
        from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

        route = OpenAIChatGPTOAuthRoute()
        try:
            url, _state = route.start_authorization()
        except ModelConfigError:
            return redirect(url_for("oscar_home") + "?err=ChatGPT+OAuth+saknar+konfiguration")
        return redirect(url)

    @app.route("/oauth/openai/callback")
    def oauth_openai_callback():
        import re

        from ecom_ops.model_router.errors import ModelAuthError, ModelRouteError
        from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

        err = (request.args.get("error") or "").strip()
        if err:
            safe = err if re.fullmatch(r"[A-Za-z0-9_]{1,40}", err) else "oauth_error"
            return redirect(url_for("oscar_home") + f"?err=ChatGPT+{safe}")
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        issued_client_id = request.args.get("client_id", "").strip()
        if not code or not state:
            return redirect(url_for("oscar_home") + "?err=ChatGPT+OAuth+saknar+code")
        try:
            OpenAIChatGPTOAuthRoute().exchange_authorization_code(
                code=code,
                state=state,
                issued_client_id=issued_client_id or None,
            )
        except ModelAuthError as exc:
            err_msg_map = {
                "incomplete_registration": "ChatGPT+OAuth+saknar+utfärdat+client_id",
                "client_mismatch": "ChatGPT+OAuth+client_id+matchar+inte",
                "invalid_grant": "ChatGPT+OAuth+koden+har+löpt+ut",
                "missing_callback_params": "ChatGPT+OAuth+saknar+code",
            }
            msg = err_msg_map.get(exc.code, "ChatGPT+OAuth+avvisades")
            return redirect(url_for("oscar_home") + f"?err={msg}")
        except ModelRouteError:
            return redirect(url_for("oscar_home") + "?err=ChatGPT+OAuth+misslyckades")
        return redirect(url_for("oscar_home") + "?msg=ChatGPT+kopplad")

    @app.route("/oauth/openai/complete-manual", methods=["POST"])
    @_oscar_required
    def oauth_openai_complete_manual():
        from flask import g

        from ecom_ops.audit import log_action
        from ecom_ops.model_router.errors import ModelAuthError, ModelRouteError
        from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

        callback_url = (request.form.get("callback_url") or "").strip()
        if not callback_url:
            return redirect(url_for("oscar_home") + "?err=Klistra+in+hel+callback-URL")
        try:
            session = OpenAIChatGPTOAuthRoute().exchange_callback_url(callback_url)
        except ModelAuthError as exc:
            err_msg_map = {
                "incomplete_registration": "ChatGPT+OAuth+saknar+utfärdat+client_id",
                "client_mismatch": "ChatGPT+OAuth+client_id+matchar+inte",
                "invalid_grant": "ChatGPT+OAuth+koden+har+löpt+ut",
                "missing_callback_params": "ChatGPT+OAuth+saknar+code+eller+state",
            }
            msg = err_msg_map.get(exc.code, "ChatGPT+OAuth+avvisades")
            return redirect(url_for("oscar_home") + f"?err={msg}")
        except ModelRouteError:
            return redirect(url_for("oscar_home") + "?err=ChatGPT+OAuth+misslyckades")
        log_action(
            actor=g.actor["name"],
            action="chatgpt_oauth_manual_connect",
            target="chatgpt_oauth",
            details={
                "generation": session.generation,
                "plan_usage": "chatgpt.tokens.use.direct" in session.scopes,
            },
        )
        return redirect(url_for("oscar_home") + "?msg=ChatGPT+kopplad")

    @app.route("/oauth/google/start")
    @_oscar_required
    def oauth_google_marketing_start():
        from ecom_ops.oauth.google_marketing import (
            build_authorize_url,
            exchange_code,
            google_marketing_oauth_configured,
        )

        if _is_mock():
            exchange_code("mock")
            return redirect(
                url_for("marketing_home") + "?msg=Google+marketing+kopplad+(mock)"
            )
        if not google_marketing_oauth_configured():
            return redirect(
                url_for("oscar_secrets") + "?err=GOOGLE_OAUTH_CLIENT_ID/SECRET+saknas"
            )
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
        return redirect(url_for("marketing_home") + "?msg=Google+marketing+kopplad")

    @app.route("/oauth/google/status")
    @_oscar_required
    def oauth_google_marketing_status():
        from ecom_ops.oauth.google_marketing import (
            GoogleMarketingOAuthStore,
            google_marketing_oauth_configured,
        )

        store = GoogleMarketingOAuthStore()
        bundle = store.load_tokens()
        return jsonify(
            {
                "configured": google_marketing_oauth_configured(),
                "connected": store.has_tokens(),
                "email": bundle.email if bundle else None,
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
            return redirect(url_for("onboarding") + "?msg=Gmail+kopplad+(mock)")
        if not gmail_oauth_configured():
            return redirect(url_for("secrets_page") + "?err=MAIL_OAUTH_CLIENT_ID/SECRET+saknas")
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
        return redirect(url_for("onboarding") + "?msg=Gmail+kopplad")

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
