"""File-backed conversation state per Telegram chat_id."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class ConversationStore:
    """Persist multi-turn bot state under AZOM_DATA_DIR/telegram_state.json."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_seconds: int = 3600,
    ) -> None:
        if path is None:
            base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
            path = base / "telegram_state.json"
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            self._data = {}
        self.cleanup_expired()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def get(self, chat_id: str | int) -> dict[str, Any] | None:
        key = str(chat_id)
        entry = self._data.get(key)
        if not entry:
            return None
        updated = float(entry.get("updated_at", 0))
        if time.time() - updated > self.ttl_seconds:
            self.clear(chat_id)
            return None
        return dict(entry)

    def set(self, chat_id: str | int, state: dict[str, Any]) -> None:
        key = str(chat_id)
        state = dict(state)
        state["updated_at"] = time.time()
        self._data[key] = state
        self._save()

    def clear(self, chat_id: str | int) -> None:
        key = str(chat_id)
        if key in self._data:
            del self._data[key]
            self._save()

    def cleanup_expired(self) -> int:
        now = time.time()
        removed = 0
        for key in list(self._data.keys()):
            updated = float(self._data[key].get("updated_at", 0))
            if now - updated > self.ttl_seconds:
                del self._data[key]
                removed += 1
        if removed:
            self._save()
        return removed
