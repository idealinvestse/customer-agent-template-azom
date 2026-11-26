"""Cross-process circuit breaker for Azom search routes.

State is persisted atomically under ``AZOM_DATA_DIR/search_router_health.json``
using the same cross-platform advisory lock (fcntl/msvcrt) as ``model_router``.
No secrets, API keys, or raw queries ever enter this file.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

from ecom_ops.integrations.search.errors import (
    SearchAuthError,
    SearchConfigError,
    SearchPolicyError,
    SearchRateLimitError,
    SearchUnavailableError,
)
from ecom_ops.model_router.store import atomic_write_text, data_dir, file_lock

_BACKOFF_STEPS_S = (30, 60, 180, 300)
_READ_CACHE_TTL_S = 2.0
_HALF_OPEN_PROBE_WINDOW_S = 20.0

_CACHE_LOCK = threading.Lock()
_cache_dir: str = ""
_cache_mtime_ns: int | None = None
_cache_loaded_at: float = 0.0
_cache_data: dict[str, Any] = {}


def health_path() -> Path:
    return data_dir() / "search_router_health.json"


def _lock_path() -> Path:
    return data_dir() / "search_router_health.lock"


def epoch_path() -> Path:
    return data_dir() / "search_router_epoch"


def current_search_epoch() -> int:
    """Return a monotonically increasing integer epoch for search auth rotation."""
    epoch = 0
    ep = epoch_path()
    if ep.is_file():
        try:
            epoch = int(ep.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            epoch = 0
    sec = data_dir() / "secrets.env"
    sec_mtime = 0
    if sec.is_file():
        try:
            sec_mtime = int(sec.stat().st_mtime_ns // 1_000_000)
        except OSError:
            sec_mtime = 0
    return epoch + (sec_mtime % 1_000_000_000)


def bump_search_epoch() -> int:
    """Increment ``search_router_epoch`` so stale ``open_auth`` breakers auto-clear."""
    ep = epoch_path()
    current = 0
    if ep.is_file():
        try:
            current = int(ep.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            current = 0
    nxt = current + 1
    atomic_write_text(ep, f"{nxt}\n", mode=0o600)
    return nxt


def _route_key(route_id: str) -> str:
    return f"route:{str(route_id or '').strip().lower()}"


def _read_raw(*, use_cache: bool = True) -> dict[str, Any]:
    global _cache_dir, _cache_mtime_ns, _cache_loaded_at, _cache_data
    path = health_path()
    ddir = str(data_dir())
    now = time.monotonic()
    try:
        mtime = path.stat().st_mtime_ns if path.is_file() else None
    except OSError:
        mtime = None
    with _CACHE_LOCK:
        if (
            use_cache
            and _cache_dir == ddir
            and _cache_mtime_ns == mtime
            and (now - _cache_loaded_at) < _READ_CACHE_TTL_S
        ):
            return copy.deepcopy(_cache_data)
    if not path.is_file():
        data: dict[str, Any] = {"entries": {}}
    else:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {"entries": {}}
        except (OSError, json.JSONDecodeError):
            data = {"entries": {}}
    if not isinstance(data.get("entries"), dict):
        data["entries"] = {}
    with _CACHE_LOCK:
        _cache_dir = ddir
        _cache_mtime_ns = mtime
        _cache_loaded_at = now
        _cache_data = copy.deepcopy(data)
    return data


def _write_raw(data: dict[str, Any]) -> None:
    global _cache_dir, _cache_mtime_ns, _cache_loaded_at, _cache_data
    path = health_path()
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    with _CACHE_LOCK:
        _cache_dir = str(data_dir())
        _cache_mtime_ns = mtime
        _cache_loaded_at = time.monotonic()
        _cache_data = copy.deepcopy(data)


def _mutate(fn: Any) -> Any:
    try:
        with file_lock(_lock_path(), timeout=2.0, label="search_router_health"):
            data = _read_raw(use_cache=False)
            result = fn(data)
            _write_raw(data)
            return result
    except TimeoutError:
        return None


def allow_attempt(
    route_id: str,
    *,
    auth_epoch: int | None = None,
    bypass: bool = False,
) -> tuple[bool, str, float]:
    """Return ``(allowed, state_reason, cooldown_remaining_s)``."""
    if bypass:
        return True, "bypass", 0.0
    resolved_epoch = current_search_epoch() if auth_epoch is None else int(auth_epoch)
    now = time.time()
    rk = _route_key(route_id)
    data = _read_raw(use_cache=True)
    entries = data.get("entries") or {}

    entry = entries.get(rk)
    if not isinstance(entry, dict):
        return True, "closed", 0.0
    state = str(entry.get("state") or "closed")
    if state == "closed":
        return True, "closed", 0.0
    if state == "open_auth" and int(entry.get("auth_epoch") or 0) != resolved_epoch:
        # Auth config/epoch rotated — clear stale auth lock immediately.
        pass
    else:
        cooldown_until = float(entry.get("cooldown_until") or 0.0)
        if now < cooldown_until:
            return False, state, max(0.0, cooldown_until - now)
        probe_until = float(entry.get("probe_in_flight_until") or 0.0)
        if now < probe_until:
            return False, "half_open_busy", max(0.0, probe_until - now)

    def _claim(locked_data: dict[str, Any]) -> tuple[bool, str, float]:
        cur_entries = locked_data.setdefault("entries", {})
        t_now = time.time()
        ent = cur_entries.get(rk)
        if not isinstance(ent, dict):
            return True, "closed", 0.0
        st = str(ent.get("state") or "closed")
        if st == "closed":
            return True, "closed", 0.0
        if st == "open_auth" and int(ent.get("auth_epoch") or 0) != resolved_epoch:
            cur_entries.pop(rk, None)
            return True, "auth_epoch_cleared", 0.0
        cd = float(ent.get("cooldown_until") or 0.0)
        if t_now < cd:
            return False, st, max(0.0, cd - t_now)
        pf = float(ent.get("probe_in_flight_until") or 0.0)
        if t_now < pf:
            return False, "half_open_busy", max(0.0, pf - t_now)
        ent["state"] = "half_open"
        ent["probe_in_flight_until"] = t_now + _HALF_OPEN_PROBE_WINDOW_S
        ent["last_probe_at"] = t_now
        return True, "half_open", 0.0

    claimed = _mutate(_claim)
    if claimed is None:
        return True, "lock_fallback", 0.0
    return claimed


def record_success(route_id: str, *, auth_epoch: int | None = None) -> None:
    del auth_epoch
    rk = _route_key(route_id)
    data = _read_raw(use_cache=True)
    entries = data.get("entries") or {}
    if rk not in entries:
        return

    def _clear(locked_data: dict[str, Any]) -> None:
        ent = locked_data.setdefault("entries", {})
        ent.pop(rk, None)

    _mutate(_clear)


def record_failure(
    route_id: str,
    exc: BaseException,
    *,
    auth_epoch: int | None = None,
) -> None:
    if isinstance(exc, SearchPolicyError):
        return
    resolved_epoch = current_search_epoch() if auth_epoch is None else int(auth_epoch)
    rk = _route_key(route_id)
    err_class = type(exc).__name__
    err_code = str(getattr(exc, "code", "") or "")
    now = time.time()

    def _apply(locked_data: dict[str, Any]) -> None:
        entries = locked_data.setdefault("entries", {})

        def _bump(*, state: str, duration_s: float, threshold: int) -> None:
            ent = dict(entries.get(rk) or {})
            recent = [float(t) for t in (ent.get("recent_failures") or []) if now - float(t) <= 60.0]
            recent.append(now)
            ent["recent_failures"] = recent[-10:]
            ent["failure_count"] = int(ent.get("failure_count") or 0) + 1
            ent["last_error_class"] = err_class
            ent["last_error_code"] = err_code
            ent["probe_in_flight_until"] = 0.0
            ent["auth_epoch"] = resolved_epoch
            was_half_open = ent.get("state") == "half_open"
            if was_half_open or len(recent) >= threshold:
                consec = int(ent.get("consecutive_opens") or 0) + 1
                ent["consecutive_opens"] = consec
                ent["state"] = state
                ent["opened_at"] = now
                ent["cooldown_until"] = now + duration_s
            entries[rk] = ent

        if isinstance(exc, SearchConfigError):
            if "budget" in err_code:
                _bump(state="open", duration_s=60.0, threshold=1)
            return

        if isinstance(exc, SearchAuthError):
            _bump(state="open_auth", duration_s=300.0, threshold=1)
            return

        if isinstance(exc, SearchRateLimitError):
            ra = getattr(exc, "retry_after_s", None)
            dur = max(15.0, min(180.0, float(ra))) if isinstance(ra, (int, float)) and ra > 0 else 30.0
            _bump(state="open", duration_s=dur, threshold=1)
            return

        if isinstance(exc, SearchUnavailableError):
            ent = entries.get(rk) or {}
            consec = int(ent.get("consecutive_opens") or 0)
            dur = float(_BACKOFF_STEPS_S[min(consec, len(_BACKOFF_STEPS_S) - 1)])
            _bump(state="open", duration_s=dur, threshold=2)

    _mutate(_apply)


def status_snapshot() -> dict[str, dict[str, Any]]:
    """Return safe public breaker status per search route key."""
    now = time.time()
    data = _read_raw(use_cache=False)
    out: dict[str, dict[str, Any]] = {}
    for key, ent in sorted((data.get("entries") or {}).items()):
        if not isinstance(ent, dict):
            continue
        state = str(ent.get("state") or "closed")
        cd = float(ent.get("cooldown_until") or 0.0)
        rem = max(0, int(round(cd - now))) if state != "closed" else 0
        out[str(key)] = {
            "state": state,
            "remaining_s": rem,
            "failure_count": int(ent.get("failure_count") or 0),
            "last_error_class": str(ent.get("last_error_class") or ""),
            "last_error_code": str(ent.get("last_error_code") or ""),
        }
    return out


def reset_breaker(key: str | None = None) -> None:
    if key is None:

        def _clear_all(locked_data: dict[str, Any]) -> None:
            locked_data["entries"] = {}

        _mutate(_clear_all)
        return

    norm_key = key if key.startswith("route:") else _route_key(key)

    def _drop(locked_data: dict[str, Any]) -> None:
        ent = locked_data.setdefault("entries", {})
        ent.pop(norm_key, None)

    _mutate(_drop)


def reset_breaker_cache_for_tests() -> None:
    global _cache_dir, _cache_mtime_ns, _cache_loaded_at, _cache_data
    with _CACHE_LOCK:
        _cache_dir = ""
        _cache_mtime_ns = None
        _cache_loaded_at = 0.0
        _cache_data = {}


__all__ = [
    "allow_attempt",
    "bump_search_epoch",
    "current_search_epoch",
    "epoch_path",
    "health_path",
    "record_failure",
    "record_success",
    "reset_breaker",
    "reset_breaker_cache_for_tests",
    "status_snapshot",
]
