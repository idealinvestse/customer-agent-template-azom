"""Dashboard central /logs viewer and /api/logs API."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    sys.modules.pop("azom_dashboard", None)
    sys.modules.pop("settings_store", None)
    sys.modules.pop("status", None)
    sys.modules.pop("secret_probes", None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _auth(user="jonatan", password="jonatan", *, client=None):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if client is not None:
        client.get("/", headers=headers)
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token")
        if csrf:
            headers["X-CSRF-Token"] = csrf
    return headers


@pytest.fixture
def config_dir(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sites.yaml").write_text(
        "customer: azom\ndomains:\n  - se\nbudget_cap_llm: 80\n",
        encoding="utf-8",
    )
    (cfg / "limits.yaml").write_text(
        "openrouter_cap: 100\njonatan_role: read_only\n", encoding="utf-8"
    )
    (cfg / "integrations.yaml").write_text(
        yaml.safe_dump(
            {
                "mailcow": True,
                "order_api": True,
                "selenium": True,
                "woocommerce_api": True,
                "wordpress_api": True,
                "smart_handling": True,
                "full_agent_tools": True,
                "email": {
                    "enabled": True,
                    "default_provider": "generic_imap",
                    "smtp": True,
                    "imap": True,
                    "pop3": True,
                },
            }
        ),
        encoding="utf-8",
    )
    (cfg / "rbac.yaml").write_text(
        "roles:\n  jonatan: viewer\n  oscar: full_admin\nescalation:\n  critical: oscar\n  code_edit: oscar\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def logs_env(tmp_path, monkeypatch, config_dir):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AZOM_LOG_DIR", str(logs))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jonatan")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "oscar")
    monkeypatch.setenv("MAIL_PASSWORD", "super-secret-mail-pass-xyz")
    # Seed runtime + event logs
    (logs / "dashboard.log").write_text(
        json.dumps(
            {
                "ts": "2026-01-01T00:00:00+00:00",
                "level": "INFO",
                "logger": "azom.dashboard",
                "message": "boot",
                "password": "super-secret-mail-pass-xyz",
            }
        )
        + "\n"
        + "plain token=super-secret-mail-pass-xyz leaked\n",
        encoding="utf-8",
    )
    (data / "telemetry.jsonl").write_text(
        json.dumps(
            {
                "action": "case_poll",
                "MAIL_PASSWORD": "super-secret-mail-pass-xyz",
                "meta": {"ok": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client(), logs, data


def test_logs_requires_auth(logs_env):
    client, _, _ = logs_env
    assert client.get("/logs").status_code == 401
    assert client.get("/api/logs?source=dashboard").status_code == 401


def test_jonatan_and_oscar_can_read_logs(logs_env):
    client, _, _ = logs_env
    for user, password in (("jonatan", "jonatan"), ("oscar", "oscar")):
        resp = client.get("/logs?source=dashboard", headers=_auth(user, password))
        assert resp.status_code == 200, user
        assert b"Loggar" in resp.data or b"dashboard" in resp.data
        api = client.get(
            "/api/logs?source=dashboard&lines=50",
            headers=_auth(user, password),
        )
        assert api.status_code == 200, user
        body = api.get_json()
        assert body["ok"] is True
        assert body["source"] == "dashboard"
        assert body["lines"] >= 1


def test_api_logs_redacts_secrets(logs_env):
    client, _, _ = logs_env
    resp = client.get(
        "/api/logs?source=dashboard&lines=50",
        headers=_auth(),
    )
    body = resp.get_json()
    blob = json.dumps(body)
    assert "super-secret-mail-pass-xyz" not in blob
    assert "***REDACTED***" in blob


def test_api_logs_unknown_source_400(logs_env):
    client, _, _ = logs_env
    resp = client.get("/api/logs?source=not-a-real-source", headers=_auth())
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_api_logs_line_cap(logs_env):
    client, logs, _ = logs_env
    path = logs / "bot.log"
    path.write_text(
        "\n".join(json.dumps({"message": f"line-{i}"}) for i in range(600)) + "\n",
        encoding="utf-8",
    )
    resp = client.get("/api/logs?source=bot&lines=9999", headers=_auth())
    body = resp.get_json()
    assert body["ok"] is True
    assert body["lines"] <= 500


def test_api_logs_telemetry_source(logs_env):
    client, _, _ = logs_env
    resp = client.get("/api/logs?source=telemetry&lines=20", headers=_auth())
    body = resp.get_json()
    assert body["ok"] is True
    assert body["exists"] is True
    blob = json.dumps(body)
    assert "super-secret-mail-pass-xyz" not in blob
    assert body["rows"]


def test_logs_html_lists_sources(logs_env):
    client, _, _ = logs_env
    resp = client.get("/logs", headers=_auth())
    assert resp.status_code == 200
    assert b"telemetry" in resp.data
    assert b"dashboard" in resp.data
