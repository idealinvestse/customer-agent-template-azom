"""Cross-process circuit breaker tests."""

from __future__ import annotations

import json
import time

import responses

from ecom_ops.model_router import circuit_breaker
from ecom_ops.model_router.base import ModelRequest
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelRateLimitError,
    ModelUnavailableError,
)
from ecom_ops.model_router.profiles import bump_config_epoch
from ecom_ops.model_router.router import ModelRouter, reset_model_router
from ecom_ops.model_router.xai_api_key import XAI_CHAT_URL
from ecom_ops.telemetry import Telemetry


def test_breaker_batch_stampede_protection_and_no_secret_material(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XAI_API_KEY", "xai-super-secret-value-999")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-super-secret-value-888")
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "xai_api_key,openrouter")
    reset_model_router()

    xai_calls = {"n": 0}
    or_calls = {"n": 0}

    class _XaiDead:
        provider = "xai"
        route_id = "xai_api_key"
        def is_configured(self): return True
        def status(self): return {"model": "grok-4"}
        def complete(self, req, *, telemetry=None):
            xai_calls["n"] += 1
            raise ModelUnavailableError("timed out")

    class _OrOk:
        provider = "openrouter"
        route_id = "openrouter_api_key"
        def is_configured(self): return True
        def status(self): return {"model": "anthropic/claude-haiku-5.5"}
        def complete(self, req, *, telemetry=None):
            or_calls["n"] += 1
            from ecom_ops.model_router.base import ModelResult
            return ModelResult(content='{"category":"shipping","confidence":0.9}', provider="openrouter", route_id="openrouter_api_key", model=req.model or "m")

    router = ModelRouter()
    router.xai_api = _XaiDead()
    router.openrouter = _OrOk()
    tel = Telemetry(path=tmp_path / "data" / "tel.jsonl")

    for _ in range(50):
        res = router.complete_profile(
            "cases_classify",
            ModelRequest(messages=[{"role": "user", "content": "hej"}]),
            telemetry=tel,
            batch_mode=True,
        )
        assert "shipping" in res.content

    # Batch mode trips on first failure -> cases 2..50 skip xai_api_key in 0ms!
    assert xai_calls["n"] == 1
    assert or_calls["n"] == 50

    raw_health = circuit_breaker.health_path().read_text(encoding="utf-8")
    assert "xai-super-secret" not in raw_health
    assert "or-super-secret" not in raw_health


def test_open_auth_auto_clears_on_epoch_bump(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    circuit_breaker.reset_breaker_cache_for_tests()
    circuit_breaker.record_failure(
        "chatgpt_oauth",
        "gpt-6.2-sol",
        ModelAuthError("expired", code="needs_reauth"),
        auth_epoch=1,
    )
    allowed, state, _ = circuit_breaker.allow_attempt("chatgpt_oauth", "gpt-6.2-sol", auth_epoch=1)
    assert allowed is False
    assert state == "open_auth"

    # Reconnect bumps session generation / epoch -> immediately unblocked
    bump_config_epoch()
    allowed2, state2, _ = circuit_breaker.allow_attempt("chatgpt_oauth", "gpt-6.2-sol", auth_epoch=2)
    assert allowed2 is True
    assert state2 in {"closed", "half_open"}


def test_single_flight_half_open_and_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    circuit_breaker.reset_breaker_cache_for_tests()
    circuit_breaker.record_failure(
        "openrouter",
        "anthropic/claude-haiku-5.5",
        ModelRateLimitError("slow down", retry_after=15),
    )
    # Wind cooldown back to the past so half_open triggers
    data = json.loads(circuit_breaker.health_path().read_text(encoding="utf-8"))
    for ent in data["entries"].values():
        ent["cooldown_until"] = time.time() - 5
    circuit_breaker.health_path().write_text(json.dumps(data), encoding="utf-8")
    circuit_breaker.reset_breaker_cache_for_tests()

    ok1, st1, _ = circuit_breaker.allow_attempt("openrouter", "anthropic/claude-haiku-5.5")
    ok2, st2, _ = circuit_breaker.allow_attempt("openrouter", "anthropic/claude-haiku-5.5")
    assert ok1 is True and st1 == "half_open"
    assert ok2 is False and st2 == "half_open_busy"

    circuit_breaker.reset_breaker()
    assert circuit_breaker.status_snapshot() == {}


@responses.activate
def test_isolated_complete_route_bypasses_open_breaker_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XAI_API_KEY", "xai-key")
    reset_model_router()

    circuit_breaker.record_failure(
        "xai_api_key",
        "grok-4",
        ModelUnavailableError("down"),
        batch_mode=True,
    )
    assert circuit_breaker.allow_attempt("xai_api_key", "grok-4")[0] is False

    responses.add(
        responses.POST,
        XAI_CHAT_URL,
        json={"choices": [{"message": {"content": "PONG"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        status=200,
    )
    res = ModelRouter().complete_route(
        "xai_api_key",
        ModelRequest(messages=[{"role": "user", "content": "PONG"}]),
    )
    assert res.content == "PONG"
    assert circuit_breaker.allow_attempt("xai_api_key", "grok-4")[0] is True
