"""Site shell extraction, standalone page composition, CSS injection, and HTML span search."""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass

from ecom_ops.wp_ops.html_sanitize import sanitize_page_draft_html

_MAX_HTML_BYTES = 3_000_000
_MAX_HUNKS = 50
_MAX_EXCERPT_LEN = 160

_CSS_HEADER_RE = re.compile(r"^\s*/\*[\s\S]*?\*/\s*")
_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"\w[\w\-]{3,}")
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>[\s\S]*?</title\s*>", re.IGNORECASE)
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_STYLE_CLOSE_RE = re.compile(r"</style", re.IGNORECASE)
_MAIN_OPEN_RE = re.compile(r"<main\b[^>]*>", re.IGNORECASE)
_CONTENT_CONTAINER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"<(?:div|section)\b[^>]*\brole\s*=\s*[\"']main[\"'][^>]*>", re.IGNORECASE),
    re.compile(
        r"<(?:div|section)\b[^>]*\bid\s*=\s*[\"'](?:primary|content|main-content|main)[\"'][^>]*>",
        re.IGNORECASE,
    ),
    re.compile(
        r"<(?:div|section)\b[^>]*\bclass\s*=\s*[\"'](?:[^\"']*\s)?(?:site-main|content-area|page-content|main-content)(?:\s[^\"']*)?[\"'][^>]*>",
        re.IGNORECASE,
    ),
)
_HEADER_OPEN_RE = re.compile(r"<header\b[^>]*>", re.IGNORECASE)
_FOOTER_OPEN_RE = re.compile(r"<footer\b[^>]*>", re.IGNORECASE)
_HEADER_OPEN_RES: tuple[re.Pattern[str], ...] = (
    _HEADER_OPEN_RE,
    re.compile(r"<(?:div|nav|section)\b[^>]*\brole\s*=\s*[\"']banner[\"'][^>]*>", re.IGNORECASE),
    re.compile(
        r"<(?:div|nav|section)\b[^>]*\bclass\s*=\s*[\"'](?:[^\"']*\s)?(?:w-nav|site-header|navbar|main-header|top-nav)(?:\s[^\"']*)?[\"'][^>]*>",
        re.IGNORECASE,
    ),
    re.compile(
        r"<(?:div|nav|section)\b[^>]*\bid\s*=\s*[\"'](?:masthead|site-header|header)[\"'][^>]*>",
        re.IGNORECASE,
    ),
)
_FOOTER_OPEN_RES: tuple[re.Pattern[str], ...] = (
    _FOOTER_OPEN_RE,
    re.compile(r"<(?:div|section)\b[^>]*\brole\s*=\s*[\"']contentinfo[\"'][^>]*>", re.IGNORECASE),
)
_CONTAINER_TAG_SCAN_RE = re.compile(
    r"<(/?)(div|section|article|main|aside)\b([^>]*)>", re.IGNORECASE
)


@dataclass(frozen=True)
class OverlayHunk:
    """Summary of a single diff hunk processed by :func:`apply_overlay`."""

    path: str
    kind: str  # "applied" | "unmatched" | "php_skipped" | "insert_unanchored"
    old_excerpt: str  # <= 160 chars, single line
    new_excerpt: str  # <= 160 chars, single line


@dataclass(frozen=True)
class OverlayResult:
    """Result of applying fragment diffs and optional child CSS onto live HTML.

    ``injected_css_bytes`` is the UTF-8 byte length of the injected
    ``<style data-azom-preview="child-css">…</style>`` tag (0 if none).
    """

    html: str
    hunks: tuple[OverlayHunk, ...]
    injected_css_bytes: int
    highlight_token: str | None  # distinctive visible text of first applied hunk


@dataclass(frozen=True)
class SiteShell:
    """Extracted site chrome (head, header/nav, footer) around a replaceable main slot."""

    before_main: str
    after_main: str
    strategy: str
    original_title: str = ""


def strip_css_header(css: str) -> str:
    """Remove a leading ``/* ... */`` WordPress theme header comment and trim."""
    if not css:
        return ""
    return _CSS_HEADER_RE.sub("", css, count=1).strip()


def distinctive_token(old_text: str, new_text: str) -> str | None:
    """Return the longest visible token introduced in ``new_text`` not in ``old_text``."""
    old_visible = html_lib.unescape(_TAG_RE.sub(" ", old_text))
    new_visible = html_lib.unescape(_TAG_RE.sub(" ", new_text))

    if not new_visible.strip():
        return None

    old_tokens_lower = {tok.lower() for tok in _TOKEN_RE.findall(old_visible)}
    new_tokens = _TOKEN_RE.findall(new_visible)
    candidates = [tok for tok in new_tokens if tok.lower() not in old_tokens_lower]
    if candidates:
        return max(candidates, key=len)

    collapsed = " ".join(new_visible.split())
    return collapsed[:40] if collapsed else None


def _check_html_size(text: str) -> None:
    if len(text) > _MAX_HTML_BYTES or len(text.encode("utf-8")) > _MAX_HTML_BYTES:
        raise ValueError("HtmlTooLarge")


def _excerpt(lines: list[str]) -> str:
    single = " ".join(" ".join(lines).split())
    if len(single) <= _MAX_EXCERPT_LEN:
        return single
    return single[: _MAX_EXCERPT_LEN - 3] + "..."


def _inject_css_blocks(
    html: str,
    blocks: list[tuple[str, str]],
    *,
    marker: str = "child-css",
) -> tuple[str, int]:
    cleaned_parts: list[str] = []
    for label, raw_css in blocks:
        cleaned = strip_css_header(raw_css or "")
        if not cleaned:
            continue
        safe_css = _STYLE_CLOSE_RE.sub(r"<\/style", cleaned)
        if label:
            safe_label = re.sub(r"[^A-Za-z0-9._/\- ]+", "", str(label))[:80]
            cleaned_parts.append(f"/* {safe_label} */\n{safe_css}")
        else:
            cleaned_parts.append(safe_css)
    if not cleaned_parts:
        return html, 0

    joined = "\n\n".join(cleaned_parts)
    style_tag = (
        f'<style data-azom-preview="{html_lib.escape(marker, quote=True)}">\n{joined}\n</style>'
    )
    injected_bytes = len(style_tag.encode("utf-8"))

    head_match = _HEAD_CLOSE_RE.search(html)
    if head_match is not None:
        idx = head_match.start()
        return html[:idx] + style_tag + "\n" + html[idx:], injected_bytes

    body_match = _BODY_OPEN_RE.search(html)
    if body_match is not None:
        idx = body_match.end()
        return html[:idx] + "\n" + style_tag + html[idx:], injected_bytes

    if html:
        return style_tag + "\n" + html, injected_bytes
    return style_tag, injected_bytes


def _find_element_span(html: str, open_tag_re: re.Pattern[str]) -> tuple[int, int, int, int] | None:
    """Find ``(tag_start, content_start, close_start, tag_end)`` respecting nested same-name tags."""
    match = open_tag_re.search(html)
    if match is None:
        return None
    open_text = match.group(0)
    tag_match = re.match(r"<\s*([A-Za-z0-9]+)", open_text)
    if tag_match is None:
        return None
    tag_name = tag_match.group(1)
    if open_text.rstrip().endswith("/>"):
        return match.start(), match.end(), match.end(), match.end()

    step_re = re.compile(rf"</?{re.escape(tag_name)}\b[^>]*>", re.IGNORECASE)
    depth = 1
    pos = match.end()
    while True:
        nxt = step_re.search(html, pos)
        if nxt is None:
            return None
        token = nxt.group(0)
        if token.startswith("</"):
            depth -= 1
            if depth == 0:
                return match.start(), match.end(), nxt.start(), nxt.end()
        elif not token.rstrip().endswith("/>"):
            depth += 1
        pos = nxt.end()


def _find_first_element_span(
    html: str, patterns: tuple[re.Pattern[str], ...]
) -> tuple[int, int, int, int] | None:
    for pat in patterns:
        span = _find_element_span(html, pat)
        if span is not None:
            return span
    return None


def _unclosed_wrapper_open_tags(middle_html: str) -> str:
    """Return opening tags in ``middle_html`` that remain unclosed at the end of the slice."""
    stack: list[tuple[str, str]] = []
    for match in _CONTAINER_TAG_SCAN_RE.finditer(middle_html):
        is_close = bool(match.group(1))
        tag = match.group(2).lower()
        rest = match.group(3)
        if is_close:
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx][0] == tag:
                    del stack[idx:]
                    break
        elif not rest.rstrip().endswith("/"):
            stack.append((tag, match.group(0)))
    return "".join(raw for _tag, raw in stack)


def _find_last_footer_start(live_html: str, *, min_pos: int = 0) -> int | None:
    best: int | None = None
    for pat in _FOOTER_OPEN_RES:
        for match in pat.finditer(live_html, min_pos):
            if best is None or match.start() > best:
                best = match.start()
    return best


def extract_site_shell(live_html: str) -> SiteShell:
    """Extract real site ``<head>``, ``<header>``, and ``<footer>`` chrome for standalone page preview."""
    _check_html_size(live_html)
    title_m = _TITLE_TAG_RE.search(live_html)
    orig_title = html_lib.unescape(_TAG_RE.sub("", title_m.group(0))).strip() if title_m else ""

    main_span = _find_element_span(live_html, _MAIN_OPEN_RE)
    if main_span is not None:
        _ts, c_start, c_end, _te = main_span
        return SiteShell(
            before_main=live_html[:c_start],
            after_main=live_html[c_end:],
            strategy="main_tag",
            original_title=orig_title,
        )

    cont_span = _find_first_element_span(live_html, _CONTENT_CONTAINER_RES)
    if cont_span is not None:
        _ts, c_start, c_end, _te = cont_span
        return SiteShell(
            before_main=live_html[:c_start],
            after_main=live_html[c_end:],
            strategy="content_container",
            original_title=orig_title,
        )

    body_open = _BODY_OPEN_RE.search(live_html)
    body_close = _BODY_CLOSE_RE.search(live_html)
    body_start = body_open.end() if body_open is not None else 0

    header_span = _find_first_element_span(live_html, _HEADER_OPEN_RES)
    if header_span is not None and header_span[0] < body_start:
        header_span = None
    footer_start = _find_last_footer_start(
        live_html,
        min_pos=header_span[3] if header_span is not None else body_start,
    )

    if header_span is not None and footer_start is not None and footer_start >= header_span[3]:
        header_end = header_span[3]
        enclosing = _unclosed_wrapper_open_tags(live_html[header_end:footer_start])
        return SiteShell(
            before_main=live_html[:header_end] + enclosing,
            after_main=live_html[footer_start:],
            strategy="header_footer",
            original_title=orig_title,
        )

    if footer_start is not None and body_open is not None and footer_start >= body_open.end():
        enclosing = _unclosed_wrapper_open_tags(live_html[body_open.end() : footer_start])
        return SiteShell(
            before_main=live_html[: body_open.end()] + enclosing,
            after_main=live_html[footer_start:],
            strategy="footer_only",
            original_title=orig_title,
        )

    if header_span is not None and body_close is not None and body_close.start() >= header_span[3]:
        return SiteShell(
            before_main=live_html[: header_span[3]],
            after_main=live_html[body_close.start() :],
            strategy="header_only",
            original_title=orig_title,
        )

    if body_open is not None and body_close is not None and body_close.start() >= body_open.end():
        return SiteShell(
            before_main=live_html[: body_open.end()],
            after_main=live_html[body_close.start() :],
            strategy="body_fallback",
            original_title=orig_title,
        )
    return SiteShell(
        before_main='<!doctype html><html><head><meta charset="utf-8"><title></title></head><body>',
        after_main="</body></html>",
        strategy="minimal_fallback",
        original_title=orig_title,
    )


def compose_standalone_page(
    shell_html: str,
    *,
    title: str,
    body_html: str,
    page_css: str = "",
    extra_css_files: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    allowed_hosts: frozenset[str] = frozenset(),
    synthetic_banner: str = "",
) -> OverlayResult:
    """Wrap sanitized net-new page HTML + CSS inside a real site's live header/footer/CSS shell."""
    _check_html_size(shell_html)
    shell = extract_site_shell(shell_html)
    clean_title = " ".join(str(title or "").split()).strip()[:200]
    before = shell.before_main
    if clean_title:
        escaped_title = html_lib.escape(clean_title, quote=False)
        if _TITLE_TAG_RE.search(before):
            before = _TITLE_TAG_RE.sub(
                lambda _m, t=escaped_title: f"<title>{t}</title>", before, count=1
            )

    sanitized_body = sanitize_page_draft_html(body_html, allowed_hosts=allowed_hosts)
    banner_html = ""
    if synthetic_banner:
        safe_banner = html_lib.escape(str(synthetic_banner).strip()[:240], quote=False)
        banner_html = (
            '<div class="azom-page-draft-synthetic-note" data-azom-synthetic-note="1" '
            'style="max-width:1120px;margin:16px auto 0;padding:10px 14px;border-radius:8px;'
            'background:#fef9c3;color:#713f12;border:1px solid #facc15;font-size:13px;font-weight:600;">'
            f"{safe_banner}</div>\n"
        )
    wrapped_main = (
        '<div class="azom-page-draft-root" data-azom-page-draft="1">\n'
        f"{banner_html}{sanitized_body}\n"
        "</div>"
    )
    composed = before + "\n" + wrapped_main + "\n" + shell.after_main
    _check_html_size(composed)

    css_blocks: list[tuple[str, str]] = list(extra_css_files)
    if page_css and page_css.strip():
        css_blocks.append(("page_draft.css", page_css))
    composed, injected_css_bytes = _inject_css_blocks(composed, css_blocks, marker="page-css")
    _check_html_size(composed)

    hunks = (
        OverlayHunk(
            "page_draft.json",
            "applied",
            f"shell:{shell.strategy}",
            _excerpt([clean_title or "standalone_page"]),
        ),
    )
    return OverlayResult(
        html=composed,
        hunks=hunks,
        injected_css_bytes=injected_css_bytes,
        highlight_token=None,
    )


__all__ = [
    "OverlayHunk",
    "OverlayResult",
    "SiteShell",
    "_BODY_OPEN_RE",
    "_MAX_EXCERPT_LEN",
    "_MAX_HTML_BYTES",
    "_MAX_HUNKS",
    "_check_html_size",
    "_excerpt",
    "_find_element_span",
    "_find_first_element_span",
    "_inject_css_blocks",
    "compose_standalone_page",
    "distinctive_token",
    "extract_site_shell",
    "strip_css_header",
]
