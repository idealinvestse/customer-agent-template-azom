"""U2: LLM support drafts include Woo order context in the prompt."""

from __future__ import annotations

import pytest

from ecom_ops.actions.support import SupportCategory, SupportService
from ecom_ops.cases.service import _enrich_draft_with_order
from ecom_ops.llm import draft_support_with_llm
from ecom_ops.telemetry import Telemetry


@pytest.fixture
def tel(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return Telemetry(path=tmp_path / "telemetry.jsonl")


def test_draft_llm_prompt_includes_order_status_and_total(monkeypatch, tel, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    captured: list[list[dict[str, str]]] = []

    def _capture(messages, **kwargs):
        captured.append(messages)
        return (
            "Hej Anna,\n\nOrder 1001 behandlas.\n\nVänliga hälsningar\nAzom Support",
            0.001,
        )

    monkeypatch.setattr("ecom_ops.llm.chat_completion", _capture)

    result = draft_support_with_llm(
        customer_message="Var är order 1001?",
        category="order_status",
        language="sv",
        customer_name="Anna",
        order_id="1001",
        order_context="[Order 1001]\nStatus: processing\nTotal: 499.00 SEK",
        telemetry=tel,
    )
    assert result
    assert captured
    user = captured[0][1]["content"]
    assert "processing" in user
    assert "499.00" in user
    assert "SEK" in user
    assert "1001" in user


def test_support_passes_woo_order_context_into_llm(monkeypatch, tel, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    captured: list[dict] = []

    def _fake_draft(**kwargs):
        captured.append(kwargs)
        return "LLM draft with order context"

    monkeypatch.setattr(
        "ecom_ops.actions.support.draft_support_with_llm",
        _fake_draft,
    )
    monkeypatch.setattr(
        "ecom_ops.actions.support.hybrid_classify",
        lambda *a, **k: (SupportCategory.ORDER_STATUS, 0.9, "llm"),
    )

    svc = SupportService(telemetry=tel)
    result = svc.handle(
        "Hej, var är min order 1001?",
        language="sv",
        actor="agent",
        customer_name="Anna",
        use_mock=True,
    )
    assert result.ok
    assert result.reply == "LLM draft with order context"
    assert captured
    ctx = captured[0].get("order_context") or ""
    assert "processing" in ctx
    assert "499.00" in ctx
    assert "SEK" in ctx


def test_support_woo_miss_still_drafts_without_crash(monkeypatch, tel, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    svc = SupportService(telemetry=tel)
    result = svc.handle(
        "Var är order 9999?",
        language="sv",
        actor="agent",
        use_mock=True,
    )
    assert result.ok
    assert result.reply
    assert result.order_id == "9999"


def test_template_fallback_still_gets_prepend_enrichment(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    draft = _enrich_draft_with_order("Hej kund", "1001", use_mock=True)
    assert "Status: processing" in draft or "processing" in draft
    assert "499.00" in draft
    assert "SEK" in draft
    assert "Hej kund" in draft


def test_enrich_skips_second_woo_fetch_when_block_present(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    already = (
        "[Order 1001]\nStatus: processing\nTotal: 499.00 SEK\n\n"
        "Hej från LLM"
    )
    calls: list[str] = []

    class _Woo:
        def get_order(self, oid):
            calls.append(str(oid))
            raise AssertionError("should not fetch when block present")

    monkeypatch.setattr(
        "ecom_ops.integrations.woocommerce.client_from_env",
        lambda **kwargs: _Woo(),
    )
    out = _enrich_draft_with_order(already, "1001", use_mock=True)
    assert "[Order 1001]" in out
    assert "Hej från LLM" in out
    assert calls == []
