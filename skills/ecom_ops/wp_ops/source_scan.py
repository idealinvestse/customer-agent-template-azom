"""Pure-Python PHP/JS source scanning and semver helpers for WordPress static tools.

No external process execution, no filesystem writes, and no network I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_SEMVER_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)(?:[-+]([A-Za-z0-9_.-]+))?\s*$")


def iter_source_files(
    files: dict[str, str] | Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Normalize and deterministically sort ``(relative_path, text)`` source pairs."""
    items = files.items() if isinstance(files, dict) else files
    normalized: list[tuple[str, str]] = []
    for raw_path, raw_content in items:
        if not isinstance(raw_path, str) or not isinstance(raw_content, str):
            continue
        clean_path = raw_path.strip().replace("\\", "/").lstrip("./")
        if not clean_path:
            continue
        normalized.append((clean_path, raw_content))
    normalized.sort(key=lambda pair: pair[0])
    return normalized


def strip_comments_and_find_strings(
    text: str,
    *,
    allow_hash: bool,
) -> tuple[str, list[bool], list[int]]:
    """Replace comments with spaces (preserving newlines) and track string positions & brace depth."""
    n = len(text)
    out = list(text)
    in_string = [False] * n
    depths = [0] * n
    i = 0
    depth = 0
    state = "code"
    quote = ""
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            depths[i] = depth
            if ch in ("'", '"'):
                state = "string"
                quote = ch
                in_string[i] = True
            elif ch == "/" and nxt == "/":
                state = "line_comment"
                out[i] = " "
                out[i + 1] = " "
                depths[i + 1] = depth
                i += 2
                continue
            elif allow_hash and ch == "#":
                state = "line_comment"
                out[i] = " "
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                out[i] = " "
                out[i + 1] = " "
                depths[i + 1] = depth
                i += 2
                continue
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
                depths[i] = depth
        elif state == "string":
            in_string[i] = True
            depths[i] = depth
            if ch == "\\":
                if i + 1 < n:
                    in_string[i + 1] = True
                    depths[i + 1] = depth
                    i += 2
                    continue
            elif ch == quote:
                state = "code"
        elif state == "line_comment":
            depths[i] = depth
            if ch == "\n":
                state = "code"
            else:
                out[i] = " "
        elif state == "block_comment":
            depths[i] = depth
            if ch == "*" and nxt == "/":
                out[i] = " "
                out[i + 1] = " "
                depths[i + 1] = depth
                state = "code"
                i += 2
                continue
            if ch != "\n":
                out[i] = " "
        i += 1
    return "".join(out), in_string, depths


def line_number(text: str, pos: int) -> int:
    """Return 1-based line number for character offset ``pos`` in ``text``."""
    return text.count("\n", 0, pos) + 1


def line_at(lines: list[str], line_no: int) -> str:
    """Return stripped text for 1-based ``line_no``, or empty string if out of range."""
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1].strip()
    return ""


def read_call_args(stripped: str, open_paren_idx: int, *, limit: int = 360) -> list[str]:
    """Read top-level comma-separated argument expressions starting at ``'('``."""
    n = len(stripped)
    if open_paren_idx >= n or stripped[open_paren_idx] != "(":
        return []
    args: list[str] = []
    cur: list[str] = []
    paren_depth = 1
    bracket_depth = 0
    brace_depth = 0
    quote = ""
    i = open_paren_idx + 1
    end_limit = min(n, open_paren_idx + limit)
    while i < end_limit and paren_depth > 0:
        ch = stripped[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < end_limit:
                i += 1
                cur.append(stripped[i])
            elif ch == quote:
                quote = ""
        else:
            if ch in ("'", '"'):
                quote = ch
                cur.append(ch)
            elif ch == "(":
                paren_depth += 1
                cur.append(ch)
            elif ch == ")":
                paren_depth -= 1
                if paren_depth > 0:
                    cur.append(ch)
            elif ch == "[":
                bracket_depth += 1
                cur.append(ch)
            elif ch == "]":
                bracket_depth = max(0, bracket_depth - 1)
                cur.append(ch)
            elif ch == "{":
                brace_depth += 1
                cur.append(ch)
            elif ch == "}":
                brace_depth = max(0, brace_depth - 1)
                cur.append(ch)
            elif ch == "," and paren_depth == 1 and bracket_depth == 0 and brace_depth == 0:
                args.append(" ".join("".join(cur).split()))
                cur = []
            else:
                cur.append(ch)
        i += 1
    tail = " ".join("".join(cur).split())
    if tail:
        args.append(tail)
    return args


def unquote_literal(expr: str) -> str | None:
    """Strip matching single or double quotes from a string literal expression."""
    text = expr.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return None


def summarize_callback(expr: str) -> str:
    """Return a concise callback summary suitable for symbol tables."""
    clean = " ".join(expr.split())
    if re.match(r"^(?:static\s+)?(?:function|fn)\s*\(", clean):
        return "closure"
    if len(clean) > 64:
        return clean[:61] + "..."
    return clean


def parse_semver_key(version: str | None) -> tuple[tuple[int, ...], int, str] | None:
    """Parse ``version`` into ``(numeric_parts, stability_bit, prerelease)`` for comparison."""
    if not isinstance(version, str) or not version.strip():
        return None
    match = _SEMVER_RE.fullmatch(version.strip())
    if not match:
        return None
    nums = tuple(int(part) for part in match.group(1).split("."))
    pre = (match.group(2) or "").lower()
    stability = 0 if pre else 1
    return nums, stability, pre


def parse_version_tuple(version: str | None) -> tuple[int, ...] | None:
    """Parse the numeric tuple of a semver string, ignoring any pre-release suffix."""
    parsed = parse_semver_key(version)
    return parsed[0] if parsed is not None else None
