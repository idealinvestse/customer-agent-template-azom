"""Structured JSON logging setup.

Configures ``logging`` to emit JSON-formatted lines to stderr and optionally
to ``{AZOM_LOG_DIR}/{AZOM_LOG_NAME}.log`` for dashboard central log reading.

Usage::

    from ecom_ops.json_logging import configure_json_logging
    configure_json_logging()  # call once at startup

    import logging
    logger = logging.getLogger(__name__)
    logger.info("case replied", extra={"actor": "jonatan", "case_id": "abc123"})
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """Emit log records as JSON lines with structured fields."""

    # Standard logrecord attrs to exclude from the extra payload
    _STD_ATTRS = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "asctime",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Include any extra fields the caller attached
        for key, val in record.__dict__.items():
            if key not in self._STD_ATTRS and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                payload["exception"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


def _truthy_disabled(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"0", "false", "no", "off"}


def _safe_log_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.strip())
    return cleaned.strip("-_") or "azom"


def resolve_log_dir(*, create: bool = False) -> Path | None:
    """Return log directory path, or None if unavailable.

    When ``create`` is True (writer path), mkdir + write-probe; fail-soft on OSError.
    When False (reader path), return the configured path without creating it.
    """
    raw = (os.environ.get("AZOM_LOG_DIR") or "/var/log/azom").strip()
    if not raw:
        return None
    path = Path(raw)
    if not create:
        return path
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".azom-log-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return path
    except OSError:
        return None


def configure_json_logging(
    *,
    level: str | None = None,
    force: bool = False,
    log_name: str | None = None,
) -> None:
    """Configure root logger to emit JSON lines to stderr (+ optional file).

    Idempotent — safe to call multiple times. Set ``AZOM_JSON_LOGGING=0`` to
    disable (keeps whatever handlers already exist for plain-text/dev).

    File target: ``{AZOM_LOG_DIR}/{AZOM_LOG_NAME}.log``. File write is fail-soft
    when the directory is not writable (e.g. Windows without AZOM_LOG_DIR).
    """
    if _truthy_disabled(os.environ.get("AZOM_JSON_LOGGING")):
        return

    root = logging.getLogger()
    if root.handlers and not force:
        if any(isinstance(getattr(h, "formatter", None), JsonFormatter) for h in root.handlers):
            return

    formatter = JsonFormatter()
    if force:
        root.handlers.clear()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    name = _safe_log_name(log_name or os.environ.get("AZOM_LOG_NAME") or "azom")
    os.environ.setdefault("AZOM_LOG_NAME", name)
    log_dir = resolve_log_dir(create=True)
    if log_dir is not None:
        try:
            file_handler = logging.FileHandler(
                log_dir / f"{name}.log",
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            pass

    root.setLevel(level or os.environ.get("AZOM_LOG_LEVEL", "INFO"))
