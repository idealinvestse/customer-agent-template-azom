"""Ops readiness markers (last case poll, stale thresholds)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _data_dir() -> Path:
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def last_case_poll_path() -> Path:
    return _data_dir() / "last_case_poll.json"


def write_last_case_poll(
    *,
    ok: bool,
    errors: int = 0,
    created: int = 0,
    polled_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    path = last_case_poll_path()
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "errors": int(errors),
        "created": int(created),
        "polled_at": polled_at
        or datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def read_last_case_poll() -> dict[str, Any] | None:
    path = last_case_poll_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def poll_stale_threshold_sec() -> int:
    raw = os.environ.get("AZOM_POLL_STALE_SEC", "").strip()
    if raw.isdigit():
        return max(60, int(raw))
    # Default: 15 minutes (timer is 5 min)
    return 900


def readiness_from_last_poll() -> dict[str, Any]:
    """Build readiness slice for /health."""
    mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    marker = read_last_case_poll()
    threshold = poll_stale_threshold_sec()
    if marker is None:
        # In mock/dev, missing poll is not a hard failure
        return {
            "ok": mock,
            "stale": not mock,
            "last_poll_at": None,
            "last_poll_age_sec": None,
            "last_poll_ok": None,
            "threshold_sec": threshold,
            "detail": "no poll marker yet",
        }

    polled_at = str(marker.get("polled_at") or "")
    age: float | None = None
    try:
        raw = polled_at.replace("Z", "+00:00")
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except Exception:
        age = None

    last_ok = bool(marker.get("ok", False))
    errors = int(marker.get("errors") or 0)
    partial = bool(marker.get("partial")) or (last_ok and errors > 0)
    stale = age is None or age > threshold
    ready = last_ok and not stale and not partial
    detail = None
    if partial and not stale:
        detail = f"partial poll failure ({errors} mailbox error(s))"
    elif (not last_ok) and errors > 0:
        detail = f"last poll failed ({errors} error(s))"
    elif stale:
        detail = "poll stale or missing"
    return {
        "ok": ready,
        "stale": stale,
        "partial": partial,
        "last_poll_at": polled_at or None,
        "last_poll_age_sec": None if age is None else round(age, 1),
        "last_poll_ok": last_ok,
        "threshold_sec": threshold,
        "errors": marker.get("errors"),
        "created": marker.get("created"),
        "detail": detail,
        "failures": marker.get("failures"),
    }
