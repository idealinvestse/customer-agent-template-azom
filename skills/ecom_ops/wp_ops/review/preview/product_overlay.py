"""Table-driven product edits overlay onto rendered WooCommerce/WordPress HTML."""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ecom_ops.wp_ops.html_sanitize import sanitize_rich_html
from ecom_ops.wp_ops.review.preview.shell import (
    _BODY_OPEN_RE,
    _MAX_HUNKS,
    OverlayHunk,
    OverlayResult,
    _check_html_size,
    _excerpt,
    _find_element_span,
    _find_first_element_span,
    distinctive_token,
)


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
    _cls_re(
        "(?:div|section|p)",
        "product-short-description|short-description|product_short_description|product-excerpt",
    ),
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
_SCRIPT_STYLE_BLOCK_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>[\s\S]*?</\1\s*>", re.IGNORECASE
)
_TAG_SPLIT_RE = re.compile(r"(?<=>)|(?=<)")
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BR_SENTINEL = "\x00AZOM_BR\x00"
_NBSP_SENTINEL = "\x00AZOM_NBSP\x00"


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


@dataclass(frozen=True)
class _SimpleFieldSpec:
    key: str
    patterns: tuple[re.Pattern[str], ...]
    rich_html: bool = False


_SIMPLE_FIELD_SPECS: tuple[_SimpleFieldSpec, ...] = (
    _SimpleFieldSpec("title", _TITLE_OPEN_RES, rich_html=False),
    _SimpleFieldSpec("short_description", _SHORT_DESC_OPEN_RES, rich_html=True),
    _SimpleFieldSpec("description", _DESC_OPEN_RES, rich_html=True),
)


def _patch_span_inner(
    html: str,
    span: tuple[int, int, int, int],
    replacement: str,
) -> tuple[str, str]:
    _ts, c_start, c_end, _te = span
    old_inner = html[c_start:c_end]
    updated = html[:c_start] + replacement + html[c_end:]
    _check_html_size(updated)
    return updated, old_inner


def _apply_simple_field(
    html: str,
    edits: dict[str, Any],
    spec: _SimpleFieldSpec,
    record_applied: Callable[[str, str, str, str, str], None],
    hunks: list[OverlayHunk],
) -> str:
    raw = edits.get(spec.key)
    if not isinstance(raw, str) or not raw.strip():
        return html
    hunk_path = f"product_edits.json#{spec.key}"
    if spec.rich_html:
        rendered = sanitize_rich_html(raw)
        span = _find_first_element_span(html, spec.patterns)
        if span is not None and rendered:
            html, old_inner = _patch_span_inner(html, span, rendered)
            record_applied(hunk_path, old_inner, rendered, old_inner, rendered)
        else:
            hunks.append(OverlayHunk(hunk_path, "unmatched", "", _excerpt([raw])))
        return html
    clean = " ".join(raw.split()).strip()
    escaped = html_lib.escape(clean, quote=True)
    span = _find_first_element_span(html, spec.patterns)
    if span is not None:
        html, old_inner = _patch_span_inner(html, span, escaped)
        record_applied(hunk_path, old_inner, clean, old_inner, clean)
    else:
        hunks.append(OverlayHunk(hunk_path, "unmatched", "", _excerpt([clean])))
    return html


def _apply_price_edit(
    html: str,
    edits: dict[str, Any],
    record_applied: Callable[[str, str, str, str, str], None],
    hunks: list[OverlayHunk],
) -> str:
    price_str = (
        " ".join(str(edits["price"]).split()).strip()
        if isinstance(edits.get("price"), str) and edits["price"].strip()
        else ""
    )
    reg_str = (
        " ".join(str(edits["regular_price"]).split()).strip()
        if isinstance(edits.get("regular_price"), str) and edits["regular_price"].strip()
        else ""
    )
    sale_str = (
        " ".join(str(edits["sale_price"]).split()).strip()
        if isinstance(edits.get("sale_price"), str) and edits["sale_price"].strip()
        else ""
    )
    if not (price_str or reg_str or sale_str):
        return html
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
        html, old_inner = _patch_span_inner(html, span, price_markup)
        record_applied("product_edits.json#price", old_inner, visible_price, old_inner, visible_price)
        return html
    amt_span = _find_element_span(html, _PRICE_AMOUNT_OPEN_RE)
    if amt_span is not None:
        inner_replacement = (
            price_markup
            if (sale_str and reg_str)
            else f"<bdi>{html_lib.escape(visible_price, quote=True)}</bdi>"
        )
        html, old_inner = _patch_span_inner(html, amt_span, inner_replacement)
        record_applied("product_edits.json#price", old_inner, visible_price, old_inner, visible_price)
        return html
    hunks.append(OverlayHunk("product_edits.json#price", "unmatched", "", _excerpt([visible_price])))
    return html


def _apply_button_edit(
    html: str,
    edits: dict[str, Any],
    record_applied: Callable[[str, str, str, str, str], None],
    hunks: list[OverlayHunk],
) -> str:
    raw_btn = edits.get("button_text")
    if not isinstance(raw_btn, str) or not raw_btn.strip():
        return html
    btn_clean = " ".join(raw_btn.split()).strip()
    btn_esc = html_lib.escape(btn_clean, quote=True)
    span = _find_first_element_span(html, _BUTTON_OPEN_RES)
    if span is not None:
        html, old_inner = _patch_span_inner(html, span, btn_esc)
        record_applied("product_edits.json#button_text", old_inner, btn_clean, old_inner, btn_clean)
        return html
    inp_match = _INPUT_BUTTON_RE.search(html)
    if inp_match is not None:
        tag_raw = inp_match.group(0)
        if _VALUE_ATTR_RE.search(tag_raw):
            new_tag = _VALUE_ATTR_RE.sub(
                lambda m, b=btn_esc: f'{m.group(1)}"{b}"', tag_raw, count=1
            )
        else:
            new_tag = tag_raw[:-1] + f' value="{btn_esc}">'
        html = html[: inp_match.start()] + new_tag + html[inp_match.end() :]
        _check_html_size(html)
        record_applied("product_edits.json#button_text", tag_raw, btn_clean, tag_raw, btn_clean)
        return html
    hunks.append(OverlayHunk("product_edits.json#button_text", "unmatched", "", _excerpt([btn_clean])))
    return html


def _apply_badge_edit(
    html: str,
    edits: dict[str, Any],
    record_applied: Callable[[str, str, str, str, str], None],
) -> str:
    raw_badge = edits.get("badge_text")
    if not isinstance(raw_badge, str) or not raw_badge.strip():
        return html
    badge_clean = " ".join(raw_badge.split()).strip()
    badge_esc = html_lib.escape(badge_clean, quote=True)
    badge_span = _find_element_span(html, _ONSALE_OPEN_RE)
    if badge_span is not None:
        html, old_inner = _patch_span_inner(html, badge_span, badge_esc)
        record_applied("product_edits.json#badge_text", old_inner, badge_clean, old_inner, badge_clean)
        return html
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
    record_applied("product_edits.json#badge_text", "", badge_clean, "", badge_clean)
    return html


def apply_product_edits_overlay(live_html: str, edits: dict[str, Any]) -> OverlayResult:
    """Safely overlay sandbox product edits onto rendered WooCommerce/WordPress HTML."""
    _check_html_size(live_html)
    if not isinstance(edits, dict) or not edits:
        return OverlayResult(html=live_html, hunks=(), injected_css_bytes=0, highlight_token=None)

    html = live_html
    hunks: list[OverlayHunk] = []
    first_applied_pair: tuple[str, str] | None = None

    def _record_applied(
        path: str, old_raw: str, new_raw: str, visible_old: str, visible_new: str
    ) -> None:
        nonlocal first_applied_pair
        hunks.append(OverlayHunk(path, "applied", _excerpt([old_raw]), _excerpt([new_raw])))
        if first_applied_pair is None:
            first_applied_pair = (visible_old, visible_new)

    html = _apply_simple_field(html, edits, _SIMPLE_FIELD_SPECS[0], _record_applied, hunks)
    html = _apply_price_edit(html, edits, _record_applied, hunks)
    html = _apply_simple_field(html, edits, _SIMPLE_FIELD_SPECS[1], _record_applied, hunks)
    html = _apply_simple_field(html, edits, _SIMPLE_FIELD_SPECS[2], _record_applied, hunks)
    html = _apply_button_edit(html, edits, _record_applied, hunks)
    html = _apply_badge_edit(html, edits, _record_applied)

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
                hunks.append(
                    OverlayHunk(hunk_path, "unmatched", _excerpt([old_txt]), _excerpt([new_txt]))
                )

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


__all__ = [
    "_build_ws_regex",
    "apply_product_edits_overlay",
]
