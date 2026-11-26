"""JSON state for check results and incidents. No SQLite and no secret values."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPLACE_RETRY_DELAYS = (0.02, 0.04, 0.06, 0.08)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: int | None = None,
    encoding: str = "utf-8",
) -> None:
    """Atomically write ``text`` to ``path`` with Windows ``PermissionError`` retry.

    Calls module-level ``os.replace`` and ``time.sleep`` directly so test
    monkeypatches on ``ecom_ops.wp_ops.store.{os.replace,time.sleep}`` apply.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(
        f"{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        tmp.write_text(text, encoding=encoding)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        for attempt in range(5):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt >= 4:
                    raise
                time.sleep(_REPLACE_RETRY_DELAYS[min(attempt, len(_REPLACE_RETRY_DELAYS) - 1)])
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "checks": {},
        "streaks": {},
        "incidents": [],
        "last_run": {},
        "on_demand": {},
        "backups": [],
        "drills": [],
        "file_edits": [],
    }


class WpOpsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return empty_state()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("wp ops state is unreadable") from exc
        if not isinstance(raw, dict):
            raise ValueError("wp ops state is unreadable")
        base = empty_state()
        for key in base:
            if key in raw:
                base[key] = raw[key]
        return base

    def update(self, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            state = self.load()
            mutate(state)
            atomic_write_text(self.path, json.dumps(state, ensure_ascii=False, indent=2))
            return state
