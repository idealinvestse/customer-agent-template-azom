"""In-process per-site locks and cross-platform advisory file locks for WordPress ops."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()


def site_lock(site_id: str) -> threading.Lock:
    """Return the process-local lock for ``site_id``."""
    with _LOCK_GUARD:
        lock = _LOCKS.get(site_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[site_id] = lock
        return lock


@contextlib.contextmanager
def hold_site(site_id: str) -> Iterator[None]:
    """Acquire ``site_lock(site_id)`` for the duration of the context."""
    lock = site_lock(site_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _chmod_quiet(path: Path, mode: int | None) -> None:
    if mode is None:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _acquire_posix_lock(fd: int, *, timeout_s: float, raise_on_timeout: bool) -> bool:
    """Acquire non-blocking POSIX ``fcntl.flock`` with bounded timeout."""
    import fcntl

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                if raise_on_timeout:
                    raise
                return False
            time.sleep(0.02)


@contextlib.contextmanager
def advisory_file_lock(
    lock_path: Path,
    *,
    timeout_s: float = 5.0,
    mode: int | None = None,
    raise_on_timeout: bool = True,
) -> Iterator[None]:
    """Cross-platform advisory exclusive file lock (``fcntl.flock`` / ``msvcrt.locking``)."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is not None:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, mode)
        _chmod_quiet(path, mode)
        fh = os.fdopen(fd, "r+b")
    else:
        fh = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0, os.SEEK_END)
            if fh.tell() < 1:
                fh.write(b"\0")
                fh.flush()
            deadline = time.monotonic() + max(0.0, float(timeout_s))
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        if raise_on_timeout:
                            raise
                        break
                    time.sleep(0.02)
        else:
            locked = _acquire_posix_lock(
                fh.fileno(),
                timeout_s=timeout_s,
                raise_on_timeout=raise_on_timeout,
            )
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass
