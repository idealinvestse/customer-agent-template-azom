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

from ecom_ops.wp_ops.page_review_intent import is_page_review

MAX_STEERS_PER_JOB = 3
MAX_QUEUED_PER_CHAT = 8
JOB_LEASE_SECONDS = 120
JOB_RETENTION_SECONDS = 7 * 24 * 60 * 60

_CONTROL = re.compile(r"^/(status|jobs|stop|cancel|reconcile)(?:@\w+)?(?:\s+(.*))?$", re.I)
_ACTION_COMMAND = re.compile(
    r"^/cases\s+(?:approve|send|close|regenerate|regen|redraft)\b", re.I
)
_ACTION_CALLBACK = re.compile(
    r"^(?:cases:approve:|cases:regen:|order:set:|product:desc:|action:yes$|"
    r"escalate:yes$|wpfile:stage:fe-[a-f0-9]{12}$)",
    re.I,
)


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


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
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(source_update_id) REFERENCES telegram_inbox(update_id)
                        ON DELETE SET NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS bot_jobs_one_running_per_conversation
                    ON bot_jobs(conversation_id) WHERE state='running';
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
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _begin(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    def update_offset(self) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM bot_meta WHERE key='telegram_update_offset'"
            ).fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def _parse_update(update: dict[str, Any]) -> dict[str, Any] | None:
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
        if chat_id is None or not isinstance(text, str):
            return {"update_id": update_id, "kind": "ignored"}
        return {
            "update_id": update_id,
            "kind": "message",
            "chat_id": str(chat_id),
            "text": text[:8000],
            "message_id": message.get("message_id"),
        }

    @staticmethod
    def approval_snapshot(state: dict[str, Any] | None) -> str:
        value = state if isinstance(state, dict) else {}
        snapshot = {
            "flow": value.get("flow"),
            "step": value.get("step"),
            "slots": value.get("slots") or {},
            "session": value.get("session") or {},
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

    def accept_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Durably store the update and advance the Telegram cursor atomically."""
        parsed = self._parse_update(update)
        if not parsed:
            return {"disposition": "ignored"}
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
            if parsed["kind"] == "message":
                text = parsed["text"]
                control = _CONTROL.fullmatch(text.strip())
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

                running = conn.execute(
                    "SELECT job_id,kind FROM bot_jobs WHERE conversation_id=? "
                    "AND state='running' LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                is_command = text.lstrip().startswith("/")
                if running and running["kind"] in {"read_turn", "page_review"} and not is_command:
                    consumed = conn.execute(
                        "SELECT COUNT(*) AS n FROM bot_job_steers WHERE job_id=? "
                        "AND consumed_at IS NOT NULL",
                        (running["job_id"],),
                    ).fetchone()["n"]
                    pending = conn.execute(
                        "SELECT COUNT(*) AS n FROM bot_job_steers WHERE job_id=? "
                        "AND consumed_at IS NULL",
                        (running["job_id"],),
                    ).fetchone()["n"]
                    queued = conn.execute(
                        "SELECT COUNT(*) AS n FROM bot_jobs WHERE conversation_id=? "
                        "AND state='queued'",
                        (conversation_id,),
                    ).fetchone()["n"]
                    if (
                        consumed + pending < MAX_STEERS_PER_JOB
                        and queued + pending + 1 <= MAX_QUEUED_PER_CHAT
                    ):
                        conn.execute(
                            "INSERT INTO bot_job_steers(job_id,source_update_id,text,created_at) "
                            "VALUES(?,?,?,?)",
                            (running["job_id"], update_id, text, now),
                        )
                        self._event(conn, running["job_id"], "steer_queued", "Följdfråga mottagen")
                        conn.execute(
                            "UPDATE telegram_inbox SET state='steer',disposition='steer',"
                            "payload_json=NULL WHERE update_id=?",
                            (update_id,),
                        )
                        conn.commit()
                        return {
                            "disposition": "steer",
                            "job_id": running["job_id"],
                            "chat_id": chat_id,
                            "update_id": update_id,
                        }
                if _ACTION_COMMAND.match(text.strip()):
                    kind = "action"
                elif is_page_review(text):
                    kind = "page_review"
                else:
                    kind = "read_turn"
                payload = {
                    "type": "message",
                    "chat_id": chat_id,
                    "text": text,
                    "source_message_id": parsed.get("message_id"),
                }
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
                    now=now,
                )
            else:
                data = parsed["data"]
                kind = "action" if _ACTION_CALLBACK.match(data) else "callback"
                payload = {
                    "type": "callback",
                    "chat_id": chat_id,
                    "data": data,
                    "callback_query_id": parsed["callback_query_id"],
                    "source_message_id": parsed.get("source_message_id"),
                }
                if data in {"action:yes", "escalate:yes"} or data.startswith("wpfile:stage:"):
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
                job_id = self._insert_job(
                    conn,
                    conversation_id=conversation_id,
                    chat_id=chat_id,
                    update_id=update_id,
                    callback_id=parsed["callback_query_id"],
                    kind=kind,
                    payload=payload,
                    approval_key=approval_key,
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
                "chat_id": chat_id,
                "update_id": update_id,
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
    ) -> str:
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM bot_jobs "
            "WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()["n"]
        job_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO bot_jobs(job_id,conversation_id,chat_id,source_update_id,"
            "callback_query_id,approval_key,kind,payload_json,state,sequence,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?)",
            (
                job_id,
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
                "AND NOT EXISTS (SELECT 1 FROM bot_jobs r WHERE "
                "r.conversation_id=q.conversation_id AND r.state='running') "
                "AND (q.kind!='action' OR NOT EXISTS (SELECT 1 FROM bot_jobs r "
                "WHERE r.conversation_id=q.conversation_id "
                "AND r.state='needs_reconciliation')) "
                "ORDER BY q.sequence,q.created_at LIMIT 1"
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

    def conversation_jobs(self, chat_id: str | int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT job_id,kind,state,sequence,created_at,updated_at,completed_at "
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
                "SELECT job_id,kind,state,sequence,created_at,updated_at,completed_at "
                "FROM bot_jobs WHERE chat_id=? ORDER BY sequence DESC LIMIT ?",
                (str(chat_id), max(1, min(int(limit), 20))),
            ).fetchall()
        return [dict(row) for row in rows]

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
                if not re.fullmatch(r"[0-9a-f]{8,32}", prefix):
                    conn.commit()
                    return "not_found"
                rows = conn.execute(
                    "SELECT job_id,state,kind,side_effect_started,source_update_id FROM bot_jobs "
                    "WHERE substr(job_id,1,?)=? AND chat_id=? LIMIT 2",
                    (len(prefix), prefix, str(chat_id)),
                ).fetchall()
                if len(rows) > 1:
                    conn.commit()
                    return "ambiguous"
                row = rows[0] if rows else None
            else:
                row = conn.execute(
                    "SELECT job_id,state,kind,side_effect_started,source_update_id FROM bot_jobs "
                    "WHERE chat_id=? AND state='running' ORDER BY started_at DESC LIMIT 1",
                    (str(chat_id),),
                ).fetchone()
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
                "SELECT conversation_id,source_update_id FROM bot_jobs "
                "WHERE job_id=? AND state='running'",
                (job_id,),
            ).fetchone()
            if not row:
                conn.commit()
                return []
            conn.execute(
                "UPDATE bot_jobs SET state=?,result_json=?,failure_class=?,"
                "lease_owner=NULL,lease_until=NULL,completed_at=?,updated_at=?,"
                "delivery_state='pending' WHERE job_id=?",
                (state, _json(result), failure_class, now, now, job_id),
            )
            if row["source_update_id"] is not None:
                conn.execute(
                    "UPDATE telegram_inbox SET state='done' WHERE update_id=?",
                    (row["source_update_id"],),
                )
            self._event(conn, job_id, state, "Svar klart" if state == "completed" else "Jobbet avslutades")
            leftover = conn.execute(
                "SELECT steer_id,source_update_id,text FROM bot_job_steers "
                "WHERE job_id=? AND consumed_at IS NULL ORDER BY steer_id",
                (job_id,),
            ).fetchall()
            queued: list[dict[str, Any]] = []
            for steer in leftover:
                sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS n FROM bot_jobs "
                    "WHERE conversation_id=?",
                    (row["conversation_id"],),
                ).fetchone()["n"]
                next_id = uuid.uuid4().hex
                job_row = conn.execute(
                    "SELECT chat_id FROM bot_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                payload = {"type": "message", "chat_id": job_row["chat_id"], "text": steer["text"]}
                conn.execute(
                    "INSERT INTO bot_jobs(job_id,conversation_id,chat_id,source_update_id,"
                    "kind,payload_json,state,sequence,created_at,updated_at) "
                    "VALUES(?,?,?,?,? ,?,'queued',?,?,?)",
                    (
                        next_id,
                        row["conversation_id"],
                        job_row["chat_id"],
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
                queued.append({"job_id": next_id, "chat_id": job_row["chat_id"]})
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

    def mark_delivered(self, job_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE bot_jobs SET delivery_state='delivered',updated_at=? WHERE job_id=?",
                (_now(), job_id),
            )

    def undelivered_results(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM bot_jobs WHERE state IN "
                "('completed','failed','cancelled','needs_reconciliation') "
                "AND result_json IS NOT NULL AND delivery_state='pending' "
                "ORDER BY completed_at LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._job(row) for row in rows if row]

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
        cutoff = _now() - max(60, retention_seconds)
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
            return int(cur.rowcount)
