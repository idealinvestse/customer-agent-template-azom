"""Dashboard settings, secrets request, and Oscar views."""

from __future__ import annotations

import base64
import importlib.util
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
    # Fresh module each time
    sys.modules.pop("azom_dashboard", None)
    sys.modules.pop("settings_store", None)
    sys.modules.pop("status", None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    return mod.app


def _auth(user="jonatan", password="jonatan"):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def config_dir(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sites.yaml").write_text(
        "customer: azom\ndomains:\n  - \"no\"\n  - se\nbudget_cap_llm: 80\n",
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
def dash_client(tmp_path, monkeypatch, config_dir):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(config_dir))
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_settings_page_and_save(dash_client, config_dir):
    resp = dash_client.get("/settings", headers=_auth())
    assert resp.status_code == 200
    assert b"Inst" in resp.data or b"settings" in resp.data.lower() or b"Kund" in resp.data

    resp = dash_client.post(
        "/settings",
        headers=_auth(),
        data={
            "customer": "azom",
            "domains": "no, se, dk",
            "budget_cap_llm": "75",
            "openrouter_cap": "90",
            "mail_provider": "gmail",
            "mock_mode": "1",
            "email_enabled": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    sites = yaml.safe_load((config_dir / "sites.yaml").read_text(encoding="utf-8"))
    assert sites["budget_cap_llm"] == 75.0
    limits = yaml.safe_load((config_dir / "limits.yaml").read_text(encoding="utf-8"))
    assert limits["openrouter_cap"] == 90.0


def test_jonatan_secret_request_escalates(dash_client, tmp_path):
    resp = dash_client.post(
        "/secrets",
        headers=_auth(),
        data={"key": "WOO_CONSUMER_SECRET", "note": "need new key"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    esc = Path(tmp_path / "data" / "escalations.jsonl")
    # AZOM_DATA_DIR from fixture
    data_dir = Path(dash_client.application.root_path)  # may not be data
    # Read from env path used in fixture — re-get via listing
    from settings_store import _data_dir

    path = _data_dir() / "escalations.jsonl"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "WOO_CONSUMER_SECRET" in text
    assert "Jonatan" in text or "jonatan" in text


def test_oscar_home_forbidden_for_jonatan(dash_client):
    assert dash_client.get("/oscar", headers=_auth()).status_code == 403


def test_oscar_home_and_secrets(dash_client, tmp_path, monkeypatch):
    resp = dash_client.get("/oscar", headers=_auth("oscar", "oscar"))
    assert resp.status_code == 200
    assert b"Oscar" in resp.data

    resp = dash_client.post(
        "/oscar/secrets",
        headers=_auth("oscar", "oscar"),
        data={"WOO_BASE_URL": "https://azom.se", "TELEGRAM_BOT_TOKEN": "tok-test"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    from settings_store import _data_dir

    secrets_file = _data_dir() / "secrets.env"
    assert secrets_file.is_file()
    body = secrets_file.read_text(encoding="utf-8")
    assert "WOO_BASE_URL=https://azom.se" in body
    assert "TELEGRAM_BOT_TOKEN=tok-test" in body


def test_oscar_resolve_escalation(dash_client):
    from ecom_ops.escalation import EscalationService
    from settings_store import _data_dir

    ticket = EscalationService(
        store_path=_data_dir() / "escalations.jsonl", notifiers=[]
    ).escalate_critical("test ticket")
    resp = dash_client.post(
        "/oscar/escalations",
        headers=_auth("oscar", "oscar"),
        data={"ticket_id": ticket.id},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    text = (_data_dir() / "escalations.jsonl").read_text(encoding="utf-8")
    assert "resolved" in text


def test_data_pages(dash_client):
    assert dash_client.get("/data/telemetry", headers=_auth()).status_code == 200
    assert dash_client.get("/data/escalations", headers=_auth()).status_code == 200
    assert dash_client.get("/interact", headers=_auth()).status_code == 200


def test_settings_rejects_secret_key_via_store(tmp_path, monkeypatch, config_dir):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    sys.path.insert(0, str(DASH_DIR))
    from settings_store import save_settings

    with pytest.raises(ValueError):
        save_settings({"WOO_CONSUMER_SECRET": "x"})
