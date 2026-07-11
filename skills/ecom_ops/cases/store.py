"""SQLite-backed support cases created from inbound mail (Cases 2.0)."""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Bump when adding breaking schema changes; _migrate() applies steps in order.
SCHEMA_VERSION = 3


def _default_db_path() -> Path:
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "cases.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd: prefixes for thread matching."""
    s = (subject or "").strip()
    while True:
        nxt = re.sub(r"^(re|fw|fwd)\s*:\s*", "", s, flags=re.I).strip()
        if nxt == s:
            return s.lower()
        s = nxt


@dataclass
class Case:
    id: str
    mailbox_id: str
    subject: str
    from_addr: str
    category: str
    status: str
    order_id: str | None
    draft_reply: str | None
    message_id: str | None
    site: str
    market: str | None
    language: str
    created_at: str
    updated_at: str
    escalation_id: str | None = None
    priority: str = "normal"
    assignee: str | None = "jonatan"
    classify_confidence: float | None = None
    classify_method: str | None = None
    suggest_approve: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mailbox_id": self.mailbox_id,
            "subject": self.subject,
            "from_addr": self.from_addr,
            "category": self.category,
            "status": self.status,
            "order_id": self.order_id,
            "draft_reply": self.draft_reply,
            "message_id": self.message_id,
            "site": self.site,
            "market": self.market,
            "language": self.language,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "escalation_id": self.escalation_id,
            "priority": self.priority,
            "assignee": self.assignee,
            "classify_confidence": self.classify_confidence,
            "classify_method": self.classify_method,
            "suggest_approve": self.suggest_approve,
        }


@dataclass
class CaseMessage:
    id: str
    case_id: str
    direction: str  # inbound | outbound
    from_addr: str
    to_addr: str
    subject: str
    body: str
    message_id: str | None
    created_at: str
    in_reply_to: str | None = None
    references_header: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "case_id": self.case_id,
            "direction": self.direction,
            "from_addr": self.from_addr,
            "to_addr": self.to_addr,
            "subject": self.subject,
            "body": self.body,
            "message_id": self.message_id,
            "created_at": self.created_at,
            "in_reply_to": self.in_reply_to,
            "references_header": self.references_header,
        }


class CaseStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_db_path()
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    mailbox_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    from_addr TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    status TEXT NOT NULL DEFAULT 'open',
                    order_id TEXT,
                    draft_reply TEXT,
                    message_id TEXT UNIQUE,
                    site TEXT NOT NULL DEFAULT 'azom',
                    market TEXT,
                    language TEXT NOT NULL DEFAULT 'sv',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    escalation_id TEXT,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    assignee TEXT DEFAULT 'jonatan'
                );
                CREATE TABLE IF NOT EXISTS case_messages (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    from_addr TEXT NOT NULL,
                    to_addr TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    message_id TEXT,
                    created_at TEXT NOT NULL,
                    in_reply_to TEXT,
                    references_header TEXT,
                    FOREIGN KEY (case_id) REFERENCES cases(id)
                );
                CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
                CREATE INDEX IF NOT EXISTS idx_cases_mailbox ON cases(mailbox_id);
                CREATE INDEX IF NOT EXISTS idx_case_messages_mid ON case_messages(message_id);
                """
            )
            self._migrate(conn)

    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()
            if row is None or row["v"] is None:
                return 0
            return int(row["v"])

    def _current_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()
        if row is None or row["v"] is None:
            return 0
        return int(row["v"])

    def _record_version(self, conn: sqlite3.Connection, version: int) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, _now()),
        )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply pending schema steps idempotently."""
        current = self._current_version(conn)
        # Legacy DBs created before schema_migrations: treat as v0 then apply.
        if current == 0:
            # Detect pre-versioned cases table
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "cases" in tables:
                self._migrate_columns(conn)
                self._record_version(conn, 1)
                self._record_version(conn, 2)
                current = 2
            else:
                self._record_version(conn, 1)
                current = 1
        if current < 2:
            self._migrate_columns(conn)
            self._record_version(conn, 2)
            current = 2
        if current < 3:
            self._migrate_v3_suggest(conn)
            self._record_version(conn, 3)

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        case_cols = {r[1] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
        msg_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(case_messages)").fetchall()
        }
        alters: list[str] = []
        if "escalation_id" not in case_cols:
            alters.append("ALTER TABLE cases ADD COLUMN escalation_id TEXT")
        if "priority" not in case_cols:
            alters.append(
                "ALTER TABLE cases ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'"
            )
        if "assignee" not in case_cols:
            alters.append(
                "ALTER TABLE cases ADD COLUMN assignee TEXT DEFAULT 'jonatan'"
            )
        if "in_reply_to" not in msg_cols:
            alters.append("ALTER TABLE case_messages ADD COLUMN in_reply_to TEXT")
        if "references_header" not in msg_cols:
            alters.append(
                "ALTER TABLE case_messages ADD COLUMN references_header TEXT"
            )
        for sql in alters:
            conn.execute(sql)

    def _migrate_v3_suggest(self, conn: sqlite3.Connection) -> None:
        case_cols = {r[1] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
        alters: list[str] = []
        if "classify_confidence" not in case_cols:
            alters.append("ALTER TABLE cases ADD COLUMN classify_confidence REAL")
        if "classify_method" not in case_cols:
            alters.append("ALTER TABLE cases ADD COLUMN classify_method TEXT")
        if "suggest_approve" not in case_cols:
            alters.append(
                "ALTER TABLE cases ADD COLUMN suggest_approve INTEGER NOT NULL DEFAULT 0"
            )
        for sql in alters:
            conn.execute(sql)

    def find_by_message_id(self, message_id: str) -> Case | None:
        if not message_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE message_id = ?", (message_id,)
            ).fetchone()
            if row:
                return self._row_to_case(row)
            # Also match inbound message ids on threaded follow-ups
            msg = conn.execute(
                "SELECT case_id FROM case_messages WHERE message_id = ? LIMIT 1",
                (message_id,),
            ).fetchone()
            if msg:
                crow = conn.execute(
                    "SELECT * FROM cases WHERE id = ?", (msg["case_id"],)
                ).fetchone()
                return self._row_to_case(crow) if crow else None
        return None

    def find_by_thread_headers(
        self,
        *,
        in_reply_to: str | None,
        references_header: str | None,
        from_addr: str,
        subject: str,
        mailbox_id: str | None = None,
    ) -> Case | None:
        """Find open/escalated case by In-Reply-To, References, or from+subject."""
        candidates: list[str] = []
        if in_reply_to:
            candidates.append(in_reply_to.strip())
        if references_header:
            for part in re.split(r"\s+", references_header.strip()):
                if part and part not in candidates:
                    candidates.append(part)

        with self._conn() as conn:
            for mid in candidates:
                # Match root case message_id
                row = conn.execute(
                    """
                    SELECT * FROM cases
                    WHERE message_id = ? AND status IN ('open', 'escalated')
                    """,
                    (mid,),
                ).fetchone()
                if row:
                    return self._row_to_case(row)
                # Match any message in thread
                msg = conn.execute(
                    """
                    SELECT c.* FROM case_messages m
                    JOIN cases c ON c.id = m.case_id
                    WHERE m.message_id = ? AND c.status IN ('open', 'escalated')
                    LIMIT 1
                    """,
                    (mid,),
                ).fetchone()
                if msg:
                    return self._row_to_case(msg)

            # Fallback: same mailbox + from + normalized subject
            norm = normalize_subject(subject)
            sql = """
                SELECT * FROM cases
                WHERE status IN ('open', 'escalated')
                  AND lower(from_addr) = lower(?)
                ORDER BY updated_at DESC
            """
            params: list[Any] = [from_addr]
            if mailbox_id:
                sql = """
                    SELECT * FROM cases
                    WHERE status IN ('open', 'escalated')
                      AND mailbox_id = ?
                      AND lower(from_addr) = lower(?)
                    ORDER BY updated_at DESC
                """
                params = [mailbox_id, from_addr]
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                if normalize_subject(row["subject"]) == norm:
                    return self._row_to_case(row)
        return None

    def get(self, case_id: str) -> Case | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
        return self._row_to_case(row) if row else None

    def resolve_id_prefix(self, prefix: str) -> Case | None:
        """Resolve short id prefix (e.g. Telegram id8)."""
        p = (prefix or "").strip().lower()
        if not p:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cases WHERE lower(id) LIKE ? LIMIT 2",
                (f"{p}%",),
            ).fetchall()
        if len(rows) == 1:
            return self._row_to_case(rows[0])
        return None

    def list_cases(
        self,
        *,
        status: str | None = "open",
        mailbox_id: str | None = None,
        category: str | None = None,
        suggest_approve: bool | None = None,
        limit: int = 50,
    ) -> list[Case]:
        sql = "SELECT * FROM cases WHERE 1=1"
        params: list[Any] = []
        if status and status != "all":
            if "," in status:
                parts = [s.strip() for s in status.split(",") if s.strip()]
                placeholders = ",".join("?" * len(parts))
                sql += f" AND status IN ({placeholders})"
                params.extend(parts)
            else:
                sql += " AND status = ?"
                params.append(status)
        if mailbox_id:
            sql += " AND mailbox_id = ?"
            params.append(mailbox_id)
        if category:
            sql += " AND category = ?"
            params.append(category)
        if suggest_approve is not None:
            sql += " AND suggest_approve = ?"
            params.append(1 if suggest_approve else 0)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_case(r) for r in rows]

    def count_by_status(self, status: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM cases WHERE status = ?", (status,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def create_case(
        self,
        *,
        mailbox_id: str,
        subject: str,
        from_addr: str,
        body: str,
        category: str,
        draft_reply: str | None,
        order_id: str | None,
        message_id: str | None,
        site: str = "azom",
        market: str | None = None,
        language: str = "sv",
        to_addr: str = "",
        status: str = "open",
        priority: str = "normal",
        escalation_id: str | None = None,
        assignee: str | None = "jonatan",
        in_reply_to: str | None = None,
        references_header: str | None = None,
        classify_confidence: float | None = None,
        classify_method: str | None = None,
        suggest_approve: bool = False,
    ) -> Case:
        if message_id:
            existing = self.find_by_message_id(message_id)
            if existing:
                return existing
        case_id = str(uuid.uuid4())
        now = _now()
        case = Case(
            id=case_id,
            mailbox_id=mailbox_id,
            subject=subject or "(no subject)",
            from_addr=from_addr,
            category=category,
            status=status,
            order_id=order_id,
            draft_reply=draft_reply,
            message_id=message_id,
            site=site,
            market=market,
            language=language,
            created_at=now,
            updated_at=now,
            escalation_id=escalation_id,
            priority=priority,
            assignee=assignee,
            classify_confidence=classify_confidence,
            classify_method=classify_method,
            suggest_approve=bool(suggest_approve),
        )
        msg_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO cases (
                    id, mailbox_id, subject, from_addr, category, status,
                    order_id, draft_reply, message_id, site, market, language,
                    created_at, updated_at, escalation_id, priority, assignee,
                    classify_confidence, classify_method, suggest_approve
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case.id,
                    case.mailbox_id,
                    case.subject,
                    case.from_addr,
                    case.category,
                    case.status,
                    case.order_id,
                    case.draft_reply,
                    case.message_id,
                    case.site,
                    case.market,
                    case.language,
                    case.created_at,
                    case.updated_at,
                    case.escalation_id,
                    case.priority,
                    case.assignee,
                    case.classify_confidence,
                    case.classify_method,
                    1 if case.suggest_approve else 0,
                ),
            )
            conn.execute(
                """
                INSERT INTO case_messages (
                    id, case_id, direction, from_addr, to_addr, subject, body,
                    message_id, created_at, in_reply_to, references_header
                ) VALUES (?, ?, 'inbound', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    case.id,
                    from_addr,
                    to_addr,
                    subject or "",
                    body or "",
                    message_id,
                    now,
                    in_reply_to,
                    references_header,
                ),
            )
        return case

    def append_inbound(
        self,
        case_id: str,
        *,
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
        message_id: str | None,
        in_reply_to: str | None = None,
        references_header: str | None = None,
        draft_reply: str | None = None,
        category: str | None = None,
        order_id: str | None = None,
        classify_confidence: float | None = None,
        classify_method: str | None = None,
        suggest_approve: bool | None = None,
    ) -> Case | None:
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO case_messages (
                    id, case_id, direction, from_addr, to_addr, subject, body,
                    message_id, created_at, in_reply_to, references_header
                ) VALUES (?, ?, 'inbound', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    case_id,
                    from_addr,
                    to_addr,
                    subject or "",
                    body or "",
                    message_id,
                    now,
                    in_reply_to,
                    references_header,
                ),
            )
            sets = ["updated_at = ?"]
            params: list[Any] = [now]
            if draft_reply is not None:
                sets.append("draft_reply = ?")
                params.append(draft_reply)
            if category is not None:
                sets.append("category = ?")
                params.append(category)
            if order_id is not None:
                sets.append("order_id = ?")
                params.append(order_id)
            if classify_confidence is not None:
                sets.append("classify_confidence = ?")
                params.append(classify_confidence)
            if classify_method is not None:
                sets.append("classify_method = ?")
                params.append(classify_method)
            if suggest_approve is not None:
                sets.append("suggest_approve = ?")
                params.append(1 if suggest_approve else 0)
            params.append(case_id)
            conn.execute(
                f"UPDATE cases SET {', '.join(sets)} WHERE id = ?",
                params,
            )
        return self.get(case_id)

    def update_draft(self, case_id: str, draft: str) -> Case | None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE cases SET draft_reply = ?, updated_at = ? WHERE id = ?",
                (draft, _now(), case_id),
            )
        return self.get(case_id)

    def set_status(self, case_id: str, status: str) -> Case | None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE cases SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), case_id),
            )
        return self.get(case_id)

    def set_escalation(
        self,
        case_id: str,
        escalation_id: str,
        *,
        priority: str = "high",
        status: str = "escalated",
    ) -> Case | None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE cases
                SET escalation_id = ?, priority = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (escalation_id, priority, status, _now(), case_id),
            )
        return self.get(case_id)

    def mark_replied(
        self,
        case_id: str,
        *,
        outbound_body: str,
        to_addr: str,
        from_addr: str,
        subject: str,
    ) -> Case | None:
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE cases SET status = 'replied', updated_at = ? WHERE id = ?",
                (now, case_id),
            )
            conn.execute(
                """
                INSERT INTO case_messages (
                    id, case_id, direction, from_addr, to_addr, subject, body,
                    message_id, created_at, in_reply_to, references_header
                ) VALUES (?, ?, 'outbound', ?, ?, ?, ?, NULL, ?, NULL, NULL)
                """,
                (str(uuid.uuid4()), case_id, from_addr, to_addr, subject, outbound_body, now),
            )
        return self.get(case_id)

    def close(self, case_id: str) -> Case | None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE cases SET status = 'closed', updated_at = ? WHERE id = ?",
                (_now(), case_id),
            )
        return self.get(case_id)

    def messages(self, case_id: str) -> list[CaseMessage]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM case_messages WHERE case_id = ? ORDER BY created_at ASC",
                (case_id,),
            ).fetchall()
        return [self._row_to_msg(r) for r in rows]

    @staticmethod
    def _row_to_case(row: sqlite3.Row) -> Case:
        keys = row.keys()
        conf = None
        if "classify_confidence" in keys and row["classify_confidence"] is not None:
            conf = float(row["classify_confidence"])
        method = None
        if "classify_method" in keys and row["classify_method"]:
            method = str(row["classify_method"])
        suggest = False
        if "suggest_approve" in keys and row["suggest_approve"] is not None:
            suggest = bool(int(row["suggest_approve"]))
        return Case(
            id=row["id"],
            mailbox_id=row["mailbox_id"],
            subject=row["subject"],
            from_addr=row["from_addr"],
            category=row["category"],
            status=row["status"],
            order_id=row["order_id"],
            draft_reply=row["draft_reply"],
            message_id=row["message_id"],
            site=row["site"],
            market=row["market"],
            language=row["language"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            escalation_id=row["escalation_id"] if "escalation_id" in keys else None,
            priority=(row["priority"] if "priority" in keys and row["priority"] else "normal"),
            assignee=row["assignee"] if "assignee" in keys else "jonatan",
            classify_confidence=conf,
            classify_method=method,
            suggest_approve=suggest,
        )

    @staticmethod
    def _row_to_msg(row: sqlite3.Row) -> CaseMessage:
        keys = row.keys()
        return CaseMessage(
            id=row["id"],
            case_id=row["case_id"],
            direction=row["direction"],
            from_addr=row["from_addr"],
            to_addr=row["to_addr"],
            subject=row["subject"],
            body=row["body"],
            message_id=row["message_id"],
            created_at=row["created_at"],
            in_reply_to=row["in_reply_to"] if "in_reply_to" in keys else None,
            references_header=(
                row["references_header"] if "references_header" in keys else None
            ),
        )
