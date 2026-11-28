"""Tests for the modern session-based login page & Basic Auth backwards compatibility."""

from __future__ import annotations

import re
import sys

import pytest
from infrastructure.dashboard.app import create_app


def _clear_all_rate_limits() -> None:
    for mod_name in ("auth", "infrastructure.dashboard.auth"):
        mod = sys.modules.get(mod_name)
        failures = getattr(mod, "_login_failures", None)
        if isinstance(failures, dict):
            failures.clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-login-secret-key")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "test-oscar-pw")
    monkeypatch.delenv("DASHBOARD_OSCAR_PASSWORD_HASH", raising=False)
    _clear_all_rate_limits()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    _clear_all_rate_limits()


def _extract_csrf(html: str) -> str:
    m = re.search(r'(?:name="_csrf"\s+value|data-logout-csrf)="([^"]+)"', html)
    assert m, "CSRF token missing from HTML"
    return m.group(1)


def test_login_page_renders_modern_accessible_strict_csp_form(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"
    html = resp.get_data(as_text=True)

    assert 'action="/login"' in html
    assert 'autocomplete="username"' in html
    assert 'spellcheck="false"' in html
    assert 'autocorrect="off"' in html
    assert 'autocapitalize="none"' in html
    assert 'autocomplete="current-password"' in html
    assert 'data-password-toggle="login-password"' in html
    assert 'data-login-account="oscar"' in html
    assert 'data-login-account="jonatan"' in html
    assert 'data-error="login-username-error"' in html
    assert 'data-error="login-password-error"' in html
    # Strict CSP: no inline script tags or inline event handlers
    assert "<script>" not in html.lower()
    assert "onclick=" not in html.lower()
    assert _extract_csrf(html)


def test_browser_html_redirects_to_login_without_www_authenticate(client):
    resp = client.get(
        "/cases?status=open",
        headers={"Accept": "text/html,application/xhtml+xml"},
    )
    assert resp.status_code == 302
    assert "WWW-Authenticate" not in resp.headers
    loc = resp.headers["Location"]
    assert "/login" in loc
    assert "next=" in loc


def test_api_non_html_still_returns_401_basic_auth_challenge(client):
    resp = client.get("/")
    assert resp.status_code == 401
    assert "Basic" in resp.headers.get("WWW-Authenticate", "")


def test_session_login_flow_and_logout(client):
    login_page = client.get("/login")
    csrf = _extract_csrf(login_page.get_data(as_text=True))

    # Wrong password returns 401 on login page
    bad = client.post(
        "/login",
        data={
            "_csrf": csrf,
            "username": "oscar",
            "password": "wrong-password",
            "next": "/oscar",
            "session_hours": "24",
        },
        headers={"Accept": "text/html"},
    )
    assert bad.status_code == 401
    assert "Fel användarnamn eller lösenord" in bad.get_data(as_text=True)

    # Valid login redirects via 303 and establishes session
    ok = client.post(
        "/login",
        data={
            "_csrf": csrf,
            "username": "Oscar",
            "password": "test-oscar-pw",
            "next": "/oscar",
            "session_hours": "24",
        },
        headers={"Accept": "text/html"},
    )
    assert ok.status_code == 303
    assert ok.headers["Location"].endswith("/oscar")

    # Authenticated session can access Oscar admin without Basic Auth
    oscar_page = client.get("/oscar", headers={"Accept": "text/html"})
    assert oscar_page.status_code == 200
    body = oscar_page.get_data(as_text=True)
    assert "oscar · full_admin" in body
    assert 'data-logout-action="/logout"' in body

    # GET /logout does NOT clear session
    get_out = client.get("/logout")
    assert get_out.status_code == 302
    assert client.get("/oscar", headers={"Accept": "text/html"}).status_code == 200

    # POST /logout with CSRF clears session
    page_csrf = _extract_csrf(body)
    post_out = client.post("/logout", data={"_csrf": page_csrf})
    assert post_out.status_code == 302 or post_out.status_code == 303
    assert "logged_out=1" in post_out.headers["Location"]

    # After logout, browser is redirected back to /login
    after = client.get("/oscar", headers={"Accept": "text/html"})
    assert after.status_code == 302
    assert "/login" in after.headers["Location"]


def test_open_redirect_in_next_is_blocked(client):
    csrf = _extract_csrf(client.get("/login").get_data(as_text=True))
    for bad_next in ("https://evil.example/steal", "//evil.example", r"/\evil.example", "/logout"):
        resp = client.post(
            "/login",
            data={
                "_csrf": csrf,
                "username": "jonatan",
                "password": "jonatan",
                "next": bad_next,
            },
            headers={"Accept": "text/html"},
        )
        assert resp.status_code == 303
        assert resp.headers["Location"].endswith("/")
        # Reset session for next iteration
        home = client.get("/", headers={"Accept": "text/html"})
        csrf = _extract_csrf(home.get_data(as_text=True))
        client.post("/logout", data={"_csrf": csrf})
        csrf = _extract_csrf(client.get("/login").get_data(as_text=True))


def test_login_rate_limit_returns_429_with_retry_after(client):
    csrf = _extract_csrf(client.get("/login").get_data(as_text=True))
    last = None
    for _ in range(10):
        last = client.post(
            "/login",
            data={"_csrf": csrf, "username": "oscar", "password": "bad"},
            headers={"Accept": "text/html"},
            environ_base={"REMOTE_ADDR": "198.51.100.42"},
        )
    assert last is not None
    assert last.status_code == 429
    assert int(last.headers.get("Retry-After", "0")) > 0
