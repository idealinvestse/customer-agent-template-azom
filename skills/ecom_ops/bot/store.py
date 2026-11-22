"""File-backed conversation state per Telegram chat_id."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Chat continuity: 24h idle TTL (refreshed on every set via updated_at).
# Longer than classic short bots — closer to OpenClaw day-long thread feel.
DEFAULT_TTL_SECONDS = 24 * 3600
# Keep last N role/content turns (user+assistant ≈ 15–20 exchanges).
MAX_MESSAGES = 40


def clamp_messages(messages: list[dict[str, Any]] | None, *, max_n: int = MAX_MESSAGES) -> list[dict[str, str]]:
    """Return sanitized message history capped to the newest ``max_n`` turns."""
    out: list[dict[str, str]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        content = str(m.get("content") or "")
        if role not in {"user", "assistant", "system"} or not content:
            continue
        out.append({"role": role, "content": content})
    if len(out) > max_n:
        out = out[-max_n:]
    return out


class ConversationStore:
    """Persist multi-turn bot state under AZOM_DATA_DIR/telegram_state.json."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
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
        payload = json.dumps(self._data, indent=2, ensure_ascii=False)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, chat_id: str | int) -> dict[str, Any] | None:
        key = str(chat_id)
        entry = self._data.get(key)
        if not entry:
            return None
        updated = float(entry.get("updated_at", 0))
        if time.time() - updated > self.ttl_seconds:
            self.clear(chat_id)
            return None
        out = dict(entry)
        if "messages" in out:
            out["messages"] = clamp_messages(out.get("messages"))
        return out

    def set(self, chat_id: str | int, state: dict[str, Any]) -> None:
        key = str(chat_id)
        state = dict(state)
        if "messages" in state:
            state["messages"] = clamp_messages(state.get("messages"))
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


class TelegramConversationStore:
    """Transactional Telegram conversation state stored beside durable jobs."""

    def __init__(
        self,
        database_path: Path,
        *,
        legacy_path: Path | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_path = legacy_path or self.path.with_name("telegram_state.json")
        self.ttl_seconds = ttl_seconds
        self._migrate_legacy_once()
        self.cleanup_expired()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
        finally:
            conn.close()
            for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                try:
                    if candidate.exists():
                        os.chmod(candidate, 0o600)
                except OSError:
                    pass

    def _migrate_legacy_once(self) -> None:
        with self._connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS telegram_conversations ("
                "chat_id TEXT PRIMARY KEY,state_json TEXT NOT NULL,updated_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            conn.execute("BEGIN IMMEDIATE")
            migrated = conn.execute(
                "SELECT value FROM bot_meta WHERE key='telegram_state_json_migrated'"
            ).fetchone()
            if migrated:
                conn.commit()
                return
            raw: Any = {}
            if self.legacy_path.is_file():
                try:
                    raw = json.loads(self.legacy_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raw = {}
            if isinstance(raw, dict):
                for chat_id, state in raw.items():
                    if not isinstance(state, dict):
                        continue
                    updated_at = state.get("updated_at", 0)
                    try:
                        updated_at = float(updated_at)
                    except (TypeError, ValueError):
                        updated_at = 0.0
                    if "messages" in state:
                        state = dict(state)
                        state["messages"] = clamp_messages(state.get("messages"))
                    conn.execute(
                        "INSERT OR IGNORE INTO telegram_conversations "
                        "(chat_id,state_json,updated_at) VALUES(?,?,?)",
                        (str(chat_id), json.dumps(state, ensure_ascii=False), updated_at),
                    )
            conn.execute(
                "INSERT INTO bot_meta(key,value) VALUES('telegram_state_json_migrated','1')"
            )
            conn.commit()

    def get(self, chat_id: str | int) -> dict[str, Any] | None:
        key = str(chat_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT state_json,updated_at FROM telegram_conversations WHERE chat_id=?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if time.time() - float(row["updated_at"] or 0) > self.ttl_seconds:
                conn.execute("DELETE FROM telegram_conversations WHERE chat_id=?", (key,))
                return None
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(state, dict):
            return None
        state = dict(state)
        state["updated_at"] = float(row["updated_at"])
        if "messages" in state:
            state["messages"] = clamp_messages(state.get("messages"))
        return state

    def set(self, chat_id: str | int, state: dict[str, Any]) -> None:
        value = dict(state)
        if "messages" in value:
            value["messages"] = clamp_messages(value.get("messages"))
        updated_at = time.time()
        value["updated_at"] = updated_at
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO telegram_conversations(chat_id,state_json,updated_at) "
                "VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
                "state_json=excluded.state_json,updated_at=excluded.updated_at",
                (str(chat_id), json.dumps(value, ensure_ascii=False), updated_at),
            )

    def clear(self, chat_id: str | int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM telegram_conversations WHERE chat_id=?", (str(chat_id),))

    def cleanup_expired(self) -> int:
        cutoff = time.time() - self.ttl_seconds
        with self._connection() as conn:
            result = conn.execute(
                "DELETE FROM telegram_conversations WHERE updated_at<?", (cutoff,)
            )
            return int(result.rowcount)
