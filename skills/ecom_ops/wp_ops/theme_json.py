"""Safe structural editor for WordPress theme.json (v2/v3).

Validates JSON pointers, forbids prototype-pollution keys, checks top-level
theme.json schema constraints, and never uses regex or shell execution.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

_ALLOWED_VERSIONS = frozenset({2, 3})
_OBJECT_TOP_KEYS = ("settings", "styles", "customTemplates", "templateParts")
_FORBIDDEN_KEYS = frozenset({"__proto__", "constructor", "prototype"})
_ARRAY_INDEX_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_MAX_OPS = 128
_MAX_DEPTH = 32
_MAX_JSON_BYTES = 1024 * 1024

_V3_SKELETON: dict[str, Any] = {
    "$schema": "https://schemas.wp.org/trunk/theme.json",
    "version": 3,
    "settings": {},
    "styles": {},
}


def _unescape_rfc6901(segment: str) -> str:
    out: list[str] = []
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if ch == "~":
            if i + 1 >= n or segment[i + 1] not in ("0", "1"):
                raise ValueError(f"Invalid RFC 6901 escape in pointer segment: {segment!r}")
            out.append("~" if segment[i + 1] == "0" else "/")
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_rfc6901(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _parse_pointer(raw_path: Any) -> tuple[list[str], str]:
    if not isinstance(raw_path, str) or not raw_path or raw_path != raw_path.strip():
        raise ValueError("Pointer path must be a non-empty string without leading/trailing whitespace.")
    if any(ord(ch) < 32 for ch in raw_path):
        raise ValueError("Pointer path contains invalid control characters.")

    if raw_path.startswith("/"):
        body = raw_path[1:]
        if not body:
            raise ValueError("Root pointer '/' is not allowed.")
        raw_segments = body.split("/")
        if any(seg == "" for seg in raw_segments):
            raise ValueError(f"Malformed JSON pointer: {raw_path!r}")
        parts = [_unescape_rfc6901(seg) for seg in raw_segments]
    elif "/" in raw_path and "." not in raw_path:
        raw_segments = raw_path.split("/")
        if any(seg == "" for seg in raw_segments):
            raise ValueError(f"Malformed slash path: {raw_path!r}")
        parts = [_unescape_rfc6901(seg) for seg in raw_segments]
    elif "." in raw_path:
        parts = raw_path.split(".")
        if any(seg == "" for seg in parts):
            raise ValueError(f"Malformed dotted path: {raw_path!r}")
    else:
        parts = [raw_path]

    if not parts or len(parts) > _MAX_DEPTH:
        raise ValueError("Pointer path depth out of bounds.")
    for part in parts:
        if not part or part in {".", ".."} or part in _FORBIDDEN_KEYS:
            raise ValueError(f"Forbidden or invalid pointer segment: {part!r}")

    canonical = "/" + "/".join(_escape_rfc6901(part) for part in parts)
    return parts, canonical


def _validate_value(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise ValueError("Value nesting exceeds maximum allowed depth.")
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Non-finite float values are not valid JSON.")
        return
    if isinstance(value, list):
        for item in value:
            _validate_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key in _FORBIDDEN_KEYS:
                raise ValueError(f"Invalid or forbidden object key: {key!r}")
            _validate_value(item, depth=depth + 1)
        return
    raise ValueError(f"Unsupported value type for theme.json: {type(value).__name__}")


def _validate_theme_json_doc(doc: Any) -> int | None:
    if not isinstance(doc, dict):
        raise ValueError("theme.json root must be a JSON object.")
    version: int | None = None
    if "version" in doc:
        raw_ver = doc["version"]
        if type(raw_ver) is not int or raw_ver not in _ALLOWED_VERSIONS:
            raise ValueError("theme.json 'version' must be integer 2 or 3.")
        version = raw_ver
    for key in _OBJECT_TOP_KEYS:
        if key in doc and not isinstance(doc[key], dict):
            raise ValueError(f"theme.json '{key}' must be a JSON object when present.")
    _validate_value(doc)
    return version


def _parse_list_index(segment: str) -> int:
    if not _ARRAY_INDEX_RE.fullmatch(segment):
        raise ValueError(f"Invalid array index segment: {segment!r}")
    return int(segment)


def _apply_one(doc: dict[str, Any], parts: list[str], op: str, value: Any) -> None:
    current: Any = doc
    for seg in parts[:-1]:
        if seg == "-":
            raise ValueError("'-' is only valid as the final segment for list append.")
        if isinstance(current, dict):
            if seg not in current:
                if op == "set":
                    current[seg] = {}
                else:
                    raise ValueError(f"Path segment not found: {seg!r}")
            current = current[seg]
        elif isinstance(current, list):
            idx = _parse_list_index(seg)
            if not (0 <= idx < len(current)):
                raise ValueError(f"Array index out of bounds: {idx}")
            current = current[idx]
        else:
            raise ValueError("Cannot traverse through non-container value.")

    leaf = parts[-1]
    if isinstance(current, dict):
        if op == "set":
            current[leaf] = copy.deepcopy(value)
        elif op == "remove":
            if leaf not in current:
                raise ValueError(f"Target key does not exist: {leaf!r}")
            del current[leaf]
        return

    if isinstance(current, list):
        if op == "set":
            if leaf == "-":
                current.append(copy.deepcopy(value))
                return
            idx = _parse_list_index(leaf)
            if idx == len(current):
                current.append(copy.deepcopy(value))
            elif 0 <= idx < len(current):
                current[idx] = copy.deepcopy(value)
            else:
                raise ValueError(f"Array index out of bounds: {idx}")
            return
        if op == "remove":
            if leaf == "-":
                raise ValueError("'-' cannot be used with remove operation.")
            idx = _parse_list_index(leaf)
            if not (0 <= idx < len(current)):
                raise ValueError(f"Array index out of bounds: {idx}")
            current.pop(idx)
            return

    raise ValueError("Target parent is not an object or array.")


def apply_theme_json_operations(
    raw_json: str | None,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply validated structural set/remove operations to theme.json."""
    if raw_json is None:
        doc: dict[str, Any] = copy.deepcopy(_V3_SKELETON)
    else:
        if not isinstance(raw_json, str):
            raise ValueError("raw_json must be a string or None.")
        if len(raw_json.encode("utf-8")) > _MAX_JSON_BYTES:
            raise ValueError("theme.json input exceeds maximum allowed size.")
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid theme.json JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("theme.json root must be a JSON object.")
        doc = copy.deepcopy(parsed)

    _validate_theme_json_doc(doc)

    if not isinstance(operations, list):
        raise ValueError("operations must be a list.")
    if len(operations) > _MAX_OPS:
        raise ValueError(f"Too many operations (max {_MAX_OPS}).")

    changed_paths: list[str] = []
    for entry in operations:
        if not isinstance(entry, dict):
            raise ValueError("Each operation must be a JSON object.")
        op = entry.get("op")
        if op not in {"set", "remove"}:
            raise ValueError(f"Unsupported operation: {op!r}")
        raw_path = entry.get("path") if "path" in entry else entry.get("pointer")
        parts, canonical = _parse_pointer(raw_path)
        if op == "set":
            if "value" not in entry:
                raise ValueError("Set operation requires a 'value' field.")
            value = entry["value"]
            _validate_value(value)
        else:
            value = None
        _apply_one(doc, parts, op, value)
        changed_paths.append(canonical)

    version = _validate_theme_json_doc(doc)
    updated_json = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(updated_json.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("Updated theme.json exceeds maximum allowed size.")
    return {
        "updated_json": updated_json,
        "changed_paths": changed_paths,
        "version": version,
    }
