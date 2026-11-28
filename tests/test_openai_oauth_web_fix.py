"""Regression tests for ChatGPT OAuth web login/reconnect and Google redirect support."""

from __future__ import annotations

import base64
import importlib.util
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import responses

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    spec = importlib.util.spec_from_file_location("azom_dashboard_oauth_fix", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_oauth_fix"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _auth(user: str = "oscar", password: str = "oscar", *, client=None):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if client is not None:
        client.get("/oscar", headers=headers)
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token")
        if csrf:
            headers["X-CSRF-Token"] = csrf
    return headers


def _setup_client(tmp_path, monkeypatch, *, mock: str = "1"):
    monkeypatch.setenv("AZOM_USE_MOCK", mock)
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "0")
    if mock == "0":
        monkeypatch.setenv("DASHBOARD_PASSWORD", "jonatan")
        monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "oscar")
    else:
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
        monkeypatch.delenv("DASHBOARD_OSCAR_PASSWORD", raising=False)
    for key in (
        "OPENAI_SIWC_CLIENT_ID",
        "OPENAI_SIWC_REDIRECT_URI",
        "OPENAI_SIWC_AUTHORIZATION_URL",
        "OPENAI_SIWC_TOKEN_URL",
        "OPENAI_SIWC_HOST_ID",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REDIRECT_URI",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_is_loopback_redirect_uri_and_status_flags(tmp_path, monkeypatch):
    from ecom_ops.model_router.openai_chatgpt import (
        OpenAIChatGPTOAuthRoute,
        is_loopback_redirect_uri,
    )
    from ecom_ops.model_router.store import ModelSession

    assert is_loopback_redirect_uri("http://127.0.0.1:8080/oauth/openai/callback") is True
    assert is_loopback_redirect_uri("http://localhost:8080/oauth/openai/callback") is True
    assert is_loopback_redirect_uri("http://[::1]:8080/oauth/openai/callback") is True
    assert is_loopback_redirect_uri("https://dashboard.azom.no/oauth/openai/callback") is False
    assert is_loopback_redirect_uri("") is False
    assert is_loopback_redirect_uri("not a url") is False

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    for key in (
        "OPENAI_SIWC_CLIENT_ID",
        "OPENAI_SIWC_REDIRECT_URI",
        "OPENAI_SIWC_AUTHORIZATION_URL",
        "OPENAI_SIWC_TOKEN_URL",
        "OPENAI_SIWC_HOST_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    route = OpenAIChatGPTOAuthRoute()
    st_empty = route.status()
    assert st_empty["connected"] is False
    assert st_empty["has_saved_session"] is False
    assert st_empty["redirect_is_loopback"] is True

    route.store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="oaiapp_saved",
            access_token="tok",
            refresh_token="ref",
            expires_at=time.time() + 3600,
            generation=2,
            status="connected",
            needs_reauth=False,
        )
    )
    st_conn = route.status()
    assert st_conn["connected"] is True
    assert st_conn["has_saved_session"] is True
    assert st_conn["redirect_is_loopback"] is True


def test_force_fresh_registration_overrides_stale_client_id(tmp_path, monkeypatch):
    from ecom_ops.model_router.openai_chatgpt import DYNAMIC_CLIENT_ID, OpenAIChatGPTOAuthRoute
    from ecom_ops.model_router.store import ModelSession

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    for key in (
        "OPENAI_SIWC_CLIENT_ID",
        "OPENAI_SIWC_REDIRECT_URI",
        "OPENAI_SIWC_AUTHORIZATION_URL",
        "OPENAI_SIWC_TOKEN_URL",
        "OPENAI_SIWC_HOST_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    route = OpenAIChatGPTOAuthRoute()
    route.store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="oaiapp_stale",
            access_token="tok",
            refresh_token="ref",
            expires_at=time.time() + 3600,
            generation=1,
            status="connected",
        )
    )

    reuse_url, _ = route.start_authorization()
    reuse_q = parse_qs(urlparse(reuse_url).query)
    assert reuse_q["client_id"][0] == "oaiapp_stale"
    assert "agent_name_hint" not in reuse_q

    fresh_url, _ = route.start_authorization(force_fresh_registration=True)
    fresh_q = parse_qs(urlparse(fresh_url).query)
    assert fresh_q["client_id"][0] == DYNAMIC_CLIENT_ID
    assert fresh_q["agent_name_hint"][0] == "Azom"


@responses.activate
def test_exchange_callback_url_accepts_quotes_angle_brackets_and_bare_queries(tmp_path, monkeypatch):
    from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENAI_SIWC_CLIENT_ID", "dynamic_agent_client")
    monkeypatch.setenv("OPENAI_SIWC_REDIRECT_URI", "http://127.0.0.1:8080/oauth/openai/callback")
    monkeypatch.setenv("OPENAI_SIWC_AUTHORIZATION_URL", "https://auth.example/authorize")
    monkeypatch.setenv("OPENAI_SIWC_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv("OPENAI_SIWC_HOST_ID", "urn:uuid:11111111-1111-4111-8111-111111111111")

    route = OpenAIChatGPTOAuthRoute()
    variants = [
        lambda s: f'  "<http://127.0.0.1:8080/oauth/openai/callback?code=c1&state={s}&client_id=oaiapp_1>"  ',
        lambda s: f"'?code=c2&state={s}&client_id=oaiapp_1'",
        lambda s: f"code=c3&state={s}&client_id=oaiapp_1",
    ]
    for idx, make_input in enumerate(variants, start=1):
        _url, state = route.start_authorization(force_fresh_registration=True)
        responses.add(
            responses.POST,
            "https://auth.example/token",
            json={
                "access_token": f"acc-{idx}",
                "refresh_token": f"ref-{idx}",
                "expires_in": 3600,
                "scope": "openid offline_access chatgpt.tokens.use.direct",
            },
            status=200,
        )
        sess = route.exchange_callback_url(make_input(state))
        assert sess.generation == idx
        assert sess.client_id == "oaiapp_1"


@responses.activate
def test_token_request_extracts_safe_oauth_error_codes(tmp_path, monkeypatch):
    from ecom_ops.model_router.errors import ModelAuthError
    from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENAI_SIWC_CLIENT_ID", "azom-client")
    monkeypatch.setenv("OPENAI_SIWC_REDIRECT_URI", "http://127.0.0.1:8080/oauth/openai/callback")
    monkeypatch.setenv("OPENAI_SIWC_AUTHORIZATION_URL", "https://auth.example/authorize")
    monkeypatch.setenv("OPENAI_SIWC_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv("OPENAI_SIWC_HOST_ID", "urn:uuid:11111111-1111-4111-8111-111111111111")

    route = OpenAIChatGPTOAuthRoute()
    for err_code in ("invalid_grant", "invalid_client", "invalid_request", "unauthorized_client", "invalid_scope"):
        _url, state = route.start_authorization()
        responses.add(
            responses.POST,
            "https://auth.example/token",
            json={"error": err_code, "error_description": "SECRET_RAW_BODY_TEXT"},
            status=400,
        )
        with pytest.raises(ModelAuthError) as exc_info:
            route.exchange_authorization_code(code="code-1", state=state)
        assert exc_info.value.code == err_code
        assert "SECRET_RAW_BODY_TEXT" not in str(exc_info.value)

    _url, state = route.start_authorization()
    responses.add(
        responses.POST,
        "https://auth.example/token",
        json={"error": "<script>bad code</script>"},
        status=400,
    )
    with pytest.raises(ModelAuthError) as unsafe_exc:
        route.exchange_authorization_code(code="code-1", state=state)
    assert unsafe_exc.value.code == ""


def test_openai_start_catches_oserror_and_supports_fresh(tmp_path, monkeypatch):
    from ecom_ops.model_router.openai_chatgpt import DYNAMIC_CLIENT_ID, OpenAIChatGPTOAuthRoute
    from ecom_ops.model_router.store import ModelSession

    client = _setup_client(tmp_path, monkeypatch)

    # 1. OSError (e.g. root-owned model_oauth dir) redirects gracefully, never 500
    def _boom_start(self, **_kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(OpenAIChatGPTOAuthRoute, "start_authorization", _boom_start)
    resp = client.get("/oauth/openai/start", headers=_auth(), follow_redirects=False)
    assert resp.status_code == 302
    assert "/oscar" in resp.headers["Location"]
    assert "err=" in resp.headers["Location"]
    page = client.get("/oscar", headers=_auth())
    assert page.status_code == 200
    assert "behörighetsfel" in page.get_data(as_text=True)

    # Restore start_authorization and test ?fresh=1
    monkeypatch.undo()
    client = _setup_client(tmp_path, monkeypatch)
    OpenAIChatGPTOAuthRoute().store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="oaiapp_old",
            access_token="tok",
            refresh_token="ref",
            expires_at=time.time() + 3600,
            generation=1,
            status="connected",
        )
    )
    fresh_resp = client.get("/oauth/openai/start?fresh=1", headers=_auth(), follow_redirects=False)
    assert fresh_resp.status_code == 302
    loc_q = parse_qs(urlparse(fresh_resp.headers["Location"]).query)
    assert loc_q["client_id"][0] == DYNAMIC_CLIENT_ID
    assert loc_q["agent_name_hint"][0] == "Azom"


@pytest.mark.parametrize(
    ("code", "expected_substr"),
    [
        ("invalid_client", "Ny registrering"),
        ("invalid_request", "redirect_uri"),
        ("unauthorized_client", "behörighet"),
        ("access_denied", "nekades"),
        ("state_expired", "löpte ut"),
        ("persist_failed", "behörighetsfel"),
        ("token_endpoint_unavailable", "svarar inte"),
    ],
)
def test_openai_callback_and_manual_map_all_errors_and_flash(tmp_path, monkeypatch, code, expected_substr):
    from ecom_ops.model_router.errors import ModelAuthError, ModelUnavailableError
    from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

    client = _setup_client(tmp_path, monkeypatch)
    exc_cls = ModelUnavailableError if code == "token_endpoint_unavailable" else ModelAuthError

    def _raise_cb(*_a, **_kw):
        raise exc_cls("err", code=code)

    monkeypatch.setattr(OpenAIChatGPTOAuthRoute, "exchange_authorization_code", _raise_cb)
    monkeypatch.setattr(OpenAIChatGPTOAuthRoute, "exchange_callback_url", _raise_cb)

    cb_resp = client.get(
        "/oauth/openai/callback",
        query_string={"code": "abc", "state": "xyz"},
        headers=_auth(),
        follow_redirects=False,
    )
    assert cb_resp.status_code == 302
    assert "err=" in cb_resp.headers["Location"]
    page = client.get("/oscar", headers=_auth())
    assert expected_substr in page.get_data(as_text=True)

    man_resp = client.post(
        "/oauth/openai/complete-manual",
        headers=_auth(client=client),
        data={"callback_url": "code=abc&state=xyz"},
        follow_redirects=True,
    )
    assert man_resp.status_code == 200
    assert expected_substr in man_resp.get_data(as_text=True)


@responses.activate
def test_openai_callback_success_includes_generation_and_oscar_ui_state(tmp_path, monkeypatch):
    from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

    client = _setup_client(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_SIWC_CLIENT_ID", "dynamic_agent_client")
    monkeypatch.setenv("OPENAI_SIWC_REDIRECT_URI", "http://127.0.0.1:8080/oauth/openai/callback")
    monkeypatch.setenv("OPENAI_SIWC_AUTHORIZATION_URL", "https://auth.example/authorize")
    monkeypatch.setenv("OPENAI_SIWC_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv("OPENAI_SIWC_HOST_ID", "urn:uuid:11111111-1111-4111-8111-111111111111")

    # Disconnected state on remote VPS host
    disc = client.get("/oscar", base_url="https://dashboard.azom.no", headers=_auth())
    assert disc.status_code == 200
    disc_html = disc.get_data(as_text=True)
    assert "/oscar/google" in disc_html
    assert "Google GA4 &amp; GSC" in disc_html
    assert "ChatGPT ej ansluten" in disc_html
    assert "ChatGPT ansluten" not in disc_html
    assert "Anslut ChatGPT" in disc_html
    assert "Återanslut ChatGPT" not in disc_html
    assert 'target="_blank" rel="noopener noreferrer"' in disc_html
    assert "Sidan kan inte nås" in disc_html
    assert re.search(r"<details[^>]*\bopen\b", disc_html)

    # Zero inline script / event handlers / | safe
    oscar_tmpl = (DASH_DIR / "templates" / "oscar.html").read_text(encoding="utf-8")
    assert "<script" not in oscar_tmpl.lower()
    assert not re.search(r"\son[a-z]+\s*=", oscar_tmpl, re.IGNORECASE)
    assert "| safe" not in oscar_tmpl

    # Start -> pending state
    start = client.get("/oauth/openai/start", headers=_auth(), follow_redirects=False)
    state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
    pending_page = client.get("/oscar", headers=_auth())
    assert "Väntar på callback-URL" in pending_page.get_data(as_text=True)

    # Complete callback -> gen 1 + connected UI
    responses.add(
        responses.POST,
        "https://auth.example/token",
        json={
            "access_token": "acc-ok-1",
            "refresh_token": "ref-ok-1",
            "expires_in": 3600,
            "scope": "openid offline_access chatgpt.tokens.use.direct",
        },
        status=200,
    )
    done = client.get(
        "/oauth/openai/callback",
        query_string={"code": "code-ok", "state": state, "client_id": "oaiapp_web"},
        headers=_auth(),
        follow_redirects=True,
    )
    assert done.status_code == 200
    done_html = done.get_data(as_text=True)
    assert "ChatGPT ansluten (gen 1) — klicka Testa modell" in done_html
    assert "ChatGPT kopplad" in done_html
    assert "Återanslut ChatGPT" in done_html
    assert "Ny registrering" in done_html
    assert "Koppla från ChatGPT" in done_html

    # Needs reauth state
    route = OpenAIChatGPTOAuthRoute()
    sess = route.store.load()
    assert sess is not None
    route._mark_needs_reauth(sess)
    reauth_html = client.get("/oscar", headers=_auth()).get_data(as_text=True)
    assert "ChatGPT behöver återanslutas" in reauth_html
    assert "Anslut ChatGPT" in reauth_html
    assert "Ny registrering" in reauth_html


@responses.activate
def test_google_oauth_next_oscar_google_redirect_preserves_marketing_default(tmp_path, monkeypatch):
    from ecom_ops.oauth.google_marketing import GoogleMarketingOAuthStore

    # 1. Mock mode default -> /marketing
    client = _setup_client(tmp_path, monkeypatch, mock="1")
    r_default = client.get("/oauth/google/start", headers=_auth(), follow_redirects=False)
    assert r_default.status_code == 302
    assert "/marketing" in r_default.headers["Location"]

    # 2. Mock mode ?next=oscar_google -> /oscar/google
    r_next = client.get("/oauth/google/start?next=oscar_google", headers=_auth(), follow_redirects=False)
    assert r_next.status_code == 302
    assert r_next.headers["Location"].endswith("/oscar/google")

    # 3. Live unconfigured ?next=oscar_google -> /oscar/google
    live_client = _setup_client(tmp_path, monkeypatch, mock="0")
    r_unconf = live_client.get("/oauth/google/start?next=oscar_google", headers=_auth(), follow_redirects=False)
    assert r_unconf.status_code == 302
    assert r_unconf.headers["Location"].endswith("/oscar/google")

    # 4. Live configured ?next=oscar_google -> Google auth -> callback -> /oscar/google
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "g-csec")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://dashboard.azom.no/oauth/google/callback")

    r_live_start = live_client.get("/oauth/google/start?next=oscar_google", headers=_auth(), follow_redirects=False)
    assert r_live_start.status_code == 302
    assert r_live_start.headers["Location"].startswith("https://accounts.google.com/")
    state = parse_qs(urlparse(r_live_start.headers["Location"]).query)["state"][0]

    responses.add(
        responses.POST,
        "https://oauth2.googleapis.com/token",
        json={"access_token": "ga-acc", "refresh_token": "ga-ref", "expires_in": 3600, "token_type": "Bearer"},
        status=200,
    )
    r_cb_oscar = live_client.get(
        "/oauth/google/callback",
        query_string={"code": "g-code", "state": state},
        follow_redirects=False,
    )
    assert r_cb_oscar.status_code == 302
    assert r_cb_oscar.headers["Location"].endswith("/oscar/google")

    # 5. Subsequent default callback without next=oscar_google redirects to /marketing
    plain_state = GoogleMarketingOAuthStore().create_state()
    responses.add(
        responses.POST,
        "https://oauth2.googleapis.com/token",
        json={"access_token": "ga-acc-2", "refresh_token": "ga-ref-2", "expires_in": 3600, "token_type": "Bearer"},
        status=200,
    )
    r_cb_mkt = live_client.get(
        "/oauth/google/callback",
        query_string={"code": "g-code-2", "state": plain_state},
        follow_redirects=False,
    )
    assert r_cb_mkt.status_code == 302
    assert "/marketing" in r_cb_mkt.headers["Location"]
