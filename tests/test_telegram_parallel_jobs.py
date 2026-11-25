"""Tests for hybrid steer routing, parallel Telegram jobs, proposals, locks, and atomic store."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from ecom_ops.bot.job_store import (
    MAX_CONCURRENT_TASKS_PER_CHAT,
    MAX_QUEUED_PER_CHAT,
    MAX_STEERS_PER_JOB,
    TelegramJobStore,
)
from ecom_ops.bot.steer_router import RouteDecision, decide_message_routing
from ecom_ops.bot.store import ConversationStore, TelegramConversationStore


def _msg_update(
    update_id: int,
    chat_id: int,
    text: str,
    *,
    message_id: int | None = None,
    reply_to_message_id: int | None = None,
) -> dict:
    message: dict = {
        "message_id": message_id if message_id is not None else 100 + update_id,
        "chat": {"id": chat_id},
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {"update_id": update_id, "message": message}


def _cb_update(
    update_id: int,
    chat_id: int,
    data: str,
    *,
    callback_id: str | None = None,
    message_id: int = 500,
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id or f"cb-{update_id}",
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }


def _running_job(
    job_id: str,
    text: str,
    *,
    task_id: str | None = None,
    kind: str = "read_turn",
    source_message_id: int = 10,
    status_message_id: int | None = 20,
    answer_message_ids: list[int] | None = None,
    steer_used: int = 0,
    steer_pending: int = 0,
) -> dict:
    return {
        "job_id": job_id,
        "task_id": task_id or job_id,
        "kind": kind,
        "payload": {"text": text, "source_message_id": source_message_id},
        "status_message_id": status_message_id,
        "answer_message_ids": answer_message_ids or [],
        "steer_used": steer_used,
        "steer_pending": steer_pending,
    }


# ---------------------------------------------------------------------------
# 1. steer_router tests
# ---------------------------------------------------------------------------


def test_router_task_command_always_starts_new_task():
    running = [_running_job("aaaa1111bbbb2222", "granska startsidans källkod", kind="page_review")]
    dec = decide_message_routing(
        "/task kolla order 1001",
        reply_to_message_id=None,
        running_jobs=running,
    )
    assert isinstance(dec, RouteDecision)
    assert dec.mode == "new_task"
    assert dec.target_job_id is None
    assert dec.cleaned_text == "kolla order 1001"


def test_router_steer_command_by_job_id_and_task_id_prefix_and_single_fallback():
    job_a = _running_job("aaaa111100000000", "visa order 1001", task_id="1111aaaa00000000")
    job_b = _running_job("bbbb222200000000", "kolla ga4 trafik", task_id="2222bbbb00000000")

    by_job = decide_message_routing(
        "/steer bbbb22 kortare tack",
        reply_to_message_id=None,
        running_jobs=[job_a, job_b],
    )
    assert by_job.mode == "steer"
    assert by_job.target_job_id == job_b["job_id"]
    assert by_job.cleaned_text == "kortare tack"

    by_task = decide_message_routing(
        "/steer 1111aa på engelska",
        reply_to_message_id=None,
        running_jobs=[job_a, job_b],
    )
    assert by_task.mode == "steer"
    assert by_task.target_job_id == job_a["job_id"]
    assert by_task.cleaned_text == "på engelska"

    single = decide_message_routing(
        "/steer ta med fraktinfo",
        reply_to_message_id=None,
        running_jobs=[job_a],
    )
    assert single.mode == "steer"
    assert single.target_job_id == job_a["job_id"]
    assert single.cleaned_text == "ta med fraktinfo"

    no_unique = decide_message_routing(
        "/steer kortare",
        reply_to_message_id=None,
        running_jobs=[job_a, job_b],
    )
    assert no_unique.mode == "new_task"
    assert no_unique.target_job_id is None


def test_router_other_slash_commands_start_new_task():
    running = [_running_job("aaaa111100000000", "visa order 1001")]
    for cmd in ("/order 2002", "/cases list", "/brief", "/marketing"):
        dec = decide_message_routing(cmd, reply_to_message_id=None, running_jobs=running)
        assert dec.mode == "new_task", cmd
        assert dec.cleaned_text == cmd


def test_router_explicit_reply_matches_source_status_and_answer_ids():
    job_a = _running_job(
        "aaaa111100000000",
        "visa order 1001",
        source_message_id=101,
        status_message_id=201,
        answer_message_ids=[301, 302],
    )
    job_b = _running_job(
        "bbbb222200000000",
        "kolla ga4",
        source_message_id=102,
        status_message_id=202,
        answer_message_ids=[303],
    )

    for mid in (101, 201, 302):
        dec = decide_message_routing(
            "kolla order 9999",
            reply_to_message_id=mid,
            running_jobs=[job_a, job_b],
        )
        assert dec.mode == "steer"
        assert dec.target_job_id == job_a["job_id"]

    full_a = _running_job(
        "aaaa111100000000",
        "visa order 1001",
        status_message_id=201,
        steer_used=2,
        steer_pending=1,
    )
    dec_full = decide_message_routing(
        "kortare",
        reply_to_message_id=201,
        running_jobs=[full_a, _running_job("cccc333300000000", "granska", kind="page_review")],
        max_steers_per_job=3,
    )
    assert dec_full.mode == "new_task"


def test_router_page_review_strict_isolation():
    pr_job = _running_job("pppp111100000000", "granska startsidans källkod", kind="page_review")
    for text in ("kolla order 1001", "ja", "hur går google ads?", "ny uppgift kolla ga4"):
        dec = decide_message_routing(text, reply_to_message_id=None, running_jobs=[pr_job])
        assert dec.mode == "steer", text
        assert dec.target_job_id == pr_job["job_id"]


def test_router_hybrid_intent_classification_single_and_multi_read_turn():
    order_job = _running_job("aaaa111100000000", "visa order 1001")

    # Explicit new-task phrases
    for phrase in (
        "ny uppgift kolla läget",
        "ny task sammanfatta",
        "parallellt kolla lagret",
        "samtidigt visa ärenden",
        "medan du jobbar kolla ga4",
        "kolla även fraktpriser",
        "kolla också status",
    ):
        dec = decide_message_routing(phrase, reply_to_message_id=None, running_jobs=[order_job])
        assert dec.mode == "new_task", phrase

    # Short confirmations / style / focus steer
    for steer_text in (
        "ja",
        "nej",
        "ok",
        "kortare",
        "på engelska",
        "på norska",
        "mer detaljer",
        "och frakten?",
        "samma order",
        "fokusera på leveransdatum",
        "ta med kundens adress",
        "strunta i fakturan",
    ):
        dec = decide_message_routing(
            steer_text, reply_to_message_id=None, running_jobs=[order_job]
        )
        assert dec.mode == "steer", steer_text
        assert dec.target_job_id == order_job["job_id"]

    # Different order id -> new_task
    diff_order = decide_message_routing(
        "kolla order 2002", reply_to_message_id=None, running_jobs=[order_job]
    )
    assert diff_order.mode == "new_task"

    # Same order id -> steer
    same_order = decide_message_routing(
        "hur ser leveransen ut för 1001?", reply_to_message_id=None, running_jobs=[order_job]
    )
    assert same_order.mode == "steer"

    # Different domains -> new_task
    for msg in (
        "hur går Google Ads och GA4?",
        "visa öppna ärenden",
        "kör wordpress backup på staging",
        "kolla produktens pris och lager",
    ):
        dec = decide_message_routing(msg, reply_to_message_id=None, running_jobs=[order_job])
        assert dec.mode == "new_task", msg

    # Standalone domain when running job has no domain
    hej_job = _running_job("hhhh111100000000", "hej")
    assert (
        decide_message_routing("ja", reply_to_message_id=None, running_jobs=[hej_job]).mode
        == "steer"
    )
    assert (
        decide_message_routing(
            "kolla ga4 trafik", reply_to_message_id=None, running_jobs=[hej_job]
        ).mode
        == "new_task"
    )

    # 2+ running read_turns without reply: only steer on unique order/case match, never random
    case_job = _running_job("cccc222200000000", "visa ärende ab12cd34")
    two_running = [order_job, case_job]

    assert (
        decide_message_routing("kortare", reply_to_message_id=None, running_jobs=two_running).mode
        == "new_task"
    )
    match_order = decide_message_routing(
        "och spårning för 1001?", reply_to_message_id=None, running_jobs=two_running
    )
    assert match_order.mode == "steer"
    assert match_order.target_job_id == order_job["job_id"]

    match_case = decide_message_routing(
        "kortare svar på ab12cd34", reply_to_message_id=None, running_jobs=two_running
    )
    assert match_case.mode == "steer"
    assert match_case.target_job_id == case_job["job_id"]


# ---------------------------------------------------------------------------
# 2. TelegramJobStore migration, parallel claiming, routing, locks, proposals
# ---------------------------------------------------------------------------


def test_job_store_constants():
    assert MAX_STEERS_PER_JOB == 3
    assert MAX_QUEUED_PER_CHAT == 12
    assert MAX_CONCURRENT_TASKS_PER_CHAT == 3


def test_job_store_migrates_legacy_schema(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE telegram_inbox (
            update_id INTEGER PRIMARY KEY,
            payload_json TEXT,
            state TEXT NOT NULL DEFAULT 'accepted',
            created_at REAL NOT NULL
        );
        CREATE TABLE bot_jobs (
            job_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            source_update_id INTEGER UNIQUE,
            callback_query_id TEXT UNIQUE,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            side_effect_started INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_until REAL,
            status_message_id INTEGER,
            answer_message_ids_json TEXT NOT NULL DEFAULT '[]',
            result_json TEXT,
            failure_class TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX bot_jobs_one_running_per_conversation
            ON bot_jobs(conversation_id) WHERE state='running';
        INSERT INTO bot_jobs(
            job_id, conversation_id, chat_id, kind, payload_json, state, sequence,
            created_at, updated_at
        ) VALUES (
            'legacyjob001', 'telegram:42', '42', 'read_turn', '{"text":"hej"}',
            'completed', 1, 100.0, 100.0
        );
        """
    )
    conn.commit()
    conn.close()

    store = TelegramJobStore(db_path)
    migrated = store.get_job("legacyjob001")
    assert migrated is not None
    assert migrated["task_id"] == "legacyjob001"
    assert migrated["delivery_attempts"] == 0
    assert migrated["next_delivery_at"] == 0

    with sqlite3.connect(db_path) as check:
        indexes = {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        tables = {
            row[0]
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "bot_jobs_one_running_per_conversation" not in indexes
    assert "bot_jobs_one_running_per_task" in indexes
    assert "bot_proposals" in tables
    assert "bot_resource_locks" in tables


def test_parallel_read_jobs_up_to_limit_and_action_serialization(tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")

    a1 = store.accept_update(_msg_update(1, 5, "visa order 1001", message_id=10))
    assert a1["disposition"] == "queued"
    assert a1["parallel"] is False
    assert a1["source_message_id"] == 10

    j1 = store.claim_next()
    assert j1 is not None

    a2 = store.accept_update(_msg_update(2, 5, "kolla order 2002", message_id=11))
    assert a2["disposition"] == "queued"
    assert a2["parallel"] is True
    assert a2["source_message_id"] == 11

    j2 = store.claim_next()
    assert j2 is not None
    assert j2["task_id"] != j1["task_id"]

    a3 = store.accept_update(_msg_update(3, 5, "/task kolla ga4 trafik", message_id=12))
    assert a3["disposition"] == "queued"
    assert a3["parallel"] is True

    j3 = store.claim_next()
    assert j3 is not None
    assert j3["payload"]["text"] == "kolla ga4 trafik"

    # 4th queued job in same chat cannot be claimed while 3 are running
    a4 = store.accept_update(_msg_update(4, 5, "/cases list", message_id=13))
    assert a4["disposition"] == "queued"
    assert store.claim_next() is None

    running = store.active_jobs(5)
    assert len(running) == MAX_CONCURRENT_TASKS_PER_CHAT
    assert store.active_job(5) is not None

    # Ambiguous cancel without job_id when multiple jobs are running
    assert store.request_cancel(5) == "ambiguous_running"
    assert store.request_cancel(5, j2["job_id"][:8]) == "requested"
    assert store.cancel_requested(j2["job_id"]) is True
    assert store.cancel_requested(j1["job_id"]) is False

    # Another chat can still claim immediately
    store.accept_update(_msg_update(5, 99, "hej", message_id=20))
    other = store.claim_next()
    assert other is not None and other["chat_id"] == "99"

    # Complete all in chat 5 and test action serialization
    for j in (j1, j2, j3):
        store.complete_job(j["job_id"], {"text": "ok"})
    j4 = store.claim_next()
    assert j4 is not None
    store.complete_job(j4["job_id"], {"text": "ok"})

    # Two actions + one read job: only 1 action runs at a time, read runs alongside it
    act1 = store.accept_update(_msg_update(6, 5, "/cases approve ab12cd34"))
    act2 = store.accept_update(_msg_update(7, 5, "/cases close ab12cd34"))
    rd1 = store.accept_update(_msg_update(8, 5, "/order 1001"))
    assert act1["disposition"] == "queued"
    assert act2["disposition"] == "queued"
    assert rd1["disposition"] == "queued"

    c1 = store.claim_next()
    c2 = store.claim_next()
    c3 = store.claim_next()
    assert c1 is not None and c1["kind"] == "action"
    assert c2 is not None and c2["kind"] == "read_turn"
    assert c3 is None  # second action blocked while first action is running


def test_same_task_id_cannot_run_twice_and_promoted_steer_inherits_task_id(tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    store.accept_update(_msg_update(1, 5, "visa order 1001", message_id=10))
    j1 = store.claim_next()
    assert j1 is not None
    store.save_message_ids(j1["job_id"], status_message_id=50)

    # Steer via reply to status_message_id
    steered = store.accept_update(
        _msg_update(2, 5, "och frakten?", message_id=11, reply_to_message_id=50)
    )
    assert steered["disposition"] == "steer"
    assert steered["job_id"] == j1["job_id"]
    assert steered["source_message_id"] == 11

    # Complete j1 without consuming steer -> promoted job inherits j1's task_id
    promoted = store.complete_job(j1["job_id"], {"text": "klar"})
    assert len(promoted) == 1
    assert promoted[0]["task_id"] == j1["task_id"]

    j_next = store.claim_next()
    assert j_next is not None
    assert j_next["task_id"] == j1["task_id"]
    assert j_next["payload"]["text"] == "och frakten?"

    # Insert another queued job with the exact same task_id: must NOT claim while j_next runs
    with store._connection() as conn:
        conn.execute(
            "INSERT INTO bot_jobs(job_id,task_id,conversation_id,chat_id,kind,payload_json,"
            "state,sequence,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',99,1.0,1.0)",
            ("same_task_job_2", j1["task_id"], "telegram:5", "5", "read_turn", '{"text":"x"}'),
        )
    assert store.claim_next() is None


def test_proposals_callbacks_and_purge(tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    p1 = store.create_proposal(
        5,
        kind="order_status",
        payload={"order_id": "1001", "status": "completed"},
        task_id="task-1",
        domain="se",
        site_id="azom-se",
    )
    pid = p1["proposal_id"]
    assert pid.startswith("pr-")
    assert len(pid) == 15
    assert p1["state"] == "open"
    assert store.get_proposal(pid, chat_id=5)["payload"]["order_id"] == "1001"
    assert store.get_proposal(pid, chat_id=999) is None

    # Callback proposal:yes:pr-... is action, sets proposal_id, does NOT require approval_snapshot
    cb = store.accept_update(_cb_update(10, 5, f"proposal:yes:{pid}", message_id=700))
    assert cb["disposition"] == "queued"
    job = store.get_job(cb["job_id"])
    assert job is not None
    assert job["kind"] == "action"
    assert job["payload"]["proposal_id"] == pid
    assert "approval_snapshot_required" not in job["payload"]

    # Double-click on same proposal callback deduplicates
    dup = store.accept_update(_cb_update(11, 5, f"proposal:yes:{pid}", message_id=700))
    assert dup["disposition"] == "duplicate_action"

    # Single-use consume
    consumed = store.consume_proposal(pid, chat_id=5)
    assert consumed is not None and consumed["state"] == "consumed"
    assert store.consume_proposal(pid, chat_id=5) is None
    assert store.cancel_proposal(pid, chat_id=5) is False

    # Cancel open proposal
    p2 = store.create_proposal(5, kind="case_reply", payload={"case_id": "abc"})
    assert store.cancel_proposal(p2["proposal_id"], chat_id=5) is True
    assert store.consume_proposal(p2["proposal_id"], chat_id=5) is None

    # Open proposal survives purge; old non-open proposals are purged
    p_open = store.create_proposal(5, kind="case_reply", payload={"case_id": "open"})
    old_ts = time.time() - 10_000
    with store._connection() as conn:
        conn.execute(
            "UPDATE bot_proposals SET created_at=?, consumed_at=? WHERE proposal_id IN (?,?)",
            (old_ts, old_ts, pid, p2["proposal_id"]),
        )
        conn.execute(
            "UPDATE bot_proposals SET created_at=? WHERE proposal_id=?",
            (old_ts, p_open["proposal_id"]),
        )
    store.purge(retention_seconds=60)
    assert store.get_proposal(pid) is None
    assert store.get_proposal(p2["proposal_id"]) is None
    assert store.get_proposal(p_open["proposal_id"]) is not None


def test_resource_locks_and_stale_cleanup(tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    store.accept_update(_msg_update(1, 5, "visa order 1001"))
    store.accept_update(_msg_update(2, 5, "kolla order 2002"))
    j1 = store.claim_next()
    j2 = store.claim_next()
    assert j1 is not None and j2 is not None

    assert store.acquire_resource_lock("wp:staging", job_id=j1["job_id"], ttl_seconds=60) is True
    # Re-entrant renew succeeds for same job
    assert store.acquire_resource_lock("wp:staging", job_id=j1["job_id"], ttl_seconds=60) is True
    # Blocked for another job
    assert store.acquire_resource_lock("wp:staging", job_id=j2["job_id"], ttl_seconds=60) is False

    # Wrong job cannot release j1's lock
    assert store.release_resource_lock("wp:staging", job_id=j2["job_id"]) is False
    assert store.release_resource_lock("wp:staging", job_id=j1["job_id"]) is True
    assert store.acquire_resource_lock("wp:staging", job_id=j2["job_id"], ttl_seconds=60) is True

    # Expired lock is automatically cleaned up on next acquire
    with store._connection() as conn:
        conn.execute(
            "UPDATE bot_resource_locks SET expires_at=? WHERE resource_key='wp:staging'",
            (time.time() - 5,),
        )
    assert store.acquire_resource_lock("wp:staging", job_id=j1["job_id"], ttl_seconds=60) is True

    # Completing j1 releases its lock and allows j2 to acquire immediately
    store.complete_job(j1["job_id"], {"text": "done"})
    assert store.acquire_resource_lock("wp:staging", job_id=j2["job_id"], ttl_seconds=60) is True

    # Purge removes expired locks
    with store._connection() as conn:
        conn.execute(
            "UPDATE bot_resource_locks SET expires_at=? WHERE resource_key='wp:staging'",
            (time.time() - 1,),
        )
    store.purge(retention_seconds=60)
    with store._connection() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM bot_resource_locks").fetchone()[0]
    assert remaining == 0


def test_delivery_retry_backoff_and_max_attempts(tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    store.accept_update(_msg_update(1, 5, "hej"))
    j1 = store.claim_next()
    assert j1 is not None
    store.complete_job(j1["job_id"], {"text": "svar"})

    # Initially ready for delivery
    ready = store.undelivered_results()
    assert len(ready) == 1 and ready[0]["job_id"] == j1["job_id"]

    # First failed attempt schedules backoff
    status1 = store.record_delivery_attempt(j1["job_id"], backoff_seconds=30.0, max_attempts=3)
    assert status1 == "retry"
    assert store.undelivered_results() == []

    # Fast-forward next_delivery_at -> visible again
    with store._connection() as conn:
        conn.execute(
            "UPDATE bot_jobs SET next_delivery_at=? WHERE job_id=?",
            (time.time() - 1, j1["job_id"]),
        )
    assert len(store.undelivered_results()) == 1

    # Second attempt -> retry, third attempt -> failed
    assert store.record_delivery_attempt(j1["job_id"], backoff_seconds=1.0, max_attempts=3) == "retry"
    assert store.record_delivery_attempt(j1["job_id"], backoff_seconds=1.0, max_attempts=3) == "failed"
    finished = store.get_job(j1["job_id"])
    assert finished is not None
    assert finished["delivery_state"] == "failed"
    assert finished["delivery_attempts"] == 3
    assert store.undelivered_results() == []


# ---------------------------------------------------------------------------
# 3. ConversationStore & TelegramConversationStore atomic turn updates
# ---------------------------------------------------------------------------


def _assert_atomic_store_behavior(conv_store) -> None:
    # Seed state with an active pending_action flow
    conv_store.set(
        "5",
        {
            "flow": "pending_action",
            "step": "confirm",
            "slots": {"pending": {"kind": "order_status", "order_id": "1001"}},
            "session": {"model": "gpt-test", "temp_key": "remove_me"},
            "messages": [{"role": "user", "content": "seed"}],
            "tool_digest": "old_digest",
        },
    )

    # Parallel read task appends turn WITHOUT flow_updates -> flow/step/slots untouched!
    res = conv_store.append_turn_atomic(
        "5",
        new_messages=[
            {"role": "user", "content": "kolla ga4"},
            {"role": "assistant", "content": "GA4 rapport"},
        ],
        session_updates={"last_market": "se"},
        pop_session_keys=("temp_key",),
        tool_digest="new_digest",
    )
    assert res["flow"] == "pending_action"
    assert res["step"] == "confirm"
    assert res["slots"] == {"pending": {"kind": "order_status", "order_id": "1001"}}
    assert res["session"] == {"model": "gpt-test", "last_market": "se"}
    assert res["tool_digest"] == "new_digest"
    assert len(res["messages"]) == 3

    # clear_tool_digest works while still preserving flow
    res2 = conv_store.append_turn_atomic("5", clear_tool_digest=True)
    assert res2["tool_digest"] == ""
    assert res2["flow"] == "pending_action"

    # Explicit flow_updates modifies flow/step/slots
    res3 = conv_store.append_turn_atomic(
        "5",
        flow_updates={"flow": None, "step": None, "slots": {}},
    )
    assert res3["flow"] is None
    assert res3["step"] is None
    assert res3["slots"] == {}

    # Re-seed pending_action and hammer concurrently from multiple threads
    conv_store.append_turn_atomic(
        "5",
        flow_updates={
            "flow": "pending_action",
            "step": "confirm",
            "slots": {"pending": {"kind": "case_reply"}},
        },
    )
    errors: list[Exception] = []

    def worker(idx: int) -> None:
        try:
            conv_store.append_turn_atomic(
                "5",
                new_messages=[{"role": "user", "content": f"t-{idx}"}],
                session_updates={f"k_{idx}": idx},
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    final = conv_store.get("5")
    assert final is not None
    assert final["flow"] == "pending_action"
    assert final["slots"] == {"pending": {"kind": "case_reply"}}
    contents = {m["content"] for m in final["messages"]}
    for i in range(10):
        assert f"t-{i}" in contents
        assert final["session"][f"k_{i}"] == i


def test_conversation_store_append_turn_atomic(tmp_path: Path):
    store = ConversationStore(path=tmp_path / "state.json")
    _assert_atomic_store_behavior(store)


def test_telegram_conversation_store_append_turn_atomic(tmp_path: Path):
    store = TelegramConversationStore(database_path=tmp_path / "jobs.sqlite3")
    _assert_atomic_store_behavior(store)
