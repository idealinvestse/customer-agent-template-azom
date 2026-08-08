"""Null-send profile: refuse customer mail at MailService + approve_and_send."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from ecom_ops.actions.mail import NULL_SEND_REFUSED_MSG, MailService
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.cli import build_parser, main
from ecom_ops.runtime_profile import (
    null_send_active,
    null_send_label,
)
from ecom_ops.telemetry import Telemetry


def test_null_send_env_and_label(monkeypatch):
    # Use setenv first so monkeypatch always owns the key (delenv on missing
    # registers no undo; enable_null_send would otherwise leak into later tests).
    monkeypatch.setenv("AZOM_NULL_SEND", "")
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    assert null_send_active() is False
    assert null_send_label() == "off"
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    assert null_send_active() is True
    assert null_send_label() == "on"
    monkeypatch.setenv("AZOM_NULL_SEND", "0")
    assert null_send_active() is False


def test_cli_null_send_flag_sets_env(monkeypatch):
    monkeypatch.setenv("AZOM_NULL_SEND", "")
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    parser = build_parser()
    args = parser.parse_args(["--null-send", "status"])
    assert args.null_send is True
    # main() applies enable_null_send (direct os.environ write)
    code = main(["--null-send", "version"])
    assert code == 0
    assert null_send_active() is True
    # Re-claim via monkeypatch so teardown clears the leak from enable_null_send
    monkeypatch.setenv("AZOM_NULL_SEND", "1")


def test_status_always_includes_null_send(monkeypatch, capsys):
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    code = main(["status"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert code in (0, 1)
    assert data["null_send"] in {"on", "off"}


def test_mail_send_refuses_without_client_call(monkeypatch):
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    client = MagicMock()
    svc = MailService(client=client)
    result = svc.send(
        to="a@b.co",
        subject="Hej",
        body="Test",
        actor="oscar",
    )
    assert result.ok is False
    assert "Null-send" in result.message
    client.send.assert_not_called()


def test_mail_reply_refuses_without_client_call(monkeypatch):
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    client = MagicMock()
    svc = MailService(client=client)
    result = svc.reply(
        to="a@b.co",
        subject="Re: Hej",
        body="Test",
        actor="oscar",
    )
    assert result.ok is False
    assert NULL_SEND_REFUSED_MSG in result.message
    client.send.assert_not_called()
    if hasattr(client, "reply"):
        client.reply.assert_not_called()


def test_approve_and_send_refuses_before_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="Var är order 1001?",
        category="order_status",
        draft_reply="Hej, ordern är på väg.",
        order_id="1001",
        message_id="<null-send-approve@azom>",
        site="azom",
    )
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    client = MagicMock()
    svc = CaseService(
        store=store,
        mail=MailService(client=client, telemetry=tel),
        telemetry=tel,
    )
    # Spy claim via wrapping store
    claimed = {"n": 0}
    orig = store.claim_for_send

    def _claim(cid):
        claimed["n"] += 1
        return orig(cid)

    store.claim_for_send = _claim  # type: ignore[method-assign]
    result = svc.approve_and_send(case.id, actor="jonatan")
    assert result.ok is False
    assert "Null-send" in result.message
    assert claimed["n"] == 0
    client.send.assert_not_called()
    fresh = store.get(case.id)
    assert fresh is not None
    assert fresh.status == "open"
    lines = Path(tmp_path / "telemetry.jsonl").read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    blocked = [e for e in events if e.get("action") == "case_reply_blocked_null_send"]
    assert blocked
    meta = blocked[-1].get("meta") or {}
    assert "subject" not in meta
    assert "body" not in meta
    assert "from" not in meta
    assert "to" not in meta


def test_null_send_off_send_still_works(monkeypatch):
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    client = MagicMock()
    client.send.return_value = {"to": ["a@b.co"], "message_id": "<m1>"}
    svc = MailService(client=client)
    result = svc.send(
        to="a@b.co",
        subject="Hej",
        body="Test",
        actor="oscar",
    )
    assert result.ok is True
    client.send.assert_called_once()
