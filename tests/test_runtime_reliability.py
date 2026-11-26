"""Regression tests for runtime reliability, streaming timeouts, OAuth races,
poller lease heartbeats, atomic state stores, and Messenger queue concurrency.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import responses

from ecom_ops.bot import __main__ as bot_main
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.messenger_adapter import InboundEvent
from ecom_ops.bot.messenger_queue import MessengerJobQueue
from ecom_ops.bot.openclaw_commands import CommandContext
from ecom_ops.bot.reply import BotReply
from ecom_ops.bot.store import ConversationStore, TelegramConversationStore
from ecom_ops.bot.telegram_transport import TelegramApiError
from ecom_ops.model_router import circuit_breaker
from ecom_ops.model_router.base import ModelRequest
from ecom_ops.model_router.errors import ModelUnavailableError
from ecom_ops.model_router.openai_chatgpt import RESPONSES_URL, OpenAIChatGPTOAuthRoute
from ecom_ops.model_router.openrouter import OPENROUTER_URL, OpenRouterRoute
from ecom_ops.model_router.store import ModelSession, ModelSessionStore
from ecom_ops.model_router.streaming import read_chat_completion_sse
from ecom_ops.model_router.xai_api_key import XAI_CHAT_URL, XaiApiKeyRoute
from ecom_ops.telemetry import Telemetry
from ecom_ops.wp_ops.store import WpOpsStore

# ---------------------------------------------------------------------------
# 1. SSE hard_timeout propagation (OpenRouter + xAI API + streaming.py)
# ---------------------------------------------------------------------------


def test_sse_hard_timeout_raises_even_during_keepalives():
    def _keepalive_lines():
        yield ": keepalive"
        yield ""
        time.sleep(0.06)
        yield ": keepalive"
        yield 'data: {"choices":[{"delta":{"content":"late"}}]}'
        yield "data: [DONE]"

    deadline = time.monotonic() + 0.03
    with pytest.raises(ModelUnavailableError) as exc_info:
        read_chat_completion_sse(
            _keepalive_lines(),
            hard_deadline=deadline,
        )
    assert exc_info.value.code == "hard_timeout"


def test_sse_on_text_delta_model_unavailable_not_swallowed_but_other_errors_are():
    lines = [
        'data: {"choices":[{"delta":{"content":"hello "}}]}',
        'data: {"choices":[{"delta":{"content":"world"}}]}',
        "data: [DONE]",
    ]

    def _raise_timeout(_chunk: str) -> None:
        raise ModelUnavailableError("hard timeout", code="hard_timeout")

    with pytest.raises(ModelUnavailableError) as exc_info:
        read_chat_completion_sse(lines, on_text_delta=_raise_timeout)
    assert exc_info.value.code == "hard_timeout"

    def _raise_transport(_chunk: str) -> None:
        raise RuntimeError("telegram edit failed")

    content, _usage = read_chat_completion_sse(lines, on_text_delta=_raise_transport)
    assert content == "hello world"


def _fake_slow_sse_response():
    class _FakeResp:
        status_code = 200
        headers: dict[str, str] = {}

        def iter_lines(self, decode_unicode: bool = True):
            del decode_unicode
            yield ": keepalive"
            yield 'data: {"choices":[{"delta":{"content":"part1"}}]}'
            time.sleep(0.08)
            yield ": keepalive-after-deadline"
            yield 'data: {"choices":[{"delta":{"content":"part2"}}]}'
            yield "data: [DONE]"

        def close(self) -> None:
            pass

    return _FakeResp()


def test_openrouter_stream_hard_timeout_propagates(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "ecom_ops.model_router.openrouter.requests.post",
        lambda url, **kwargs: _fake_slow_sse_response(),
    )
    route = OpenRouterRoute()
    deltas: list[str] = []
    req = ModelRequest(
        messages=[{"role": "user", "content": "hej"}],
        hard_timeout=0.04,
        on_text_delta=deltas.append,
    )
    with pytest.raises(ModelUnavailableError) as exc_info:
        route.complete(req, telemetry=Telemetry(path=tmp_path / "tel.jsonl"))
    assert exc_info.value.code == "hard_timeout"
    assert OPENROUTER_URL.startswith("https://")


def test_xai_api_stream_hard_timeout_propagates(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "ecom_ops.model_router.xai_api_key.requests.post",
        lambda url, **kwargs: _fake_slow_sse_response(),
    )
    route = XaiApiKeyRoute()
    deltas: list[str] = []
    req = ModelRequest(
        messages=[{"role": "user", "content": "hej"}],
        hard_timeout=0.04,
        on_text_delta=deltas.append,
    )
    with pytest.raises(ModelUnavailableError) as exc_info:
        route.complete(req, telemetry=Telemetry(path=tmp_path / "tel.jsonl"))
    assert exc_info.value.code == "hard_timeout"
    assert XAI_CHAT_URL.startswith("https://")


# ---------------------------------------------------------------------------
# 2. Poller lease renewal across TimeoutErrors + SIGTERM exit 0 + timestamps
# ---------------------------------------------------------------------------


def test_poller_heartbeat_renews_leases_during_timeouts(tmp_path):
    store = TelegramJobStore(path=tmp_path / "jobs.sqlite3")
    assert store.acquire_poller_lease(ttl_seconds=2)
    store.accept_update(
        {"update_id": 1, "message": {"message_id": 10, "chat": {"id": 42}, "text": "hej"}}
    )
    job = store.claim_next(lease_seconds=1)
    assert job is not None
    initial_lease = float(job["lease_until"])

    stopped = threading.Event()
    lease_lost = threading.Event()
    hb = threading.Thread(
        target=bot_main._lease_heartbeat_loop,
        args=(store, stopped, lease_lost),
        kwargs={"interval_seconds": 0.05, "lease_seconds": 60},
        daemon=True,
    )
    hb.start()
    try:
        time.sleep(0.25)
    finally:
        stopped.set()
        hb.join(timeout=2.0)

    assert not lease_lost.is_set()
    refreshed = store.get_job(job["job_id"])
    assert refreshed is not None
    assert refreshed["state"] == "running"
    assert float(refreshed["lease_until"]) > initial_lease + 10.0
    # Running job is NOT recovered as expired
    rec = store.recover_expired_jobs()
    assert rec == {"queued": 0, "reconciliation": 0}


def test_poller_sigterm_returns_zero_and_logs_timestamped_status(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:SECRET_TOKEN_VALUE")
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setattr(bot_main, "_LEASE_HEARTBEAT_INTERVAL_S", 0.05)

    captured_handlers: dict[int, Any] = {}
    monkeypatch.setattr(
        signal,
        "signal",
        lambda sig, fn: captured_handlers.__setitem__(sig, fn),
    )

    calls = {"n": 0}
    renew_calls = {"poller": 0, "jobs": 0}
    orig_renew_poller = TelegramJobStore.renew_poller_lease
    orig_renew_jobs = TelegramJobStore.renew_leases

    def _spy_renew_poller(self, *args, **kwargs):
        renew_calls["poller"] += 1
        return orig_renew_poller(self, *args, **kwargs)

    def _spy_renew_jobs(self, *args, **kwargs):
        renew_calls["jobs"] += 1
        return orig_renew_jobs(self, *args, **kwargs)

    monkeypatch.setattr(TelegramJobStore, "renew_poller_lease", _spy_renew_poller)
    monkeypatch.setattr(TelegramJobStore, "renew_leases", _spy_renew_jobs)

    def _fake_api(token: str, method: str, **params: object) -> dict:
        del token, params
        if method == "setMyCommands":
            return {"ok": True}
        if method == "getUpdates":
            calls["n"] += 1
            if calls["n"] == 1:
                raise TelegramApiError(
                    502,
                    "Bad Gateway bot999999:SECRET_TOKEN_VALUE raw body",
                    method="getUpdates",
                )
            if calls["n"] == 2:
                raise TimeoutError("timed out")
            # Simulate clean SIGTERM shutdown
            captured_handlers[signal.SIGTERM](signal.SIGTERM, None)
            return {"ok": True, "result": []}
        return {"ok": True}

    monkeypatch.setattr(bot_main, "_api", _fake_api)
    # Speed up error backoff waits in test
    orig_wait = threading.Event.wait

    def _fast_wait(self, timeout=None):
        return orig_wait(self, min(timeout, 0.05) if timeout is not None else None)

    monkeypatch.setattr(threading.Event, "wait", _fast_wait)

    rc = bot_main.main()
    err_out = capsys.readouterr().err

    assert rc == 0
    assert renew_calls["poller"] >= 1
    assert renew_calls["jobs"] >= 1
    assert "TelegramApiError status=502 method=getUpdates" in err_out
    assert "TimeoutError" in err_out
    assert "SECRET_TOKEN_VALUE" not in err_out
    assert "raw body" not in err_out
    # ISO-8601 UTC timestamp prefix present
    assert "T" in err_out and "Z " in err_out


def test_poller_lease_lost_returns_two(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:TEST")
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setattr(bot_main, "_LEASE_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(signal, "signal", lambda _sig, _fn: None)
    monkeypatch.setattr(TelegramJobStore, "renew_poller_lease", lambda self, **kw: False)
    monkeypatch.setattr(
        bot_main,
        "_api",
        lambda _tok, method, **kw: {"ok": True, "result": []} if method == "getUpdates" else {"ok": True},
    )

    rc = bot_main.main()
    assert rc == 2


# ---------------------------------------------------------------------------
# 3. ChatGPT OAuth Windows PermissionError retry + atomic (token, generation)
# ---------------------------------------------------------------------------

_SIWC_ENV = {
    "OPENAI_SIWC_CLIENT_ID": "azom-client",
    "OPENAI_SIWC_REDIRECT_URI": "https://azom.example/oauth/openai/callback",
    "OPENAI_SIWC_AUTHORIZATION_URL": "https://auth.example/authorize",
    "OPENAI_SIWC_TOKEN_URL": "https://auth.example/token",
    "OPENAI_SIWC_HOST_ID": "urn:uuid:11111111-2222-3333-4444-555555555555",
}


def _configure_siwc(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    for k, v in _SIWC_ENV.items():
        monkeypatch.setenv(k, v)


def test_oauth_store_save_and_load_retry_windows_permission_error(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = ModelSessionStore("openai", "default")
    monkeypatch.setattr("ecom_ops.model_router.store.time.sleep", lambda _s: None)

    real_replace = os.replace
    replace_attempts = {"n": 0}

    def _flaky_replace(src, dst):
        replace_attempts["n"] += 1
        if replace_attempts["n"] <= 2:
            raise PermissionError("locked by reader")
        return real_replace(src, dst)

    monkeypatch.setattr("ecom_ops.model_router.store.os.replace", _flaky_replace)
    store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            access_token="tok-1",
            refresh_token="ref-1",
            expires_at=time.time() + 3600,
            generation=1,
        )
    )
    assert replace_attempts["n"] == 3
    # No stale .tmp files left behind
    assert not list(store.session_path.parent.glob("*.tmp"))

    real_read_text = Path.read_text
    read_attempts = {"n": 0}

    def _flaky_read(self: Path, *args, **kwargs):
        if self == store.session_path:
            read_attempts["n"] += 1
            if read_attempts["n"] <= 2:
                raise PermissionError("writer replacing")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read)
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "tok-1"
    assert read_attempts["n"] == 3


def test_oauth_refresh_permission_error_does_not_mark_needs_reauth(monkeypatch, tmp_path):
    _configure_siwc(monkeypatch, tmp_path)
    route = OpenAIChatGPTOAuthRoute()
    route.store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="azom-client",
            access_token="old-tok",
            refresh_token="old-ref",
            expires_at=time.time() - 10,
            generation=1,
        )
    )
    monkeypatch.setattr(
        route,
        "_token_request",
        lambda _form: {
            "access_token": "new-tok",
            "refresh_token": "rotated-ref",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        route.store,
        "save",
        lambda _sess: (_ for _ in ()).throw(PermissionError("transient lock")),
    )
    with pytest.raises(ModelUnavailableError) as exc_info:
        route._refresh_session()
    assert exc_info.value.code == "persist_locked"
    # Session must NOT be marked needs_reauth on transient PermissionError
    saved = route.store.load()
    assert saved is not None
    assert saved.needs_reauth is False


@responses.activate
def test_oauth_atomic_token_generation_avoids_double_refresh_on_401(monkeypatch, tmp_path):
    _configure_siwc(monkeypatch, tmp_path)
    route = OpenAIChatGPTOAuthRoute()
    now = time.time()
    # Start at generation 1 with valid token
    route.store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="azom-client",
            access_token="tok-gen1",
            refresh_token="ref-gen1",
            expires_at=now + 3600,
            earliest_refresh_at=0.0,
            generation=1,
        )
    )

    token_refresh_calls = {"n": 0}

    def _forbid_refresh(_form):
        token_refresh_calls["n"] += 1
        raise AssertionError("Should have reused peer's generation 2 token without re-refreshing")

    monkeypatch.setattr(route, "_token_request", _forbid_refresh)

    # First POST with tok-gen1 returns 401 AND simulates peer already rotating to generation 2
    # with earliest_refresh_at in the future (which would have caused ModelAuthError if double-refreshed!).
    def _first_401(_req):
        route.store.save(
            ModelSession(
                provider="openai",
                account_id="default",
                client_id="azom-client",
                access_token="tok-gen2",
                refresh_token="ref-gen2",
                expires_at=time.time() + 3600,
                earliest_refresh_at=time.time() + 600,
                generation=2,
            )
        )
        return (401, {}, json.dumps({"error": {"code": "invalid_api_key"}}))

    responses.add_callback(responses.POST, RESPONSES_URL, callback=_first_401)
    responses.add(
        responses.POST,
        RESPONSES_URL,
        body=(
            'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}\n\n'
            "data: [DONE]\n\n"
        ),
        status=200,
    )

    res = route.complete(
        ModelRequest(messages=[{"role": "user", "content": "ping"}]),
        telemetry=Telemetry(path=tmp_path / "tel.jsonl"),
    )
    assert res.content == "OK"
    assert token_refresh_calls["n"] == 0
    assert len(responses.calls) == 2
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok-gen1"
    assert responses.calls[1].request.headers["Authorization"] == "Bearer tok-gen2"


@responses.activate
def test_exchange_authorization_code_acquires_store_lock(monkeypatch, tmp_path):
    _configure_siwc(monkeypatch, tmp_path)
    route = OpenAIChatGPTOAuthRoute()
    _url, state = route.start_authorization()
    responses.add(
        responses.POST,
        _SIWC_ENV["OPENAI_SIWC_TOKEN_URL"],
        json={
            "access_token": "acc-lock",
            "refresh_token": "ref-lock",
            "expires_in": 3600,
        },
        status=200,
    )
    lock_entered = {"n": 0}
    orig_lock = route.store.lock

    def _spy_lock(*args, **kwargs):
        lock_entered["n"] += 1
        return orig_lock(*args, **kwargs)

    monkeypatch.setattr(route.store, "lock", _spy_lock)
    sess = route.exchange_authorization_code(code="code-xyz", state=state)
    assert sess.access_token == "acc-lock"
    assert lock_entered["n"] == 1


# ---------------------------------------------------------------------------
# 4. Circuit breaker thread-safe cache & locked reset_breaker(None)
# ---------------------------------------------------------------------------


def test_circuit_breaker_thread_safety_and_locked_reset_all(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    circuit_breaker.reset_breaker_cache_for_tests()

    # Verify reset_breaker(None) uses file_lock
    lock_calls = {"n": 0}
    orig_file_lock = circuit_breaker.file_lock

    def _spy_file_lock(*args, **kwargs):
        lock_calls["n"] += 1
        return orig_file_lock(*args, **kwargs)

    monkeypatch.setattr(circuit_breaker, "file_lock", _spy_file_lock)

    circuit_breaker.record_failure(
        "openrouter",
        "m1",
        ModelUnavailableError("err", code="unknown_model"),
    )
    assert circuit_breaker.allow_attempt("openrouter", "m1")[0] is False
    before_reset = lock_calls["n"]
    circuit_breaker.reset_breaker(None)
    assert lock_calls["n"] > before_reset
    assert circuit_breaker.allow_attempt("openrouter", "m1")[0] is True

    # Mutating returned raw cache dict must not corrupt internal cache
    snap = circuit_breaker._read_raw(use_cache=True)
    snap["entries"]["injected"] = {"state": "open"}
    assert "injected" not in circuit_breaker._read_raw(use_cache=True)["entries"]

    # Concurrent hammer across threads must raise no errors
    errors: list[BaseException] = []

    def _worker(tid: int) -> None:
        try:
            for i in range(15):
                circuit_breaker.allow_attempt("openrouter", f"m{tid}")
                if i % 3 == 0:
                    circuit_breaker.record_failure(
                        "openrouter",
                        f"m{tid}",
                        ModelUnavailableError("boom", code="http_503"),
                    )
                else:
                    circuit_breaker.record_success("openrouter", f"m{tid}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    assert not errors


# ---------------------------------------------------------------------------
# 5. ConversationStore / TelegramConversationStore update_atomic & atomic tmp
# ---------------------------------------------------------------------------


def test_conversation_store_update_atomic_concurrent_threads(tmp_path):
    store = ConversationStore(path=tmp_path / "conv.json")
    errors: list[BaseException] = []

    def _worker(tid: int) -> None:
        try:
            for step in range(8):
                def _mutate(prev: dict[str, Any], t=tid, s=step) -> dict[str, Any]:
                    state = dict(prev)
                    session = dict(state.get("session") or {})
                    session[f"k_{t}_{s}"] = True
                    state["session"] = session
                    return state

                store.update_atomic("chat-1", _mutate)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors
    final = store.get("chat-1")
    assert final is not None
    assert len(final["session"]) == 48
    assert not list(tmp_path.glob("*.tmp"))

    # Returning None clears state atomically
    assert store.update_atomic("chat-1", lambda _prev: None) is None
    assert store.get("chat-1") is None


def test_telegram_conversation_store_update_atomic_concurrent_threads(tmp_path):
    db_path = tmp_path / "tg.sqlite3"
    store = TelegramConversationStore(db_path)
    errors: list[BaseException] = []

    def _worker(tid: int) -> None:
        try:
            for step in range(8):
                def _mutate(prev: dict[str, Any], t=tid, s=step) -> dict[str, Any]:
                    state = dict(prev)
                    session = dict(state.get("session") or {})
                    session[f"k_{t}_{s}"] = True
                    state["session"] = session
                    return state

                store.update_atomic("999", _mutate)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors
    final = store.get("999")
    assert final is not None
    assert len(final["session"]) == 48

    assert store.update_atomic("999", lambda _prev: None) is None
    assert store.get("999") is None


def test_openclaw_save_session_concurrent_no_lost_updates(tmp_path):
    store = ConversationStore(path=tmp_path / "oc.json")
    errors: list[BaseException] = []

    def _worker(tid: int) -> None:
        try:
            ctx = CommandContext(chat_id="chat-oc", text="/model", args="", store=store)
            for step in range(6):
                ctx.save_session(**{f"opt_{tid}_{step}": step})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors
    state = store.get("chat-oc")
    assert state is not None
    assert len(state["session"]) == 30


def test_telemetry_purge_and_wp_ops_store_retry_permission_error(monkeypatch, tmp_path):
    # Telemetry.purge_old_events
    tel_path = tmp_path / "telemetry.jsonl"
    tel = Telemetry(path=tel_path)
    old_ts = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    new_ts = datetime.now(UTC).isoformat()
    tel_path.write_text(
        json.dumps({"id": "1", "action": "a", "site": "s", "units": 1, "unit_type": "x", "cost_usd": 0, "created_at": old_ts})
        + "\n"
        + json.dumps({"id": "2", "action": "b", "site": "s", "units": 1, "unit_type": "x", "cost_usd": 0, "created_at": new_ts})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("ecom_ops.telemetry.time.sleep", lambda _s: None)
    real_replace = os.replace
    tel_calls = {"n": 0}

    def _flaky_tel_replace(src, dst):
        tel_calls["n"] += 1
        if tel_calls["n"] == 1:
            raise PermissionError("busy")
        return real_replace(src, dst)

    monkeypatch.setattr("ecom_ops.telemetry.os.replace", _flaky_tel_replace)
    removed = tel.purge_old_events(retention_days=90)
    assert removed == 1
    assert tel_calls["n"] == 2
    assert not list(tmp_path.glob("*.tmp"))

    # WpOpsStore.update
    wp_path = tmp_path / "wp_ops_state.json"
    wp_store = WpOpsStore(wp_path)
    monkeypatch.setattr("ecom_ops.wp_ops.store.time.sleep", lambda _s: None)
    wp_calls = {"n": 0}

    def _flaky_wp_replace(src, dst):
        wp_calls["n"] += 1
        if wp_calls["n"] == 1:
            raise PermissionError("busy")
        return real_replace(src, dst)

    monkeypatch.setattr("ecom_ops.wp_ops.store.os.replace", _flaky_wp_replace)
    updated = wp_store.update(lambda s: s["incidents"].append({"id": "inc-1"}))
    assert updated["incidents"] == [{"id": "inc-1"}]
    assert wp_calls["n"] == 2
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 6. MessengerJobQueue per-peer exclusion & non-destructive ensure_started
# ---------------------------------------------------------------------------


def test_messenger_queue_serializes_same_peer_across_workers(monkeypatch, tmp_path):
    q = MessengerJobQueue(tmp_path / "messenger.sqlite3", max_workers=2)
    monkeypatch.setattr(
        "ecom_ops.bot.messenger_queue.send_sender_action",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "ecom_ops.bot.messenger_queue.send_bot_reply",
        lambda *args, **kwargs: [],
    )

    guard = threading.Lock()
    active_by_peer: dict[str, int] = {}
    max_concurrent_by_peer: dict[str, int] = {}
    max_total_concurrent = {"n": 0}
    total_active = {"n": 0}
    completed: list[str] = []
    all_done = threading.Event()

    def _fake_process(ev: InboundEvent, _handler: Any) -> BotReply:
        with guard:
            active_by_peer[ev.peer_id] = active_by_peer.get(ev.peer_id, 0) + 1
            max_concurrent_by_peer[ev.peer_id] = max(
                max_concurrent_by_peer.get(ev.peer_id, 0),
                active_by_peer[ev.peer_id],
            )
            total_active["n"] += 1
            max_total_concurrent["n"] = max(max_total_concurrent["n"], total_active["n"])
        try:
            time.sleep(0.12)
        finally:
            with guard:
                active_by_peer[ev.peer_id] -= 1
                total_active["n"] -= 1
                completed.append(str(ev.mid))
                if len(completed) == 3:
                    all_done.set()
        return BotReply(text="ok")

    monkeypatch.setattr("ecom_ops.bot.messenger_queue.process_inbound", _fake_process)

    q.enqueue(InboundEvent(peer_id="peer-A", text="one", mid="m-a1"))
    q.enqueue(InboundEvent(peer_id="peer-A", text="two", mid="m-a2"))
    q.enqueue(InboundEvent(peer_id="peer-B", text="three", mid="m-b1"))

    q.ensure_started(handler_factory=lambda: object(), page_token=None, dry_run=True)
    try:
        assert all_done.wait(timeout=5.0)
    finally:
        q.stop()

    assert max_concurrent_by_peer["peer-A"] == 1
    assert max_concurrent_by_peer["peer-B"] == 1
    # Different peers CAN run concurrently across the 2 workers
    assert max_total_concurrent["n"] == 2
    assert q.stats()["done"] == 3


def test_messenger_queue_ensure_started_does_not_reconcile_live_running_job(monkeypatch, tmp_path):
    q = MessengerJobQueue(tmp_path / "messenger_live.sqlite3", max_workers=2)
    monkeypatch.setattr(
        "ecom_ops.bot.messenger_queue.send_sender_action",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "ecom_ops.bot.messenger_queue.send_bot_reply",
        lambda *args, **kwargs: [],
    )

    started_a = threading.Event()
    release_a = threading.Event()
    executions: list[str] = []
    exec_lock = threading.Lock()

    def _fake_process(ev: InboundEvent, _handler: Any) -> BotReply:
        with exec_lock:
            executions.append(str(ev.mid))
        if ev.mid == "m-long":
            started_a.set()
            release_a.wait(timeout=5.0)
        return BotReply(text="ok")

    monkeypatch.setattr("ecom_ops.bot.messenger_queue.process_inbound", _fake_process)

    q.enqueue(InboundEvent(peer_id="peer-1", text="slow", mid="m-long"))
    q.ensure_started(handler_factory=lambda: object(), page_token=None, dry_run=True)
    try:
        assert started_a.wait(timeout=3.0)
        # Simulate one sibling thread having exited while one worker thread is still alive
        with q._worker_lock:
            q._threads = q._threads[:1]
        # Enqueue a second event and call ensure_started — MUST NOT reset running m-long!
        q.enqueue(InboundEvent(peer_id="peer-2", text="fast", mid="m-fast"))
        q.ensure_started(handler_factory=lambda: object(), page_token=None, dry_run=True)

        # Wait briefly and verify m-long was NOT reset to pending or re-executed
        time.sleep(0.2)
        with exec_lock:
            assert executions.count("m-long") == 1
        release_a.set()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and q.stats()["done"] < 2:
            time.sleep(0.05)
    finally:
        release_a.set()
        q.stop()

    assert executions.count("m-long") == 1
    assert executions.count("m-fast") == 1
    assert q.stats()["done"] == 2

    # When 0 threads are alive (fresh queue after crash), ensure_started DOES reconcile
    q2 = MessengerJobQueue(tmp_path / "messenger_crash.sqlite3", max_workers=1)
    q2.enqueue(InboundEvent(peer_id="peer-x", text="crashed", mid="m-crash"))
    claimed = q2._claim_next()
    assert claimed is not None
    assert q2.stats()["running"] == 1
    reconciled_done = threading.Event()

    def _fake_process_crash(ev: InboundEvent, _handler: Any) -> BotReply:
        if ev.mid == "m-crash":
            reconciled_done.set()
        return BotReply(text="ok")

    monkeypatch.setattr("ecom_ops.bot.messenger_queue.process_inbound", _fake_process_crash)
    q2.ensure_started(handler_factory=lambda: object(), page_token=None, dry_run=True)
    try:
        assert reconciled_done.wait(timeout=3.0)
    finally:
        q2.stop()
    assert q2.stats()["done"] == 1


# ---------------------------------------------------------------------------
# 7. ReviewPlans.open() cross-process file lock & unified BLOCKED_WP_PATHS
# ---------------------------------------------------------------------------


def test_review_plans_open_acquires_store_file_lock_and_preserves_concurrent_plans(
    monkeypatch, tmp_path
):
    import contextlib

    from ecom_ops.wp_ops.files import LocalTree
    from ecom_ops.wp_ops.review_plan import ReviewPlans

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")

    files_root = tmp_path / "wp_files"
    theme_dir = files_root / "wp-content" / "themes" / "azom-child"
    theme_dir.mkdir(parents=True)
    (theme_dir / "style.css").write_text(
        "/*\nTheme Name: Azom Child\nTemplate: storefront\n*/\nbody { color: #111; }\n",
        encoding="utf-8",
    )
    registry_path = Path(__file__).resolve().parents[1] / "config" / "wp_sites.mock.yaml"

    plans = ReviewPlans(
        data_dir=tmp_path / "data",
        registry_path=registry_path,
        transport_factory=lambda site: LocalTree(site.files_root, files_root),
    )
    lock_events: list[str] = []
    real_lock = plans._store_file_lock

    @contextlib.contextmanager
    def _tracking_lock():
        lock_events.append("enter")
        with real_lock():
            yield
        lock_events.append("exit")

    monkeypatch.setattr(plans, "_store_file_lock", _tracking_lock)

    first = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="agent",
        summary="lock check",
        variant_group="vg-first",
    )
    assert first.ok is True
    assert lock_events == ["enter", "exit"]

    # Concurrent open() calls with distinct variant groups must persist every plan row
    results: list[Any] = []
    res_guard = threading.Lock()
    barrier = threading.Barrier(6)

    def _worker(idx: int) -> None:
        barrier.wait()
        out = plans.open(
            "mock-staging",
            "theme",
            "azom-child",
            actor="agent",
            summary=f"concurrent {idx}",
            variant_group=f"vg-conc-{idx}",
        )
        with res_guard:
            results.append(out)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert len(results) == 6
    assert all(r.ok for r in results)
    expected_ids = {first.data["plan_id"], *(r.data["plan_id"] for r in results)}
    stored = json.loads((tmp_path / "data" / "wp_review_plans.json").read_text(encoding="utf-8"))
    stored_ids = {row["plan_id"] for row in stored["plans"]}
    assert expected_ids <= stored_ids
    assert lock_events.count("enter") == 7
    assert lock_events.count("exit") == 7


def test_blocked_wp_paths_unified_single_source():
    import re

    from ecom_ops.integrations.wp_live_preview import (
        _REFUSED_PATH_TOKENS,
        _SITE_BLOCKED_SUBSTRINGS,
        BLOCKED_WP_PATHS,
        build_interactive_sandbox_html,
    )

    assert _SITE_BLOCKED_SUBSTRINGS is BLOCKED_WP_PATHS
    assert _REFUSED_PATH_TOKENS == tuple(
        "wp-json" if tok == "/wp-json" else tok for tok in BLOCKED_WP_PATHS
    )
    html = build_interactive_sandbox_html(
        "<html><body><p>Hello</p></body></html>",
        site_canonical_url="https://azom.no",
        site_id="azom-no",
        plan_id="wr-0123456789ab",
        path="/",
    )
    m = re.search(r"var BLOCKED_PATHS = (\[[^\n;]+\]);", html)
    assert m is not None
    assert json.loads(m.group(1)) == list(BLOCKED_WP_PATHS)
