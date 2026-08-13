"""FAQ context injected into support drafts."""

from __future__ import annotations

from ecom_ops.actions.support import SupportCategory, SupportService
from ecom_ops.faq.config import clear_faq_config_cache
from ecom_ops.faq.store import clear_faq_store_cache
from ecom_ops.llm import draft_support_with_llm
from ecom_ops.telemetry import Telemetry


def test_draft_llm_prompt_includes_faq_context(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    captured: list[list[dict[str, str]]] = []

    def _capture(messages, **kwargs):
        captured.append(messages)
        return ("Hej,\n\nHär är svar.\n\nAzom Support", 0.001)

    monkeypatch.setattr("ecom_ops.llm.chat_completion", _capture)

    result = draft_support_with_llm(
        customer_message="När kommer paketet?",
        category="shipping",
        language="sv",
        customer_name="Anna",
        order_id="1001",
        order_context="[Order 1001]\nStatus: processing",
        faq_context="- [se-shipping-delivery-times] Leveranstider: 1–3 arbetsdagar",
        telemetry=tel,
    )
    assert result
    user = captured[0][1]["content"]
    assert "FAQ context:" in user
    assert "se-shipping-delivery-times" in user


def test_support_handle_attaches_faq_ids(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_FAQ_INJECT_INTO_DRAFT", "1")
    clear_faq_config_cache()
    clear_faq_store_cache()
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")

    monkeypatch.setattr(
        "ecom_ops.actions.support.hybrid_classify",
        lambda *a, **k: (SupportCategory.SHIPPING, 0.9, "llm"),
    )
    monkeypatch.setattr(
        "ecom_ops.actions.support.draft_support_with_llm",
        lambda **kwargs: "Draft med FAQ",
    )

    svc = SupportService(telemetry=tel)
    result = svc.handle(
        "Hej, hur spårar jag mitt paket?",
        language="sv",
        market="se",
        actor="agent",
        use_mock=True,
    )
    assert result.ok
    assert result.faq_article_ids
    assert any("shipping" in i or "tracking" in i for i in result.faq_article_ids)


def test_template_fallback_includes_faq_context(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_FAQ_INJECT_INTO_DRAFT", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    clear_faq_config_cache()
    clear_faq_store_cache()
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")

    monkeypatch.setattr(
        "ecom_ops.actions.support.hybrid_classify",
        lambda *a, **k: (SupportCategory.SHIPPING, 0.9, "llm"),
    )
    monkeypatch.setattr(
        "ecom_ops.actions.support.draft_support_with_llm",
        lambda **kwargs: None,
    )

    svc = SupportService(telemetry=tel)
    result = svc.handle(
        "Hej, hur spårar jag mitt paket?",
        language="sv",
        market="se",
        actor="agent",
        use_mock=True,
    )
    assert result.ok
    assert result.reply
    assert "FAQ context:" in result.reply
    assert result.faq_article_ids
