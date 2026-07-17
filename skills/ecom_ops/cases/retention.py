"""GDPR PII retention + deletion for closed cases (Art 17 / Art 5(1)(e)).

Default retention: 90 days after ``closed`` status. After that, the case row,
its messages, and any draft are permanently deleted from ``cases.db``.

A best-effort redaction mode is also offered: instead of hard delete, overwrite
``from_addr`` / ``body`` / ``draft_reply`` with a tombstone marker. This keeps
aggregate analytics (counts, KPIs) while removing PII. Use ``redact=True`` for
that mode.

Wired via CLI: ``python -m ecom_ops cases retention-purge`` and a systemd timer
``azom-retention-purge.timer`` (daily).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ecom_ops.cases.store import CaseStore

# Default retention window (days) for closed cases.
DEFAULT_RETENTION_DAYS = 90


@dataclass(frozen=True)
class RetentionResult:
    ok: bool
    message: str
    deleted: int = 0
    redacted: int = 0
    retention_days: int = DEFAULT_RETENTION_DAYS

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "deleted": self.deleted,
            "redacted": self.redacted,
            "retention_days": self.retention_days,
        }


def purge_closed_cases(
    *,
    store: CaseStore | None = None,
    retention_days: int | None = None,
    redact: bool = False,
    now: datetime | None = None,
) -> RetentionResult:
    """Delete (or redact) closed cases older than ``retention_days``.

    Args:
        store: CaseStore instance (default: from env).
        retention_days: Days to retain after close (default: 90).
        redact: If True, overwrite PII fields instead of deleting rows.
        now: Override current time for testing.
    """
    days = int(retention_days or DEFAULT_RETENTION_DAYS)
    if days < 1:
        return RetentionResult(ok=False, message="retention_days must be >= 1")
    cs = store or CaseStore()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    import sqlite3

    conn = sqlite3.connect(str(cs.path))
    conn.row_factory = sqlite3.Row
    try:
        if redact:
            # Redact PII fields but keep the row for aggregate analytics.
            cur = conn.execute(
                """
                UPDATE cases SET
                    from_addr = '[redacted]',
                    draft_reply = NULL,
                    subject = '[redacted]'
                WHERE status = 'closed'
                  AND updated_at < ?
                """,
                (cutoff_iso,),
            )
            redacted = cur.rowcount
            # Also redact message bodies
            conn.execute(
                """
                UPDATE case_messages SET
                    from_addr = '[redacted]',
                    to_addr = '[redacted]',
                    body = '[redacted]',
                    subject = '[redacted]'
                WHERE case_id IN (
                    SELECT id FROM cases
                    WHERE status = 'closed' AND updated_at < ?
                )
                """,
                (cutoff_iso,),
            )
            conn.commit()
            return RetentionResult(
                ok=True,
                message=f"Redacted {redacted} closed cases older than {days}d",
                redacted=redacted,
                retention_days=days,
            )
        # Hard delete: messages first (FK), then cases
        conn.execute(
            """
            DELETE FROM case_messages
            WHERE case_id IN (
                SELECT id FROM cases
                WHERE status = 'closed' AND updated_at < ?
            )
            """,
            (cutoff_iso,),
        )
        cur = conn.execute(
            "DELETE FROM cases WHERE status = 'closed' AND updated_at < ?",
            (cutoff_iso,),
        )
        deleted = cur.rowcount
        conn.commit()
        return RetentionResult(
            ok=True,
            message=f"Deleted {deleted} closed cases older than {days}d",
            deleted=deleted,
            retention_days=days,
        )
    except Exception as exc:
        conn.rollback()
        return RetentionResult(ok=False, message=f"Retention purge error: {exc}")
    finally:
        conn.close()
