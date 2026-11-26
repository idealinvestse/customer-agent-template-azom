"""HTML fragment diff overlay with static WordPress i18n expansion and CSS injection."""

from __future__ import annotations

import difflib
import html as html_lib
import re
from typing import Any

from ecom_ops.wp_ops.html_sanitize import _neutralize_overlay_fragment
from ecom_ops.wp_ops.review.preview.product_overlay import (
    _build_ws_regex,
    apply_product_edits_overlay,
)
from ecom_ops.wp_ops.review.preview.shell import (
    _MAX_HUNKS,
    OverlayHunk,
    OverlayResult,
    _check_html_size,
    _excerpt,
    _inject_css_blocks,
    distinctive_token,
)

_FRAGMENT_SUFFIXES = (".php", ".html")
_PHP_I18N_TAG_RE = re.compile(
    r"<\?(?:php|=)?\s*(?:echo\s+)?(?P<fn>__|esc_html__|esc_attr__|_e|esc_html_e|esc_attr_e)\s*\(\s*"
    r"(?P<q>['\"])(?P<msg>(?:\\.|(?!(?P=q))[^\\])*?)(?P=q)"
    r"(?:\s*,\s*(?P<dq>['\"])(?:\\.|(?!(?P=dq))[^\\])*?(?P=dq))?\s*\)\s*;?\s*\?>",
)
_PHP_I18N_CONTEXT_TAG_RE = re.compile(
    r"<\?(?:php|=)?\s*(?:echo\s+)?(?P<fn>_x|_ex|esc_html_x|esc_attr_x)\s*\(\s*"
    r"(?P<q>['\"])(?P<msg>(?:\\.|(?!(?P=q))[^\\])*?)(?P=q)\s*,\s*"
    r"(?P<cq>['\"])(?:\\.|(?!(?P=cq))[^\\])*?(?P=cq)"
    r"(?:\s*,\s*(?P<dq>['\"])(?:\\.|(?!(?P=dq))[^\\])*?(?P=dq))?\s*\)\s*;?\s*\?>",
)


def _unescape_php_literal(text: str, quote: str) -> str:
    return text.replace(f"\\{quote}", quote).replace("\\\\", "\\")


def _expand_php_i18n_literals(text: str) -> str:
    """Render static WordPress i18n string-literal tags into plain HTML text without executing PHP."""
    if "<?" not in text:
        return text

    def _repl(match: re.Match[str]) -> str:
        fn = match.group("fn")
        raw = _unescape_php_literal(match.group("msg"), match.group("q"))
        if fn.startswith("esc_"):
            return html_lib.escape(raw, quote=True)
        return _neutralize_overlay_fragment(raw)

    expanded = _PHP_I18N_TAG_RE.sub(_repl, text)
    if "<?" in expanded:
        expanded = _PHP_I18N_CONTEXT_TAG_RE.sub(_repl, expanded)
    return expanded


def _has_php(line: str) -> bool:
    return "<?" in line or "?>" in line


def _find_anchor_line(before_lines: list[str], insert_idx: int) -> str | None:
    for idx in range(insert_idx - 1, -1, -1):
        candidate = _expand_php_i18n_literals(before_lines[idx])
        if candidate.strip() and not _has_php(candidate):
            return candidate
    return None


def apply_overlay(
    live_html: str,
    changes: list[tuple[str, str, str]],
    *,
    child_css: str | None = None,
    extra_css_files: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    product_edits: dict[str, Any] | None = None,
) -> OverlayResult:
    """Apply HTML fragments from ``changes``, optional ``child_css``, and optional ``product_edits``."""
    _check_html_size(live_html)

    html = live_html
    hunks: list[OverlayHunk] = []
    first_applied_pair: tuple[str, str] | None = None

    for rel_path, before_text, after_text in changes:
        if len(hunks) >= _MAX_HUNKS:
            break
        if not rel_path.lower().endswith(_FRAGMENT_SUFFIXES):
            continue

        before_lines = before_text.splitlines()
        after_lines = after_text.splitlines()
        matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if len(hunks) >= _MAX_HUNKS:
                break

            old_lines = before_lines[i1:i2]
            new_lines = after_lines[j1:j2]
            rendered_old_lines = [_expand_php_i18n_literals(line) for line in old_lines]
            rendered_new_lines = [_expand_php_i18n_literals(line) for line in new_lines]
            old_ex = _excerpt(old_lines)
            new_ex = _excerpt(new_lines)

            if any(_has_php(line) for line in rendered_old_lines) or any(
                _has_php(line) for line in rendered_new_lines
            ):
                hunks.append(OverlayHunk(rel_path, "php_skipped", old_ex, new_ex))
                continue

            is_insert = tag == "insert" or not "".join(rendered_old_lines).strip()
            if is_insert:
                anchor = _find_anchor_line(before_lines, i1)
                anchor_re = _build_ws_regex(anchor) if anchor is not None else None
                match = anchor_re.search(html) if anchor_re is not None else None
                if match is None:
                    hunks.append(OverlayHunk(rel_path, "insert_unanchored", old_ex, new_ex))
                    continue
                new_block = _neutralize_overlay_fragment("\n".join(rendered_new_lines))
                insertion = f"\n{new_block}" if rendered_new_lines else ""
                html = html[: match.end()] + insertion + html[match.end() :]
                _check_html_size(html)
                hunks.append(OverlayHunk(rel_path, "applied", old_ex, new_ex))
                if first_applied_pair is None:
                    first_applied_pair = ("", new_block)
                continue

            old_block = "\n".join(rendered_old_lines)
            new_block = _neutralize_overlay_fragment("\n".join(rendered_new_lines))
            pattern = _build_ws_regex(old_block)
            match = pattern.search(html) if pattern is not None else None
            if match is None:
                hunks.append(OverlayHunk(rel_path, "unmatched", old_ex, new_ex))
                continue

            html = html[: match.start()] + new_block + html[match.end() :]
            _check_html_size(html)
            hunks.append(OverlayHunk(rel_path, "applied", old_ex, new_ex))
            if first_applied_pair is None:
                first_applied_pair = (old_block, new_block)

    css_blocks: list[tuple[str, str]] = []
    if child_css:
        css_blocks.append(("", child_css))
    for css_label, css_body in extra_css_files:
        if css_body:
            css_blocks.append((css_label, css_body))
    html, injected_css_bytes = _inject_css_blocks(html, css_blocks, marker="child-css")
    _check_html_size(html)

    highlight = (
        distinctive_token(first_applied_pair[0], first_applied_pair[1])
        if first_applied_pair is not None
        else None
    )

    if isinstance(product_edits, dict) and product_edits:
        prod_res = apply_product_edits_overlay(html, product_edits)
        html = prod_res.html
        hunks.extend(prod_res.hunks)
        if prod_res.highlight_token:
            highlight = prod_res.highlight_token

    return OverlayResult(
        html=html,
        hunks=tuple(hunks),
        injected_css_bytes=injected_css_bytes,
        highlight_token=highlight,
    )


__all__ = ["apply_overlay"]
