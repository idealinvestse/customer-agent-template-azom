"""Tests for sandbox_preview_url() and dashboard GET /wp/<site_id>/reviews/<plan_id>/preview."""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
from pathlib import Path

import pytest

from ecom_ops.bot.dashboard_links import review_plan_url, sandbox_preview_url
from ecom_ops.wp_ops.review_plan import ReviewPlans

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"
MOCK_REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"
SECRET = "mock-shop-super-secret-xyz987"


def _load_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    for name in ("azom_dashboard", "wp_routes", "settings_store", "auth", "support"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _auth(client, user: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{user}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    client.get("/wp", headers=headers)
    with client.session_transaction() as sess:
        csrf = sess.get("csrf_token")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def _clear_url_env(monkeypatch) -> None:
    for key in (
        "AZOM_SANDBOX_PREVIEW_BASE_URL",
        "AZOM_DASHBOARD_PUBLIC_URL",
        "DASHBOARD_HOST",
        "DASHBOARD_PORT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_sandbox_preview_url_https_loopback_and_rejection_rules(monkeypatch) -> None:
    _clear_url_env(monkeypatch)
    site_id = "azom-no"
    plan_id = "wr-0123456789ab"

    assert sandbox_preview_url(site_id, plan_id) is None

    # Public HTTPS origin works
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dash.azom.no/")
    assert (
        sandbox_preview_url(site_id, plan_id)
        == f"https://dash.azom.no/wp/{site_id}/reviews/{plan_id}/preview"
    )
    assert (
        review_plan_url(site_id, plan_id)
        == f"https://dash.azom.no/wp/{site_id}/reviews/{plan_id}"
    )

    # AZOM_SANDBOX_PREVIEW_BASE_URL overrides AZOM_DASHBOARD_PUBLIC_URL and allows loopback HTTP
    monkeypatch.setenv("AZOM_SANDBOX_PREVIEW_BASE_URL", "http://127.0.0.1:8080")
    assert (
        sandbox_preview_url(site_id, plan_id)
        == f"http://127.0.0.1:8080/wp/{site_id}/reviews/{plan_id}/preview"
    )
    monkeypatch.setenv("AZOM_SANDBOX_PREVIEW_BASE_URL", "http://localhost:9000/")
    assert (
        sandbox_preview_url(site_id, plan_id)
        == f"http://localhost:9000/wp/{site_id}/reviews/{plan_id}/preview"
    )

    # review_plan_url stays strictly HTTPS-only
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "http://127.0.0.1:8080")
    assert review_plan_url(site_id, plan_id) is None
    monkeypatch.delenv("AZOM_SANDBOX_PREVIEW_BASE_URL")
    assert (
        sandbox_preview_url(site_id, plan_id)
        == f"http://127.0.0.1:8080/wp/{site_id}/reviews/{plan_id}/preview"
    )

    # Reject non-loopback http, userinfo, query, fragment, invalid port
    for bad_base in (
        "http://dash.azom.no",
        "http://0.0.0.0:8080",
        "http://127.0.0.1.evil.example:8080",
        "http://192.168.1.5:8080",
        "https://user:pass@dash.azom.no",
        "https://dash.azom.no?foo=bar",
        "https://dash.azom.no#frag",
        "ftp://dash.azom.no",
        "http://127.0.0.1:notaport",
    ):
        monkeypatch.setenv("AZOM_SANDBOX_PREVIEW_BASE_URL", bad_base)
        assert sandbox_preview_url(site_id, plan_id) is None

    # Fallback to DASHBOARD_HOST + DASHBOARD_PORT only for loopback
    _clear_url_env(monkeypatch)
    monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setenv("DASHBOARD_PORT", "8080")
    assert (
        sandbox_preview_url(site_id, plan_id)
        == f"http://127.0.0.1:8080/wp/{site_id}/reviews/{plan_id}/preview"
    )

    monkeypatch.setenv("DASHBOARD_HOST", "localhost")
    monkeypatch.setenv("DASHBOARD_PORT", "8765")
    assert (
        sandbox_preview_url(site_id, plan_id)
        == f"http://localhost:8765/wp/{site_id}/reviews/{plan_id}/preview"
    )

    monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
    assert sandbox_preview_url(site_id, plan_id) is None

    monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setenv("DASHBOARD_PORT", "abc")
    assert sandbox_preview_url(site_id, plan_id) is None

    # Invalid site_id / plan_id rejected
    monkeypatch.setenv("DASHBOARD_PORT", "8080")
    assert sandbox_preview_url("../evil", plan_id) is None
    assert sandbox_preview_url("AZOM", plan_id) is None
    assert sandbox_preview_url(site_id, "wr-nothex") is None
    assert sandbox_preview_url(site_id, "../wr-0123456789ab") is None


def test_dashboard_sandbox_preview_route_auth_headers_scrub_and_symlink_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", SECRET)
    for name in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(name, raising=False)
    from ecom_ops.rbac import clear_rbac_cache

    clear_rbac_cache()

    site_id = "mock-shop"
    plan_id = "wr-123456abcdef"
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    ws_dir = tmp_path / "wp_workspaces" / site_id / plan_id
    (ws_dir / "base").mkdir(parents=True)
    (ws_dir / "work").mkdir(parents=True)
    (ws_dir / "artifacts").mkdir(parents=True)
    plans._save(
        [
            {
                "plan_id": plan_id,
                "site_id": site_id,
                "environment": "production",
                "kind": "theme",
                "slug": "twentytwentyfive-child",
                "work_slug": "twentytwentyfive-child",
                "theme_relationship": "no_child",
                "status": "validated",
                "risk": "low",
                "created_at": "2026-11-25T17:00:00+00:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "review_path": f"/wp/{site_id}/reviews/{plan_id}",
            }
        ]
    )

    app = _load_app()
    app.config["TESTING"] = True
    client = app.test_client()

    preview_url = f"/wp/{site_id}/reviews/{plan_id}/preview"
    review_url = f"/wp/{site_id}/reviews/{plan_id}"

    # Unauthenticated request rejected (401)
    unauth = client.get(preview_url)
    assert unauth.status_code in {302, 401}

    jonatan = _auth(client, "jonatan")
    oscar = _auth(client, "oscar")

    # 404 before artifacts/preview.html is generated, and review page has no link yet
    assert client.get(preview_url, headers=jonatan).status_code == 404
    rev_before = client.get(review_url, headers=oscar)
    assert rev_before.status_code == 200
    assert "Öppna interaktiv sandbox-förhandsvisning" not in rev_before.get_data(as_text=True)

    # Path traversal / invalid IDs rejected
    assert client.get(f"/wp/{site_id}/reviews/bad-id/preview", headers=oscar).status_code == 404
    assert client.get(f"/wp/..%2Fetc/reviews/{plan_id}/preview", headers=oscar).status_code == 404

    # Write preview.html containing an accidental secret to verify defensive scrubbing
    preview_file = ws_dir / "artifacts" / "preview.html"
    preview_file.write_text(
        f"<!doctype html><html><body><div id='azom-sandbox-toolbar'>AZOM SANDBOX</div>"
        f"<h1>Testprodukt 123</h1><p>{SECRET}</p></body></html>",
        encoding="utf-8",
    )

    # Both viewer (jonatan with WP_OPS_READ) and admin (oscar) can view interactive sandbox preview
    for headers in (jonatan, oscar):
        resp = client.get(preview_url, headers=headers)
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")
        assert "charset=utf-8" in resp.content_type.lower()
        assert resp.headers.get("Cache-Control") == "no-store"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
        csp = resp.headers.get("Content-Security-Policy") or ""
        assert "connect-src 'none'" in csp
        assert "form-action 'none'" in csp
        assert "frame-ancestors 'self'" in csp
        body = resp.get_data(as_text=True)
        assert "Testprodukt 123" in body
        assert SECRET not in body
        assert "***" in body

    # Review page now shows the interactive sandbox preview button/link
    rev_after = client.get(review_url, headers=oscar)
    assert rev_after.status_code == 200
    rev_html = rev_after.get_data(as_text=True)
    assert "Öppna interaktiv sandbox-förhandsvisning" in rev_html
    assert preview_url in rev_html

    # POST /preview still enforces viewer 403
    assert (
        client.post(
            preview_url,
            headers=jonatan,
            data={"_csrf": jonatan["X-CSRF-Token"]},
        ).status_code
        == 403
    )

    # Symlink escape in artifacts/preview.html is strictly refused
    outside = tmp_path / "outside_secret.html"
    outside.write_text("<html><body>OUTSIDE_SECRET_CONTENT</body></html>", encoding="utf-8")
    preview_file.unlink()
    try:
        os.symlink(outside, preview_file)
    except (OSError, NotImplementedError):
        pytest.skip("OS does not permit creating symlinks in test environment")

    sym_resp = client.get(preview_url, headers=oscar)
    assert sym_resp.status_code == 404
    assert "OUTSIDE_SECRET_CONTENT" not in sym_resp.get_data(as_text=True)
