"""Support-loop KPI telemetry: time-to-approve and draft edit distance."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ecom_ops.actions.mail import MailService
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailProvider,
)
from ecom_ops.telemetry import Telemetry


def _events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_approve_records_time_to_approve_sec(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel_path = tmp_path / "telemetry.jsonl"
    tel = Telemetry(path=tel_path)
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="KPI",
        from_addr="kund@example.com",
        body="help",
        category="other",
        draft_reply="Original draft text here",
        order_id=None,
        message_id="<kpi-approve@x>",
    )
    # Backdate created_at by 120 seconds via SQL
    past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with store._conn() as conn:
        conn.execute(
            "UPDATE cases SET created_at = ? WHERE id = ?", (past, case.id)
        )

    transport = InMemoryMailTransport()
    client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP, from_addr="support@azom.se"
        ),
        transport=transport,
    )
    mail = MailService(client=client, telemetry=tel)
    svc = CaseService(store=store, mail=mail, telemetry=tel)
    result = svc.approve_and_send(case.id, actor="jonatan")
    assert result.ok, result.message
    events = [e for e in _events(tel_path) if e.get("action") == "case_replied"]
    assert events
    meta = events[-1].get("meta") or {}
    assert "time_to_approve_sec" in meta
    assert float(meta["time_to_approve_sec"]) >= 100
    assert "suggest_approve" in meta
    assert meta["suggest_approve"] is False


def test_approve_records_suggest_approve_true(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel_path = tmp_path / "telemetry.jsonl"
    tel = Telemetry(path=tel_path)
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="KPI suggest",
        from_addr="kund@example.com",
        body="Var är order 1001?",
        category="order_status",
        draft_reply="Din order är under behandling.",
        order_id="1001",
        message_id="<kpi-suggest@x>",
        suggest_approve=True,
    )
    transport = InMemoryMailTransport()
    client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP, from_addr="support@azom.se"
        ),
        transport=transport,
    )
    mail = MailService(client=client, telemetry=tel)
    svc = CaseService(store=store, mail=mail, telemetry=tel)
    result = svc.approve_and_send(case.id, actor="jonatan")
    assert result.ok, result.message
    events = [e for e in _events(tel_path) if e.get("action") == "case_replied"]
    assert events
    assert events[-1]["meta"].get("suggest_approve") is True


def test_save_draft_records_edit_distance(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel_path = tmp_path / "telemetry.jsonl"
    tel = Telemetry(path=tel_path)
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Edit KPI",
        from_addr="kund@example.com",
        body="help",
        category="other",
        draft_reply="Hej, vi hjälper dig snart.",
        order_id=None,
        message_id="<kpi-edit@x>",
    )
    svc = CaseService(store=store, telemetry=tel)
    result = svc.save_draft(
        case.id,
        "Hej Anna, vi har kollat order 1001 och skickar den idag.",
        actor="jonatan",
    )
    assert result.ok
    events = [e for e in _events(tel_path) if e.get("action") == "case_draft_saved"]
    assert events
    meta = events[-1].get("meta") or {}
    assert "draft_edit_distance" in meta
    assert float(meta["draft_edit_distance"]) > 0
