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
    sys.modules.pop("secret_probes", None)
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


def test_oscar_secret_probes_forbidden_for_jonatan(dash_client):
    assert dash_client.post(
        "/oscar/secrets/test",
        headers=_auth("jonatan", "jonatan"),
        data={"probe": "all"},
    ).status_code == 403


def test_oscar_secret_probes_run(dash_client):
    resp = dash_client.get("/oscar/secrets", headers=_auth("oscar", "oscar"))
    assert resp.status_code == 200
    assert b"Anslutningstester" in resp.data or b"Testa" in resp.data

    resp = dash_client.post(
        "/oscar/secrets/test",
        headers=_auth("oscar", "oscar"),
        data={"probe": "ssh"},
    )
    assert resp.status_code == 200
    assert b"SSH" in resp.data or b"ssh" in resp.data.lower()

    hdrs = {**_auth("oscar", "oscar"), "Accept": "application/json"}
    resp = dash_client.post(
        "/oscar/secrets/test",
        headers=hdrs,
        data={"probe": "gmail_oauth"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload is not None
    assert "results" in payload
    assert payload["results"][0]["id"] == "gmail_oauth"


def test_index_shows_integration_summary(dash_client):
    resp = dash_client.get("/", headers=_auth())
    assert resp.status_code == 200
    assert b"Integrationer" in resp.data or b"integration" in resp.data.lower()
    # Presence-only: no requirement for live probe ok/error table on home
    body = resp.data.decode("utf-8", errors="replace")
    assert "Senaste Oscar-test" in body or "Integrationer" in body


def test_nav_badges_when_cases_exist(dash_client):
    import os

    from ecom_ops.cases.store import CaseStore

    store = CaseStore(Path(os.environ["AZOM_DATA_DIR"]) / "cases.db")
    store.create_case(
        mailbox_id="support_default",
        subject="Ops polish badge",
        from_addr="a@b.co",
        body="hej",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<ops-badge@x>",
        status="open",
    )
    resp = dash_client.get("/", headers=_auth())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "az-badge" in body
    assert ">1<" in body or "Öppna" in body
    cases = dash_client.get("/cases", headers=_auth())
    assert cases.status_code == 200
    assert b"Ops polish badge" in cases.data


def test_oscar_escalations_default_open(dash_client):
    import os

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    esc = data_dir / "escalations.jsonl"
    esc.write_text(
        '{"id":"esc-open-1","status":"open","summary":"need help open"}\n'
        '{"id":"esc-done-1","status":"resolved","summary":"already resolved done"}\n',
        encoding="utf-8",
    )
    resp = dash_client.get("/oscar/escalations", headers=_auth("oscar", "oscar"))
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "need help open" in body
    assert "already resolved done" not in body
    assert "show=open" in body or "Öppna" in body

    all_resp = dash_client.get(
        "/oscar/escalations?show=all", headers=_auth("oscar", "oscar")
    )
    assert all_resp.status_code == 200
    assert b"already resolved done" in all_resp.data


def test_case_detail_approve_guard(dash_client):
    import os

    from ecom_ops.cases.store import CaseStore

    store = CaseStore(Path(os.environ["AZOM_DATA_DIR"]) / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Approve guard",
        from_addr="a@b.co",
        body="hej",
        category="other",
        draft_reply="Tack for ditt mail",
        order_id=None,
        message_id="<approve-guard@x>",
        status="open",
    )
    resp = dash_client.get(f"/cases/{case.id}", headers=_auth())
    assert resp.status_code == 200
    assert b"data-approve-guard" in resp.data
    assert b"confirm(" in resp.data or b"Skicka svar" in resp.data


def test_interact_escalate_cta(dash_client):
    resp = dash_client.get("/interact", headers=_auth())
    assert resp.status_code == 200
    post = dash_client.post(
        "/interact",
        headers=_auth(),
        data={"message": "Var ar min order?"},
    )
    assert post.status_code == 200
    assert b"Eskalera till Oscar" in post.data


def test_probe_last_cached_after_oscar_test(dash_client):
    import os

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    resp = dash_client.post(
        "/oscar/secrets/test",
        headers=_auth("oscar", "oscar"),
        data={"probe": "all"},
    )
    assert resp.status_code == 200
    assert (data_dir / "probe_last.json").is_file()
    home = dash_client.get("/", headers=_auth())
    assert home.status_code == 200
    assert b"Senaste Oscar-test" in home.data


def test_index_shows_cached_probe_results(dash_client):
    import json
    import os

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    (data_dir / "probe_last.json").write_text(
        json.dumps(
            {
                "checked_at": "2026-07-11T10:00:00+00:00",
                "results": [
                    {
                        "id": "telegram",
                        "label": "Telegram",
                        "status": "ok",
                        "message": "ok",
                        "checked_at": "2026-07-11T10:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    home = dash_client.get("/", headers=_auth())
    assert home.status_code == 200
    assert b"Senaste Oscar-test" in home.data
    assert b"Telegram" in home.data
    assert b"kan vara inaktuell" in home.data


def test_secret_probes_module_mock(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("OPENROUTER_API_KEY", "key")
    monkeypatch.setenv("WOO_BASE_URL", "https://example.com")
    monkeypatch.setenv("WOO_CONSUMER_KEY", "ck")
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "cs")
    sys.path.insert(0, str(DASH_DIR))
    from secret_probes import probe_summary, run_probe

    tg = run_probe("telegram")
    assert tg.status == "ok"
    summary = probe_summary()
    assert "counts" in summary
    assert len(summary["results"]) >= 6
