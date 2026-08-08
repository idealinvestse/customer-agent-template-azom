"""Allowlisted log / JSONL tail for dashboard central log reading."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ecom_ops.json_logging import resolve_log_dir
from ecom_ops.security import SECRET_ENV_KEYS, redact_secrets

MAX_LINES = 500
DEFAULT_LINES = 200
MAX_BYTES = 512_000

# Runtime log files under AZOM_LOG_DIR
_RUNTIME_SOURCES = frozenset(
    {
        "dashboard",
        "bot",
        "cases-poll",
        "daily-brief",
        "cli",
    }
)

# Event JSONL under AZOM_DATA_DIR
_EVENT_SOURCES: dict[str, str] = {
    "telemetry": "telemetry.jsonl",
    "audit": "audit.jsonl",
    "escalations": "escalations.jsonl",
    "probe_history": "probe_history.jsonl",
}

LOG_SOURCES = frozenset(set(_RUNTIME_SOURCES) | set(_EVENT_SOURCES))

_BEARER_RE = re.compile(
    r"(?i)\b(Bearer|token|password|secret|api[_-]?key)\s*[:=]\s*['\"]?([^\s'\"]{8,})"
)


def data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def list_sources() -> list[dict[str, Any]]:
    """Describe allowlisted sources and whether their files exist."""
    out: list[dict[str, Any]] = []
    log_dir = resolve_log_dir()
    for name in sorted(_RUNTIME_SOURCES):
        path = (log_dir / f"{name}.log") if log_dir else None
        out.append(
            {
                "id": name,
                "kind": "runtime",
                "path": str(path) if path else None,
                "exists": bool(path and path.is_file()),
            }
        )
    base = data_dir()
    for name, filename in sorted(_EVENT_SOURCES.items()):
        path = base / filename
        out.append(
            {
                "id": name,
                "kind": "event",
                "path": str(path),
                "exists": path.is_file(),
            }
        )
    return out


def resolve_source_path(source: str) -> Path | None:
    """Return path for an allowlisted source, or None if unavailable."""
    if source not in LOG_SOURCES:
        raise ValueError(f"unknown log source: {source!r}")
    if source in _EVENT_SOURCES:
        return data_dir() / _EVENT_SOURCES[source]
    log_dir = resolve_log_dir()
    if log_dir is None:
        return None
    return log_dir / f"{source}.log"


def _secret_values_from_env() -> list[str]:
    values: list[str] = []
    for key in SECRET_ENV_KEYS:
        val = (os.environ.get(key) or "").strip()
        if len(val) >= 8:
            values.append(val)
    # Common dashboard/mail secrets not always in SECRET_ENV_KEYS length filter
    for key in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_SECRET_KEY",
        "TELEGRAM_BOT_TOKEN",
        "MESSENGER_PAGE_ACCESS_TOKEN",
        "MESSENGER_APP_SECRET",
        "OPENROUTER_API_KEY",
    ):
        val = (os.environ.get(key) or "").strip()
        if len(val) >= 8:
            values.append(val)
    # Longest first so overlapping values redact fully
    values.sort(key=len, reverse=True)
    return values


def redact_log_text(text: str) -> str:
    """Redact secrets in a plain or JSON log line.

    JSON lines get key-based ``redact_secrets`` first, then env-value and
    bearer-pattern scrubbing so secrets embedded in ``message`` / ``exception``
    fields are not returned verbatim from ``/logs``.
    """
    raw = text.rstrip("\n")
    if not raw:
        return raw
    try:
        obj = json.loads(raw)
        redacted = json.dumps(redact_secrets(obj), ensure_ascii=False, default=str)
    except (json.JSONDecodeError, TypeError):
        redacted = raw
    for secret in _secret_values_from_env():
        if secret in redacted:
            redacted = redacted.replace(secret, "***REDACTED***")
    return _BEARER_RE.sub(r"\1=***REDACTED***", redacted)


def _tail_raw_lines(path: Path, *, max_lines: int, max_bytes: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    read_from = max(0, size - max_bytes)
    try:
        with path.open("rb") as fh:
            if read_from:
                fh.seek(read_from)
            data = fh.read(max_bytes + 1)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    if read_from:
        # Drop partial first line when mid-file seek
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :]
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


def read_log_source(
    source: str,
    *,
    lines: int = DEFAULT_LINES,
    query: str = "",
) -> dict[str, Any]:
    """Tail an allowlisted source and return redacted rows."""
    if source not in LOG_SOURCES:
        raise ValueError(f"unknown log source: {source!r}")
    limit = max(1, min(int(lines), MAX_LINES))
    path = resolve_source_path(source)
    q = (query or "").strip().lower()
    rows: list[dict[str, Any]] = []
    if path is None or not path.is_file():
        return {
            "ok": True,
            "source": source,
            "path": str(path) if path else None,
            "exists": False,
            "lines": 0,
            "rows": [],
        }
    raw_lines = _tail_raw_lines(path, max_lines=limit * 4 if q else limit, max_bytes=MAX_BYTES)
    for line in raw_lines:
        redacted = redact_log_text(line)
        if q and q not in redacted.lower():
            continue
        entry: dict[str, Any]
        try:
            parsed = json.loads(redacted)
            if isinstance(parsed, dict):
                entry = {"raw": redacted, "parsed": parsed}
            else:
                entry = {"raw": redacted}
        except (json.JSONDecodeError, TypeError):
            entry = {"raw": redacted}
        rows.append(entry)
    if len(rows) > limit:
        rows = rows[-limit:]
    return {
        "ok": True,
        "source": source,
        "path": str(path),
        "exists": True,
        "lines": len(rows),
        "rows": rows,
    }
