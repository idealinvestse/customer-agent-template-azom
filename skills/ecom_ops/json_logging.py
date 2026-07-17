"""Structured JSON logging setup (P6.3).

Configures ``logging`` to emit JSON-formatted lines with structured fields
(actor, action, case_id, latency_ms, etc.) for easy ingestion into
Loki/Datadog/CloudWatch.

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
from typing import Any


class JsonFormatter(logging.Formatter):
    """Emit log records as JSON lines with structured fields."""

    # Standard logrecord attrs to exclude from the extra payload
    _STD_ATTRS = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "asctime", "taskName",
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
        if record.exc_info and record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging(*, level: str | None = None, force: bool = False) -> None:
    """Configure root logger to emit JSON lines to stderr (P6.3).

    Idempotent — safe to call multiple times. Set ``AZOM_JSON_LOGGING=0`` to
    disable (keeps plain text logging for dev).
    """
    if os.environ.get("AZOM_JSON_LOGGING", "").lower() in {"0", "false", "no"}:
        return
    root = logging.getLogger()
    if root.handlers and not force:
        # Already configured — check if our handler is present
        if any(isinstance(getattr(h, "formatter", None), JsonFormatter) for h in root.handlers):
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    # Replace existing handlers to avoid duplicate output
    if force:
        root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level or os.environ.get("AZOM_LOG_LEVEL", "INFO"))
