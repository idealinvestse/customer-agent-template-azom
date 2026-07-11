"""Auto-send guardrails (Path B U3) — rails only; default never sends."""

from __future__ import annotations

import inspect

from ecom_ops.cases.auto_send import (
    AUTO_SEND_TELEMETRY_ACTION,
    AutoSendDayCounter,
    should_auto_send,
)
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.suggest import CasesAiConfig, load_cases_ai_config


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
    )
    base.update(overrides)
    return CasesAiConfig(**base)


def _eligible_kwargs(**overrides):
    base = dict(
        category="order_status",
        confidence=0.95,
        order_id="1001",
        escalated=False,
        auto_sends_today=0,
        config=_cfg(),
    )
    base.update(overrides)
    return base


def test_yaml_default_auto_send_disabled():
    cfg = load_cases_ai_config()
    assert cfg.auto_send_enabled is False
    assert cfg.auto_send_min_confidence >= 0.92
    assert cfg.max_auto_sends_per_day > 0
    assert cfg.kill_switch_env == "AZOM_AUTO_SEND_KILL"


def test_flag_off_always_denies(monkeypatch):
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    assert not should_auto_send(**_eligible_kwargs(config=_cfg(auto_send_enabled=False)))


def test_deny_by_default_without_explicit_enable(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    # Missing yaml → safe defaults (enabled false)
    assert not should_auto_send(
        category="order_status",
        confidence=0.99,
        order_id="1001",
        escalated=False,
        auto_sends_today=0,
    )


def test_kill_switch_overrides_enabled(monkeypatch):
    monkeypatch.setenv("AZOM_AUTO_SEND_KILL", "1")
    assert not should_auto_send(**_eligible_kwargs())


def test_allowlist_miss_denies(monkeypatch):
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    assert not should_auto_send(**_eligible_kwargs(category="shipping"))
    assert not should_auto_send(**_eligible_kwargs(category="return", confidence=0.99))
    assert not should_auto_send(**_eligible_kwargs(category="abuse", confidence=1.0))


def test_low_confidence_denies(monkeypatch):
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    assert not should_auto_send(**_eligible_kwargs(confidence=0.91))


def test_missing_order_id_denies(monkeypatch):
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    assert not should_auto_send(**_eligible_kwargs(order_id=None))
    assert not should_auto_send(**_eligible_kwargs(order_id=""))


def test_escalated_denies(monkeypatch):
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    assert not should_auto_send(**_eligible_kwargs(escalated=True))


def test_daily_cap_reached_denies(monkeypatch):
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    assert not should_auto_send(**_eligible_kwargs(auto_sends_today=10))
    assert not should_auto_send(**_eligible_kwargs(auto_sends_today=11))


def test_eligible_when_all_rails_pass(monkeypatch):
    monkeypatch.delenv("AZOM_AUTO_SEND_KILL", raising=False)
    assert should_auto_send(**_eligible_kwargs(auto_sends_today=9))


def test_day_counter_interface(tmp_path):
    counter = AutoSendDayCounter(path=tmp_path / "auto_send_count.json")
    assert counter.count_today() == 0
    counter.increment()
    counter.increment()
    assert counter.count_today() == 2


def test_telemetry_action_name_reserved():
    assert AUTO_SEND_TELEMETRY_ACTION == "case_auto_sent"


def test_poll_source_does_not_call_auto_send():
    """Safety: poll must not wire auto-send (rails only until Oscar experiment)."""
    src = inspect.getsource(CaseService.poll)
    assert "should_auto_send" not in src
    assert "evaluate_auto_send" not in src
    assert "case_auto_sent" not in src
    assert "AutoSendDayCounter" not in src


def test_service_checkpoint_denies_when_disabled():
    """Documented CaseService hook exists but stays deny-by-default."""
    svc = CaseService.__new__(CaseService)
    assert not svc.evaluate_auto_send_eligibility(
        {
            "category": "order_status",
            "classify_confidence": 0.99,
            "order_id": "1001",
            "status": "open",
            "escalation_id": None,
        }
    )
