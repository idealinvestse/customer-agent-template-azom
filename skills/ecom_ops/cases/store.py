"""SQLite-backed support cases created from inbound mail."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _default_db_path() -> Path:
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "cases.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                    updated_at TEXT NOT NULL
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
                    FOREIGN KEY (case_id) REFERENCES cases(id)
                );
                CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
                CREATE INDEX IF NOT EXISTS idx_cases_mailbox ON cases(mailbox_id);
                """
            )

    def find_by_message_id(self, message_id: str) -> Case | None:
        if not message_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._row_to_case(row) if row else None

    def get(self, case_id: str) -> Case | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
        return self._row_to_case(row) if row else None

    def list_cases(
        self,
        *,
        status: str | None = "open",
        mailbox_id: str | None = None,
        limit: int = 50,
    ) -> list[Case]:
        sql = "SELECT * FROM cases WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if mailbox_id:
            sql += " AND mailbox_id = ?"
            params.append(mailbox_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_case(r) for r in rows]

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
            status="open",
            order_id=order_id,
            draft_reply=draft_reply,
            message_id=message_id,
            site=site,
            market=market,
            language=language,
            created_at=now,
            updated_at=now,
        )
        msg_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO cases (
                    id, mailbox_id, subject, from_addr, category, status,
                    order_id, draft_reply, message_id, site, market, language,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.execute(
                """
                INSERT INTO case_messages (
                    id, case_id, direction, from_addr, to_addr, subject, body,
                    message_id, created_at
                ) VALUES (?, ?, 'inbound', ?, ?, ?, ?, ?, ?)
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
                ),
            )
        return case

    def update_draft(self, case_id: str, draft: str) -> Case | None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE cases SET draft_reply = ?, updated_at = ? WHERE id = ?",
                (draft, _now(), case_id),
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
                    message_id, created_at
                ) VALUES (?, ?, 'outbound', ?, ?, ?, ?, NULL, ?)
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
        )

    @staticmethod
    def _row_to_msg(row: sqlite3.Row) -> CaseMessage:
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
        )
