"""Pure-Python HTML metadata, link, selector, and JSON-LD parsers for WordPress tools."""

from __future__ import annotations

import html
import html.parser
import json
import re
import urllib.parse
from typing import Any

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_OPEN_TAG_RE = re.compile(r"<([A-Za-z][A-Za-z0-9-]*)\b([^>]*)>", re.S)
_ATTR_RE = re.compile(r"""([A-Za-z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""")


class HtmlMetaParser(html.parser.HTMLParser):
    """Extract page metadata, headings, asset counts, and external hosts from HTML."""

    def __init__(self, *, site_host: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.site_host = str(site_host or "").strip().lower()
        self.in_title = False
        self.in_h1 = False
        self.title_parts: list[str] = []
        self.h1_current: list[str] = []
        self.h1_list: list[str] = []
        self.lang = ""
        self.meta_desc = ""
        self.canonical = ""
        self.links = 0
        self.images = 0
        self.images_missing_alt = 0
        self.scripts = 0
        self.external_hosts: list[str] = []

    def _track_url(self, u: str | None) -> None:
        val = str(u or "").strip()
        if not val.startswith("https://") and not val.startswith("http://"):
            return
        host = (urllib.parse.urlparse(val).hostname or "").strip().lower()
        if (
            host
            and host != self.site_host
            and host != f"www.{self.site_host}"
            and host not in self.external_hosts
        ):
            if len(self.external_hosts) < 10:
                self.external_hosts.append(host)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "html" and not self.lang:
            self.lang = attr_map.get("lang", "")[:24]
        elif t == "title":
            self.in_title = True
        elif t == "h1":
            self.in_h1 = True
            self.h1_current = []
        elif t == "meta" and attr_map.get("name", "").strip().lower() == "description" and not self.meta_desc:
            self.meta_desc = attr_map.get("content", "")
        elif t == "link":
            rels = {r.strip().lower() for r in attr_map.get("rel", "").split()}
            if "canonical" in rels and not self.canonical:
                self.canonical = attr_map.get("href", "")
            self._track_url(attr_map.get("href"))
        elif t == "a":
            self.links += 1
        elif t == "img":
            self.images += 1
            if not attr_map.get("alt", "").strip():
                self.images_missing_alt += 1
            self._track_url(attr_map.get("src"))
        elif t == "script":
            self.scripts += 1
            self._track_url(attr_map.get("src"))

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "title":
            self.in_title = False
        elif t == "h1" and self.in_h1:
            self.in_h1 = False
            txt = " ".join("".join(self.h1_current).split())
            if txt and len(self.h1_list) < 5:
                self.h1_list.append(txt[:160])

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.in_h1:
            self.h1_current.append(data)


class ProductLinkParser(html.parser.HTMLParser):
    """Collect ``(href, label)`` pairs from anchor elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_href: str | None = None
        self.current_parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            href = attr_map.get("href", "").strip()
            if href:
                self.current_href = href
                self.current_parts = []
                title_attr = attr_map.get("title") or attr_map.get("aria-label") or ""
                if title_attr:
                    self.current_parts.append(title_attr)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self.current_href is not None:
            label = " ".join(" ".join(self.current_parts).split()).strip()
            self.links.append((self.current_href, label))
            self.current_href = None
            self.current_parts = []

    def handle_data(self, data: str) -> None:
        if self.current_href is not None:
            self.current_parts.append(data)


def parse_html_meta(raw_html: str, *, site_host: str = "") -> HtmlMetaParser:
    parser = HtmlMetaParser(site_host=site_host)
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        pass
    return parser


def parse_product_links(raw_html: str) -> list[tuple[str, str]]:
    parser = ProductLinkParser()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        pass
    candidates: list[tuple[str, str]] = list(parser.links)
    for loc_match in re.finditer(r"(?is)<loc>\s*(https?://[^<\s]+)\s*</loc>", raw_html):
        candidates.append((loc_match.group(1).strip(), ""))
    return candidates


def strip_html_plain(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    no_scripts = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", raw)
    no_tags = re.sub(r"<[^>]+>", " ", no_scripts)
    return " ".join(html.unescape(no_tags).split()).strip()


def _parse_selector_target(
    selector: str,
) -> tuple[str | None, list[str], list[str], list[tuple[str, str | None]]]:
    parts = [p for p in re.split(r"\s*>\s*|\s+", selector) if p]
    target = parts[-1] if parts else selector
    tag_m = re.match(r"^([A-Za-z][A-Za-z0-9-]*)", target)
    want_tag = tag_m.group(1).lower() if tag_m else None
    want_ids = [m.group(1).lower() for m in re.finditer(r"#([A-Za-z0-9_-]+)", target)]
    want_classes = [m.group(1).lower() for m in re.finditer(r"\.([A-Za-z0-9_-]+)", target)]
    want_attrs: list[tuple[str, str | None]] = []
    for am in re.finditer(
        r"""\[([A-Za-z0-9_-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\]]+)))?\]""", target
    ):
        aval = (
            am.group(2)
            if am.group(2) is not None
            else (am.group(3) if am.group(3) is not None else am.group(4))
        )
        want_attrs.append((am.group(1).lower(), aval.strip() if isinstance(aval, str) else None))
    return want_tag, want_ids, want_classes, want_attrs


def _attrs_match(
    attrs_str: str,
    want_ids: list[str],
    want_classes: list[str],
    want_attrs: list[tuple[str, str | None]],
) -> bool:
    attr_map: dict[str, str] = {}
    for am in _ATTR_RE.finditer(attrs_str):
        val = (
            am.group(2)
            if am.group(2) is not None
            else (am.group(3) if am.group(3) is not None else (am.group(4) or ""))
        )
        attr_map[am.group(1).lower()] = val
    if want_ids and attr_map.get("id", "").strip().lower() not in want_ids:
        return False
    if want_classes:
        cls_tokens = {c.strip().lower() for c in attr_map.get("class", "").split() if c.strip()}
        if not all(wc in cls_tokens for wc in want_classes):
            return False
    for ak, av in want_attrs:
        if ak not in attr_map:
            return False
        if av is not None and attr_map[ak] != av:
            return False
    return True


def _find_balanced_close(raw_html: str, tag_name: str, open_start: int, open_end: int) -> int:
    step_re = re.compile(rf"</?{re.escape(tag_name)}\b[^>]*>", re.I)
    depth = 1
    scan = open_end
    while True:
        nxt = step_re.search(raw_html, scan)
        if nxt is None:
            return min(len(raw_html), open_start + 3000)
        tok = nxt.group(0)
        if tok.startswith("</"):
            depth -= 1
            if depth == 0:
                return nxt.end()
        elif not tok.rstrip().endswith("/>"):
            depth += 1
        scan = nxt.end()


def extract_html_by_selector(raw_html: str, selector: str) -> tuple[str, int]:
    """Extract bounded outer HTML for elements matching a simple CSS selector (tag/#id/.class/[attr])."""
    sel = str(selector or "").strip()
    if not sel:
        return "", 0
    want_tag, want_ids, want_classes, want_attrs = _parse_selector_target(sel)
    if not want_tag and not want_ids and not want_classes and not want_attrs:
        return "", 0

    snippets: list[str] = []
    match_count = 0
    pos = 0
    while pos < len(raw_html) and len(snippets) < 3:
        m = _OPEN_TAG_RE.search(raw_html, pos)
        if m is None:
            break
        pos = m.end()
        tname = m.group(1).lower()
        if want_tag and tname != want_tag:
            continue
        attrs_str = m.group(2) or ""
        if not _attrs_match(attrs_str, want_ids, want_classes, want_attrs):
            continue
        match_count += 1
        if tname in _VOID_TAGS or attrs_str.rstrip().endswith("/"):
            snippets.append(m.group(0))
            continue
        end_idx = _find_balanced_close(raw_html, tname, m.start(), m.end())
        snippets.append(raw_html[m.start() : end_idx])
        pos = max(pos, end_idx)
    return "\n\n".join(snippets), match_count


def extract_json_ld_products(raw_html: str) -> list[dict[str, Any]]:
    """Return Product JSON-LD nodes embedded in ``<script type="application/ld+json">`` blocks."""
    products: list[dict[str, Any]] = []
    for ld_match in re.finditer(
        r'(?is)<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        raw_html,
    ):
        block = ld_match.group(1).strip()
        if not block or len(block) > 200_000:
            continue
        try:
            parsed_ld = json.loads(block)
        except ValueError:
            continue
        nodes: list[dict[str, Any]] = []
        if isinstance(parsed_ld, dict):
            if isinstance(parsed_ld.get("@graph"), list):
                nodes.extend(n for n in parsed_ld["@graph"] if isinstance(n, dict))
            nodes.append(parsed_ld)
        elif isinstance(parsed_ld, list):
            nodes.extend(n for n in parsed_ld if isinstance(n, dict))
        for node in nodes:
            tval = node.get("@type")
            if tval == "Product" or (isinstance(tval, list) and "Product" in tval):
                products.append(node)
                break
    return products


def extract_json_ld_image_url(node: dict[str, Any]) -> str:
    img_node = node.get("image")
    if isinstance(img_node, str):
        return img_node
    if isinstance(img_node, list) and img_node:
        first = img_node[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return str(first.get("url") or "")
    if isinstance(img_node, dict):
        return str(img_node.get("url") or "")
    return ""


def extract_product_dom_fields(raw_html: str) -> dict[str, Any]:
    """Extract fallback WooCommerce product DOM fields (headings, descriptions, price, button, SKU, categories, og:image)."""
    fields: dict[str, Any] = {
        "h1": "",
        "title": "",
        "short_description": "",
        "tab_description": "",
        "regular_price": "",
        "sale_price": "",
        "price": "",
        "button_text": "",
        "sku": "",
        "categories": [],
        "og_image": "",
    }
    h1_match = re.search(r"(?is)<h1\b[^>]*>(.*?)</h1>", raw_html)
    if h1_match:
        fields["h1"] = strip_html_plain(h1_match.group(1))
    title_match = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", raw_html)
    if title_match:
        fields["title"] = strip_html_plain(title_match.group(1))

    short_match = re.search(
        r'(?is)<div\b[^>]*class=["\'][^"\']*woocommerce-product-details__short-description[^"\']*["\'][^>]*>(.*?)</div>',
        raw_html,
    )
    if short_match:
        fields["short_description"] = strip_html_plain(short_match.group(1))
    if not fields["short_description"]:
        meta_match = re.search(
            r'(?is)<meta\b[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']',
            raw_html,
        )
        if meta_match:
            fields["short_description"] = strip_html_plain(meta_match.group(1))

    tab_match = re.search(
        r'(?is)<div\b[^>]*(?:id=["\']tab-description["\']|class=["\'][^"\']*woocommerce-Tabs-panel--description[^"\']*["\'])[^>]*>(.*?)</div>',
        raw_html,
    )
    if tab_match:
        fields["tab_description"] = strip_html_plain(tab_match.group(1))

    price_block = re.search(
        r'(?is)<p\b[^>]*class=["\'][^"\']*\bprice\b[^"\']*["\'][^>]*>(.*?)</p>',
        raw_html,
    )
    if price_block:
        block_html = price_block.group(1)
        del_match = re.search(r"(?is)<del\b[^>]*>(.*?)</del>", block_html)
        ins_match = re.search(r"(?is)<ins\b[^>]*>(.*?)</ins>", block_html)
        if del_match:
            fields["regular_price"] = strip_html_plain(del_match.group(1))
        if ins_match:
            fields["sale_price"] = strip_html_plain(ins_match.group(1))
        fields["price"] = fields["sale_price"] or strip_html_plain(block_html)
    if not fields["price"]:
        amt_match = re.search(
            r'(?is)<span\b[^>]*class=["\'][^"\']*woocommerce-Price-amount[^"\']*["\'][^>]*>(.*?)</span>',
            raw_html,
        )
        if amt_match:
            fields["price"] = strip_html_plain(amt_match.group(1))

    btn_match = re.search(
        r'(?is)<button\b[^>]*class=["\'][^"\']*single_add_to_cart_button[^"\']*["\'][^>]*>(.*?)</button>',
        raw_html,
    )
    if btn_match:
        fields["button_text"] = strip_html_plain(btn_match.group(1))

    sku_match = re.search(
        r'(?is)<span\b[^>]*class=["\'][^"\']*\bsku\b[^"\']*["\'][^>]*>(.*?)</span>',
        raw_html,
    )
    if sku_match:
        fields["sku"] = strip_html_plain(sku_match.group(1))

    cat_block = re.search(
        r'(?is)<span\b[^>]*class=["\'][^"\']*posted_in[^"\']*["\'][^>]*>(.*?)</span>',
        raw_html,
    )
    if cat_block:
        cats: list[str] = []
        for a_match in re.finditer(r"(?is)<a\b[^>]*>(.*?)</a>", cat_block.group(1)):
            c_txt = strip_html_plain(a_match.group(1))
            if c_txt and c_txt not in cats and len(cats) < 15:
                cats.append(c_txt)
        fields["categories"] = cats

    og_img = re.search(
        r'(?is)<meta\b[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
        raw_html,
    )
    if og_img:
        fields["og_image"] = og_img.group(1)

    return fields


__all__ = [
    "HtmlMetaParser",
    "ProductLinkParser",
    "extract_html_by_selector",
    "extract_json_ld_image_url",
    "extract_json_ld_products",
    "extract_product_dom_fields",
    "parse_html_meta",
    "parse_product_links",
    "strip_html_plain",
]
