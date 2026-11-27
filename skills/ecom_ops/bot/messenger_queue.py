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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from ecom_ops.bot import dashboard_links
from ecom_ops.bot.messenger_adapter import (
    InboundEvent,
    process_inbound,
    send_bot_reply,
    send_sender_action,
)
from ecom_ops.bot.progress_events import sanitize_status_fragment
from ecom_ops.bot.reply import ActionButton, ActionMarkup, BotReply, with_dashboard_footer
from ecom_ops.bot.store import ConversationStore
from ecom_ops.research.config import is_mock_mode
from ecom_ops.runtime_env import env_flag

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_MESSENGER_24H_WINDOW_S = 24 * 3600
_RESEARCH_TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})
_WORKERS_LOCK = threading.Lock()
_QUEUES: dict[str, MessengerJobQueue] = {}


def _parse_iso_timestamp(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _within_messenger_24h_window(
    peer_id: str,
    *,
    data_dir: Path,
    job_created_at: str | None = None,
    now_ts: float | None = None,
) -> bool:
    """Return True if ``peer_id`` had inbound activity within Meta's 24h RESPONSE window."""
    now = float(now_ts) if now_ts is not None else time.time()
    candidates: list[float] = []

    job_ts = _parse_iso_timestamp(job_created_at)
    if job_ts is not None:
        candidates.append(job_ts)

    q_db = data_dir / "messenger_jobs.sqlite3"
    if q_db.is_file():
        try:
            conn = sqlite3.connect(str(q_db), timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT MAX(created_at) FROM messenger_jobs WHERE peer_id=?",
                    (str(peer_id),),
                ).fetchone()
                if row and row[0] is not None:
                    candidates.append(float(row[0]))
            finally:
                conn.close()
        except Exception:
            pass

    state_path = data_dir / "messenger_state.json"
    if state_path.is_file():
        try:
            conv = ConversationStore(path=state_path)
            st = conv.get(peer_id)
            if isinstance(st, dict) and st.get("updated_at") is not None:
                candidates.append(float(st["updated_at"]))
        except Exception:
            pass

    if not candidates:
        return False
    latest = max(candidates)
    return 0.0 <= (now - latest) <= _MESSENGER_24H_WINDOW_S or latest > now


def build_messenger_research_completion_reply(
    job: Any,
    *,
    data_dir: Path | str | None = None,
    store: Any | None = None,
) -> BotReply:
    """Build a concise Swedish Messenger completion reply with HTTPS dashboard link + Quick Replies."""
    base = Path(data_dir) if data_dir is not None else Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    rid = str(getattr(job, "research_id", "") or "")
    status = str(getattr(job, "status", "") or "").lower()
    spec = getattr(job, "spec", None)
    topic = sanitize_status_fragment(getattr(spec, "topic", "") if spec else "", max_len=120)
    brief_url = dashboard_links.research_brief_url(rid)

    if status in {"cancelled", "failed"}:
        quick_actions = ActionMarkup(
            rows=[
                [
                    ActionButton(label="Status", payload=f"research:status:{rid}"),
                    ActionButton(label="Ärenden", payload="cases:list"),
                ],
                *([[ActionButton(label="Öppna i dashboard", url=brief_url)]] if brief_url else []),
            ]
        )
    else:
        quick_actions = ActionMarkup(
            rows=[
                [
                    ActionButton(label="Visa brief", payload=f"research:show:{rid}"),
                    ActionButton(label="Status", payload=f"research:status:{rid}"),
                ],
                [
                    ActionButton(label="Alla jobb", payload=f"research:list:{rid}"),
                    ActionButton(label="Ärenden", payload="cases:list"),
                ],
                *([[ActionButton(label="Öppna i dashboard", url=brief_url)]] if brief_url else []),
            ]
        )

    if status == "cancelled":
        text = f"🛑 Research {rid} avbröts."
        if topic:
            text += f"\nÄmne: {topic}"
        return BotReply(text=with_dashboard_footer(text, brief_url), actions=quick_actions)

    if status == "failed":
        err_cls = sanitize_status_fragment(getattr(job, "error_class", "") or "", max_len=60)
        suffix = f" ({err_cls})" if err_cls else ""
        text = f"⚠️ Research {rid} misslyckades{suffix}."
        if topic:
            text += f"\nÄmne: {topic}"
        return BotReply(text=with_dashboard_footer(text, brief_url), actions=quick_actions)

    brief_payload: dict[str, Any] = {}
    if status in {"completed", "partial"}:
        rstore = store
        if rstore is None and (base / "research_jobs.sqlite3").is_file():
            from ecom_ops.research.store import ResearchStore

            rstore = ResearchStore(
                path=base / "research_jobs.sqlite3",
                artifacts_dir=base / "research_artifacts",
            )
        if rstore is not None:
            try:
                loaded = rstore.read_brief(rid)
                if isinstance(loaded, dict) and loaded.get("ok"):
                    brief_payload = loaded
            except Exception:
                brief_payload = {}

    brief = brief_payload.get("brief") if isinstance(brief_payload.get("brief"), dict) else {}
    header = "🔎 Delvis research klar" if status == "partial" else "🔎 Research klar"
    ev_status = str(
        brief.get("evidence_status") or getattr(job, "evidence_status", "") or "insufficient"
    )
    provenance = str(brief.get("provenance") or "mock")
    prov_label = "mock" if provenance in {"mock", "mock_fixture"} else "live"

    lines = [f"{header} ({rid}) · {ev_status} · {prov_label}"]
    if topic:
        lines.append(f"Ämne: {topic}")

    summary = str(brief.get("summary") or "").strip()
    if summary:
        if len(summary) > 1400:
            summary = summary[:1397].rstrip() + "…"
        lines.extend(["", summary])

    recs = [str(r).strip() for r in (brief.get("recommendations") or []) if str(r).strip()]
    if recs:
        lines.append("")
        lines.append("Rekommendationer:")
        for rec in recs[:3]:
            lines.append(f"• {rec[:220]}")

    cits = [c for c in (brief.get("citations") or []) if isinstance(c, dict)]
    if cits:
        lines.append("")
        lines.append(f"Källor ({len(cits)} st):")
        for cit in cits[:3]:
            cid = str(cit.get("id") or "S")
            title = str(cit.get("title") or cit.get("domain") or "")[:90]
            domain = str(cit.get("domain") or "")[:60]
            lines.append(f"[{cid}] {title} ({domain})" if domain else f"[{cid}] {title}")

    return BotReply(
        text=with_dashboard_footer("\n".join(lines), brief_url),
        actions=quick_actions,
    )


def _update_messenger_session_research_ids(
    peer_id: str,
    research_id: str,
    *,
    data_dir: Path,
) -> None:
    try:
        conv = ConversationStore(path=data_dir / "messenger_state.json")

        def _mutate(state: dict[str, Any]) -> dict[str, Any]:
            sess = dict(state.get("session") or {})
            recent = [str(x) for x in (sess.get("recent_research_ids") or []) if str(x).strip()]
            sess["last_research_id"] = research_id
            sess["recent_research_ids"] = ([research_id] + [x for x in recent if x != research_id])[:5]
            state["session"] = sess
            return state

        conv.update_atomic(peer_id, _mutate)
    except Exception:
        pass


def _delivery_result_succeeded(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("ok") is True or item.get("dry_run") is True:
        return True
    return bool(item.get("message_id")) and not item.get("error")


def deliver_messenger_research_completion(
    job: Any,
    *,
    data_dir: Path | str | None = None,
    page_token: str | None = None,
    dry_run: bool | None = None,
    store: Any | None = None,
    now_ts: float | None = None,
) -> list[dict[str, Any]] | None:
    """Push terminal Messenger research completion via ``send_bot_reply`` inside 24h window."""
    if str(getattr(job, "channel", "") or "").lower() != "messenger":
        return None
    peer_id = str(getattr(job, "peer_id", "") or "").strip()
    if not peer_id:
        return None
    status = str(getattr(job, "status", "") or "").lower()
    if status not in _RESEARCH_TERMINAL_STATUSES:
        return None
    if str(getattr(job, "delivery_state", "pending") or "pending") != "pending":
        return None

    base = Path(data_dir) if data_dir is not None else Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    rid = str(getattr(job, "research_id", "") or "")
    rstore = store
    if rstore is None and (base / "research_jobs.sqlite3").is_file():
        from ecom_ops.research.store import ResearchStore

        rstore = ResearchStore(
            path=base / "research_jobs.sqlite3",
            artifacts_dir=base / "research_artifacts",
        )

    if not _within_messenger_24h_window(
        peer_id,
        data_dir=base,
        job_created_at=getattr(job, "created_at", None),
        now_ts=now_ts,
    ):
        if rstore is not None:
            try:
                rstore.update_progress(rid, delivery_state="expired_window")
            except Exception:
                pass
        return None

    reply = build_messenger_research_completion_reply(job, data_dir=base, store=rstore)
    tok = (
        page_token
        if page_token is not None
        else ((os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip() or None)
    )
    is_dry = dry_run if dry_run is not None else (not tok)
    results = send_bot_reply(peer_id, reply, page_token=tok, dry_run=is_dry)
    _update_messenger_session_research_ids(peer_id, rid, data_dir=base)
    if rstore is not None and results and all(_delivery_result_succeeded(r) for r in results):
        try:
            rstore.update_progress(rid, delivery_state="delivered")
        except Exception:
            pass
    return results


def on_messenger_research_complete(
    job: Any,
    *,
    data_dir: Path | str | None = None,
    page_token: str | None = None,
    dry_run: bool | None = None,
) -> list[dict[str, Any]] | None:
    """Worker completion callback wrapper for Messenger research jobs."""
    return deliver_messenger_research_completion(
        job,
        data_dir=data_dir,
        page_token=page_token,
        dry_run=dry_run,
    )


class MessengerJobQueue:
    """SQLite-backed durable queue + bounded background worker pool."""

    def __init__(self, db_path: str | Path, *, max_workers: int = 2) -> None:
        self.db_path = Path(db_path)
        self.max_workers = max(1, min(4, int(max_workers)))
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._worker_lock = threading.Lock()
        self._sweep_lock = threading.Lock()
        self._last_sweep_at: float = 0.0
        self._delivery_backoff: dict[str, tuple[int, float]] = {}
        self._threads: list[threading.Thread] = []
        self._handler_factory: Callable[[], Any] | None = None
        self._page_token: str | None = None
        self._dry_run: bool = True
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

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

    def on_research_complete(self, job: Any) -> list[dict[str, Any]] | None:
        return deliver_messenger_research_completion(
            job,
            data_dir=self.db_path.parent,
            page_token=self._page_token,
            dry_run=self._dry_run,
        )

    def sweep_pending_research_deliveries(self) -> int:
        """Deliver completed Messenger research jobs still marked ``delivery_state='pending'``."""
        base = self.db_path.parent
        db_path = base / "research_jobs.sqlite3"
        if not db_path.is_file():
            return 0
        delivered = 0
        with self._sweep_lock:
            from ecom_ops.research.store import ResearchStore

            rstore = ResearchStore(
                path=db_path,
                artifacts_dir=base / "research_artifacts",
            )
            marks = ",".join("?" for _ in _RESEARCH_TERMINAL_STATUSES)
            with rstore._connection() as conn:
                rows = conn.execute(
                    f"SELECT * FROM research_jobs "
                    f"WHERE channel='messenger' AND peer_id != '' "
                    f"AND status IN ({marks}) AND delivery_state='pending' "
                    f"ORDER BY updated_at ASC LIMIT 20",
                    tuple(sorted(_RESEARCH_TERMINAL_STATUSES)),
                ).fetchall()
            now = time.monotonic()
            for row in rows:
                job = rstore._row_to_job(row)
                if job is None:
                    continue
                rid = str(job.research_id)
                attempts_done, next_at = self._delivery_backoff.get(rid, (0, 0.0))
                if now < next_at:
                    continue
                try:
                    results = deliver_messenger_research_completion(
                        job,
                        data_dir=base,
                        page_token=self._page_token,
                        dry_run=self._dry_run,
                        store=rstore,
                    )
                except Exception:
                    results = None
                refreshed = rstore.get_job(rid)
                new_state = str(getattr(refreshed, "delivery_state", "pending") or "pending")
                if new_state != "pending":
                    self._delivery_backoff.pop(rid, None)
                    if new_state == "delivered":
                        delivered += 1
                elif results is not None:
                    next_attempts = attempts_done + 1
                    if next_attempts >= 8:
                        try:
                            rstore.update_progress(rid, delivery_state="failed")
                        except Exception:
                            pass
                        self._delivery_backoff.pop(rid, None)
                    else:
                        delay = min(4.0 * (2 ** (next_attempts - 1)), 300.0)
                        self._delivery_backoff[rid] = (next_attempts, now + delay)
                else:
                    next_attempts = attempts_done + 1
                    if next_attempts >= 8:
                        try:
                            rstore.update_progress(rid, delivery_state="failed")
                        except Exception:
                            pass
                        self._delivery_backoff.pop(rid, None)
                    else:
                        delay = min(4.0 * (2 ** (next_attempts - 1)), 300.0)
                        self._delivery_backoff[rid] = (next_attempts, now + delay)
        return delivered

    def _maybe_sweep_research_deliveries(self, *, min_interval_s: float = 2.0) -> None:
        now = time.monotonic()
        if (now - self._last_sweep_at) < min_interval_s:
            return
        self._last_sweep_at = now
        try:
            self.sweep_pending_research_deliveries()
        except Exception as exc:
            log.debug("Messenger research delivery sweep failed: %s", type(exc).__name__)

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
        if not env_flag("AZOM_DISABLE_BACKGROUND_WORKERS", default=False) and not is_mock_mode():
            try:
                from ecom_ops.research.worker import ensure_research_worker_started

                ensure_research_worker_started(
                    data_dir=self.db_path.parent,
                    on_job_complete=self.on_research_complete,
                )
            except Exception as exc:
                log.warning("Messenger research worker start skipped: %s", type(exc).__name__)
        self._wakeup.set()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            self._maybe_sweep_research_deliveries()
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
