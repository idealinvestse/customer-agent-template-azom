"""Sprint A: order panel, next-in-queue, suggest count, brief, KPIs."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from ecom_ops.bot.openclaw_commands import dispatch_openclaw_command
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.cli import main
from ecom_ops.kpis import support_kpis_last_days
from ecom_ops.order_context import resolve_order_panel
from ecom_ops.telemetry import Telemetry

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
        "openrouter_cap: 100\nopenrouter_warn_ratio: 0.8\njonatan_role: read_only\n",
        encoding="utf-8",
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


def _seed_two_open(store: CaseStore) -> tuple:
    a = store.create_case(
        mailbox_id="support_default",
        subject="First suggest",
        from_addr="a@example.com",
        body="Var är order 1001?",
        category="order_status",
        draft_reply="Order 1001 är under behandling.",
        order_id="1001",
        message_id="<sprint-a-1@x>",
        status="open",
        classify_confidence=0.91,
        classify_method="llm",
        suggest_approve=True,
    )
    b = store.create_case(
        mailbox_id="support_default",
        subject="Second open",
        from_addr="b@example.com",
        body="Hej",
        category="other",
        draft_reply="Tack för ditt meddelande.",
        order_id=None,
        message_id="<sprint-a-2@x>",
        status="open",
        suggest_approve=False,
    )
    return a, b


def test_resolve_order_panel_mock(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    panel = resolve_order_panel("1001", use_mock=True)
    assert panel is not None
    assert panel["ok"] is True
    assert panel["order_id"] == "1001"
    assert panel["status"] == "processing"
    assert panel["line_items"]


def test_count_suggest_approve(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    _seed_two_open(store)
    assert store.count_suggest_approve(status="open,escalated") == 1


def test_next_in_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    a, b = _seed_two_open(store)
    svc = CaseService(store=store)
    # Sorted: suggest first (a), then b
    nxt = svc.next_in_queue(a.id, status="open,escalated")
    assert nxt is not None
    assert nxt.id == b.id
    assert svc.next_in_queue(b.id, status="open,escalated") is None


def test_case_detail_order_panel(dash_client):
    store = CaseStore(Path(os.environ["AZOM_DATA_DIR"]) / "cases.db")
    a, _b = _seed_two_open(store)
    resp = dash_client.get(f"/cases/{a.id}", headers=_auth())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert 'data-order-panel' in body or "Order 1001" in body
    assert "processing" in body
    assert "Godkänn &amp; nästa" in body or "Godkänn & nästa" in body
    assert "Nästa i kö" in body


def test_list_open_links_preserve_queue_query(dash_client):
    """P1.1: list → detail must keep status/suggest so next-in-queue stays in filter."""
    store = CaseStore(Path(os.environ["AZOM_DATA_DIR"]) / "cases.db")
    a, b = _seed_two_open(store)
    listed = dash_client.get(
        "/cases?status=open,escalated&suggest=1", headers=_auth()
    )
    assert listed.status_code == 200
    body = listed.data.decode("utf-8", errors="replace")
    assert f"/cases/{a.id}?status=open,escalated" in body
    assert "suggest=1" in body
    assert f"/cases/{a.id}?status=open,escalated" in body and "suggest=1" in body
    # Full href for suggest row should include both
    assert f'/cases/{a.id}?status=open,escalated&suggest=1' in body.replace(
        "&amp;", "&"
    ) or f"/cases/{a.id}?status=open,escalated&amp;suggest=1" in body
    # Non-suggest case not in filtered list
    assert b.subject not in body or f"/cases/{b.id}" not in body


def test_reply_next_redirects_to_next_with_filter(dash_client):
    """Integration: reply_next lands on next open case and preserves filter qs."""
    store = CaseStore(Path(os.environ["AZOM_DATA_DIR"]) / "cases.db")
    # Two non-suggest open cases so queue order is by created_at (newest first)
    older = store.create_case(
        mailbox_id="support_default",
        subject="Older open",
        from_addr="old@example.com",
        body="hej",
        category="other",
        draft_reply="Tack, vi återkommer.",
        order_id=None,
        message_id="<reply-next-old@x>",
        status="open",
        suggest_approve=False,
    )
    newer = store.create_case(
        mailbox_id="support_default",
        subject="Newer open",
        from_addr="new@example.com",
        body="hej",
        category="other",
        draft_reply="Tack igen, vi återkommer.",
        order_id=None,
        message_id="<reply-next-new@x>",
        status="open",
        suggest_approve=False,
    )
    # Sort: newest first → newer before older; next after newer is older
    headers = _auth(client=dash_client)
    resp = dash_client.post(
        f"/cases/{newer.id}",
        headers=headers,
        data={
            "action": "reply_next",
            "body": "Tack igen, vi återkommer.",
            "q_status": "open,escalated",
            "_csrf": headers.get("X-CSRF-Token", ""),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers.get("Location") or ""
    assert older.id in loc
    assert "status=open,escalated" in loc
    assert "msg=" in loc
    # msg must be quoted-safe (no raw spaces from Swedish flash)
    assert "msg=Skickat" in loc or "msg=Skickat" in loc.replace("+", " ")


def test_overview_suggest_count(dash_client):
    store = CaseStore(Path(os.environ["AZOM_DATA_DIR"]) / "cases.db")
    _seed_two_open(store)
    resp = dash_client.get("/", headers=_auth())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "föreslå" in body.lower() or "★" in body


def test_brief_includes_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    store = CaseStore(path=tmp_path / "cases.db")
    _seed_two_open(store)
    conv = ConversationStore(path=tmp_path / "tg.json")
    text = dispatch_openclaw_command(1, "/brief", conv)
    assert text
    lower = text.lower()
    assert "cases:" in lower or "open" in lower
    assert "★" in text or "suggest" in lower or "1" in text
    assert "budget" in lower


def test_support_kpis_median(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    path = tmp_path / "telemetry.jsonl"
    tel = Telemetry(path=path)
    now = datetime.now(timezone.utc)
    for sec in (100.0, 200.0, 300.0):
        tel.record(
            action="case_replied",
            site="azom",
            meta={
                "time_to_approve_sec": sec,
                "draft_edit_distance": 0.1,
            },
        )
    # Old event outside window
    old = (now - timedelta(days=30)).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "id": "old",
                    "action": "case_replied",
                    "site": "azom",
                    "units": 1,
                    "unit_type": "api_calls",
                    "cost_usd": 0,
                    "meta": {"time_to_approve_sec": 9999},
                    "created_at": old,
                }
            )
            + "\n"
        )
    k = support_kpis_last_days(telemetry=tel, days=7, now=now)
    assert k["n_case_approved"] == 3
    assert k["median_time_to_approve_sec"] == 200.0
    assert k["mean_draft_edit_distance"] == 0.1


def test_cli_kpis(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    tel.record(
        action="case_replied",
        site="azom",
        meta={"time_to_approve_sec": 42, "draft_edit_distance": 0.05},
    )
    code = main(["kpis", "--days", "7"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n_case_approved"] == 1
    assert out["median_time_to_approve_sec"] == 42
