"""Shadow FU9 observation under null-send profile."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from ecom_ops.cases.auto_send import explain_auto_send
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import SCHEMA_VERSION, Case, CaseStore
from ecom_ops.cases.suggest import CasesAiConfig
from ecom_ops.telemetry import Telemetry


def _cfg(**overrides) -> CasesAiConfig:
    base = dict(
        suggest_approve_categories=("order_status", "shipping"),
        suggest_approve_min_confidence=0.8,
        suggest_approve_require_order_id=True,
        never_suggest_categories=("abuse", "return", "billing"),
        auto_send_enabled=True,
        auto_send_categories=("order_status",),
        auto_send_min_confidence=0.92,
        max_auto_sends_per_day=10,
        kill_switch_env="AZOM_AUTO_SEND_KILL",
        auto_send_experiment_name="",
    )
    base.update(overrides)
    return CasesAiConfig(**base)


def test_explain_auto_send_reason_codes(monkeypatch):
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    ok, reason = explain_auto_send(
        category="order_status",
        confidence=0.95,
        order_id="1001",
        escalated=False,
        config=_cfg(),
    )
    assert ok is True
    assert reason == "eligible"
    denied, why = explain_auto_send(
        category="order_status",
        confidence=0.95,
        order_id=None,
        escalated=False,
        config=_cfg(),
    )
    assert denied is False
    assert why == "missing_order_id"


def test_to_dict_preserves_none_vs_false():
    c = Case(
        id="x",
        mailbox_id="m",
        subject="s",
        from_addr="a@b.co",
        category="other",
        status="open",
        order_id=None,
        draft_reply=None,
        message_id=None,
        site="azom",
        market=None,
        language="sv",
        created_at="t",
        updated_at="t",
        shadow_eligible=None,
    )
    assert c.to_dict()["shadow_eligible"] is None
    c2 = Case(
        id="y",
        mailbox_id="m",
        subject="s",
        from_addr="a@b.co",
        category="other",
        status="open",
        order_id=None,
        draft_reply=None,
        message_id=None,
        site="azom",
        market=None,
        language="sv",
        created_at="t",
        updated_at="t",
        shadow_eligible=False,
        shadow_deny_reason="missing_order_id",
    )
    assert c2.to_dict()["shadow_eligible"] is False
    assert c2.shadow_hint() == "Skugga: nej (saknar order_id)"


def test_schema_v5_nullable_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    assert store.schema_version() >= 5
    assert SCHEMA_VERSION == 5
    case = store.create_case(
        mailbox_id="support_default",
        subject="Hej",
        from_addr="a@b.co",
        body="hej",
        category="other",
        draft_reply="svar",
        order_id=None,
        message_id="<shadow-schema@azom>",
    )
    assert case.shadow_eligible is None
    updated = store.set_shadow_decision(
        case.id, eligible=False, deny_reason="missing_order_id"
    )
    assert updated is not None
    assert updated.shadow_eligible is False
    assert updated.shadow_deny_reason == "missing_order_id"


def test_record_shadow_missing_order(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    monkeypatch.setattr(
        "ecom_ops.cases.auto_send.load_cases_ai_config",
        lambda: _cfg(auto_send_enabled=True),
    )
    store = CaseStore(path=tmp_path / "cases.db")
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Var är min order?",
        from_addr="a@b.co",
        body="Saknar nummer",
        category="order_status",
        draft_reply="Kan du skicka ordernummer?",
        order_id=None,
        message_id="<shadow-miss-oid@azom>",
        classify_confidence=0.95,
        classify_method="fixture",
    )
    svc = CaseService(store=store, telemetry=tel)
    out = svc._maybe_record_shadow(case)
    assert out.shadow_eligible is False
    assert out.shadow_deny_reason == "missing_order_id"
    events = [
        json.loads(line)
        for line in Path(tmp_path / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    shadow = [e for e in events if e.get("action") == "case_shadow_decision"]
    assert shadow
    meta = shadow[-1]["meta"]
    assert meta["shadow_eligible"] is False
    assert meta["shadow_deny_reason"] == "missing_order_id"
    assert "subject" not in meta
    assert "body" not in meta


def test_ae6_eligible_but_send_blocked(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    monkeypatch.setattr(
        "ecom_ops.cases.auto_send.load_cases_ai_config",
        lambda: _cfg(auto_send_enabled=True),
    )
    store = CaseStore(path=tmp_path / "cases.db")
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="Var är order 1001?",
        category="order_status",
        draft_reply="Ordern är skickad.",
        order_id="1001",
        message_id="<shadow-ae6@azom>",
        classify_confidence=0.95,
        classify_method="fixture",
    )
    client = MagicMock()
    from ecom_ops.actions.mail import MailService

    svc = CaseService(
        store=store,
        mail=MailService(client=client, telemetry=tel),
        telemetry=tel,
    )
    shadowed = svc._maybe_record_shadow(case)
    assert shadowed.shadow_eligible is True
    assert shadowed.shadow_deny_reason is None
    result = svc.approve_and_send(case.id, actor="jonatan")
    assert result.ok is False
    client.send.assert_not_called()


def test_null_send_off_does_not_clear_or_write(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order",
        category="order_status",
        draft_reply="ok",
        order_id="1001",
        message_id="<shadow-off@azom>",
        classify_confidence=0.95,
    )
    store.set_shadow_decision(case.id, eligible=False, deny_reason="auto_send_disabled")
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    svc = CaseService(store=store, telemetry=tel)
    out = svc._maybe_record_shadow(store.get(case.id))  # type: ignore[arg-type]
    assert out.shadow_eligible is False
    assert out.shadow_deny_reason == "auto_send_disabled"
    tel_path = Path(tmp_path / "telemetry.jsonl")
    if tel_path.is_file():
        assert "case_shadow_decision" not in tel_path.read_text(encoding="utf-8")
