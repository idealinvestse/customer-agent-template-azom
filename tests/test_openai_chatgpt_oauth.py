"""ChatGPT OAuth: PKCE, refresh lock, and Responses SSE."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from ecom_ops.model_router.base import ModelRequest
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelRateLimitError,
    ModelUnavailableError,
)
from ecom_ops.model_router.openai_chatgpt import (
    RESPONSES_URL,
    OpenAIChatGPTOAuthRoute,
    _completed_output_text,
    _prefer_output_text,
    build_responses_payload,
)
from ecom_ops.model_router.store import ModelSession
from ecom_ops.telemetry import Telemetry

_ENV = {
    "OPENAI_SIWC_CLIENT_ID": "azom-client",
    "OPENAI_SIWC_REDIRECT_URI": "https://azom.example/oauth/openai/callback",
    "OPENAI_SIWC_AUTHORIZATION_URL": "https://auth.example/authorize",
    "OPENAI_SIWC_TOKEN_URL": "https://auth.example/token",
    "OPENAI_SIWC_HOST_ID": "host-1",
}


def _configure(monkeypatch) -> None:
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)


def _sse(text: str = "Hej") -> str:
    completed = {
        "type": "response.completed",
        "response": {
            "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}
        },
    }
    return (
        f'data: {{"type":"response.output_text.delta","delta":"{text}"}}\n\n'
        f"data: {json.dumps(completed)}\n\n"
        "data: [DONE]\n\n"
    )


def _connected(route: OpenAIChatGPTOAuthRoute, *, fresh: bool = True, generation: int = 1) -> None:
    expires = time.time() + 3600 if fresh else time.time() - 10
    route.store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="azom-client",
            access_token="access-secret",
            refresh_token="refresh-secret",
            expires_at=expires,
            earliest_refresh_at=0,
            generation=generation,
            status="connected",
        )
    )


def test_is_configured_requires_every_env(monkeypatch):
    _configure(monkeypatch)
    assert OpenAIChatGPTOAuthRoute().is_configured() is True
    monkeypatch.delenv("OPENAI_SIWC_HOST_ID")
    assert OpenAIChatGPTOAuthRoute().is_configured() is False
    with pytest.raises(ModelConfigError):
        OpenAIChatGPTOAuthRoute().start_authorization()


def test_start_authorization_pkce_and_state(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    url, state = route.start_authorization()
    query = parse_qs(urlparse(url).query)
    assert query["state"][0] == state
    assert query["code_challenge_method"][0] == "S256"
    assert "offline_access" in query["scope"][0]
    assert query["ext_agent_host_id"][0] == "host-1"
    assert query["resource"][0] == "https://api.openai.com/v1"
    assert "chatgpt.tokens.use.direct" in query["scope"][0]
    assert "agent_name_hint" not in query
    assert query["nonce"][0]
    pending = route._state_store().load()
    assert pending is not None
    assert pending.ext_agent_host_id == "host-1"
    digest = hashlib.sha256(pending.code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert query["code_challenge"][0] == expected
    public = pending.public_view()
    assert pending.code_verifier not in str(public)
    assert state not in str(public)


def test_exchange_rejects_bad_or_expired_state(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _url, state = route.start_authorization()
    with pytest.raises(ModelAuthError):
        route.exchange_authorization_code(code="abc", state="wrong")
    assert route._state_store().load() is not None

    pending = route._state_store().load()
    pending.expires_at = time.time() - 5
    route._state_store().save(pending)
    with pytest.raises(ModelAuthError):
        route.exchange_authorization_code(code="abc", state=state)
    assert route._state_store().load() is None


@responses.activate
def test_exchange_saves_session(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _url, state = route.start_authorization()
    responses.add(
        responses.POST,
        _ENV["OPENAI_SIWC_TOKEN_URL"],
        json={
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "id_token": "id-1",
            "expires_in": 3600,
            "earliest_refresh_at": 30,
            "scope": "openid offline_access",
        },
        status=200,
    )
    session = route.exchange_authorization_code(code="code-1", state=state)
    assert session.generation == 1
    assert session.client_id == "azom-client"
    assert session.refresh_token == "refresh-1"
    assert "offline_access" in session.scopes
    assert route._state_store().load() is None
    loaded = route.store.load()
    assert loaded is not None
    assert loaded.access_token == "access-1"
    assert loaded.ext_agent_host_id == "host-1"
    assert "access-1" not in str(loaded.public_view())


@responses.activate
def test_dynamic_registration_saves_issued_client_id(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("OPENAI_SIWC_CLIENT_ID", "dynamic_agent_client")
    route = OpenAIChatGPTOAuthRoute()
    url, state = route.start_authorization()
    query = parse_qs(urlparse(url).query)
    assert query["client_id"][0] == "dynamic_agent_client"
    assert query["agent_name_hint"][0] == "Azom"
    responses.add(
        responses.POST,
        _ENV["OPENAI_SIWC_TOKEN_URL"],
        json={
            "access_token": "access-issued",
            "refresh_token": "refresh-issued",
            "id_token": "id-issued",
            "expires_in": 3600,
            "scope": "openid offline_access chatgpt.tokens.use.direct",
        },
        status=200,
    )
    session = route.exchange_authorization_code(
        code="code-issued",
        state=state,
        issued_client_id="oaiapp_issued",
    )
    assert session.client_id == "oaiapp_issued"
    assert "chatgpt.tokens.use.direct" in session.scopes
    assert session.ext_agent_host_id == "host-1"
    sent = responses.calls[0].request.body
    if isinstance(sent, bytes):
        sent = sent.decode("utf-8")
    assert "client_id=oaiapp_issued" in sent
    assert "dynamic_agent_client" not in sent
    assert "resource=" in sent


def test_dynamic_registration_without_issued_client_is_rejected(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("OPENAI_SIWC_CLIENT_ID", "dynamic_agent_client")
    route = OpenAIChatGPTOAuthRoute()
    _url, state = route.start_authorization()
    with pytest.raises(ModelAuthError) as caught:
        route.exchange_authorization_code(code="code-1", state=state)
    assert caught.value.code == "incomplete_registration"
    assert route.store.load() is None


def test_callback_client_mismatch_is_rejected(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _url, state = route.start_authorization()
    with pytest.raises(ModelAuthError) as caught:
        route.exchange_authorization_code(
            code="code-1",
            state=state,
            issued_client_id="oaiapp_other",
        )
    assert caught.value.code == "client_mismatch"
    assert route._state_store().load() is not None


def test_refresh_respects_window_and_requires_token(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    route.store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            access_token="old",
            refresh_token="",
            expires_at=time.time() - 10,
            generation=1,
            status="connected",
        )
    )
    with pytest.raises(ModelAuthError):
        route._refresh_session()

    route.store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="azom-client",
            access_token="old",
            refresh_token="refresh-secret",
            expires_at=time.time() - 10,
            earliest_refresh_at=time.time() + 600,
            generation=1,
            status="connected",
        )
    )
    with pytest.raises(ModelAuthError):
        route._refresh_session()
    assert route.store.load().refresh_token == "refresh-secret"


@responses.activate
def test_refresh_bumps_generation_atomically(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=False, generation=4)
    responses.add(
        responses.POST,
        _ENV["OPENAI_SIWC_TOKEN_URL"],
        json={"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3600},
        status=200,
    )
    refreshed = route._refresh_session()
    assert refreshed.generation == 5
    assert refreshed.access_token == "access-new"
    assert route.store.load().generation == 5
    assert list(route.store.session_path.parent.glob("*.tmp")) == []


def test_refresh_under_lock_skips_second_rotation(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=False, generation=1)
    calls = {"n": 0}

    def token_request(form):
        calls["n"] += 1
        time.sleep(0.25)
        return {
            "access_token": "access-once",
            "refresh_token": "refresh-once",
            "expires_in": 3600,
        }

    monkeypatch.setattr(route, "_token_request", token_request)
    results: list[ModelSession] = []

    def run() -> None:
        results.append(OpenAIChatGPTOAuthRoute()._refresh_session())

    # Bind both threads to the same store object via one route instance.
    def run_same() -> None:
        results.append(route._refresh_session())

    threads = [threading.Thread(target=run_same) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert calls["n"] == 1
    assert len(results) == 2
    assert {item.access_token for item in results} == {"access-once"}
    assert route.store.load().generation == 2


@responses.activate
def test_invalid_grant_sets_needs_reauth(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=False, generation=2)
    responses.add(
        responses.POST,
        _ENV["OPENAI_SIWC_TOKEN_URL"],
        json={"error": "invalid_grant"},
        status=400,
    )
    with pytest.raises(ModelAuthError) as exc:
        route._refresh_session()
    assert exc.value.code == "invalid_grant"
    loaded = route.store.load()
    assert loaded.needs_reauth is True
    assert loaded.status == "needs_reauth"
    with pytest.raises(ModelAuthError):
        route._refresh_session()


def test_invalid_grant_uses_newer_generation(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=False, generation=2)

    def raced(_form):
        newer = route.store.load()
        newer.generation = 3
        newer.access_token = "from-peer"
        newer.refresh_token = "peer-refresh"
        newer.expires_at = time.time() + 3600
        newer.needs_reauth = False
        newer.status = "connected"
        route.store.save(newer)
        raise ModelAuthError("rejected", code="invalid_grant")

    monkeypatch.setattr(route, "_token_request", raced)
    got = route._refresh_session()
    assert got.access_token == "from-peer"
    assert got.generation == 3
    assert route.store.load().needs_reauth is False


def test_persist_failure_requires_reconnect(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=False, generation=1)
    real_save = route.store.save

    def flaky(session: ModelSession) -> None:
        if session.generation == 2 and session.status == "connected":
            raise OSError("disk")
        real_save(session)

    monkeypatch.setattr(route, "_token_request", lambda _form: {
        "access_token": "new",
        "refresh_token": "rotated",
        "expires_in": 3600,
    })
    monkeypatch.setattr(route.store, "save", flaky)
    with pytest.raises(ModelAuthError) as exc:
        route._refresh_session()
    assert exc.value.code == "persist_failed"
    assert route.store.load().needs_reauth is True


def test_sse_prefers_full_text_without_duplicating_deltas():
    assert _prefer_output_text("Hej", "", "") == "Hej"
    assert _prefer_output_text("He", "Hej", "") == "Hej"
    assert _prefer_output_text("Hej", "Annat", "Ocksannat") == "Hej"
    completed = _completed_output_text(
        {"output": [{"content": [{"type": "output_text", "text": "Pong"}]}]}
    )
    assert completed == "Pong"
    assert _prefer_output_text("", "", completed) == "Pong"
    direct = _completed_output_text({"output_text": "Pong"})
    assert direct == "Pong"


@responses.activate
def test_complete_uses_done_text_when_deltas_are_missing(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=True)
    completed = {
        "type": "response.completed",
        "response": {"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
    }
    body = (
        'data: {"type":"response.output_text.done","text":"Pong"}\n\n'
        f"data: {json.dumps(completed)}\n\n"
    )
    responses.add(responses.POST, RESPONSES_URL, body=body, status=200)
    result = route.complete(ModelRequest(messages=[{"role": "user", "content": "ping"}]))
    assert result.content == "Pong"
    assert result.total_tokens == 2


@responses.activate
def test_complete_uses_completed_output_when_no_deltas(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=True)
    event = {
        "type": "response.completed",
        "response": {
            "output_text": "Pong",
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        },
    }
    responses.add(
        responses.POST,
        RESPONSES_URL,
        body=f"data: {json.dumps(event)}\n\n",
        status=200,
    )
    result = route.complete(ModelRequest(messages=[{"role": "user", "content": "ping"}]))
    assert result.content == "Pong"
    assert result.prompt_tokens == 2


def test_responses_payload_shape():
    payload = build_responses_payload(
        ModelRequest(
            messages=[
                {"role": "system", "content": "Var Azom."},
                {"role": "developer", "content": "Kort."},
                {"role": "user", "content": "Hej"},
            ],
            max_output_tokens=32,
            temperature=0.9,
        ),
        "gpt-5.2",
    )
    assert payload["stream"] is True
    assert payload["store"] is False
    assert payload["max_output_tokens"] == 32
    assert "temperature" not in payload
    assert "max_tokens" not in payload
    assert payload["instructions"] == "Var Azom."
    assert payload["input"][0]["role"] == "developer"
    assert payload["input"][1] == {"role": "user", "content": "Hej"}
    assert all(item["role"] != "system" for item in payload["input"])


@responses.activate
def test_complete_parses_sse_and_hides_tokens(monkeypatch, tmp_path):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=True)
    responses.add(responses.POST, RESPONSES_URL, body=_sse("Hej"), status=200)
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    result = route.complete(
        ModelRequest(
            messages=[
                {"role": "system", "content": "Var Azom."},
                {"role": "user", "content": "Hej"},
            ],
            max_output_tokens=40,
            temperature=0.8,
        ),
        telemetry=tel,
    )
    assert result.content == "Hej"
    assert result.usage_known is True
    assert result.prompt_tokens == 3
    assert result.total_tokens == 4
    assert result.cost_usd == 0.0
    sent = json.loads(responses.calls[0].request.body)
    assert sent["stream"] is True
    assert sent["store"] is False
    assert "temperature" not in sent
    assert sent["instructions"] == "Var Azom."
    text = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "access-secret" not in text
    assert "refresh-secret" not in text


@responses.activate
def test_401_refreshes_once(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=True, generation=1)
    responses.add(responses.POST, RESPONSES_URL, body="no", status=401)
    responses.add(
        responses.POST,
        _ENV["OPENAI_SIWC_TOKEN_URL"],
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        status=200,
    )
    responses.add(responses.POST, RESPONSES_URL, body=_sse("Pong"), status=200)
    result = route.complete(ModelRequest(messages=[{"role": "user", "content": "ping"}]))
    assert result.content == "Pong"
    token_calls = [call for call in responses.calls if call.request.url.endswith("/token")]
    response_calls = [call for call in responses.calls if call.request.url.endswith("/responses")]
    assert len(token_calls) == 1
    assert len(response_calls) == 2
    assert route.store.load().generation == 2


@responses.activate
def test_second_401_does_not_refresh_again(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=True, generation=1)
    responses.add(responses.POST, RESPONSES_URL, body="no", status=401)
    responses.add(
        responses.POST,
        _ENV["OPENAI_SIWC_TOKEN_URL"],
        json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
        status=200,
    )
    responses.add(responses.POST, RESPONSES_URL, body="still-no", status=401)
    with pytest.raises(ModelAuthError):
        route.complete(ModelRequest(messages=[{"role": "user", "content": "ping"}]))
    token_calls = [call for call in responses.calls if call.request.url.endswith("/token")]
    assert len(token_calls) == 1


@responses.activate
def test_429_does_not_refresh(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=True)
    responses.add(responses.POST, RESPONSES_URL, body="later", status=429)
    with pytest.raises(ModelRateLimitError):
        route.complete(ModelRequest(messages=[{"role": "user", "content": "ping"}]))
    assert len(responses.calls) == 1


@responses.activate
def test_403_does_not_refresh(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=True)
    responses.add(responses.POST, RESPONSES_URL, body="nope", status=403)
    with pytest.raises(ModelAuthError) as exc:
        route.complete(ModelRequest(messages=[{"role": "user", "content": "ping"}]))
    assert exc.value.code == "forbidden"
    assert len(responses.calls) == 1


@responses.activate
def test_upstream_5xx_is_unavailable(monkeypatch):
    _configure(monkeypatch)
    route = OpenAIChatGPTOAuthRoute()
    _connected(route, fresh=True)
    responses.add(responses.POST, RESPONSES_URL, body="boom", status=503)
    with pytest.raises(ModelUnavailableError) as exc:
        route.complete(ModelRequest(messages=[{"role": "user", "content": "ping"}]))
    assert "boom" not in str(exc.value)
