"""Cross-process circuit breaker for Azom model routes and slots.

State is persisted atomically under ``AZOM_DATA_DIR/model_router_health.json``
using the same cross-platform advisory lock (fcntl/msvcrt) as OAuth sessions.
No secrets, tokens, or API key hashes ever enter this file.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelRateLimitError,
    ModelUnavailableError,
)
from ecom_ops.model_router.store import atomic_write_text, data_dir, file_lock

_BACKOFF_STEPS_S = (30, 60, 180, 300)
_READ_CACHE_TTL_S = 2.0

_CACHE_LOCK = threading.Lock()
_cache_dir: str = ""
_cache_mtime_ns: int | None = None
_cache_loaded_at: float = 0.0
_cache_data: dict[str, Any] = {}


def health_path() -> Path:
    return data_dir() / "model_router_health.json"


def _lock_path() -> Path:
    return data_dir() / "model_router_health.lock"


def _route_key(route_id: str) -> str:
    rid = "openrouter" if route_id == "openrouter_api_key" else route_id
    return f"route:{rid}"


def _slot_key(route_id: str, model_id: str) -> str:
    rid = "openrouter" if route_id == "openrouter_api_key" else route_id
    return f"slot:{rid}:{model_id or 'default'}"


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
        with file_lock(_lock_path(), timeout=2.0, label="model_router_health"):
            data = _read_raw(use_cache=False)
            before = copy.deepcopy(data)
            result = fn(data)
            if data != before:
                _write_raw(data)
            return result
    except TimeoutError:
        return None


def allow_attempt(
    route_id: str,
    model_id: str,
    *,
    auth_epoch: int = 0,
    bypass: bool = False,
) -> tuple[bool, str, float]:
    """Return ``(allowed, state_reason, cooldown_remaining_s)``."""
    if bypass:
        return True, "bypass", 0.0
    now = time.time()
    rk = _route_key(route_id)
    sk = _slot_key(route_id, model_id)
    data = _read_raw(use_cache=True)
    entries = data.get("entries") or {}

    needs_probe: list[str] = []
    for key in (rk, sk):
        entry = entries.get(key)
        if not isinstance(entry, dict):
            continue
        state = str(entry.get("state") or "closed")
        if state == "closed":
            continue
        if state == "open_auth" and int(entry.get("auth_epoch") or 0) != int(auth_epoch):
            # Auth was refreshed/rotated — clear stale auth lock immediately.
            needs_probe.append(key)
            continue
        cooldown_until = float(entry.get("cooldown_until") or 0.0)
        if now < cooldown_until:
            return False, state, max(0.0, cooldown_until - now)
        probe_until = float(entry.get("probe_in_flight_until") or 0.0)
        if now < probe_until:
            return False, "half_open_busy", max(0.0, probe_until - now)
        needs_probe.append(key)

    if not needs_probe:
        return True, "closed", 0.0

    def _claim(locked_data: dict[str, Any]) -> tuple[bool, str, float]:
        cur_entries = locked_data.setdefault("entries", {})
        t_now = time.time()
        to_clear: list[str] = []
        to_probe: list[dict[str, Any]] = []
        for key in (rk, sk):
            ent = cur_entries.get(key)
            if not isinstance(ent, dict):
                continue
            st = str(ent.get("state") or "closed")
            if st == "closed":
                continue
            if st == "open_auth" and int(ent.get("auth_epoch") or 0) != int(auth_epoch):
                to_clear.append(key)
                continue
            cd = float(ent.get("cooldown_until") or 0.0)
            if t_now < cd:
                return False, st, max(0.0, cd - t_now)
            pf = float(ent.get("probe_in_flight_until") or 0.0)
            if t_now < pf:
                return False, "half_open_busy", max(0.0, pf - t_now)
            to_probe.append(ent)
        for key in to_clear:
            cur_entries.pop(key, None)
        for ent in to_probe:
            ent["state"] = "half_open"
            ent["probe_in_flight_until"] = t_now + 20.0
            ent["last_probe_at"] = t_now
        if to_probe:
            return True, "half_open", 0.0
        return True, "closed", 0.0

    claimed = _mutate(_claim)
    if claimed is None:
        return True, "lock_fallback", 0.0
    return claimed


def record_success(route_id: str, model_id: str, *, auth_epoch: int = 0) -> None:
    del auth_epoch
    rk = _route_key(route_id)
    sk = _slot_key(route_id, model_id)
    data = _read_raw(use_cache=True)
    entries = data.get("entries") or {}
    if rk not in entries and sk not in entries:
        return

    def _clear(locked_data: dict[str, Any]) -> None:
        ent = locked_data.setdefault("entries", {})
        ent.pop(rk, None)
        ent.pop(sk, None)

    _mutate(_clear)


def record_failure(
    route_id: str,
    model_id: str,
    exc: BaseException,
    *,
    auth_epoch: int = 0,
    batch_mode: bool = False,
) -> None:
    rk = _route_key(route_id)
    sk = _slot_key(route_id, model_id)
    err_class = type(exc).__name__
    err_code = str(getattr(exc, "code", "") or "")
    now = time.time()

    def _apply(locked_data: dict[str, Any]) -> None:
        entries = locked_data.setdefault("entries", {})

        def _bump(key: str, *, state: str, duration_s: float, threshold: int) -> None:
            ent = dict(entries.get(key) or {})
            recent = [float(t) for t in (ent.get("recent_failures") or []) if now - float(t) <= 60.0]
            recent.append(now)
            ent["recent_failures"] = recent[-10:]
            ent["failure_count"] = int(ent.get("failure_count") or 0) + 1
            ent["last_error_class"] = err_class
            ent["last_error_code"] = err_code
            ent["probe_in_flight_until"] = 0.0
            ent["auth_epoch"] = int(auth_epoch)
            was_half_open = ent.get("state") == "half_open"
            if was_half_open or len(recent) >= threshold:
                consec = int(ent.get("consecutive_opens") or 0) + 1
                ent["consecutive_opens"] = consec
                ent["state"] = state
                ent["opened_at"] = now
                ent["cooldown_until"] = now + duration_s
            entries[key] = ent

        if isinstance(exc, ModelConfigError):
            if err_code == "budget_exhausted":
                _bump(rk, state="open", duration_s=60.0, threshold=1)
            elif "not found" in str(exc).lower() or "binary" in err_code:
                _bump(rk, state="open", duration_s=300.0, threshold=1)
            else:
                for key in (rk, sk):
                    ent = entries.get(key)
                    if isinstance(ent, dict) and ent.get("state") == "half_open":
                        ent = dict(ent)
                        ent["probe_in_flight_until"] = 0.0
                        entries[key] = ent
            return

        if isinstance(exc, ModelAuthError):
            _bump(rk, state="open_auth", duration_s=300.0, threshold=1)
            return

        if isinstance(exc, ModelRateLimitError):
            ra = getattr(exc, "retry_after", None)
            dur = max(15.0, min(180.0, float(ra))) if isinstance(ra, (int, float)) and ra > 0 else 30.0
            _bump(sk, state="open", duration_s=dur, threshold=1)
            # If >=2 distinct models on this route hit 429 in 60s, open route briefly.
            norm_r = "openrouter" if route_id == "openrouter_api_key" else route_id
            models_429 = {
                k for k, v in entries.items()
                if k.startswith(f"slot:{norm_r}:")
                and isinstance(v, dict)
                and v.get("last_error_class") == "ModelRateLimitError"
                and now - float(v.get("opened_at") or 0.0) <= 60.0
            }
            if len(models_429) >= 2:
                _bump(rk, state="open", duration_s=60.0, threshold=1)
            return

        if isinstance(exc, ModelUnavailableError):
            if err_code == "unknown_model":
                _bump(sk, state="open", duration_s=600.0, threshold=1)
                return
            ent = entries.get(sk) or {}
            consec = int(ent.get("consecutive_opens") or 0)
            dur = float(_BACKOFF_STEPS_S[min(consec, len(_BACKOFF_STEPS_S) - 1)])
            slot_thresh = 1 if batch_mode else 2
            _bump(sk, state="open", duration_s=dur, threshold=slot_thresh)
            _bump(rk, state="open", duration_s=dur, threshold=2)

    _mutate(_apply)


def status_snapshot() -> dict[str, dict[str, Any]]:
    """Return safe public breaker status per entry key."""
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

    def _drop(locked_data: dict[str, Any]) -> None:
        ent = locked_data.setdefault("entries", {})
        if key.startswith("route:"):
            ent.pop(key, None)
            prefix = "slot:" + key.split(":", 1)[1] + ":"
            for k in list(ent):
                if k.startswith(prefix):
                    ent.pop(k, None)
        else:
            ent.pop(key, None)

    _mutate(_drop)


def reset_breaker_cache_for_tests() -> None:
    global _cache_dir, _cache_mtime_ns, _cache_loaded_at, _cache_data
    with _CACHE_LOCK:
        _cache_dir = ""
        _cache_mtime_ns = None
        _cache_loaded_at = 0.0
        _cache_data = {}
