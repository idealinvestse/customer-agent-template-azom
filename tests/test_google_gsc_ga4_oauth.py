"""Tests for GA4 & isolated GSC OAuth hub, onboarding UI, probes, and settings."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import socket
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


class _StrictHtmlAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_events: list[tuple[str, str]] = []
        self.bad_scripts: list[str] = []
        self._in_non_json_script = False
        self._script_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        for key in attr_map:
            if key.startswith("on"):
                self.inline_events.append((tag, key))
        if tag.lower() == "script":
            stype = attr_map.get("type", "").strip().lower()
            if stype != "application/json":
                self._in_non_json_script = True
                self._script_buf = []
                if not attr_map.get("src", "").strip():
                    self.bad_scripts.append(f"<script {attr_map}> missing src")

    def handle_data(self, data: str) -> None:
        if self._in_non_json_script:
            self._script_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_non_json_script:
            body = "".join(self._script_buf).strip()
            if body:
                self.bad_scripts.append(f"inline script: {body[:60]!r}")
            self._in_non_json_script = False
            self._script_buf = []


@pytest.fixture(autouse=True)
def _block_live_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Live network socket connection attempted in offline test")

    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


def _clear_google_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AZOM_DASHBOARD_PUBLIC_URL",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REDIRECT_URI",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "AZOM_GA4_PROPERTY_IDS",
        "AZOM_GADS_CUSTOMER_IDS",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "MAIL_OAUTH_CLIENT_ID",
        "MAIL_OAUTH_CLIENT_SECRET",
        "GMAIL_REFRESH_TOKEN",
        "GSC_OAUTH_CLIENT_ID",
        "GSC_OAUTH_CLIENT_SECRET",
        "GSC_OAUTH_REDIRECT_URI",
        "GSC_OAUTH_REFRESH_TOKEN",
        "GSC_OAUTH_ACCESS_TOKEN",
        "AZOM_GSC_CLIENT_ID",
        "AZOM_GSC_CLIENT_SECRET",
        "AZOM_GSC_REDIRECT_URI",
        "AZOM_GSC_REFRESH_TOKEN",
        "AZOM_GSC_ACCESS_TOKEN",
        "AZOM_GSC_ENABLED",
        "AZOM_GSC_KILL",
        "AZOM_GSC_PROPERTY_URL_NO",
        "AZOM_GSC_PROPERTY_URL_SE",
        "AZOM_GSC_PROPERTY_URL_DK",
    ):
        monkeypatch.delenv(key, raising=False)


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    for name in (
        "azom_dashboard_google_oauth_test",
        "auth",
        "cases_routes",
        "core_routes",
        "google_oauth_routes",
        "health_routes",
        "marketing_routes",
        "oauth_routes",
        "oscar_routes",
        "research_routes",
        "seo_routes",
        "secret_probes",
        "settings_store",
        "shop_intel_routes",
        "status",
        "support",
        "webhooks",
        "wp",
        "wp_routes",
    ):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "azom_dashboard_google_oauth_test", DASH_DIR / "app.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_google_oauth_test"] = mod
    spec.loader.exec_module(mod)
    app = mod.create_app()
    app.config["TESTING"] = True
    return app


def _basic_auth(user: str, pw: str) -> dict[str, str]:
    tok = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
    return {"Authorization": f"Basic {tok}"}


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "Expected hidden _csrf input"
    return m.group(1)


@pytest.fixture()
def dash_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_google_env(monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jon-pw")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "osc-pw")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-google-oauth-secret-key")
    monkeypatch.setenv("AZOM_DISABLE_BACKGROUND_WORKERS", "1")

    app = _load_dashboard_app()
    return {
        "client": app.test_client(),
        "jonatan": _basic_auth("jonatan", "jon-pw"),
        "oscar": _basic_auth("oscar", "osc-pw"),
        "data_dir": tmp_path / "data",
    }


def test_google_marketing_redirect_uri_and_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ecom_ops.oauth.google_marketing import (
        GoogleMarketingOAuthStore,
        GoogleMarketingTokenBundle,
        google_marketing_redirect_uri,
    )

    _clear_google_env(monkeypatch)
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))

    assert google_marketing_redirect_uri() == "http://127.0.0.1:8080/oauth/google/callback"

    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dashboard.azom.no/")
    assert google_marketing_redirect_uri() == "https://dashboard.azom.no/oauth/google/callback"

    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://custom.azom.no/cb")
    assert google_marketing_redirect_uri() == "https://custom.azom.no/cb"

    store = GoogleMarketingOAuthStore()
    store.save_tokens(
        GoogleMarketingTokenBundle(
            access_token="ga-acc",
            refresh_token="ga-ref",
            expires_at=9999999999.0,
            token_type="Bearer",
            scope="analytics.readonly",
        )
    )
    store.create_state()
    assert store.has_tokens() is True
    store.clear()
    assert store.has_tokens() is False
    assert not store.token_path.is_file()
    assert not store.state_path.is_file()


def test_gsc_oauth_strict_isolation_store_and_flows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecom_ops.integrations.gsc import _isolated_gsc_credentials, gsc_status
    from ecom_ops.oauth.gsc import (
        GSC_READONLY_SCOPE,
        GscOAuthStore,
        GscTokenBundle,
        build_gsc_authorize_url,
        exchange_gsc_code,
        gsc_oauth_client_id,
        gsc_oauth_client_secret,
        gsc_oauth_configured,
        gsc_oauth_redirect_uri,
        list_gsc_properties,
        refresh_gsc_access_token,
    )
    from ecom_ops.security import SecurityError

    _clear_google_env(monkeypatch)
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")

    # Gmail and GA4 credentials must NEVER leak into GSC
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "ga-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "ga-csec")
    monkeypatch.setenv("MAIL_OAUTH_CLIENT_ID", "mail-cid")
    monkeypatch.setenv("MAIL_OAUTH_CLIENT_SECRET", "mail-csec")
    assert gsc_oauth_client_id() == ""
    assert gsc_oauth_client_secret() == ""
    assert gsc_oauth_configured() is False

    # Redirect URI precedence
    assert gsc_oauth_redirect_uri() == "http://127.0.0.1:8080/oauth/gsc/callback"
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dashboard.azom.no/")
    assert gsc_oauth_redirect_uri() == "https://dashboard.azom.no/oauth/gsc/callback"
    monkeypatch.setenv("GSC_OAUTH_REDIRECT_URI", "https://override.azom.no/gsc/cb")
    assert gsc_oauth_redirect_uri() == "https://override.azom.no/gsc/cb"

    monkeypatch.setenv("GSC_OAUTH_CLIENT_ID", "gsc-cid")
    monkeypatch.setenv("GSC_OAUTH_CLIENT_SECRET", "gsc-csec-secret")
    assert gsc_oauth_configured() is True

    auth_url = build_gsc_authorize_url("state-abc-123")
    parsed = urlparse(auth_url)
    qs = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert qs["scope"] == [GSC_READONLY_SCOPE]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["include_granted_scopes"] == ["false"]
    assert qs["state"] == ["state-abc-123"]
    assert qs["client_id"] == ["gsc-cid"]

    # State mismatch must not wipe pending state
    store = GscOAuthStore()
    good_state = store.create_state()
    assert store.consume_state("wrong") is False
    assert store.validate_state(good_state) is True
    assert store.consume_state(good_state) is True
    assert not store.state_path.is_file()

    # Live code exchange & refresh via pinned_https_request-compatible transport
    calls: list[dict[str, Any]] = []

    def fake_http(url: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes, str]:
        calls.append({"url": url, **kwargs})
        if "oauth2.googleapis.com/token" in url:
            body_str = (kwargs.get("body") or b"").decode("utf-8")
            if "grant_type=refresh_token" in body_str:
                raw = json.dumps({"access_token": "refreshed-gsc-access", "expires_in": 3600}).encode()
                return 200, {}, raw, url
            raw = json.dumps(
                {
                    "access_token": "initial-gsc-access",
                    "refresh_token": "initial-gsc-refresh",
                    "expires_in": 3600,
                    "scope": GSC_READONLY_SCOPE,
                }
            ).encode()
            return 200, {}, raw, url
        if "searchconsole.googleapis.com/webmasters/v3/sites" in url:
            raw = json.dumps(
                {
                    "siteEntry": [
                        {"siteUrl": "sc-domain:azom.no", "permissionLevel": "siteOwner"},
                        {"siteUrl": "https://azom.se/", "permissionLevel": "siteFullUser"},
                    ]
                }
            ).encode()
            return 200, {}, raw, url
        raise AssertionError(f"Unexpected URL: {url}")

    with pytest.raises(ValueError, match="Mock OAuth code rejected"):
        exchange_gsc_code("mock")

    bundle = exchange_gsc_code("auth-code-1", store=store, http_request=fake_http)
    assert bundle.access_token == "initial-gsc-access"
    assert bundle.refresh_token == "initial-gsc-refresh"
    assert store.token_path == tmp_path / "data" / "oauth" / "gsc.json"
    assert store.has_tokens() is True

    # Public status never leaks secrets
    pub = store.public_status()
    pub_dump = json.dumps(pub)
    assert "initial-gsc-access" not in pub_dump
    assert "initial-gsc-refresh" not in pub_dump
    assert "gsc-csec-secret" not in pub_dump
    assert pub["connected"] is True
    assert pub["configured"] is True

    # _isolated_gsc_credentials falls back to GscOAuthStore when token env vars unset
    monkeypatch.setenv("AZOM_GSC_ENABLED", "1")
    creds = _isolated_gsc_credentials()
    assert creds["access_token"] == "initial-gsc-access"
    assert creds["refresh_token"] == "initial-gsc-refresh"

    # Expired access token in store is suppressed in _isolated_gsc_credentials so refresh runs
    store.save(
        GscTokenBundle(
            access_token="expired-access",
            refresh_token="initial-gsc-refresh",
            expires_at=1.0,
        )
    )
    creds_exp = _isolated_gsc_credentials()
    assert creds_exp["access_token"] == ""
    assert creds_exp["refresh_token"] == "initial-gsc-refresh"

    refreshed = refresh_gsc_access_token(store=store, http_request=fake_http)
    assert refreshed.access_token == "refreshed-gsc-access"
    assert refreshed.refresh_token == "initial-gsc-refresh"

    props = list_gsc_properties(refreshed.access_token, http_request=fake_http)
    assert props == [
        {"site_url": "sc-domain:azom.no", "permission_level": "siteOwner"},
        {"site_url": "https://azom.se/", "permission_level": "siteFullUser"},
    ]

    st = gsc_status("https://azom.no/")
    assert st["connected"] is True
    assert st["oauth_configured"] is True
    assert st["oauth_connected"] is True
    assert st["redirect_uri"] == "https://override.azom.no/gsc/cb"

    store.clear()
    assert store.has_tokens() is False
    with pytest.raises(SecurityError):
        refresh_gsc_access_token(store=store, http_request=fake_http)


def test_settings_store_and_gsc_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    import secret_probes
    import settings_store
    from ecom_ops.runtime_env import PROTECTED_OVERLAY_KEYS

    _clear_google_env(monkeypatch)
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))

    expected_keys = (
        "GSC_OAUTH_CLIENT_ID",
        "GSC_OAUTH_CLIENT_SECRET",
        "GSC_OAUTH_REDIRECT_URI",
        "AZOM_GSC_PROPERTY_URL_NO",
        "AZOM_GSC_PROPERTY_URL_SE",
        "AZOM_GSC_PROPERTY_URL_DK",
    )
    for k in expected_keys:
        assert k in settings_store.EDITABLE_SECRET_KEYS
        assert k in settings_store._SECRET_FIELD_META

    groups = {g["group"]: g["items"] for g in settings_store.editable_secret_groups()}
    assert "Google Search Console" in groups
    assert {i["name"] for i in groups["Google Search Console"]} == set(expected_keys)

    assert "AZOM_GSC_ENABLED" in PROTECTED_OVERLAY_KEYS
    assert "AZOM_GSC_KILL" in PROTECTED_OVERLAY_KEYS
    assert "AZOM_GSC_ENABLED" not in settings_store.EDITABLE_SECRET_KEYS
    assert "AZOM_GSC_KILL" not in settings_store.EDITABLE_SECRET_KEYS

    # Mock probe
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    r_mock = secret_probes.run_probe("gsc")
    assert r_mock.id == "gsc"
    assert r_mock.status == "ok"

    all_ids = [r.id for r in secret_probes.run_all_probes()]
    assert "gsc" in all_ids

    # Live disabled -> skipped
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    r_skip = secret_probes.run_probe("gsc")
    assert r_skip.status == "skipped"

    # Live enabled without creds -> missing
    monkeypatch.setenv("AZOM_GSC_ENABLED", "1")
    r_miss = secret_probes.run_probe("gsc")
    assert r_miss.status == "missing"

    # Kill switch -> skipped even in mock
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_GSC_KILL", "1")
    r_kill = secret_probes.run_probe("gsc")
    assert r_kill.status == "skipped"


def test_oscar_google_hub_rbac_csp_css_and_flows(dash_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client = dash_env["client"]
    jonatan = dash_env["jonatan"]
    oscar = dash_env["oscar"]
    data_dir: Path = dash_env["data_dir"]

    # Compiled CSS audit on oscar_google.html
    css = (DASH_DIR / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
    tpl_raw = (DASH_DIR / "templates" / "oscar_google.html").read_text(encoding="utf-8")
    assert "| safe" not in tpl_raw
    assert not re.search(r"\bon[a-z]+\s*=", tpl_raw, re.IGNORECASE)
    stripped = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", tpl_raw, flags=re.S)
    for m in re.finditer(r'(?<![:\w-])class="([^"]*)"', stripped):
        for tok in m.group(1).split():
            if not tok:
                continue
            esc = (
                tok.replace(":", "\\:")
                .replace("/", "\\/")
                .replace(".", "\\.")
                .replace("[", "\\[")
                .replace("]", "\\]")
            )
            assert esc in css or f".{tok}" in css, f"Uncompiled CSS class in oscar_google.html: {tok}"

    # Unauthenticated -> 401
    assert client.get("/oscar/google").status_code == 401
    assert client.get("/oauth/gsc/status").status_code == 401

    # Viewer on /marketing and /seo must NOT see Oscar hub link or extra POST forms
    mkt_j = client.get("/marketing", headers=jonatan)
    assert mkt_j.status_code == 200
    mkt_j_html = mkt_j.get_data(as_text=True)
    assert "/oscar/google" not in mkt_j_html
    jon_csrf = _extract_csrf(mkt_j_html)

    seo_j = client.get("/seo", headers=jonatan)
    assert seo_j.status_code == 200
    assert "/oscar/google" not in seo_j.get_data(as_text=True)

    # Viewer (Jonatan) -> 403 on all hub & GSC OAuth routes (even with valid CSRF)
    for get_path in ("/oscar/google", "/oauth/gsc/start", "/oauth/gsc/callback", "/oauth/gsc/status"):
        assert client.get(get_path, headers=jonatan).status_code == 403
    for post_path in (
        "/oscar/google/ga4/config",
        "/oscar/google/ga4/test",
        "/oauth/google/disconnect",
        "/oscar/google/gsc/config",
        "/oscar/google/gsc/test",
        "/oauth/gsc/disconnect",
    ):
        assert client.post(post_path, headers=jonatan, data={"_csrf": jon_csrf}).status_code == 403

    # Oscar sees link on /marketing and /seo (rendered as <a> link, no <form method="post">)
    mkt_o = client.get("/marketing", headers=oscar)
    assert mkt_o.status_code == 200
    assert 'href="/oscar/google"' in mkt_o.get_data(as_text=True)
    seo_o = client.get("/seo", headers=oscar)
    assert seo_o.status_code == 200
    assert 'href="/oscar/google"' in seo_o.get_data(as_text=True)

    # Oscar GET /oscar/google + strict CSP audit
    hub = client.get("/oscar/google", headers=oscar)
    assert hub.status_code == 200
    csp = hub.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    html = hub.get_data(as_text=True)
    auditor = _StrictHtmlAudit()
    auditor.feed(html)
    assert auditor.bad_scripts == []
    assert auditor.inline_events == []
    assert 'data-copy-target="#ga4-redirect-uri"' in html
    assert 'data-copy-target="#gsc-redirect-uri"' in html
    assert "data-google-setup-guide" in html
    csrf = _extract_csrf(html)

    # CSRF enforced on POST
    assert client.post("/oscar/google/gsc/config", headers=oscar, data={}).status_code == 400
    assert client.post("/oauth/gsc/disconnect", headers=oscar, data={}).status_code == 400

    # Save GA4 & GSC config -> secrets.env written, secrets never echoed
    r_ga4 = client.post(
        "/oscar/google/ga4/config",
        headers=oscar,
        data={
            "_csrf": csrf,
            "GOOGLE_OAUTH_CLIENT_ID": "ga4-client-123",
            "GOOGLE_OAUTH_CLIENT_SECRET": "super-secret-ga4-val",
            "AZOM_GA4_PROPERTY_IDS": "123456789",
        },
        follow_redirects=True,
    )
    assert r_ga4.status_code == 200
    assert "super-secret-ga4-val" not in r_ga4.get_data(as_text=True)

    r_gsc = client.post(
        "/oscar/google/gsc/config",
        headers=oscar,
        data={
            "_csrf": csrf,
            "GSC_OAUTH_CLIENT_ID": "gsc-client-999",
            "GSC_OAUTH_CLIENT_SECRET": "super-secret-gsc-val",
            "AZOM_GSC_PROPERTY_URL_NO": "sc-domain:azom.no",
            "AZOM_GSC_PROPERTY_URL_SE": "https://azom.se/",
        },
        follow_redirects=True,
    )
    assert r_gsc.status_code == 200
    r_gsc_html = r_gsc.get_data(as_text=True)
    assert "super-secret-gsc-val" not in r_gsc_html
    assert "sc-domain:azom.no" in r_gsc_html

    # Audit log must never store secret values
    audit_text = (data_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "super-secret-ga4-val" not in audit_text
    assert "super-secret-gsc-val" not in audit_text

    # Mock OAuth connect for GSC + GA4, test probes & disconnect
    gsc_start = client.get("/oauth/gsc/start", headers=oscar, follow_redirects=True)
    assert gsc_start.status_code == 200
    assert (data_dir / "oauth" / "gsc.json").is_file()

    gsc_test = client.post(
        "/oscar/google/gsc/test",
        headers=oscar,
        data={"_csrf": csrf},
        follow_redirects=True,
    )
    assert gsc_test.status_code == 200
    gsc_test_html = gsc_test.get_data(as_text=True)
    assert "data-gsc-verified-properties" in gsc_test_html
    assert "sc-domain:azom.no" in gsc_test_html

    ga4_test = client.post(
        "/oscar/google/ga4/test",
        headers=oscar,
        data={"_csrf": csrf},
        follow_redirects=True,
    )
    assert ga4_test.status_code == 200
    assert "GA4-test ok" in ga4_test.get_data(as_text=True)

    status_resp = client.get("/oauth/gsc/status", headers=oscar)
    assert status_resp.status_code == 200
    st_json = status_resp.get_json()
    assert st_json["connected"] is True
    assert "mock-gsc-access" not in json.dumps(st_json)
    assert "super-secret-gsc-val" not in json.dumps(st_json)

    # Disconnect GSC only -> GA4 untouched if present
    from ecom_ops.oauth.google_marketing import (
        GoogleMarketingOAuthStore,
        GoogleMarketingTokenBundle,
    )

    GoogleMarketingOAuthStore().save_tokens(
        GoogleMarketingTokenBundle(
            access_token="ga-tok",
            refresh_token="ga-ref",
            expires_at=9999999999.0,
            token_type="Bearer",
            scope="analytics.readonly",
        )
    )
    disc_gsc = client.post(
        "/oauth/gsc/disconnect",
        headers=oscar,
        data={"_csrf": csrf},
        follow_redirects=True,
    )
    assert disc_gsc.status_code == 200
    assert not (data_dir / "oauth" / "gsc.json").is_file()
    assert GoogleMarketingOAuthStore().has_tokens() is True

    disc_ga = client.post(
        "/oauth/google/disconnect",
        headers=oscar,
        data={"_csrf": csrf},
        follow_redirects=True,
    )
    assert disc_ga.status_code == 200
    assert GoogleMarketingOAuthStore().has_tokens() is False

    # Live GSC callback CSRF validation & token exchange
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    from ecom_ops.oauth.gsc import GscOAuthStore

    live_start = client.get("/oauth/gsc/start", headers=oscar, follow_redirects=False)
    assert live_start.status_code == 302
    loc = live_start.headers["Location"]
    assert "accounts.google.com" in loc
    q = parse_qs(urlparse(loc).query)
    assert q["include_granted_scopes"] == ["false"]
    live_state = q["state"][0]

    # Bad state rejected without wiping valid state
    bad_cb = client.get(
        "/oauth/gsc/callback?code=real-code&state=wrong-state",
        headers=oscar,
    )
    assert bad_cb.status_code == 400
    assert GscOAuthStore().validate_state(live_state) is True

    monkeypatch.setattr(
        "ecom_ops.oauth.gsc.pinned_https_request",
        lambda url, **_kw: (
            200,
            {},
            json.dumps(
                {
                    "access_token": "live-gsc-acc",
                    "refresh_token": "live-gsc-ref",
                    "expires_in": 3600,
                }
            ).encode(),
            url,
        ),
    )
    good_cb = client.get(
        f"/oauth/gsc/callback?code=real-code&state={live_state}",
        headers=oscar,
        follow_redirects=False,
    )
    assert good_cb.status_code == 302
    assert good_cb.headers["Location"].endswith("/oscar/google")
    assert GscOAuthStore().has_tokens() is True
    assert "live-gsc-acc" not in (data_dir / "audit.jsonl").read_text(encoding="utf-8")

    # /oauth/google/status includes redirect_uri
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dashboard.azom.no")
    ga_status = client.get("/oauth/google/status", headers=oscar)
    assert ga_status.status_code == 200
    assert ga_status.get_json()["redirect_uri"] == "https://dashboard.azom.no/oauth/google/callback"
