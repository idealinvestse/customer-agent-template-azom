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


def _is_mock_mode() -> bool:
    return os.environ.get("AZOM_USE_MOCK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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


def prod_channel_fail_closed() -> bool:
    """True when non-mock runtime should deny empty allowlist/actor-map."""
    return not _is_mock_mode()


def channel_posture(channel: str) -> dict[str, bool | str]:
    """Allowlist/actor-map posture for probes and /health."""
    ch = (channel or "telegram").strip().lower()
    if ch == "messenger":
        allow_key, map_key, force_key = (
            "MESSENGER_ALLOWED_PSIDS",
            "MESSENGER_ACTOR_MAP",
            "MESSENGER_FAIL_CLOSED",
        )
    else:
        allow_key, map_key, force_key = (
            "TELEGRAM_ALLOWED_CHAT_IDS",
            "TELEGRAM_ACTOR_MAP",
            "TELEGRAM_FAIL_CLOSED",
        )
    allow_set = bool((os.environ.get(allow_key) or "").strip())
    map_set = bool(_parse_actor_map(map_key))
    force = _fail_closed_flag(force_key) or prod_channel_fail_closed()
    fail_open = (not allow_set) or (not map_set and not force)
    return {
        "channel": ch,
        "allowlist_set": allow_set,
        "actor_map_set": map_set,
        "fail_closed": force,
        "fail_open": fail_open and _is_mock_mode(),
    }


def resolve_channel_actor(channel: str, peer_id: str | int) -> str:
    """
    Map channel peer_id to ecom_ops actor name.

    Telegram: TELEGRAM_ACTOR_MAP / TELEGRAM_FAIL_CLOSED
    Messenger: MESSENGER_ACTOR_MAP / MESSENGER_FAIL_CLOSED

    Fail-closed when map is non-empty, fail-closed flag set, or
    ``AZOM_USE_MOCK=0`` (prod). Empty map in mock → default ``jonatan``.
    """
    ch = (channel or "telegram").strip().lower()
    if ch == "messenger":
        map_key, force_key = "MESSENGER_ACTOR_MAP", "MESSENGER_FAIL_CLOSED"
    else:
        map_key, force_key = "TELEGRAM_ACTOR_MAP", "TELEGRAM_FAIL_CLOSED"

    mapping = _parse_actor_map(map_key)
    key = str(peer_id)
    force = _fail_closed_flag(force_key) or prod_channel_fail_closed()
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
    """Allowlist check.

    Empty allowlist: allow all in mock/dev; deny all when ``AZOM_USE_MOCK=0``.
    """
    ch = (channel or "telegram").strip().lower()
    if ch == "messenger":
        env_key = "MESSENGER_ALLOWED_PSIDS"
    else:
        env_key = "TELEGRAM_ALLOWED_CHAT_IDS"
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return not prod_channel_fail_closed()
    allowed = {p.strip() for p in raw.split(",") if p.strip()}
    return str(peer_id) in allowed


def messenger_page_token_present() -> bool:
    return bool((os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip())


def messenger_mutations_allowed() -> bool:
    """Mutating Messenger actions require page token outside mock mode."""
    if _is_mock_mode():
        return True
    return messenger_page_token_present()
