"""Site-bound theme and plugin change plans.

The model may propose a diff. This module stores that diff outside the chat,
audit, and job result. It never starts a shell, PHP, or WP-CLI process.
Live SFTP stays read-only. Production deploy stays closed until a real
backup and restore exist.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.config import rest_reads_allowed as _config_rest_reads_allowed
from ecom_ops.wp_ops.credentials import bind_credential, redact_secret_text
from ecom_ops.wp_ops.file_policy import (
    is_safe_pure_css_file,  # noqa: F401  # compatibility re-export
    production_deploy_block,
    risk_for,
)
from ecom_ops.wp_ops.file_policy import (
    normalize_css_for_check as _normalize_css_for_check,  # noqa: F401  # compatibility re-export
)
from ecom_ops.wp_ops.files import (
    _MAX_BYTES,
    FileEditor,
    LocalTree,
    PathRejected,
    TransportError,
    _digest,
    component_dir,
    resolve_file,
    transport_message,
)
from ecom_ops.wp_ops.models import Site, WpOpsResult
from ecom_ops.wp_ops.store import atomic_write_text

_PLAN_ID = re.compile(r"^fe-[a-f0-9]{12}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_EXTENSIONS = frozenset(
    {".php", ".css", ".scss", ".js", ".json", ".txt", ".md", ".html", ".pot", ".po"}
)
_SKIP_DIRS = frozenset({"vendor", "node_modules", "cache", "uploads", ".git"})
_SENSITIVE = re.compile(
    r"(?i)(^\.env(?:$|\.)|^wp-config[^/]*\.php$|"
    r"(?:^|[-_.])(?:secrets?|credentials?|tokens?|passwords?|auth|private[-_.]?key|api[-_.]?key)(?:[-_.]|$)|"
    r"\.(?:bak|backup|sql|sqlite3?|dump|swp|orig)$)"
)
_MAX_CSS_SEARCH_BYTES = 512 * 1024
_CSS_SEARCH_RESULT_BYTES = 8192
_PLAN_TTL = timedelta(hours=12)
_MAX_SUMMARY = 200
_MAX_DIFF_BYTES = 65536
_SEARCH_FILES = 80
_SEARCH_BYTES = 512 * 1024
_SEARCH_HITS = 20
_TREE_CAP = 256
_SEARCH_MODES = frozenset({"literal", "regex"})

FILE_TOOL_NAMES = frozenset(
    {
        "wp_component_context",
        "wp_list_components",
        "wp_theme_overview",
        "wp_plugin_overview",
        "wp_list_component_tree",
        "wp_search_component",
        "wp_read_theme_plugin_file",
        "wp_propose_file_diff",
        "wp_propose_new_file",
        "wp_validate_plan",
        "wp_apply_staging",
        "wp_request_production",
        "wp_deploy_production",
        "wp_verify_change",
        "wp_rollback_file",
        "wp_open_review_workspace",
        "wp_workspace_patch_file",
        "wp_workspace_create_file",
        "wp_workspace_copy_from_parent",
        "wp_workspace_status",
        "wp_workspace_run_checks",
    }
)


def rest_reads_allowed() -> bool:
    """Live content reads only when mock mode is explicitly off."""
    return _config_rest_reads_allowed()


def sensitive_relative(relative: str) -> bool:
    parts = [part for part in str(relative or "").replace("\\", "/").split("/") if part]
    if any(part.lower() in _SKIP_DIRS or _SENSITIVE.search(part) for part in parts):
        return True
    return False


def payload_path(data_dir: Path, plan_id: str) -> Path:
    if not _PLAN_ID.fullmatch(plan_id):
        raise TransportError("UnknownPlan")
    root = (Path(data_dir) / "wp_file_payloads").resolve()
    path = (root / plan_id).resolve()
    if path.parent != root:
        raise TransportError("UnknownPlan")
    return path


def write_payload(data_dir: Path, plan_id: str, new_text: str, diff: str) -> None:
    path = payload_path(data_dir, plan_id)
    atomic_write_text(
        path,
        json.dumps({"new_text": new_text, "diff": diff}, ensure_ascii=False),
        mode=0o600,
    )


def read_payload(data_dir: Path, plan_id: str) -> dict[str, str]:
    path = payload_path(data_dir, plan_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise TransportError("PayloadMissing") from exc
    if not isinstance(raw, dict):
        raise TransportError("PayloadMissing")
    new_text = raw.get("new_text")
    diff = raw.get("diff")
    if not isinstance(new_text, str) or not isinstance(diff, str):
        raise TransportError("PayloadMissing")
    return {"new_text": new_text, "diff": diff}


def delete_payload(data_dir: Path, plan_id: str) -> None:
    try:
        payload_path(data_dir, plan_id).unlink(missing_ok=True)
    except TransportError:
        return


def plan_expired(plan: dict[str, Any], now: datetime) -> bool:
    stamp = plan.get("expires_at") or plan.get("created_at")
    if not isinstance(stamp, str) or not stamp:
        return True
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if plan.get("expires_at"):
        return now >= parsed
    return now - parsed > _PLAN_TTL


def _unified(before: str, after: str, path: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            n=2,
        )
    )
    if len(diff.encode("utf-8")) > _MAX_DIFF_BYTES:
        return ""
    return diff


def _directory(root: str, kind: str, slug: str, relative: str) -> str:
    base = component_dir(root, kind, slug)
    text = str(relative or "").strip().replace("\\", "/")
    if not text:
        return base
    parts = [part for part in text.split("/") if part]
    if text.startswith("/") or any(part in {".", ".."} for part in parts):
        raise PathRejected("relative")
    if any(_SEGMENT.fullmatch(part) is None or sensitive_relative(part) for part in parts):
        raise PathRejected("segment")
    return base + "/" + "/".join(parts)


def _scrub_search_text(text: str, extra_secrets: list[str] | None = None) -> str:
    return redact_secret_text(text, extra_secrets)


_QUANT_BRACE_RE = re.compile(r"\{(\d+)(?:,(\d*))?\}")
_BACKREF_RE = re.compile(r"(?<!\\)(?:\\\\)*(\\[1-9]|\(\?P=)")


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
                # Skip group header prefix like (?:, (?i), (?P<name>
                if i + 2 < n and pattern[i + 2] in (":", "=", "!"):
                    start = i + 3
                    advance = 3
                elif i + 2 < n and pattern[i + 2] == "<":
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
            is_repeat = False
            max_rep: int | None = 1
            is_optional_after = False
            if i < n and pattern[i] in ("+", "*"):
                is_repeat = True
                max_rep = None
                if pattern[i] == "*":
                    is_optional_after = True
            elif i < n and pattern[i] == "?":
                is_optional_after = True
            elif i < n and pattern[i] == "{":
                m_brace = _QUANT_BRACE_RE.match(pattern, i)
                if m_brace is not None:
                    lo = int(m_brace.group(1))
                    hi_raw = m_brace.group(2)
                    if hi_raw is None:
                        is_repeat = lo > 1
                        max_rep = lo
                    elif hi_raw == "":
                        is_repeat = True
                        max_rep = None
                    else:
                        hi_val = int(hi_raw)
                        is_repeat = hi_val > 1
                        max_rep = hi_val
                        if lo == 0 or hi_val != lo:
                            is_optional_after = True
            if is_repeat:
                if had_quant:
                    raise ValueError("Unsafe regular expression: nested quantifier rejected.")
                if had_optional and (max_rep is None or max_rep >= 6):
                    raise ValueError("Unsafe regular expression: quantified optional group rejected.")
                if max_rep is not None and max_rep > 50:
                    raise ValueError("Unsafe regular expression: excessive group repetition rejected.")
                if had_alt:
                    if max_rep is None or max_rep >= 6:
                        branches: list[str] = []
                        for b_idx, b_start in enumerate(branch_starts):
                            b_end = (
                                (branch_starts[b_idx + 1] - 1)
                                if (b_idx + 1 < len(branch_starts))
                                else group_end
                            )
                            branches.append(pattern[b_start:b_end])
                        first_chars: set[str] = set()
                        for br in branches:
                            if not br or any(
                                tok in br for tok in (".", "[", "\\w", "\\d", "\\s", "\\W", "\\D", "\\S")
                            ):
                                raise ValueError(
                                    "Unsafe regular expression: ambiguous quantified alternation rejected."
                                )
                            lead = br[0]
                            if lead in first_chars:
                                raise ValueError(
                                    "Unsafe regular expression: overlapping quantified alternation rejected."
                                )
                            first_chars.add(lead)
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
    if mode not in _SEARCH_MODES:
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
        excerpt = line.strip()
        if len(excerpt) > 120:
            excerpt = excerpt[:120]
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
        if "\x00" in body or len(body.encode("utf-8")) > _MAX_BYTES:
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


class FileChangeOps:
    """Operations that sit on top of the existing typed file editor."""

    def __init__(self, editor: FileEditor) -> None:
        self.editor = editor

    def _data_dir(self) -> Path:
        return self.editor.store.path.parent

    def _now(self) -> datetime:
        current = self.editor.clock()
        if not isinstance(current, datetime):
            return datetime.now(UTC)
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current

    def list_tree(
        self, site_id: str, kind: str, slug: str, relative: str, *, actor: str
    ) -> WpOpsResult:
        editor = self.editor
        site, denied = editor._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        if sensitive_relative(relative):
            return editor._fail("PathRejected", "Kind or path is not editable.")
        transport = editor._transport(site)
        try:
            directory = _directory(site.files_root, kind, slug, relative)
            if hasattr(transport, "list_entries") and callable(transport.list_entries):
                raw_entries = list(transport.list_entries(directory))
            else:
                raw_entries = [
                    (name, Path(name).suffix.lower() not in _EXTENSIONS)
                    for name in transport.list_dir(directory)
                ]
        except PathRejected:
            return editor._fail("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return editor._fail(
                exc.error_class, transport_message(exc.error_class), ssh=editor._ssh(site)
            )
        entries: list[dict[str, str]] = []
        truncated = False
        for name, is_dir in raw_entries:
            if name in {".", ".."} or sensitive_relative(name) or name.lower() in _SKIP_DIRS:
                continue
            if len(entries) >= _TREE_CAP:
                truncated = True
                break
            kind_name = "directory" if is_dir else "file"
            entries.append({"name": name, "kind": kind_name})
        return WpOpsResult(
            ok=True,
            message="tree",
            data={
                "ok": True,
                "status": "listed",
                "site_id": site.id,
                "environment": site.environment,
                "kind": kind,
                "slug": slug,
                "directory": relative or "",
                "entries": entries,
                "truncated": truncated,
                "ssh": editor._ssh(site),
                "live_calls": editor._ssh(site),
            },
        )

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
        site, denied = editor._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        admin = editor.need(actor, Permission.WP_OPS_ADMIN, site=site, action="wp_ops_file_read")
        if admin is not None:
            return admin
        if mode not in _SEARCH_MODES:
            return editor._fail("InvalidToolArguments", "Invalid search mode.")
        if type(context_lines) is not int:
            return editor._fail("InvalidToolArguments", "Invalid context_lines.")
        ctx = max(0, min(3, context_lines))
        if type(case_insensitive) is not bool:
            return editor._fail("InvalidToolArguments", "Invalid case_insensitive flag.")
        text = str(query or "")
        if len(text) < 2 or len(text) > 80:
            return editor._fail("InvalidSearch", "Search text must be 2 to 80 characters.")
        try:
            compiled = _compile_search_matcher(text, mode=mode, case_insensitive=case_insensitive)
        except ValueError as exc:
            return editor._fail("InvalidSearch", str(exc))
        try:
            allowed_suffixes = _normalize_extensions(extensions, allowed=_EXTENSIONS)
        except ValueError:
            return editor._fail("InvalidToolArguments", "Invalid extensions filter.")
        assert allowed_suffixes is not None
        if sensitive_relative(relative):
            return editor._fail("PathRejected", "Kind or path is not editable.")
        try:
            directory = _directory(site.files_root, kind, slug, relative)
        except PathRejected:
            return editor._fail("PathRejected", "Kind or path is not editable.")

        bound = bind_credential(site)
        extra_secrets = [bound.value] if bound.present else []
        hits: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0
        truncated = False
        stack = [("", directory)]
        transport = editor._transport(site)
        has_entries = hasattr(transport, "list_entries") and callable(transport.list_entries)
        batch_ctx = (
            transport.open_batch()
            if hasattr(transport, "open_batch") and callable(transport.open_batch)
            else contextlib.nullcontext()
        )
        with batch_ctx:
            while stack and scanned_files < _SEARCH_FILES and scanned_bytes < _SEARCH_BYTES:
                prefix, current = stack.pop(0)
                try:
                    if has_entries:
                        dir_entries = list(transport.list_entries(current))
                    else:
                        dir_entries = [
                            (name, Path(name).suffix.lower() not in _EXTENSIONS)
                            for name in transport.list_dir(current)
                        ]
                except (PathRejected, TransportError):
                    continue
                for name, is_dir in dir_entries:
                    if sensitive_relative(name) or name.lower() in _SKIP_DIRS:
                        continue
                    rel = f"{prefix}/{name}" if prefix else name
                    if is_dir:
                        if _SEGMENT.fullmatch(name):
                            try:
                                stack.append((rel, _directory(site.files_root, kind, slug, rel)))
                            except PathRejected:
                                continue
                        continue
                    suffix = Path(name).suffix.lower()
                    if suffix not in allowed_suffixes:
                        continue
                    if scanned_files >= _SEARCH_FILES or scanned_bytes >= _SEARCH_BYTES:
                        truncated = True
                        break
                    resolved_abs = resolve_file(site.files_root, kind, slug, rel)
                    large_css_used = False
                    try:
                        raw_bytes = transport.read_bytes(resolved_abs)
                    except TransportError as exc:
                        if (
                            exc.error_class == "FileTooLarge"
                            and suffix == ".css"
                            and hasattr(transport, "read_css_search_bytes")
                            and callable(transport.read_css_search_bytes)
                        ):
                            try:
                                raw_bytes = transport.read_css_search_bytes(
                                    resolved_abs, max_bytes=_MAX_CSS_SEARCH_BYTES
                                )
                                large_css_used = True
                            except (PathRejected, TransportError):
                                continue
                        else:
                            continue
                    except PathRejected:
                        continue
                    if b"\x00" in raw_bytes:
                        continue
                    if not large_css_used and len(raw_bytes) > _MAX_BYTES:
                        if (
                            suffix == ".css"
                            and len(raw_bytes) <= _MAX_CSS_SEARCH_BYTES
                        ):
                            large_css_used = True
                        else:
                            continue
                    try:
                        body = raw_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    encoded = body.encode("utf-8")
                    scanned_files += 1
                    if large_css_used:
                        scanned_bytes += min(len(encoded), _MAX_BYTES)
                    else:
                        scanned_bytes += len(encoded)
                        if scanned_bytes > _SEARCH_BYTES:
                            truncated = True
                            break
                    scrubbed = _scrub_search_text(editor._scrub(site, body), extra_secrets)
                    matched = _match_text_lines(
                        rel,
                        scrubbed,
                        query=text,
                        compiled=compiled,
                        case_insensitive=case_insensitive,
                        context_lines=ctx,
                        remaining=_SEARCH_HITS - len(hits),
                    )
                    if large_css_used and matched:
                        used_hit_bytes = len(json.dumps(hits, ensure_ascii=False).encode("utf-8"))
                        bounded_matched: list[dict[str, Any]] = []
                        for item in matched:
                            item_size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                            if used_hit_bytes + item_size > _CSS_SEARCH_RESULT_BYTES:
                                truncated = True
                                break
                            bounded_matched.append(item)
                            used_hit_bytes += item_size
                        matched = bounded_matched
                    hits.extend(matched)
                    if len(hits) >= _SEARCH_HITS:
                        truncated = True
                        break
                if truncated:
                    break
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
                "ssh": editor._ssh(site),
                "live_calls": editor._ssh(site),
            },
        )

    def propose(
        self,
        site_id: str,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        base_sha256: str,
        summary: str,
        *,
        actor: str,
        create: bool,
        parent_slug: str | None,
    ) -> WpOpsResult:
        editor = self.editor
        site, denied = editor._site(site_id, actor, write=True)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        if site.environment == "production":
            code = editor.need(actor, Permission.CODE_EDIT, site=site, action="wp_ops_file_edit")
            if code is not None:
                return code
        if kind == "plugin" and slug == "woocommerce":
            return editor._fail("WooCoreRefused", "WooCommerce core files cannot be edited.")
        if sensitive_relative(relative):
            return editor._fail("PathRejected", "Kind or path is not editable.")
        if kind == "theme" and parent_slug and slug == parent_slug:
            return editor._fail("ParentThemeRefused", "Edit the child theme, not the parent.")
        note = " ".join(str(summary or "").split())[:_MAX_SUMMARY]
        locked = editor.lock(site.id)
        if not locked.acquire(blocking=False):
            return editor._fail("SiteBusy", "Site check already running", executed=False)
        try:
            return self._propose_locked(
                site,
                kind,
                slug,
                relative,
                new_text,
                base_sha256,
                note,
                actor=actor,
                create=create,
                parent_slug=parent_slug,
            )
        finally:
            locked.release()

    def _propose_locked(
        self,
        site: Site,
        kind: str,
        slug: str,
        relative: str,
        new_text: str,
        base_sha256: str,
        summary: str,
        *,
        actor: str,
        create: bool,
        parent_slug: str | None,
    ) -> WpOpsResult:
        editor = self.editor
        try:
            path = resolve_file(site.files_root, kind, slug, relative)
            proposed = editor._validate_text(new_text)
        except PathRejected:
            return editor._fail("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return editor._fail(exc.error_class, transport_message(exc.error_class))
        if create and not self._parent_ready(site, path):
            return editor._fail("PathRejected", "Kind or path is not editable.")
        try:
            current = editor._read_text(site, path)
        except TransportError as exc:
            if create and exc.error_class == "FileMissing":
                current = ""
            else:
                return editor._fail(
                    exc.error_class, transport_message(exc.error_class), ssh=editor._ssh(site)
                )
        else:
            if create:
                return editor._fail("AlreadyExists", "The file already exists.")
        before = _digest(current.encode())
        if str(base_sha256 or "") != before:
            return editor._fail("DigestMismatch", "The base hash does not match the file.")
        after = _digest(proposed.encode())
        if before == after:
            return editor._fail("NoChange", "The file is unchanged.")
        risk = risk_for(relative, proposed)
        plan_id = "fe-" + uuid.uuid4().hex[:12]
        now = self._now()
        diff = _unified(current, proposed, path)
        try:
            write_payload(self._data_dir(), plan_id, proposed, diff)
        except (OSError, TransportError):
            return editor._fail("PayloadMissing", "The review copy could not be stored.")
        theme_relationship = "unknown" if parent_slug is None else "known"
        plan = {
            "id": plan_id,
            "plan_id": plan_id,
            "site_id": site.id,
            "environment": site.environment,
            "kind": kind,
            "slug": slug,
            "relative_path": relative.replace("\\", "/").strip("/"),
            "path": path,
            "before_digest": before,
            "after_digest": after,
            "byte_length": len(proposed.encode()),
            "created_at": now.isoformat(),
            "expires_at": (now + _PLAN_TTL).isoformat(),
            "created_by": actor,
            "status": "pending_review",
            "risk": risk,
            "summary": summary,
            "created": create,
            "theme_relationship": theme_relationship,
            "payload_present": True,
            "ssh": editor._ssh(site),
        }
        editor.store.update(lambda state: state.setdefault("file_edits", []).append(plan))
        editor._audit_file(
            actor,
            site,
            success=True,
            error_class=None,
            operation="file_diff_propose",
            path=path,
            byte_length=plan["byte_length"],
            plan_id=plan_id,
            digest=after,
        )
        public = {
            key: value
            for key, value in plan.items()
            if key not in {"path"}
        }
        return WpOpsResult(
            ok=True,
            message="diff proposed",
            data={
                "ok": True,
                "status": "pending_review",
                "plan": public,
                "executed": False,
                "isolated_execution": "not_available",
                "ssh": editor._ssh(site),
                "live_calls": False,
            },
        )

    def _load_plan(self, plan_id: str, site_id: str | None) -> dict[str, Any] | None:
        if not _PLAN_ID.fullmatch(plan_id):
            return None
        state = self.editor.store.load()
        for row in state.get("file_edits", []):
            if not isinstance(row, dict) or row.get("id") != plan_id:
                continue
            if site_id is not None and row.get("site_id") != site_id:
                continue
            return row
        return None

    def _set_status(self, plan_id: str, status: str, *, actor: str) -> None:
        now = self._now().isoformat()

        def mutate(state: dict[str, Any]) -> None:
            for row in state.get("file_edits", []):
                if isinstance(row, dict) and row.get("id") == plan_id:
                    row["status"] = status
                    row["updated_at"] = now
                    row["updated_by"] = actor

        self.editor.store.update(mutate)

    def validate(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        site, denied = editor._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        plan = self._load_plan(plan_id, site.id)
        if plan is None or plan.get("payload_present") is not True:
            return editor._fail("UnknownPlan", "Unknown edit plan.")
        if plan_expired(plan, self._now()):
            delete_payload(self._data_dir(), plan_id)
            return editor._fail("PlanExpired", "The edit plan has expired.")
        try:
            payload = read_payload(self._data_dir(), plan_id)
            resolved = resolve_file(
                site.files_root,
                str(plan.get("kind") or ""),
                str(plan.get("slug") or ""),
                str(plan.get("relative_path") or ""),
            )
        except (PathRejected, TransportError):
            return editor._fail("PathRejected", "Kind or path is not editable.")
        if _digest(payload["new_text"].encode()) != plan.get("after_digest"):
            return editor._fail("DigestMismatch", "The stored diff does not match the plan.")
        if plan.get("created") is True:
            try:
                editor._read_text(site, resolved)
            except TransportError as exc:
                if exc.error_class != "FileMissing":
                    return editor._fail(exc.error_class, transport_message(exc.error_class))
            else:
                return editor._fail("AlreadyExists", "The file already exists.")
        else:
            try:
                current = editor._read_text(site, resolved)
            except TransportError as exc:
                return editor._fail(exc.error_class, transport_message(exc.error_class))
            if _digest(current.encode()) != plan.get("before_digest"):
                return editor._fail("StaleFile", "The file changed after the plan was created.")
        advance = plan.get("risk") != "high"
        if advance and plan.get("status") == "pending_review":
            self._set_status(plan_id, "validated", actor=actor)
        return WpOpsResult(
            ok=True,
            message="validated",
            data={
                "ok": True,
                "status": "validated" if advance else "pending_review",
                "plan_id": plan_id,
                "site_id": site.id,
                "environment": site.environment,
                "risk": plan.get("risk"),
                "advance": advance,
                "isolated_execution": "not_available",
                "executed": False,
                "live_calls": False,
            },
        )

    def _has_staging(self) -> bool:
        from ecom_ops.wp_ops.registry import load_sites

        try:
            sites = load_sites(self.editor.registry_path, data_dir=self._data_dir())
        except ValueError:
            return False
        return any(site.environment == "staging" for site in sites)

    def _parent_ready(self, site: Site, path: str) -> bool:
        transport = self.editor._transport(site)
        if not isinstance(transport, LocalTree):
            return True
        return transport._local(path).parent.is_dir()

    def _prepare_stage(self, plan_id: str, *, actor: str) -> tuple[Any, ...] | WpOpsResult:
        """Check every staging guard. The tuple is ready to write; a result is a refusal."""
        editor = self.editor
        plan = self._load_plan(plan_id, None)
        if plan is None:
            return editor._fail("UnknownPlan", "Unknown edit plan.")
        site_id = str(plan.get("site_id") or "")
        site, denied = editor._site(site_id, actor, write=True)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        if site.environment != "staging":
            if not self._has_staging():
                return editor._fail("StagingUnconfigured", "No staging site is registered.")
            return editor._fail("ProductionWriteDisabled", "Production file writes are disabled.")
        if plan.get("risk") == "high":
            return editor._fail(
                "HighRiskBlocked",
                "High-risk changes stop until an isolated check exists.",
            )
        if plan.get("status") not in {"pending_review", "validated"}:
            return editor._fail("PlanNotPending", "Edit plan is not pending.")
        if plan_expired(plan, self._now()):
            delete_payload(self._data_dir(), plan_id)
            return editor._fail("PlanExpired", "The edit plan has expired.")
        transport = editor._transport(site)
        if not isinstance(transport, LocalTree):
            return editor._fail(
                "DeployTransportUnavailable",
                "Live SFTP cannot write. A separate deploy identity is required.",
                ssh=True,
            )
        try:
            payload = read_payload(self._data_dir(), plan_id)
            proposed = editor._validate_text(payload["new_text"])
            resolved = resolve_file(
                site.files_root,
                str(plan.get("kind") or ""),
                str(plan.get("slug") or ""),
                str(plan.get("relative_path") or ""),
            )
        except PathRejected:
            return editor._fail("PathRejected", "Kind or path is not editable.")
        except TransportError as exc:
            return editor._fail(exc.error_class, transport_message(exc.error_class))
        if _digest(proposed.encode()) != plan.get("after_digest"):
            return editor._fail("DigestMismatch", "The stored diff does not match the plan.")
        if plan.get("created") is True:
            if not self._parent_ready(site, resolved):
                return editor._fail("PathRejected", "Kind or path is not editable.")
            try:
                editor._read_text(site, resolved)
            except TransportError as exc:
                if exc.error_class != "FileMissing":
                    return editor._fail(exc.error_class, transport_message(exc.error_class))
            else:
                return editor._fail("AlreadyExists", "The file already exists.")
        else:
            try:
                current = editor._read_text(site, resolved)
            except TransportError as exc:
                return editor._fail(exc.error_class, transport_message(exc.error_class))
            if _digest(current.encode()) != plan.get("before_digest"):
                return editor._fail("StaleFile", "The file changed after the plan was created.")
        return site, plan, proposed, resolved, transport

    def preview_stage(self, plan_id: str, *, actor: str) -> WpOpsResult:
        """Tell the chat a server button is required. This method does not write."""
        prepared = self._prepare_stage(plan_id, actor=actor)
        if isinstance(prepared, WpOpsResult):
            return prepared
        site, plan, _proposed, _resolved, _transport = prepared
        return WpOpsResult(
            ok=True,
            message="approval required",
            data={
                "ok": True,
                "status": "approval_required",
                "executed": False,
                "approval_required": True,
                "plan_id": plan_id,
                "site_id": site.id,
                "environment": site.environment,
                "risk": plan.get("risk"),
                "live_calls": False,
            },
        )

    def stage(self, plan_id: str, *, actor: str) -> WpOpsResult:
        """Write a staging plan on the local mock tree. Live transport is refused."""
        editor = self.editor
        prepared = self._prepare_stage(plan_id, actor=actor)
        if isinstance(prepared, WpOpsResult):
            return prepared
        site, plan, proposed, resolved, transport = prepared
        if plan.get("created") is True:
            try:
                target_path = transport._local_checked(resolved)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(proposed.encode())
            except PathRejected:
                return editor._fail("PathRejected", "Kind or path is not editable.")
        else:
            try:
                transport.write_bytes(resolved, proposed.encode())
            except PathRejected:
                return editor._fail("PathRejected", "Kind or path is not editable.")
            except TransportError as exc:
                return editor._fail(exc.error_class, transport_message(exc.error_class))
        written = editor._read_text(site, resolved)
        if _digest(written.encode()) != plan.get("after_digest"):
            return editor._fail("DigestMismatch", "The written file does not match the plan.")
        self._set_status(plan_id, "staged", actor=actor)
        delete_payload(self._data_dir(), plan_id)
        editor._audit_file(
            actor,
            site,
            success=True,
            error_class=None,
            operation="file_stage",
            path=resolved,
            byte_length=len(written.encode()),
            plan_id=plan_id,
            digest=str(plan.get("after_digest") or ""),
        )
        return WpOpsResult(
            ok=True,
            message="staged",
            data={
                "ok": True,
                "status": "staged",
                "plan_id": plan_id,
                "site_id": site.id,
                "environment": site.environment,
                "executed": True,
                "ssh": False,
                "live_calls": False,
            },
        )

    def deny(self, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        plan = self._load_plan(plan_id, None)
        if plan is None:
            return editor._fail("UnknownPlan", "Unknown edit plan.")
        _site, denied = editor._site(str(plan.get("site_id") or ""), actor, write=True)
        if denied is not None:
            return denied
        if plan.get("status") not in {"pending_review", "validated"}:
            return editor._fail("PlanNotPending", "Edit plan is not pending.")
        delete_payload(self._data_dir(), plan_id)
        self._set_status(plan_id, "denied", actor=actor)
        return WpOpsResult(
            ok=True,
            message="denied",
            data={
                "ok": True,
                "status": "denied",
                "plan_id": plan_id,
                "executed": False,
                "live_calls": False,
            },
        )

    def request_production(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        site, denied = editor._site(site_id, actor, write=True)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        plan = self._load_plan(plan_id, site.id)
        if plan is None:
            return editor._fail("UnknownPlan", "Unknown edit plan.")
        blocked = production_deploy_block()
        return WpOpsResult(
            ok=False,
            message="Production deploy is not available.",
            data={
                "ok": False,
                "status": "production_blocked",
                "plan_id": plan_id,
                "site_id": site.id,
                "environment": site.environment,
                **blocked,
            },
        )

    def deploy_production(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        return self.request_production(site_id, plan_id, actor=actor)

    def verify(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        site, denied = editor._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        plan = self._load_plan(plan_id, site.id)
        if plan is None:
            return editor._fail("UnknownPlan", "Unknown edit plan.")
        matched = False
        if plan.get("status") in {"staged", "deployed", "verified"}:
            try:
                resolved = resolve_file(
                    site.files_root,
                    str(plan.get("kind") or ""),
                    str(plan.get("slug") or ""),
                    str(plan.get("relative_path") or ""),
                )
                current = editor._read_text(site, resolved)
                matched = _digest(current.encode()) == plan.get("after_digest")
            except (PathRejected, TransportError):
                matched = False
        return WpOpsResult(
            ok=True,
            message="verify",
            data={
                "ok": True,
                "status": "verified" if matched else plan.get("status") or "unknown",
                "plan_id": plan_id,
                "site_id": site.id,
                "hash_matches": matched,
                "page_checks": "not_run",
                "executed": False,
                "live_calls": False,
            },
        )

    def rollback(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        denied = editor.need(actor, Permission.WP_OPS_RESTORE, action="wp_ops_restore")
        if denied is not None:
            return denied
        plan = self._load_plan(plan_id, site_id)
        if plan is None:
            return editor._fail("UnknownPlan", "Unknown edit plan.")
        return editor._fail("RestoreNotInMvp", "Restore is not available in this phase.")

    def review(self, site_id: str, plan_id: str, *, actor: str) -> WpOpsResult:
        editor = self.editor
        site, denied = editor._site(site_id, actor, write=False)
        if denied is not None:
            return denied
        assert isinstance(site, Site)
        plan = self._load_plan(plan_id, site.id)
        if plan is None:
            return editor._fail("UnknownPlan", "Unknown edit plan.")
        diff = ""
        if plan.get("payload_present") is True and not plan_expired(plan, self._now()):
            try:
                diff = read_payload(self._data_dir(), plan_id)["diff"]
            except TransportError:
                diff = ""
        public = {
            key: plan.get(key)
            for key in (
                "plan_id",
                "site_id",
                "environment",
                "kind",
                "slug",
                "relative_path",
                "before_digest",
                "after_digest",
                "risk",
                "summary",
                "status",
                "expires_at",
                "created",
            )
        }
        return WpOpsResult(
            ok=True,
            message="review",
            data={"ok": True, "plan": public, "diff": diff, "executed": False, "live_calls": False},
        )
