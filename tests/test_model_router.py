"""Router order, fallback, and safe telemetry."""

from __future__ import annotations

import json

import pytest

from ecom_ops.model_router.base import ModelRequest, ModelResult
from ecom_ops.model_router.errors import ModelAuthError, ModelConfigError, ModelRouteError
from ecom_ops.model_router.router import ModelRouter
from ecom_ops.telemetry import Telemetry


def _request() -> ModelRequest:
    return ModelRequest(messages=[{"role": "user", "content": "hej"}])


class _Ok:
    def __init__(self, provider: str, route_id: str, content: str = "ok") -> None:
        self.provider = provider
        self.route_id = route_id
        self.content = content
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    def status(self) -> dict:
        return {"route_id": self.route_id}

    def complete(self, request, *, telemetry=None):
        self.calls += 1
        return ModelResult(
            content=self.content,
            provider=self.provider,
            route_id=self.route_id,
            model="m",
            usage_known=True,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        )


class _Boom:
    provider = "openai"
    route_id = "chatgpt_oauth"

    def is_configured(self) -> bool:
        return True

    def status(self) -> dict:
        return {}

    def complete(self, request, *, telemetry=None):
        raise ModelAuthError("denied", code="needs_reauth")


def test_router_skips_unconfigured_and_uses_openrouter(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_SIWC_CLIENT_ID", raising=False)
    monkeypatch.delenv("XAI_GROK_BIN", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "chatgpt_oauth,xai_grok_build,xai_api_key,openrouter")
    router = ModelRouter()
    fake = _Ok("openrouter", "openrouter_api_key")
    router.openrouter = fake
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    result = router.complete(_request(), telemetry=tel)
    assert result.content == "ok"
    assert result.route_id == "openrouter_api_key"
    assert fake.calls == 1
    lines = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "model_route_selected" in lines
    assert "openrouter_api_key" in lines
    assert "test-key" not in lines


def test_router_respects_order(monkeypatch):
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "openrouter,xai_api_key")
    router = ModelRouter()
    first = _Ok("openrouter", "openrouter_api_key", "first")
    second = _Ok("xai", "xai_api_key", "second")
    router.openrouter = first
    router.xai_api = second
    result = router.complete(_request())
    assert result.content == "first"
    assert second.calls == 0


def test_router_falls_through_after_auth_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "chatgpt_oauth,openrouter")
    router = ModelRouter()
    router.chatgpt = _Boom()
    router.openrouter = _Ok("openrouter", "openrouter_api_key", "fallback")
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    result = router.complete(_request(), telemetry=tel)
    assert result.content == "fallback"
    events = [
        json.loads(line)
        for line in (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    errors = [event for event in events if event["action"] == "model_route_error"]
    assert errors
    assert errors[0]["meta"]["error_class"] == "ModelAuthError"
    assert errors[0]["meta"]["error_code"] == "needs_reauth"
    assert "denied" not in json.dumps(errors)


def test_router_raises_when_none_succeed(monkeypatch):
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "chatgpt_oauth")
    router = ModelRouter()
    router.chatgpt = _Boom()
    with pytest.raises(ModelRouteError):
        router.complete(_request())


def test_router_unknown_route_is_config_error(monkeypatch):
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "not_a_route")
    router = ModelRouter()
    with pytest.raises(ModelRouteError):
        router.complete(_request())


def test_complete_route_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    router = ModelRouter()
    router.chatgpt = _Boom()
    router.openrouter = _Ok("openrouter", "openrouter_api_key")
    with pytest.raises(ModelAuthError):
        router.complete_route("chatgpt_oauth", _request())
    assert router.openrouter.calls == 0


def test_unknown_complete_route():
    router = ModelRouter()
    with pytest.raises(ModelConfigError):
        router.complete_route("nope", _request())
