"""Tests for structured Telegram progress events, cooperative cancellation, and proposal-bound HITL."""

from __future__ import annotations

import time

import pytest

from ecom_ops.bot import chat_agent, handlers
from ecom_ops.bot.chat_agent import (
    _WP_TOOL_LABELS,
    ChatCancelled,
    ChatResult,
    ToolPrefetch,
    _inspect_tool_outcome,
    _prefetch_listed_orders,
    _run_fast_read_loop,
    gather_tool_results,
    run_chat,
)
from ecom_ops.bot.dialog_actions import PendingAction
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.progress_events import (
    ProgressEvent,
    format_progress_line,
    mark_event_consumer,
)
from ecom_ops.bot.reply import proposal_confirm_actions
from ecom_ops.bot.store import ConversationStore
from ecom_ops.wp_ops.agent_tools import context_for_storage


def _collect_events() -> tuple[list[ProgressEvent], object]:
    events: list[ProgressEvent] = []

    @mark_event_consumer
    def on_progress(ev: ProgressEvent) -> None:
        events.append(ev)

    return events, on_progress


@pytest.fixture()
def env_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "100,200")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "100:oscar,200:operator")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, cap: True
    )
    return tmp_path


def test_wp_open_review_workspace_label_present():
    assert _WP_TOOL_LABELS.get("wp_open_review_workspace") == "Granskningsyta"


def test_proposal_confirm_actions_format():
    actions = proposal_confirm_actions("pr-abcdef012345")
    assert len(actions.rows) == 1
    assert len(actions.rows[0]) == 2
    assert actions.rows[0][0].payload == "proposal:yes:pr-abcdef012345"
    assert actions.rows[0][1].payload == "proposal:no:pr-abcdef012345"


def test_prefetch_emits_structured_events_and_no_pii(env_setup, monkeypatch):
    monkeypatch.setattr(
        chat_agent,
        "tool_lookup_order",
        lambda oid, domain=None: (
            f"Order #{oid}\n"
            "Status: processing\n"
            "Total: 499 SEK\n"
            "Kund: customer@example.com\n"
            "Token: Bearer sk-supersecretkey123456\n"
        ),
    )
    events, on_progress = _collect_events()
    pref = gather_tool_results(
        "Vad är status på order 1001?",
        on_progress=on_progress,
    )
    assert any(name == "lookup_order" for name, _body in pref.results)
    started = [e for e in events if e.phase == "started" and e.tool == "lookup_order"]
    completed = [e for e in events if e.phase == "completed" and e.tool == "lookup_order"]
    assert len(started) == 1
    assert len(completed) == 1
    assert completed[0].elapsed_ms is not None and completed[0].elapsed_ms >= 0
    assert completed[0].provenance == "mock"

    for ev in events:
        rendered = format_progress_line(ev)
        for forbidden in ("SEK", "499", "customer@", "sk-supersecretkey"):
            assert forbidden not in rendered
            assert forbidden not in (ev.outcome or "")
            assert forbidden not in (ev.detail or "")


def test_soft_error_marks_event_failed_not_completed(env_setup, monkeypatch):
    ok, summary = _inspect_tool_outcome(
        "lookup_order", "Order lookup failed: HTTP 502 Bad Gateway"
    )
    assert ok is False
    assert "HTTP 502" in summary or "misslyckades" in summary

    ok2, summary2 = _inspect_tool_outcome(
        "lookup_order", "Kunde inte hämta order 9999 för customer@example.com (499 SEK)"
    )
    assert ok2 is False
    assert "customer@" not in summary2
    assert "499" not in summary2
    assert "SEK" not in summary2

    monkeypatch.setattr(
        chat_agent,
        "tool_lookup_order",
        lambda oid, domain=None: "Order lookup failed: HTTP 500 server error",
    )
    events, on_progress = _collect_events()
    gather_tool_results(
        "Kolla order 8888",
        on_progress=on_progress,
    )
    failed = [e for e in events if e.tool == "lookup_order" and e.phase == "failed"]
    completed = [e for e in events if e.tool == "lookup_order" and e.phase == "completed"]
    assert len(failed) == 1
    assert completed == []


def test_slash_commands_and_actions_emit_progress_events(env_setup, monkeypatch):
    store = ConversationStore(env_setup / "state.json")
    bot = BotHandler(store=store)

    # /health
    events_health, cb_health = _collect_events()
    reply_health = bot.handle(100, "/health", on_progress=cb_health)
    assert reply_health.text
    assert any(e.tool == "health" and e.phase == "started" for e in events_health)
    health_done = [
        e for e in events_health if e.tool == "health" and e.phase in {"completed", "failed"}
    ]
    assert len(health_done) == 1
    assert health_done[0].elapsed_ms is not None
    assert health_done[0].provenance == "mock"

    # /order 1001
    monkeypatch.setattr(
        handlers,
        "tool_lookup_order",
        lambda oid, domain=None: (
            f"Order #{oid}\nStatus: processing\nTotal: 499 SEK\nKund: customer@example.com"
        ),
    )
    events_order, cb_order = _collect_events()
    reply_order = bot.handle(100, "/order 1001", on_progress=cb_order)
    assert "1001" in reply_order.text
    assert any(e.tool == "order_lookup" and e.phase == "started" for e in events_order)
    order_done = [e for e in events_order if e.tool == "order_lookup" and e.phase == "completed"]
    assert len(order_done) == 1
    assert order_done[0].elapsed_ms is not None
    assert "SEK" not in (order_done[0].outcome or "")
    assert "customer@" not in (order_done[0].outcome or "")

    # Approved action callback (order:set)
    monkeypatch.setattr(
        handlers,
        "execute_order_status",
        lambda order_id, status, actor, domain=None: (True, f"Order #{order_id} -> {status}"),
    )
    events_act, cb_act = _collect_events()
    reply_act = bot.handle_callback(100, "order:set:1001:completed", on_progress=cb_act)
    assert "Klart." in reply_act.text
    assert any(e.tool == "order_status" and e.phase == "started" for e in events_act)
    act_done = [e for e in events_act if e.tool == "order_status" and e.phase == "completed"]
    assert len(act_done) == 1
    assert act_done[0].elapsed_ms is not None


def test_stream_clear_used_instead_of_text_reset_on_read_rounds_and_steers(env_setup, monkeypatch):
    calls = {"n": 0}
    phase_seen: list[dict] = []

    def complete_fast(messages):
        calls["n"] += 1
        phase_seen.append({"phase": "streaming", "model": "fast-test"})
        if calls["n"] == 1:
            return '<tool_call>{"name":"lookup_order","arguments":{"order_id":"1001"}}</tool_call>', 0.0
        return "Order 1001 är klar.", 0.0

    monkeypatch.setattr(
        chat_agent,
        "tool_lookup_order",
        lambda oid, domain=None: f"Order #{oid}\nStatus: completed",
    )

    clears: list[int] = []
    resets: list[int] = []
    events, on_progress = _collect_events()
    steer_sent = {"done": False}

    def checkpoint() -> list[str]:
        if calls["n"] == 1 and not steer_sent["done"]:
            steer_sent["done"] = True
            return ["Kolla även om den är levererad"]
        return []

    tool_bits: list[tuple[str, str]] = []
    reply, cost, _shown = _run_fast_read_loop(
        messages=[{"role": "user", "content": "Kolla order 1001"}],
        tool_bits=tool_bits,
        complete_fast=complete_fast,
        domain="azom.se",
        offset=0,
        on_progress=on_progress,
        on_text_reset=lambda: resets.append(1),
        on_stream_clear=lambda: clears.append(1),
        started_at=time.monotonic(),
        on_checkpoint=checkpoint,
    )
    assert "Order 1001 är klar." in reply
    assert cost == 0.0
    assert len(clears) >= 1
    assert resets == []
    assert len(phase_seen) >= 1
    assert any(e.phase == "steer" for e in events)
    assert any(e.tool == "lookup_order" and e.phase == "completed" for e in events)


def test_run_chat_forwards_on_phase_change_conditionally(env_setup, monkeypatch):
    # 1) Minimal stub WITHOUT on_phase_change must NOT crash
    def minimal_chat(
        messages,
        max_tokens=700,
        temperature=0.3,
        model=None,
        route_id=None,
        timeout=None,
        telemetry=None,
        site=None,
        actor=None,
        on_text_delta=None,
        on_text_reset=None,
    ):
        return "Hej från stub!", 0.0

    monkeypatch.setattr(chat_agent, "chat_completion", minimal_chat)
    res = run_chat(
        "Hej där",
        actor="oscar",
        on_stream_clear=lambda: None,
        on_phase_change=lambda _info: None,
    )
    assert res.text == "Hej från stub!"

    # 2) Rich stub WITH on_phase_change receives it
    seen_phases: list[dict] = []

    def rich_chat(
        messages,
        max_tokens=700,
        temperature=0.3,
        model=None,
        route_id=None,
        timeout=None,
        telemetry=None,
        site=None,
        actor=None,
        on_text_delta=None,
        on_text_reset=None,
        on_phase_change=None,
    ):
        if on_phase_change:
            on_phase_change({"phase": "reasoning", "route_id": "test"})
        return "Hej med faser!", 0.0

    monkeypatch.setattr(chat_agent, "chat_completion", rich_chat)
    res2 = run_chat(
        "Hej igen",
        actor="oscar",
        on_phase_change=seen_phases.append,
    )
    assert res2.text == "Hej med faser!"
    assert seen_phases == [{"phase": "reasoning", "route_id": "test"}]


def test_chat_cancelled_aborts_prefetch_and_fast_read_loop_immediately(env_setup, monkeypatch):
    fetched: list[str] = []

    def fake_lookup(oid, domain=None):
        fetched.append(oid)
        return f"Order #{oid}\nStatus: processing"

    monkeypatch.setattr(chat_agent, "tool_lookup_order", fake_lookup)

    checkpoint_calls = {"n": 0}

    def cancel_on_second() -> list[str]:
        checkpoint_calls["n"] += 1
        if checkpoint_calls["n"] >= 2:
            raise ChatCancelled("cancelled by user")
        return []

    history = [
        {
            "role": "assistant",
            "content": (
                "Senaste ordrar:\n"
                "- 1001 | processing | 2026-07-10 | 10 SEK\n"
                "- 1002 | processing | 2026-07-10 | 10 SEK\n"
                "- 1003 | processing | 2026-07-10 | 10 SEK"
            ),
        }
    ]
    pref = ToolPrefetch()
    with pytest.raises(ChatCancelled):
        _prefetch_listed_orders(
            pref,
            "Kolla alla dessa 3 ordrar",
            history,
            on_progress=None,
            domain="azom.se",
            started_at=time.monotonic(),
            on_checkpoint=cancel_on_second,
        )
    assert fetched == ["1001"]

    # Fast read loop cooperative cancel before model round
    model_called = {"n": 0}

    def complete_fast(messages):
        model_called["n"] += 1
        return "should not run", 0.0

    def cancel_immediately() -> list[str]:
        raise ChatCancelled("cancelled before round")

    with pytest.raises(ChatCancelled):
        _run_fast_read_loop(
            messages=[{"role": "user", "content": "Hej"}],
            tool_bits=[],
            complete_fast=complete_fast,
            domain="azom.se",
            offset=0,
            on_progress=None,
            on_text_reset=None,
            started_at=time.monotonic(),
            on_checkpoint=cancel_immediately,
        )
    assert model_called["n"] == 0


def test_parallel_proposals_freeze_context_and_enforce_single_use(env_setup, monkeypatch):
    conv_store = ConversationStore(env_setup / "state.json")
    job_store = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=conv_store, proposal_store=job_store)

    # Seed chat 100 with market azom.se and valid wp site_id site-se
    conv_store.set(
        100,
        {
            "flow": None,
            "step": None,
            "slots": {},
            "session": {
                "last_market": "azom.se",
                "wp_ops_context": context_for_storage(
                    actor="oscar",
                    channel="telegram",
                    conversation_id="telegram:100",
                    site_id="site-se",
                    operation_ids=[],
                ),
            },
            "messages": [],
            "tool_digest": "",
        },
    )

    # Turn 1 creates Proposal A (order 1001 -> completed on azom.se)
    def fake_run_chat_a(*args, **kwargs):
        return ChatResult(
            text="Ändra order #1001 till completed?",
            messages=[
                {"role": "user", "content": "Sätt order 1001 till klar"},
                {"role": "assistant", "content": "Ändra order #1001 till completed?"},
            ],
            pending_action=PendingAction(
                kind="order_status",
                payload={"order_id": "1001", "status": "completed"},
            ),
        )

    monkeypatch.setattr(handlers, "run_chat", fake_run_chat_a)
    reply_a = bot.handle(100, "Sätt order 1001 till klar", task_id="task-a")
    assert reply_a.actions is not None
    yes_a = reply_a.actions.rows[0][0].payload
    no_a = reply_a.actions.rows[0][1].payload
    assert yes_a is not None and yes_a.startswith("proposal:yes:pr-")
    assert no_a is not None and no_a.startswith("proposal:no:pr-")

    # Switch session market to azom.no & site-no, then create Proposal B (order 2002 -> cancelled)
    state_mid = conv_store.get(100) or {}
    session_mid = dict(state_mid.get("session") or {})
    session_mid["last_market"] = "azom.no"
    session_mid["wp_ops_context"] = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:100",
        site_id="site-no",
        operation_ids=[],
    )
    conv_store.set(
        100, {**state_mid, "flow": None, "step": None, "slots": {}, "session": session_mid}
    )

    def fake_run_chat_b(*args, **kwargs):
        return ChatResult(
            text="Ändra order #2002 till cancelled?",
            messages=list(kwargs.get("history") or [])
            + [
                {"role": "user", "content": "Avbryt order 2002"},
                {"role": "assistant", "content": "Ändra order #2002 till cancelled?"},
            ],
            pending_action=PendingAction(
                kind="order_status",
                payload={"order_id": "2002", "status": "cancelled"},
            ),
        )

    monkeypatch.setattr(handlers, "run_chat", fake_run_chat_b)
    reply_b = bot.handle(100, "Avbryt order 2002", task_id="task-b")
    assert reply_b.actions is not None
    yes_b = reply_b.actions.rows[0][0].payload
    no_b = reply_b.actions.rows[0][1].payload
    assert yes_b is not None and yes_b.startswith("proposal:yes:pr-")
    assert yes_b != yes_a
    assert no_b is not None

    # Verify frozen domain & site_id in store
    pid_a = yes_a.split(":")[-1]
    pid_b = yes_b.split(":")[-1]
    stored_a = job_store.get_proposal(pid_a, chat_id=100)
    stored_b = job_store.get_proposal(pid_b, chat_id=100)
    assert stored_a is not None and stored_a["domain"] == "azom.se" and stored_a["site_id"] == "site-se"
    assert stored_b is not None and stored_b["domain"] == "azom.no" and stored_b["site_id"] == "site-no"

    # Clicking Proposal A executes EXACTLY Proposal A with frozen azom.se!
    executed: list[dict] = []

    def fake_exec_order(order_id, status, actor, domain=None):
        executed.append({"order_id": order_id, "status": status, "actor": actor, "domain": domain})
        return True, f"Order #{order_id} uppdaterad ({domain})"

    monkeypatch.setattr(handlers, "execute_order_status", fake_exec_order)

    events_a, cb_a = _collect_events()
    res_a = bot.handle_callback(100, yes_a, on_progress=cb_a)
    assert "Klart." in res_a.text
    assert executed == [
        {"order_id": "1001", "status": "completed", "actor": "oscar", "domain": "azom.se"}
    ]
    assert any(e.tool == "order_status" and e.phase == "completed" for e in events_a)

    # Second click on Proposal A MUST be rejected without re-executing!
    res_a2 = bot.handle_callback(100, yes_a)
    assert "redan hanterat eller utgånget" in res_a2.text.lower()
    assert len(executed) == 1

    # Cancel Proposal B via proposal:no:...
    res_b_no = bot.handle_callback(100, no_b)
    assert "Avbrutet" in res_b_no.text
    # Clicking Proposal B yes after cancel MUST be rejected!
    res_b_yes = bot.handle_callback(100, yes_b)
    assert "redan hanterat eller utgånget" in res_b_yes.text.lower()
    assert len(executed) == 1


def test_freetext_ja_does_not_execute_when_proposal_store_active_on_telegram(env_setup, monkeypatch):
    conv_store = ConversationStore(env_setup / "state.json")
    job_store = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=conv_store, proposal_store=job_store, channel="telegram")

    def fake_run_chat(*args, **kwargs):
        return ChatResult(
            text="Ändra order #1001 till completed?",
            messages=[
                {"role": "user", "content": "Sätt order 1001 till klar"},
                {"role": "assistant", "content": "Ändra order #1001 till completed?"},
            ],
            pending_action=PendingAction(
                kind="order_status",
                payload={"order_id": "1001", "status": "completed"},
            ),
        )

    monkeypatch.setattr(handlers, "run_chat", fake_run_chat)
    executed: list[str] = []
    monkeypatch.setattr(
        handlers,
        "execute_order_status",
        lambda order_id, status, actor, domain=None: (executed.append(order_id) or True, "ok"),
    )

    first = bot.handle(100, "Sätt order 1001 till klar")
    assert first.actions is not None
    yes_cb = first.actions.rows[0][0].payload
    assert yes_cb is not None and yes_cb.startswith("proposal:yes:pr-")

    # Free-text "ja", "ok", "kör" MUST NOT execute the action!
    for phrase in ("ja", "ok", "kör"):
        resp = bot.handle(100, phrase)
        assert executed == []
        assert "Ja-knappen" in resp.text
        assert resp.actions is not None
        assert resp.actions.rows[0][0].payload == yes_cb


def test_continue_flow_forwards_callbacks_and_atomic_turn_preserves_pending(env_setup, monkeypatch):
    conv_store = ConversationStore(env_setup / "state.json")
    job_store = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=conv_store, proposal_store=job_store)

    # Set an active pending_action flow
    conv_store.set(
        100,
        {
            "flow": "pending_action",
            "step": "confirm",
            "slots": {
                "pending": PendingAction(
                    kind="order_status", payload={"order_id": "1001", "status": "completed"}
                ).to_dict(),
                "proposal_id": "pr-1234567890ab",
            },
            "session": {"last_market": "azom.se"},
            "messages": [{"role": "user", "content": "tidigare"}],
            "tool_digest": "",
        },
    )

    seen_kwargs: dict = {}

    def fake_run_chat(message, **kwargs):
        seen_kwargs.update(kwargs)
        return ChatResult(
            text="Svar på ny fråga",
            messages=list(kwargs.get("history") or [])
            + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "Svar på ny fråga"},
            ],
        )

    monkeypatch.setattr(handlers, "run_chat", fake_run_chat)

    clear_cb = lambda: None  # noqa: E731
    phase_cb = lambda _d: None  # noqa: E731
    chk_cb = lambda: []  # noqa: E731
    prog_cb = lambda _e: None  # noqa: E731

    # Free-text mid-confirm (>8 chars) pivots to _run_llm_chat and MUST forward all callbacks
    reply = bot.handle(
        100,
        "Hur många öppna ärenden har vi?",
        on_progress=prog_cb,
        on_stream_clear=clear_cb,
        on_phase_change=phase_cb,
        on_checkpoint=chk_cb,
        task_id="task-read-1",
    )
    assert reply.text == "Svar på ny fråga"
    assert seen_kwargs.get("on_progress") is prog_cb
    assert seen_kwargs.get("on_stream_clear") is clear_cb
    assert seen_kwargs.get("on_phase_change") is phase_cb
    assert seen_kwargs.get("on_checkpoint") is chk_cb

    # Verify atomic turn merge does NOT overwrite another task's pending_action flow
    conv_store.set(
        200,
        {
            "flow": "pending_action",
            "step": "confirm",
            "slots": {"proposal_id": "pr-aaaaaaaaaaaa"},
            "session": {"last_market": "azom.se"},
            "messages": [],
            "tool_digest": "",
        },
    )
    bot._run_llm_chat(200, "En parallell läsfråga", task_id="task-read-2")
    after = conv_store.get(200) or {}
    assert after.get("flow") == "pending_action"
    assert (after.get("slots") or {}).get("proposal_id") == "pr-aaaaaaaaaaaa"
    assert len(after.get("messages") or []) == 2
