"""GDPR PII retention + deletion for closed and replied cases.

Default retention: 90 days after ``closed`` or ``replied``. After that, the
case row, its messages, and any draft are permanently deleted from
``cases.db``. JSONL sidecars (audit / telemetry / escalations) are purged
on the same timer: telemetry 90d, audit 365d, escalations 90d.

A best-effort redaction mode is also offered: instead of hard delete,
overwrite PII fields with a tombstone marker.

Wired via CLI: ``python -m ecom_ops --actor oscar cases retention-purge``
(requires ``Permission.ADMIN``) and systemd timer ``azom-retention-purge.timer``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.cases.store import CaseStore
from ecom_ops.security import SecurityError, validate_email

# Default retention window (days) for closed + replied cases.
DEFAULT_RETENTION_DAYS = 90
AUDIT_RETENTION_DAYS = 365
JSONL_RETENTION_DAYS = 90
TERMINAL_STATUSES = ("closed", "replied")


@dataclass(frozen=True)
class RetentionResult:
    ok: bool
    message: str
    deleted: int = 0
    redacted: int = 0
    retention_days: int = DEFAULT_RETENTION_DAYS
    jsonl_purged: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "deleted": self.deleted,
            "redacted": self.redacted,
            "retention_days": self.retention_days,
            "jsonl_purged": dict(self.jsonl_purged),
        }


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _status_sql() -> str:
    return ",".join("?" * len(TERMINAL_STATUSES))


def count_eligible_cases(
    *,
    store: CaseStore | None = None,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> int:
    """Count closed+replied cases older than the retention window."""
    days = int(retention_days or DEFAULT_RETENTION_DAYS)
    cs = store or CaseStore()
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
    n = 0
    for status in TERMINAL_STATUSES:
        for case in cs.list_cases(status=status, limit=10000):
            if (case.updated_at or "") < cutoff:
                n += 1
    return n


def purge_jsonl(
    path: Path,
    *,
    retention_days: int,
    now: datetime | None = None,
    ts_key: str = "created_at",
) -> int:
    """Drop JSONL rows older than ``retention_days``. Returns removed count."""
    if retention_days < 1 or not path.is_file():
        return 0
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).isoformat()
    kept: list[str] = []
    removed = 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            kept.append(s)
            continue
        ts = str(ev.get(ts_key) or ev.get("ts") or ev.get("updated_at") or "")
        if ts and ts < cutoff:
            removed += 1
            continue
        kept.append(s)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    os.replace(tmp, path)
    return removed


def purge_sidecar_jsonl(*, now: datetime | None = None) -> dict[str, int]:
    """Purge audit / telemetry / escalations under AZOM_DATA_DIR."""
    base = _data_dir()
    out = {
        "telemetry.jsonl": purge_jsonl(
            base / "telemetry.jsonl",
            retention_days=JSONL_RETENTION_DAYS,
            now=now,
        ),
        "audit.jsonl": purge_jsonl(
            base / "audit.jsonl",
            retention_days=AUDIT_RETENTION_DAYS,
            now=now,
        ),
        "escalations.jsonl": purge_jsonl(
            base / "escalations.jsonl",
            retention_days=JSONL_RETENTION_DAYS,
            now=now,
        ),
    }
    return out


def purge_closed_cases(
    *,
    store: CaseStore | None = None,
    retention_days: int | None = None,
    redact: bool = False,
    now: datetime | None = None,
) -> RetentionResult:
    """Delete (or redact) closed+replied cases older than ``retention_days``."""
    days = int(retention_days or DEFAULT_RETENTION_DAYS)
    if days < 1:
        return RetentionResult(ok=False, message="retention_days must be >= 1")
    cs = store or CaseStore()
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()
    placeholders = _status_sql()

    import sqlite3

    conn = sqlite3.connect(str(cs.path))
    conn.row_factory = sqlite3.Row
    try:
        if redact:
            cur = conn.execute(
                f"""
                UPDATE cases SET
                    from_addr = '[redacted]',
                    draft_reply = NULL,
                    subject = '[redacted]'
                WHERE status IN ({placeholders})
                  AND updated_at < ?
                """,
                (*TERMINAL_STATUSES, cutoff_iso),
            )
            redacted = cur.rowcount
            conn.execute(
                f"""
                UPDATE case_messages SET
                    from_addr = '[redacted]',
                    to_addr = '[redacted]',
                    body = '[redacted]',
                    subject = '[redacted]'
                WHERE case_id IN (
                    SELECT id FROM cases
                    WHERE status IN ({placeholders}) AND updated_at < ?
                )
                """,
                (*TERMINAL_STATUSES, cutoff_iso),
            )
            conn.commit()
            jsonl = purge_sidecar_jsonl(now=now)
            return RetentionResult(
                ok=True,
                message=f"Redacted {redacted} closed/replied cases older than {days}d",
                redacted=redacted,
                retention_days=days,
                jsonl_purged=jsonl,
            )
        conn.execute(
            f"""
            DELETE FROM case_messages
            WHERE case_id IN (
                SELECT id FROM cases
                WHERE status IN ({placeholders}) AND updated_at < ?
            )
            """,
            (*TERMINAL_STATUSES, cutoff_iso),
        )
        cur = conn.execute(
            f"""
            DELETE FROM cases
            WHERE status IN ({placeholders}) AND updated_at < ?
            """,
            (*TERMINAL_STATUSES, cutoff_iso),
        )
        deleted = cur.rowcount
        conn.commit()
        jsonl = purge_sidecar_jsonl(now=now)
        return RetentionResult(
            ok=True,
            message=f"Deleted {deleted} closed/replied cases older than {days}d",
            deleted=deleted,
            retention_days=days,
            jsonl_purged=jsonl,
        )
    except Exception as exc:
        conn.rollback()
        return RetentionResult(ok=False, message=f"Retention purge error: {exc}")
    finally:
        conn.close()


def gdpr_cases_for_email(conn: Any, email: str) -> list[str]:
    """Case ids matching ``from_addr`` on the case or any message (case-insensitive)."""
    email_l = email.strip().lower()
    rows = conn.execute(
        """
        SELECT DISTINCT c.id FROM cases c
        LEFT JOIN case_messages m ON m.case_id = c.id
        WHERE lower(c.from_addr) = ? OR lower(m.from_addr) = ?
        """,
        (email_l, email_l),
    ).fetchall()
    return [r[0] for r in rows]


def gdpr_export(*, email: str, db_path: Path) -> dict[str, Any]:
    """Oscar GDPR export for one email. Validates address."""
    try:
        email = validate_email(email)
    except SecurityError as exc:
        return {"ok": False, "message": str(exc)}
    if not db_path.is_file():
        return {"ok": False, "message": "cases.db not found"}
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ids = gdpr_cases_for_email(conn, email)
        if not ids:
            return {
                "ok": True,
                "email": email,
                "cases": [],
                "messages": [],
                "message": "No data found",
            }
        placeholders = ",".join("?" * len(ids))
        cases = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM cases WHERE id IN ({placeholders})", ids
            ).fetchall()
        ]
        messages = [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM case_messages WHERE case_id IN ({placeholders})",
                ids,
            ).fetchall()
        ]
        return {
            "ok": True,
            "email": email,
            "cases": cases,
            "messages": messages,
        }
    finally:
        conn.close()


def gdpr_delete(*, email: str, db_path: Path) -> dict[str, Any]:
    """Oscar GDPR delete for one email. Validates address."""
    try:
        email = validate_email(email)
    except SecurityError as exc:
        return {"ok": False, "message": str(exc)}
    if not db_path.is_file():
        return {"ok": False, "message": "cases.db not found"}
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        ids = gdpr_cases_for_email(conn, email)
        if not ids:
            return {
                "ok": True,
                "deleted": 0,
                "message": "No cases found for this email",
            }
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"DELETE FROM case_messages WHERE case_id IN ({placeholders})", ids
        )
        conn.execute(f"DELETE FROM cases WHERE id IN ({placeholders})", ids)
        conn.commit()
        return {
            "ok": True,
            "deleted": len(ids),
            "message": f"Deleted {len(ids)} cases for {email}",
        }
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "message": str(exc)[:200]}
    finally:
        conn.close()
