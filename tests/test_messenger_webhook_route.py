"""Flask /webhooks/messenger integration tests."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    for name in ("azom_dashboard", "settings_store", "status", "secret_probes"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


@pytest.fixture
def messenger_client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sites.yaml").write_text("customer: azom\ndomains:\n  - se\n", encoding="utf-8")
    (cfg / "limits.yaml").write_text("openrouter_cap: 100\n", encoding="utf-8")
    (cfg / "integrations.yaml").write_text("email:\n  enabled: true\n", encoding="utf-8")
    (cfg / "rbac.yaml").write_text(
        "roles:\n  jonatan: viewer\n  oscar: full_admin\n", encoding="utf-8"
    )
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("MESSENGER_VERIFY_TOKEN", "verify-token")
    monkeypatch.setenv("MESSENGER_APP_SECRET", "app-secret")
    monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", "PSID1")
    monkeypatch.setenv("MESSENGER_ACTOR_MAP", "PSID1:jonatan")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def _sign(body: bytes) -> str:
    dig = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={dig}"


def test_messenger_webhook_verify_challenge(messenger_client):
    resp = messenger_client.get(
        "/webhooks/messenger"
        "?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=9999"
    )
    assert resp.status_code == 200
    assert resp.data == b"9999"


def test_messenger_webhook_rejects_bad_signature(messenger_client):
    body = b'{"object":"page","entry":[]}'
    resp = messenger_client.post(
        "/webhooks/messenger",
        data=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert resp.status_code == 401


def test_messenger_webhook_handles_message(messenger_client):
    payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "message": {"mid": "m1", "text": "/help"},
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    resp = messenger_client.post(
        "/webhooks/messenger",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["handled"] == 1
    assert data["errors"] == 0


def test_health_includes_messenger(monkeypatch, messenger_client):
    monkeypatch.delenv("MESSENGER_PAGE_ACCESS_TOKEN", raising=False)
    resp = messenger_client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "messenger" in data
    assert data["messenger"]["configured"] is True
    assert data["messenger"]["page_token_present"] is False
    assert data["messenger"]["send_enabled"] is False
