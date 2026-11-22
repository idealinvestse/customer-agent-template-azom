"""JSON state for check results and incidents. No SQLite and no secret values."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return state
