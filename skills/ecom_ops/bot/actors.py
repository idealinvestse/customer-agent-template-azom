"""Channel peer_id → RBAC actor mapping (Telegram + Messenger)."""

from __future__ import annotations

import os


class ChannelActorDenied(Exception):
    """Raised when a peer has no mapping under a configured actor map."""

    def __init__(self, channel: str, peer_id: str | int) -> None:
        self.channel = channel
        self.peer_id = str(peer_id)
        self.chat_id = self.peer_id  # back-compat for Telegram callers
        super().__init__(
            f"{channel} peer {self.peer_id} is not mapped in actor map"
        )


class TelegramActorDenied(ChannelActorDenied):
    """Telegram-specific denial (one-arg constructor for back-compat)."""

    def __init__(self, chat_id: str | int) -> None:
        super().__init__("telegram", chat_id)


def _parse_actor_map(env_key: str) -> dict[str, str]:
    raw = os.environ.get(env_key, "").strip()
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


def _fail_closed_flag(env_key: str) -> bool:
    return os.environ.get(env_key, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_channel_actor(channel: str, peer_id: str | int) -> str:
    """
    Map channel peer_id to ecom_ops actor name.

    Telegram: TELEGRAM_ACTOR_MAP / TELEGRAM_FAIL_CLOSED
    Messenger: MESSENGER_ACTOR_MAP / MESSENGER_FAIL_CLOSED

    Fail-closed when map is non-empty (or fail-closed flag set).
    Empty map → default ``jonatan`` (dev/mock).
    """
    ch = (channel or "telegram").strip().lower()
    if ch == "messenger":
        map_key, force_key = "MESSENGER_ACTOR_MAP", "MESSENGER_FAIL_CLOSED"
    else:
        map_key, force_key = "TELEGRAM_ACTOR_MAP", "TELEGRAM_FAIL_CLOSED"

    mapping = _parse_actor_map(map_key)
    key = str(peer_id)
    force = _fail_closed_flag(force_key)
    if key in mapping:
        return mapping[key]
    if mapping or force:
        if ch == "messenger":
            raise ChannelActorDenied(ch, peer_id)
        raise TelegramActorDenied(peer_id)
    return "jonatan"


def resolve_telegram_actor(chat_id: str | int) -> str:
    """Backward-compatible Telegram resolver."""
    return resolve_channel_actor("telegram", chat_id)


def channel_peer_allowed(channel: str, peer_id: str | int) -> bool:
    """Allowlist check. Empty allowlist = allow all (dev)."""
    ch = (channel or "telegram").strip().lower()
    if ch == "messenger":
        env_key = "MESSENGER_ALLOWED_PSIDS"
    else:
        env_key = "TELEGRAM_ALLOWED_CHAT_IDS"
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return True
    allowed = {p.strip() for p in raw.split(",") if p.strip()}
    return str(peer_id) in allowed
