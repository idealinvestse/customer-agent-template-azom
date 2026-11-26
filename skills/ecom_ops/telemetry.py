"""Local usage telemetry for billing (AI usage metering only).

V2.1 additions:
- Schema versioning (``TELEMETRY_SCHEMA_VERSION``) stamped on every event.
- ``case_id`` field on events for per-case cost attribution (P7.1).
- Rotation + retention (``rotate_telemetry``, ``purge_old_events``) (P7.2).
- Cached budget sum to avoid O(n) scan per LLM call (P3.3).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.security import redact_secrets

_lock = threading.Lock()

# Bump when breaking changes to the event schema.
TELEMETRY_SCHEMA_VERSION = 2

# Default retention for raw telemetry events (days). 0 = no purge.
DEFAULT_RETENTION_DAYS = 90


def _store_path() -> Path:
    override = os.environ.get("AZOM_TELEMETRY_PATH")
    if override:
        return Path(override)
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "telemetry.jsonl"


@dataclass
class UsageEvent:
    id: str
    action: str
    site: str
    units: float
    unit_type: str  # e.g. tokens, api_calls, ssh_cmds
    cost_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None
    schema_version: int = TELEMETRY_SCHEMA_VERSION
    actor: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["meta"] = redact_secrets(self.meta)
        return data


class Telemetry:
    def __init__(self, path: Path | None = None) -> None:
        self._explicit_path = path
        self._cost_cache: float | None = None
        self._cost_cache_mtime: float = 0.0
        self._cost_cache_path: Path | None = None

    @property
    def path(self) -> Path:
        return self._explicit_path if self._explicit_path is not None else _store_path()

    @path.setter
    def path(self, value: Path | None) -> None:
        self._explicit_path = value
        self._cost_cache = None
        self._cost_cache_path = None

    def record(
        self,
        *,
        action: str,
        site: str,
        units: float = 1.0,
        unit_type: str = "api_calls",
        cost_usd: float = 0.0,
        meta: dict[str, Any] | None = None,
        case_id: str | None = None,
        actor: str | None = None,
    ) -> UsageEvent:
        event = UsageEvent(
            id=str(uuid.uuid4()),
            action=action,
            site=site,
            units=units,
            unit_type=unit_type,
            cost_usd=cost_usd,
            meta=meta or {},
            case_id=case_id,
            actor=actor,
        )
        line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            # Invalidate cost cache when we append a cost-bearing event
            if cost_usd != 0.0:
                self._cost_cache = None
        return event

    def sum_cost_usd(self) -> float:
        """Return total cost. Uses a cached value keyed on file mtime (P3.3)."""
        target = self.path
        if not target.is_file():
            return 0.0
        try:
            mtime = target.stat().st_mtime
        except OSError:
            return 0.0
        if (
            self._cost_cache is not None
            and mtime == self._cost_cache_mtime
            and self._cost_cache_path == target
        ):
            return self._cost_cache
        total = 0.0
        with target.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    total += float(json.loads(line).get("cost_usd", 0) or 0)
                except json.JSONDecodeError:
                    continue
        self._cost_cache = total
        self._cost_cache_mtime = mtime
        self._cost_cache_path = target
        return total

    def within_budget(self, cap_usd: float) -> bool:
        return self.sum_cost_usd() < cap_usd

    def sum_cost_usd_since(self, since_iso: str) -> float:
        """Sum cost_usd for events with created_at >= since_iso."""
        if not self.path.is_file():
            return 0.0
        total = 0.0
        cutoff = since_iso
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = str(ev.get("created_at") or "")
                if ts and ts < cutoff:
                    continue
                try:
                    total += float(ev.get("cost_usd", 0) or 0)
                except (TypeError, ValueError):
                    continue
        return total

    def purge_old_events(self, *, retention_days: int | None = None) -> int:
        """Remove events older than retention_days. Returns count removed (P7.2)."""
        days = int(retention_days if retention_days is not None else DEFAULT_RETENTION_DAYS)
        if days < 1 or not self.path.is_file():
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        retry_delays = (0.02, 0.04, 0.06, 0.08)
        with _lock:
            if not self.path.is_file():
                return 0
            kept: list[str] = []
            removed = 0
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        ev = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    ts = str(ev.get("created_at") or "")
                    if ts < cutoff:
                        removed += 1
                        continue
                    kept.append(s)
            tmp = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                for attempt in range(5):
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        if attempt >= 4:
                            raise
                        time.sleep(retry_delays[min(attempt, len(retry_delays) - 1)])
                self._cost_cache = None
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        return removed


def rotate_telemetry(
    *,
    telemetry: Telemetry | None = None,
    retention_days: int | None = None,
) -> dict[str, Any]:
    """Rotate + purge old telemetry events (P7.2). Returns summary."""
    tel = telemetry or Telemetry()
    removed = tel.purge_old_events(retention_days=retention_days)
    return {
        "ok": True,
        "removed": removed,
        "retention_days": int(retention_days if retention_days is not None else DEFAULT_RETENTION_DAYS),
        "message": f"Rotated telemetry: removed {removed} old events",
    }


default_telemetry = Telemetry()


def timed_action(action: str, site: str):
    """Decorator factory for simple latency meta on telemetry."""

    def decorator(fn):
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            result = fn(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000
            default_telemetry.record(
                action=action,
                site=site,
                units=1,
                unit_type="api_calls",
                meta={"latency_ms": round(elapsed_ms, 2)},
            )
            return result

        return wrapper

    return decorator
