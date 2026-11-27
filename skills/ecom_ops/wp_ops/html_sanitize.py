"""I/O-free HTML sanitization, overlay fragment neutralization, and preview CSP hardening.

This leaf module never performs network/filesystem I/O and never imports from
``ecom_ops.integrations``. Callers that serve preview HTML inject a
``guard_script_builder`` callable into :func:`harden_preview_html`.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import json
import re
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urlparse

from ecom_ops.wp_ops.file_policy import normalize_css_for_check

UNSAFE_PRODUCT_HTML_RE = re.compile(
    r"(?i)(?:<\s*/?\s*(?:script|iframe|object|embed)\b|javascript\s*:|vbscript\s*:|\bon[a-z]{2,20}\s*=)"
)

_OVERLAY_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script\s*>", re.IGNORECASE)
_OVERLAY_SCRIPT_STRAY_RE = re.compile(r"</?script\b[^>]*>", re.IGNORECASE)
_OVERLAY_OPEN_TAG_RE = re.compile(
    r"""<([A-Za-z][A-Za-z0-9:-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>""",
    re.DOTALL,
)
_OVERLAY_TAG_ATTR_SCAN_RE = re.compile(
    r"""(?P<ev>(?:[\s/]+|(?<=["']))on[a-z0-9_-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s/>]+))|(?P<q>"[^"]*"|'[^']*')""",
    re.IGNORECASE,
)
_OVERLAY_URL_ATTR_RE = re.compile(
    r"""(\b(?:href|src|action|formaction|xlink:href)\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s/>]+))""",
    re.IGNORECASE,
)

_VALID_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_VALID_TLD_LABEL_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9]{2,59})$")
_CGNAT_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
_METADATA_IPV4 = ipaddress.IPv4Address("169.254.169.254")
_METADATA_IPV6 = ipaddress.IPv6Address("fd00:ec2::254")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_COMPAT_NETWORK = ipaddress.IPv6Network("::/96")
_SIXTOFOUR_NETWORK = ipaddress.IPv6Network("2002::/16")
_TEREDO_NETWORK = ipaddress.IPv6Network("2001::/32")

_PAGE_DRAFT_ALLOWED_TAGS = frozenset(
    {
        "section",
        "div",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "span",
        "a",
        "img",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "figure",
        "figcaption",
        "strong",
        "b",
        "em",
        "i",
        "small",
        "mark",
        "br",
        "hr",
        "button",
        "blockquote",
        "code",
        "pre",
        "time",
        "label",
    }
)
_PAGE_DRAFT_VOID_TAGS = frozenset({"br", "hr", "img"})
_PAGE_DRAFT_DROP_TAGS = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "svg",
        "noscript",
        "template",
        "form",
        "input",
        "textarea",
        "select",
        "meta",
        "link",
        "base",
    }
)
_PAGE_DRAFT_VOID_DROP_TAGS = frozenset({"meta", "link", "base", "input"})
_PAGE_DRAFT_GLOBAL_ATTRS = frozenset(
    {"class", "id", "role", "title", "aria-label", "aria-hidden", "aria-describedby"}
)
_PAGE_DRAFT_TAG_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "target", "rel"}),
    "img": frozenset({"src", "alt", "width", "height", "loading", "decoding"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "td": frozenset({"colspan", "rowspan"}),
    "time": frozenset({"datetime"}),
    "button": frozenset({"type", "disabled"}),
}
_SAFE_DATA_ATTR_RE = re.compile(r"^data-[a-z0-9_-]{1,40}$")
_UNSAFE_INLINE_STYLE_RE = re.compile(
    r"(?:javascript:|vbscript:|expression\s*\(|url\s*\(|image-set\s*\(|@import|behavior\s*:|-moz-binding\s*:|</|position\s*:\s*fixed)",
    re.IGNORECASE,
)

_ALLOWED_RICH_TAGS = frozenset(
    {
        "p",
        "br",
        "strong",
        "b",
        "em",
        "i",
        "ul",
        "ol",
        "li",
        "h2",
        "h3",
        "h4",
        "span",
        "div",
        "blockquote",
        "hr",
    }
)
_VOID_RICH_TAGS = frozenset({"br", "hr"})
_DROP_CONTENT_TAGS = frozenset(
    {"script", "style", "iframe", "object", "embed", "svg", "noscript", "template"}
)

# Preview HTML hardening & CSP patterns (canonicalized from dashboard/wp_routes.py)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_STRAY_SCRIPT_RE = re.compile(r"</?script\b[^>]*>", re.IGNORECASE)
_SCRIPT_SRC_ATTR_RE = re.compile(r"\bsrc\s*=", re.IGNORECASE)
_AZOM_GUARD_ATTR_RE = re.compile(
    r"""\b(?:data-azom-guard\s*=\s*(?:"1"|'1'|1(?=[\s>/]|$))|data-azom-sandbox\s*=\s*(?:"guard"|'guard'|guard(?=[\s>/]|$)))""",
    re.IGNORECASE,
)
_AZOM_TOOLBAR_RE = re.compile(
    r"""\bid\s*=\s*(?:"azom-sandbox-toolbar"|'azom-sandbox-toolbar')""",
    re.IGNORECASE,
)
_GUARD_PARAM_RES = (
    ("highlight_token", re.compile(r"""\bvar\s+highlightToken\s*=\s*("(?:\\.|[^"\\])*")\s*;""")),
    ("focus_text", re.compile(r"""\bvar\s+focusText\s*=\s*("(?:\\.|[^"\\])*")\s*;""")),
    ("focus_selector", re.compile(r"""\bvar\s+focusSelector\s*=\s*("(?:\\.|[^"\\])*")\s*;""")),
)
_HTML_OPEN_TAG_RE = re.compile(r"""<([a-zA-Z][a-zA-Z0-9:-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>""")
_TAG_ATTR_RE = re.compile(
    r"""([\s/]*)([a-zA-Z_:][a-zA-Z0-9_.:-]*)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'=<>`/]+))?""",
    re.DOTALL,
)
_UNSAFE_URL_ATTRS = frozenset({"href", "src", "action", "formaction", "xlink:href"})
_UNSAFE_SCHEME_RE = re.compile(r"^\s*(?:javascript|vbscript)\s*:", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _is_public_global_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True iff *addr* is a globally routable public IP (pure stdlib, no integrations import)."""
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            addr = mapped
        elif (
            addr == _METADATA_IPV6
            or addr in _NAT64_WELL_KNOWN
            or addr in _NAT64_LOCAL
            or addr in _IPV4_COMPAT_NETWORK
            or addr in _SIXTOFOUR_NETWORK
            or addr in _TEREDO_NETWORK
            or addr.sixtofour is not None
            or addr.teredo is not None
        ):
            return False
    if not addr.is_global:
        return False
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    if isinstance(addr, ipaddress.IPv4Address):
        if addr == _METADATA_IPV4 or addr in _CGNAT_NETWORK:
            return False
    return True


def _is_safe_url(url: str, *, allowed_hosts: frozenset[str], is_image: bool) -> bool:
    raw = str(url or "").strip()
    if not raw or "\\" in raw or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return False
    low = raw.lower()
    if low.startswith(("javascript:", "vbscript:", "data:", "blob:")):
        return False
    if raw.startswith(("/", "#")) and not raw.startswith("//"):
        return True
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        return False
    netloc = parsed.netloc or ""
    if parsed.username is not None or parsed.password is not None or "@" in netloc:
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if not _is_public_global_ip(ip):
            return False
    else:
        labels = host.split(".")
        if (
            len(labels) < 2
            or not all(_VALID_HOST_LABEL_RE.fullmatch(lbl) for lbl in labels)
            or _VALID_TLD_LABEL_RE.fullmatch(labels[-1]) is None
        ):
            return False
    if is_image and allowed_hosts:
        return host in allowed_hosts
    return True


def _strip_tag_event_handlers(html: str) -> str:
    """Strip inline ``on*=`` handlers across open HTML tags, including ``/``-separated and quoted-``>`` forms."""

    def _scrub_unsafe_url_attr(match: re.Match[str]) -> str:
        raw_val = (
            match.group(2)
            if match.group(2) is not None
            else (match.group(3) if match.group(3) is not None else match.group(4))
        )
        compact = re.sub(r"[\x00-\x20]+", "", html_lib.unescape(raw_val or ""))
        if _UNSAFE_SCHEME_RE.match(compact):
            return f'{match.group(1)}"#"'
        return match.group(0)

    def _clean_tag(match: re.Match[str]) -> str:
        tag_name = match.group(1)
        attrs_part = match.group(2)
        cleaned_attrs = _OVERLAY_TAG_ATTR_SCAN_RE.sub(
            lambda m: "" if m.group("ev") is not None else m.group(0),
            attrs_part,
        )
        cleaned_attrs = _OVERLAY_URL_ATTR_RE.sub(_scrub_unsafe_url_attr, cleaned_attrs)
        return f"<{tag_name}{cleaned_attrs}>"

    return _OVERLAY_OPEN_TAG_RE.sub(_clean_tag, html)


def _neutralize_overlay_fragment(fragment: str) -> str:
    """Strip ``<script>`` tags, ``on*=`` event handlers, and ``javascript:`` attrs from overlay hunks."""
    cleaned = str(fragment or "")
    for _ in range(8):
        prev = cleaned
        cleaned = _OVERLAY_SCRIPT_BLOCK_RE.sub("", cleaned)
        cleaned = _OVERLAY_SCRIPT_STRAY_RE.sub("", cleaned)
        if cleaned == prev:
            break
    cleaned = _strip_tag_event_handlers(cleaned)
    return cleaned


class _AllowlistHtmlSanitizer(HTMLParser):
    """Unified allowlist HTML sanitizer supporting both page drafts and rich product text."""

    def __init__(
        self,
        *,
        allowed_tags: frozenset[str],
        void_tags: frozenset[str],
        drop_tags: frozenset[str],
        void_drop_tags: frozenset[str] = frozenset(),
        allow_attrs: bool = False,
        allowed_hosts: frozenset[str] = frozenset(),
        expand_non_void_startend: bool = True,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._allowed_tags = allowed_tags
        self._void_tags = void_tags
        self._drop_tags = drop_tags
        self._void_drop_tags = void_drop_tags
        self._allow_attrs = allow_attrs
        self._allowed_hosts = allowed_hosts
        self._expand_non_void_startend = expand_non_void_startend
        self.parts: list[str] = []
        self._stack: list[str] = []
        self._skip_depth = 0
        self.saw_allowed_tag = False

    def _clean_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        if not self._allow_attrs:
            return ""
        allowed_for_tag = _PAGE_DRAFT_TAG_ATTRS.get(tag, frozenset())
        out: list[str] = []
        seen: set[str] = set()
        target_blank = False
        rel_idx: int | None = None
        rel_val = ""
        for raw_key, raw_val in attrs:
            key = str(raw_key or "").strip().lower()
            if not key or key.startswith("on") or key in seen:
                continue
            val = "" if raw_val is None else str(raw_val).strip()
            if key == "style":
                if not val or len(val) > 500:
                    continue
                normalized_style = normalize_css_for_check(val)
                if _UNSAFE_INLINE_STYLE_RE.search(val) or _UNSAFE_INLINE_STYLE_RE.search(
                    normalized_style
                ):
                    continue
                seen.add(key)
                out.append(f'style="{html_lib.escape(val, quote=True)}"')
                continue
            is_allowed = (
                key in _PAGE_DRAFT_GLOBAL_ATTRS
                or key in allowed_for_tag
                or _SAFE_DATA_ATTR_RE.fullmatch(key) is not None
            )
            if not is_allowed:
                continue
            if key == "href":
                if not _is_safe_url(val, allowed_hosts=self._allowed_hosts, is_image=False):
                    continue
            elif key == "src":
                if not _is_safe_url(val, allowed_hosts=self._allowed_hosts, is_image=True):
                    continue
            elif key == "target":
                if val not in {"_blank", "_self"}:
                    continue
                if val == "_blank":
                    target_blank = True
            elif key == "rel":
                if len(val) > 200:
                    val = val[:200]
                rel_idx = len(out)
                rel_val = val
            elif key == "type" and tag == "button":
                val = "button"
            elif len(val) > 500:
                val = val[:500]
            seen.add(key)
            out.append(f'{key}="{html_lib.escape(val, quote=True)}"')
        if tag == "a" and target_blank:
            tokens = [tok for tok in rel_val.split() if tok]
            low_tokens = {tok.lower() for tok in tokens}
            for req_tok in ("noopener", "noreferrer"):
                if req_tok not in low_tokens:
                    tokens.append(req_tok)
            enforced_rel = f'rel="{html_lib.escape(" ".join(tokens), quote=True)}"'
            if rel_idx is not None:
                out[rel_idx] = enforced_rel
            else:
                out.append(enforced_rel)
        if tag == "button" and "type" not in seen:
            out.append('type="button"')
        return (" " + " ".join(out)) if out else ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        if low in self._drop_tags:
            if low not in self._void_drop_tags:
                self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if low in self._allowed_tags:
            self.saw_allowed_tag = True
            attr_str = self._clean_attrs(low, attrs)
            self.parts.append(f"<{low}{attr_str}>")
            if low not in self._void_tags:
                self._stack.append(low)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        if self._skip_depth > 0 or low in self._drop_tags:
            return
        if low in self._void_tags and low in self._allowed_tags:
            self.saw_allowed_tag = True
            attr_str = self._clean_attrs(low, attrs)
            self.parts.append(f"<{low}{attr_str}>")
        elif self._expand_non_void_startend and low in self._allowed_tags:
            self.saw_allowed_tag = True
            attr_str = self._clean_attrs(low, attrs)
            self.parts.append(f"<{low}{attr_str}></{low}>")

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in self._drop_tags:
            if self._skip_depth > 0 and low not in self._void_drop_tags:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if low in self._allowed_tags and low not in self._void_tags and low in self._stack:
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


def sanitize_page_draft_html(
    body_html: str,
    *,
    allowed_hosts: frozenset[str] = frozenset(),
) -> str:
    """Sanitize standalone page draft HTML using a strict semantic tag & URL allowlist."""
    raw = str(body_html or "").strip()
    if not raw:
        return ""
    parser = _AllowlistHtmlSanitizer(
        allowed_tags=_PAGE_DRAFT_ALLOWED_TAGS,
        void_tags=_PAGE_DRAFT_VOID_TAGS,
        drop_tags=_PAGE_DRAFT_DROP_TAGS,
        void_drop_tags=_PAGE_DRAFT_VOID_DROP_TAGS,
        allow_attrs=True,
        allowed_hosts=frozenset(h.lower() for h in allowed_hosts if h),
        expand_non_void_startend=True,
    )
    parser.feed(raw)
    parser.close()
    return parser.result().strip()


def sanitize_rich_html(raw: str) -> str:
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
    parser = _AllowlistHtmlSanitizer(
        allowed_tags=_ALLOWED_RICH_TAGS,
        void_tags=_VOID_RICH_TAGS,
        drop_tags=_DROP_CONTENT_TAGS,
        allow_attrs=False,
        expand_non_void_startend=False,
    )
    parser.feed(text)
    parser.close()
    cleaned = parser.result().strip()
    if not cleaned:
        return ""
    if not parser.saw_allowed_tag:
        return f"<p>{cleaned}</p>"
    return cleaned


def strip_tag_unsafe_attrs(match: re.Match[str]) -> str:
    """Strip inline ``on*`` event handlers and ``javascript:``/``vbscript:`` URLs from one open tag."""
    tag_name = match.group(1)
    raw_tail = match.group(2) or ""
    if not raw_tail:
        return match.group(0)
    self_close = False
    tail = raw_tail
    if tail.rstrip().endswith("/"):
        stripped = tail.rstrip()
        if stripped == "/" or stripped[-2] in " \t\r\n\"'":
            self_close = True
            tail = stripped[:-1]
    kept: list[str] = []
    pos = 0
    first = True
    while pos < len(tail):
        m = _TAG_ATTR_RE.match(tail, pos)
        if m is None:
            pos += 1
            continue
        sep, name, raw_val = m.group(1), m.group(2), m.group(3)
        if first and not sep:
            pos = m.end()
            continue
        first = False
        pos = m.end()
        lname = name.lower()
        if lname.startswith("on") and len(lname) > 2:
            continue
        if raw_val is not None and lname in _UNSAFE_URL_ATTRS:
            unquoted = raw_val[1:-1] if len(raw_val) >= 2 and raw_val[0] in "\"'" else raw_val
            compact = re.sub(r"[\x00-\x20]+", "", html_lib.unescape(unquoted))
            if _UNSAFE_SCHEME_RE.match(compact):
                continue
        clean_sep = " " if (not sep or "/" in sep) else sep
        kept.append(f"{clean_sep}{name}={raw_val}" if raw_val is not None else f"{clean_sep}{name}")
    suffix = " />" if self_close else ">"
    return f"<{tag_name}{''.join(kept)}{suffix}"


def extract_genuine_guard_params(html_text: str) -> tuple[bool, dict[str, str]]:
    """Extract only safe JSON string parameters if a genuine Azom guard script was present."""
    found = False
    params = {"highlight_token": "", "focus_text": "", "focus_selector": ""}
    for match in _SCRIPT_BLOCK_RE.finditer(html_text):
        attrs = match.group(1) or ""
        body = match.group(2) or ""
        if _SCRIPT_SRC_ATTR_RE.search(attrs) or not _AZOM_GUARD_ATTR_RE.search(attrs):
            continue
        if "Blockerad i sandbox-läget" not in body or "#azom-sandbox-toast" not in body:
            continue
        found = True
        extracted: dict[str, str] = {"highlight_token": "", "focus_text": "", "focus_selector": ""}
        for key, pat in _GUARD_PARAM_RES:
            pm = pat.search(body)
            if pm is None:
                continue
            try:
                val = json.loads(pm.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(val, str):
                extracted[key] = val[:400]
        params = extracted
    return found, params


def harden_preview_html(
    html_text: str,
    *,
    nonce: str,
    guard_script_builder: Callable[..., str],
) -> str:
    """Strip ALL on-disk scripts & inline handlers; inject only server-generated canonical guard."""
    had_genuine_guard, guard_params = extract_genuine_guard_params(html_text)

    working = html_text
    for _ in range(8):
        prev = working
        working = _SCRIPT_BLOCK_RE.sub("", working)
        working = _STRAY_SCRIPT_RE.sub("", working)
        if working == prev:
            break
    working = _HTML_OPEN_TAG_RE.sub(strip_tag_unsafe_attrs, working)

    if had_genuine_guard or _AZOM_TOOLBAR_RE.search(working):
        canonical_guard = guard_script_builder(nonce=nonce, **guard_params)
        if canonical_guard:
            body_close = _BODY_CLOSE_RE.search(working)
            if body_close is not None:
                working = (
                    working[: body_close.start()]
                    + canonical_guard
                    + "\n"
                    + working[body_close.start() :]
                )
            else:
                working = working + "\n" + canonical_guard
    return working


def preview_csp_header(nonce: str = "") -> str:
    """Return the opaque sandbox CSP header for served preview HTML responses."""
    script_src = f"'nonce-{nonce}'" if nonce else "'none'"
    return (
        "sandbox allow-scripts; "
        "default-src 'none'; "
        f"script-src {script_src}; "
        "style-src 'unsafe-inline' https: data:; "
        "img-src https: data: blob:; "
        "font-src https: data:; "
        "media-src https: data:; "
        "connect-src 'none'; "
        "form-action 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'self'"
    )


__all__ = [
    "UNSAFE_PRODUCT_HTML_RE",
    "_AllowlistHtmlSanitizer",
    "_is_safe_url",
    "_neutralize_overlay_fragment",
    "_strip_tag_event_handlers",
    "extract_genuine_guard_params",
    "harden_preview_html",
    "preview_csp_header",
    "sanitize_page_draft_html",
    "sanitize_rich_html",
    "strip_tag_unsafe_attrs",
]
