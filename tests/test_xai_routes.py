"""xAI delegated CLI route and API-key route."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import responses

from ecom_ops.model_router.base import ModelRequest
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelRateLimitError,
    ModelUnavailableError,
)
from ecom_ops.model_router.xai_api_key import XAI_CHAT_URL, XaiApiKeyRoute
from ecom_ops.model_router.xai_grok_build import XaiGrokBuildRoute
from ecom_ops.telemetry import Telemetry


def _request() -> ModelRequest:
    return ModelRequest(messages=[{"role": "user", "content": "hej"}], timeout=5)


def test_grok_route_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("XAI_GROK_BIN", raising=False)
    monkeypatch.delenv("AZOM_XAI_GROK_BUILD", raising=False)
    route = XaiGrokBuildRoute()
    assert route.is_configured() is False
    with pytest.raises(ModelConfigError):
        route.complete(_request())


def test_grok_missing_binary(monkeypatch):
    monkeypatch.setenv("XAI_GROK_BIN", "azom-grok-missing")
    monkeypatch.setattr(
        "ecom_ops.model_router.xai_grok_build.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    route = XaiGrokBuildRoute()
    with pytest.raises(ModelConfigError):
        route.complete(_request())


def test_grok_timeout_and_nonzero_hide_stderr(monkeypatch):
    monkeypatch.setenv("XAI_GROK_BIN", "grok")
    route = XaiGrokBuildRoute()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build.subprocess.run", timeout)
    with pytest.raises(ModelUnavailableError):
        route.complete(_request())

    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 2, stdout="", stderr="refresh-token-secret"
        )

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build.subprocess.run", failed)
    with pytest.raises(ModelUnavailableError) as unavailable:
        route.complete(_request())
    assert unavailable.value.code == "exit_2"
    assert "refresh-token-secret" not in str(unavailable.value)

    def unauthenticated(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="please run grok login --device-auth before continuing",
        )

    monkeypatch.setattr(
        "ecom_ops.model_router.xai_grok_build.subprocess.run", unauthenticated
    )
    with pytest.raises(ModelAuthError) as auth_exc:
        route.complete(_request())
    assert auth_exc.value.code == "exit_1"
    assert "device-auth before continuing" not in str(auth_exc.value)


def test_grok_does_not_read_auth_json(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_GROK_BIN", "grok")
    opened: list[str] = []
    real_open = open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)

    def ok(command, **kwargs):
        joined = " ".join(command)
        assert "auth.json" not in joined
        assert "--print" not in command
        assert "--single" not in command
        assert "--always-approve" not in command
        assert "--permission-mode" not in command
        assert "--prompt-file" in command
        prompt_path = Path(command[command.index("--prompt-file") + 1])
        prompt_text = prompt_path.read_text(encoding="utf-8")
        assert prompt_text.startswith("USER:")
        assert "hej" in prompt_text
        assert "hej" not in command
        assert kwargs.get("input") in (None, "")
        assert kwargs.get("cwd")
        return subprocess.CompletedProcess(command, 0, stdout="pong\n", stderr="")

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build.subprocess.run", ok)
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    result = XaiGrokBuildRoute().complete(_request(), telemetry=tel)
    assert result.content == "pong"
    assert result.usage_known is False
    assert result.cost_usd == 0.0
    status = XaiGrokBuildRoute().status()
    assert status["refresh_owner"] == "grok_build"
    assert status["app_owned_refresh"] is False
    assert status["connected"] is False
    assert status["auth_checked"] is False
    assert not any("auth.json" in path for path in opened)


def test_api_key_required(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    route = XaiApiKeyRoute()
    assert route.is_configured() is False
    with pytest.raises(ModelConfigError):
        route.complete(_request())


@responses.activate
@pytest.mark.parametrize(
    ("status", "error"),
    [(401, ModelAuthError), (429, ModelRateLimitError), (503, ModelUnavailableError)],
)
def test_api_key_classifies_status(monkeypatch, status, error):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    responses.add(responses.POST, XAI_CHAT_URL, json={"error": "hidden"}, status=status)
    with pytest.raises(error) as exc:
        XaiApiKeyRoute().complete(_request())
    assert "hidden" not in str(exc.value)


@responses.activate
def test_api_key_extracts_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-should-not-leak")
    responses.add(
        responses.POST,
        XAI_CHAT_URL,
        json={
            "choices": [{"message": {"content": "pong"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        },
        status=200,
    )
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    result = XaiApiKeyRoute().complete(_request(), telemetry=tel)
    assert result.content == "pong"
    assert result.usage_known is True
    assert result.total_tokens == 5
    text = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "xai-secret-should-not-leak" not in text
    assert "usage_known" in text
