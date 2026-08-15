"""Gmail + Google marketing OAuth start/callback/status."""

from __future__ import annotations

from flask import Flask, Response, jsonify, redirect, request, url_for

from auth import _auth_required, _oscar_required
from support import _is_mock


def register_oauth_routes(app: Flask) -> None:
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
            return Response(f"OAuth error: {error}", 400)
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        if not code or not state:
            return Response("Missing code or state", 400)
        store = GoogleMarketingOAuthStore()
        if not store.consume_state(state):
            return Response("Invalid or expired OAuth state", 400)
        try:
            exchange_code(code)
        except Exception as exc:
            return Response(f"Token exchange failed: {exc}", 502)
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
            return Response(f"OAuth error: {error}", 400)
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        if not code or not state:
            return Response("Missing code or state", 400)
        store = GmailOAuthStore()
        if not store.validate_state(state):
            return Response("Invalid or expired OAuth state", 400)
        try:
            store.exchange_code(code)
        except Exception as exc:
            return Response(f"Token exchange failed: {exc}", 502)
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
