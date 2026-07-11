"""Dashboard routes: Basic Auth, onboarding, Gmail OAuth."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    """Load Flask app from infrastructure/dashboard/app.py."""
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    return mod.app


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def _auth_headers(user="jonatan", password="jonatan"):
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_health_unauthenticated(dash_client):
    resp = dash_client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_index_requires_auth(dash_client):
    assert dash_client.get("/").status_code == 401


def test_index_with_auth(dash_client):
    resp = dash_client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    assert b"Azom Agent Dashboard" in resp.data


def test_onboarding_page(dash_client):
    resp = dash_client.get("/onboarding", headers=_auth_headers())
    assert resp.status_code == 200
    assert b"Onboarding wizard" in resp.data
    assert b"Secrets checklist" in resp.data


def test_onboarding_status_json(dash_client):
    resp = dash_client.get("/onboarding/status", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert "runtime" in data
    assert "secrets" in data
    assert "health" in data


def test_gmail_oauth_mock_connect(dash_client, tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    resp = dash_client.get("/oauth/gmail/start", headers=_auth_headers())
    assert resp.status_code == 302
    assert "onboarding" in resp.headers.get("Location", "")

    from ecom_ops.oauth.gmail import GmailOAuthStore

    store = GmailOAuthStore()
    assert store.has_tokens()

    status = dash_client.get("/oauth/gmail/status", headers=_auth_headers())
    assert status.status_code == 200
    assert status.get_json()["connected"] is True


def test_index_presence_not_live_probe_storm(dash_client):
    """Home uses presence/runtime chrome; does not require live probe labels."""
    resp = dash_client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Integrationer" in body or "integration" in body.lower()
    # Presence summary / counts chrome (not Oscar live probe table)
    assert "Öppna" in body or "open_cases" in body or "Ärenden" in body


def test_gmail_status_authenticated(dash_client):
    resp = dash_client.get("/oauth/gmail/status", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert "connected" in data


def test_onboarding_refresh_markup(dash_client):
    resp = dash_client.get("/onboarding", headers=_auth_headers())
    assert resp.status_code == 200
    assert b"/onboarding/status" in resp.data
    assert b"Uppdatera status" in resp.data
    assert b"x-for=\"s in secrets\"" in resp.data or b"x-for='s in secrets'" in resp.data
    assert b"x-for=\"c in checks\"" in resp.data or b"x-for='c in checks'" in resp.data
