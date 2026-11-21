"""Oscar model panel: CSRF, no fallback, no token echo."""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    spec = importlib.util.spec_from_file_location("azom_dashboard_models", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_models"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _auth(user="oscar", password="oscar", *, client=None):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if client is not None:
        client.get("/oscar", headers=headers)
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token")
        if csrf:
            headers["X-CSRF-Token"] = csrf
    return headers


def test_oscar_model_panel_and_isolated_test(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()

    denied = client.get("/oscar", headers=_auth("jonatan", "jonatan"))
    assert denied.status_code == 403

    page = client.get("/oscar", headers=_auth())
    assert page.status_code == 200
    assert b"Modellrouter" in page.data
    assert b"grok_build" in page.data
    assert b"access_token" not in page.data

    failed = client.post(
        "/oscar/models/test",
        headers=_auth(client=client),
        data={"route_id": "chatgpt_oauth"},
    )
    assert failed.status_code == 302
    again = client.get("/oscar", headers=_auth())
    assert b"misslyckades" in again.data
    body = again.get_data(as_text=True)
    assert "refresh-secret" not in body
    assert "authorization code" not in body.lower()
