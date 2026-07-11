"""OpenRouter-assisted support drafts with budget cap and template fallback."""

from __future__ import annotations

import pytest
import responses

from ecom_ops.actions.support import SupportService, draft_reply, SupportCategory
from ecom_ops.telemetry import Telemetry


@pytest.fixture
def tel(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    path = tmp_path / "telemetry.jsonl"
    return Telemetry(path=path)


def test_support_draft_falls_back_to_template_without_api_key(monkeypatch, tel):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = SupportService(telemetry=tel)
    result = svc.handle("Var är order 1001?", language="sv", actor="agent")
    assert result.ok
    assert result.reply
    assert "1001" in result.reply or "order" in result.reply.lower()
    # Template signature
    assert "Azom Support" in result.reply or "Vänliga" in result.reply


@responses.activate
def test_support_uses_openrouter_when_key_set(monkeypatch, tel, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    # Generous cap via config already 100; ensure telemetry empty
    responses.add(
        responses.POST,
        "https://openrouter.ai/api/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "content": (
                            "Hej,\n\nVi ser att order 1001 är under behandling "
                            "och återkommer snart.\n\nVänliga hälsningar\nAzom Support"
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
        status=200,
    )
    svc = SupportService(telemetry=tel)
    result = svc.handle(
        "Hej, var är min order 1001?",
        language="sv",
        actor="agent",
        customer_name="Anna",
    )
    assert result.ok
    assert result.reply
    assert "under behandling" in result.reply
    assert tel.sum_cost_usd() > 0


@responses.activate
def test_support_falls_back_when_over_budget(monkeypatch, tel, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    # Exhaust budget
    tel.record(action="prior", site="azom", cost_usd=100.0)
    responses.add(
        responses.POST,
        "https://openrouter.ai/api/v1/chat/completions",
        json={"choices": [{"message": {"content": "LLM text"}}]},
        status=200,
    )
    svc = SupportService(telemetry=tel)
    result = svc.handle("Var är order 1001?", language="sv", actor="agent")
    assert result.ok
    assert result.reply
    assert "LLM text" not in result.reply
    assert len(responses.calls) == 0


@responses.activate
def test_support_falls_back_on_openrouter_error(monkeypatch, tel, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    responses.add(
        responses.POST,
        "https://openrouter.ai/api/v1/chat/completions",
        json={"error": "boom"},
        status=500,
    )
    svc = SupportService(telemetry=tel)
    result = svc.handle("Var är order 1001?", language="sv", actor="agent")
    assert result.ok
    assert result.reply
    template = draft_reply(
        category=SupportCategory.ORDER_STATUS,
        customer_name=None,
        order_id="1001",
        language="sv",
    )
    # Should look like template (contains familiar sign-off)
    assert "Azom Support" in result.reply or "återkommer" in result.reply.lower()
