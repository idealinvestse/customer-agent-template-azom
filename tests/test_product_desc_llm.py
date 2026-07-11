"""P10: OpenRouter product description generation with budget fallback."""

from __future__ import annotations

import responses

from ecom_ops.actions.product_desc import ProductDescService
from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient
from ecom_ops.telemetry import Telemetry


@responses.activate
def test_product_desc_uses_openrouter_when_key_set(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    responses.add(
        responses.POST,
        "https://openrouter.ai/api/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "content": (
                            "SHORT: LLM korttext om Widget\n"
                            "LONG: <p>LLM lång beskrivning av Widget</p>"
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 80, "completion_tokens": 40},
        },
        status=200,
    )
    woo = WooCommerceClient(base_url="https://mock.local", transport=InMemoryWooTransport())
    svc = ProductDescService(woo=woo, telemetry=tel)
    result = svc.generate(name="Widget", features="snabb", language="sv", actor="agent")
    assert result.ok
    assert result.short_description
    assert "LLM" in (result.short_description or "") or "Widget" in (
        result.description or ""
    )
    assert tel.sum_cost_usd() > 0


@responses.activate
def test_product_desc_falls_back_over_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    tel.record(action="prior", site="azom", cost_usd=100.0)
    responses.add(
        responses.POST,
        "https://openrouter.ai/api/v1/chat/completions",
        json={"choices": [{"message": {"content": "SHORT: x\nLONG: y"}}]},
        status=200,
    )
    woo = WooCommerceClient(base_url="https://mock.local", transport=InMemoryWooTransport())
    svc = ProductDescService(woo=woo, telemetry=tel)
    result = svc.generate(name="Widget", features="x", language="sv", actor="agent")
    assert result.ok
    assert result.short_description
    assert "professionell" in (result.short_description or "").lower() or "Widget" in (
        result.short_description or ""
    )
    assert len(responses.calls) == 0
