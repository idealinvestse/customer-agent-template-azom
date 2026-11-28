"""Strict argument validation, UTF-8 windowing, tool-call parsing, and error/digest helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from ecom_ops.wp_ops.models import WpOpsResult, ensure_utc
from ecom_ops.wp_ops.tools.types import OPERATION_ID_RE, WP_TOOL_MAX_CALLS


def utf8_window(encoded: bytes, offset: int, limit: int) -> tuple[str, int | None]:
    """Return one strict UTF-8 window and the next byte offset.

    The slice starts and ends on a character boundary. A character longer
    than ``limit`` is returned whole, at most 4 bytes, so the cursor moves.
    ``next_offset`` is None only when the file is exhausted.
    """
    size = len(encoded)
    if offset >= size or limit < 1:
        return "", None
    start = offset
    if start > 0:
        hops = 0
        while start < size and 0x80 <= encoded[start] <= 0xBF and hops < 3:
            start += 1
            hops += 1
    if start >= size:
        return "", None
    end = min(start + limit, size)
    text = ""
    while end > start:
        try:
            text = encoded[start:end].decode()
            break
        except UnicodeDecodeError:
            end -= 1
    else:
        end = start
    if text == "" and start < size:
        end = min(start + 4, size)
        while end > start:
            try:
                text = encoded[start:end].decode()
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            end = min(start + 1, size)
            text = ""
    next_offset = end if end < size else None
    return text, next_offset


_CODE_FENCE_RE = re.compile(r"^```(?:[A-Za-z0-9_-]+)?\s*(.*?)\s*```$", re.S)


def parse_tool_call(content: str) -> tuple[str, dict[str, Any]] | None:
    """Parse the first valid ``<tool_call>{...}</tool_call>`` JSON directive from model output."""
    text = content or ""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?is)<tool_call>(.*?)</tool_call>", text):
        inner = match.group(1)
        if re.search(r"<tool_call>", inner, re.I):
            inner = inner[inner.lower().rfind("<tool_call>") + len("<tool_call>") :]
        block = inner.strip()
        fence_match = _CODE_FENCE_RE.fullmatch(block)
        if fence_match:
            block = fence_match.group(1).strip()
        if not block.startswith("{"):
            continue
        try:
            payload, _ = decoder.raw_decode(block, idx=0)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
            continue
        arguments = payload.get("arguments", {})
        if not isinstance(arguments, dict):
            continue
        return payload["name"], arguments
    return None


def error_payload(error_class: str, *, site_id: str | None = None) -> dict[str, Any]:
    safe_cls = error_class_token(error_class) or "OperationUnavailable"
    payload: dict[str, Any] = {
        "ok": False,
        "status": "denied" if safe_cls == "AccessDenied" else "unknown",
        "error_class": safe_cls,
        "live_calls": False,
    }
    if isinstance(site_id, str) and site_id.strip():
        payload["site_id"] = site_id.strip()
    return payload


def error_class_token(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", value):
        return None
    return value


def safe_failure(result: WpOpsResult) -> dict[str, Any]:
    data = result.data or {}
    error = error_payload(error_class_token(data.get("error_class")) or "OperationUnavailable")
    error["live_calls"] = data.get("live_calls") is True
    if error["error_class"] == "UnknownSite":
        error["error_class"] = "UnknownSiteOrDenied"
    if error["error_class"] == "AccessDenied":
        error["status"] = "denied"
    return error


def site_state(site: dict[str, Any]) -> str:
    if site.get("down") is True:
        return "attention"
    if site.get("healthy") is True:
        return "ok"
    return "unknown"


def result_token(value: Any) -> str:
    return value if value in {"ok", "fail", "unknown", "verified"} else "unknown"


_SHORT_TEXT_STRIP_RE = re.compile(
    r"(?i)[\x00-\x1f\x7f]|</?(?:tool_call|clarify)\b[^>]*>|<<<(?:END_)?UNTRUSTED_[^>\r\n]{0,120}>>>|<<<(?:END_)?UNTRUSTED_"
)


def short_text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(_SHORT_TEXT_STRIP_RE.sub(" ", str(value)).split())
    return cleaned[:max_length]


def safe_operation_id(value: Any) -> bool:
    return isinstance(value, str) and OPERATION_ID_RE.fullmatch(value) is not None


def payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def valid_plan(row: dict[str, Any]) -> bool:
    digest = row.get("digest")
    payload = {
        key: value
        for key, value in row.items()
        if key not in {"digest", "_scope", "_scope_digest"}
    }
    scope = row.get("_scope")
    scope_digest = row.get("_scope_digest")
    scope_ok = (
        isinstance(scope, dict)
        and isinstance(scope.get("channel"), str)
        and len(scope.get("channel")) <= 32
        and isinstance(scope.get("conversation_id"), str)
        and len(scope.get("conversation_id")) <= 120
    )
    return (
        scope_ok
        and scope_digest == payload_digest(scope)
        and isinstance(digest, str)
        and digest == payload_digest(payload)
    )


def is_expired(value: Any, *, age_seconds: int | None = None) -> bool:
    if not isinstance(value, str):
        return True
    try:
        expires = datetime.fromisoformat(value)
    except ValueError:
        return True
    expires = ensure_utc(expires)
    now = datetime.now(UTC)
    if age_seconds is not None:
        return (now - expires).total_seconds() > age_seconds
    return expires <= now


def format_results_sv(results: list[dict[str, Any]], reason: str) -> str:
    lines = [reason]
    for result in results[:WP_TOOL_MAX_CALLS]:
        site_id = result.get("site_id")
        status = result.get("status", "unknown")
        name = result.get("plan", {}).get("operation") if isinstance(result.get("plan"), dict) else None
        if name:
            lines.append(f"Förhandsgranskning för {site_id or 'sajten'} ({name}): {status}; ingen ändring utfördes.")
        else:
            lines.append(f"WordPress-verktygsresultat för {site_id or 'sajten'}: {status}.")
    return "\n".join(lines)


def parse_line_range(
    arguments: dict[str, Any], *, max_line: int = 50000
) -> tuple[int | None, int | None, bool]:
    """Validate optional ``start_line``/``end_line`` integer bounds.

    Returns ``(start_line, end_line, valid)``.
    """
    raw_start = arguments.get("start_line")
    raw_end = arguments.get("end_line")
    if raw_start is not None and (type(raw_start) is not int or raw_start < 1 or raw_start > max_line):
        return None, None, False
    if raw_end is not None and (type(raw_end) is not int or raw_end < 1 or raw_end > max_line):
        return None, None, False
    if raw_start is not None and raw_end is not None and raw_end < raw_start:
        return None, None, False
    return raw_start, raw_end, True


def optional_str_arg(arguments: dict[str, Any], key: str, *, default: str | None = None) -> tuple[str | None, bool]:
    """Read an optional string argument; returns ``(value, ok)``."""
    raw = arguments.get(key, default)
    if raw is None:
        return None, True
    if not isinstance(raw, str):
        return None, False
    return raw, True


def optional_bool_arg(arguments: dict[str, Any], key: str, *, default: bool = False) -> tuple[bool, bool]:
    """Read an optional strict bool argument (rejects ints); returns ``(value, ok)``."""
    raw = arguments.get(key, default)
    if type(raw) is not bool:
        return default, False
    return raw, True


# Backwards-compatible underscore aliases for agent_tools.py
_utf8_window = utf8_window
_parse_tool_call = parse_tool_call
_error = error_payload
_safe_failure = safe_failure
_site_state = site_state
_result = result_token
_short = short_text
_error_class = error_class_token
_safe_operation_id = safe_operation_id
_digest = payload_digest
_valid_plan = valid_plan
_expired = is_expired
_format_results_sv = format_results_sv

__all__ = [
    "_digest",
    "_error",
    "_error_class",
    "_expired",
    "_format_results_sv",
    "_parse_tool_call",
    "_result",
    "_safe_failure",
    "_safe_operation_id",
    "_short",
    "_site_state",
    "_utf8_window",
    "_valid_plan",
    "error_class_token",
    "error_payload",
    "format_results_sv",
    "is_expired",
    "optional_bool_arg",
    "optional_str_arg",
    "parse_line_range",
    "parse_tool_call",
    "payload_digest",
    "result_token",
    "safe_failure",
    "safe_operation_id",
    "short_text",
    "site_state",
    "utf8_window",
    "valid_plan",
]
