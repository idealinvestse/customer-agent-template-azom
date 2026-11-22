"""xAI delegated CLI route and API-key route."""

from __future__ import annotations

import json
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


class FakeGrok:
    """Process stand-in. hold=0 exits on the first poll. hold=None waits for release."""

    def __init__(
        self,
        *,
        code: int = 0,
        stdout: str = "pong\n",
        stderr: str = "",
        hold: int | None = 0,
    ):
        self.code = code
        self.stdout_data = stdout
        self.stderr_data = stderr
        self.hold = hold
        self.polls = 0
        self.killed = False
        self.released = False
        self.returncode: int | None = None
        self.pid = 4242

    def poll(self) -> int | None:
        self.polls += 1
        if self.killed:
            self.returncode = -9
            return self.returncode
        if self.released or (self.hold is not None and self.polls > self.hold):
            self.returncode = self.code
            return self.code
        return None

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        del timeout
        return self.returncode

    def release(self) -> None:
        self.released = True


def _request() -> ModelRequest:
    return ModelRequest(messages=[{"role": "user", "content": "hej"}], timeout=5)


def _clock(monkeypatch):
    state = {"t": 0.0}

    def now() -> float:
        return state["t"]

    def sleep(seconds: float) -> None:
        state["t"] += seconds

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build._now", now)
    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build._sleep", sleep)
    return state


def test_grok_route_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("XAI_GROK_BIN", raising=False)
    monkeypatch.delenv("AZOM_XAI_GROK_BUILD", raising=False)
    route = XaiGrokBuildRoute()
    assert route.is_configured() is False
    with pytest.raises(ModelConfigError):
        route.complete(_request())


def test_grok_missing_binary(monkeypatch):
    monkeypatch.setenv("XAI_GROK_BIN", "azom-grok-missing")

    def missing(command, cwd):
        del command, cwd
        raise FileNotFoundError("missing")

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build.spawn_grok", missing)
    route = XaiGrokBuildRoute()
    with pytest.raises(ModelConfigError):
        route.complete(_request())


def test_grok_timeout_and_nonzero_hide_stderr(monkeypatch):
    monkeypatch.setenv("XAI_GROK_BIN", "grok")
    route = XaiGrokBuildRoute()
    clock = _clock(monkeypatch)
    silent = FakeGrok(hold=None)
    monkeypatch.setattr(
        "ecom_ops.model_router.xai_grok_build.spawn_grok",
        lambda command, cwd: silent,
    )
    with pytest.raises(ModelUnavailableError) as timed_out:
        route.complete(_request())
    assert silent.killed is True
    assert timed_out.value.code == ""
    assert clock["t"] >= 5
    assert clock["t"] < 8

    failed = FakeGrok(code=2, stdout="", stderr="refresh-token-secret")
    monkeypatch.setattr(
        "ecom_ops.model_router.xai_grok_build.spawn_grok",
        lambda command, cwd: failed,
    )
    with pytest.raises(ModelUnavailableError) as unavailable:
        route.complete(_request())
    assert unavailable.value.code == "exit_2"
    assert "refresh-token-secret" not in str(unavailable.value)

    unauthenticated = FakeGrok(
        code=1,
        stdout="",
        stderr="please run grok login --device-auth before continuing",
    )
    monkeypatch.setattr(
        "ecom_ops.model_router.xai_grok_build.spawn_grok",
        lambda command, cwd: unauthenticated,
    )
    with pytest.raises(ModelAuthError) as auth_exc:
        route.complete(_request())
    assert auth_exc.value.code == "exit_1"
    assert "device-auth before continuing" not in str(auth_exc.value)


def test_grok_extends_while_reasoning_or_text(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_GROK_BIN", "grok")
    monkeypatch.setenv("AZOM_XAI_GROK_HARD_TIMEOUT", "30")
    clock = _clock(monkeypatch)
    events = tmp_path / "events.jsonl"
    events.write_text("", encoding="utf-8")
    proc = FakeGrok(hold=None)
    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build._event_files", lambda work: [events])

    def sleep(seconds: float) -> None:
        clock["t"] += seconds
        phase = "streaming_reasoning" if clock["t"] < 8 else "streaming_text"
        if clock["t"] < 12:
            with events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "phase_changed", "phase": phase}) + "\n")
        else:
            proc.release()

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build._sleep", sleep)
    monkeypatch.setattr(
        "ecom_ops.model_router.xai_grok_build.spawn_grok",
        lambda command, cwd: proc,
    )
    result = XaiGrokBuildRoute().complete(_request())
    assert result.content == "pong"
    assert proc.killed is False
    assert clock["t"] > 5


def test_grok_waiting_does_not_extend(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_GROK_BIN", "grok")
    clock = _clock(monkeypatch)
    events = tmp_path / "events.jsonl"
    proc = FakeGrok(hold=None)
    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build._event_files", lambda work: [events])

    def sleep(seconds: float) -> None:
        clock["t"] += seconds
        with events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "phase_changed", "phase": "waiting_for_model"}) + "\n")

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build._sleep", sleep)
    monkeypatch.setattr(
        "ecom_ops.model_router.xai_grok_build.spawn_grok",
        lambda command, cwd: proc,
    )
    with pytest.raises(ModelUnavailableError):
        XaiGrokBuildRoute().complete(_request())
    assert proc.killed is True
    assert clock["t"] >= 5
    assert clock["t"] < 8


def test_grok_hard_cap_stops_endless_reasoning(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_GROK_BIN", "grok")
    monkeypatch.setenv("AZOM_XAI_GROK_HARD_TIMEOUT", "8")
    clock = _clock(monkeypatch)
    events = tmp_path / "events.jsonl"
    proc = FakeGrok(hold=None)
    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build._event_files", lambda work: [events])

    def sleep(seconds: float) -> None:
        clock["t"] += 1
        with events.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"type": "phase_changed", "phase": "streaming_reasoning"}) + "\n"
            )

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build._sleep", sleep)
    monkeypatch.setattr(
        "ecom_ops.model_router.xai_grok_build.spawn_grok",
        lambda command, cwd: proc,
    )
    with pytest.raises(ModelUnavailableError) as timed_out:
        XaiGrokBuildRoute().complete(_request())
    assert proc.killed is True
    assert timed_out.value.code == ""
    assert clock["t"] >= 8
    assert clock["t"] < 12


def test_grok_does_not_read_auth_json(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_GROK_BIN", "grok")
    opened: list[str] = []
    real_open = open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)

    def ok(command, cwd):
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
        assert cwd
        return FakeGrok()

    monkeypatch.setattr("ecom_ops.model_router.xai_grok_build.spawn_grok", ok)
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
