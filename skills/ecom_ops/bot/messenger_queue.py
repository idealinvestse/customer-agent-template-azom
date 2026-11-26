"""Durable SQLite job queue for Meta Messenger webhook turns.

Prevents Meta webhook 20s timeouts on slow LLM/WP turns, keeps `mid`
deduplication atomic across retries/restarts, and reconciles interrupted
`running` jobs on startup.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ecom_ops.bot.messenger_adapter import (
    InboundEvent,
    process_inbound,
    send_bot_reply,
    send_sender_action,
)

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_WORKERS_LOCK = threading.Lock()
_QUEUES: dict[str, MessengerJobQueue] = {}


class MessengerJobQueue:
    """SQLite-backed durable queue + bounded background worker pool."""

    def __init__(self, db_path: str | Path, *, max_workers: int = 2) -> None:
        self.db_path = Path(db_path)
        self.max_workers = max(1, min(4, int(max_workers)))
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._worker_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._handler_factory: Callable[[], Any] | None = None
        self._page_token: str | None = None
        self._dry_run: bool = True
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messenger_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mid TEXT UNIQUE,
                    peer_id TEXT NOT NULL,
                    text TEXT,
                    postback TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messenger_jobs_status ON messenger_jobs(status, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messenger_jobs_peer_status ON messenger_jobs(peer_id, status)"
            )

    def reconcile_running(self) -> int:
        """Reset interrupted ``running`` jobs back to ``pending`` after restart."""
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE messenger_jobs
                SET status = 'pending', updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            return int(cur.rowcount or 0)

    def enqueue(self, event: InboundEvent) -> tuple[bool, int | None]:
        """Insert a new job atomically. Returns ``(enqueued, job_id)`` (False if duplicate mid)."""
        now = time.time()
        mid = (event.mid or "").strip() or None
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO messenger_jobs(mid, peer_id, text, postback, status, attempts, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', 0, '', ?, ?)
                    """,
                    (mid, event.peer_id, event.text, event.postback, now, now),
                )
                return True, int(cur.lastrowid) if cur.lastrowid is not None else None
            except sqlite3.IntegrityError:
                return False, None

    def _claim_next(self) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT m.id, m.mid, m.peer_id, m.text, m.postback, m.attempts
                    FROM messenger_jobs m
                    WHERE m.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM messenger_jobs r
                          WHERE r.peer_id = m.peer_id
                            AND r.status = 'running'
                      )
                    ORDER BY m.id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """
                    UPDATE messenger_jobs
                    SET status = 'running', attempts = attempts + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                conn.execute("COMMIT")
                return dict(row)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _mark_done(self, job_id: int) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE messenger_jobs
                SET status = 'done', error = '', updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (now, now, job_id),
            )

    def _mark_failed_or_retry(self, job_id: int, attempts_done: int, exc: BaseException) -> None:
        now = time.time()
        err = f"{type(exc).__name__}: {str(exc)[:180]}"
        status = "pending" if attempts_done < _MAX_ATTEMPTS else "failed"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE messenger_jobs
                SET status = ?, error = ?, updated_at = ?, finished_at = CASE WHEN ? = 'failed' THEN ? ELSE NULL END
                WHERE id = ?
                """,
                (status, err, now, status, now, job_id),
            )

    def _execute_job(
        self,
        job: dict[str, Any],
        *,
        handler_factory: Callable[[], Any],
        page_token: str | None,
        dry_run: bool,
    ) -> bool:
        ev = InboundEvent(
            peer_id=str(job["peer_id"]),
            text=job.get("text"),
            postback=job.get("postback"),
            mid=job.get("mid"),
        )
        attempts_done = int(job.get("attempts") or 0) + 1
        try:
            send_sender_action(ev.peer_id, "typing_on", page_token=page_token, dry_run=dry_run)
            handler = handler_factory()
            reply = process_inbound(ev, handler)
            send_bot_reply(ev.peer_id, reply, dry_run=dry_run, page_token=page_token)
            self._mark_done(int(job["id"]))
            return True
        except Exception as exc:
            log.warning("Messenger job %s failed: %s", job.get("id"), type(exc).__name__)
            self._mark_failed_or_retry(int(job["id"]), attempts_done, exc)
            return False

    def drain_pending_sync(
        self,
        *,
        handler_factory: Callable[[], Any],
        page_token: str | None = None,
        dry_run: bool = False,
    ) -> tuple[int, int]:
        """Process all pending jobs synchronously (used in unit tests). Returns ``(handled, errors)``."""
        handled = 0
        errors = 0
        while True:
            job = self._claim_next()
            if job is None:
                break
            ok = self._execute_job(
                job,
                handler_factory=handler_factory,
                page_token=page_token,
                dry_run=dry_run,
            )
            if ok:
                handled += 1
            else:
                errors += 1
        return handled, errors

    def ensure_started(
        self,
        *,
        handler_factory: Callable[[], Any],
        page_token: str | None = None,
        dry_run: bool = False,
    ) -> None:
        with self._worker_lock:
            self._handler_factory = handler_factory
            self._page_token = page_token
            self._dry_run = dry_run
            alive = [t for t in self._threads if t.is_alive()]
            self._threads = alive
            if len(self._threads) == 0:
                self.reconcile_running()
            while len(self._threads) < self.max_workers:
                t = threading.Thread(target=self._worker_loop, daemon=True)
                t.start()
                self._threads.append(t)
        self._wakeup.set()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job = self._claim_next()
            if job is None:
                self._wakeup.wait(timeout=1.0)
                self._wakeup.clear()
                continue
            factory = self._handler_factory
            if factory is None:
                break
            self._execute_job(
                job,
                handler_factory=factory,
                page_token=self._page_token,
                dry_run=self._dry_run,
            )
            self._wakeup.set()

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM messenger_jobs GROUP BY status"
            ).fetchall()
        out = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        for row in rows:
            out[str(row["status"])] = int(row["n"])
        return out

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        with self._worker_lock:
            for t in self._threads:
                t.join(timeout=2.0)
            self._threads.clear()


def get_messenger_queue(data_dir: Path | str | None = None) -> MessengerJobQueue:
    base = Path(data_dir or os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    key = str(base.resolve())
    with _WORKERS_LOCK:
        q = _QUEUES.get(key)
        if q is None:
            q = MessengerJobQueue(base / "messenger_jobs.sqlite3")
            _QUEUES[key] = q
        return q
