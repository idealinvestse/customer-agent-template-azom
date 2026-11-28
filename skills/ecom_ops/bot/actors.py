"""Channel peer_id → RBAC actor mapping (Telegram + Messenger)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VALID_TELEGRAM_ACTORS = frozenset({"jonatan", "oscar", "agent"})
_TELEGRAM_ID_RE = re.compile(r"^-?\d{3,20}$")
_REPLACE_RETRY_DELAYS = (0.02, 0.04, 0.06, 0.08)


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


def telegram_access_path() -> Path:
    """Return path to Oscar-managed Telegram access JSON under AZOM_DATA_DIR."""
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    return base / "telegram_access.json"


def _atomic_write_0600(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt >= 4:
                    raise
                time.sleep(_REPLACE_RETRY_DELAYS[min(attempt, len(_REPLACE_RETRY_DELAYS) - 1)])
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _parse_telegram_id_tokens(
    value: list[str] | tuple[str, ...] | set[str] | str | None,
    *,
    actor: str,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_tokens = [tok for tok in re.split(r"[\s,;]+", value.strip()) if tok]
    elif isinstance(value, (list, tuple, set)):
        raw_tokens = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                raise ValueError(f"Ogiltigt Telegram User ID för {actor}: {item!r}")
            tok = str(item).strip()
            if tok:
                raw_tokens.append(tok)
    else:
        raise ValueError(f"Ogiltig lista av Telegram User ID för {actor}")

    cleaned: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens:
        if not _TELEGRAM_ID_RE.match(tok):
            raise ValueError(
                f"Ogiltigt Telegram User ID för {actor}: {tok!r} (ange siffror, 3–20 tecken)"
            )
        if tok not in seen:
            seen.add(tok)
            cleaned.append(tok)
    return cleaned


def load_telegram_access_store() -> dict[str, list[str]]:
    """Load validated Oscar-managed Telegram actor IDs from disk.

    Returns ``{}`` when missing, symlinked, or corrupt.
    """
    path = telegram_access_path()
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    if "actors" in raw:
        raw_actors = raw["actors"]
        if not isinstance(raw_actors, dict):
            return {}
    else:
        raw_actors = raw

    loaded: dict[str, list[str]] = {}
    seen_ids: dict[str, str] = {}
    for raw_actor, raw_ids in raw_actors.items():
        actor = str(raw_actor or "").strip().lower()
        if actor not in VALID_TELEGRAM_ACTORS or not isinstance(raw_ids, list):
            continue
        valid_ids: list[str] = []
        actor_seen: set[str] = set()
        for item in raw_ids:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                continue
            cid = str(item).strip()
            if not _TELEGRAM_ID_RE.match(cid):
                continue
            prev_actor = seen_ids.get(cid)
            if prev_actor is not None and prev_actor != actor:
                continue
            if cid not in actor_seen:
                actor_seen.add(cid)
                seen_ids[cid] = actor
                valid_ids.append(cid)
        if valid_ids:
            loaded[actor] = valid_ids
    return loaded


def save_telegram_access_store(
    actor_ids: dict[str, list[str] | str],
    *,
    updated_by: str = "oscar",
) -> dict[str, list[str]]:
    """Validate and atomically persist Telegram User IDs (0600 JSON)."""
    if not isinstance(actor_ids, dict):
        raise ValueError("actor_ids måste vara en dict")

    normalized: dict[str, list[str]] = {}
    seen_ids: dict[str, str] = {}
    for raw_actor, raw_val in actor_ids.items():
        actor = str(raw_actor or "").strip().lower()
        if actor not in VALID_TELEGRAM_ACTORS:
            raise ValueError(f"Ogiltig Telegram-roll: {raw_actor!r}")
        ids = _parse_telegram_id_tokens(raw_val, actor=actor)
        for cid in ids:
            prev_actor = seen_ids.get(cid)
            if prev_actor is not None and prev_actor != actor:
                raise ValueError(
                    f"Telegram User ID {cid} kan inte tilldelas flera roller ({prev_actor} och {actor})"
                )
            seen_ids[cid] = actor
        if ids:
            existing = normalized.setdefault(actor, [])
            for cid in ids:
                if cid not in existing:
                    existing.append(cid)

    payload: dict[str, Any] = {
        "version": 1,
        "actors": normalized,
        "updated_by": str(updated_by or "oscar").strip() or "oscar",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write_0600(
        telegram_access_path(),
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    return normalized


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


def effective_telegram_allowlist() -> set[str]:
    """Merge process env ``TELEGRAM_ALLOWED_CHAT_IDS`` with ``telegram_access.json``."""
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    env_ids = {p.strip() for p in raw.replace(";", ",").split(",") if p.strip()}
    store_ids = {
        cid for ids in load_telegram_access_store().values() for cid in ids
    }
    return env_ids | store_ids


def effective_telegram_actor_map() -> dict[str, str]:
    """Merge ``telegram_access.json`` with process env ``TELEGRAM_ACTOR_MAP``.

    Process env wins on conflict for the same chat ID.
    """
    merged: dict[str, str] = {}
    for actor, ids in load_telegram_access_store().items():
        for cid in ids:
            merged[cid] = actor
    merged.update(_parse_actor_map("TELEGRAM_ACTOR_MAP"))
    return merged


def parse_actor_map(channel: str = "telegram") -> dict[str, str]:
    """Return effective peer_id -> actor map for ``channel``."""
    ch = (channel or "telegram").strip().lower()
    if ch == "messenger":
        return _parse_actor_map("MESSENGER_ACTOR_MAP")
    return effective_telegram_actor_map()


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
        allow_set = bool((os.environ.get("MESSENGER_ALLOWED_PSIDS") or "").strip())
        map_set = bool(_parse_actor_map("MESSENGER_ACTOR_MAP"))
        force_key = "MESSENGER_FAIL_CLOSED"
    else:
        allow_set = bool(effective_telegram_allowlist())
        map_set = bool(effective_telegram_actor_map())
        force_key = "TELEGRAM_FAIL_CLOSED"
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

    Telegram: effective map (process ``TELEGRAM_ACTOR_MAP`` + ``telegram_access.json``)
    Messenger: ``MESSENGER_ACTOR_MAP`` / ``MESSENGER_FAIL_CLOSED``

    Fail-closed when map is non-empty, fail-closed flag set, or
    ``AZOM_USE_MOCK=0`` (prod). Empty map in mock → default ``jonatan``.
    """
    ch = (channel or "telegram").strip().lower()
    force_key = "MESSENGER_FAIL_CLOSED" if ch == "messenger" else "TELEGRAM_FAIL_CLOSED"
    mapping = parse_actor_map(ch)
    key = str(peer_id)
    force = _fail_closed_flag(force_key) or prod_channel_fail_closed()
    if key in mapping:
        return mapping[key]
    if mapping or force:
        if ch == "messenger":
            raise ChannelActorDenied(ch, peer_id)
        raise TelegramActorDenied(peer_id)
    return "jonatan"


def resolve_actor(channel: str = "telegram", peer_id: str | int = "") -> str:
    """Alias for ``resolve_channel_actor``."""
    return resolve_channel_actor(channel, peer_id)


def resolve_telegram_actor(chat_id: str | int) -> str:
    """Backward-compatible Telegram resolver."""
    return resolve_channel_actor("telegram", chat_id)


def channel_peer_allowed(channel: str, peer_id: str | int) -> bool:
    """Allowlist check.

    Empty allowlist: allow all in mock/dev; deny all when ``AZOM_USE_MOCK=0``.
    """
    ch = (channel or "telegram").strip().lower()
    if ch == "messenger":
        raw = os.environ.get("MESSENGER_ALLOWED_PSIDS", "").strip()
        if not raw:
            return not prod_channel_fail_closed()
        allowed = {p.strip() for p in raw.split(",") if p.strip()}
        return str(peer_id) in allowed

    allowed = effective_telegram_allowlist()
    if not allowed:
        return not prod_channel_fail_closed()
    return str(peer_id) in allowed


def is_chat_allowed(channel: str = "telegram", peer_id: str | int = "") -> bool:
    """Alias for ``channel_peer_allowed``."""
    return channel_peer_allowed(channel, peer_id)


def messenger_page_token_present() -> bool:
    return bool((os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip())


def messenger_mutations_allowed() -> bool:
    """Mutating Messenger actions require page token outside mock mode."""
    if _is_mock_mode():
        return True
    return messenger_page_token_present()
