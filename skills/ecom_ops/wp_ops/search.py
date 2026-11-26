"""Safe regex validation, in-memory text search, and component filesystem search."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.credentials import bind_credential, redact_secret_text
from ecom_ops.wp_ops.file_paths import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_BYTES,
    SEGMENT_RE,
    SKIP_DIRS,
    PathRejected,
    TransportError,
    resolve_dir,
    resolve_file,
    sensitive_relative,
)
from ecom_ops.wp_ops.file_transport import transport_list_entries, transport_open_batch
from ecom_ops.wp_ops.models import Site, WpOpsResult

MAX_CSS_SEARCH_BYTES = 512 * 1024
CSS_SEARCH_RESULT_BYTES = 8192
SEARCH_FILES = 80
SEARCH_BYTES = 512 * 1024
SEARCH_HITS = 20
SEARCH_MODES = frozenset({"literal", "regex"})

_QUANT_BRACE_RE = re.compile(r"\{(\d+)(?:,(\d*))?\}")
_BACKREF_RE = re.compile(r"(?<!\\)(?:\\\\)*(\\[1-9]|\(\?P=)")


def _scrub_search_text(text: str, extra_secrets: list[str] | None = None) -> str:
    return redact_secret_text(text, extra_secrets)


def _parse_group_quantifier(pattern: str, index: int) -> tuple[bool, int | None, bool]:
    """Inspect an optional quantifier immediately following a closing parenthesis."""
    n = len(pattern)
    if index >= n:
        return False, 1, False
    ch = pattern[index]
    if ch in ("+", "*"):
        return True, None, ch == "*"
    if ch == "?":
        return False, 1, True
    if ch == "{":
        match = _QUANT_BRACE_RE.match(pattern, index)
        if match is not None:
            lo = int(match.group(1))
            hi_raw = match.group(2)
            if hi_raw is None:
                return lo > 1, lo, False
            if hi_raw == "":
                return True, None, False
            hi_val = int(hi_raw)
            return hi_val > 1, hi_val, (lo == 0 or hi_val != lo)
    return False, 1, False


def _validate_quantified_alternation(
    pattern: str,
    branch_starts: list[int],
    group_end: int,
) -> None:
    branches: list[str] = []
    for idx, b_start in enumerate(branch_starts):
        b_end = (branch_starts[idx + 1] - 1) if (idx + 1 < len(branch_starts)) else group_end
        branches.append(pattern[b_start:b_end])
    first_chars: set[str] = set()
    for branch in branches:
        if not branch or any(
            tok in branch for tok in (".", "[", "\\w", "\\d", "\\s", "\\W", "\\D", "\\S")
        ):
            raise ValueError("Unsafe regular expression: ambiguous quantified alternation rejected.")
        lead = branch[0]
        if lead in first_chars:
            raise ValueError("Unsafe regular expression: overlapping quantified alternation rejected.")
        first_chars.add(lead)


def validate_safe_regex(pattern: str) -> None:
    """Reject regex patterns prone to catastrophic backtracking (ReDoS)."""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("Invalid regular expression.")
    if _BACKREF_RE.search(pattern):
        raise ValueError("Unsafe regular expression: backreferences are not allowed.")

    # Stack entries: [has_repeat_quant, has_alt, start_index, branch_starts, has_optional]
    stack: list[list[Any]] = []
    in_class = False
    quant_count = 0
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            continue
        if ch == "(":
            if len(stack) >= 6:
                raise ValueError("Unsafe regular expression: group nesting too deep.")
            start = i + 1
            advance = 1
            if i + 1 < n and pattern[i + 1] == "?":
                if i + 2 < n and pattern[i + 2] in (":", "=", "!", "<"):
                    start = i + 3
                    advance = 3
                else:
                    advance = 2
            stack.append([False, False, start, [start], False])
            i += advance
            continue
        if ch == "|":
            if stack:
                stack[-1][1] = True
                stack[-1][3].append(i + 1)
            i += 1
            continue
        if ch == ")":
            if not stack:
                i += 1
                continue
            had_quant, had_alt, _g_start, branch_starts, had_optional = stack.pop()
            group_end = i
            i += 1
            is_repeat, max_rep, is_optional_after = _parse_group_quantifier(pattern, i)
            if is_repeat:
                if had_quant:
                    raise ValueError("Unsafe regular expression: nested quantifier rejected.")
                if had_optional and (max_rep is None or max_rep >= 6):
                    raise ValueError("Unsafe regular expression: quantified optional group rejected.")
                if max_rep is not None and max_rep > 50:
                    raise ValueError("Unsafe regular expression: excessive group repetition rejected.")
                if had_alt and (max_rep is None or max_rep >= 6):
                    _validate_quantified_alternation(pattern, branch_starts, group_end)
            if stack:
                if had_quant or is_repeat:
                    stack[-1][0] = True
                if had_optional or is_optional_after:
                    stack[-1][4] = True
            continue
        if ch == "?":
            if stack:
                stack[-1][4] = True
            i += 1
            continue
        if ch in ("+", "*"):
            quant_count += 1
            if quant_count > 16:
                raise ValueError("Unsafe regular expression: too many quantifiers.")
            if stack:
                stack[-1][0] = True
                if ch == "*":
                    stack[-1][4] = True
            i += 1
            continue
        if ch == "{":
            m_brace = _QUANT_BRACE_RE.match(pattern, i)
            if m_brace is not None:
                lo = int(m_brace.group(1))
                hi_raw = m_brace.group(2)
                repeats = (lo > 1) if hi_raw is None else (True if hi_raw == "" else int(hi_raw) > 1)
                variable = hi_raw is not None and (hi_raw == "" or int(hi_raw) != lo)
                if repeats:
                    quant_count += 1
                    if quant_count > 16:
                        raise ValueError("Unsafe regular expression: too many quantifiers.")
                    if stack:
                        stack[-1][0] = True
                if (lo == 0 or variable) and stack:
                    stack[-1][4] = True
                i = m_brace.end()
                continue
        i += 1


def _compile_search_matcher(
    query: str,
    *,
    mode: str,
    case_insensitive: bool,
) -> re.Pattern[str] | None:
    if mode not in SEARCH_MODES:
        raise ValueError("Invalid search mode.")
    if not isinstance(query, str) or len(query) < 2 or len(query) > 80:
        raise ValueError("Search text must be 2 to 80 characters.")
    if mode == "literal":
        return None
    validate_safe_regex(query)
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        return re.compile(query, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc.msg}") from exc


def _normalize_extensions(
    extensions: list[str] | None,
    *,
    allowed: frozenset[str] | None = None,
) -> frozenset[str] | None:
    if extensions is None:
        return allowed
    if not isinstance(extensions, list) or not extensions:
        raise ValueError("Invalid extensions filter.")
    normalized: list[str] = []
    for item in extensions:
        if not isinstance(item, str):
            raise ValueError("Invalid extensions filter.")
        ext = item.strip().lower()
        if not ext or any(ch in ext for ch in ("/", "\\", " ")):
            raise ValueError("Invalid extensions filter.")
        if not ext.startswith("."):
            ext = "." + ext
        if allowed is not None and ext not in allowed:
            raise ValueError("Invalid extensions filter.")
        normalized.append(ext)
    return frozenset(normalized)


def _match_text_lines(
    rel: str,
    scrubbed_body: str,
    *,
    query: str,
    compiled: re.Pattern[str] | None,
    case_insensitive: bool,
    context_lines: int,
    remaining: int,
) -> list[dict[str, Any]]:
    if remaining <= 0:
        return []
    lines = scrubbed_body.splitlines()
    needle = query.lower() if case_insensitive else query
    out: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        if compiled is not None:
            matched = compiled.search(line) is not None
        else:
            haystack = line.lower() if case_insensitive else line
            matched = needle in haystack
        if not matched:
            continue
        excerpt = line.strip()[:120]
        hit: dict[str, Any] = {"path": rel, "line": idx + 1, "excerpt": excerpt}
        if context_lines > 0:
            hit["before"] = [
                lines[j].strip()[:120] for j in range(max(0, idx - context_lines), idx)
            ]
            hit["after"] = [
                lines[j].strip()[:120]
                for j in range(idx + 1, min(len(lines), idx + 1 + context_lines))
            ]
        out.append(hit)
        if len(out) >= remaining:
            break
    return out


def search_texts(
    files: dict[str, str],
    query: str,
    *,
    mode: str = "literal",
    context_lines: int = 0,
    case_insensitive: bool = True,
    extensions: list[str] | None = None,
    path_prefix: str | None = None,
    max_matches: int = 40,
    extra_secrets: list[str] | None = None,
) -> dict[str, Any]:
    """Search in-memory text files with literal/regex matching, context, and secret scrubbing."""
    if type(context_lines) is not int:
        raise ValueError("Invalid context_lines.")
    ctx = max(0, min(3, context_lines))
    if type(case_insensitive) is not bool:
        raise ValueError("Invalid case_insensitive flag.")
    if type(max_matches) is not int:
        raise ValueError("Invalid max_matches.")
    limit = max(1, min(200, max_matches))
    compiled = _compile_search_matcher(query, mode=mode, case_insensitive=case_insensitive)
    allowed_suffixes = _normalize_extensions(extensions, allowed=None)

    clean_prefix = ""
    if path_prefix is not None:
        if not isinstance(path_prefix, str):
            raise ValueError("Invalid path_prefix.")
        clean_prefix = path_prefix.strip().replace("\\", "/").strip("/")
        if clean_prefix and (
            any(part in {"", ".", ".."} for part in clean_prefix.split("/"))
            or sensitive_relative(clean_prefix)
        ):
            raise ValueError("Invalid path_prefix.")

    hits: list[dict[str, Any]] = []
    files_scanned = 0
    truncated = False
    for rel in sorted(files):
        body = files[rel]
        if not isinstance(rel, str) or not isinstance(body, str):
            continue
        clean_rel = rel.strip().replace("\\", "/").lstrip("/")
        if not clean_rel or sensitive_relative(clean_rel):
            continue
        if clean_prefix and clean_rel != clean_prefix and not clean_rel.startswith(clean_prefix + "/"):
            continue
        suffix = Path(clean_rel).suffix.lower()
        if allowed_suffixes is not None and suffix not in allowed_suffixes:
            continue
        if "\x00" in body or len(body.encode("utf-8")) > MAX_FILE_BYTES:
            continue
        if len(hits) >= limit:
            truncated = True
            break
        files_scanned += 1
        scrubbed = _scrub_search_text(body, extra_secrets)
        found = _match_text_lines(
            clean_rel,
            scrubbed,
            query=query,
            compiled=compiled,
            case_insensitive=case_insensitive,
            context_lines=ctx,
            remaining=limit - len(hits),
        )
        hits.extend(found)
        if len(hits) >= limit:
            truncated = True
            break
    return {
        "hits": hits,
        "truncated": truncated,
        "files_scanned": files_scanned,
        "mode": mode,
        "context_lines": ctx,
    }


def _read_candidate_text(
    transport: Any,
    resolved_abs: str,
    suffix: str,
) -> tuple[str | None, bool]:
    """Read a candidate file for search, allowing bounded larger CSS reads."""
    large_css_used = False
    try:
        raw_bytes = transport.read_bytes(resolved_abs)
    except TransportError as exc:
        read_css = getattr(transport, "read_css_search_bytes", None)
        if exc.error_class == "FileTooLarge" and suffix == ".css" and callable(read_css):
            try:
                raw_bytes = read_css(resolved_abs, max_bytes=MAX_CSS_SEARCH_BYTES)
                large_css_used = True
            except (PathRejected, TransportError):
                return None, False
        else:
            return None, False
    except PathRejected:
        return None, False

    if b"\x00" in raw_bytes:
        return None, False
    if not large_css_used and len(raw_bytes) > MAX_FILE_BYTES:
        if suffix == ".css" and len(raw_bytes) <= MAX_CSS_SEARCH_BYTES:
            large_css_used = True
        else:
            return None, False
    try:
        return raw_bytes.decode("utf-8"), large_css_used
    except UnicodeDecodeError:
        return None, False


def _bound_css_hits(
    existing_hits: list[dict[str, Any]],
    matched: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    used_bytes = len(json.dumps(existing_hits, ensure_ascii=False).encode("utf-8"))
    bounded: list[dict[str, Any]] = []
    truncated = False
    for item in matched:
        item_size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if used_bytes + item_size > CSS_SEARCH_RESULT_BYTES:
            truncated = True
            break
        bounded.append(item)
        used_bytes += item_size
    return bounded, truncated


class ComponentSearcher:
    """Bounded, secret-scrubbed literal/regex search across a WordPress component."""

    def __init__(self, editor: Any) -> None:
        self.editor = editor

    def _validate_args(
        self,
        *,
        site: Site,
        kind: str,
        slug: str,
        relative: str,
        query: str,
        mode: str,
        context_lines: int,
        case_insensitive: bool,
        extensions: list[str] | None,
    ) -> tuple[tuple[str, re.Pattern[str] | None, int, frozenset[str], str] | None, WpOpsResult | None]:
        editor = self.editor
        if mode not in SEARCH_MODES:
            return None, editor.fail_result("InvalidToolArguments", "Invalid search mode.")
        if type(context_lines) is not int:
            return None, editor.fail_result("InvalidToolArguments", "Invalid context_lines.")
        ctx = max(0, min(3, context_lines))
        if type(case_insensitive) is not bool:
            return None, editor.fail_result("InvalidToolArguments", "Invalid case_insensitive flag.")
        text = str(query or "")
        if len(text) < 2 or len(text) > 80:
            return None, editor.fail_result("InvalidSearch", "Search text must be 2 to 80 characters.")
        try:
            compiled = _compile_search_matcher(text, mode=mode, case_insensitive=case_insensitive)
        except ValueError as exc:
            return None, editor.fail_result("InvalidSearch", str(exc))
        try:
            allowed_suffixes = _normalize_extensions(extensions, allowed=ALLOWED_EXTENSIONS)
        except ValueError:
            return None, editor.fail_result("InvalidToolArguments", "Invalid extensions filter.")
        assert allowed_suffixes is not None
        if sensitive_relative(relative):
            return None, editor.fail_result("PathRejected", "Kind or path is not editable.")
        try:
            directory = resolve_dir(site.files_root, kind, slug, relative)
        except PathRejected:
            return None, editor.fail_result("PathRejected", "Kind or path is not editable.")
        return (text, compiled, ctx, allowed_suffixes, directory), None

    def search(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        query: str,
        *,
        actor: str,
        extensions: list[str] | None = None,
        case_insensitive: bool = True,
        mode: str = "literal",
        context_lines: int = 0,
    ) -> WpOpsResult:
        editor = self.editor
        site, denied = editor.resolve_site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        admin = editor.need(actor, Permission.WP_OPS_ADMIN, site=site, action="wp_ops_file_read")
        if admin is not None:
            return admin

        validated, err = self._validate_args(
            site=site,
            kind=kind,
            slug=slug,
            relative=relative,
            query=query,
            mode=mode,
            context_lines=context_lines,
            case_insensitive=case_insensitive,
            extensions=extensions,
        )
        if err is not None:
            return err
        assert validated is not None
        text, compiled, ctx, allowed_suffixes, directory = validated

        bound = bind_credential(site)
        extra_secrets = [bound.value] if bound.present else []
        hits, scanned_files, truncated = self._walk_and_match(
            site=site,
            kind=kind,
            slug=slug,
            directory=directory,
            query=text,
            compiled=compiled,
            case_insensitive=case_insensitive,
            context_lines=ctx,
            allowed_suffixes=allowed_suffixes,
            extra_secrets=extra_secrets,
        )
        uses_ssh = editor.uses_ssh(site)
        return WpOpsResult(
            ok=True,
            message="search",
            data={
                "ok": True,
                "status": "searched",
                "site_id": site.id,
                "environment": site.environment,
                "hits": hits,
                "truncated": truncated,
                "files_scanned": scanned_files,
                "mode": mode,
                "context_lines": ctx,
                "ssh": uses_ssh,
                "live_calls": uses_ssh,
            },
        )

    def _walk_and_match(
        self,
        *,
        site: Site,
        kind: str,
        slug: str,
        directory: str,
        query: str,
        compiled: re.Pattern[str] | None,
        case_insensitive: bool,
        context_lines: int,
        allowed_suffixes: frozenset[str],
        extra_secrets: list[str],
    ) -> tuple[list[dict[str, Any]], int, bool]:
        editor = self.editor
        transport = editor.transport_for(site)
        hits: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0
        truncated = False
        stack = [("", directory)]

        with transport_open_batch(transport):
            while stack and scanned_files < SEARCH_FILES and scanned_bytes < SEARCH_BYTES:
                prefix, current = stack.pop(0)
                try:
                    dir_entries = transport_list_entries(transport, current)
                except (PathRejected, TransportError):
                    continue
                for name, is_dir in dir_entries:
                    if sensitive_relative(name) or name.lower() in SKIP_DIRS:
                        continue
                    rel = f"{prefix}/{name}" if prefix else name
                    if is_dir:
                        if SEGMENT_RE.fullmatch(name):
                            try:
                                stack.append((rel, resolve_dir(site.files_root, kind, slug, rel)))
                            except PathRejected:
                                continue
                        continue
                    suffix = Path(name).suffix.lower()
                    if suffix not in allowed_suffixes:
                        continue
                    if scanned_files >= SEARCH_FILES or scanned_bytes >= SEARCH_BYTES:
                        truncated = True
                        break
                    resolved_abs = resolve_file(site.files_root, kind, slug, rel)
                    body, large_css_used = _read_candidate_text(transport, resolved_abs, suffix)
                    if body is None:
                        continue
                    encoded_len = len(body.encode("utf-8"))
                    scanned_files += 1
                    if large_css_used:
                        scanned_bytes += min(encoded_len, MAX_FILE_BYTES)
                    else:
                        scanned_bytes += encoded_len
                        if scanned_bytes > SEARCH_BYTES:
                            truncated = True
                            break
                    scrubbed = _scrub_search_text(editor.scrub_text(site, body), extra_secrets)
                    matched = _match_text_lines(
                        rel,
                        scrubbed,
                        query=query,
                        compiled=compiled,
                        case_insensitive=case_insensitive,
                        context_lines=context_lines,
                        remaining=SEARCH_HITS - len(hits),
                    )
                    if large_css_used and matched:
                        matched, css_truncated = _bound_css_hits(hits, matched)
                        if css_truncated:
                            truncated = True
                    hits.extend(matched)
                    if len(hits) >= SEARCH_HITS:
                        truncated = True
                        break
                if truncated:
                    break
        return hits, scanned_files, truncated


__all__ = [
    "CSS_SEARCH_RESULT_BYTES",
    "ComponentSearcher",
    "MAX_CSS_SEARCH_BYTES",
    "SEARCH_BYTES",
    "SEARCH_FILES",
    "SEARCH_HITS",
    "SEARCH_MODES",
    "_normalize_extensions",
    "_scrub_search_text",
    "search_texts",
    "validate_safe_regex",
]
