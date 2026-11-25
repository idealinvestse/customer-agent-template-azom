"""Tests for Telegram live transport, status board, heartbeat, and anti-interleaving."""

from __future__ import annotations

import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

from ecom_ops.bot import __main__ as bot_main
from ecom_ops.bot.chat_agent import ChatCancelled
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.progress_events import ProgressEvent, utf16_length
from ecom_ops.bot.reply import BotReply
from ecom_ops.bot.telegram_transport import (
    PerChatRateLimiter,
    TelegramApiError,
    telegram_api_post,
)


def _http_error(code: int, payload: dict, *, url: str = "https://api.telegram.org/bot123456:SECRET_TOKEN/editMessageText") -> urllib.error.HTTPError:
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        url=url,
        code=code,
        msg="Error",
        hdrs={},
        fp=io.BytesIO(body),
    )


@pytest.fixture(autouse=True)
def _reset_default_rate_limiter():
    bot_main._DEFAULT_RATE_LIMITER.reset()
    yield
    bot_main._DEFAULT_RATE_LIMITER.reset()


def test_http_400_message_not_modified_returns_ok_and_avoids_outbox_retry(monkeypatch, tmp_path: Path):
    def fake_urlopen(_req, timeout=60):
        del timeout
        raise _http_error(
            400,
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: message is not modified: specified new message content and reply markup are exactly the same",
            },
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    direct = telegram_api_post("123456:SECRET_TOKEN", "editMessageText", chat_id=1, message_id=7, text="Hej")
    assert direct["ok"] is True
    assert direct.get("not_modified") is True

    # Verify _LiveTelegramTurn._safe_api and _deliver_job_result treat it as synced (no outbox retry)
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    store.accept_update({"update_id": 1, "message": {"message_id": 10, "chat": {"id": 42}, "text": "hej"}})
    job = store.claim_next()
    assert job is not None
    store.save_message_ids(job["job_id"], status_message_id=50, answer_message_ids=[51])
    store.complete_job(job["job_id"], {"text": "Svar klart", "reply_markup": None}, state="completed")
    finished_job = store.get_job(job["job_id"])
    assert finished_job is not None

    def fake_api(_token, method, **params):
        del params
        if method == "editMessageText":
            raise _http_error(
                400,
                {"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"},
            )
        return {"ok": True, "result": {"message_id": 51}}

    monkeypatch.setattr(bot_main, "_api", fake_api)
    synced = bot_main._deliver_job_result("123456:SECRET_TOKEN", store, finished_job)
    assert synced is True
    assert store.undelivered_results() == []


def test_http_429_updates_per_chat_rate_limiter_private_and_group(monkeypatch):
    clock = [100.0]
    limiter = PerChatRateLimiter(clock=lambda: clock[0])

    assert limiter.min_interval_for(42) == PerChatRateLimiter.PRIVATE_MIN_INTERVAL
    assert limiter.min_interval_for(-1001234567890) == PerChatRateLimiter.GROUP_MIN_INTERVAL

    def fake_urlopen(_req, timeout=60):
        del timeout
        raise _http_error(
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 12",
                "parameters": {"retry_after": 12},
            },
            url="https://api.telegram.org/bot999999:TOPSECRET_TOKEN_VALUE/editMessageText",
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(TelegramApiError) as exc_info:
        telegram_api_post("999999:TOPSECRET_TOKEN_VALUE", "editMessageText", chat_id=42, message_id=1, text="x")
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 12.0
    assert "TOPSECRET_TOKEN_VALUE" not in str(exc_info.value)

    # Now verify _LiveTelegramTurn._safe_api records retry_after for both private and group chats
    def raising_api(_token, method, **params):
        del params
        raise TelegramApiError(429, "Too Many Requests", retry_after=9.0, method=method)

    monkeypatch.setattr(bot_main, "_api", raising_api)
    priv_turn = bot_main._LiveTelegramTurn("tok", 42, rate_limiter=limiter, heartbeat_interval=0)
    group_turn = bot_main._LiveTelegramTurn("tok", -1001234567890, rate_limiter=limiter, heartbeat_interval=0)
    try:
        assert priv_turn._safe_api("editMessageText", chat_id=42, message_id=1, text="a") == {}
        assert limiter.retry_delay(42, now=100.0) == pytest.approx(9.0)
        assert limiter.retry_delay(-1001234567890, now=100.0) == 0.0

        assert group_turn._safe_api("editMessageText", chat_id=-1001234567890, message_id=2, text="b") == {}
        assert limiter.retry_delay(-1001234567890, now=100.0) == pytest.approx(9.0)
        clock[0] = 110.0
        assert limiter.retry_delay(42, now=110.0) == 0.0
        assert limiter.retry_delay(-1001234567890, now=110.0) == 0.0
    finally:
        priv_turn.fail()
        group_turn.fail()


def test_reply_to_message_id_and_allow_sending_without_reply(monkeypatch):
    calls: list[tuple[str, dict]] = []
    next_id = [100]

    def fake_api(_token, method, **params):
        calls.append((method, dict(params)))
        if method == "sendMessage":
            next_id[0] += 1
            return {"ok": True, "result": {"message_id": next_id[0]}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot_main, "_api", fake_api)

    # _send_reply: only chunk 0 carries reply_to_message_id + allow_sending_without_reply
    long_text = ("A" * 3800) + "\n" + ("B" * 3800)
    bot_main._send_reply("tok", 7, BotReply(text=long_text), reply_to_message_id=555)
    send_calls = [p for m, p in calls if m == "sendMessage"]
    assert len(send_calls) == 2
    assert send_calls[0]["reply_to_message_id"] == 555
    assert send_calls[0]["allow_sending_without_reply"] is True
    assert "reply_to_message_id" not in send_calls[1]

    calls.clear()
    limiter = PerChatRateLimiter(private_min_interval=0.0)
    turn = bot_main._LiveTelegramTurn(
        "tok",
        7,
        reply_to_message_id=777,
        rate_limiter=limiter,
        heartbeat_interval=0,
    )
    turn.timer.cancel()
    turn._refresh_status(force=True)
    turn.finish(BotReply(text=long_text))

    sends = [p for m, p in calls if m == "sendMessage"]
    # 1 status draft + 2 answer chunks
    assert len(sends) == 3
    # Status draft
    assert sends[0]["reply_to_message_id"] == 777
    assert sends[0]["allow_sending_without_reply"] is True
    # First answer bubble
    assert sends[1]["reply_to_message_id"] == 777
    assert sends[1]["allow_sending_without_reply"] is True
    # Second answer bubble does not repeat reply_to_message_id
    assert "reply_to_message_id" not in sends[2]


def test_status_text_respects_utf16_limit_with_emojis():
    turn = bot_main._LiveTelegramTurn("tok", 1, job_id="abcdef123456", title="🚀" * 60, heartbeat_interval=0)
    turn.timer.cancel()
    turn._refresh_status = lambda force=False: None
    for idx in range(25):
        turn.on_progress(
            ProgressEvent(
                step_id=f"step-{idx}",
                tool="woo",
                label=f"Hämtar order {idx} " + ("📦🚀✨" * 20),
                phase="completed",
                step_index=idx + 1,
                step_total=25,
                outcome="Klar " + ("🎉🔥" * 40),
            )
        )
    # Also push huge raw lines directly to stress board clip
    for idx in range(15):
        turn.on_progress(("⚡" * 250) + f" steg {idx}")

    rendered = turn._status_text()
    assert utf16_length(rendered) <= 3800
    assert rendered.startswith("⚡ Azom Live #abcdef12")
    turn.fail()


def test_trailing_edge_flush_and_heartbeat_stop_on_finish(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_api(_token, method, **params):
        calls.append((method, dict(params)))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 200}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot_main, "_api", fake_api)
    monkeypatch.setattr(bot_main._LiveTelegramTurn, "_STATUS_DELAY", 0.03)
    monkeypatch.setattr(bot_main._LiveTelegramTurn, "_EDIT_INTERVAL", 0.12)

    limiter = PerChatRateLimiter(private_min_interval=0.05)
    turn = bot_main._LiveTelegramTurn(
        "tok",
        9,
        job_id="11223344aabb",
        rate_limiter=limiter,
        heartbeat_interval=0.08,
    )
    # Wait for initial status card to appear
    deadline = time.monotonic() + 2.0
    while turn.status_message_id is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert turn.status_message_id == 200

    # Rapidly push two updates within _EDIT_INTERVAL so trailing-edge flush schedules
    turn.on_progress("Steg 1 klart")
    turn.on_progress("Steg 2 slutligt tillstånd")

    # Wait for trailing-edge flush + at least one heartbeat tick (typing + edit)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        edits = [p["text"] for m, p in calls if m == "editMessageText"]
        typings = [p for m, p in calls if m == "sendChatAction"]
        if any("Steg 2 slutligt tillstånd" in txt for txt in edits) and len(typings) >= 1:
            break
        time.sleep(0.02)

    edits = [p["text"] for m, p in calls if m == "editMessageText"]
    typings = [p for m, p in calls if m == "sendChatAction"]
    assert any("Steg 2 slutligt tillstånd" in txt for txt in edits)
    assert len(typings) >= 1

    # Finish stops all timers cleanly
    turn.finish(BotReply(text="Färdigt svar"))
    assert turn._flush_timer is None
    assert turn._heartbeat_timer is None
    count_after_finish = len(calls)
    time.sleep(0.22)
    assert len(calls) == count_after_finish


def test_stream_clear_and_phase_change_without_duplicate_route_switch(monkeypatch):
    calls: list[tuple[str, dict]] = []
    msg_id = [300]

    def fake_api(_token, method, **params):
        calls.append((method, dict(params)))
        if method == "sendMessage":
            msg_id[0] += 1
            return {"ok": True, "result": {"message_id": msg_id[0]}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot_main, "_api", fake_api)
    limiter = PerChatRateLimiter(private_min_interval=0.0)
    turn = bot_main._LiveTelegramTurn("tok", 5, rate_limiter=limiter, heartbeat_interval=0)
    turn.timer.cancel()
    turn._refresh_status(force=True)

    # Stream partial answer, then clear it via on_stream_clear (must NOT mention "Byter modellrutt")
    turn.on_text_delta("Preliminär text som rensas")
    assert len(turn.answer_message_ids) == 1
    first_answer_id = turn.answer_message_ids[0]

    turn.on_stream_clear()
    assert turn.answer_message_ids == []
    assert turn.stream_text == ""
    assert any(m == "deleteMessage" and p.get("message_id") == first_answer_id for m, p in calls)
    assert "Byter modellrutt" not in turn._status_text()

    # Model phase: reasoning -> route_switch
    turn.on_phase_change({"phase": "reasoning", "route_id": "chatgpt_oauth", "model": "gpt-5.4"})
    rendered_reasoning = turn._status_text()
    assert "Analyserar och resonerar" in rendered_reasoning
    assert "chatgpt_oauth" in rendered_reasoning

    turn.on_phase_change(
        {
            "phase": "route_switch",
            "from_route": "chatgpt_oauth",
            "to_route": "xai_grok_build",
            "error_class": "rate_limit",
        }
    )
    rendered_switch = turn._status_text()
    assert "Byter modellrutt" in rendered_switch
    assert "chatgpt_oauth" in rendered_switch
    assert "xai_grok_build" in rendered_switch

    # Immediately following on_text_reset must NOT duplicate "Byter modellrutt"
    turn.on_text_reset()
    rendered_after_reset = turn._status_text()
    assert rendered_after_reset.count("Byter modellrutt") == 1
    turn.fail()


def test_process_job_error_and_cancel_status_icons(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, dict]] = []
    next_id = [400]

    def fake_api(_token, method, **params):
        calls.append((method, dict(params)))
        if method == "sendMessage":
            next_id[0] += 1
            return {"ok": True, "result": {"message_id": next_id[0]}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot_main, "_api", fake_api)

    # 1) Crash -> ⚠️ (NOT "✓ Svar klart")
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    store.accept_update({"update_id": 1, "message": {"message_id": 11, "chat": {"id": 5}, "text": "krascha"}})
    job_crash = store.claim_next()
    assert job_crash is not None
    # Pre-seed a status_message_id so finish() edits the status card deterministically
    store.save_message_ids(job_crash["job_id"], status_message_id=901)
    job_crash = store.get_job(job_crash["job_id"])
    assert job_crash is not None

    class CrashHandler:
        def handle(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    bot_main._process_job("tok", CrashHandler(), store, job_crash)
    status_edits = [
        p["text"]
        for m, p in calls
        if m == "editMessageText" and p.get("message_id") == 901
    ]
    assert status_edits
    assert "⚠️" in status_edits[-1]
    assert "RuntimeError" in status_edits[-1]
    assert "✓ Svar klart" not in status_edits[-1]

    # 2) ChatCancelled -> 🛑 (NOT "✓ Svar klart")
    calls.clear()
    store.accept_update({"update_id": 2, "message": {"message_id": 12, "chat": {"id": 5}, "text": "avbryt"}})
    job_cancel = store.claim_next()
    assert job_cancel is not None
    store.save_message_ids(job_cancel["job_id"], status_message_id=902)
    job_cancel = store.get_job(job_cancel["job_id"])
    assert job_cancel is not None

    class CancelHandler:
        def handle(self, *_args, **_kwargs):
            raise ChatCancelled()

    bot_main._process_job("tok", CancelHandler(), store, job_cancel)
    cancel_edits = [
        p["text"]
        for m, p in calls
        if m == "editMessageText" and p.get("message_id") == 902
    ]
    assert cancel_edits
    assert "🛑" in cancel_edits[-1]
    assert "✓ Svar klart" not in cancel_edits[-1]


def test_multitask_anti_interleaving_buffers_second_chunk_until_finish(monkeypatch):
    calls: list[tuple[str, dict]] = []
    next_id = [500]

    def fake_api(_token, method, **params):
        calls.append((method, dict(params)))
        if method == "sendMessage":
            next_id[0] += 1
            return {"ok": True, "result": {"message_id": next_id[0]}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot_main, "_api", fake_api)

    class ParallelStore:
        def active_jobs(self, _chat_id):
            return [{"job_id": "aaaa1111"}, {"job_id": "bbbb2222"}]

        def save_message_ids(self, *_args, **_kwargs):
            pass

    limiter = PerChatRateLimiter(private_min_interval=0.0)
    turn = bot_main._LiveTelegramTurn(
        "tok",
        77,
        job_store=ParallelStore(),
        job_id="aaaa1111",
        rate_limiter=limiter,
        heartbeat_interval=0,
    )
    turn.timer.cancel()

    part1 = "Första delen. " * 290  # > 3900 chars -> forces 2 chunks
    part2 = "Andra delen. " * 200
    full_text = part1 + "\n" + part2

    turn.on_text_delta(full_text)
    # While streaming with parallel siblings, only ONE answer bubble is sent!
    assert len(turn.answer_message_ids) == 1
    first_send = [p["text"] for m, p in calls if m == "sendMessage"][-1]
    assert "fortsättning levereras när svaret är klart" in first_send
    assert utf16_length(first_send) <= 3900

    # On finish(), all canonical chunks are delivered coherently
    synced = turn.finish(BotReply(text=full_text))
    assert synced is True
    assert len(turn.answer_message_ids) >= 2


def test_missing_message_edit_falls_back_to_send_message(monkeypatch):
    calls: list[tuple[str, dict]] = []
    next_id = [600]

    def fake_api(_token, method, **params):
        calls.append((method, dict(params)))
        if method == "editMessageText" and params.get("message_id") == 55:
            raise TelegramApiError(400, "Bad Request: message to edit not found", method=method)
        if method == "sendMessage":
            next_id[0] += 1
            return {"ok": True, "result": {"message_id": next_id[0]}}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(bot_main, "_api", fake_api)
    turn = bot_main._LiveTelegramTurn(
        "tok",
        10,
        answer_message_ids=[55],
        reply_to_message_id=88,
        heartbeat_interval=0,
    )
    turn.timer.cancel()
    synced = turn.finish(BotReply(text="Uppdaterat svar"))
    assert synced is True
    assert turn.answer_message_ids == [601]
    sent_params = [p for m, p in calls if m == "sendMessage"]
    assert len(sent_params) == 1
    assert sent_params[0]["reply_to_message_id"] == 88
    assert sent_params[0]["allow_sending_without_reply"] is True


def test_deliver_job_result_preserves_events_and_records_failed_attempt(monkeypatch, tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    store.accept_update({"update_id": 1, "message": {"message_id": 10, "chat": {"id": 9}, "text": "kolla order 1001"}})
    job = store.claim_next()
    assert job is not None
    store.add_event(job["job_id"], "progress", "Hämtade order 1001 från Woo")
    store.save_message_ids(job["job_id"], status_message_id=700)
    store.complete_job(job["job_id"], {"text": "Order 1001 är klar.", "reply_markup": None}, state="completed")
    finished_job = store.get_job(job["job_id"])
    assert finished_job is not None

    attempts: list[str] = []
    monkeypatch.setattr(store, "record_delivery_attempt", lambda jid: attempts.append(jid), raising=False)

    # First attempt fails on answer sendMessage -> record_delivery_attempt called, status still retains history
    edited_texts: list[str] = []

    def fail_answer_api(_token, method, **params):
        if method == "editMessageText":
            edited_texts.append(str(params.get("text") or ""))
            return {"ok": True, "result": {}}
        raise TelegramApiError(500, "Internal error", method=method)

    monkeypatch.setattr(bot_main, "_api", fail_answer_api)
    synced = bot_main._deliver_job_result("tok", store, finished_job)
    assert synced is False
    assert attempts == [job["job_id"]]
    assert edited_texts and "Hämtade order 1001 från Woo" in edited_texts[-1]


def test_preflight_proposal_and_control_reply_multitask(monkeypatch):
    class FakeHandler:
        def __init__(self) -> None:
            self.store = type("S", (), {"get": lambda _self, _chat: {}})()

        def _peer_allowed(self, _chat_id: str) -> bool:
            return True

        def _resolve_actor(self, _chat_id: str) -> str:
            return "oscar"

    class FakeProposalStore:
        def __init__(self, state: str = "open") -> None:
            self._state = state

        def get_proposal(self, proposal_id: str, chat_id: str | None = None):
            return {"proposal_id": proposal_id, "chat_id": chat_id or "5", "state": self._state}

    job = {
        "chat_id": "5",
        "payload": {"type": "callback", "chat_id": "5", "data": "proposal:yes:pr-abcdef012345"},
    }
    assert bot_main._preflight_action_job(FakeHandler(), job, store=FakeProposalStore("open")) is None
    assert bot_main._preflight_action_job(FakeHandler(), job, store=FakeProposalStore("consumed")) is not None

    # _control_reply for /status with 2 active jobs + /cancel ambiguous_running
    sent: list[str] = []
    monkeypatch.setattr(
        bot_main,
        "_api",
        lambda _tok, _m, **p: (sent.append(str(p.get("text") or "")), {"ok": True, "result": {"message_id": 1}})[1],
    )

    class MultiStore:
        def active_jobs(self, _chat_id):
            return [
                {"job_id": "aaaa11110000", "kind": "read_turn"},
                {"job_id": "bbbb22220000", "kind": "page_review"},
            ]

        def conversation_jobs(self, _chat_id):
            return [
                {"job_id": "aaaa11110000", "kind": "read_turn", "state": "running"},
                {"job_id": "bbbb22220000", "kind": "page_review", "state": "running"},
                {"job_id": "cccc33330000", "kind": "read_turn", "state": "queued"},
            ]

        def events(self, jid, limit=3):
            del limit
            return [{"summary": f"Steg för {jid[:4]}"}]

        def request_cancel(self, _chat_id, _arg):
            return "ambiguous_running"

        def mark_control_handled(self, _uid):
            pass

    bot_main._control_reply("tok", FakeHandler(), MultiStore(), {"chat_id": "5", "update_id": 1, "action": "status", "args": ""})
    assert "#aaaa1111" in sent[-1]
    assert "#bbbb2222" in sent[-1]
    assert "Köade jobb: 1" in sent[-1]

    bot_main._control_reply("tok", FakeHandler(), MultiStore(), {"chat_id": "5", "update_id": 2, "action": "cancel", "args": ""})
    assert "/cancel aaaa1111" in sent[-1]
    assert "/cancel bbbb2222" in sent[-1]
