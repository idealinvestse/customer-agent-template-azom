"""Preview overlay: patch live rendered HTML in-memory from staging file diffs.

Applies non-PHP HTML fragment hunks and optional child-theme CSS onto live
rendered HTML so operators can preview proposed edits without touching live sites.
"""

from __future__ import annotations

import difflib
import html as html_lib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

_MAX_HTML_BYTES = 3_000_000
_MAX_HUNKS = 50
_MAX_EXCERPT_LEN = 160
_FRAGMENT_SUFFIXES = (".php", ".html")

_CSS_HEADER_RE = re.compile(r"^\s*/\*[\s\S]*?\*/\s*")
_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"\w[\w\-]{3,}")
_TAG_SPLIT_RE = re.compile(r"(?<=>)|(?=<)")
_HEAD_CLOSE_RE = re.compile(r"</head\s*>", re.IGNORECASE)
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_STYLE_CLOSE_RE = re.compile(r"</style", re.IGNORECASE)
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
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BR_SENTINEL = "\x00AZOM_BR\x00"
_NBSP_SENTINEL = "\x00AZOM_NBSP\x00"

_ALLOWED_RICH_TAGS = frozenset(
    {"p", "br", "strong", "b", "em", "i", "ul", "ol", "li", "h2", "h3", "h4", "span", "div", "blockquote", "hr"}
)
_VOID_RICH_TAGS = frozenset({"br", "hr"})
_DROP_CONTENT_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "svg", "noscript", "template"})
_SCRIPT_STYLE_BLOCK_RE = re.compile(r"<(script|style|noscript|template)\b[^>]*>[\s\S]*?</\1\s*>", re.IGNORECASE)

def _cls_re(tags: str, classes: str) -> re.Pattern[str]:
    return re.compile(
        rf"<{tags}\b[^>]*\bclass\s*=\s*[\"'](?:[^\"']*\s)?(?:{classes})(?:\s[^\"']*)?[\"'][^>]*>",
        re.IGNORECASE,
    )


_TITLE_OPEN_RES: tuple[re.Pattern[str], ...] = (
    _cls_re("h1", "product_title|entry-title|product-title|product_name|heading-product"),
    re.compile(r"<h1\b[^>]*>", re.IGNORECASE),
)
_PRICE_CONTAINER_OPEN_RES: tuple[re.Pattern[str], ...] = (
    _cls_re("(?:p|div|span)", "price"),
    _cls_re("(?:p|div|span)", "product-price|product_price|price-box"),
)
_PRICE_AMOUNT_OPEN_RE = _cls_re("span", "woocommerce-Price-amount")
_SHORT_DESC_OPEN_RES: tuple[re.Pattern[str], ...] = (
    _cls_re("(?:div|section|p)", "woocommerce-product-details__short-description"),
    _cls_re("(?:div|section|p)", "product-short-description|short-description|product_short_description|product-excerpt"),
)
_DESC_OPEN_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"<(?:div|section)\b[^>]*\bid\s*=\s*[\"']tab-description[\"'][^>]*>", re.IGNORECASE),
    _cls_re("(?:div|section)", "woocommerce-Tabs-panel--description"),
    _cls_re("(?:div|section)", "product-description|product_description|entry-content"),
)
_BUTTON_OPEN_RES: tuple[re.Pattern[str], ...] = (
    _cls_re("button", "single_add_to_cart_button"),
    _cls_re("button", "add_to_cart_button"),
    _cls_re("a", "single_add_to_cart_button|add_to_cart_button"),
)
_INPUT_BUTTON_RE = _cls_re("input", "single_add_to_cart_button|add_to_cart_button")
_ONSALE_OPEN_RE = _cls_re("(?:span|div)", "onsale")
_VALUE_ATTR_RE = re.compile(r"""(\bvalue\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)


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
        return raw

    expanded = _PHP_I18N_TAG_RE.sub(_repl, text)
    if "<?" in expanded:
        expanded = _PHP_I18N_CONTEXT_TAG_RE.sub(_repl, expanded)
    return expanded


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


def _has_php(line: str) -> bool:
    return "<?" in line or "?>" in line


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _escape_part(part: str) -> str:
    if part == _BR_SENTINEL:
        return r"(?i:<br\s*/?>)"
    if part == _NBSP_SENTINEL:
        return r"(?:&nbsp;|\s)+"
    return re.escape(part)


def _build_ws_regex(text: str) -> re.Pattern[str] | None:
    """Build a whitespace-tolerant regex matching ``text`` across formatting diffs."""
    normalized = _BR_TAG_RE.sub(f" {_BR_SENTINEL} ", text)
    normalized = normalized.replace("&nbsp;", f" {_NBSP_SENTINEL} ")
    raw_tokens = normalized.split()
    if not raw_tokens:
        return None

    parts: list[str] = []
    for token in raw_tokens:
        if token in {_BR_SENTINEL, _NBSP_SENTINEL}:
            parts.append(token)
            continue
        for piece in _TAG_SPLIT_RE.split(token):
            if piece:
                parts.append(piece)

    if not parts:
        return None

    pattern_bits = [_escape_part(parts[0])]
    for idx in range(1, len(parts)):
        prev_part = parts[idx - 1]
        curr_part = parts[idx]
        if prev_part in {_BR_SENTINEL, _NBSP_SENTINEL} or curr_part in {_BR_SENTINEL, _NBSP_SENTINEL}:
            joiner = r"\s*"
        elif _is_word_char(prev_part[-1]) and _is_word_char(curr_part[0]):
            joiner = r"\s+"
        else:
            joiner = r"\s*"
        pattern_bits.append(joiner)
        pattern_bits.append(_escape_part(curr_part))
    return re.compile("".join(pattern_bits))


def _find_anchor_line(before_lines: list[str], insert_idx: int) -> str | None:
    for idx in range(insert_idx - 1, -1, -1):
        candidate = _expand_php_i18n_literals(before_lines[idx])
        if candidate.strip() and not _has_php(candidate):
            return candidate
    return None


def _inject_child_css(html: str, child_css: str | None) -> tuple[str, int]:
    if not child_css:
        return html, 0
    cleaned = strip_css_header(child_css)
    if not cleaned:
        return html, 0

    safe_css = _STYLE_CLOSE_RE.sub(r"<\/style", cleaned)
    style_tag = f'<style data-azom-preview="child-css">\n{safe_css}\n</style>'
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


class _SafeRichHtmlSanitizer(HTMLParser):
    """Allowlist-only HTML sanitizer for product descriptions (no attributes, no scripts)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._stack: list[str] = []
        self._skip_depth = 0
        self.saw_allowed_tag = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        low = tag.lower()
        if low in _DROP_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if low in _ALLOWED_RICH_TAGS:
            self.saw_allowed_tag = True
            if low in _VOID_RICH_TAGS:
                self.parts.append(f"<{low}>")
            else:
                self._stack.append(low)
                self.parts.append(f"<{low}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        low = tag.lower()
        if self._skip_depth > 0 or low in _DROP_CONTENT_TAGS:
            return
        if low in _VOID_RICH_TAGS:
            self.saw_allowed_tag = True
            self.parts.append(f"<{low}>")

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in _DROP_CONTENT_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if low in _ALLOWED_RICH_TAGS and low not in _VOID_RICH_TAGS and low in self._stack:
            while self._stack:
                top = self._stack.pop()
                self.parts.append(f"</{top}>")
                if top == low:
                    break

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self.parts.append(html_lib.escape(data, quote=False))

    def result(self) -> str:
        while self._stack:
            self.parts.append(f"</{self._stack.pop()}>")
        return "".join(self.parts)


def _sanitize_rich_html(raw: str) -> str:
    """Sanitize optional rich HTML or format escaped plain text into safe paragraphs."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if "<" not in text and ">" not in text:
        paragraphs = [p.strip() for p in re.split(r"\r?\n\s*\r?\n", text) if p.strip()]
        if not paragraphs:
            return ""
        rendered = []
        for para in paragraphs:
            escaped = html_lib.escape(para, quote=False)
            escaped = re.sub(r"\r?\n", "<br>", escaped)
            rendered.append(f"<p>{escaped}</p>")
        return "\n".join(rendered)
    parser = _SafeRichHtmlSanitizer()
    parser.feed(text)
    parser.close()
    cleaned = parser.result().strip()
    if not cleaned:
        return ""
    if not parser.saw_allowed_tag:
        return f"<p>{cleaned}</p>"
    return cleaned


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


def _replace_visible_text(html: str, old_text: str, new_escaped: str) -> tuple[str, bool, str]:
    """Replace first matching visible occurrence of ``old_text`` in body HTML."""
    body_match = _BODY_OPEN_RE.search(html)
    search_start = body_match.end() if body_match is not None else 0

    candidates = [old_text]
    escaped_old = html_lib.escape(old_text, quote=False)
    if escaped_old != old_text:
        candidates.append(escaped_old)

    if "<" in old_text:
        for cand in candidates:
            pat = _build_ws_regex(cand)
            if pat is None:
                continue
            m = pat.search(html, search_start) or (pat.search(html) if search_start > 0 else None)
            if m is not None:
                return html[: m.start()] + new_escaped + html[m.end() :], True, m.group(0)
        return html, False, ""

    excluded_ranges: list[tuple[int, int]] = [
        (m.start(), m.end()) for m in _SCRIPT_STYLE_BLOCK_RE.finditer(html)
    ]

    def _in_excluded_or_tag(pos: int, end: int) -> bool:
        for s, e in excluded_ranges:
            if pos < e and end > s:
                return True
        last_lt = html.rfind("<", 0, pos)
        last_gt = html.rfind(">", 0, pos)
        return last_lt > last_gt

    for start_offset in (search_start, 0) if search_start > 0 else (0,):
        for cand in candidates:
            pat = _build_ws_regex(cand)
            if pat is None:
                continue
            for m in pat.finditer(html, start_offset):
                if _in_excluded_or_tag(m.start(), m.end()):
                    continue
                return html[: m.start()] + new_escaped + html[m.end() :], True, m.group(0)
    return html, False, ""


def apply_product_edits_overlay(live_html: str, edits: dict[str, Any]) -> OverlayResult:
    """Safely overlay sandbox product edits onto rendered WooCommerce/WordPress HTML."""
    _check_html_size(live_html)
    if not isinstance(edits, dict) or not edits:
        return OverlayResult(html=live_html, hunks=(), injected_css_bytes=0, highlight_token=None)

    html = live_html
    hunks: list[OverlayHunk] = []
    first_applied_pair: tuple[str, str] | None = None

    def _record_applied(path: str, old_raw: str, new_raw: str, visible_old: str, visible_new: str) -> None:
        nonlocal first_applied_pair
        hunks.append(OverlayHunk(path, "applied", _excerpt([old_raw]), _excerpt([new_raw])))
        if first_applied_pair is None:
            first_applied_pair = (visible_old, visible_new)

    # 1. Product title
    raw_title = edits.get("title")
    if isinstance(raw_title, str) and raw_title.strip():
        title_clean = " ".join(raw_title.split()).strip()
        title_esc = html_lib.escape(title_clean, quote=True)
        span = _find_first_element_span(html, _TITLE_OPEN_RES)
        if span is not None:
            _ts, c_start, c_end, _te = span
            old_inner = html[c_start:c_end]
            html = html[:c_start] + title_esc + html[c_end:]
            _check_html_size(html)
            _record_applied("product_edits.json#title", old_inner, title_clean, old_inner, title_clean)
        else:
            hunks.append(OverlayHunk("product_edits.json#title", "unmatched", "", _excerpt([title_clean])))

    # 2. Price / regular_price / sale_price
    raw_price = edits.get("price")
    raw_regular = edits.get("regular_price")
    raw_sale = edits.get("sale_price")
    price_str = " ".join(str(raw_price).split()).strip() if isinstance(raw_price, str) and raw_price.strip() else ""
    reg_str = " ".join(str(raw_regular).split()).strip() if isinstance(raw_regular, str) and raw_regular.strip() else ""
    sale_str = " ".join(str(raw_sale).split()).strip() if isinstance(raw_sale, str) and raw_sale.strip() else ""
    if price_str or reg_str or sale_str:
        if sale_str and reg_str:
            price_markup = (
                f'<del aria-hidden="true"><span class="woocommerce-Price-amount amount">'
                f"<bdi>{html_lib.escape(reg_str, quote=True)}</bdi></span></del> "
                f'<ins><span class="woocommerce-Price-amount amount">'
                f"<bdi>{html_lib.escape(sale_str, quote=True)}</bdi></span></ins>"
            )
            visible_price = sale_str
        elif sale_str:
            price_markup = (
                f'<ins><span class="woocommerce-Price-amount amount">'
                f"<bdi>{html_lib.escape(sale_str, quote=True)}</bdi></span></ins>"
            )
            visible_price = sale_str
        else:
            chosen_price = price_str or reg_str
            price_markup = (
                f'<span class="woocommerce-Price-amount amount">'
                f"<bdi>{html_lib.escape(chosen_price, quote=True)}</bdi></span>"
            )
            visible_price = chosen_price

        span = _find_first_element_span(html, _PRICE_CONTAINER_OPEN_RES)
        if span is not None:
            _ts, c_start, c_end, _te = span
            old_inner = html[c_start:c_end]
            html = html[:c_start] + price_markup + html[c_end:]
            _check_html_size(html)
            _record_applied("product_edits.json#price", old_inner, visible_price, old_inner, visible_price)
        else:
            amt_span = _find_element_span(html, _PRICE_AMOUNT_OPEN_RE)
            if amt_span is not None:
                _ts, c_start, c_end, _te = amt_span
                old_inner = html[c_start:c_end]
                inner_replacement = (
                    price_markup
                    if (sale_str and reg_str)
                    else f"<bdi>{html_lib.escape(visible_price, quote=True)}</bdi>"
                )
                html = html[:c_start] + inner_replacement + html[c_end:]
                _check_html_size(html)
                _record_applied("product_edits.json#price", old_inner, visible_price, old_inner, visible_price)
            else:
                hunks.append(OverlayHunk("product_edits.json#price", "unmatched", "", _excerpt([visible_price])))

    # 3. Short description
    raw_short = edits.get("short_description")
    if isinstance(raw_short, str) and raw_short.strip():
        safe_short = _sanitize_rich_html(raw_short)
        span = _find_first_element_span(html, _SHORT_DESC_OPEN_RES)
        if span is not None and safe_short:
            _ts, c_start, c_end, _te = span
            old_inner = html[c_start:c_end]
            html = html[:c_start] + safe_short + html[c_end:]
            _check_html_size(html)
            _record_applied(
                "product_edits.json#short_description",
                old_inner,
                safe_short,
                old_inner,
                safe_short,
            )
        else:
            hunks.append(
                OverlayHunk("product_edits.json#short_description", "unmatched", "", _excerpt([raw_short]))
            )

    # 4. Full description
    raw_desc = edits.get("description")
    if isinstance(raw_desc, str) and raw_desc.strip():
        safe_desc = _sanitize_rich_html(raw_desc)
        span = _find_first_element_span(html, _DESC_OPEN_RES)
        if span is not None and safe_desc:
            _ts, c_start, c_end, _te = span
            old_inner = html[c_start:c_end]
            html = html[:c_start] + safe_desc + html[c_end:]
            _check_html_size(html)
            _record_applied(
                "product_edits.json#description",
                old_inner,
                safe_desc,
                old_inner,
                safe_desc,
            )
        else:
            hunks.append(OverlayHunk("product_edits.json#description", "unmatched", "", _excerpt([raw_desc])))

    # 5. Add-to-cart button text
    raw_btn = edits.get("button_text")
    if isinstance(raw_btn, str) and raw_btn.strip():
        btn_clean = " ".join(raw_btn.split()).strip()
        btn_esc = html_lib.escape(btn_clean, quote=True)
        span = _find_first_element_span(html, _BUTTON_OPEN_RES)
        if span is not None:
            _ts, c_start, c_end, _te = span
            old_inner = html[c_start:c_end]
            html = html[:c_start] + btn_esc + html[c_end:]
            _check_html_size(html)
            _record_applied("product_edits.json#button_text", old_inner, btn_clean, old_inner, btn_clean)
        else:
            inp_match = _INPUT_BUTTON_RE.search(html)
            if inp_match is not None:
                tag_raw = inp_match.group(0)
                if _VALUE_ATTR_RE.search(tag_raw):
                    new_tag = _VALUE_ATTR_RE.sub(rf'\1"{btn_esc}"', tag_raw, count=1)
                else:
                    new_tag = tag_raw[:-1] + f' value="{btn_esc}">'
                html = html[: inp_match.start()] + new_tag + html[inp_match.end() :]
                _check_html_size(html)
                _record_applied("product_edits.json#button_text", tag_raw, btn_clean, tag_raw, btn_clean)
            else:
                hunks.append(OverlayHunk("product_edits.json#button_text", "unmatched", "", _excerpt([btn_clean])))

    # 6. Badge text
    raw_badge = edits.get("badge_text")
    if isinstance(raw_badge, str) and raw_badge.strip():
        badge_clean = " ".join(raw_badge.split()).strip()
        badge_esc = html_lib.escape(badge_clean, quote=True)
        badge_span = _find_element_span(html, _ONSALE_OPEN_RE)
        if badge_span is not None:
            _ts, c_start, c_end, _te = badge_span
            old_inner = html[c_start:c_end]
            html = html[:c_start] + badge_esc + html[c_end:]
            _check_html_size(html)
            _record_applied("product_edits.json#badge_text", old_inner, badge_clean, old_inner, badge_clean)
        else:
            badge_tag = (
                '<span class="onsale azom-sandbox-badge" data-azom-sandbox-badge="1" '
                'style="display:inline-block;background:#16a34a;color:#fff;font-weight:700;'
                'font-size:0.85em;padding:4px 10px;border-radius:999px;margin-bottom:8px;">'
                f"{badge_esc}</span>"
            )
            title_span = _find_first_element_span(html, _TITLE_OPEN_RES)
            if title_span is not None:
                insert_at = title_span[0]
                html = html[:insert_at] + badge_tag + "\n" + html[insert_at:]
            else:
                body_match = _BODY_OPEN_RE.search(html)
                if body_match is not None:
                    html = html[: body_match.end()] + "\n" + badge_tag + html[body_match.end() :]
                else:
                    html = badge_tag + "\n" + html
            _check_html_size(html)
            _record_applied("product_edits.json#badge_text", "", badge_clean, "", badge_clean)

    # 7. Whitespace-tolerant visible text replacements
    raw_repls = edits.get("replacements")
    if isinstance(raw_repls, list):
        for idx, item in enumerate(raw_repls):
            if len(hunks) >= _MAX_HUNKS:
                break
            if not isinstance(item, dict):
                continue
            old_txt = item.get("old_text")
            new_txt = item.get("new_text")
            if not isinstance(old_txt, str) or not isinstance(new_txt, str) or not old_txt.strip():
                continue
            new_esc = html_lib.escape(new_txt, quote=True)
            html, matched, matched_old = _replace_visible_text(html, old_txt, new_esc)
            hunk_path = f"product_edits.json#replacements[{idx}]"
            if matched:
                _check_html_size(html)
                _record_applied(hunk_path, matched_old, new_txt, matched_old, new_txt)
            else:
                hunks.append(OverlayHunk(hunk_path, "unmatched", _excerpt([old_txt]), _excerpt([new_txt])))

    highlight = (
        distinctive_token(first_applied_pair[0], first_applied_pair[1])
        if first_applied_pair is not None
        else None
    )
    return OverlayResult(
        html=html,
        hunks=tuple(hunks),
        injected_css_bytes=0,
        highlight_token=highlight,
    )


def apply_overlay(
    live_html: str,
    changes: list[tuple[str, str, str]],
    *,
    child_css: str | None = None,
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
                new_block = "\n".join(rendered_new_lines)
                insertion = f"\n{new_block}" if rendered_new_lines else ""
                html = html[: match.end()] + insertion + html[match.end() :]
                _check_html_size(html)
                hunks.append(OverlayHunk(rel_path, "applied", old_ex, new_ex))
                if first_applied_pair is None:
                    first_applied_pair = ("", new_block)
                continue

            old_block = "\n".join(rendered_old_lines)
            new_block = "\n".join(rendered_new_lines)
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

    html, injected_css_bytes = _inject_child_css(html, child_css)
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
