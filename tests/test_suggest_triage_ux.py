"""U4/U5: suggest-approve triage UX on dashboard + Telegram."""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

from ecom_ops.bot.openclaw_commands import _format_case_show, dispatch_openclaw_command
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.store import CaseStore

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
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def _seed_pair(store: CaseStore) -> tuple:
    suggest = store.create_case(
        mailbox_id="support_default",
        subject="Suggestable order status",
        from_addr="kund@example.com",
        body="Var är order 1001?",
        category="order_status",
        draft_reply="Din order 1001 är skickad.",
        order_id="1001",
        message_id="<suggest-triage@x>",
        status="open",
        classify_confidence=0.91,
        classify_method="llm",
        suggest_approve=True,
    )
    other = store.create_case(
        mailbox_id="support_default",
        subject="Ordinary return question",
        from_addr="kund2@example.com",
        body="Jag vill returnera",
        category="return",
        draft_reply="Vi hjälper dig med retur.",
        order_id=None,
        message_id="<other-triage@x>",
        status="open",
        suggest_approve=False,
    )
    return suggest, other


def test_list_cases_filters_suggest_approve(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    suggest, other = _seed_pair(store)

    only_suggest = store.list_cases(status="open", suggest_approve=True)
    assert len(only_suggest) == 1
    assert only_suggest[0].id == suggest.id

    all_open = store.list_cases(status="open")
    assert {c.id for c in all_open} == {suggest.id, other.id}


def test_cases_list_suggest_filter_and_badge(dash_client):
    store = CaseStore(Path(os.environ["AZOM_DATA_DIR"]) / "cases.db")
    suggest, other = _seed_pair(store)

    filtered = dash_client.get("/cases?suggest=1", headers=_auth())
    assert filtered.status_code == 200
    body = filtered.data.decode("utf-8", errors="replace")
    assert "Suggestable order status" in body
    assert "Ordinary return question" not in body
    assert "Föreslå godkänn" in body
    assert 'name="suggest"' in body or "suggest=1" in body

    unfiltered = dash_client.get("/cases", headers=_auth())
    assert unfiltered.status_code == 200
    both = unfiltered.data.decode("utf-8", errors="replace")
    assert "Suggestable order status" in both
    assert "Ordinary return question" in both
    assert "Föreslå godkänn" in both


def test_case_detail_suggest_shorter_confirm(dash_client):
    store = CaseStore(Path(os.environ["AZOM_DATA_DIR"]) / "cases.db")
    suggest, other = _seed_pair(store)

    suggest_resp = dash_client.get(f"/cases/{suggest.id}", headers=_auth())
    assert suggest_resp.status_code == 200
    suggest_body = suggest_resp.data.decode("utf-8", errors="replace")
    assert "Föreslå godkänn" in suggest_body
    assert "confirm(" in suggest_body
    assert "Skicka svar till kunden nu?" not in suggest_body
    assert "Skicka nu?" in suggest_body or "Skicka föreslaget svar?" in suggest_body

    other_resp = dash_client.get(f"/cases/{other.id}", headers=_auth())
    assert other_resp.status_code == 200
    other_body = other_resp.data.decode("utf-8", errors="replace")
    assert "Skicka svar till kunden nu?" in other_body


def test_telegram_list_and_show_suggest_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    store = CaseStore(path=tmp_path / "cases.db")
    suggest, _other = _seed_pair(store)
    conv = ConversationStore(path=tmp_path / "tg.json")

    listed = dispatch_openclaw_command(1, "/cases", conv)
    assert listed and suggest.id[:8] in listed
    assert "★föreslå" in listed.lower() or "★ föreslå" in listed.lower()
    suggest_line = next(
        line for line in listed.splitlines() if suggest.id[:8] in line
    )
    assert "★" in suggest_line

    shown = dispatch_openclaw_command(1, f"/cases show {suggest.id[:8]}", conv)
    assert shown and "★" in shown
    assert "Föreslå godkänn" in shown
    assert "91%" in shown or "0.91" in shown or "91" in shown
    assert f"/cases approve {suggest.id[:8]}" in shown


def test_format_case_show_non_suggest_has_no_star(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    _suggest, other = _seed_pair(store)
    text = _format_case_show(other)
    assert "★" not in text
    assert "Föreslå godkänn" not in text
    assert f"/cases approve {other.id[:8]}" in text


def test_telegram_approve_still_explicit_command(tmp_path, monkeypatch):
    """Approve remains an explicit subcommand — list/show never auto-send."""
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    store = CaseStore(path=tmp_path / "cases.db")
    suggest, _ = _seed_pair(store)
    conv = ConversationStore(path=tmp_path / "tg.json")

    listed = dispatch_openclaw_command(1, "/cases", conv)
    shown = dispatch_openclaw_command(1, f"/cases show {suggest.id[:8]}", conv)
    assert listed and "approve" in listed.lower()
    assert shown and f"/cases approve {suggest.id[:8]}" in shown
    # Case must still be open until explicit approve
    assert store.get(suggest.id).status == "open"
