"""Append-only audit log for write actions (P8.3).

Records actor + action + target for every state-changing operation:
case close, case reply/send, order status update, product publish,
settings save, secrets save, escalation resolve.

Stored as JSONL in ``AZOM_DATA_DIR/audit.jsonl``. Append-only — no
deletion API (retention handled by the same purge logic as telemetry).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ecom_ops.security import redact_secrets

_lock = threading.Lock()


def _audit_path() -> Path:
    override = os.environ.get("AZOM_AUDIT_PATH")
    if override:
        return Path(override)
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "audit.jsonl"


def log_action(
    *,
    actor: str,
    action: str,
    target: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
    success: bool = True,
) -> dict[str, Any]:
    """Append an audit entry. Returns the entry dict."""
    entry = {
        "id": str(uuid.uuid4()),
        "actor": actor,
        "action": action,
        "target": target,
        "target_id": target_id,
        "success": success,
        "details": redact_secrets(details or {}),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    path = _audit_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return entry


def read_audit_log(*, limit: int = 100, actor: str | None = None) -> list[dict[str, Any]]:
    """Read recent audit entries (newest first). Optional actor filter."""
    path = _audit_path()
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                ev = json.loads(s)
            except json.JSONDecodeError:
                continue
            if actor and ev.get("actor") != actor:
                continue
            out.append(ev)
    out.reverse()  # newest first
    return out[:limit]
