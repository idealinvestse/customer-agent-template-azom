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
    mod._configure_secret_key()
    return mod.app


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
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


def test_index_partial_readiness_shows_runbook(dash_client):
    from ecom_ops.ops_status import write_last_case_poll

    write_last_case_poll(ok=True, errors=1, created=0, extra={"partial": True})
    resp = dash_client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "PARTIAL" in body
    assert "mail-poll-stuck" in body


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


def test_case_detail_shows_draft_diff(dash_client, tmp_path):
    """After regenerate, case_detail shows Föregående/Nytt when draft_before_regen set."""
    import os
    from pathlib import Path

    from ecom_ops.cases.store import CaseStore

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    store = CaseStore(path=data_dir / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order 1001?",
        category="order_status",
        draft_reply="NYTT UTKAST TEXT",
        order_id="1001",
        message_id="<dash-draft-diff@azom>",
        site="azom",
    )
    with store._conn() as conn:
        conn.execute(
            """
            UPDATE cases SET draft_before_regen = ?, draft_regenerated_at = ?
            WHERE id = ?
            """,
            ("GAMMALT UTKAST TEXT", "2026-07-29T12:00:00Z", case.id),
        )
    resp = dash_client.get(f"/cases/{case.id}", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Föregående utkast" in body
    assert "Nytt utkast" in body
    assert "GAMMALT UTKAST TEXT" in body
    assert "NYTT UTKAST TEXT" in body
