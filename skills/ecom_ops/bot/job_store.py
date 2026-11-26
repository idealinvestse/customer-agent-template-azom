"""Durable Telegram inbox, background jobs, and safe steer mailbox."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ecom_ops.bot.actors import channel_peer_allowed, resolve_channel_actor
from ecom_ops.bot.progress_events import sanitize_status_fragment
from ecom_ops.bot.steer_router import decide_message_routing
from ecom_ops.bot.telegram_oauth import is_ephemeral_oauth_message
from ecom_ops.wp_ops.page_review_intent import is_page_review

MAX_STEERS_PER_JOB = 3
MAX_QUEUED_PER_CHAT = 12
MAX_CONCURRENT_TASKS_PER_CHAT = 3
JOB_LEASE_SECONDS = 120
JOB_RETENTION_SECONDS = 7 * 24 * 60 * 60

_CONTROL = re.compile(r"^/(status|jobs|stop|cancel|reconcile)(?:@\w+)?(?:\s+(.*))?$", re.I)
_STEER_RES_CMD = re.compile(r"^/steer(?:@\w+)?\s+(res-[0-9a-f]{4,12})(?:\s+([\s\S]*))?$", re.I)
_RESEARCH_ACTIVE_STATUSES = (
    "queued",
    "planning",
    "searching",
    "fetching",
    "evaluating",
    "synthesizing",
    "running",
)
_ACTION_COMMAND = re.compile(
    r"^/cases\s+(?:approve|send|close|regenerate|regen|redraft)\b", re.I
)
_ACTION_CALLBACK = re.compile(
    r"^(?:cases:approve:|cases:regen:|order:set:|product:desc:|action:yes$|"
    r"escalate:yes$|wpfile:stage:fe-[a-f0-9]{12}$|"
    r"wpreview:(?:stage|push_draft):wr-[a-f0-9]{12}$|proposal:yes:pr-[0-9a-f]{12}$)",
    re.I,
)
_PROPOSAL_YES_CALLBACK = re.compile(r"^proposal:yes:(pr-[0-9a-f]{12})$", re.I)


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def research_jobs_summary_for_peer(
    channel: str,
    peer_id: str | int,
    actor: str,
    *,
    data_dir: Path | str | None = None,
    active_only: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Query ``ResearchStore`` safely for research jobs belonging to ``(channel, peer_id)``."""
    base = Path(data_dir) if data_dir is not None else Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    db_path = base / "research_jobs.sqlite3"
    if not db_path.is_file():
        return []
    ch = str(channel or "").strip().lower()
    pid = str(peer_id or "").strip()
    if not ch or not pid:
        return []
    try:
        from ecom_ops.rbac import AccessDenied
        from ecom_ops.research.store import ResearchStore

        rstore = ResearchStore(
            path=db_path,
            artifacts_dir=base / "research_artifacts",
        )
        jobs = rstore.list_jobs(actor=actor, limit=50)
    except (AccessDenied, sqlite3.Error, OSError, ValueError):
        return []
    except Exception:
        return []

    chat_ref = f"chat:{ch}:{pid}"
    lim = max(1, min(int(limit), 50))
    out: list[dict[str, Any]] = []
    for job in jobs:
        if not ((job.channel.lower() == ch and job.peer_id == pid) or job.target_ref == chat_ref):
            continue
        if active_only and job.status not in _RESEARCH_ACTIVE_STATUSES:
            continue
        topic_raw = job.spec.topic if job.spec is not None else ""
        profile_id = job.spec.profile_id if job.spec is not None else "deep_general"
        depth = job.spec.depth if job.spec is not None else "standard"
        out.append(
            {
                "research_id": job.research_id,
                "job_id": job.research_id,
                "task_id": job.research_id,
                "kind": "research",
                "status": job.status,
                "state": job.status,
                "profile_id": profile_id,
                "depth": depth,
                "topic": sanitize_status_fragment(topic_raw, max_len=72),
                "channel": job.channel,
                "peer_id": job.peer_id,
                "status_message_id": job.status_message_id,
                "current_round": job.current_round,
                "queries_used": job.queries_used,
                "fetches_used": job.fetches_used,
                "evidence_status": str(job.evidence_status) if job.evidence_status else None,
                "delivery_state": job.delivery_state,
                "cancel_requested": job.cancel_requested,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "completed_at": job.completed_at,
            }
        )
        if len(out) >= lim:
            break
    return out


def _active_research_jobs_for_steer(data_dir: Path, chat_id: str) -> list[dict[str, Any]]:
    """Return active Telegram research jobs formatted for ``decide_message_routing``."""
    db_path = data_dir / "research_jobs.sqlite3"
    if not db_path.is_file():
        return []
    marks = ",".join("?" for _ in _RESEARCH_ACTIVE_STATUSES)
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            rows = conn.execute(
                f"SELECT j.id, j.topic, j.status_message_id, "
                "(SELECT COUNT(*) FROM research_steers s "
                " WHERE s.job_id=j.id AND s.consumed_at IS NOT NULL) AS steer_used, "
                "(SELECT COUNT(*) FROM research_steers s "
                " WHERE s.job_id=j.id AND s.consumed_at IS NULL) AS steer_pending "
                f"FROM research_jobs j WHERE j.channel='telegram' AND j.peer_id=? "
                f"AND j.status IN ({marks}) ORDER BY j.created_at ASC LIMIT 20",
                (str(chat_id), *_RESEARCH_ACTIVE_STATUSES),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        raw_mid = r["status_message_id"]
        status_mid: int | None = None
        if isinstance(raw_mid, int) and not isinstance(raw_mid, bool):
            status_mid = raw_mid
        elif isinstance(raw_mid, str) and raw_mid.strip().isdigit():
            status_mid = int(raw_mid.strip())
        jid = str(r["id"])
        out.append(
            {
                "job_id": jid,
                "task_id": jid,
                "research_id": jid,
                "kind": "research",
                "payload": {"text": str(r["topic"] or "")},
                "status_message_id": status_mid,
                "answer_message_ids": [],
                "steer_used": int(r["steer_used"] or 0),
                "steer_pending": int(r["steer_pending"] or 0),
            }
        )
    return out


class TelegramJobStore:
    """SQLite state store for one Telegram polling process and its workers."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
            path = base / "telegram_jobs.sqlite3"
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.owner = f"{os.getpid()}:{uuid.uuid4().hex}"
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _chmod_db_files(self) -> None:
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError:
                pass

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_inbox (
                    update_id INTEGER PRIMARY KEY,
                    payload_json TEXT,
                    state TEXT NOT NULL DEFAULT 'accepted',
                    disposition TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_jobs (
                    job_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    conversation_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    source_update_id INTEGER UNIQUE,
                    callback_query_id TEXT UNIQUE,
                    approval_key TEXT,
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
                    reconciliation_notified_at REAL,
                    reconciliation_outcome TEXT,
                    reconciled_by TEXT,
                    reconciled_at REAL,
                    delivery_state TEXT NOT NULL DEFAULT 'pending',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    next_delivery_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(source_update_id) REFERENCES telegram_inbox(update_id)
                        ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS bot_jobs_queue_order
                    ON bot_jobs(state, sequence, created_at);
                CREATE INDEX IF NOT EXISTS bot_jobs_conversation
                    ON bot_jobs(conversation_id, state, sequence);
                CREATE TABLE IF NOT EXISTS bot_job_steers (
                    steer_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES bot_jobs(job_id) ON DELETE CASCADE,
                    source_update_id INTEGER NOT NULL UNIQUE
                        REFERENCES telegram_inbox(update_id) ON DELETE CASCADE,
                    text TEXT NOT NULL,
                    consumed_at REAL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_job_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES bot_jobs(job_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS bot_job_events_job
                    ON bot_job_events(job_id, event_id);
                CREATE TABLE IF NOT EXISTS telegram_conversations (
                    chat_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_poller_lease (
                    lease_name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    domain TEXT,
                    site_id TEXT,
                    state TEXT NOT NULL DEFAULT 'open',
                    created_at REAL NOT NULL,
                    consumed_at REAL,
                    expires_at REAL,
                    callback_key TEXT
                );
                CREATE INDEX IF NOT EXISTS bot_proposals_chat_state
                    ON bot_proposals(chat_id, state);
                CREATE TABLE IF NOT EXISTS wp_review_pins (
                    chat_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    base_sha256 TEXT NOT NULL,
                    work_sha256 TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, plan_id)
                );
                CREATE TABLE IF NOT EXISTS bot_resource_locks (
                    resource_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )
            inbox_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(telegram_inbox)")
            }
            if "disposition" not in inbox_columns:
                conn.execute("ALTER TABLE telegram_inbox ADD COLUMN disposition TEXT")
            job_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(bot_jobs)")
            }
            if "approval_key" not in job_columns:
                conn.execute("ALTER TABLE bot_jobs ADD COLUMN approval_key TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS bot_jobs_approval_key_unique "
                "ON bot_jobs(chat_id,approval_key) WHERE approval_key IS NOT NULL"
            )
            if "reconciliation_notified_at" not in job_columns:
                conn.execute(
                    "ALTER TABLE bot_jobs ADD COLUMN reconciliation_notified_at REAL"
                )
            for column in ("reconciliation_outcome", "reconciled_by"):
                if column not in job_columns:
                    conn.execute(f"ALTER TABLE bot_jobs ADD COLUMN {column} TEXT")
            if "reconciled_at" not in job_columns:
                conn.execute("ALTER TABLE bot_jobs ADD COLUMN reconciled_at REAL")
            if "task_id" not in job_columns:
                conn.execute("ALTER TABLE bot_jobs ADD COLUMN task_id TEXT")
            if "delivery_attempts" not in job_columns:
                conn.execute(
                    "ALTER TABLE bot_jobs ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "next_delivery_at" not in job_columns:
                conn.execute(
                    "ALTER TABLE bot_jobs ADD COLUMN next_delivery_at REAL NOT NULL DEFAULT 0"
                )
            conn.execute("UPDATE bot_jobs SET task_id=job_id WHERE task_id IS NULL")
            conn.execute("DROP INDEX IF EXISTS bot_jobs_one_running_per_conversation")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS bot_jobs_one_running_per_task "
                "ON bot_jobs(conversation_id, task_id) WHERE state='running'"
            )
            prop_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(bot_proposals)")
            }
            if "expires_at" not in prop_columns:
                conn.execute("ALTER TABLE bot_proposals ADD COLUMN expires_at REAL")
            if "callback_key" not in prop_columns:
                conn.execute("ALTER TABLE bot_proposals ADD COLUMN callback_key TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS bot_proposals_chat_cb "
                "ON bot_proposals(chat_id, callback_key, state)"
            )
        self._chmod_db_files()

    def _begin(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    def update_offset(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM bot_meta WHERE key='telegram_update_offset'"
            ).fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def _parse_update(
        update: dict[str, Any],
        *,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any] | None:
        update_id = update.get("update_id")
        try:
            update_id = int(update_id)
        except (TypeError, ValueError):
            return None
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            callback_id = callback.get("id")
            data = str(callback.get("data") or "")[:256]
            if chat_id is None or not callback_id:
                return {"update_id": update_id, "kind": "ignored"}
            return {
                "update_id": update_id,
                "kind": "callback",
                "chat_id": str(chat_id),
                "callback_query_id": str(callback_id)[:128],
                "source_message_id": message.get("message_id"),
                "data": data,
            }
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = message.get("text")
        raw_images = (
            image_paths
            if image_paths is not None
            else message.get("_azom_image_paths")
        )
        norm_images = (
            [str(p).strip() for p in raw_images if isinstance(p, str) and str(p).strip()][:4]
            if isinstance(raw_images, (list, tuple))
            else []
        )
        if chat_id is None:
            return {"update_id": update_id, "kind": "ignored"}
        if isinstance(text, str):
            effective_text = text[:8000]
        elif norm_images:
            caption = message.get("caption")
            effective_text = caption[:8000] if isinstance(caption, str) else ""
        else:
            return {"update_id": update_id, "kind": "ignored"}
        reply_to = message.get("reply_to_message")
        reply_to_id = reply_to.get("message_id") if isinstance(reply_to, dict) else None
        parsed_msg: dict[str, Any] = {
            "update_id": update_id,
            "kind": "message",
            "chat_id": str(chat_id),
            "text": effective_text,
            "message_id": message.get("message_id"),
            "reply_to_message_id": reply_to_id,
        }
        if norm_images:
            parsed_msg["image_paths"] = norm_images
        return parsed_msg

    _VOLATILE_APPROVAL_SESSION_KEYS = frozenset(
        {
            "last_order_id",
            "last_case_id8",
            "order_list_shown",
            "last_product_id",
            "last_page_id",
            "last_post_id",
            "pending_image_paths",
            "clarify_proposals",
            "active_wp_review",
            "last_research_id",
            "recent_research_ids",
        }
    )

    @staticmethod
    def approval_snapshot(state: dict[str, Any] | None) -> str:
        value = state if isinstance(state, dict) else {}
        raw_session = value.get("session")
        session = (
            {
                k: v
                for k, v in raw_session.items()
                if k not in TelegramJobStore._VOLATILE_APPROVAL_SESSION_KEYS
            }
            if isinstance(raw_session, dict)
            else {}
        )
        snapshot = {
            "flow": value.get("flow"),
            "step": value.get("step"),
            "slots": value.get("slots") or {},
            "session": session,
        }
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _approval_key(chat_id: str, payload: dict[str, Any]) -> str:
        identity = {
            "chat_id": chat_id,
            "type": payload.get("type"),
            "data": payload.get("data"),
            "text": payload.get("text"),
            "source_message_id": payload.get("source_message_id"),
            "approval_snapshot": payload.get("approval_snapshot"),
        }
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def acknowledge_ephemeral_update(self, update_id: int) -> bool:
        """Record an ephemeral OAuth update_id and advance offset without storing message text."""
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            existing = conn.execute(
                "SELECT 1 FROM telegram_inbox WHERE update_id=? LIMIT 1",
                (int(update_id),),
            ).fetchone()
            if existing:
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO telegram_inbox(update_id,payload_json,state,disposition,created_at) "
                "VALUES(?,?,?,?,?)",
                (int(update_id), '{"kind":"ephemeral_oauth"}', "done", "ephemeral", now),
            )
            row = conn.execute(
                "SELECT value FROM bot_meta WHERE key='telegram_update_offset'"
            ).fetchone()
            current = int(row["value"]) if row else 0
            next_offset = max(current, int(update_id) + 1)
            conn.execute(
                "INSERT INTO bot_meta(key,value) VALUES('telegram_update_offset',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(next_offset),),
            )
            conn.commit()
            return True

    def accept_update(
        self,
        update: dict[str, Any],
        *,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Durably store the update and advance the Telegram cursor atomically."""
        parsed = self._parse_update(update, image_paths=image_paths)
        if not parsed:
            return {"disposition": "ignored"}
        if parsed["kind"] == "message" and is_ephemeral_oauth_message(str(parsed.get("text") or "")):
            # Defense-in-depth: never write OAuth authorization codes or states into SQLite.
            parsed["text"] = "/oauth callback"
        update_id = parsed["update_id"]
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            existing = conn.execute(
                "SELECT inbox.disposition AS disposition, jobs.job_id AS job_id "
                "FROM telegram_inbox AS inbox "
                "LEFT JOIN bot_jobs AS jobs ON jobs.source_update_id=inbox.update_id "
                "WHERE inbox.update_id=?",
                (update_id,),
            ).fetchone()
            if existing:
                pending_control = None
                if existing["disposition"] == "control":
                    control_row = conn.execute(
                        "SELECT payload_json FROM telegram_inbox WHERE update_id=?",
                        (update_id,),
                    ).fetchone()
                    pending_control = _decode_json(
                        control_row["payload_json"] if control_row else None, None
                    )
                conn.commit()
                return {
                    "disposition": str(existing["disposition"] or "duplicate"),
                    "job_id": existing["job_id"],
                    "control": pending_control,
                    "duplicate": True,
                }

            # Retain only the bounded fields required to resume an accepted update.
            conn.execute(
                "INSERT INTO telegram_inbox(update_id,payload_json,state,created_at) "
                "VALUES(?,?,?,?)",
                (update_id, _json(parsed), "accepted", now),
            )
            row = conn.execute(
                "SELECT value FROM bot_meta WHERE key='telegram_update_offset'"
            ).fetchone()
            current = int(row["value"]) if row else 0
            next_offset = max(current, update_id + 1)
            conn.execute(
                "INSERT INTO bot_meta(key,value) VALUES('telegram_update_offset',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(next_offset),),
            )

            if parsed["kind"] == "ignored":
                conn.execute(
                    "UPDATE telegram_inbox SET state='ignored', disposition='ignored' "
                    "WHERE update_id=?",
                    (update_id,),
                )
                conn.commit()
                return {"disposition": "ignored", "update_id": update_id}

            chat_id = parsed["chat_id"]
            conversation_id = f"telegram:{chat_id}"
            parallel = False
            source_message_id: int | None = None
            task_id: str | None = None

            if parsed["kind"] == "message":
                text = parsed["text"]
                msg_image_paths = list(parsed.get("image_paths") or [])
                source_message_id = parsed.get("message_id")
                control = None if msg_image_paths else _CONTROL.fullmatch(text.strip())
                if control:
                    action = control.group(1).lower()
                    args = (control.group(2) or "").strip()
                    control_payload = {
                        "action": action,
                        "args": args,
                        "chat_id": chat_id,
                        "update_id": update_id,
                    }
                    conn.execute(
                        "UPDATE telegram_inbox SET state='control',disposition='control',"
                        "payload_json=? WHERE update_id=?",
                        (_json(control_payload), update_id),
                    )
                    conn.commit()
                    return {"disposition": "control", "control": control_payload}

                running_rows = conn.execute(
                    "SELECT j.job_id, j.task_id, j.kind, j.payload_json, "
                    "j.status_message_id, j.answer_message_ids_json, "
                    "(SELECT COUNT(*) FROM bot_job_steers s "
                    " WHERE s.job_id=j.job_id AND s.consumed_at IS NOT NULL) AS steer_used, "
                    "(SELECT COUNT(*) FROM bot_job_steers s "
                    " WHERE s.job_id=j.job_id AND s.consumed_at IS NULL) AS steer_pending "
                    "FROM bot_jobs j WHERE j.conversation_id=? AND j.state='running' "
                    "ORDER BY j.started_at ASC, j.sequence ASC",
                    (conversation_id,),
                ).fetchall()
                running_jobs = [
                    {
                        "job_id": r["job_id"],
                        "task_id": r["task_id"] or r["job_id"],
                        "kind": r["kind"],
                        "payload": _decode_json(r["payload_json"], {}),
                        "status_message_id": r["status_message_id"],
                        "answer_message_ids": _decode_json(r["answer_message_ids_json"], []),
                        "steer_used": int(r["steer_used"] or 0),
                        "steer_pending": int(r["steer_pending"] or 0),
                    }
                    for r in running_rows
                ]
                parallel = bool(running_jobs)
                research_running = _active_research_jobs_for_steer(self.path.parent, chat_id)
                if msg_image_paths:
                    cleaned_text = text
                    kind = "read_turn"
                else:
                    decision = decide_message_routing(
                        text,
                        reply_to_message_id=parsed.get("reply_to_message_id"),
                        running_jobs=running_jobs + research_running,
                        max_steers_per_job=MAX_STEERS_PER_JOB,
                    )
                    if decision.mode == "steer" and decision.target_job_id:
                        if str(decision.target_job_id).startswith("res-"):
                            r_target = next(
                                (j for j in research_running if j["job_id"] == decision.target_job_id),
                                None,
                            )
                            steer_ok = False
                            if (
                                r_target is not None
                                and (r_target["steer_used"] + r_target["steer_pending"]) < MAX_STEERS_PER_JOB
                                and channel_peer_allowed("telegram", chat_id)
                            ):
                                try:
                                    actor_name = resolve_channel_actor("telegram", chat_id)
                                    from ecom_ops.research import get_research_service

                                    get_research_service(self.path.parent).steer_research(
                                        decision.target_job_id,
                                        decision.cleaned_text,
                                        actor=actor_name,
                                    )
                                    steer_ok = True
                                except Exception:
                                    steer_ok = False
                            if steer_ok:
                                conn.execute(
                                    "UPDATE telegram_inbox SET state='steer',disposition='steer',"
                                    "payload_json=NULL WHERE update_id=?",
                                    (update_id,),
                                )
                                conn.commit()
                                return {
                                    "disposition": "steer",
                                    "job_id": decision.target_job_id,
                                    "chat_id": chat_id,
                                    "update_id": update_id,
                                    "source_message_id": source_message_id,
                                    "kind": "research",
                                }
                            control_payload = {
                                "action": "steer",
                                "args": f"{decision.target_job_id} {decision.cleaned_text}".strip(),
                                "chat_id": chat_id,
                                "update_id": update_id,
                            }
                            conn.execute(
                                "UPDATE telegram_inbox SET state='control',disposition='control',"
                                "payload_json=? WHERE update_id=?",
                                (_json(control_payload), update_id),
                            )
                            conn.commit()
                            return {"disposition": "control", "control": control_payload}

                        target = next(
                            (j for j in running_jobs if j["job_id"] == decision.target_job_id),
                            None,
                        )
                        queued = conn.execute(
                            "SELECT COUNT(*) AS n FROM bot_jobs WHERE conversation_id=? "
                            "AND state='queued'",
                            (conversation_id,),
                        ).fetchone()["n"]
                        reserved_steers = conn.execute(
                            "SELECT COUNT(*) AS n FROM bot_job_steers s JOIN bot_jobs j "
                            "ON j.job_id=s.job_id WHERE j.conversation_id=? AND j.state='running' "
                            "AND s.consumed_at IS NULL",
                            (conversation_id,),
                        ).fetchone()["n"]
                        if (
                            target is not None
                            and (target["steer_used"] + target["steer_pending"]) < MAX_STEERS_PER_JOB
                            and queued + reserved_steers + 1 <= MAX_QUEUED_PER_CHAT
                        ):
                            conn.execute(
                                "INSERT INTO bot_job_steers(job_id,source_update_id,text,created_at) "
                                "VALUES(?,?,?,?)",
                                (decision.target_job_id, update_id, decision.cleaned_text, now),
                            )
                            self._event(
                                conn,
                                decision.target_job_id,
                                "steer_queued",
                                "Följdfråga mottagen",
                            )
                            conn.execute(
                                "UPDATE telegram_inbox SET state='steer',disposition='steer',"
                                "payload_json=NULL WHERE update_id=?",
                                (update_id,),
                            )
                            conn.commit()
                            return {
                                "disposition": "steer",
                                "job_id": decision.target_job_id,
                                "chat_id": chat_id,
                                "update_id": update_id,
                                "source_message_id": source_message_id,
                            }

                    if _STEER_RES_CMD.fullmatch(text.strip()):
                        res_args = text.strip().split(None, 1)[1]
                        control_payload = {
                            "action": "steer",
                            "args": res_args,
                            "chat_id": chat_id,
                            "update_id": update_id,
                        }
                        conn.execute(
                            "UPDATE telegram_inbox SET state='control',disposition='control',"
                            "payload_json=? WHERE update_id=?",
                            (_json(control_payload), update_id),
                        )
                        conn.commit()
                        return {"disposition": "control", "control": control_payload}

                    cleaned_text = decision.cleaned_text
                    if _ACTION_COMMAND.match(cleaned_text.strip()):
                        kind = "action"
                    elif is_page_review(cleaned_text):
                        kind = "page_review"
                    else:
                        kind = "read_turn"
                task_id = uuid.uuid4().hex
                payload: dict[str, Any] = {
                    "type": "message",
                    "chat_id": chat_id,
                    "text": cleaned_text,
                    "source_message_id": source_message_id,
                    "reply_to_message_id": parsed.get("reply_to_message_id"),
                }
                if msg_image_paths:
                    payload["image_paths"] = msg_image_paths
                approval_key = self._approval_key(chat_id, payload) if kind == "action" else None
                job_id = self._insert_job(
                    conn,
                    conversation_id=conversation_id,
                    chat_id=chat_id,
                    update_id=update_id,
                    callback_id=None,
                    kind=kind,
                    payload=payload,
                    approval_key=approval_key,
                    task_id=task_id,
                    now=now,
                )
            else:
                data = parsed["data"]
                source_message_id = parsed.get("source_message_id")
                has_running = (
                    conn.execute(
                        "SELECT 1 FROM bot_jobs WHERE conversation_id=? AND state='running' LIMIT 1",
                        (conversation_id,),
                    ).fetchone()
                    is not None
                )
                parallel = has_running
                kind = "action" if _ACTION_CALLBACK.match(data) else "callback"
                payload = {
                    "type": "callback",
                    "chat_id": chat_id,
                    "data": data,
                    "callback_query_id": parsed["callback_query_id"],
                    "source_message_id": source_message_id,
                }
                proposal_match = _PROPOSAL_YES_CALLBACK.fullmatch(data)
                if proposal_match:
                    payload["proposal_id"] = proposal_match.group(1).lower()
                elif data in {"action:yes", "escalate:yes"} or data.startswith(
                    ("wpfile:stage:", "wpreview:stage:", "wpreview:push_draft:")
                ):
                    state_row = conn.execute(
                        "SELECT state_json FROM telegram_conversations WHERE chat_id=?",
                        (chat_id,),
                    ).fetchone()
                    current_state = _decode_json(
                        state_row["state_json"] if state_row else None, {}
                    )
                    payload["approval_snapshot_required"] = True
                    payload["approval_snapshot"] = self.approval_snapshot(current_state)
                approval_key = self._approval_key(chat_id, payload) if kind == "action" else None
                if approval_key:
                    duplicate = conn.execute(
                        "SELECT job_id FROM bot_jobs WHERE chat_id=? AND approval_key=? LIMIT 1",
                        (chat_id, approval_key),
                    ).fetchone()
                    if duplicate:
                        conn.execute(
                            "UPDATE telegram_inbox SET state='done',disposition='duplicate_action',"
                            "payload_json=NULL WHERE update_id=?",
                            (update_id,),
                        )
                        conn.commit()
                        return {
                            "disposition": "duplicate_action",
                            "job_id": duplicate["job_id"],
                            "chat_id": chat_id,
                            "update_id": update_id,
                        }
                task_id = uuid.uuid4().hex
                job_id = self._insert_job(
                    conn,
                    conversation_id=conversation_id,
                    chat_id=chat_id,
                    update_id=update_id,
                    callback_id=parsed["callback_query_id"],
                    kind=kind,
                    payload=payload,
                    approval_key=approval_key,
                    task_id=task_id,
                    now=now,
                )

            queued = conn.execute(
                "SELECT COUNT(*) AS n FROM bot_jobs WHERE conversation_id=? "
                "AND state='queued'",
                (conversation_id,),
            ).fetchone()["n"]
            reserved_steers = conn.execute(
                "SELECT COUNT(*) AS n FROM bot_job_steers s JOIN bot_jobs j "
                "ON j.job_id=s.job_id WHERE j.conversation_id=? AND j.state='running' "
                "AND s.consumed_at IS NULL",
                (conversation_id,),
            ).fetchone()["n"]
            if queued + reserved_steers > MAX_QUEUED_PER_CHAT:
                conn.execute("DELETE FROM bot_jobs WHERE job_id=?", (job_id,))
                conn.execute(
                    "UPDATE telegram_inbox SET state='rejected',disposition='overloaded',"
                    "payload_json=NULL WHERE update_id=?",
                    (update_id,),
                )
                conn.commit()
                return {
                    "disposition": "overloaded",
                    "chat_id": chat_id,
                    "update_id": update_id,
                }
            conn.execute(
                "UPDATE telegram_inbox SET state='queued',disposition='queued',"
                "payload_json=NULL WHERE update_id=?",
                (update_id,),
            )
            conn.commit()
            return {
                "disposition": "queued",
                "job_id": job_id,
                "task_id": task_id,
                "chat_id": chat_id,
                "update_id": update_id,
                "parallel": parallel,
                "source_message_id": source_message_id,
            }

    @staticmethod
    def _insert_job(
        conn: sqlite3.Connection,
        *,
        conversation_id: str,
        chat_id: str,
        update_id: int,
        callback_id: str | None,
        kind: str,
        payload: dict[str, Any],
        approval_key: str | None,
        now: float,
        task_id: str | None = None,
    ) -> str:
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM bot_jobs "
            "WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()["n"]
        job_id = uuid.uuid4().hex
        resolved_task_id = task_id or job_id
        conn.execute(
            "INSERT INTO bot_jobs(job_id,task_id,conversation_id,chat_id,source_update_id,"
            "callback_query_id,approval_key,kind,payload_json,state,sequence,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
            (
                job_id,
                resolved_task_id,
                conversation_id,
                chat_id,
                update_id,
                callback_id,
                approval_key,
                kind,
                _json(payload),
                sequence,
                now,
                now,
            ),
        )
        TelegramJobStore._event(conn, job_id, "queued", "Uppgift köad")
        return job_id

    @staticmethod
    def _event(
        conn: sqlite3.Connection, job_id: str, event_type: str, summary: str
    ) -> None:
        conn.execute(
            "INSERT INTO bot_job_events(job_id,event_type,summary,created_at) "
            "VALUES(?,?,?,?)",
            (job_id, event_type[:40], " ".join(summary.split())[:180], _now()),
        )

    def add_event(self, job_id: str, event_type: str, summary: str) -> None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._event(conn, job_id, event_type, summary)
            conn.commit()

    @staticmethod
    def _job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = _decode_json(result.pop("payload_json", None), {})
        result["result"] = _decode_json(result.pop("result_json", None), None)
        result["answer_message_ids"] = _decode_json(
            result.pop("answer_message_ids_json", None), []
        )
        return result

    @staticmethod
    def _proposal(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = _decode_json(result.pop("payload_json", None), {})
        return result

    def get_job(self, job_id: str, *, chat_id: str | None = None) -> dict[str, Any] | None:
        with self._connection() as conn:
            if chat_id is None:
                row = conn.execute("SELECT * FROM bot_jobs WHERE job_id=?", (job_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM bot_jobs WHERE job_id=? AND chat_id=?",
                    (job_id, str(chat_id)),
                ).fetchone()
        return self._job(row)

    def claim_next(self, *, lease_seconds: int = JOB_LEASE_SECONDS) -> dict[str, Any] | None:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            row = conn.execute(
                "SELECT q.* FROM bot_jobs q WHERE q.state='queued' "
                "AND (SELECT COUNT(*) FROM bot_jobs r "
                "     WHERE r.conversation_id=q.conversation_id AND r.state='running') < ? "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM bot_jobs r "
                "    WHERE r.conversation_id=q.conversation_id AND r.state='running' "
                "    AND COALESCE(r.task_id, r.job_id)=COALESCE(q.task_id, q.job_id)"
                ") "
                "AND (q.kind!='action' OR ("
                "    NOT EXISTS ("
                "        SELECT 1 FROM bot_jobs r "
                "        WHERE r.conversation_id=q.conversation_id "
                "        AND r.state='running' AND r.kind='action'"
                "    ) AND NOT EXISTS ("
                "        SELECT 1 FROM bot_jobs r "
                "        WHERE r.conversation_id=q.conversation_id "
                "        AND r.state='needs_reconciliation'"
                "    )"
                ")) "
                "ORDER BY q.sequence,q.created_at LIMIT 1",
                (MAX_CONCURRENT_TASKS_PER_CHAT,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            changed = conn.execute(
                "UPDATE bot_jobs SET state='running',lease_owner=?,lease_until=?,"
                "started_at=COALESCE(started_at,?),updated_at=? WHERE job_id=? AND state='queued'",
                (self.owner, now + lease_seconds, now, now, row["job_id"]),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            self._event(conn, row["job_id"], "started", "Arbetet startade")
            claimed = conn.execute(
                "SELECT * FROM bot_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            conn.commit()
        return self._job(claimed)

    def renew_leases(self, *, lease_seconds: int = JOB_LEASE_SECONDS) -> None:
        now = _now()
        with self._connection() as conn:
            conn.execute(
                "UPDATE bot_jobs SET lease_until=?,updated_at=? WHERE state='running' "
                "AND lease_owner=?",
                (now + lease_seconds, now, self.owner),
            )

    def recover_after_restart(self) -> dict[str, int]:
        now = _now()
        counts = {"queued": 0, "reconciliation": 0}
        with self._connection() as conn:
            self._begin(conn)
            rows = conn.execute(
                "SELECT job_id,kind,side_effect_started FROM bot_jobs "
                "WHERE state='running' AND (lease_until IS NULL OR lease_until<?)",
                (now,),
            ).fetchall()
            for row in rows:
                if row["kind"] == "action" and row["side_effect_started"]:
                    state = "needs_reconciliation"
                    event = "Åtgärden behöver stämmas av efter omstart"
                    counts["reconciliation"] += 1
                else:
                    state = "queued"
                    event = "Jobbet återupptas efter omstart"
                    counts["queued"] += 1
                conn.execute(
                    "UPDATE bot_jobs SET state=?,lease_owner=NULL,lease_until=NULL,"
                    "updated_at=? WHERE job_id=?",
                    (state, now, row["job_id"]),
                )
                if state == "queued":
                    conn.execute(
                        "UPDATE bot_job_steers SET consumed_at=NULL WHERE job_id=?",
                        (row["job_id"],),
                    )
                self._event(conn, row["job_id"], "recovered", event)
            conn.commit()
        return counts

    def recover_expired_jobs(self) -> dict[str, int]:
        """Recover jobs whose worker lease expired without an orderly restart."""
        now = _now()
        counts = {"queued": 0, "reconciliation": 0}
        with self._connection() as conn:
            self._begin(conn)
            rows = conn.execute(
                "SELECT job_id,kind,side_effect_started FROM bot_jobs "
                "WHERE state='running' AND lease_until<?",
                (now,),
            ).fetchall()
            for row in rows:
                if row["kind"] == "action" and row["side_effect_started"]:
                    state = "needs_reconciliation"
                    event = "Åtgärden behöver stämmas av efter att arbetslåset löpte ut"
                    counts["reconciliation"] += 1
                else:
                    state = "queued"
                    event = "Jobbet köas om efter att arbetslåset löpte ut"
                    counts["queued"] += 1
                conn.execute(
                    "UPDATE bot_jobs SET state=?,lease_owner=NULL,lease_until=NULL,"
                    "updated_at=? WHERE job_id=? AND state='running'",
                    (state, now, row["job_id"]),
                )
                if state == "queued":
                    conn.execute(
                        "UPDATE bot_job_steers SET consumed_at=NULL WHERE job_id=?",
                        (row["job_id"],),
                    )
                self._event(conn, row["job_id"], "lease_recovered", event)
            conn.commit()
        return counts

    def active_job(self, chat_id: str | int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM bot_jobs WHERE chat_id=? AND state='running' "
                "ORDER BY started_at DESC LIMIT 1",
                (str(chat_id),),
            ).fetchone()
        return self._job(row)

    def active_jobs(self, chat_id: str | int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM bot_jobs WHERE chat_id=? AND state='running' "
                "ORDER BY started_at ASC, sequence ASC",
                (str(chat_id),),
            ).fetchall()
        return [job for row in rows if (job := self._job(row)) is not None]

    def conversation_jobs(self, chat_id: str | int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT job_id,task_id,kind,state,sequence,created_at,updated_at,completed_at "
                "FROM bot_jobs WHERE chat_id=? AND state IN "
                "('queued','running','needs_reconciliation') ORDER BY sequence LIMIT 10",
                (str(chat_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_reconciliation(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT job_id,chat_id FROM bot_jobs WHERE state='needs_reconciliation' "
                "AND reconciliation_notified_at IS NULL ORDER BY updated_at LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_reconciliation_notified(self, job_id: str) -> None:
        now = _now()
        with self._connection() as conn:
            conn.execute(
                "UPDATE bot_jobs SET reconciliation_notified_at=?,updated_at=? "
                "WHERE job_id=? AND state='needs_reconciliation'",
                (now, now, job_id),
            )

    def resolve_reconciliation(
        self, chat_id: str | int, job_prefix: str, *, outcome: str, actor: str
    ) -> str:
        prefix = str(job_prefix).strip().lower()
        if outcome not in {"done", "not_applied"} or not re.fullmatch(r"[0-9a-f]{8,32}", prefix):
            return "invalid"
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            rows = conn.execute(
                "SELECT job_id FROM bot_jobs WHERE chat_id=? AND state='needs_reconciliation' "
                "AND substr(job_id,1,?)=? LIMIT 2",
                (str(chat_id), len(prefix), prefix),
            ).fetchall()
            if len(rows) > 1:
                conn.commit()
                return "invalid"
            if not rows:
                conn.commit()
                return "not_found"
            row = rows[0]
            conn.execute(
                "UPDATE bot_jobs SET state='reconciled',reconciliation_outcome=?,"
                "reconciled_by=?,reconciled_at=?,updated_at=? WHERE job_id=?",
                (outcome, str(actor)[:80], now, now, row["job_id"]),
            )
            self._event(
                conn,
                row["job_id"],
                "reconciled",
                f"Manuellt avstämt av {actor}: {outcome}",
            )
            conn.commit()
        return "resolved"

    def list_jobs(self, chat_id: str | int, *, limit: int = 8) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT job_id,task_id,kind,state,sequence,created_at,updated_at,completed_at "
                "FROM bot_jobs WHERE chat_id=? ORDER BY sequence DESC LIMIT ?",
                (str(chat_id), max(1, min(int(limit), 20))),
            ).fetchall()
        return [dict(row) for row in rows]

    def research_jobs_summary(
        self,
        channel: str,
        peer_id: str | int,
        actor: str,
        *,
        active_only: bool = False,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        return research_jobs_summary_for_peer(
            channel,
            peer_id,
            actor,
            data_dir=self.path.parent,
            active_only=active_only,
            limit=limit,
        )

    def events(self, job_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT event_type,summary,created_at FROM bot_job_events "
                "WHERE job_id=? ORDER BY event_id DESC LIMIT ?",
                (job_id, max(1, min(int(limit), 30))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def consume_steers(self, job_id: str) -> tuple[list[str], bool]:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            job = conn.execute(
                "SELECT cancel_requested,state FROM bot_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not job or job["state"] != "running":
                conn.commit()
                return [], True
            if job["cancel_requested"]:
                conn.commit()
                return [], True
            used = conn.execute(
                "SELECT COUNT(*) AS n FROM bot_job_steers WHERE job_id=? "
                "AND consumed_at IS NOT NULL",
                (job_id,),
            ).fetchone()["n"]
            remaining = max(0, MAX_STEERS_PER_JOB - int(used))
            rows = conn.execute(
                "SELECT steer_id,text FROM bot_job_steers WHERE job_id=? "
                "AND consumed_at IS NULL ORDER BY steer_id LIMIT ?",
                (job_id, remaining),
            ).fetchall()
            if rows:
                ids = [int(row["steer_id"]) for row in rows]
                marks = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE bot_job_steers SET consumed_at=? WHERE steer_id IN ({marks})",
                    (now, *ids),
                )
                for _row in rows:
                    self._event(conn, job_id, "steer_applied", "Följdfråga infogad")
            conn.commit()
        return [str(row["text"]) for row in rows], False

    def cancel_requested(self, job_id: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT state,cancel_requested FROM bot_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return row is None or row["state"] != "running" or bool(row["cancel_requested"])

    def request_cancel(
        self, chat_id: str | int, job_id: str | None = None
    ) -> str:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            if job_id:
                prefix = str(job_id).strip().lower()
                if not re.fullmatch(r"[0-9a-f]{4,32}", prefix):
                    conn.commit()
                    return "not_found"
                rows = conn.execute(
                    "SELECT job_id,state,kind,side_effect_started,source_update_id FROM bot_jobs "
                    "WHERE (substr(job_id,1,?)=? OR substr(COALESCE(task_id,''),1,?)=?) "
                    "AND chat_id=? LIMIT 2",
                    (len(prefix), prefix, len(prefix), prefix, str(chat_id)),
                ).fetchall()
                if len(rows) > 1:
                    conn.commit()
                    return "ambiguous"
                row = rows[0] if rows else None
            else:
                running_rows = conn.execute(
                    "SELECT job_id,state,kind,side_effect_started,source_update_id FROM bot_jobs "
                    "WHERE chat_id=? AND state='running' ORDER BY started_at DESC LIMIT 2",
                    (str(chat_id),),
                ).fetchall()
                if len(running_rows) > 1:
                    conn.commit()
                    return "ambiguous_running"
                row = running_rows[0] if running_rows else None
                if row is None:
                    row = conn.execute(
                        "SELECT job_id,state,kind,side_effect_started,source_update_id FROM bot_jobs "
                        "WHERE chat_id=? AND state='queued' ORDER BY sequence LIMIT 1",
                        (str(chat_id),),
                    ).fetchone()
            if not row:
                conn.commit()
                return "not_found"
            if row["state"] == "cancelled":
                conn.commit()
                return "cancelled_queued"
            if row["state"] == "queued":
                result = {"text": "Jobbet avbröts innan det startade.", "reply_markup": None}
                conn.execute(
                    "UPDATE bot_jobs SET state='cancelled',payload_json='{}',result_json=?,"
                    "completed_at=?,updated_at=?,delivery_state='delivered' WHERE job_id=?",
                    (_json(result), now, now, row["job_id"]),
                )
                if row["source_update_id"] is not None:
                    conn.execute(
                        "UPDATE telegram_inbox SET state='done' WHERE update_id=?",
                        (row["source_update_id"],),
                    )
                self._event(conn, row["job_id"], "cancelled", "Avbrutet före start")
                conn.commit()
                return "cancelled_queued"
            if row["state"] != "running":
                conn.commit()
                return "not_running"
            if row["kind"] == "action" and row["side_effect_started"]:
                conn.commit()
                return "too_late"
            conn.execute(
                "UPDATE bot_jobs SET cancel_requested=1,updated_at=? WHERE job_id=?",
                (now, row["job_id"]),
            )
            self._event(conn, row["job_id"], "cancel_requested", "Avbrytning begärd")
            conn.commit()
            return "requested"

    def mark_side_effect_started(self, job_id: str) -> bool:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            row = conn.execute(
                "SELECT state,cancel_requested FROM bot_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not row or row["state"] != "running" or row["cancel_requested"]:
                conn.commit()
                return False
            changed = conn.execute(
                "UPDATE bot_jobs SET side_effect_started=1,updated_at=? "
                "WHERE job_id=? AND state='running' AND cancel_requested=0",
                (now, job_id),
            ).rowcount
            if changed != 1:
                conn.commit()
                return False
            self._event(conn, job_id, "action_started", "Godkänd åtgärd startade")
            conn.commit()
        return True

    def save_message_ids(
        self,
        job_id: str,
        *,
        status_message_id: int | None = None,
        answer_message_ids: list[int] | None = None,
    ) -> None:
        with self._connection() as conn:
            if status_message_id is not None:
                conn.execute(
                    "UPDATE bot_jobs SET status_message_id=?,updated_at=? WHERE job_id=?",
                    (status_message_id, _now(), job_id),
                )
            if answer_message_ids is not None:
                conn.execute(
                    "UPDATE bot_jobs SET answer_message_ids_json=?,updated_at=? WHERE job_id=?",
                    (_json(answer_message_ids[:10]), _now(), job_id),
                )

    def complete_job(
        self,
        job_id: str,
        result: dict[str, Any],
        *,
        state: str = "completed",
        failure_class: str | None = None,
    ) -> list[dict[str, Any]]:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            row = conn.execute(
                "SELECT conversation_id,chat_id,task_id,source_update_id,kind FROM bot_jobs "
                "WHERE job_id=? AND state='running'",
                (job_id,),
            ).fetchone()
            if not row:
                conn.commit()
                return []
            parent_task_id = row["task_id"] or job_id
            conn.execute(
                "UPDATE bot_jobs SET state=?,result_json=?,failure_class=?,"
                "lease_owner=NULL,lease_until=NULL,completed_at=?,updated_at=?,"
                "delivery_state='pending',next_delivery_at=0 WHERE job_id=?",
                (state, _json(result), failure_class, now, now, job_id),
            )
            conn.execute("DELETE FROM bot_resource_locks WHERE job_id=?", (job_id,))
            if row["source_update_id"] is not None:
                conn.execute(
                    "UPDATE telegram_inbox SET state='done' WHERE update_id=?",
                    (row["source_update_id"],),
                )
            self._event(
                conn, job_id, state, "Svar klart" if state == "completed" else "Jobbet avslutades"
            )
            leftover = conn.execute(
                "SELECT steer_id,source_update_id,text FROM bot_job_steers "
                "WHERE job_id=? AND consumed_at IS NULL ORDER BY steer_id",
                (job_id,),
            ).fetchall()
            if row["kind"] == "page_review":
                for steer in leftover:
                    conn.execute(
                        "UPDATE bot_job_steers SET consumed_at=? WHERE steer_id=?",
                        (now, steer["steer_id"]),
                    )
                    self._event(
                        conn,
                        job_id,
                        "steer_held",
                        "Följdfrågan stannar i sidgranskningen",
                    )
                leftover = []
            queued: list[dict[str, Any]] = []
            for steer in leftover:
                sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM bot_jobs "
                    "WHERE conversation_id=?",
                    (row["conversation_id"],),
                ).fetchone()["n"]
                next_id = uuid.uuid4().hex
                payload = {"type": "message", "chat_id": row["chat_id"], "text": steer["text"]}
                conn.execute(
                    "INSERT INTO bot_jobs(job_id,task_id,conversation_id,chat_id,source_update_id,"
                    "kind,payload_json,state,sequence,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'queued',?,?,?)",
                    (
                        next_id,
                        parent_task_id,
                        row["conversation_id"],
                        row["chat_id"],
                        steer["source_update_id"],
                        "read_turn",
                        _json(payload),
                        sequence,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE bot_job_steers SET consumed_at=? WHERE steer_id=?",
                    (now, steer["steer_id"]),
                )
                conn.execute(
                    "UPDATE telegram_inbox SET state='queued',disposition='queued',"
                    "payload_json=NULL WHERE update_id=?",
                    (steer["source_update_id"],),
                )
                self._event(conn, next_id, "queued", "Följdfråga köad som ny tur")
                queued.append(
                    {
                        "job_id": next_id,
                        "task_id": parent_task_id,
                        "chat_id": row["chat_id"],
                    }
                )
            consumed_ids = conn.execute(
                "SELECT source_update_id FROM bot_job_steers WHERE job_id=? "
                "AND consumed_at IS NOT NULL",
                (job_id,),
            ).fetchall()
            promoted_ids = {int(steer["source_update_id"]) for steer in leftover}
            for steer in consumed_ids:
                source_id = int(steer["source_update_id"])
                if source_id not in promoted_ids:
                    conn.execute(
                        "UPDATE telegram_inbox SET state='done' WHERE update_id=?",
                        (source_id,),
                    )
            conn.commit()
        return queued

    def create_proposal(
        self,
        chat_id: str | int,
        *,
        kind: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        domain: str | None = None,
        site_id: str | None = None,
        id_prefix: str = "pr",
        ttl_seconds: int = 12 * 3600,
    ) -> dict[str, Any]:
        raw_prefix = str(id_prefix or "")
        prefix = raw_prefix if re.fullmatch(r"[a-z]{2,4}", raw_prefix) else "pr"
        proposal_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
        now = _now()
        expires_at = now + max(60, int(ttl_seconds))
        with self._connection() as conn:
            self._begin(conn)
            conn.execute(
                "INSERT INTO bot_proposals("
                "proposal_id,chat_id,task_id,kind,payload_json,domain,site_id,state,created_at,expires_at"
                ") VALUES(?,?,?,?,?,?,?,'open',?,?)",
                (
                    proposal_id,
                    str(chat_id),
                    task_id,
                    str(kind),
                    _json(payload),
                    domain,
                    site_id,
                    now,
                    expires_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM bot_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            conn.commit()
        return self._proposal(row) or {}

    def pin_review_hashes(
        self,
        chat_id: str | int,
        *,
        actor: str,
        site_id: str,
        plan_id: str,
        base_sha256: str,
        work_sha256: str,
        ttl_seconds: int = 12 * 3600,
    ) -> None:
        """Durably bind approved WP review base/work SHA-256 hashes per chat and plan."""
        now = _now()
        expires_at = now + max(60, int(ttl_seconds))
        with self._connection() as conn:
            self._begin(conn)
            conn.execute(
                "INSERT INTO wp_review_pins("
                "chat_id,plan_id,actor,site_id,base_sha256,work_sha256,expires_at,created_at"
                ") VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(chat_id,plan_id) DO UPDATE SET "
                "actor=excluded.actor,site_id=excluded.site_id,"
                "base_sha256=excluded.base_sha256,work_sha256=excluded.work_sha256,"
                "expires_at=excluded.expires_at,created_at=excluded.created_at",
                (
                    str(chat_id),
                    str(plan_id).strip().lower(),
                    str(actor),
                    str(site_id),
                    str(base_sha256),
                    str(work_sha256),
                    expires_at,
                    now,
                ),
            )
            conn.commit()

    def get_pinned_review_hashes(
        self, chat_id: str | int, plan_id: str
    ) -> dict[str, str] | None:
        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT actor,site_id,base_sha256,work_sha256,expires_at "
                "FROM wp_review_pins WHERE chat_id=? AND plan_id=?",
                (str(chat_id), str(plan_id).strip().lower()),
            ).fetchone()
        if row is None or float(row["expires_at"]) <= now:
            return None
        return {
            "actor": str(row["actor"]),
            "site_id": str(row["site_id"]),
            "base_sha256": str(row["base_sha256"]),
            "work_sha256": str(row["work_sha256"]),
        }

    def drop_pinned_review_hashes(self, chat_id: str | int, plan_id: str) -> None:
        with self._connection() as conn:
            self._begin(conn)
            conn.execute(
                "DELETE FROM wp_review_pins WHERE chat_id=? AND plan_id=?",
                (str(chat_id), str(plan_id).strip().lower()),
            )
            conn.commit()

    def bind_callback_proposal(
        self,
        chat_id: str | int,
        callback_key: str,
        *,
        actor: str,
        domain: str | None = None,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        """Bind a legacy callback key to pinned ``(actor, domain, target_id, expiry)``."""
        key = str(callback_key or "").strip()
        proposal_id = f"cb-{uuid.uuid4().hex[:12]}"
        now = _now()
        expires_at = now + max(60, int(ttl_seconds))
        bound_payload: dict[str, Any] = dict(payload or {})
        bound_payload["actor"] = str(actor)
        if domain:
            bound_payload["domain"] = str(domain)
        if target_id:
            bound_payload["target_id"] = str(target_id)
        with self._connection() as conn:
            self._begin(conn)
            conn.execute(
                "UPDATE bot_proposals SET state='superseded', consumed_at=? "
                "WHERE chat_id=? AND callback_key=? AND state='open'",
                (now, str(chat_id), key),
            )
            conn.execute(
                "INSERT INTO bot_proposals("
                "proposal_id,chat_id,task_id,kind,payload_json,domain,site_id,state,created_at,expires_at,callback_key"
                ") VALUES(?,?,?,?,?,?,?,'open',?,?,?)",
                (
                    proposal_id,
                    str(chat_id),
                    None,
                    "callback_bind",
                    _json(bound_payload),
                    domain,
                    None,
                    now,
                    expires_at,
                    key,
                ),
            )
            row = conn.execute(
                "SELECT * FROM bot_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            conn.commit()
        return self._proposal(row) or {}

    def consume_callback_proposal(
        self,
        chat_id: str | int,
        callback_key: str,
        *,
        actor: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Atomically verify and consume a bound callback proposal if present."""
        key = str(callback_key or "").strip()
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            row = conn.execute(
                "SELECT * FROM bot_proposals "
                "WHERE chat_id=? AND callback_key=? "
                "ORDER BY created_at DESC LIMIT 1",
                (str(chat_id), key),
            ).fetchone()
            if row is None:
                conn.commit()
                return "unbound", None
            if str(row["state"]) != "open":
                conn.commit()
                return "already_used", None
            exp = row["expires_at"]
            if exp is not None and float(exp) <= now:
                conn.execute(
                    "UPDATE bot_proposals SET state='expired', consumed_at=? WHERE proposal_id=? AND state='open'",
                    (now, row["proposal_id"]),
                )
                conn.commit()
                return "expired", None
            payload_dict = _decode_json(row["payload_json"], {})
            bound_actor = str(payload_dict.get("actor") or "").strip().lower()
            if bound_actor and bound_actor != actor.strip().lower():
                conn.commit()
                return "wrong_actor", None
            cur = conn.execute(
                "UPDATE bot_proposals SET state='consumed', consumed_at=? "
                "WHERE proposal_id=? AND state='open'",
                (now, row["proposal_id"]),
            )
            conn.commit()
            if cur.rowcount != 1:
                return "already_used", None
        return "ok", self._proposal(row)

    def get_proposal(
        self, proposal_id: str, *, chat_id: str | int | None = None
    ) -> dict[str, Any] | None:
        with self._connection() as conn:
            if chat_id is None:
                row = conn.execute(
                    "SELECT * FROM bot_proposals WHERE proposal_id=?",
                    (proposal_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM bot_proposals WHERE proposal_id=? AND chat_id=?",
                    (proposal_id, str(chat_id)),
                ).fetchone()
        return self._proposal(row)

    def consume_proposal(
        self, proposal_id: str, *, chat_id: str | int | None = None
    ) -> dict[str, Any] | None:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            if chat_id is None:
                changed = conn.execute(
                    "UPDATE bot_proposals SET state='consumed',consumed_at=? "
                    "WHERE proposal_id=? AND state='open'",
                    (now, proposal_id),
                ).rowcount
            else:
                changed = conn.execute(
                    "UPDATE bot_proposals SET state='consumed',consumed_at=? "
                    "WHERE proposal_id=? AND chat_id=? AND state='open'",
                    (now, proposal_id, str(chat_id)),
                ).rowcount
            if changed != 1:
                conn.commit()
                return None
            row = conn.execute(
                "SELECT * FROM bot_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            conn.commit()
        return self._proposal(row)

    def cancel_proposal(
        self, proposal_id: str, *, chat_id: str | int | None = None
    ) -> bool:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            if chat_id is None:
                changed = conn.execute(
                    "UPDATE bot_proposals SET state='cancelled',consumed_at=? "
                    "WHERE proposal_id=? AND state='open'",
                    (now, proposal_id),
                ).rowcount
            else:
                changed = conn.execute(
                    "UPDATE bot_proposals SET state='cancelled',consumed_at=? "
                    "WHERE proposal_id=? AND chat_id=? AND state='open'",
                    (now, proposal_id, str(chat_id)),
                ).rowcount
            conn.commit()
        return changed == 1

    def acquire_resource_lock(
        self,
        resource_key: str,
        *,
        job_id: str,
        ttl_seconds: int = JOB_LEASE_SECONDS,
    ) -> bool:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            conn.execute(
                "DELETE FROM bot_resource_locks "
                "WHERE expires_at<=? "
                "OR job_id NOT IN (SELECT job_id FROM bot_jobs WHERE state='running')",
                (now,),
            )
            existing = conn.execute(
                "SELECT job_id,owner,expires_at FROM bot_resource_locks WHERE resource_key=?",
                (resource_key,),
            ).fetchone()
            if existing is not None:
                if existing["job_id"] == job_id and existing["owner"] == self.owner:
                    conn.execute(
                        "UPDATE bot_resource_locks SET expires_at=? WHERE resource_key=?",
                        (now + ttl_seconds, resource_key),
                    )
                    conn.commit()
                    return True
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO bot_resource_locks(resource_key,job_id,owner,expires_at) "
                "VALUES(?,?,?,?)",
                (resource_key, job_id, self.owner, now + ttl_seconds),
            )
            conn.commit()
        return True

    def release_resource_lock(
        self, resource_key: str, *, job_id: str | None = None
    ) -> bool:
        with self._connection() as conn:
            self._begin(conn)
            if job_id is None:
                changed = conn.execute(
                    "DELETE FROM bot_resource_locks WHERE resource_key=?",
                    (resource_key,),
                ).rowcount
            else:
                changed = conn.execute(
                    "DELETE FROM bot_resource_locks WHERE resource_key=? AND job_id=?",
                    (resource_key, job_id),
                ).rowcount
            conn.commit()
        return changed > 0

    def mark_delivered(self, job_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE bot_jobs SET delivery_state='delivered',updated_at=? WHERE job_id=?",
                (_now(), job_id),
            )

    def record_delivery_attempt(
        self,
        job_id: str,
        *,
        backoff_seconds: float = 4.0,
        max_attempts: int = 8,
    ) -> str:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            row = conn.execute(
                "SELECT delivery_state,delivery_attempts FROM bot_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return "not_found"
            if row["delivery_state"] != "pending":
                conn.commit()
                return str(row["delivery_state"])
            attempts = int(row["delivery_attempts"] or 0) + 1
            if attempts >= max_attempts:
                conn.execute(
                    "UPDATE bot_jobs SET delivery_attempts=?,delivery_state='failed',"
                    "next_delivery_at=0,updated_at=? WHERE job_id=?",
                    (attempts, now, job_id),
                )
                self._event(
                    conn,
                    job_id,
                    "delivery_failed",
                    "Leverans misslyckades efter max försök",
                )
                conn.commit()
                return "failed"
            delay = min(float(backoff_seconds) * (2 ** (attempts - 1)), 300.0)
            conn.execute(
                "UPDATE bot_jobs SET delivery_attempts=?,next_delivery_at=?,updated_at=? "
                "WHERE job_id=?",
                (attempts, now + delay, now, job_id),
            )
            conn.commit()
            return "retry"

    def undelivered_results(self, *, limit: int = 50) -> list[dict[str, Any]]:
        now = _now()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM bot_jobs WHERE state IN "
                "('completed','failed','cancelled','needs_reconciliation') "
                "AND result_json IS NOT NULL AND delivery_state='pending' "
                "AND next_delivery_at<=? "
                "ORDER BY completed_at LIMIT ?",
                (now, max(1, min(int(limit), 100))),
            ).fetchall()
        return [job for row in rows if (job := self._job(row)) is not None]

    def pending_controls(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT update_id,payload_json FROM telegram_inbox "
                "WHERE state='control' ORDER BY update_id LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [
            item
            for row in rows
            if (item := _decode_json(row["payload_json"], None)) is not None
        ]

    def mark_control_handled(self, update_id: int) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE telegram_inbox SET state='done',payload_json=NULL "
                "WHERE update_id=? AND state='control'",
                (int(update_id),),
            )

    def mark_inbox_done(self, update_id: int) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE telegram_inbox SET state='done' WHERE update_id=?",
                (int(update_id),),
            )

    def acquire_poller_lease(self, *, ttl_seconds: int = 120) -> bool:
        now = _now()
        with self._connection() as conn:
            self._begin(conn)
            row = conn.execute(
                "SELECT owner,expires_at FROM telegram_poller_lease WHERE lease_name='poller'"
            ).fetchone()
            if row and row["owner"] != self.owner and float(row["expires_at"]) > now:
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO telegram_poller_lease(lease_name,owner,expires_at) "
                "VALUES('poller',?,?) ON CONFLICT(lease_name) DO UPDATE SET "
                "owner=excluded.owner,expires_at=excluded.expires_at",
                (self.owner, now + ttl_seconds),
            )
            conn.commit()
        return True

    def renew_poller_lease(self, *, ttl_seconds: int = 120) -> bool:
        now = _now()
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE telegram_poller_lease SET expires_at=? WHERE lease_name='poller' "
                "AND owner=?",
                (now + ttl_seconds, self.owner),
            ).rowcount
        return changed == 1

    def release_poller_lease(self) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM telegram_poller_lease WHERE lease_name='poller' AND owner=?",
                (self.owner,),
            )

    def purge(self, *, retention_seconds: int = JOB_RETENTION_SECONDS) -> int:
        now = _now()
        cutoff = now - max(60, retention_seconds)
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM telegram_inbox WHERE created_at<? AND state!='control'",
                (cutoff,),
            )
            cur = conn.execute(
                "DELETE FROM bot_jobs WHERE (state IN ('completed','failed','cancelled') "
                "OR (state='reconciled' AND reconciliation_outcome='not_applied')) "
                "AND completed_at IS NOT NULL AND completed_at<?",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM bot_proposals "
                "WHERE (state!='open' AND COALESCE(consumed_at, created_at)<?) "
                "OR (state='open' AND COALESCE(expires_at, created_at + 43200)<?)",
                (cutoff, now),
            )
            conn.execute(
                "DELETE FROM wp_review_pins WHERE expires_at<=?",
                (now,),
            )
            conn.execute(
                "DELETE FROM bot_resource_locks "
                "WHERE expires_at<=? "
                "OR job_id NOT IN (SELECT job_id FROM bot_jobs WHERE state='running')",
                (now,),
            )
            return int(cur.rowcount)
