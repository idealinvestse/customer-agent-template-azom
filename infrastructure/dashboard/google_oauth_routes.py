"""Oscar-only Google (GA4 + Ads + isolated GSC) onboarding & OAuth hub routes."""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for

from auth import _oscar_required
from secret_probes import run_probe
from settings_store import apply_env_overlays, save_secrets
from support import _dashboard_context, _flash, _is_mock, _load_probe_last, _merge_probe_last

_GA4_CONFIG_KEYS = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "AZOM_GA4_PROPERTY_IDS",
    "AZOM_GADS_CUSTOMER_IDS",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "GOOGLE_ADS_DEVELOPER_TOKEN",
)

_GSC_CONFIG_KEYS = (
    "GSC_OAUTH_CLIENT_ID",
    "GSC_OAUTH_CLIENT_SECRET",
    "GSC_OAUTH_REDIRECT_URI",
    "AZOM_GSC_PROPERTY_URL_NO",
    "AZOM_GSC_PROPERTY_URL_SE",
    "AZOM_GSC_PROPERTY_URL_DK",
)


def _find_probe(probe_id: str) -> dict[str, Any] | None:
    last = _load_probe_last()
    for row in (last or {}).get("results") or []:
        if isinstance(row, dict) and row.get("id") == probe_id:
            return dict(row)
    return None


def _valid_http_url(value: str) -> bool:
    clean = value.strip()
    return clean.startswith(("https://", "http://")) and not any(ch.isspace() for ch in clean)


def _valid_gsc_property(value: str) -> bool:
    clean = value.strip()
    if not clean or any(ch.isspace() for ch in clean):
        return False
    return clean.startswith(("https://", "http://", "sc-domain:"))


def _safe_property_suggestions(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw[:25]:
        if not isinstance(item, dict):
            continue
        site_url = str(item.get("site_url") or "").strip()
        if not site_url:
            continue
        out.append(
            {
                "site_url": site_url,
                "permission_level": str(item.get("permission_level") or "").strip(),
            }
        )
    return out


def register_google_oauth_routes(app: Flask) -> None:
    """Register Oscar-only Google GA4/Ads and isolated GSC OAuth hub routes."""

    @app.route("/oscar/google", methods=["GET"])
    @_oscar_required
    def oscar_google():
        apply_env_overlays()
        from ecom_ops.integrations.gsc import gsc_kill_active, gsc_status
        from ecom_ops.oauth.google_marketing import (
            GoogleMarketingOAuthStore,
            google_marketing_oauth_configured,
            google_marketing_redirect_uri,
        )
        from ecom_ops.oauth.gsc import (
            GscOAuthStore,
            gsc_oauth_client_id,
            gsc_oauth_client_secret,
            gsc_oauth_configured,
            gsc_oauth_redirect_uri,
            list_gsc_properties,
        )
        from ecom_ops.runtime_env import env_flag

        ga4_store = GoogleMarketingOAuthStore()
        ga4_bundle = ga4_store.load_tokens()
        ga4_connected = ga4_store.has_tokens()

        gsc_store = GscOAuthStore()
        gsc_public = gsc_store.public_status()
        gsc_connected = gsc_store.has_tokens()
        gsc_st = gsc_status()

        verified_properties = _safe_property_suggestions(session.get("gsc_verified_properties"))
        if not verified_properties and _is_mock() and gsc_connected:
            try:
                verified_properties = _safe_property_suggestions(
                    list_gsc_properties("mock-gsc-access")
                )
            except Exception:
                verified_properties = []

        return render_template(
            "oscar_google.html",
            **_dashboard_context(
                public_url=os.environ.get("AZOM_DASHBOARD_PUBLIC_URL", "").strip(),
                ga4_configured=google_marketing_oauth_configured(),
                ga4_connected=ga4_connected,
                ga4_has_refresh=bool(ga4_bundle and ga4_bundle.refresh_token),
                ga4_scope=ga4_bundle.scope if ga4_bundle else "",
                ga4_redirect_uri=google_marketing_redirect_uri(),
                ga4_redirect_override=os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip(),
                ga4_client_id_present=bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()),
                ga4_client_secret_present=bool(
                    os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
                ),
                ga4_dev_token_present=bool(
                    os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
                ),
                ga4_property_ids=os.environ.get("AZOM_GA4_PROPERTY_IDS", "").strip(),
                gads_customer_ids=os.environ.get("AZOM_GADS_CUSTOMER_IDS", "").strip(),
                gads_login_customer_id=os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip(),
                ga4_probe=_find_probe("ga4"),
                gsc_configured=gsc_oauth_configured(),
                gsc_connected=gsc_connected,
                gsc_public=gsc_public,
                gsc_status=gsc_st,
                gsc_redirect_uri=gsc_oauth_redirect_uri(),
                gsc_redirect_override=os.environ.get("GSC_OAUTH_REDIRECT_URI", "").strip(),
                gsc_client_id_present=bool(gsc_oauth_client_id()),
                gsc_client_secret_present=bool(gsc_oauth_client_secret()),
                gsc_enabled=env_flag("AZOM_GSC_ENABLED"),
                gsc_kill=gsc_kill_active(),
                gsc_property_no=os.environ.get("AZOM_GSC_PROPERTY_URL_NO", "").strip(),
                gsc_property_se=os.environ.get("AZOM_GSC_PROPERTY_URL_SE", "").strip(),
                gsc_property_dk=os.environ.get("AZOM_GSC_PROPERTY_URL_DK", "").strip(),
                gsc_verified_properties=verified_properties,
                gsc_probe=_find_probe("gsc"),
            ),
        )

    @app.route("/oscar/google/ga4/config", methods=["POST"])
    @_oscar_required
    def oscar_google_ga4_config():
        from ecom_ops.audit import log_action
        from ecom_ops.marketing.config import clear_marketing_config_cache

        redirect_uri = (request.form.get("GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
        if redirect_uri and not _valid_http_url(redirect_uri):
            _flash("Ogiltig GOOGLE_OAUTH_REDIRECT_URI (måste börja med https:// eller http://)", "error")
            return redirect(url_for("oscar_google"))

        updates: dict[str, str] = {}
        for key in _GA4_CONFIG_KEYS:
            if key in request.form:
                updates[key] = request.form.get(key, "")
        try:
            saved = save_secrets(updates)
        except ValueError as exc:
            _flash(str(exc), "error")
            return redirect(url_for("oscar_google"))

        clear_marketing_config_cache()
        if saved:
            log_action(
                actor=g.actor["name"],
                action="google_ga4_config_save",
                target="secrets.env",
                details={"keys": saved},
            )
        _flash(
            f"Sparade {len(saved)} GA4/Ads-fält" if saved else "Inga ändringar för GA4/Ads",
            "ok" if saved else "info",
        )
        return redirect(url_for("oscar_google"))

    @app.route("/oscar/google/ga4/test", methods=["POST"])
    @_oscar_required
    def oscar_google_ga4_test():
        apply_env_overlays()
        single = run_probe("ga4")
        _merge_probe_last(single)
        if single.status == "error":
            _flash(f"GA4-test fel: {single.message}", "error")
        elif single.status in {"warn", "missing", "skipped"}:
            _flash(f"GA4-test ({single.status}): {single.message}", "warn")
        else:
            _flash(f"GA4-test ok: {single.message}", "ok")
        return redirect(url_for("oscar_google"))

    @app.route("/oauth/google/disconnect", methods=["POST"])
    @_oscar_required
    def oauth_google_disconnect():
        from ecom_ops.audit import log_action
        from ecom_ops.oauth.google_marketing import GoogleMarketingOAuthStore

        GoogleMarketingOAuthStore().clear()
        log_action(
            actor=g.actor["name"],
            action="google_marketing_oauth_disconnect",
            target="google_marketing_oauth",
            details={},
        )
        _flash("Google Marketing OAuth frånkopplad", "ok")
        return redirect(url_for("oscar_google"))

    @app.route("/oscar/google/gsc/config", methods=["POST"])
    @_oscar_required
    def oscar_google_gsc_config():
        from ecom_ops.audit import log_action

        redirect_uri = (request.form.get("GSC_OAUTH_REDIRECT_URI") or "").strip()
        if redirect_uri and not _valid_http_url(redirect_uri):
            _flash("Ogiltig GSC_OAUTH_REDIRECT_URI (måste börja med https:// eller http://)", "error")
            return redirect(url_for("oscar_google"))

        for prop_key in (
            "AZOM_GSC_PROPERTY_URL_NO",
            "AZOM_GSC_PROPERTY_URL_SE",
            "AZOM_GSC_PROPERTY_URL_DK",
        ):
            raw_prop = (request.form.get(prop_key) or "").strip()
            if raw_prop and not _valid_gsc_property(raw_prop):
                _flash(
                    f"Ogiltig {prop_key} (använd https://... eller sc-domain:...)",
                    "error",
                )
                return redirect(url_for("oscar_google"))

        updates: dict[str, str] = {}
        for key in _GSC_CONFIG_KEYS:
            if key in request.form:
                updates[key] = request.form.get(key, "")
        try:
            saved = save_secrets(updates)
        except ValueError as exc:
            _flash(str(exc), "error")
            return redirect(url_for("oscar_google"))

        if saved:
            log_action(
                actor=g.actor["name"],
                action="google_gsc_config_save",
                target="secrets.env",
                details={"keys": saved},
            )
        _flash(
            f"Sparade {len(saved)} GSC-fält" if saved else "Inga ändringar för GSC",
            "ok" if saved else "info",
        )
        return redirect(url_for("oscar_google"))

    @app.route("/oscar/google/gsc/test", methods=["POST"])
    @_oscar_required
    def oscar_google_gsc_test():
        from ecom_ops.integrations.gsc import _isolated_gsc_credentials
        from ecom_ops.oauth.gsc import (
            GscOAuthStore,
            list_gsc_properties,
            refresh_gsc_access_token,
        )

        apply_env_overlays()
        single = run_probe("gsc")
        _merge_probe_last(single)

        if _is_mock():
            try:
                session["gsc_verified_properties"] = _safe_property_suggestions(
                    list_gsc_properties("mock-gsc-access")
                )
            except Exception:
                pass
        else:
            creds = _isolated_gsc_credentials()
            tok = creds.get("access_token", "")
            if not tok and creds.get("refresh_token") and GscOAuthStore().has_tokens():
                try:
                    tok = refresh_gsc_access_token().access_token
                except Exception:
                    tok = ""
            if tok:
                try:
                    session["gsc_verified_properties"] = _safe_property_suggestions(
                        list_gsc_properties(tok)
                    )
                except Exception:
                    pass

        if single.status == "error":
            _flash(f"GSC-test fel: {single.message}", "error")
        elif single.status in {"warn", "missing", "skipped"}:
            _flash(f"GSC-test ({single.status}): {single.message}", "warn")
        else:
            _flash(f"GSC-test ok: {single.message}", "ok")
        return redirect(url_for("oscar_google"))

    @app.route("/oauth/gsc/disconnect", methods=["POST"])
    @_oscar_required
    def oauth_gsc_disconnect():
        from ecom_ops.audit import log_action
        from ecom_ops.oauth.gsc import GscOAuthStore

        GscOAuthStore().clear()
        session.pop("gsc_verified_properties", None)
        log_action(
            actor=g.actor["name"],
            action="google_gsc_oauth_disconnect",
            target="gsc_oauth",
            details={},
        )
        _flash("Google Search Console OAuth frånkopplad", "ok")
        return redirect(url_for("oscar_google"))

    @app.route("/oauth/gsc/start", methods=["GET"])
    @_oscar_required
    def oauth_gsc_start():
        from ecom_ops.oauth.gsc import (
            GscOAuthStore,
            build_gsc_authorize_url,
            exchange_gsc_code,
            gsc_oauth_configured,
        )

        if _is_mock():
            exchange_gsc_code("mock")
            _flash("Google Search Console kopplad (mock)", "ok")
            return redirect(url_for("oscar_google"))
        if not gsc_oauth_configured():
            _flash("GSC_OAUTH_CLIENT_ID/SECRET saknas", "error")
            return redirect(url_for("oscar_google"))
        state = GscOAuthStore().create_state()
        return redirect(build_gsc_authorize_url(state))

    @app.route("/oauth/gsc/callback", methods=["GET"])
    @_oscar_required
    def oauth_gsc_callback():
        from ecom_ops.audit import log_action
        from ecom_ops.oauth.gsc import GscOAuthStore, exchange_gsc_code

        error = request.args.get("error")
        if error:
            return Response("OAuth error", 400, mimetype="text/plain")
        code = (request.args.get("code") or "").strip()
        state = (request.args.get("state") or "").strip()
        if not code or not state:
            return Response("Missing code or state", 400, mimetype="text/plain")
        store = GscOAuthStore()
        if not store.consume_state(state):
            return Response("Invalid or expired OAuth state", 400, mimetype="text/plain")
        try:
            bundle = exchange_gsc_code(code, store=store)
        except Exception:
            return Response("Token exchange failed", 502, mimetype="text/plain")
        log_action(
            actor=g.actor["name"],
            action="google_gsc_oauth_connect",
            target="gsc_oauth",
            details={"scope": bundle.scope},
        )
        _flash("Google Search Console kopplad", "ok")
        return redirect(url_for("oscar_google"))

    @app.route("/oauth/gsc/status", methods=["GET"])
    @_oscar_required
    def oauth_gsc_status():
        apply_env_overlays()
        from ecom_ops.integrations.gsc import gsc_status
        from ecom_ops.oauth.gsc import GscOAuthStore

        status_payload = GscOAuthStore().public_status()
        status_payload["gsc_status"] = gsc_status()
        return jsonify(status_payload)
