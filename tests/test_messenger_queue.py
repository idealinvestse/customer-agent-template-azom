"""Durable SQLite Messenger queue, typing_on, and 1950-char chunking tests."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path

from ecom_ops.bot.messenger_adapter import (
    InboundEvent,
    reply_to_messenger_messages,
    send_sender_action,
)
from ecom_ops.bot.messenger_queue import MessengerJobQueue
from ecom_ops.bot.reply import ActionButton, ActionMarkup, BotReply

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def test_messenger_chunking_preserves_full_text_and_buttons():
    long_text = ("Första stycket med viktig info. " * 60) + "\n\n" + ("Andra stycket med avslut. " * 60)
    assert len(long_text) > 2500
    reply = BotReply(
        text=long_text,
        actions=ActionMarkup(rows=[[ActionButton(label="Godkänn", payload="cases:approve:1")]]),
    )
    msgs = reply_to_messenger_messages(reply)
    assert len(msgs) >= 3
    text_parts = [m["text"] for m in msgs if "text" in m]
    combined = " ".join(text_parts)
    assert "Första stycket" in combined
    assert "Andra stycket" in combined
    for part in text_parts:
        assert len(part) <= 1950
    last = msgs[-1]
    assert last["attachment"]["type"] == "template"
    assert last["attachment"]["payload"]["buttons"][0]["payload"] == "cases:approve:1"


def test_sender_action_dry_run():
    res = send_sender_action("PSID1", "typing_on", dry_run=True)
    assert res["dry_run"] is True
    assert res["body"]["sender_action"] == "typing_on"


def test_messenger_queue_dedup_and_restart_reconcile(tmp_path):
    q = MessengerJobQueue(tmp_path / "messenger_jobs.sqlite3")
    ok1, jid1 = q.enqueue(InboundEvent(peer_id="P1", text="/help", mid="m-100"))
    ok2, jid2 = q.enqueue(InboundEvent(peer_id="P1", text="/help", mid="m-100"))
    assert ok1 is True and jid1 is not None
    assert ok2 is False and jid2 is None

    # Simulate crashed running job -> reconcile_running resets to pending
    with q._connect() as conn:
        conn.execute("UPDATE messenger_jobs SET status='running' WHERE id=?", (jid1,))
    assert q.reconcile_running() == 1
    assert q.stats()["pending"] == 1


def test_messenger_webhook_async_returns_fast(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sites.yaml").write_text("customer: azom\ndomains:\n  - se\n", encoding="utf-8")
    (cfg / "limits.yaml").write_text("openrouter_cap: 100\n", encoding="utf-8")
    (cfg / "integrations.yaml").write_text("email:\n  enabled: true\n", encoding="utf-8")
    (cfg / "rbac.yaml").write_text("roles:\n  jonatan: viewer\n  oscar: full_admin\n", encoding="utf-8")
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("MESSENGER_VERIFY_TOKEN", "verify-token")
    monkeypatch.setenv("MESSENGER_APP_SECRET", "app-secret")
    monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", "PSID1")
    monkeypatch.setenv("MESSENGER_ACTOR_MAP", "PSID1:jonatan")
    monkeypatch.setenv("AZOM_MESSENGER_ASYNC_IN_TESTS", "1")
    monkeypatch.chdir(DASH_DIR)
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    spec = importlib.util.spec_from_file_location("azom_dash_async", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dash_async"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    mod.app.config["TESTING"] = True
    client = mod.app.test_client()

    body = json.dumps(
        {
            "object": "page",
            "entry": [{"messaging": [{"sender": {"id": "PSID1"}, "message": {"mid": "async-1", "text": "/help"}}]}],
        }
    ).encode("utf-8")
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    t0 = time.monotonic()
    resp = client.post("/webhooks/messenger", data=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert resp.status_code == 200
    assert (time.monotonic() - t0) < 1.0
    payload = resp.get_json()
    assert payload["queued"] == 1

    from ecom_ops.bot.messenger_queue import get_messenger_queue

    q = get_messenger_queue(data)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if q.stats()["done"] >= 1:
            break
        time.sleep(0.05)
    assert q.stats()["done"] == 1
    q.stop()
