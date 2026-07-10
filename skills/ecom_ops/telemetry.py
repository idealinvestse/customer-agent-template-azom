"""Local usage telemetry for billing (AI usage metering only)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ecom_ops.security import redact_secrets

_lock = threading.Lock()


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
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["meta"] = redact_secrets(self.meta)
        return data


class Telemetry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _store_path()

    def record(
        self,
        *,
        action: str,
        site: str,
        units: float = 1.0,
        unit_type: str = "api_calls",
        cost_usd: float = 0.0,
        meta: dict[str, Any] | None = None,
    ) -> UsageEvent:
        event = UsageEvent(
            id=str(uuid.uuid4()),
            action=action,
            site=site,
            units=units,
            unit_type=unit_type,
            cost_usd=cost_usd,
            meta=meta or {},
        )
        line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return event

    def sum_cost_usd(self) -> float:
        if not self.path.is_file():
            return 0.0
        total = 0.0
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    total += float(json.loads(line).get("cost_usd", 0) or 0)
                except json.JSONDecodeError:
                    continue
        return total

    def within_budget(self, cap_usd: float) -> bool:
        return self.sum_cost_usd() < cap_usd


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
