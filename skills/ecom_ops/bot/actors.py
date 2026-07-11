"""Telegram chat_id → RBAC actor mapping."""

from __future__ import annotations

import os


def resolve_telegram_actor(chat_id: str | int) -> str:
    """
    Map Telegram chat_id to ecom_ops actor name.

    Env TELEGRAM_ACTOR_MAP: comma-separated `chat_id:actor` pairs,
    e.g. `111:jonatan,222:oscar`. Unmapped chats default to `jonatan`
    (viewer + CASE_REPLY) for backward compatibility.
    """
    raw = os.environ.get("TELEGRAM_ACTOR_MAP", "").strip()
    mapping: dict[str, str] = {}
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            cid, actor = part.split(":", 1)
            cid, actor = cid.strip(), actor.strip().lower()
            if cid and actor:
                mapping[cid] = actor
    return mapping.get(str(chat_id), "jonatan")
