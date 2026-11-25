"""Pure-Python static symbol indexer for WordPress/WooCommerce theme and plugin sources.

No external process execution, no PHP execution, no network calls, and no filesystem writes.
Extracted signatures are scrubbed of secret-like literals before returning.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

_PHP_EXTENSIONS = frozenset({".php", ".phtml", ".inc"})
_JS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})

_WP_HEADERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Theme Name", re.compile(r"^[ \t/*#]*Theme Name\s*:\s*(.+)$", re.I | re.M)),
    ("Template", re.compile(r"^[ \t/*#]*Template\s*:\s*(.+)$", re.I | re.M)),
    ("Plugin Name", re.compile(r"^[ \t/*#]*Plugin Name\s*:\s*(.+)$", re.I | re.M)),
    ("Version", re.compile(r"^[ \t/*#]*Version\s*:\s*(.+)$", re.I | re.M)),
    (
        "WC requires at least",
        re.compile(r"^[ \t/*#]*WC requires at least\s*:\s*(.+)$", re.I | re.M),
    ),
    ("WC tested up to", re.compile(r"^[ \t/*#]*WC tested up to\s*:\s*(.+)$", re.I | re.M)),
)

_SECRET_NAME_RE = re.compile(
    r"(?i)(?:secret|password|passwd|passphrase|token|api[_-]?key|private[_-]?key|"
    r"consumer[_-]?(?:key|secret)|client[_-]?secret|access[_-]?key|signing[_-]?key|"
    r"webhook[_-]?secret|auth[_-]?(?:key|salt|token)|logged_in_(?:key|salt)|"
    r"secure_auth_(?:key|salt)|nonce_(?:key|salt)|db_password)"
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(\b(?:[A-Za-z0-9_]*(?:secret|password|passwd|passphrase|token|api[_-]?key|"
    r"private[_-]?key|consumer[_-]?(?:key|secret)|client[_-]?secret|access[_-]?key|"
    r"signing[_-]?key|webhook[_-]?secret|auth[_-]?(?:key|salt|token))[A-Za-z0-9_]*)"
    r"\s*(?:=|=>|:)\s*)(['\"])(?:\\.|(?!\2).)*\2"
)
_SECRET_TOKENS_RE = re.compile(
    r"(?:"
    r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{8,}\b|"
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{12,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|"
    r"\bAIza[0-9A-Za-z_-]{20,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"(?i:\bBearer\s+[A-Za-z0-9._~+/=-]{8,})|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")"
)

_PHP_TYPE_RE = re.compile(
    r"(?<![A-Za-z0-9_>$:])(?:(?:abstract|final|readonly)\s+)*"
    r"(class|interface|trait)\s+([A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)\b"
)
_PHP_FUNC_RE = re.compile(
    r"(?<![A-Za-z0-9_>$:])function\s+&?\s*([A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)\s*\("
)
_PHP_HOOK_RE = re.compile(
    r"(?<![A-Za-z0-9_>-])\b(add_action|add_filter|do_action|apply_filters)\s*\("
)
_PHP_SHORTCODE_RE = re.compile(r"(?<![A-Za-z0-9_>-])\badd_shortcode\s*\(")
_PHP_REST_RE = re.compile(r"(?<![A-Za-z0-9_>-])\bregister_rest_route\s*\(")
_PHP_BLOCK_RE = re.compile(
    r"(?<![A-Za-z0-9_>-])\bregister_block_type(?:_from_metadata)?\s*\("
)
_PHP_DEFINE_RE = re.compile(r"(?<![A-Za-z0-9_>-])\bdefine\s*\(")

_JS_EXPORT_FUNC_RE = re.compile(
    r"(?m)^[ \t]*export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
)
_JS_EXPORT_ARROW_RE = re.compile(
    r"(?m)^[ \t]*export\s+(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>"
)
_JS_EXPORT_FN_EXPR_RE = re.compile(
    r"(?m)^[ \t]*export\s+(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
    r"(?:async\s+)?function\b"
)
_JS_REGISTER_BLOCK_RE = re.compile(r"(?<![A-Za-z0-9_$.])\bregisterBlockType\s*\(")
_JS_HOOK_RE = re.compile(r"(?<![A-Za-z0-9_$])\b(addAction|addFilter)\s*\(")

_KIND_ALIASES = {
    "hook": "hook",
    "hooks": "hook",
    "function": "function",
    "functions": "function",
    "method": "method",
    "methods": "method",
    "class": "class",
    "classes": "class",
    "interface": "interface",
    "interfaces": "interface",
    "trait": "trait",
    "traits": "trait",
    "shortcode": "shortcode",
    "shortcodes": "shortcode",
    "rest_route": "rest_route",
    "rest_routes": "rest_route",
    "block": "block",
    "blocks": "block",
    "constant": "constant",
    "constants": "constant",
    "header": "header",
    "headers": "header",
}


def _iter_files(
    files: dict[str, str] | Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
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


def _scrub_signature(text: str, *, max_len: int = 160) -> str:
    collapsed = " ".join(str(text or "").split())
    cleaned = _SECRET_ASSIGN_RE.sub(r"\1'***'", collapsed)
    cleaned = _SECRET_TOKENS_RE.sub("***", cleaned)
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3] + "..."
    return cleaned


def _strip_comments_and_find_strings(
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


def _line_number(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _line_at(lines: list[str], line_no: int) -> str:
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1].strip()
    return ""


def _read_call_args(stripped: str, open_paren_idx: int, *, limit: int = 360) -> list[str]:
    """Read top-level comma-separated argument expressions starting at '('."""
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


def _unquote(expr: str) -> str | None:
    text = expr.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return None


def _summarize_callback(expr: str) -> str:
    clean = " ".join(expr.split())
    if re.match(r"^(?:static\s+)?(?:function|fn)\s*\(", clean):
        return "closure"
    if len(clean) > 64:
        return clean[:61] + "..."
    return clean


def _extract_headers(path: str, content: str, *, require_plugin_or_theme: bool) -> list[dict[str, Any]]:
    head = content[:8192]
    comment_match = re.search(r"/\*[\s\S]*?\*/", head)
    if not comment_match:
        return []
    block = comment_match.group(0)
    block_start_line = _line_number(head, comment_match.start())
    if require_plugin_or_theme and not (
        re.search(r"(?im)^[ \t/*#]*(?:Plugin|Theme)\s+Name\s*:", block)
    ):
        return []
    entries: list[dict[str, Any]] = []
    for key, pattern in _WP_HEADERS:
        match = pattern.search(block)
        if not match:
            continue
        val = match.group(1).strip().rstrip("*/").strip()
        if not val:
            continue
        line = block_start_line + block[: match.start()].count("\n")
        entries.append(
            {
                "kind": "header",
                "name": key,
                "path": path,
                "line": line,
                "signature": _scrub_signature(f"{key}: {val}"),
            }
        )
    return entries


def _extract_block_json(path: str, content: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    name = data.get("name") or data.get("title")
    if not isinstance(name, str) or not name.strip():
        return []
    clean_name = name.strip()
    parts = [f"block.json name={clean_name}"]
    title = data.get("title")
    if isinstance(title, str) and title.strip() and title.strip() != clean_name:
        parts.append(f"title={title.strip()}")
    api_version = data.get("apiVersion")
    if api_version is not None:
        parts.append(f"apiVersion={api_version}")
    line_match = re.search(r'"name"\s*:', content)
    line_no = _line_number(content, line_match.start()) if line_match else 1
    return [
        {
            "kind": "block",
            "name": clean_name,
            "path": path,
            "line": line_no,
            "signature": _scrub_signature(" ".join(parts)),
        }
    ]


def _extract_php_symbols(path: str, content: str) -> list[dict[str, Any]]:
    entries = _extract_headers(path, content, require_plugin_or_theme=True)
    stripped, in_string, depths = _strip_comments_and_find_strings(content, allow_hash=True)
    lines = stripped.splitlines()

    # Collect class/interface/trait spans mapped to their opening brace depth.
    containers: list[tuple[int, int, str]] = []  # (start_pos, body_depth, name)
    for match in _PHP_TYPE_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        kind = match.group(1).lower()
        name = match.group(2)
        line_no = _line_number(stripped, pos)
        entries.append(
            {
                "kind": kind,
                "name": name,
                "path": path,
                "line": line_no,
                "signature": _scrub_signature(_line_at(lines, line_no)),
            }
        )
        brace_idx = stripped.find("{", match.end())
        if brace_idx != -1:
            body_depth = depths[brace_idx] + 1
            containers.append((brace_idx + 1, body_depth, name))

    def _enclosing_class(pos: int) -> str | None:
        cur_depth = depths[pos]
        candidate_name: str | None = None
        for start_pos, body_depth, cname in containers:
            if start_pos > pos:
                break
            if cur_depth < body_depth:
                continue
            if min(depths[start_pos : pos + 1]) >= body_depth:
                candidate_name = cname
        return candidate_name

    for match in _PHP_FUNC_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        fn_name = match.group(1)
        line_no = _line_number(stripped, pos)
        owner = _enclosing_class(pos)
        kind = "method" if owner else "function"
        full_name = f"{owner}::{fn_name}" if owner else fn_name
        entries.append(
            {
                "kind": kind,
                "name": full_name,
                "path": path,
                "line": line_no,
                "signature": _scrub_signature(_line_at(lines, line_no)),
            }
        )

    for match in _PHP_HOOK_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        hook_fn = match.group(1)
        args = _read_call_args(stripped, match.end() - 1)
        if not args:
            continue
        hook_name = _unquote(args[0]) or args[0]
        sig = f"{hook_fn}('{hook_name}'"
        if len(args) >= 2 and hook_fn in {"add_action", "add_filter"}:
            sig += f", {_summarize_callback(args[1])}"
        sig += ")"
        entries.append(
            {
                "kind": "hook",
                "name": hook_name,
                "path": path,
                "line": _line_number(stripped, pos),
                "signature": _scrub_signature(sig),
            }
        )

    for match in _PHP_SHORTCODE_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        args = _read_call_args(stripped, match.end() - 1)
        if not args:
            continue
        tag = _unquote(args[0]) or args[0]
        cb = f", {_summarize_callback(args[1])}" if len(args) >= 2 else ""
        entries.append(
            {
                "kind": "shortcode",
                "name": tag,
                "path": path,
                "line": _line_number(stripped, pos),
                "signature": _scrub_signature(f"add_shortcode('{tag}'{cb})"),
            }
        )

    for match in _PHP_REST_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        args = _read_call_args(stripped, match.end() - 1)
        if len(args) < 2:
            continue
        ns = (_unquote(args[0]) or args[0]).strip("/")
        route = (_unquote(args[1]) or args[1]).strip("/")
        full_route = "/".join(part for part in (ns, route) if part) or "/"
        entries.append(
            {
                "kind": "rest_route",
                "name": full_route,
                "path": path,
                "line": _line_number(stripped, pos),
                "signature": _scrub_signature(f"register_rest_route({args[0]}, {args[1]})"),
            }
        )

    for match in _PHP_BLOCK_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        args = _read_call_args(stripped, match.end() - 1)
        if not args:
            continue
        block_name = _unquote(args[0]) or args[0]
        entries.append(
            {
                "kind": "block",
                "name": block_name,
                "path": path,
                "line": _line_number(stripped, pos),
                "signature": _scrub_signature(f"register_block_type({args[0]})"),
            }
        )

    for match in _PHP_DEFINE_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        args = _read_call_args(stripped, match.end() - 1)
        if not args:
            continue
        const_name = _unquote(args[0]) or args[0]
        val_expr = args[1] if len(args) >= 2 else ""
        if _SECRET_NAME_RE.search(const_name):
            val_expr = "'***'"
        sig = f"define('{const_name}', {val_expr})" if val_expr else f"define('{const_name}')"
        entries.append(
            {
                "kind": "constant",
                "name": const_name,
                "path": path,
                "line": _line_number(stripped, pos),
                "signature": _scrub_signature(sig),
            }
        )

    return entries


def _extract_js_symbols(path: str, content: str) -> list[dict[str, Any]]:
    stripped, in_string, _depths = _strip_comments_and_find_strings(content, allow_hash=False)
    lines = stripped.splitlines()
    entries: list[dict[str, Any]] = []

    for pattern in (_JS_EXPORT_FUNC_RE, _JS_EXPORT_ARROW_RE, _JS_EXPORT_FN_EXPR_RE):
        for match in pattern.finditer(stripped):
            pos = match.start()
            if in_string[pos]:
                continue
            fn_name = match.group(1)
            line_no = _line_number(stripped, pos)
            entries.append(
                {
                    "kind": "function",
                    "name": fn_name,
                    "path": path,
                    "line": line_no,
                    "signature": _scrub_signature(_line_at(lines, line_no)),
                }
            )

    for match in _JS_REGISTER_BLOCK_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        args = _read_call_args(stripped, match.end() - 1)
        if not args:
            continue
        block_name = _unquote(args[0]) or args[0]
        entries.append(
            {
                "kind": "block",
                "name": block_name,
                "path": path,
                "line": _line_number(stripped, pos),
                "signature": _scrub_signature(f"registerBlockType({args[0]})"),
            }
        )

    for match in _JS_HOOK_RE.finditer(stripped):
        pos = match.start()
        if in_string[pos]:
            continue
        hook_fn = match.group(1)
        args = _read_call_args(stripped, match.end() - 1)
        if not args:
            continue
        hook_name = _unquote(args[0]) or args[0]
        entries.append(
            {
                "kind": "hook",
                "name": hook_name,
                "path": path,
                "line": _line_number(stripped, pos),
                "signature": _scrub_signature(f"{hook_fn}('{hook_name}')"),
            }
        )

    return entries


def _extract_file_symbols(path: str, content: str) -> list[dict[str, Any]]:
    lower = path.lower()
    basename = lower.rsplit("/", 1)[-1]
    if basename == "style.css":
        return _extract_headers(path, content, require_plugin_or_theme=False)
    if basename == "block.json":
        return _extract_block_json(path, content)
    if any(lower.endswith(ext) for ext in _PHP_EXTENSIONS):
        return _extract_php_symbols(path, content)
    if any(lower.endswith(ext) for ext in _JS_EXTENSIONS):
        return _extract_js_symbols(path, content)
    return []


def index_sources(
    files: dict[str, str] | list[tuple[str, str]],
    query: str | None = None,
    symbols: list[str] | None = None,
    max_results: int = 80,
) -> dict[str, Any]:
    """Index WordPress/WooCommerce symbols across source files and filter cleanly."""
    normalized_files = _iter_files(files)
    all_entries: list[dict[str, Any]] = []
    for path, content in normalized_files:
        all_entries.extend(_extract_file_symbols(path, content))

    all_entries.sort(key=lambda item: (item["path"], int(item["line"]), item["kind"], item["name"]))

    kind_filter: set[str] | None = None
    if symbols is not None:
        kind_filter = set()
        for raw_kind in symbols:
            token = str(raw_kind or "").strip().lower()
            if token:
                kind_filter.add(_KIND_ALIASES.get(token, token))

    needle = str(query or "").strip().lower()
    filtered: list[dict[str, Any]] = []
    for entry in all_entries:
        if kind_filter is not None and entry["kind"] not in kind_filter:
            continue
        if needle:
            haystack = " ".join(
                (entry["kind"], entry["name"], entry["path"], entry["signature"])
            ).lower()
            if needle not in haystack:
                continue
        filtered.append(entry)

    cap = max(0, min(int(max_results), 500))
    returned = filtered[:cap]
    counts_by_kind: dict[str, int] = {}
    for entry in filtered:
        counts_by_kind[entry["kind"]] = counts_by_kind.get(entry["kind"], 0) + 1

    return {
        "total_files": len(normalized_files),
        "total_symbols": len(all_entries),
        "matched": len(filtered),
        "returned": len(returned),
        "truncated": len(filtered) > len(returned),
        "counts_by_kind": counts_by_kind,
        "entries": returned,
    }
