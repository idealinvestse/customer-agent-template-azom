"""Suggest-approve eligibility + cases_ai config rails."""

from __future__ import annotations

from ecom_ops.actions.support import SupportCategory, SupportService
from ecom_ops.cases.store import CaseStore
from ecom_ops.cases.suggest import is_suggest_approve_eligible, load_cases_ai_config


def test_cases_ai_defaults_disable_auto_send(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(tmp_path))
    # Missing file → safe defaults
    cfg = load_cases_ai_config()
    assert cfg.auto_send_enabled is False
    assert "order_status" in cfg.suggest_approve_categories
    assert "return" not in cfg.suggest_approve_categories
    assert cfg.suggest_approve_min_confidence == 0.8


def test_suggest_approve_eligible_order_status_high_confidence():
    assert is_suggest_approve_eligible(
        category="order_status",
        confidence=0.9,
        order_id="1001",
        escalated=False,
    )


def test_suggest_approve_rejects_return_even_if_confident():
    assert not is_suggest_approve_eligible(
        category="return",
        confidence=0.95,
        order_id="1001",
        escalated=False,
    )


def test_suggest_approve_rejects_missing_order_id():
    assert not is_suggest_approve_eligible(
        category="order_status",
        confidence=0.95,
        order_id=None,
        escalated=False,
    )


def test_suggest_approve_rejects_escalated():
    assert not is_suggest_approve_eligible(
        category="order_status",
        confidence=0.95,
        order_id="1001",
        escalated=True,
    )


def test_suggest_approve_rejects_low_confidence():
    assert not is_suggest_approve_eligible(
        category="order_status",
        confidence=0.5,
        order_id="1001",
        escalated=False,
    )


def test_support_result_exposes_confidence_and_suggest_flag(telemetry, escalation, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle(
        "Hej, var är min order 1001?",
        customer_name="Anna",
        customer_email="anna@example.com",
        language="sv",
        actor="agent",
    )
    assert result.ok
    assert result.category == SupportCategory.ORDER_STATUS
    assert result.order_id == "1001"
    assert isinstance(result.confidence, float)
    assert result.classify_method in {"keyword", "llm", "hybrid"}
    # Keyword fallback confidence is below suggest threshold by default
    assert result.suggest_approve is False


def test_abuse_never_suggest_approve(telemetry, escalation):
    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle(
        "This is a chargeback and legal threat regarding order 55",
        actor="agent",
    )
    assert result.ok
    assert result.escalated
    assert result.category == SupportCategory.ABUSE
    assert result.confidence == 1.0
    assert result.suggest_approve is False


def test_case_store_persists_suggest_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    assert store.schema_version() >= 3
    case = store.create_case(
        mailbox_id="mb1",
        subject="Var är order?",
        from_addr="a@b.co",
        body="order 1001",
        category="order_status",
        draft_reply="Hej",
        order_id="1001",
        message_id="<sa-1@x>",
        classify_confidence=0.91,
        classify_method="llm",
        suggest_approve=True,
    )
    loaded = store.get(case.id)
    assert loaded is not None
    assert loaded.classify_confidence == 0.91
    assert loaded.classify_method == "llm"
    assert loaded.suggest_approve is True
    assert loaded.to_dict()["suggest_approve"] is True


def test_mocked_llm_classify_can_mark_suggest_approve(telemetry, escalation, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fake_classify(**kwargs):
        return SupportCategory.ORDER_STATUS, 0.91, "llm"

    monkeypatch.setattr(
        "ecom_ops.actions.support.classify_with_llm",
        fake_classify,
    )
    # Avoid real draft LLM call
    monkeypatch.setattr(
        "ecom_ops.actions.support.draft_support_with_llm",
        lambda **kwargs: "Hej Anna,\n\nDraft\n\nAzom Support",
    )

    svc = SupportService(telemetry=telemetry, escalation=escalation)
    result = svc.handle(
        "Where is my package for order 4242?",
        customer_name="Anna",
        actor="agent",
    )
    assert result.ok
    assert result.category == SupportCategory.ORDER_STATUS
    assert result.confidence == 0.91
    assert result.classify_method == "llm"
    assert result.suggest_approve is True
