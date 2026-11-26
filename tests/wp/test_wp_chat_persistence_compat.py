"""Tests for WP chat persistence, proposal coexistence, attachments, progress events, and page-review checkpoints."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    MOCK_REGISTRY,
    REPO_ROOT,
    passing_catalog,
    tool_call,
    write_theme_file,
)

from ecom_ops.bot import __main__ as bot_main
from ecom_ops.bot import job_store as job_store_mod
from ecom_ops.bot import telegram_transport
from ecom_ops.bot.chat_agent import (
    ChatCancelled,
    ChatResult,
)
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.progress_events import (
    ProgressEvent,
    mark_event_consumer,
)
from ecom_ops.bot.reply import BotReply
from ecom_ops.bot.store import TelegramConversationStore
from ecom_ops.bot.telegram_transport import (
    PerChatRateLimiter,
    TelegramApiError,
)
from ecom_ops.model_router import reset_model_router
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops import agent_tools as agent_tools_mod
from ecom_ops.wp_ops import review_plan as review_plan_mod
from ecom_ops.wp_ops.agent_tools import (
    _WP_TOOL_LABELS,
    EXTRA_FILE_TOOLS,
    FILE_LOOP_MAX_SECONDS,
    WpAgentTools,
    _safe_tool_detail,
    run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.page_review_loop import run_bounded_page_review_loop
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import VerifiedTheme
from ecom_ops.wp_ops.service import WpOpsService


def _setup_mock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
    monkeypatch.setenv("XAI_GROK_BIN", "")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()
    reset_model_router()


def test_merge_compat_run_llm_chat_persists_wp_review_hashes_atomically_without_clobbering_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.test")

    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        summary="Header spacing fix",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    assert (
        plans.create_file(
            "mock-staging", plan_id, "includes/spacing.php", "<?php\n// spacing\n", actor="oscar"
        ).ok
        is True
    )
    monkeypatch.setattr(review_plan_mod, "run_catalog", passing_catalog)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.data["status"] == "tests_passed"
    plan_row = plans.get(plan_id)
    assert plan_row is not None

    db_path = tmp_path / "jobs.sqlite3"
    conv_store = TelegramConversationStore(db_path)
    conv_store.set(
        42,
        {
            "flow": "confirm_action",
            "step": "await_confirm",
            "slots": {"pending": {"kind": "order_status", "order_id": "1001"}},
            "session": {"existing_key": "keep"},
            "messages": [{"role": "user", "content": "tidigare fråga"}],
        },
    )

    recorded_calls: list[dict[str, Any]] = []
    real_append = conv_store.append_turn_atomic

    def spy_append(chat_id: str | int, **kwargs: Any) -> dict[str, Any]:
        recorded_calls.append({"chat_id": chat_id, **kwargs})
        return real_append(chat_id, **kwargs)

    monkeypatch.setattr(conv_store, "append_turn_atomic", spy_append)
    monkeypatch.setattr(
        "ecom_ops.bot.handlers.run_chat",
        lambda *_a, **_k: ChatResult(
            text="Kontroller klara för staging.",
            messages=[
                {"role": "user", "content": "tidigare fråga"},
                {"role": "user", "content": "kör kontroller"},
                {"role": "assistant", "content": "Kontroller klara för staging."},
            ],
            review_stage_plan_id=plan_id,
        ),
    )

    handler = BotHandler(store=conv_store)
    reply = handler._run_llm_chat(42, "kör kontroller")
    assert reply.reply_markup is not None
    buttons = [btn for row in reply.reply_markup["inline_keyboard"] for btn in row]
    assert any(btn.get("callback_data") == f"wpreview:stage:{plan_id}" for btn in buttons)

    assert len(recorded_calls) == 1
    session_updates = recorded_calls[0]["session_updates"]
    assert isinstance(session_updates, dict)
    assert plan_id in session_updates["wp_review_hashes"]

    reopened = TelegramConversationStore(db_path)
    persisted = reopened.get(42)
    assert persisted is not None
    assert persisted["flow"] == "confirm_action"
    assert persisted["step"] == "await_confirm"
    assert persisted["slots"] == {"pending": {"kind": "order_status", "order_id": "1001"}}
    assert persisted["session"]["existing_key"] == "keep"
    assert persisted["session"]["wp_review_hashes"][plan_id] == {
        "actor": "oscar",
        "site_id": "mock-staging",
        "base_sha256": plan_row["base_sha256"],
        "work_sha256": plan_row["work_sha256"],
    }
    assert [m["content"] for m in persisted["messages"]] == [
        "tidigare fråga",
        "kör kontroller",
        "Kontroller klara för staging.",
    ]


def test_merge_compat_wpreview_stage_and_proposal_yes_coexist_across_job_store_and_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")

    db_path = tmp_path / "jobs.sqlite3"
    conv_store = TelegramConversationStore(db_path)
    job_store = TelegramJobStore(db_path)
    handler = BotHandler(store=conv_store)

    conv_store.set(
        42,
        {
            "session": {
                "wp_review_hashes": {
                    "wr-abcdef012345": {"actor": "oscar", "site_id": "mock-staging"}
                }
            }
        },
    )
    cached_state = conv_store.get(42)
    assert cached_state is not None

    proposal = job_store.create_proposal(
        42,
        kind="wp_content",
        payload={"site_id": "mock-staging", "action": "update_page"},
        site_id="mock-staging",
    )
    proposal_id = proposal["proposal_id"]

    wpreview_cb = "wpreview:stage:wr-abcdef012345"
    proposal_cb = f"proposal:yes:{proposal_id}"

    assert job_store_mod._ACTION_CALLBACK.match(wpreview_cb) is not None
    assert job_store_mod._ACTION_CALLBACK.match(proposal_cb) is not None
    assert bot_main._ACTION_CALLBACK.fullmatch(wpreview_cb) is not None
    assert bot_main._ACTION_CALLBACK.fullmatch(proposal_cb) is not None
    assert job_store_mod._ACTION_CALLBACK.match("wpreview:no:wr-abcdef012345") is None
    assert bot_main._ACTION_CALLBACK.fullmatch(f"proposal:no:{proposal_id}") is None

    wp_accepted = job_store.accept_update(
        {
            "update_id": 2001,
            "callback_query": {
                "id": "cb-wp",
                "data": wpreview_cb,
                "message": {"message_id": 11, "chat": {"id": 42}},
            },
        }
    )
    assert wp_accepted["disposition"] == "queued"
    wp_job = job_store.get_job(wp_accepted["job_id"])
    assert wp_job is not None
    assert wp_job["kind"] == "action"
    assert wp_job["payload"]["approval_snapshot_required"] is True
    assert wp_job["payload"]["approval_snapshot"] == TelegramJobStore.approval_snapshot(cached_state)
    assert "proposal_id" not in wp_job["payload"]
    assert bot_main._action_lock_key(wp_job, handler) == "wp-file-edits"
    assert bot_main._preflight_action_job(handler, wp_job, store=job_store) is None

    prop_accepted = job_store.accept_update(
        {
            "update_id": 2002,
            "callback_query": {
                "id": "cb-prop",
                "data": proposal_cb,
                "message": {"message_id": 12, "chat": {"id": 42}},
            },
        }
    )
    assert prop_accepted["disposition"] == "queued"
    prop_job = job_store.get_job(prop_accepted["job_id"])
    assert prop_job is not None
    assert prop_job["kind"] == "action"
    assert prop_job["payload"]["proposal_id"] == proposal_id
    assert "approval_snapshot_required" not in prop_job["payload"]
    assert bot_main._action_lock_key(prop_job, handler) == "approved-actions"
    assert bot_main._preflight_action_job(handler, prop_job, store=job_store) is None

    missing_snapshot_job = {
        **wp_job,
        "payload": {k: v for k, v in wp_job["payload"].items() if not k.startswith("approval_snapshot")},
    }
    assert bot_main._preflight_action_job(handler, missing_snapshot_job, store=job_store) is not None

    assert job_store.consume_proposal(proposal_id, chat_id=42) is not None
    assert bot_main._preflight_action_job(handler, prop_job, store=job_store) is not None


def test_merge_compat_attachments_roundtrip_send_photo_delivery_and_token_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    png_path = (
        tmp_path
        / "wp_workspaces"
        / "mock-staging"
        / "wr-abcdef012345"
        / "artifacts"
        / "work-desktop.png"
    )
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02\x03" * 4
    png_path.write_bytes(png_bytes)

    original = BotReply(
        text="Preview klar.",
        needs_typing=True,
        attachments=(str(png_path),),
    )
    serialized = bot_main._result_for_reply(original)
    assert serialized["attachments"] == [str(png_path)]
    restored = bot_main._reply_from_result(serialized)
    assert restored.text == "Preview klar."
    assert restored.attachments == (str(png_path),)

    secret_token = "123456:ABCDEF_secret_token_99"
    requests_seen: list[tuple[str, str, bytes]] = []

    class _FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._body = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    next_msg_id = iter(range(100, 120))

    def fake_urlopen(req: urllib.request.Request, timeout: float = 15.0) -> _FakeResponse:
        del timeout
        body = req.data if isinstance(req.data, bytes) else b""
        content_type = str(req.get_header("Content-type") or "")
        requests_seen.append((req.full_url, content_type, body))
        return _FakeResponse({"ok": True, "result": {"message_id": next(next_msg_id)}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    bot_main._send_reply(secret_token, 42, restored, reply_to_message_id=77)
    assert len(requests_seen) == 2
    assert requests_seen[0][0].endswith("/sendMessage")
    photo_url, photo_ctype, photo_body = requests_seen[1]
    assert photo_url.endswith("/sendPhoto")
    assert photo_ctype.startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"\r\n\r\n42\r\n' in photo_body
    assert b'name="reply_to_message_id"\r\n\r\n77\r\n' in photo_body
    assert b'name="allow_sending_without_reply"\r\n\r\ntrue\r\n' in photo_body
    assert b'filename="work-desktop.png"' in photo_body
    assert png_bytes in photo_body

    requests_seen.clear()
    job_store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    accepted = job_store.accept_update(
        {
            "update_id": 3001,
            "message": {"message_id": 88, "chat": {"id": 42}, "text": "visa preview"},
        }
    )
    job = job_store.claim_next()
    assert job is not None and job["job_id"] == accepted["job_id"]
    job_store.complete_job(job["job_id"], serialized)
    outbox_job = job_store.get_job(job["job_id"])
    assert outbox_job is not None

    turn = bot_main._LiveTelegramTurn(
        secret_token,
        42,
        reply_to_message_id=88,
        rate_limiter=PerChatRateLimiter(),
    )
    assert turn.finish(bot_main._reply_from_result(outbox_job["result"]), ok=True) is True
    assert len(requests_seen) == 2
    assert requests_seen[0][0].endswith("/sendMessage")
    assert requests_seen[1][0].endswith("/sendPhoto")
    assert b'name="reply_to_message_id"\r\n\r\n88\r\n' in requests_seen[1][2]
    assert png_bytes in requests_seen[1][2]

    requests_seen.clear()
    monkeypatch.setattr(bot_main, "_DEFAULT_RATE_LIMITER", PerChatRateLimiter())
    assert bot_main._deliver_job_result(secret_token, job_store, outbox_job) is True
    assert len(requests_seen) == 2
    assert requests_seen[1][0].endswith("/sendPhoto")
    assert b'name="reply_to_message_id"\r\n\r\n88\r\n' in requests_seen[1][2]
    delivered_job = job_store.get_job(job["job_id"])
    assert delivered_job is not None and delivered_job["delivery_state"] == "delivered"

    def failing_urlopen(req: urllib.request.Request, timeout: float = 15.0) -> Any:
        del timeout
        err_body = json.dumps(
            {
                "ok": False,
                "error_code": 400,
                "description": f"Bad Request via bot{secret_token}/sendPhoto",
            }
        ).encode("utf-8")
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(err_body),
        )

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    with pytest.raises(TelegramApiError) as exc_info:
        telegram_transport.send_photo(
            secret_token,
            42,
            png_path,
            caption="test",
            reply_to_message_id=88,
        )
    assert secret_token not in str(exc_info.value)
    assert secret_token not in exc_info.value.description
    assert "bot[REDACTED]" in exc_info.value.description
    assert exc_info.value.__cause__ is None


def test_merge_compat_file_tool_loop_progress_events_cancel_after_three_steers_and_budget_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        summary="Static check test",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    assert (
        plans.create_file(
            "mock-staging", plan_id, "includes/helper.php", "<?php\n// safe\n", actor="oscar"
        ).ok
        is True
    )

    assert {"wp_playbook", "wp_workspace_static_checks"}.issubset(EXTRA_FILE_TOOLS)
    assert _safe_tool_detail("wp_playbook", {"topic": "child_theme_edit"}, None) == "child_theme_edit"
    assert (
        _safe_tool_detail(
            "wp_workspace_static_checks",
            {"site_id": "mock-staging", "plan_id": plan_id},
            None,
        )
        == f"mock-staging · {plan_id}"
    )

    svc = WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    tools = WpAgentTools(svc)
    events: list[ProgressEvent] = []

    @mark_event_consumer
    def collect(ev: ProgressEvent) -> None:
        events.append(ev)

    followup_answers = iter(
        [
            (
                tool_call(
                    "wp_workspace_static_checks",
                    {"site_id": "mock-staging", "plan_id": plan_id},
                ),
                0.001,
            ),
            ("Guide och statisk kontroll är klara.", 0.001),
        ]
    )
    res = run_bounded_file_tool_loop(
        tool_call("wp_playbook", {"topic": "child_theme_edit"}),
        [{"role": "user", "content": "kör guide och statisk kontroll"}],
        complete=lambda _msgs: next(followup_answers),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
        on_progress=collect,
    )
    assert res.handled is True
    assert res.calls == 2

    playbook_done = [
        ev for ev in events if ev.tool == "wp_playbook" and ev.phase == "completed"
    ]
    assert len(playbook_done) == 1
    assert playbook_done[0].label == _WP_TOOL_LABELS["wp_playbook"]
    assert playbook_done[0].detail == "child_theme_edit"
    assert playbook_done[0].outcome == "guide: child_theme_edit"

    static_done = [
        ev for ev in events if ev.tool == "wp_workspace_static_checks" and ev.phase == "completed"
    ]
    assert len(static_done) == 1
    assert static_done[0].label == _WP_TOOL_LABELS["wp_workspace_static_checks"]
    assert static_done[0].detail == f"mock-staging · {plan_id}"
    assert static_done[0].outcome is not None and "statiska fynd" in static_done[0].outcome

    # Verify before_tool() is still invoked on the 4th+ iteration so ChatCancelled interrupts after 3 steers
    steer_events: list[ProgressEvent] = []

    @mark_event_consumer
    def collect_steer(ev: ProgressEvent) -> None:
        steer_events.append(ev)

    before_calls = 0

    def steer_then_cancel() -> list[str]:
        nonlocal before_calls
        before_calls += 1
        if before_calls <= 3:
            return [f"följdfråga {before_calls}"]
        raise ChatCancelled()

    with pytest.raises(ChatCancelled):
        run_bounded_file_tool_loop(
            tool_call("wp_playbook", {"topic": "child_theme_edit"}),
            [{"role": "user", "content": "testa avbrott"}],
            complete=lambda _msgs: (
                tool_call("wp_playbook", {"topic": "child_theme_edit"}),
                0.001,
            ),
            actor="oscar",
            channel="telegram",
            conversation_id="telegram:42",
            tools=tools,
            on_progress=collect_steer,
            before_tool=steer_then_cancel,
        )
    assert before_calls == 4
    assert [ev.phase for ev in steer_events] == ["steer", "steer", "steer"]

    # Verify _finish_on_budget emits a failed ProgressEvent with ToolTimeBudgetExceeded on timeout
    timeout_events: list[ProgressEvent] = []

    @mark_event_consumer
    def collect_timeout(ev: ProgressEvent) -> None:
        timeout_events.append(ev)

    ticks = iter([100.0, 100.0 + FILE_LOOP_MAX_SECONDS + 5.0])
    monkeypatch.setattr(agent_tools_mod.time, "monotonic", lambda: next(ticks))

    budget_res = run_bounded_file_tool_loop(
        tool_call("wp_playbook", {"topic": "child_theme_edit"}),
        [{"role": "user", "content": "testa tidsbudget"}],
        complete=lambda _msgs: ("Sammanfattning efter tidsgräns.", 0.001),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
        on_progress=collect_timeout,
    )
    assert budget_res.handled is True
    assert budget_res.calls == 1
    assert any(
        ev.step_id == "wp-file-budget-timeout"
        and ev.tool == "wp_file_loop"
        and ev.phase == "failed"
        and ev.error_class == "ToolTimeBudgetExceeded"
        for ev in timeout_events
    )


def test_merge_compat_page_review_loop_on_checkpoint_target_page_and_io_timeout_failed_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    events: list[ProgressEvent] = []

    @mark_event_consumer
    def collect(ev: ProgressEvent) -> None:
        events.append(ev)

    checkpoints_called = 0

    def checkpoint() -> list[str]:
        nonlocal checkpoints_called
        checkpoints_called += 1
        return ["fokusera på checkout"] if checkpoints_called == 1 else []

    class _TimeoutWpClient:
        timeout = 10.0

        def list_themes(self) -> list[dict[str, Any]]:
            raise TimeoutError("simulated REST timeout")

    outcome = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=_TimeoutWpClient(),
        registry_path=MOCK_REGISTRY,
        data_dir=tmp_path,
        on_progress=collect,
        on_checkpoint=checkpoint,
        target_page="/kassa",
    )

    assert checkpoints_called >= 1
    assert any(
        ev.tool == "page_review" and ev.phase == "steer" and "Orderverktyg kördes inte" in (ev.outcome or "")
        for ev in events
    )

    home_step = next(row for row in outcome.steps if row["tool"] == "wp_page_review_home")
    assert home_step["status"] == "not_run"
    assert home_step["reason"] == "io_timeout"

    home_done = [
        ev
        for ev in events
        if ev.tool == "wp_page_review_home" and ev.phase != "started"
    ]
    assert len(home_done) == 1
    assert home_done[0].phase == "failed"
    assert home_done[0].error_class == "io_timeout"
    assert home_done[0].outcome == "tidsgräns nådd (timeout)"
