"""Technical SEO analyzers: HTML <head>/JSON-LD, robots.txt, XML sitemaps, Nordic hreflang."""

from __future__ import annotations

import html as html_lib
import html.parser
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from ecom_ops.integrations._http_guard import pinned_https_request
from ecom_ops.seo.models import SeoIssue, normalize_market
from ecom_ops.seo.policy import (
    ALT_TEXT_MAX,
    ALT_TEXT_MIN,
    META_DESC_MAX,
    META_DESC_MIN,
    SEO_TITLE_MAX,
    SEO_TITLE_MIN,
    is_generic_alt,
    is_generic_anchor,
    is_generic_filename,
)
from ecom_ops.wp_ops.config import is_mock_mode

_MAX_SEO_FETCH_BYTES = 1_500_000
_ALLOWED_SEO_MIMES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/xml",
        "text/xml",
        "application/rss+xml",
    }
)

_NORDIC_HREFLANG_NORMALIZE: dict[str, str] = {
    "nb": "nb-NO",
    "nb-no": "nb-NO",
    "no": "nb-NO",
    "no-no": "nb-NO",
    "nn": "nn-NO",
    "nn-no": "nn-NO",
    "sv": "sv-SE",
    "sv-se": "sv-SE",
    "se": "sv-SE",
    "da": "da-DK",
    "da-dk": "da-DK",
    "dk": "da-DK",
    "x-default": "x-default",
}


class _SeoHtmlParser(html.parser.HTMLParser):
    """Pure-Python parser extracting head metadata, headings, links, images, and JSON-LD."""

    def __init__(self, *, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.page_host = (urlparse(page_url).hostname or "").lower()
        self.html_lang = ""
        self.title_parts: list[str] = []
        self._in_title = False
        self._in_head = False
        self._in_body = False
        self._svg_depth = 0
        self.meta_description = ""
        self.meta_robots = ""
        self.canonical = ""
        self.og: dict[str, str] = {}
        self.twitter: dict[str, str] = {}
        self.hreflang: list[dict[str, str]] = []
        self.headings: list[dict[str, str]] = []
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self.images: list[dict[str, Any]] = []
        self.internal_links: list[dict[str, Any]] = []
        self.external_links: list[dict[str, Any]] = []
        self._anchor_href: str | None = None
        self._anchor_rel: str = ""
        self._anchor_parts: list[str] = []
        self._anchor_fallback: str = ""
        self._anchor_img_alt: str = ""
        self.json_ld_raw: list[str] = []
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []
        self._skip_text_depth = 0
        self._body_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        am = {str(k).lower(): (v or "") for k, v in attrs}

        if t == "svg":
            self._svg_depth += 1
            self._skip_text_depth += 1
            return
        if self._svg_depth > 0:
            return

        if t in {"script", "style", "noscript"}:
            self._skip_text_depth += 1
            if t == "script" and am.get("type", "").split(";", 1)[0].strip().lower() == "application/ld+json":
                self._in_json_ld = True
                self._json_ld_parts = []
            return

        if t == "head":
            self._in_head = True
            return
        if t == "body":
            self._in_head = False
            self._in_body = True
            return

        if t == "html" and not self.html_lang:
            self.html_lang = am.get("lang", "").strip()[:24]
        elif t == "title":
            if (self._in_head or not self._in_body) and not self.title_parts:
                self._in_title = True
        elif t == "meta":
            name = am.get("name", "").strip().lower()
            prop = am.get("property", "").strip().lower()
            content = am.get("content", "").strip()
            if name == "description" and not self.meta_description:
                self.meta_description = content
            elif name == "robots" and not self.meta_robots:
                self.meta_robots = content
            elif prop.startswith("og:") and content:
                self.og.setdefault(prop[3:], content)
            elif name.startswith("twitter:") and content:
                self.twitter.setdefault(name[8:], content)
        elif t == "link":
            rels = {r.strip().lower() for r in am.get("rel", "").split() if r.strip()}
            href = am.get("href", "").strip()
            if "canonical" in rels and href and not self.canonical:
                self.canonical = href
            if "alternate" in rels and am.get("hreflang", "").strip() and href:
                self.hreflang.append(
                    {
                        "hreflang": am.get("hreflang", "").strip(),
                        "href": urljoin(self.page_url, href),
                    }
                )
        elif t in {"h1", "h2", "h3"}:
            self._heading_tag = t
            self._heading_parts = []
        elif t == "img":
            raw_src = am.get("src", "").strip()
            lazy_src = am.get("data-src", "").strip() or am.get("data-lazy-src", "").strip()
            if (not raw_src or raw_src.lower().startswith("data:")) and lazy_src:
                src = lazy_src
            else:
                src = raw_src
            alt = am.get("alt", "").strip()
            width = am.get("width", "").strip()
            height = am.get("height", "").strip()
            filename = (
                ""
                if not src or src.lower().startswith("data:")
                else urlparse(src).path.rsplit("/", 1)[-1]
            )
            if self._anchor_href is not None and alt and not self._anchor_img_alt:
                self._anchor_img_alt = alt
            self.images.append(
                {
                    "src": src,
                    "filename": filename,
                    "alt": alt,
                    "width": width,
                    "height": height,
                    "missing_alt": not bool(alt),
                    "generic_alt": bool(alt) and is_generic_alt(alt),
                    "generic_filename": bool(filename) and is_generic_filename(filename),
                    "missing_dimensions": not (width and height),
                }
            )
        elif t == "a":
            href = am.get("href", "").strip()
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                self._anchor_href = href
                self._anchor_rel = am.get("rel", "").strip().lower()
                self._anchor_parts = []
                self._anchor_fallback = am.get("aria-label", "").strip() or am.get("title", "").strip()
                self._anchor_img_alt = ""

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "svg":
            if self._svg_depth > 0:
                self._svg_depth -= 1
                self._skip_text_depth = max(0, self._skip_text_depth - 1)
            return
        if self._svg_depth > 0:
            return

        if t == "head":
            self._in_head = False
            return
        if t == "body":
            self._in_body = False
            return

        if t in {"script", "style", "noscript"}:
            if t == "script" and self._in_json_ld:
                self._in_json_ld = False
                raw = "".join(self._json_ld_parts).strip()
                if raw:
                    self.json_ld_raw.append(raw)
                self._json_ld_parts = []
            self._skip_text_depth = max(0, self._skip_text_depth - 1)
            return

        if t == "title":
            self._in_title = False
        elif t in {"h1", "h2", "h3"} and self._heading_tag == t:
            txt = " ".join(" ".join(self._heading_parts).split()).strip()
            if txt:
                self.headings.append({"tag": t, "text": txt[:200]})
            self._heading_tag = None
            self._heading_parts = []
        elif t == "a" and self._anchor_href is not None:
            href = urljoin(self.page_url, self._anchor_href)
            visible = " ".join(" ".join(self._anchor_parts).split()).strip()
            anchor_text = (visible or self._anchor_fallback or self._anchor_img_alt)[:160]
            host = (urlparse(href).hostname or "").lower()
            entry = {
                "href": href,
                "anchor": anchor_text,
                "rel": self._anchor_rel,
                "generic_anchor": bool(anchor_text) and is_generic_anchor(anchor_text),
            }
            if not host or host == self.page_host or host == f"www.{self.page_host}" or self.page_host == f"www.{host}":
                self.internal_links.append(entry)
            else:
                self.external_links.append(entry)
            self._anchor_href = None
            self._anchor_rel = ""
            self._anchor_parts = []
            self._anchor_fallback = ""
            self._anchor_img_alt = ""

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_parts.append(data)
            return
        if self._svg_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._in_head:
            return
        if self._heading_tag is not None:
            self._heading_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_parts.append(data)
        if self._skip_text_depth == 0:
            self._body_text_parts.append(data)

    @property
    def word_count(self) -> int:
        text = " ".join(html_lib.unescape(" ".join(self._body_text_parts)).split())
        return len(re.findall(r"[A-Za-z0-9ÅÄÖåäöØøÆæ]+", text))


def _flatten_jsonld_items(parsed: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(parsed, list):
        for entry in parsed:
            items.extend(_flatten_jsonld_items(entry))
    elif isinstance(parsed, dict):
        if isinstance(parsed.get("@graph"), list):
            for node in parsed["@graph"]:
                if isinstance(node, dict):
                    items.append(node)
        else:
            items.append(parsed)
    return items


def _validate_jsonld_blocks(
    raw_blocks: list[str],
    *,
    url: str,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Parse and validate JSON-LD blocks for Product, BreadcrumbList, FAQPage, Organization, Article, HowTo."""
    blocks: list[dict[str, Any]] = []
    types_found: list[str] = []
    issues: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_blocks):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            issues.append(
                SeoIssue(
                    code="invalid_jsonld",
                    severity="high",
                    category="schema",
                    title="Ogiltig JSON-LD strukturerad data",
                    detail=f"JSON-LD block #{idx + 1} kunde inte parsas: {exc.msg}",
                    url=url,
                    suggestion="Korrigera JSON-syntaxen i <script type=\"application/ld+json\">.",
                    fix_type="schema_jsonld",
                ).to_dict()
            )
            continue

        for node in _flatten_jsonld_items(decoded):
            raw_type = node.get("@type")
            node_types = raw_type if isinstance(raw_type, list) else ([raw_type] if raw_type else [])
            clean_types = [str(t) for t in node_types if t]
            for nt in clean_types:
                if nt not in types_found:
                    types_found.append(nt)
            blocks.append({"types": clean_types, "data": node})

            if "Product" in clean_types:
                missing_req = [k for k in ("name", "image", "offers") if not node.get(k)]
                if missing_req:
                    issues.append(
                        SeoIssue(
                            code="product_schema_incomplete",
                            severity="high",
                            category="schema",
                            title="Ofullständig Product JSON-LD",
                            detail=f"Product-schema saknar fält: {', '.join(missing_req)}.",
                            url=url,
                            suggestion="Lägg till name, image och offers (price, priceCurrency, availability) i Product-schemat.",
                            fix_type="schema_jsonld",
                        ).to_dict()
                    )
                if not node.get("sku"):
                    issues.append(
                        SeoIssue(
                            code="product_schema_missing_sku",
                            severity="medium",
                            category="schema",
                            title="Product JSON-LD saknar SKU",
                            detail="Product-schemat saknar 'sku' vilket försämrar produktmatchning i Google Shopping/Rich Results.",
                            url=url,
                            suggestion="Lägg till produktens SKU i Product JSON-LD.",
                            fix_type="schema_jsonld",
                        ).to_dict()
                    )
                offers = node.get("offers")
                offer_obj = offers[0] if isinstance(offers, list) and offers else offers
                if isinstance(offer_obj, dict):
                    miss_offer = [
                        k for k in ("price", "priceCurrency", "availability") if not offer_obj.get(k)
                    ]
                    if miss_offer:
                        issues.append(
                            SeoIssue(
                                code="product_offer_incomplete",
                                severity="high",
                                category="schema",
                                title="Ofullständig Product Offer i JSON-LD",
                                detail=f"offers saknar: {', '.join(miss_offer)}.",
                                url=url,
                                suggestion="Lägg till price, priceCurrency (NOK/SEK/DKK) och availability i offers.",
                                fix_type="schema_jsonld",
                            ).to_dict()
                        )

            if "BreadcrumbList" in clean_types:
                elements = node.get("itemListElement")
                if not isinstance(elements, list) or not elements:
                    issues.append(
                        SeoIssue(
                            code="breadcrumb_schema_empty",
                            severity="low",
                            category="schema",
                            title="Tom BreadcrumbList JSON-LD",
                            detail="BreadcrumbList saknar itemListElement.",
                            url=url,
                            suggestion="Populera itemListElement med position, name och item.",
                            fix_type="schema_jsonld",
                        ).to_dict()
                    )

            if "FAQPage" in clean_types:
                main_entity = node.get("mainEntity")
                if not isinstance(main_entity, list) or not main_entity:
                    issues.append(
                        SeoIssue(
                            code="faq_schema_empty",
                            severity="medium",
                            category="schema",
                            title="Tom FAQPage JSON-LD",
                            detail="FAQPage saknar frågor i mainEntity.",
                            url=url,
                            suggestion="Lägg till Question/Answer-objekt i FAQPage.mainEntity.",
                            fix_type="schema_jsonld",
                        ).to_dict()
                    )

            if "Organization" in clean_types:
                miss_org = [k for k in ("name", "url") if not node.get(k)]
                if miss_org:
                    issues.append(
                        SeoIssue(
                            code="org_schema_incomplete",
                            severity="low",
                            category="schema",
                            title="Ofullständig Organization JSON-LD",
                            detail=f"Organization saknar: {', '.join(miss_org)}.",
                            url=url,
                            suggestion="Komplettera Organization-schema med name, url och logo.",
                            fix_type="schema_jsonld",
                        ).to_dict()
                    )

            if any(t in clean_types for t in ("Article", "BlogPosting", "NewsArticle")):
                miss_art = [k for k in ("headline", "author", "datePublished") if not node.get(k)]
                if miss_art:
                    issues.append(
                        SeoIssue(
                            code="article_schema_incomplete",
                            severity="low",
                            category="schema",
                            title="Ofullständig Article JSON-LD",
                            detail=f"Article-schema saknar: {', '.join(miss_art)}.",
                            url=url,
                            suggestion="Lägg till headline, author och datePublished i Article JSON-LD.",
                            fix_type="schema_jsonld",
                        ).to_dict()
                    )

            if "HowTo" in clean_types and not node.get("step"):
                issues.append(
                    SeoIssue(
                        code="howto_schema_missing_steps",
                        severity="low",
                        category="schema",
                        title="HowTo JSON-LD saknar steg",
                        detail="HowTo-schema saknar 'step'-lista.",
                        url=url,
                        suggestion="Lägg till HowToStep-element i 'step'.",
                        fix_type="schema_jsonld",
                    ).to_dict()
                )

    return blocks, types_found, issues


def analyze_html_seo(
    html_text: str,
    url: str = "https://mock-shop.example.test/",
    *,
    response_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Analyze rendered HTML for technical/on-page/schema/hreflang/image SEO."""
    parser = _SeoHtmlParser(page_url=url)
    try:
        parser.feed(str(html_text or ""))
        parser.close()
    except Exception:
        pass

    title = " ".join("".join(parser.title_parts).split()).strip()
    meta_desc = " ".join(parser.meta_description.split()).strip()
    hdrs = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
    x_robots = hdrs.get("x-robots-tag", "").strip()
    robots_combined = f"{parser.meta_robots} {x_robots}".lower()
    noindex = "noindex" in robots_combined
    nofollow = "nofollow" in robots_combined

    canonical_abs = urljoin(url, parser.canonical) if parser.canonical else ""
    norm_url = url.rstrip("/")
    norm_canon = canonical_abs.rstrip("/")
    canonical_matches = bool(norm_canon and norm_canon == norm_url)

    h1s = [h["text"] for h in parser.headings if h["tag"] == "h1"]
    h2s = [h["text"] for h in parser.headings if h["tag"] == "h2"]
    h3s = [h["text"] for h in parser.headings if h["tag"] == "h3"]

    schema_blocks, schema_types, schema_issues = _validate_jsonld_blocks(
        parser.json_ld_raw, url=url
    )

    issues: list[dict[str, Any]] = list(schema_issues)

    # Title checks
    if not title:
        issues.append(
            SeoIssue(
                code="missing_title",
                severity="critical",
                category="technical",
                title="Saknar <title>-tagg",
                detail=f"Sidan {url} saknar en <title>-tagg.",
                url=url,
                suggestion=f"Lägg till en unik SEO-titel på {SEO_TITLE_MIN}–{SEO_TITLE_MAX} tecken med huvudsökordet.",
                fix_type="seo_meta",
            ).to_dict()
        )
    elif len(title) < SEO_TITLE_MIN:
        issues.append(
            SeoIssue(
                code="title_too_short",
                severity="medium",
                category="on_page",
                title="SEO-titel för kort",
                detail=f"Titeln '{title}' är {len(title)} tecken (rekommenderat {SEO_TITLE_MIN}–{SEO_TITLE_MAX}).",
                url=url,
                suggestion="Utöka titeln med huvudsökord och värdeerbjudande (30–60 tecken).",
                fix_type="seo_meta",
            ).to_dict()
        )
    elif len(title) > SEO_TITLE_MAX:
        issues.append(
            SeoIssue(
                code="title_too_long",
                severity="medium",
                category="on_page",
                title="SEO-titel för lång (risk för avklippning i SERP)",
                detail=f"Titeln är {len(title)} tecken (max rekommenderat {SEO_TITLE_MAX}).",
                url=url,
                suggestion="Korta ned titeln till 50–60 tecken med huvudsökordet tidigt.",
                fix_type="seo_meta",
            ).to_dict()
        )

    # Meta description checks
    if not meta_desc:
        issues.append(
            SeoIssue(
                code="missing_meta_description",
                severity="high",
                category="on_page",
                title="Saknar meta description",
                detail=f"Sidan {url} saknar meta description.",
                url=url,
                suggestion=f"Skriv en säljande meta description på {META_DESC_MIN}–{META_DESC_MAX} tecken med CTA.",
                fix_type="seo_meta",
            ).to_dict()
        )
    elif len(meta_desc) < META_DESC_MIN or len(meta_desc) > META_DESC_MAX:
        issues.append(
            SeoIssue(
                code="meta_description_length",
                severity="low",
                category="on_page",
                title="Meta description utanför optimalt spann",
                detail=f"Meta description är {len(meta_desc)} tecken (optimalt {META_DESC_MIN}–{META_DESC_MAX}).",
                url=url,
                suggestion=f"Justera längden till {META_DESC_MIN}–{META_DESC_MAX} tecken.",
                fix_type="seo_meta",
            ).to_dict()
        )

    # Robots / Canonical
    if noindex:
        issues.append(
            SeoIssue(
                code="page_noindex",
                severity="critical",
                category="technical",
                title="Sidan är markerad noindex",
                detail=f"Robots-direktiv ({parser.meta_robots or x_robots}) hindrar indexering på {url}.",
                url=url,
                suggestion="Ta bort noindex om sidan ska ranka organiskt.",
                fix_type="seo_meta",
            ).to_dict()
        )
    if not canonical_abs:
        issues.append(
            SeoIssue(
                code="missing_canonical",
                severity="high",
                category="technical",
                title="Saknar canonical-tagg",
                detail=f"Sidan {url} saknar <link rel=\"canonical\">.",
                url=url,
                suggestion="Lägg till en självrefererande canonical-URL.",
                fix_type="seo_meta",
            ).to_dict()
        )
    elif not canonical_matches:
        issues.append(
            SeoIssue(
                code="canonical_mismatch",
                severity="medium",
                category="technical",
                title="Canonical pekar på annan URL",
                detail=f"Canonical '{canonical_abs}' skiljer sig från sidans URL '{url}'.",
                url=url,
                suggestion="Verifiera att canonical avsiktligt pekar på en primär version.",
                fix_type="seo_meta",
            ).to_dict()
        )

    # Headings
    if len(h1s) == 0:
        issues.append(
            SeoIssue(
                code="missing_h1",
                severity="high",
                category="on_page",
                title="Saknar H1-rubrik",
                detail=f"Ingen H1 hittades på {url}.",
                url=url,
                suggestion="Lägg till exakt en tydlig H1 som matchar sidans huvudsökord.",
                fix_type="page_seo",
            ).to_dict()
        )
    elif len(h1s) > 1:
        issues.append(
            SeoIssue(
                code="multiple_h1",
                severity="medium",
                category="on_page",
                title="Flera H1-rubriker på samma sida",
                detail=f"{len(h1s)} st H1-rubriker hittades på {url}.",
                url=url,
                suggestion="Behåll en primär H1 och konvertera övriga till H2/H3.",
                fix_type="page_seo",
            ).to_dict()
        )

    # Open Graph
    missing_og = [k for k in ("title", "description", "image") if k not in parser.og]
    if missing_og:
        issues.append(
            SeoIssue(
                code="missing_open_graph",
                severity="low",
                category="technical",
                title="Ofullständiga Open Graph-taggar",
                detail=f"Saknar og:{', og:'.join(missing_og)} på {url}.",
                url=url,
                suggestion="Konfigurera OG-titel, beskrivning och bild i SEO-pluginet.",
                fix_type="seo_meta",
            ).to_dict()
        )

    # JSON-LD presence
    if not schema_blocks:
        issues.append(
            SeoIssue(
                code="missing_jsonld",
                severity="medium",
                category="schema",
                title="Saknar strukturerad data (JSON-LD)",
                detail=f"Inga <script type=\"application/ld+json\">-block hittades på {url}.",
                url=url,
                suggestion="Lägg till Organization/BreadcrumbList (samt Product/FAQPage på produktsidor).",
                fix_type="schema_jsonld",
            ).to_dict()
        )

    # Images
    missing_alt = [img for img in parser.images if img["missing_alt"]]
    generic_alt = [img for img in parser.images if img["generic_alt"]]
    missing_dims = [img for img in parser.images if img["missing_dimensions"]]
    bad_alt_len = [
        img
        for img in parser.images
        if img["alt"] and (len(img["alt"]) < ALT_TEXT_MIN or len(img["alt"]) > ALT_TEXT_MAX)
    ]
    if missing_alt:
        issues.append(
            SeoIssue(
                code="html_images_missing_alt",
                severity="high",
                category="media",
                title=f"{len(missing_alt)} bild(er) saknar alt-text",
                detail=f"Exempel: {missing_alt[0].get('filename') or missing_alt[0].get('src')}",
                url=url,
                suggestion="Lägg till beskrivande alt-texter (5–125 tecken) med produkt-/modellnamn.",
                fix_type="media_alt",
            ).to_dict()
        )
    if generic_alt:
        issues.append(
            SeoIssue(
                code="html_images_generic_alt",
                severity="medium",
                category="media",
                title=f"{len(generic_alt)} bild(er) har generisk alt-text",
                detail=f"Exempel: '{generic_alt[0]['alt']}'",
                url=url,
                suggestion="Byt ut generiska alt-texter mot beskrivande texter.",
                fix_type="media_alt",
            ).to_dict()
        )
    if missing_dims:
        issues.append(
            SeoIssue(
                code="html_images_missing_dimensions",
                severity="low",
                category="media",
                title=f"{len(missing_dims)} bild(er) saknar width/height (CLS-risk)",
                detail="Bilder utan explicita width/height-attribut kan orsaka layoutskiften (CLS).",
                url=url,
                suggestion="Säkerställ att <img>-taggar renderar explicita width- och height-attribut.",
            ).to_dict()
        )
    if bad_alt_len:
        issues.append(
            SeoIssue(
                code="html_images_alt_length",
                severity="low",
                category="media",
                title=f"{len(bad_alt_len)} bild(er) har alt-text utanför {ALT_TEXT_MIN}–{ALT_TEXT_MAX} tecken",
                detail=f"Exempel: '{bad_alt_len[0]['alt'][:60]}'",
                url=url,
                suggestion=f"Håll alt-texter mellan {ALT_TEXT_MIN} och {ALT_TEXT_MAX} tecken.",
                fix_type="media_alt",
            ).to_dict()
        )

    return {
        "url": url,
        "html_lang": parser.html_lang,
        "title": title,
        "title_length": len(title),
        "meta_description": meta_desc,
        "meta_description_length": len(meta_desc),
        "meta_robots": parser.meta_robots,
        "x_robots_tag": x_robots,
        "noindex": noindex,
        "nofollow": nofollow,
        "canonical": canonical_abs,
        "canonical_matches_url": canonical_matches,
        "og": parser.og,
        "twitter": parser.twitter,
        "hreflang": parser.hreflang,
        "headings": {
            "h1": h1s,
            "h2": h2s,
            "h3": h3s,
            "h1_count": len(h1s),
            "outline": parser.headings[:30],
        },
        "word_count": parser.word_count,
        "images_summary": {
            "total": len(parser.images),
            "missing_alt": len(missing_alt),
            "generic_alt": len(generic_alt),
            "missing_dimensions": len(missing_dims),
        },
        "images": parser.images[:50],
        "internal_links_count": len(parser.internal_links),
        "external_links_count": len(parser.external_links),
        "internal_links": parser.internal_links[:100],
        "json_ld_types": schema_types,
        "json_ld_count": len(schema_blocks),
        "issues": issues,
    }


_PRODUCT_ROBOTS_SECTIONS: tuple[str, ...] = (
    "/produkt",
    "/product",
    "/products",
    "/produkter",
    "/shop",
    "/butik",
)


def _robots_blocks_section(rule: str, sections: tuple[str, ...]) -> bool:
    r = str(rule or "").strip()
    if not r or "?" in r:
        return False
    clean = r.rstrip("*").rstrip("/")
    return clean in sections


def analyze_robots_txt(robots_text: str, *, site_url: str = "") -> dict[str, Any]:
    """Parse and audit ``robots.txt`` for crawl blockers and sitemap declarations."""
    lines = str(robots_text or "").splitlines()
    current_agents: list[str] = []
    seen_rule_in_group = False
    disallows_all: list[str] = []
    disallows_googlebot: list[str] = []
    allows: list[str] = []
    sitemaps: list[str] = []
    issues: list[dict[str, Any]] = []

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = [p.strip() for p in line.split(":", 1)]
        low_key = key.lower()
        if low_key == "user-agent":
            agent = val.lower()
            if seen_rule_in_group or not current_agents:
                current_agents = [agent]
                seen_rule_in_group = False
            elif agent not in current_agents:
                current_agents.append(agent)
        elif low_key == "disallow":
            seen_rule_in_group = True
            if val:
                if "*" in current_agents:
                    disallows_all.append(val)
                if "googlebot" in current_agents:
                    disallows_googlebot.append(val)
        elif low_key == "allow":
            seen_rule_in_group = True
            if "*" in current_agents and val:
                allows.append(val)
        elif low_key == "sitemap" and val:
            sitemaps.append(val)

    effective_disallows = disallows_all + disallows_googlebot
    blocks_entire_site = "/" in effective_disallows
    blocks_assets = any(
        d.startswith(("/wp-content/uploads", "/wp-content/themes", "/wp-includes/js"))
        for d in effective_disallows
    )
    blocks_products = any(
        _robots_blocks_section(d, _PRODUCT_ROBOTS_SECTIONS) for d in effective_disallows
    )

    robots_url = f"{site_url.rstrip('/')}/robots.txt" if site_url else "/robots.txt"
    if not str(robots_text or "").strip():
        issues.append(
            SeoIssue(
                code="robots_empty",
                severity="medium",
                category="technical",
                title="robots.txt är tom eller saknas",
                detail="Ingen robots.txt kunde läsas.",
                url=robots_url,
                suggestion="Publicera en tydlig robots.txt som tillåter crawl av produkter/kategorier och listar XML-sitemap.",
            ).to_dict()
        )
    if blocks_entire_site:
        issues.append(
            SeoIssue(
                code="robots_blocks_all",
                severity="critical",
                category="technical",
                title="robots.txt blockerar hela webbplatsen (Disallow: /)",
                detail="User-agent: * har Disallow: /, vilket stoppar sökmotorer från att crawla sajten.",
                url=robots_url,
                suggestion="Ta bort 'Disallow: /' i robots.txt (kontrollera även WordPress 'blog_public').",
            ).to_dict()
        )
    if blocks_assets:
        issues.append(
            SeoIssue(
                code="robots_blocks_assets",
                severity="high",
                category="technical",
                title="robots.txt blockerar statiska resurser/bilder",
                detail="Blockering av uploads/themes/CSS/JS försämrar Googles rendering.",
                url=robots_url,
                suggestion="Tillåt /wp-content/uploads/ och tema-resurser i robots.txt.",
            ).to_dict()
        )
    if blocks_products:
        issues.append(
            SeoIssue(
                code="robots_blocks_products",
                severity="critical",
                category="technical",
                title="robots.txt blockerar produkt- eller butiks-URL:er",
                detail="Produkt- eller shop-sökvägar är blockerade i robots.txt.",
                url=robots_url,
                suggestion="Ta bort Disallow-regler för produkt- och kategorisökvägar.",
            ).to_dict()
        )
    if not sitemaps:
        issues.append(
            SeoIssue(
                code="robots_missing_sitemap",
                severity="medium",
                category="technical",
                title="robots.txt saknar Sitemap-direktiv",
                detail="Ingen Sitemap:-rad hittades i robots.txt.",
                url=robots_url,
                suggestion="Lägg till 'Sitemap: https://<domän>/sitemap_index.xml' i robots.txt.",
            ).to_dict()
        )

    return {
        "robots_url": robots_url,
        "blocks_entire_site": blocks_entire_site,
        "blocks_assets": blocks_assets,
        "blocks_products": blocks_products,
        "disallows_wildcard": disallows_all,
        "allows_wildcard": allows,
        "sitemaps": sitemaps,
        "issues": issues,
    }


def analyze_sitemap_xml(xml_text: str, *, sitemap_url: str = "") -> dict[str, Any]:
    """Parse and audit an XML sitemap or sitemap index safely."""
    raw = str(xml_text or "").lstrip("\ufeff").strip()
    issues: list[dict[str, Any]] = []
    if not raw:
        issues.append(
            SeoIssue(
                code="sitemap_empty",
                severity="high",
                category="technical",
                title="XML-sitemap saknas eller är tom",
                detail=f"Ingen XML-data hittades vid {sitemap_url or 'sitemap'}.",
                url=sitemap_url,
                suggestion="Aktivera XML-sitemap i SEO-pluginet (t.ex. Yoast/RankMath) och länka från robots.txt.",
            ).to_dict()
        )
        return {
            "sitemap_url": sitemap_url,
            "kind": "empty",
            "valid_xml": False,
            "url_count": 0,
            "child_sitemaps": [],
            "urls": [],
            "non_https_urls": [],
            "duplicate_urls": [],
            "issues": issues,
        }

    # Refuse DTD / XXE constructs defensively
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        issues.append(
            SeoIssue(
                code="sitemap_dtd_refused",
                severity="high",
                category="technical",
                title="XML-sitemap innehåller otillåten DTD/ENTITY-deklaration",
                detail="DTD/ENTITY-deklarationer i XML-sitemaps avvisas av säkerhetsskäl.",
                url=sitemap_url,
                suggestion="Generera en ren standard-XML-sitemap utan DOCTYPE/ENTITY.",
            ).to_dict()
        )
        return {
            "sitemap_url": sitemap_url,
            "kind": "invalid",
            "valid_xml": False,
            "url_count": 0,
            "child_sitemaps": [],
            "urls": [],
            "non_https_urls": [],
            "duplicate_urls": [],
            "issues": issues,
        }

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        issues.append(
            SeoIssue(
                code="sitemap_invalid_xml",
                severity="high",
                category="technical",
                title="Ogiltig XML i sitemap",
                detail=f"XML-parsningsfel: {exc}",
                url=sitemap_url,
                suggestion="Kontrollera att inga PHP-varningar eller HTML läcker ut i sitemap-svaret.",
            ).to_dict()
        )
        return {
            "sitemap_url": sitemap_url,
            "kind": "invalid",
            "valid_xml": False,
            "url_count": 0,
            "child_sitemaps": [],
            "urls": [],
            "non_https_urls": [],
            "duplicate_urls": [],
            "issues": issues,
        }

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    root_name = _local(root.tag)
    if root_name not in {"sitemapindex", "urlset"}:
        issues.append(
            SeoIssue(
                code="sitemap_invalid_xml",
                severity="high",
                category="technical",
                title="XML-svaret är inte en giltig sitemap",
                detail=f"Rot-elementet <{root_name}> är varken <urlset> eller <sitemapindex>.",
                url=sitemap_url,
                suggestion="Kontrollera att sitemap-URL:en returnerar en standard XML-sitemap (inte HTML/RSS).",
            ).to_dict()
        )
        return {
            "sitemap_url": sitemap_url,
            "kind": "invalid",
            "valid_xml": False,
            "url_count": 0,
            "child_sitemaps": [],
            "urls": [],
            "non_https_urls": [],
            "duplicate_urls": [],
            "issues": issues,
        }

    child_sitemaps: list[str] = []
    urls: list[dict[str, Any]] = []
    seen_locs: set[str] = set()
    duplicates: list[str] = []
    non_https: list[str] = []
    missing_lastmod = 0

    if root_name == "sitemapindex":
        kind = "sitemapindex"
        for child in root:
            if _local(child.tag) != "sitemap":
                continue
            loc = ""
            for sub in child:
                if _local(sub.tag) == "loc" and sub.text:
                    loc = sub.text.strip()
            if loc:
                child_sitemaps.append(loc)
                if loc.lower().startswith("http://"):
                    non_https.append(loc)
    else:
        kind = "urlset"
        for child in root:
            if _local(child.tag) != "url":
                continue
            loc = ""
            lastmod = ""
            for sub in child:
                lt = _local(sub.tag)
                if lt == "loc" and sub.text:
                    loc = sub.text.strip()
                elif lt == "lastmod" and sub.text:
                    lastmod = sub.text.strip()
            if not loc:
                continue
            if loc in seen_locs:
                duplicates.append(loc)
            seen_locs.add(loc)
            if loc.lower().startswith("http://"):
                non_https.append(loc)
            if not lastmod:
                missing_lastmod += 1
            urls.append({"loc": loc, "lastmod": lastmod})

    if (kind == "urlset" and not urls) or (kind == "sitemapindex" and not child_sitemaps):
        issues.append(
            SeoIssue(
                code="sitemap_empty",
                severity="high",
                category="technical",
                title="XML-sitemap innehåller inga URL:er",
                detail=f"Sitemapen vid {sitemap_url or 'sitemap'} är giltig XML men saknar poster.",
                url=sitemap_url,
                suggestion="Kontrollera sitemap-inställningarna i SEO-pluginet och generera om sitemapen.",
            ).to_dict()
        )

    if non_https:
        issues.append(
            SeoIssue(
                code="sitemap_non_https",
                severity="high",
                category="technical",
                title=f"{len(non_https)} HTTP-URL:er (ej HTTPS) i sitemap",
                detail=f"Exempel: {non_https[0]}",
                url=sitemap_url,
                suggestion="Uppdatera WordPress siteurl/home till HTTPS och rensa sitemap-cache.",
            ).to_dict()
        )
    if duplicates:
        issues.append(
            SeoIssue(
                code="sitemap_duplicate_urls",
                severity="medium",
                category="technical",
                title=f"{len(duplicates)} duplicerade URL:er i sitemap",
                detail=f"Exempel: {duplicates[0]}",
                url=sitemap_url,
                suggestion="Ta bort duplicerade poster från XML-sitemapen.",
            ).to_dict()
        )
    if kind == "urlset" and urls and missing_lastmod == len(urls):
        issues.append(
            SeoIssue(
                code="sitemap_missing_lastmod",
                severity="low",
                category="technical",
                title="Sitemap saknar <lastmod>-datum",
                detail="Alla URL:er i sitemapen saknar <lastmod>.",
                url=sitemap_url,
                suggestion="Aktivera <lastmod> i SEO-pluginets sitemap-inställningar.",
            ).to_dict()
        )

    return {
        "sitemap_url": sitemap_url,
        "kind": kind,
        "valid_xml": True,
        "url_count": len(urls) if kind == "urlset" else len(child_sitemaps),
        "child_sitemaps": child_sitemaps[:50],
        "urls": urls[:100],
        "non_https_urls": non_https[:20],
        "duplicate_urls": duplicates[:20],
        "issues": issues,
    }


def _norm_hreflang_code(code: str) -> str:
    low = str(code or "").strip().lower().replace("_", "-")
    return _NORDIC_HREFLANG_NORMALIZE.get(low, low)


def _norm_hreflang_url(raw_url: str) -> str:
    raw = str(raw_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.hostname:
        scheme = (parsed.scheme or "https").lower()
        host = parsed.hostname.lower()
        port = f":{parsed.port}" if parsed.port and parsed.port not in {80, 443} else ""
        path = unquote(parsed.path or "/").rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{scheme}://{host}{port}{path}{query}"
    return unquote(raw).rstrip("/")


def audit_nordic_hreflang(pages_by_url: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Audit SE/NO/DK cross-market hreflang symmetry, self-ref, x-default, and noindex conflicts."""
    norm_pages: dict[str, dict[str, Any]] = {
        _norm_hreflang_url(url): data for url, data in (pages_by_url or {}).items()
    }
    missing_self_ref: list[str] = []
    missing_x_default: list[str] = []
    missing_return_links: list[dict[str, str]] = []
    noindex_conflicts: list[dict[str, str]] = []
    ambiguous_codes: list[dict[str, str]] = []
    reciprocal_pairs: list[tuple[str, str]] = []
    markets_covered: set[str] = set()
    issues: list[dict[str, Any]] = []

    for page_url, analysis in norm_pages.items():
        links = list(analysis.get("hreflang") or [])
        if not links:
            continue

        codes_seen: set[str] = set()
        hrefs_seen: set[str] = set()
        for entry in links:
            raw_code = str(entry.get("hreflang") or "").strip()
            target = _norm_hreflang_url(str(entry.get("href") or ""))
            norm_code = _norm_hreflang_code(raw_code)
            codes_seen.add(norm_code)
            if target:
                hrefs_seen.add(target)

            if raw_code.lower() in {"se", "dk"}:
                ambiguous_codes.append({"url": page_url, "code": raw_code, "expected": norm_code})
            if norm_code.startswith(("nb", "nn")):
                markets_covered.add("no")
            elif norm_code.startswith("sv"):
                markets_covered.add("se")
            elif norm_code.startswith("da"):
                markets_covered.add("dk")

            if target and target != page_url and target in norm_pages:
                target_analysis = norm_pages[target]
                if target_analysis.get("noindex"):
                    noindex_conflicts.append({"from": page_url, "to": target, "hreflang": raw_code})
                target_hrefs = {
                    _norm_hreflang_url(str(x.get("href") or ""))
                    for x in (target_analysis.get("hreflang") or [])
                }
                if page_url in target_hrefs:
                    pair = tuple(sorted((page_url, target)))
                    if pair not in reciprocal_pairs:
                        reciprocal_pairs.append(pair)
                else:
                    missing_return_links.append(
                        {"from": page_url, "to": target, "hreflang": raw_code}
                    )

        if page_url not in hrefs_seen:
            missing_self_ref.append(page_url)
        if "x-default" not in codes_seen:
            missing_x_default.append(page_url)

    for url in missing_self_ref:
        issues.append(
            SeoIssue(
                code="hreflang_missing_self_ref",
                severity="high",
                category="hreflang",
                title="Hreflang saknar självreferens",
                detail=f"{url} listar hreflang-alternativ men pekar inte på sig själv.",
                url=url,
                suggestion="Lägg till en självrefererande <link rel=\"alternate\" hreflang=\"...\"> på sidan.",
            ).to_dict()
        )
    for url in missing_x_default:
        issues.append(
            SeoIssue(
                code="hreflang_missing_x_default",
                severity="medium",
                category="hreflang",
                title="Hreflang saknar x-default",
                detail=f"{url} saknar fallback-tagg hreflang=\"x-default\".",
                url=url,
                suggestion="Lägg till hreflang=\"x-default\" som pekar på huvudmarknaden.",
            ).to_dict()
        )
    for miss in missing_return_links:
        issues.append(
            SeoIssue(
                code="hreflang_missing_reciprocity",
                severity="high",
                category="hreflang",
                title="Bruten hreflang-reciprocitet mellan nordiska marknader",
                detail=f"{miss['from']} pekar på {miss['to']} ({miss['hreflang']}), men saknar returlänk tillbaka.",
                url=miss["from"],
                suggestion="Säkerställ ömsesidiga hreflang-länkar mellan NO (nb-NO), SE (sv-SE) och DK (da-DK).",
            ).to_dict()
        )
    for conflict in noindex_conflicts:
        issues.append(
            SeoIssue(
                code="hreflang_noindex_conflict",
                severity="critical",
                category="hreflang",
                title="Hreflang-alternativ är markerat noindex",
                detail=f"{conflict['from']} pekar med hreflang={conflict['hreflang']} på {conflict['to']} som har noindex.",
                url=conflict["to"],
                suggestion="Ta bort noindex på målsidan eller ta bort den från hreflang-klustret.",
            ).to_dict()
        )
    for amb in ambiguous_codes:
        issues.append(
            SeoIssue(
                code="hreflang_invalid_code",
                severity="medium",
                category="hreflang",
                title=f"Felaktig hreflang-språkkod '{amb['code']}'",
                detail=f"Använd ISO 639-1 språkkod '{amb['expected']}' istället för landskoden '{amb['code']}'.",
                url=amb["url"],
                suggestion=f"Byt hreflang=\"{amb['code']}\" till hreflang=\"{amb['expected']}\".",
            ).to_dict()
        )

    return {
        "checked_urls": list(norm_pages.keys()),
        "markets_covered": sorted(markets_covered),
        "reciprocal_pairs_count": len(reciprocal_pairs),
        "missing_self_ref": missing_self_ref,
        "missing_x_default": missing_x_default,
        "missing_return_links": missing_return_links,
        "noindex_conflicts": noindex_conflicts,
        "ambiguous_codes": ambiguous_codes,
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# Deterministic Mock Fixtures & SSRF-Safe Live Fetch Wrapper
# --------------------------------------------------------------------------- #


def _mock_origin(market: str, base_url: str = "") -> str:
    if base_url and base_url.startswith("https://"):
        return base_url.rstrip("/")
    mkt = normalize_market(market)
    return {"no": "https://azom.no", "se": "https://azom.se", "dk": "https://azom.dk"}[mkt]


def mock_technical_fixtures(
    *,
    site_id: str = "mock-shop",
    market: str = "no",
    path: str = "/",
    base_url: str = "",
) -> dict[str, Any]:
    """Deterministic Nordic CarPlay mock HTML, robots.txt, sitemap, and cross-market hreflang."""
    mkt = normalize_market(market)
    origin = _mock_origin(mkt, base_url)
    clean_path = "/" + str(path or "/").lstrip("/")
    page_url = f"{origin}{clean_path}"

    # Nordic cross-market mock pages (intentionally includes realistic fixable issues:
    # - Product JSON-LD missing sku
    # - 1 image missing alt and width/height
    # - DK alternate missing return link to NO + missing x-default on DK)
    no_url = "https://azom.no/" if clean_path == "/" else f"https://azom.no{clean_path}"
    se_url = "https://azom.se/" if clean_path == "/" else f"https://azom.se{clean_path}"
    dk_url = "https://azom.dk/" if clean_path == "/" else f"https://azom.dk{clean_path}"

    html_no = f"""<!doctype html>
<html lang="nb-NO">
<head>
  <title>Azom Trådløs Apple CarPlay &amp; Android Auto Skjerm til Bil | Rask Levering i Norge</title>
  <meta name="description" content="Oppgrader bilen med trådløs Apple CarPlay og Android Auto fra Azom. Plug &amp; play-montering på 5 minutter, 2 års garanti og rask levering i hele Norge.">
  <meta name="robots" content="index, follow">
  <link rel="canonical" href="{no_url}">
  <link rel="alternate" hreflang="nb-NO" href="{no_url}">
  <link rel="alternate" hreflang="sv-SE" href="{se_url}">
  <link rel="alternate" hreflang="da-DK" href="{dk_url}">
  <link rel="alternate" hreflang="x-default" href="{no_url}">
  <meta property="og:title" content="Azom Trådløs CarPlay Skjerm">
  <meta property="og:description" content="Plug &amp; play CarPlay og Android Auto for eldre og nyere biler.">
  <meta property="og:image" content="{origin}/wp-content/uploads/carplay-pro-hero.webp">
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@graph": [
      {{
        "@type": "Organization",
        "name": "Azom",
        "url": "https://azom.no",
        "logo": "https://azom.no/wp-content/uploads/azom-logo.webp"
      }},
      {{
        "@type": "Product",
        "name": "Azom CarPlay Skjerm Pro 9\\"",
        "image": "https://azom.no/wp-content/uploads/carplay-pro-hero.webp",
        "offers": {{
          "@type": "Offer",
          "price": "2490",
          "priceCurrency": "NOK",
          "availability": "https://schema.org/InStock"
        }}
      }},
      {{
        "@type": "BreadcrumbList",
        "itemListElement": [
          {{"@type": "ListItem", "position": 1, "name": "Hjem", "item": "https://azom.no/"}},
          {{"@type": "ListItem", "position": 2, "name": "Apple CarPlay", "item": "https://azom.no/kategori/apple-carplay/"}}
        ]
      }}
    ]
  }}
  </script>
</head>
<body>
  <h1>Trådløs Apple CarPlay &amp; Android Auto skjerm til din bil</h1>
  <p>Azom leverer markedets enkleste plug-and-play CarPlay-skjermer og adaptere for nordiske bilister. Passer Volvo, Volkswagen, Toyota, BMW, Audi og de fleste biler med 12V-uttak.</p>
  <h2>Hvorfor velge Azom CarPlay?</h2>
  <p>Rask oppstart, skarpt IPS-panel, ryggekamera-støtte og norsk kundeservice.</p>
  <img src="{origin}/wp-content/uploads/carplay-pro-hero.webp" alt="Azom 9 tommers trådløs CarPlay-skjerm montert på dashbord" width="1200" height="800">
  <img src="{origin}/wp-content/uploads/IMG_4821.jpg" alt="">
  <a href="{origin}/produkt/carplay-skarm-pro/">Azom CarPlay Skjerm Pro</a>
  <a href="{origin}/guide/montera-carplay/">Les mer</a>
</body>
</html>"""

    html_se = f"""<!doctype html>
<html lang="sv-SE">
<head>
  <title>Azom Trådlös CarPlay-skärm &amp; Adapter för Bil | Azom.se</title>
  <meta name="description" content="Uppgradera din bil med trådlös Apple CarPlay och Android Auto. Enkel montering på 5 minuter, snabb leverans i Sverige och 2 års garanti hos Azom.">
  <meta name="robots" content="index, follow">
  <link rel="canonical" href="{se_url}">
  <link rel="alternate" hreflang="nb-NO" href="{no_url}">
  <link rel="alternate" hreflang="sv-SE" href="{se_url}">
  <link rel="alternate" hreflang="da-DK" href="{dk_url}">
  <link rel="alternate" hreflang="x-default" href="{no_url}">
  <meta property="og:title" content="Azom Trådlös CarPlay-skärm">
  <meta property="og:description" content="Plug &amp; play CarPlay och Android Auto för svenska bilister.">
  <meta property="og:image" content="https://azom.se/wp-content/uploads/carplay-pro-hero.webp">
</head>
<body>
  <h1>Trådlös Apple CarPlay &amp; Android Auto skärm för bil</h1>
  <p>Montera trådlös CarPlay i din bil utan verktyg. Kompatibel med iOS och Android.</p>
  <a href="https://azom.se/produkt/carplay-skarm-pro/">Se CarPlay Skärm Pro</a>
</body>
</html>"""

    html_dk = f"""<!doctype html>
<html lang="da-DK">
<head>
  <title>Azom Trådløs CarPlay Skærm til Bil | Azom.dk</title>
  <meta name="description" content="Få trådløs Apple CarPlay og Android Auto i din bil på 5 minutter. Hurtig levering i Danmark og 2 års garanti fra Azom.">
  <meta name="robots" content="index, follow">
  <link rel="canonical" href="{dk_url}">
  <link rel="alternate" hreflang="da-DK" href="{dk_url}">
  <link rel="alternate" hreflang="sv-SE" href="{se_url}">
</head>
<body>
  <h1>Trådløs CarPlay skærm til bil</h1>
  <p>Plug-and-play opgradering til ældre biler i Danmark.</p>
</body>
</html>"""

    robots_txt = (
        "User-agent: *\n"
        "Disallow: /wp-admin/\n"
        "Disallow: /cart/\n"
        "Disallow: /checkout/\n"
        "Allow: /wp-admin/admin-ajax.php\n"
        f"Sitemap: {origin}/sitemap_index.xml\n"
    )

    sitemap_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{origin}/</loc><lastmod>2026-11-20</lastmod></url>
  <url><loc>{origin}/kategori/apple-carplay/</loc><lastmod>2026-11-20</lastmod></url>
  <url><loc>{origin}/produkt/carplay-skarm-pro/</loc><lastmod>2026-11-22</lastmod></url>
  <url><loc>{origin}/produkt/carplay-adapter-mini/</loc><lastmod>2026-11-21</lastmod></url>
  <url><loc>{origin}/guide/montera-carplay/</loc><lastmod>2026-11-18</lastmod></url>
</urlset>"""

    primary_html = {"no": html_no, "se": html_se, "dk": html_dk}[mkt]
    return {
        "site_id": site_id,
        "market": mkt,
        "origin": origin,
        "page_url": page_url,
        "html": primary_html,
        "robots_txt": robots_txt,
        "sitemap_xml": sitemap_xml,
        "nordic_pages": {
            no_url: html_no,
            se_url: html_se,
            dk_url: html_dk,
        },
    }


def fetch_public_seo_resource(
    url: str,
    *,
    use_mock: bool | None = None,
    site_id: str = "mock-shop",
    market: str = "no",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Fetch a public HTML/robots/sitemap URL via SSRF-safe DNS-pinned HTTPS or mock fixtures."""
    mock = is_mock_mode() if use_mock is None else bool(use_mock)
    if mock:
        parsed = urlparse(str(url or ""))
        path = parsed.path or "/"
        origin = f"https://{parsed.netloc}" if parsed.netloc else ""
        fx = mock_technical_fixtures(site_id=site_id, market=market, path=path, base_url=origin)
        if path.endswith("robots.txt"):
            return {
                "ok": True,
                "status": 200,
                "url": url,
                "final_url": url,
                "headers": {"content-type": "text/plain; charset=utf-8"},
                "text": fx["robots_txt"],
                "provenance": "mock",
            }
        if path.endswith(".xml"):
            return {
                "ok": True,
                "status": 200,
                "url": url,
                "final_url": url,
                "headers": {"content-type": "application/xml; charset=utf-8"},
                "text": fx["sitemap_xml"],
                "provenance": "mock",
            }
        return {
            "ok": True,
            "status": 200,
            "url": url,
            "final_url": fx["page_url"],
            "headers": {"content-type": "text/html; charset=utf-8"},
            "text": fx["html"],
            "provenance": "mock",
        }

    try:
        status, resp_headers, raw, final_url = pinned_https_request(
            url,
            method="GET",
            headers={
                "User-Agent": "AzomSeoStudio/1.0 (read-only technical audit)",
                "Accept": "text/html,application/xhtml+xml,application/xml,text/plain;q=0.9",
            },
            connect_timeout=5.0,
            read_timeout=float(timeout),
            max_redirects=3,
            max_raw_bytes=_MAX_SEO_FETCH_BYTES,
            max_decompressed_bytes=_MAX_SEO_FETCH_BYTES * 2,
            allowed_content_types=_ALLOWED_SEO_MIMES,
        )
        ct_header = ""
        for hk, hv in (resp_headers or {}).items():
            if str(hk).lower() == "content-type":
                ct_header = str(hv)
                break
        charset_m = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", ct_header, re.I)
        encoding = charset_m.group(1) if charset_m else "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        is_ok = 200 <= status < 300
        return {
            "ok": is_ok,
            "status": status,
            "url": url,
            "final_url": final_url,
            "headers": resp_headers,
            "text": text,
            "provenance": "live",
            **({"message": f"HTTP {status}"} if not is_ok else {}),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "url": url,
            "final_url": url,
            "headers": {},
            "text": "",
            "provenance": "live",
            "error_class": type(exc).__name__,
            "message": str(exc)[:200],
        }


# --------------------------------------------------------------------------- #
# Deep Multi-Page Sitemap Crawl (Proposal 3)
# --------------------------------------------------------------------------- #


def expand_sitemap_index(
    xml_text: str,
    *,
    max_children: int = 5,
    fetcher: Callable[[str], Any] | None = None,
    use_mock: bool | None = None,
    site_id: str = "mock-shop",
    market: str = "no",
    sitemap_url: str = "",
) -> dict[str, Any]:
    """Expand a sitemap or ``<sitemapindex>`` into concrete page URLs (up to ``max_children`` children)."""
    sm = analyze_sitemap_xml(xml_text, sitemap_url=sitemap_url)
    issues: list[dict[str, Any]] = list(sm.get("issues") or [])
    if not sm.get("valid_xml"):
        return {
            "ok": False,
            "kind": sm.get("kind", "invalid"),
            "child_sitemaps": [],
            "urls": [],
            "entries": [],
            "issues": issues,
        }

    if sm.get("kind") == "urlset":
        entries = list(sm.get("urls") or [])
        return {
            "ok": True,
            "kind": "urlset",
            "child_sitemaps": [],
            "urls": [str(e.get("loc") or "") for e in entries if e.get("loc")],
            "entries": entries,
            "issues": issues,
        }

    # sitemapindex: fetch up to max_children child sitemaps
    cap = max(1, min(int(max_children or 5), 25))
    children = list(sm.get("child_sitemaps") or [])[:cap]
    mock = is_mock_mode() if use_mock is None else bool(use_mock)
    all_entries: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for child_url in children:
        raw_res: Any
        if fetcher is not None:
            raw_res = fetcher(child_url)
        else:
            raw_res = fetch_public_seo_resource(
                child_url,
                use_mock=mock,
                site_id=site_id,
                market=market,
            )
        if isinstance(raw_res, str):
            child_ok = True
            child_xml = raw_res
        elif isinstance(raw_res, dict):
            child_ok = bool(raw_res.get("ok", True))
            child_xml = str(raw_res.get("text") or "")
        else:
            child_ok = False
            child_xml = ""

        if not child_ok or not child_xml.strip():
            issues.append(
                SeoIssue(
                    code="sitemap_child_fetch_failed",
                    severity="medium",
                    category="technical",
                    title=f"Kunde inte hämta undersitemap {child_url}",
                    detail=f"Undersitemapen i sitemap_index svarade inte med giltig XML ({child_url}).",
                    url=child_url,
                    suggestion="Verifiera att alla undersitemaps i sitemap_index svarar med HTTP 200 och giltig XML.",
                ).to_dict()
            )
            continue

        child_sm = analyze_sitemap_xml(child_xml, sitemap_url=child_url)
        issues.extend(child_sm.get("issues") or [])
        for entry in child_sm.get("urls") or []:
            loc = str(entry.get("loc") or "").strip()
            if loc and loc not in seen_urls:
                seen_urls.add(loc)
                all_entries.append(entry)

    return {
        "ok": True,
        "kind": "sitemapindex",
        "child_sitemaps": children,
        "urls": [str(e.get("loc") or "") for e in all_entries if e.get("loc")],
        "entries": all_entries,
        "issues": issues,
    }


def _mock_crawl_fetch(url: str, *, site_id: str = "mock-shop", market: str = "no") -> dict[str, Any]:
    """Deterministic multi-page mock crawler response with realistic statuses & SEO edge cases."""
    parsed = urlparse(str(url or ""))
    path = (parsed.path or "/").rstrip("/") or "/"
    origin = f"https://{parsed.netloc}" if parsed.netloc else _mock_origin(market)

    if any(tok in path.lower() for tok in ("/404", "/utgatt", "/broken")):
        return {
            "ok": False,
            "status": 404,
            "url": url,
            "final_url": url,
            "redirect_chain": [],
            "headers": {"content-type": "text/html; charset=utf-8"},
            "text": "<!doctype html><html><head><title>404 Not Found</title></head><body><h1>404</h1></body></html>",
            "provenance": "mock",
        }
    if any(tok in path.lower() for tok in ("/500", "/error")):
        return {
            "ok": False,
            "status": 500,
            "url": url,
            "final_url": url,
            "redirect_chain": [],
            "headers": {"content-type": "text/html; charset=utf-8"},
            "text": "Internal Server Error",
            "provenance": "mock",
        }
    if any(tok in path.lower() for tok in ("/old-", "/gammal-")):
        final_u = f"{origin}/produkt/carplay-skarm-pro/"
        return {
            "ok": True,
            "status": 200,
            "url": url,
            "final_url": final_u,
            "redirect_chain": [url, f"{origin}/produkt/carplay-old/", final_u],
            "headers": {"content-type": "text/html; charset=utf-8"},
            "text": mock_technical_fixtures(site_id=site_id, market=market, path="/produkt/carplay-skarm-pro/", base_url=origin)["html"],
            "provenance": "mock",
        }
    if "noindex" in path.lower():
        return {
            "ok": True,
            "status": 200,
            "url": url,
            "final_url": url,
            "redirect_chain": [],
            "headers": {"content-type": "text/html; charset=utf-8"},
            "text": (
                f'<!doctype html><html lang="nb-NO"><head>'
                f'<title>Test Noindex Sida | Azom</title>'
                f'<meta name="robots" content="noindex, follow">'
                f'<link rel="canonical" href="{origin}/">'
                f'</head><body><h1>Noindex sida</h1></body></html>'
            ),
            "provenance": "mock",
        }

    fx = mock_technical_fixtures(site_id=site_id, market=market, path=path, base_url=origin)
    return {
        "ok": True,
        "status": 200,
        "url": url,
        "final_url": url,
        "redirect_chain": [],
        "headers": {"content-type": "text/html; charset=utf-8"},
        "text": fx["html"],
        "provenance": "mock",
    }


def crawl_site_pages(
    urls: list[str],
    *,
    max_pages: int = 10,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_mock: bool | None = None,
    site_id: str = "mock-shop",
    market: str = "no",
) -> dict[str, Any]:
    """Crawl up to ``max_pages`` sitemap/site URLs for 4xx/5xx, redirect chains, and noindex/canonical conflicts."""
    cap = max(1, min(int(max_pages or 10), 50))
    unique_urls = list(dict.fromkeys(str(u).strip() for u in (urls or []) if str(u).strip()))[:cap]
    mock = is_mock_mode() if use_mock is None else bool(use_mock)

    crawled: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    ok_count = 0
    err_4xx = 0
    err_5xx = 0
    redirect_count = 0
    noindex_count = 0
    canonical_conflict_count = 0

    for target_url in unique_urls:
        if fetcher is not None:
            res = fetcher(target_url)
        elif mock:
            res = _mock_crawl_fetch(target_url, site_id=site_id, market=market)
        else:
            res = fetch_public_seo_resource(
                target_url,
                use_mock=False,
                site_id=site_id,
                market=market,
            )

        status = int(res.get("status") or 0)
        final_url = str(res.get("final_url") or target_url).strip()
        raw_chain = list(res.get("redirect_chain") or [])
        norm_req = target_url.rstrip("/")
        norm_fin = final_url.rstrip("/")
        redirected = bool(raw_chain and len(raw_chain) >= 2) or (norm_fin != norm_req) or (300 <= status < 400)
        redirect_hops = max(len(raw_chain) - 1, 1 if redirected else 0)
        page_issue_codes: list[str] = []

        if status in {404, 410}:
            err_4xx += 1
            page_issue_codes.append("crawl_http_404")
            issues.append(
                SeoIssue(
                    code="crawl_http_404",
                    severity="high",
                    category="technical",
                    title=f"Sitemap-URL svarar HTTP {status}",
                    detail=f"{target_url} returnerade HTTP {status} vid crawl.",
                    url=target_url,
                    suggestion="Ta bort döda URL:er från XML-sitemapen eller lägg in 301-redirect till motsvarande aktiv sida.",
                ).to_dict()
            )
        elif 400 <= status < 500:
            err_4xx += 1
            page_issue_codes.append("crawl_http_4xx")
            issues.append(
                SeoIssue(
                    code="crawl_http_4xx",
                    severity="high",
                    category="technical",
                    title=f"Klientfel HTTP {status} på crawlad URL",
                    detail=f"{target_url} returnerade HTTP {status}.",
                    url=target_url,
                    suggestion="Kontrollera åtkomst/behörighet eller ta bort URL:en ur sitemapen.",
                ).to_dict()
            )
        elif status >= 500 or (status == 0 and not res.get("ok")):
            err_5xx += 1
            page_issue_codes.append("crawl_http_5xx")
            issues.append(
                SeoIssue(
                    code="crawl_http_5xx",
                    severity="critical",
                    category="technical",
                    title=f"Serverfel (HTTP {status or 'timeout'}) på {target_url}",
                    detail=f"Crawl av {target_url} misslyckades ({res.get('message') or f'HTTP {status}'}).",
                    url=target_url,
                    suggestion="Kontrollera PHP/WooCommerce-felloggar och säkerställ HTTP 200 OK.",
                ).to_dict()
            )

        if redirected:
            redirect_count += 1
            if redirect_hops >= 2 or (300 <= status < 400):
                page_issue_codes.append("crawl_redirect_chain")
                issues.append(
                    SeoIssue(
                        code="crawl_redirect_chain",
                        severity="medium",
                        category="technical",
                        title=f"Redirect-kedja ({redirect_hops} hopp) på sitemap-URL",
                        detail=f"{target_url} omdirigerades till {final_url} ({redirect_hops} hopp).",
                        url=target_url,
                        suggestion="Peka direkt på slut-URL:en i sitemap och internlänkar utan redirect-kedjor.",
                    ).to_dict()
                )
            else:
                page_issue_codes.append("crawl_sitemap_redirect")
                issues.append(
                    SeoIssue(
                        code="crawl_sitemap_redirect",
                        severity="medium",
                        category="technical",
                        title="Sitemap-URL omdirigerar till annan URL",
                        detail=f"{target_url} omdirigerade till {final_url}.",
                        url=target_url,
                        suggestion="Uppdatera sitemapen så endast slutliga kanoniska 200-URL:er inkluderas.",
                    ).to_dict()
                )

        page_noindex = False
        canonical_url = ""
        canonical_matches = True
        page_title = ""
        word_count = 0

        if 200 <= status < 300:
            ok_count += 1
            html_info = analyze_html_seo(
                str(res.get("text") or ""),
                url=final_url or target_url,
                response_headers=res.get("headers") if isinstance(res.get("headers"), dict) else None,
            )
            page_noindex = bool(html_info.get("noindex"))
            canonical_url = str(html_info.get("canonical") or "")
            canonical_matches = bool(html_info.get("canonical_matches_url", True)) if canonical_url else True
            page_title = str(html_info.get("title") or "")
            word_count = int(html_info.get("word_count") or 0)

            if page_noindex and canonical_url and not canonical_matches:
                noindex_count += 1
                canonical_conflict_count += 1
                page_issue_codes.append("crawl_noindex_canonical_conflict")
                issues.append(
                    SeoIssue(
                        code="crawl_noindex_canonical_conflict",
                        severity="critical",
                        category="technical",
                        title="Konflikt: sitemap-URL har både noindex och avvikande canonical",
                        detail=f"{target_url} har noindex och canonical -> {canonical_url}.",
                        url=target_url,
                        suggestion="Ta bort sidan ur sitemapen eller korrigera både robots (index, follow) och canonical.",
                        fix_type="seo_meta",
                    ).to_dict()
                )
            else:
                if page_noindex:
                    noindex_count += 1
                    page_issue_codes.append("crawl_sitemap_noindex")
                    issues.append(
                        SeoIssue(
                            code="crawl_sitemap_noindex",
                            severity="critical",
                            category="technical",
                            title="Sitemap-URL är markerad noindex",
                            detail=f"{target_url} finns i sitemapen men har noindex-direktiv.",
                            url=target_url,
                            suggestion="Ta bort noindex om sidan ska indexeras, eller exkludera sidan ur XML-sitemapen.",
                            fix_type="seo_meta",
                        ).to_dict()
                    )
                if canonical_url and not canonical_matches:
                    canonical_conflict_count += 1
                    page_issue_codes.append("crawl_canonical_conflict")
                    issues.append(
                        SeoIssue(
                            code="crawl_canonical_conflict",
                            severity="high",
                            category="technical",
                            title="Canonical-konflikt på sitemap-URL",
                            detail=f"Sitemap-URL {target_url} har canonical som pekar på {canonical_url}.",
                            url=target_url,
                            suggestion="Säkerställ att XML-sitemapen endast innehåller självrefererande kanoniska URL:er.",
                            fix_type="seo_meta",
                        ).to_dict()
                    )

        crawled.append(
            {
                "url": target_url,
                "final_url": final_url,
                "status": status,
                "ok": 200 <= status < 300,
                "redirect_hops": redirect_hops,
                "noindex": page_noindex,
                "canonical": canonical_url,
                "canonical_matches": canonical_matches,
                "title": page_title,
                "word_count": word_count,
                "issue_codes": page_issue_codes,
            }
        )

    summary = {
        "crawled_count": len(crawled),
        "ok_count": ok_count,
        "error_4xx_count": err_4xx,
        "error_5xx_count": err_5xx,
        "redirect_count": redirect_count,
        "noindex_count": noindex_count,
        "canonical_conflict_count": canonical_conflict_count,
        "total_issues": len(issues),
    }
    return {
        "crawled": crawled,
        "issues": issues,
        "summary": summary,
    }


# --------------------------------------------------------------------------- #
# Nordic SE/NO/DK Parity Matrix (Proposal 3)
# --------------------------------------------------------------------------- #

_EXPECTED_MARKET_CURRENCY: dict[str, str] = {
    "se": "SEK",
    "no": "NOK",
    "dk": "DKK",
}

_SWEDISH_LEAK_RE = re.compile(
    r"\b(k[ö]p|erbjudande|kundtj[ä]nst|fraktfritt)\b",
    re.I,
)


def _extract_jsonld_currencies(html_text: str) -> list[str]:
    found: list[str] = []
    parser = _SeoHtmlParser(page_url="https://example.test/")
    try:
        parser.feed(str(html_text or ""))
        parser.close()
    except Exception:
        return found
    for raw in parser.json_ld_raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _flatten_jsonld_items(decoded):
            offers = node.get("offers")
            offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
            for offer in offer_list:
                if isinstance(offer, dict):
                    cur = str(offer.get("priceCurrency") or "").strip().upper()
                    if cur and cur not in found:
                        found.append(cur)
    return found


def _page_canonical_key(page: dict[str, Any]) -> str:
    if page.get("key"):
        return str(page["key"]).strip().lower()
    path = str(page.get("path") or "").strip()
    if not path and page.get("url"):
        path = urlparse(str(page["url"])).path or "/"
    if not path and page.get("slug"):
        path = "/" + str(page["slug"]).strip("/") + "/"
    return ("/" + path.lstrip("/")).rstrip("/") or "/"


def audit_nordic_parity_matrix(
    market_pages: dict[str, Any],
    translations: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Audit SE/NO/DK parity: untranslated pages, Swedish word leaks on NO/DK, priceCurrency, and hreflang."""
    normalized_by_market: dict[str, dict[str, dict[str, Any]]] = {}
    markets_order: list[str] = []

    for raw_mkt, pages_val in (market_pages or {}).items():
        mkt = normalize_market(raw_mkt)
        if mkt not in markets_order:
            markets_order.append(mkt)
        bucket = normalized_by_market.setdefault(mkt, {})
        if isinstance(pages_val, list):
            page_iter = [p for p in pages_val if isinstance(p, dict)]
        elif isinstance(pages_val, dict):
            if any(k in pages_val for k in ("url", "html", "title", "slug", "path")):
                page_iter = [pages_val]
            else:
                page_iter = [
                    {**p, "key": p.get("key") or str(k)}
                    for k, p in pages_val.items()
                    if isinstance(p, dict)
                ]
        else:
            page_iter = []

        for page in page_iter:
            pkey = _page_canonical_key(page)
            bucket[pkey] = dict(page)

    # Also build canonical keys from optional translations map {key: {se: url, no: url, dk: url}}
    all_keys: list[str] = []
    for k in (translations or {}):
        ck = str(k).strip().lower()
        if ck and ck not in all_keys:
            all_keys.append(ck)
    for mkt in markets_order:
        for pk in normalized_by_market.get(mkt, {}):
            if pk not in all_keys:
                all_keys.append(pk)

    target_markets = [m for m in ("se", "no", "dk") if m in markets_order] or ["se", "no", "dk"]
    if len(target_markets) < 3 and (translations or len(markets_order) >= 2):
        for m in ("se", "no", "dk"):
            if m not in target_markets:
                target_markets.append(m)

    issues: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    hreflang_pages: dict[str, dict[str, Any]] = {}

    missing_translations = 0
    swedish_leaks = 0
    wrong_currency = 0

    for pkey in all_keys:
        row_markets: dict[str, dict[str, Any]] = {}
        se_page = normalized_by_market.get("se", {}).get(pkey)
        se_title_norm = " ".join(str((se_page or {}).get("title") or "").strip().lower().split())

        for mkt in target_markets:
            page = normalized_by_market.get(mkt, {}).get(pkey)
            # Also allow matching via translations map URL
            if page is None and translations and isinstance(translations.get(pkey), dict):
                mapped_u = str(translations[pkey].get(mkt) or "").strip()
                if mapped_u:
                    mapped_path = ("/" + (urlparse(mapped_u).path or "/").lstrip("/")).rstrip("/") or "/"
                    page = normalized_by_market.get(mkt, {}).get(mapped_path)

            if page is None:
                missing_translations += 1
                row_markets[mkt] = {"present": False, "url": "", "currency": "", "leaks": []}
                issues.append(
                    SeoIssue(
                        code="parity_missing_translation",
                        severity="high",
                        category="hreflang",
                        title=f"Saknar översatt sida för {mkt.upper()}: {pkey}",
                        detail=f"Sidan '{pkey}' finns på annan nordisk marknad men saknas på {mkt.upper()}.",
                        url=pkey,
                        suggestion=f"Skapa och lokalisera motsvarande {mkt.upper()}-sida och koppla med ömsesidig hreflang.",
                    ).to_dict()
                )
                continue

            page_url = str(page.get("url") or pkey).strip()
            html_text = str(page.get("html") or "")
            parsed_html: dict[str, Any] | None = None
            if html_text:
                parsed_html = analyze_html_seo(html_text, url=page_url)
                if parsed_html.get("hreflang"):
                    hreflang_pages[page_url] = parsed_html
            elif page.get("hreflang"):
                hreflang_pages[page_url] = {
                    "noindex": bool(page.get("noindex", False)),
                    "hreflang": list(page.get("hreflang") or []),
                }

            title = str(page.get("title") or (parsed_html or {}).get("title") or "").strip()
            meta_title = str(page.get("meta_title") or "").strip()
            meta_desc = str(page.get("meta_description") or (parsed_html or {}).get("meta_description") or "").strip()
            body_text = str(page.get("text") or "")
            if not body_text and html_text:
                body_text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html_text, flags=re.I | re.S)
                body_text = re.sub(r"<style\b[^>]*>.*?</style>", " ", body_text, flags=re.I | re.S)
                body_text = html_lib.unescape(re.sub(r"<[^>]+>", " ", body_text))

            # Untranslated check (exact copy of SE title on NO/DK when title is substantive)
            untranslated = False
            if mkt in {"no", "dk"} and se_title_norm and len(se_title_norm) >= 12:
                cur_title_norm = " ".join(title.lower().split())
                if cur_title_norm == se_title_norm and any(ch in cur_title_norm for ch in ("ä", "ö")):
                    untranslated = True
                    missing_translations += 1
                    issues.append(
                        SeoIssue(
                            code="parity_untranslated_page",
                            severity="high",
                            category="on_page",
                            title=f"Oöversatt svensk titel på {mkt.upper()}-sida ({pkey})",
                            detail=f"Sidan {page_url} har samma svenska titel som SE: '{title}'.",
                            url=page_url,
                            suggestion=f"Lokalisera titel, meta description och brödtext till {mkt.upper()}.",
                            fix_type="seo_meta",
                        ).to_dict()
                    )

            # Swedish word leaks on NO/DK
            leaks: list[str] = []
            if mkt in {"no", "dk"}:
                combined_copy = f"{title} {meta_title} {meta_desc} {body_text}"
                leaks = sorted({m.group(1).lower() for m in _SWEDISH_LEAK_RE.finditer(combined_copy)})
                if leaks:
                    swedish_leaks += 1
                    issues.append(
                        SeoIssue(
                            code="parity_swedish_leak",
                            severity="high",
                            category="on_page",
                            title=f"Svenska ord läcker på {mkt.upper()}-sida ({', '.join(leaks)})",
                            detail=f"{page_url} innehåller svenska ord: {', '.join(leaks)}.",
                            url=page_url,
                            suggestion=f"Byt ut svenska ord mot korrekt lokalisering för {mkt.upper()}.",
                            fix_type="seo_meta",
                        ).to_dict()
                    )

            # Currency check (JSON-LD priceCurrency or explicit price_currency/currency)
            expected_cur = _EXPECTED_MARKET_CURRENCY[mkt]
            currencies: list[str] = []
            for ck in ("price_currency", "priceCurrency", "currency"):
                cv = str(page.get(ck) or "").strip().upper()
                if cv and cv not in currencies:
                    currencies.append(cv)
            if html_text:
                for cv in _extract_jsonld_currencies(html_text):
                    if cv not in currencies:
                        currencies.append(cv)

            wrong_curs = [c for c in currencies if c != expected_cur]
            if wrong_curs:
                wrong_currency += 1
                issues.append(
                    SeoIssue(
                        code="parity_wrong_currency",
                        severity="critical",
                        category="schema",
                        title=f"Fel JSON-LD priceCurrency ({', '.join(wrong_curs)}) på {mkt.upper()}-sida",
                        detail=f"{page_url} rapporterar {', '.join(wrong_curs)} men {mkt.upper()} kräver {expected_cur}.",
                        url=page_url,
                        suggestion=f"Korrigera Product JSON-LD priceCurrency till '{expected_cur}'.",
                        fix_type="schema_jsonld",
                    ).to_dict()
                )

            row_markets[mkt] = {
                "present": True,
                "url": page_url,
                "title": title,
                "currency": currencies[0] if currencies else "",
                "currency_ok": not bool(wrong_curs),
                "leaks": leaks,
                "untranslated": untranslated,
            }

        rows.append({"key": pkey, "markets": row_markets})

    hreflang_report = audit_nordic_hreflang(hreflang_pages) if hreflang_pages else {
        "checked_urls": [],
        "markets_covered": [],
        "reciprocal_pairs_count": 0,
        "missing_self_ref": [],
        "missing_x_default": [],
        "missing_return_links": [],
        "noindex_conflicts": [],
        "ambiguous_codes": [],
        "issues": [],
    }
    hreflang_issues = list(hreflang_report.get("issues") or [])
    issues.extend(hreflang_issues)

    return {
        "markets": target_markets,
        "rows": rows,
        "hreflang": hreflang_report,
        "issues": issues,
        "summary": {
            "total_pages": len(rows),
            "markets_checked": len(target_markets),
            "missing_translations": missing_translations,
            "swedish_leaks": swedish_leaks,
            "wrong_currency": wrong_currency,
            "hreflang_issues": len(hreflang_issues),
            "total_issues": len(issues),
        },
    }


# --------------------------------------------------------------------------- #
# Live Backlink Placement Verifier (Proposal 4)
# --------------------------------------------------------------------------- #


def _norm_link_target(raw_url: str) -> tuple[str, str]:
    parsed = urlparse(str(raw_url or "").strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = ("/" + (parsed.path or "/").lstrip("/")).rstrip("/") or "/"
    return host, path


def verify_backlink_placement(
    published_url: str,
    *,
    expected_target_url: str,
    expected_anchor: str = "",
    fetcher: Callable[[str], dict[str, Any]] | None = None,
    use_mock: bool = True,
) -> dict[str, Any]:
    """Verify a published backlink placement: HTTP 200, indexable, target URL present, anchor & rel."""
    pub_url = str(published_url or "").strip()
    exp_target = str(expected_target_url or "").strip()
    exp_anchor = " ".join(str(expected_anchor or "").strip().split())

    if fetcher is not None:
        res = fetcher(pub_url)
    elif use_mock:
        low_pub = pub_url.lower()
        if "404" in low_pub or "missing-page" in low_pub:
            res = {"ok": False, "status": 404, "url": pub_url, "final_url": pub_url, "headers": {}, "text": ""}
        elif "noindex" in low_pub:
            res = {
                "ok": True,
                "status": 200,
                "url": pub_url,
                "final_url": pub_url,
                "headers": {"content-type": "text/html"},
                "text": (
                    f'<!doctype html><html><head><title>Artikel</title>'
                    f'<meta name="robots" content="noindex, follow"></head>'
                    f'<body><p>Se <a href="{exp_target}">{exp_anchor or "Azom"}</a>.</p></body></html>'
                ),
            }
        elif "nofollow" in low_pub:
            res = {
                "ok": True,
                "status": 200,
                "url": pub_url,
                "final_url": pub_url,
                "headers": {"content-type": "text/html"},
                "text": (
                    f'<!doctype html><html><head><title>Artikel</title></head>'
                    f'<body><p>Se <a href="{exp_target}" rel="nofollow">{exp_anchor or "Azom"}</a>.</p></body></html>'
                ),
            }
        elif "nolink" in low_pub:
            res = {
                "ok": True,
                "status": 200,
                "url": pub_url,
                "final_url": pub_url,
                "headers": {"content-type": "text/html"},
                "text": "<!doctype html><html><head><title>Artikel utan länk</title></head><body><p>Ingen länk här.</p></body></html>",
            }
        else:
            res = {
                "ok": True,
                "status": 200,
                "url": pub_url,
                "final_url": pub_url,
                "headers": {"content-type": "text/html"},
                "text": (
                    f'<!doctype html><html><head><title>Redaktionell artikel om CarPlay</title></head>'
                    f'<body><h1>Uppgradera bilen</h1>'
                    f'<p>Läs mer hos <a href="{exp_target}">{exp_anchor or "Azom"}</a> för enkel montering.</p>'
                    f'</body></html>'
                ),
            }
    else:
        res = fetch_public_seo_resource(pub_url, use_mock=False)

    status = int(res.get("status") or 0)
    http_ok = 200 <= status < 300
    issues: list[str] = []

    if not http_ok:
        issues.append(f"http_status_{status or 'error'}")
        return {
            "ok": False,
            "verified": False,
            "published_url": pub_url,
            "expected_target_url": exp_target,
            "expected_anchor": exp_anchor,
            "http_status": status,
            "http_ok": False,
            "indexable": False,
            "link_found": False,
            "found_href": "",
            "found_anchor": "",
            "anchor_match": False,
            "rel_type": "unknown",
            "rel_attributes": [],
            "issues": issues,
        }

    html_text = str(res.get("text") or "")
    final_url = str(res.get("final_url") or pub_url)
    hdrs = res.get("headers") if isinstance(res.get("headers"), dict) else {}
    html_info = analyze_html_seo(html_text, url=final_url, response_headers=hdrs)
    indexable = not bool(html_info.get("noindex"))
    if not indexable:
        issues.append("page_noindex")

    # Parse all links (internal + external) directly with _SeoHtmlParser so external links are never truncated
    parser = _SeoHtmlParser(page_url=final_url)
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass

    exp_host, exp_path = _norm_link_target(exp_target)
    matched: dict[str, Any] | None = None
    for link in parser.external_links + parser.internal_links:
        lh, lp = _norm_link_target(str(link.get("href") or ""))
        if lh == exp_host and lp == exp_path:
            matched = link
            break

    link_found = matched is not None
    found_href = str(matched.get("href") or "") if matched else ""
    found_anchor = " ".join(str(matched.get("anchor") or "").strip().split()) if matched else ""
    raw_rel = str(matched.get("rel") or "") if matched else ""
    rel_attrs = sorted({r.strip().lower() for r in raw_rel.split() if r.strip()})

    if not link_found:
        issues.append("target_link_missing")
        rel_type = "none"
        anchor_match = False
    else:
        if "sponsored" in rel_attrs:
            rel_type = "sponsored"
        elif "ugc" in rel_attrs:
            rel_type = "ugc"
        elif "nofollow" in rel_attrs:
            rel_type = "nofollow"
        else:
            rel_type = "dofollow"

        if not exp_anchor:
            anchor_match = True
        else:
            anchor_match = found_anchor.lower() == exp_anchor.lower()
            if not anchor_match:
                issues.append("anchor_mismatch")

    ok = bool(http_ok and indexable and link_found)
    verified = bool(ok and anchor_match)
    return {
        "ok": ok,
        "verified": verified,
        "published_url": pub_url,
        "expected_target_url": exp_target,
        "expected_anchor": exp_anchor,
        "http_status": status,
        "http_ok": http_ok,
        "indexable": indexable,
        "link_found": link_found,
        "found_href": found_href,
        "found_anchor": found_anchor,
        "anchor_match": anchor_match,
        "rel_type": rel_type,
        "rel_attributes": rel_attrs,
        "issues": issues,
    }


__all__ = [
    "analyze_html_seo",
    "analyze_robots_txt",
    "analyze_sitemap_xml",
    "audit_nordic_hreflang",
    "audit_nordic_parity_matrix",
    "crawl_site_pages",
    "expand_sitemap_index",
    "fetch_public_seo_resource",
    "mock_technical_fixtures",
    "verify_backlink_placement",
]
