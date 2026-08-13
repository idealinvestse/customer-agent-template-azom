"""FAQ ingest RBAC, kill-switch, PII, and config fail-closed behavior."""

from __future__ import annotations

import pytest

from ecom_ops.faq.config import clear_faq_config_cache, load_faq_config
from ecom_ops.faq.ingest_config import (
    clear_faq_ingest_config_cache,
    effective_use_mock,
    faq_ingest_killed,
    load_faq_ingest_config,
)
from ecom_ops.faq.kill_switch import faq_publish_killed
from ecom_ops.faq.pii import redact_pii
from ecom_ops.faq.rbac_ingest import require_faq_ingest, require_faq_promote
from ecom_ops.rbac import AccessDenied, Actor


def test_redact_pii_empty_and_plus_phone() -> None:
    assert redact_pii("") == ""
    out = redact_pii("Ring +46 70 123 45 67 tack.")
    assert "+46 70 123 45 67" not in out
    assert "[TELEFON]" in out


def test_require_faq_ingest_mock_operator_ok() -> None:
    actor = require_faq_ingest("agent", use_mock=True)
    assert actor.role == "operator"


def test_require_faq_ingest_mock_viewer_denied() -> None:
    with pytest.raises(AccessDenied):
        require_faq_ingest("jonatan", use_mock=True)


def test_require_faq_ingest_live_operator_denied() -> None:
    with pytest.raises(AccessDenied):
        require_faq_ingest("agent", use_mock=False)


def test_require_faq_ingest_live_oscar_ok() -> None:
    actor = require_faq_ingest("oscar", use_mock=False)
    assert actor.name == "oscar"


def test_require_faq_promote_always_oscar() -> None:
    with pytest.raises(AccessDenied):
        require_faq_promote("agent")
    with pytest.raises(AccessDenied):
        require_faq_promote(Actor("jonatan", "viewer"))
    assert require_faq_promote("oscar").name == "oscar"


def test_faq_ingest_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZOM_FAQ_INGEST_KILL", raising=False)
    clear_faq_ingest_config_cache()
    assert faq_ingest_killed() is False
    monkeypatch.setenv("AZOM_FAQ_INGEST_KILL", "yes")
    assert faq_ingest_killed() is True


def test_faq_publish_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZOM_FAQ_PUBLISH_KILL", raising=False)
    clear_faq_config_cache()
    assert faq_publish_killed() is False
    monkeypatch.setenv("AZOM_FAQ_PUBLISH_KILL", "on")
    clear_faq_config_cache()
    assert faq_publish_killed() is True


def test_effective_use_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    assert effective_use_mock(True) is True
    assert effective_use_mock(False) is False
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    assert effective_use_mock(None) is True
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    assert effective_use_mock(None) is False


def test_faq_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_FAQ_ENABLED", "0")
    monkeypatch.setenv("AZOM_FAQ_INJECT_INTO_DRAFT", "0")
    clear_faq_config_cache()
    cfg = load_faq_config()
    assert cfg.enabled is False
    assert cfg.inject_into_draft is False
    monkeypatch.setenv("AZOM_FAQ_ENABLED", "1")
    monkeypatch.setenv("AZOM_FAQ_INJECT_INTO_DRAFT", "1")
    clear_faq_config_cache()
    cfg2 = load_faq_config()
    assert cfg2.enabled is True
    assert cfg2.inject_into_draft is True


def test_ingest_config_loads_repo_defaults() -> None:
    clear_faq_ingest_config_cache()
    cfg = load_faq_ingest_config()
    assert cfg.dataset_max_age_days == 365
    assert "abuse" in cfg.dataset_exclude_categories
    assert cfg.llm_suggest_enabled is False
    assert "azom.se" in cfg.guide_allowlist_hosts
    assert cfg.ingest_kill_env == "AZOM_FAQ_INGEST_KILL"
