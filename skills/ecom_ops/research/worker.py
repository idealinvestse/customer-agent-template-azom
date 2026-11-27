"""Bounded, test-safe background worker pool for Deep Research jobs.

Invariants:
- NEVER spawns threads at module import time or inside Flask ``create_app()``.
- Respects ``AZOM_DISABLE_BACKGROUND_WORKERS=1`` and ``is_mock_mode()`` unless
  explicitly started via ``pool.start()`` or ``ensure_research_worker_started(force=True)``.
- Enforces host-wide concurrency cap (``max_concurrent_jobs = 2`` across bot + dashboard)
  via atomic ``ResearchStore.claim_next(worker_id, lease_s=120)``.
- Runs a 25s lease heartbeat loop while executing a job, recovers stale leases on
  startup, and exposes ``shutdown_research_workers()`` for ``tests/conftest.py``.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ecom_ops.integrations.search.errors import SearchConfigError
from ecom_ops.research.config import (
    is_mock_mode,
    kill_switch_active,
    load_research_config,
    research_enabled,
)
from ecom_ops.research.models import ResearchJob
from ecom_ops.research.store import ResearchStore
from ecom_ops.runtime_env import env_flag

log = logging.getLogger(__name__)

_POOLS_LOCK = threading.Lock()
_POOLS: dict[str, ResearchWorkerPool] = {}


def _default_worker_id() -> str:
    host = socket.gethostname() or "azom"
    return f"{host}-{os.getpid()}-{secrets.token_hex(4)}"


class ResearchWorkerPool:
    """SQLite-leased bounded background worker pool for ``ResearchEngine``."""

    def __init__(
        self,
        *,
        store: ResearchStore | None = None,
        engine: Any | None = None,
        max_workers: int = 1,
        lease_s: int = 120,
        heartbeat_interval_s: float = 25.0,
        poll_interval_s: float = 1.0,
        worker_id: str | None = None,
        on_job_complete: Callable[[ResearchJob], None] | None = None,
    ) -> None:
        self.store = store or ResearchStore()
        if engine is None:
            from ecom_ops.research.engine import ResearchEngine

            self.engine = ResearchEngine(store=self.store)
        else:
            self.engine = engine
        self.max_workers = max(1, min(4, int(max_workers)))
        self.lease_s = max(10, int(lease_s))
        self.heartbeat_interval_s = max(0.05, float(heartbeat_interval_s))
        self.poll_interval_s = max(0.05, float(poll_interval_s))
        self.worker_id = worker_id or _default_worker_id()
        self.on_job_complete = on_job_complete

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._threads: list[threading.Thread] = []
        self._recovered_once = False

    def is_running(self) -> bool:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            return len(self._threads) > 0

    def wake(self) -> None:
        """Signal idle worker threads to check for newly queued jobs."""
        self._wakeup.set()

    def start(self) -> bool:
        """Recover stale leases once and spawn daemon worker thread(s)."""
        if env_flag("AZOM_DISABLE_BACKGROUND_WORKERS", default=False):
            return False
        with self._lock:
            self._stop.clear()
            self._threads = [t for t in self._threads if t.is_alive()]
            if not self._recovered_once:
                try:
                    self.store.recover_stale_leases()
                except Exception as exc:
                    log.warning("Failed stale research lease recovery: %s", type(exc).__name__)
                self._recovered_once = True
            while len(self._threads) < self.max_workers:
                idx = len(self._threads) + 1
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"research-worker-{idx}",
                    daemon=True,
                )
                t.start()
                self._threads.append(t)
        self._wakeup.set()
        return True

    def stop(self, timeout: float = 2.0) -> None:
        """Cooperatively stop all worker threads and wait up to ``timeout`` seconds."""
        self._stop.set()
        self._wakeup.set()
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout=timeout)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]

    def _run_claimed_job(self, job: ResearchJob, thread_worker_id: str) -> ResearchJob:
        hb_stop = threading.Event()

        def _heartbeat_loop() -> None:
            while not hb_stop.wait(timeout=self.heartbeat_interval_s):
                if self._stop.is_set():
                    break
                try:
                    if not self.store.heartbeat(job.research_id, thread_worker_id, lease_s=self.lease_s):
                        break
                except Exception:
                    break

        hb_thread = threading.Thread(
            target=_heartbeat_loop,
            name=f"research-hb-{job.research_id}",
            daemon=True,
        )
        hb_thread.start()
        try:
            finished = self.engine.run_job(job.research_id, worker_id=thread_worker_id)
        finally:
            hb_stop.set()
            hb_thread.join(timeout=1.0)

        if self.on_job_complete is not None:
            try:
                self.on_job_complete(finished)
            except Exception as exc:
                log.warning("Research completion callback failed: %s", type(exc).__name__)
        return finished

    def _worker_loop(self) -> None:
        thread_worker_id = f"{self.worker_id}-{threading.get_ident()}"
        while not self._stop.is_set():
            max_conc = load_research_config().budgets.max_concurrent_jobs
            try:
                job = self.store.claim_next(
                    thread_worker_id,
                    lease_s=self.lease_s,
                    max_concurrent=max_conc,
                )
            except Exception as exc:
                log.warning("Research claim_next error: %s", type(exc).__name__)
                job = None

            if job is None:
                self._wakeup.wait(timeout=self.poll_interval_s)
                self._wakeup.clear()
                continue

            self._run_claimed_job(job, thread_worker_id)

    def drain_pending_sync(self, *, max_jobs: int = 20) -> int:
        """Synchronously claim and run up to ``max_jobs`` queued jobs (for tests/CLI)."""
        processed = 0
        max_conc = load_research_config().budgets.max_concurrent_jobs
        for _ in range(max(1, int(max_jobs))):
            job = self.store.claim_next(
                self.worker_id,
                lease_s=self.lease_s,
                max_concurrent=max_conc,
            )
            if job is None:
                break
            self._run_claimed_job(job, self.worker_id)
            processed += 1
        return processed


def get_research_worker_pool(
    data_dir: Path | str | None = None,
    *,
    store: ResearchStore | None = None,
    on_job_complete: Callable[[ResearchJob], None] | None = None,
) -> ResearchWorkerPool:
    """Return the per-``AZOM_DATA_DIR`` singleton ``ResearchWorkerPool`` without starting it."""
    base = Path(data_dir or os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    key = str(base.resolve())
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            resolved_store = store or ResearchStore(
                path=base / "research_jobs.sqlite3",
                artifacts_dir=base / "research_artifacts",
            )
            pool = ResearchWorkerPool(store=resolved_store, on_job_complete=on_job_complete)
            _POOLS[key] = pool
        elif on_job_complete is not None and pool.on_job_complete is None:
            pool.on_job_complete = on_job_complete
        return pool


def ensure_research_worker_started(
    *,
    data_dir: Path | str | None = None,
    force: bool = False,
    on_job_complete: Callable[[ResearchJob], None] | None = None,
) -> ResearchWorkerPool | None:
    """Lazily start the research worker pool when runtime posture permits."""
    if env_flag("AZOM_DISABLE_BACKGROUND_WORKERS", default=False):
        return None
    if not force and is_mock_mode():
        return None
    if not is_mock_mode():
        if kill_switch_active("research"):
            raise SearchConfigError(
                "Deep research is disabled by kill switch",
                code="research_kill_switch",
            )
        if not research_enabled():
            raise SearchConfigError(
                "Live deep research is disabled (requires AZOM_USE_MOCK=0 and AZOM_RESEARCH_ENABLED=1)",
                code="research_disabled",
            )
    pool = get_research_worker_pool(data_dir=data_dir, on_job_complete=on_job_complete)
    if not pool.start():
        return None
    return pool


def shutdown_research_workers(timeout: float = 2.0) -> None:
    """Stop all active ``ResearchWorkerPool`` instances and clear singletons (tests/shutdown)."""
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.stop(timeout=timeout)


__all__ = [
    "ResearchWorkerPool",
    "ensure_research_worker_started",
    "get_research_worker_pool",
    "shutdown_research_workers",
]
