"""llm.py stays a facade and no longer requires OPENROUTER_API_KEY."""

from __future__ import annotations

import json

import pytest
import responses

from ecom_ops.llm import (
    chat_completion,
    classify_support_with_llm,
    draft_support_with_llm,
    generate_product_desc_with_llm,
)
from ecom_ops.model_router.xai_api_key import XAI_CHAT_URL
from ecom_ops.telemetry import Telemetry


@pytest.fixture
def tel(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    return Telemetry(path=tmp_path / "telemetry.jsonl")


def _use_xai(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "xai_api_key")
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-should-not-leak")


@responses.activate
def test_chat_completion_without_openrouter_key(monkeypatch, tel):
    _use_xai(monkeypatch)
    responses.add(
        responses.POST,
        XAI_CHAT_URL,
        json={
            "choices": [{"message": {"content": "pong"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        },
        status=200,
    )
    content, cost = chat_completion(
        [{"role": "user", "content": "ping"}],
        telemetry=tel,
    )
    assert content == "pong"
    assert cost == 0.0
    assert isinstance(content, str)
    text = tel.path.read_text(encoding="utf-8")
    assert "xai-secret-should-not-leak" not in text
    assert "xai_api_key" in text


@responses.activate
def test_classify_draft_and_product_use_router(monkeypatch, tel):
    _use_xai(monkeypatch)
    responses.add(
        responses.POST,
        XAI_CHAT_URL,
        json={
            "choices": [
                {
                    "message": {
                        "content": '{"category":"shipping","confidence":0.91}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        status=200,
    )
    parsed = classify_support_with_llm(
        customer_message="Var är paketet?",
        language="sv",
        telemetry=tel,
    )
    assert parsed == ("shipping", 0.91)

    responses.add(
        responses.POST,
        XAI_CHAT_URL,
        json={
            "choices": [{"message": {"content": "Hej,\n\nVi tittar på det.\n\nAzom Support"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        status=200,
    )
    draft = draft_support_with_llm(
        customer_message="Var är paketet?",
        category="shipping",
        language="sv",
        customer_name="Anna",
        order_id="1001",
        telemetry=tel,
    )
    assert draft
    assert "Azom Support" in draft

    responses.add(
        responses.POST,
        XAI_CHAT_URL,
        json={
            "choices": [{"message": {"content": "SHORT: Kort text\nLONG: <p>Lång text</p>"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        status=200,
    )
    product = generate_product_desc_with_llm(
        name="Lampa",
        features="matt",
        language="sv",
        telemetry=tel,
    )
    assert product == ("Kort text", "<p>Lång text</p>")
    text = tel.path.read_text(encoding="utf-8")
    assert "xai-secret-should-not-leak" not in text
    assert "openai/gpt-4o-mini" not in text
    business = {
        "llm_support_classify",
        "llm_support_draft",
        "llm_product_desc",
    }
    seen = {
        json.loads(line)["action"]: json.loads(line)
        for line in text.splitlines()
        if line.strip()
    }
    for action in business:
        assert seen[action]["meta"]["model"] == "grok-4"


def test_template_fallback_when_no_route(monkeypatch, tel):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert (
        draft_support_with_llm(
            customer_message="Hej",
            category="other",
            language="sv",
            customer_name=None,
            order_id=None,
            telemetry=tel,
        )
        is None
    )
    assert (
        generate_product_desc_with_llm(
            name="Lampa",
            features="",
            language="sv",
            telemetry=tel,
        )
        is None
    )


def test_budget_skips_openrouter_without_http(monkeypatch, tel):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "openrouter")
    tel.record(action="prior", site="azom", cost_usd=100.0)

    def boom(*_args, **_kwargs):
        raise AssertionError("OpenRouter was called despite the budget cap")

    monkeypatch.setattr("ecom_ops.model_router.openrouter.requests.post", boom)
    assert (
        draft_support_with_llm(
            customer_message="Hej",
            category="other",
            language="sv",
            customer_name=None,
            order_id=None,
            telemetry=tel,
        )
        is None
    )


def test_chat_does_not_send_openrouter_slug_to_other_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-should-not-leak")
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "xai_api_key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    from ecom_ops.bot.chat_agent import run_chat
    from ecom_ops.model_router import reset_model_router

    reset_model_router()
    seen: dict[str, object] = {}

    def fake_chat(messages, **kwargs):
        seen["model"] = kwargs.get("model")
        return "pong", 0.0

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)
    result = run_chat("Hej", telemetry=Telemetry(path=tmp_path / "tel.jsonl"))
    assert result.text == "pong"
    assert seen["model"] is None
