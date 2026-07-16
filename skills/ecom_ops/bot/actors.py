"""Telegram chat_id → RBAC actor mapping."""

from __future__ import annotations

import os


class TelegramActorDenied(Exception):
    """Raised when a chat has no mapping under a configured TELEGRAM_ACTOR_MAP."""

    def __init__(self, chat_id: str | int) -> None:
        self.chat_id = str(chat_id)
        super().__init__(
            f"Telegram chat {self.chat_id} is not mapped in TELEGRAM_ACTOR_MAP"
        )


def _parse_actor_map() -> dict[str, str]:
    raw = os.environ.get("TELEGRAM_ACTOR_MAP", "").strip()
    mapping: dict[str, str] = {}
    if not raw:
        return mapping
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        cid, actor = part.split(":", 1)
        cid, actor = cid.strip(), actor.strip().lower()
        if cid and actor:
            mapping[cid] = actor
    return mapping


def resolve_telegram_actor(chat_id: str | int) -> str:
    """
    Map Telegram chat_id to ecom_ops actor name.

    Env TELEGRAM_ACTOR_MAP: comma-separated ``chat_id:actor`` pairs,
    e.g. ``111:jonatan,222:oscar``.

    Fail-closed (Sprint C):
    - If the map is **non-empty** and chat is unmapped → raise ``TelegramActorDenied``.
    - If the map is **empty** → default ``jonatan`` (dev/mock compatibility).

    Also force fail-closed with ``TELEGRAM_FAIL_CLOSED=1`` even when map is empty
    (unmapped/empty map denies everything until mapped).
    """
    mapping = _parse_actor_map()
    key = str(chat_id)
    force = os.environ.get("TELEGRAM_FAIL_CLOSED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if key in mapping:
        return mapping[key]
    if mapping or force:
        raise TelegramActorDenied(chat_id)
    return "jonatan"
