"""WordPress page/guide listing, lookup, and synthetic sample-data builder tools."""

from __future__ import annotations

import re
import sys
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from ecom_ops.wp_ops.file_changes import rest_reads_allowed as _default_rest_reads_allowed
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.review_workspace import ReviewError
from ecom_ops.wp_ops.tools.catalog import infer_site_currency, parse_product_id, slug_from_text
from ecom_ops.wp_ops.tools.html_parse import strip_html_plain
from ecom_ops.wp_ops.tools.registry import count_outcome, register_tool
from ecom_ops.wp_ops.tools.types import (
    PRODUCT_LINK_PATH_RE,
    PRODUCT_SLUG_RE,
    SAMPLE_SCENARIOS,
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import error_payload, short_text


def _rest_reads_allowed() -> bool:
    facade = sys.modules.get("ecom_ops.wp_ops.agent_tools")
    fn = getattr(facade, "rest_reads_allowed", None) if facade is not None else None
    if callable(fn):
        return bool(fn())
    return _default_rest_reads_allowed()


def normalize_wp_page(
    tools: Any,
    site_obj: Site,
    row: dict[str, Any],
    *,
    post_type: str = "page",
    include_content: bool = False,
) -> dict[str, Any]:
    pid = parse_product_id(row.get("id"))
    raw_title = row.get("title")
    if isinstance(raw_title, dict):
        raw_title = raw_title.get("rendered") or ""
    title = tools._scrub_bounded(site_obj, strip_html_plain(raw_title), max_len=200)
    raw_slug = str(row.get("slug") or "").strip().lower()
    slug = raw_slug[:120] if raw_slug else slug_from_text(title)
    link = str(row.get("link") or "").strip()
    path = "/"
    if link:
        parsed = urllib.parse.urlparse(link)
        if parsed.path:
            path = parsed.path if parsed.path.startswith("/") else "/" + parsed.path
    elif slug:
        path = f"/{slug}/"
    if not link and str(site_obj.canonical_url or "").strip():
        link = str(site_obj.canonical_url).rstrip("/") + path
    raw_excerpt = row.get("excerpt")
    if isinstance(raw_excerpt, dict):
        raw_excerpt = raw_excerpt.get("rendered") or ""
    excerpt = tools._scrub_bounded(site_obj, strip_html_plain(raw_excerpt), max_len=300)
    status = tools._scrub_bounded(site_obj, row.get("status") or "publish", max_len=32)
    out: dict[str, Any] = {
        "id": pid,
        "type": str(row.get("type") or post_type),
        "slug": slug,
        "title": title,
        "link": link[:300],
        "path": path[:200],
        "status": status or "publish",
        "excerpt": excerpt,
    }
    if include_content:
        raw_content = row.get("content")
        if isinstance(raw_content, dict):
            raw_content = raw_content.get("rendered") or ""
        out["content_summary"] = tools._scrub_bounded(site_obj, strip_html_plain(raw_content), max_len=3600)
    return out


def _get_page_outcome(data: dict[str, Any], _reads: int | None) -> str:
    page = data.get("page")
    if isinstance(page, dict) and page.get("slug"):
        return f"sida: {short_text(page.get('slug'), 40)}"
    return "sida hämtad"


def _sample_data_outcome(data: dict[str, Any], _reads: int | None) -> str:
    prods = data.get("products")
    guides = data.get("guides")
    np = len(prods) if isinstance(prods, list) else 0
    ng = len(guides) if isinstance(guides, list) else 0
    return f"exempeldata ({np} produkter, {ng} guider)"


@register_tool(
    name="wp_list_pages",
    group="file",
    label="Lista sidor & guider",
    short_label="Sidlista",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "search": {"type": "string"},
            "post_type": {"type": "string", "enum": ["page", "post", "both"]},
            "per_page": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "additionalProperties": False,
    },
    outcome=count_outcome("pages", "sidor/guider", "sidor listade"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="preview_catalog",
)
def handle_list_pages(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    site_obj, denied_out = tools._site_for_preview(site_id, ctx.actor)
    if denied_out is not None:
        return denied_out
    assert site_obj is not None

    raw_search = arguments.get("search", "")
    if raw_search is not None and (not isinstance(raw_search, str) or len(raw_search) > 80):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    search = (raw_search or "").strip()

    raw_type = arguments.get("post_type", "page")
    if raw_type not in {"page", "post", "both"}:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    post_type = str(raw_type)

    raw_limit = arguments.get("per_page", 10)
    if type(raw_limit) is not int or raw_limit < 1 or raw_limit > 20:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    per_page = raw_limit

    if not _rest_reads_allowed():
        wp = tools._mock_wp_client()
        items: list[dict[str, Any]] = []
        if post_type in {"page", "both"}:
            for p in wp.list_pages(per_page=per_page, search=search or None):
                row = (
                    dict(p.raw)
                    if isinstance(p.raw, dict)
                    else {"id": p.id, "title": p.title, "status": p.status, "link": p.link, "type": "page"}
                )
                items.append(normalize_wp_page(tools, site_obj, row, post_type="page"))
        if post_type in {"post", "both"} and len(items) < per_page:
            for p in wp.list_posts(per_page=per_page - len(items), search=search or None):
                row = (
                    dict(p.raw)
                    if isinstance(p.raw, dict)
                    else {"id": p.id, "title": p.title, "status": p.status, "link": p.link, "type": "post"}
                )
                items.append(normalize_wp_page(tools, site_obj, row, post_type="post"))
        return ToolOutput(
            {
                "ok": True,
                "status": "listed",
                "site_id": site_id,
                "source": "mock",
                "post_type": post_type,
                "count": len(items),
                "pages": items[:per_page],
                "live_calls": False,
            },
            site_id=site_id,
        )

    items = []
    endpoints: list[tuple[str, str]] = []
    if post_type in {"page", "both"}:
        endpoints.append(("page", "/wp-json/wp/v2/pages"))
    if post_type in {"post", "both"}:
        endpoints.append(("post", "/wp-json/wp/v2/posts"))
    source = "wp_rest"
    for ptype, ep in endpoints:
        if len(items) >= per_page:
            break
        params: dict[str, Any] = {"per_page": per_page - len(items), "status": "publish"}
        if search:
            params["search"] = search
        rows = tools._safe_site_get_json(site_obj, ep, params=params, use_wp_auth=False)
        if not isinstance(rows, list):
            rows = tools._safe_site_get_json(site_obj, ep, params=params, use_wp_auth=True)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    items.append(normalize_wp_page(tools, site_obj, row, post_type=ptype))

    if not items:
        site_host = (urllib.parse.urlparse(str(site_obj.canonical_url or "")).hostname or "").lower()
        allowed_hosts = tools._allowed_site_hosts(site_host)
        needle = search.lower()
        seen_paths: set[str] = set()
        for cand in ("/page-sitemap.xml", "/"):
            try:
                fetched = tools._fetch_live_html(site_obj, cand, use_cache=True)
            except ReviewError:
                continue
            raw_text = str(fetched.get("html") or "")
            links: list[tuple[str, str]] = [
                (m.group(1).strip(), "")
                for m in re.finditer(r"(?is)<loc>\s*(https?://[^<\s]+)\s*</loc>", raw_text)
            ]
            if not links:
                for am in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw_text):
                    links.append((am.group(1).strip(), strip_html_plain(am.group(2))))
            for href, label in links:
                parsed = urllib.parse.urlparse(href)
                if parsed.hostname and parsed.hostname.lower() not in allowed_hosts:
                    continue
                raw_p = parsed.path or ""
                if not raw_p or raw_p == "/" or PRODUCT_LINK_PATH_RE.fullmatch(raw_p):
                    continue
                try:
                    clean_p = tools._validate_preview_path(raw_p)
                except ReviewError:
                    continue
                if clean_p in seen_paths:
                    continue
                slug = clean_p.strip("/").split("/")[-1].lower()
                title = tools._scrub_bounded(site_obj, label, max_len=160) or slug.replace("-", " ").title()
                if needle and needle not in title.lower() and needle not in clean_p.lower():
                    continue
                seen_paths.add(clean_p)
                items.append(
                    {
                        "id": None,
                        "type": "page",
                        "slug": slug[:120],
                        "title": title,
                        "link": str(site_obj.canonical_url or "").rstrip("/") + clean_p,
                        "path": clean_p,
                        "status": "publish",
                        "excerpt": "",
                    }
                )
                if len(items) >= per_page:
                    break
            if items:
                source = "live_html"
                break

    return ToolOutput(
        {
            "ok": True,
            "status": "listed",
            "site_id": site_id,
            "source": source if items else "unavailable",
            "post_type": post_type,
            "count": len(items),
            "pages": items[:per_page],
            "live_calls": True,
        },
        site_id=site_id,
    )


@register_tool(
    name="wp_get_page",
    group="file",
    label="Hämta sid-/guideinnehåll",
    short_label="Sidinnehåll",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "page_id": {"type": ["integer", "string"]},
            "slug": {"type": "string"},
            "post_type": {"type": "string", "enum": ["page", "post"]},
        },
        "additionalProperties": False,
    },
    outcome=_get_page_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="preview_catalog",
)
def handle_get_page(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    site_obj, denied_out = tools._site_for_preview(site_id, ctx.actor)
    if denied_out is not None:
        return denied_out
    assert site_obj is not None

    has_id = "page_id" in arguments
    has_slug = "slug" in arguments
    if not (has_id or has_slug):
        err = error_payload("InvalidToolArguments")
        err["hint"] = "Ange page_id eller slug (t.ex. slug='setup-guide') för wp_get_page."
        return ToolOutput(err, site_id)

    page_id: int | None = None
    if has_id:
        page_id = parse_product_id(arguments.get("page_id"))
        if page_id is None:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    slug = ""
    if has_slug:
        raw_slug = arguments.get("slug")
        if not isinstance(raw_slug, str) or not PRODUCT_SLUG_RE.fullmatch(raw_slug.strip().strip("/").lower()):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        slug = raw_slug.strip().strip("/").lower()

    post_type = str(arguments.get("post_type") or "page")
    if post_type not in {"page", "post"}:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    if not _rest_reads_allowed():
        wp = tools._mock_wp_client()
        rows = wp.list_pages(per_page=20) if post_type == "page" else wp.list_posts(per_page=20)
        matched: dict[str, Any] | None = None
        for p in rows:
            raw_row = (
                dict(p.raw)
                if isinstance(p.raw, dict)
                else {"id": p.id, "title": p.title, "status": p.status, "link": p.link, "type": post_type}
            )
            norm = normalize_wp_page(tools, site_obj, raw_row, post_type=post_type, include_content=True)
            if (page_id is not None and norm["id"] == page_id) or (
                slug and (norm["slug"] == slug or norm["path"].strip("/").endswith(slug))
            ):
                matched = norm
                break
        if matched is None:
            err = error_payload("PageNotFound")
            err["recovery_tool"] = "wp_list_pages"
            err["hint"] = "Sidan hittades inte. Kör wp_list_pages för att lista tillgängliga sidor och guider."
            return ToolOutput(err, site_id)
        return ToolOutput(
            {"ok": True, "status": "read", "site_id": site_id, "source": "mock", "page": matched, "live_calls": False},
            site_id=site_id,
        )

    ep_base = "/wp-json/wp/v2/pages" if post_type == "page" else "/wp-json/wp/v2/posts"
    detail: dict[str, Any] | None = None
    source = "wp_rest"
    if page_id is not None:
        row = tools._safe_site_get_json(site_obj, f"{ep_base}/{page_id}", use_wp_auth=False)
        if not isinstance(row, dict) or not row.get("id"):
            row = tools._safe_site_get_json(site_obj, f"{ep_base}/{page_id}", use_wp_auth=True)
        if isinstance(row, dict) and row.get("id"):
            detail = normalize_wp_page(tools, site_obj, row, post_type=post_type, include_content=True)
    elif slug:
        rows = tools._safe_site_get_json(site_obj, ep_base, params={"slug": slug, "per_page": 1}, use_wp_auth=False)
        if not isinstance(rows, list) or not rows:
            rows = tools._safe_site_get_json(site_obj, ep_base, params={"slug": slug, "per_page": 1}, use_wp_auth=True)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            detail = normalize_wp_page(tools, site_obj, rows[0], post_type=post_type, include_content=True)

    if detail is None and slug:
        try:
            cand_path = tools._validate_preview_path(f"/{slug}/")
            fetched = tools._fetch_live_html(site_obj, cand_path, use_cache=True)
            raw_html = str(fetched.get("html") or "")
            h1_m = re.search(r"(?is)<h1\b[^>]*>(.*?)</h1>", raw_html)
            title_m = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", raw_html)
            title = (
                strip_html_plain(h1_m.group(1))
                if h1_m
                else (strip_html_plain(title_m.group(1)) if title_m else slug)
            )
            clean_body = re.sub(r"(?is)<(?:script|style|noscript)\b[^>]*>.*?</(?:script|style|noscript)>", " ", raw_html)
            detail = {
                "id": None,
                "type": post_type,
                "slug": slug,
                "title": tools._scrub_bounded(site_obj, title, max_len=200),
                "link": str(fetched.get("final_url") or (str(site_obj.canonical_url or "").rstrip("/") + cand_path)),
                "path": cand_path,
                "status": "publish",
                "excerpt": tools._scrub_bounded(site_obj, strip_html_plain(clean_body), max_len=300),
                "content_summary": tools._scrub_bounded(site_obj, strip_html_plain(clean_body), max_len=3600),
            }
            source = "live_html"
        except ReviewError:
            detail = None

    if detail is None:
        err = error_payload("PageNotFound")
        err["recovery_tool"] = "wp_list_pages"
        err["hint"] = "Sidan hittades inte. Kör wp_list_pages för att se befintliga sidor och guider."
        return ToolOutput(err, site_id)
    return ToolOutput(
        {"ok": True, "status": "read", "site_id": site_id, "source": source, "page": detail, "live_calls": True},
        site_id=site_id,
    )


@register_tool(
    name="wp_build_sample_data",
    group="file",
    label="Bygg syntetisk exempeldata",
    short_label="Exempeldata",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "scenario": {
                "type": "string",
                "enum": ["thankyou", "landing", "campaign", "comparison", "upsell", "custom"],
            },
            "product_ids": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": ["integer", "string"]},
            },
            "product_count": {"type": "integer", "minimum": 1, "maximum": 4},
            "guide_search": {"type": "string"},
            "currency": {"type": "string"},
        },
        "additionalProperties": False,
    },
    outcome=_sample_data_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    deep_phase=True,
    untrusted_output=True,
    capability_group="preview_catalog",
)
def handle_build_sample_data(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    site_obj, denied_out = tools._site_for_preview(site_id, ctx.actor)
    if denied_out is not None:
        return denied_out
    assert site_obj is not None

    scenario = str(arguments.get("scenario") or "thankyou").strip().lower()
    if scenario not in SAMPLE_SCENARIOS:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    raw_count = arguments.get("product_count", 2)
    if type(raw_count) is not int or raw_count < 1 or raw_count > 4:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    raw_pids = arguments.get("product_ids")
    product_ids: list[int] = []
    if raw_pids is not None:
        if not isinstance(raw_pids, list) or len(raw_pids) > 4:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        for item in raw_pids:
            pid = parse_product_id(item)
            if pid is None:
                return ToolOutput(error_payload("InvalidToolArguments"), site_id)
            if pid not in product_ids:
                product_ids.append(pid)
    raw_guide_search = arguments.get("guide_search", "")
    if raw_guide_search is not None and (not isinstance(raw_guide_search, str) or len(raw_guide_search) > 60):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    guide_search = (raw_guide_search or "").strip()
    raw_currency = arguments.get("currency", "")
    if raw_currency is not None and (not isinstance(raw_currency, str) or len(raw_currency) > 8):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    products: list[dict[str, Any]] = []
    live_calls = False
    if product_ids:
        for pid in product_ids:
            res = tools._get_product(site_id, {"product_id": pid}, actor=ctx.actor)
            if res.data.get("ok") is True and isinstance(res.data.get("product"), dict):
                products.append(res.data["product"])
                if res.data.get("live_calls"):
                    live_calls = True
    if len(products) < raw_count:
        listed = tools._list_products(site_id, {"limit": max(6, raw_count * 2)}, actor=ctx.actor)
        if listed.data.get("live_calls"):
            live_calls = True
        seen_keys = {(p.get("id"), p.get("slug")) for p in products}
        for row in listed.data.get("products") or []:
            if len(products) >= raw_count:
                break
            if not isinstance(row, dict):
                continue
            key = (row.get("id"), row.get("slug"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if not row.get("image_url") or not row.get("price"):
                lookup_arg: dict[str, Any] = {}
                if row.get("id"):
                    lookup_arg["product_id"] = row["id"]
                elif row.get("path"):
                    lookup_arg["path"] = row["path"]
                if lookup_arg:
                    det = tools._get_product(site_id, lookup_arg, actor=ctx.actor)
                    if det.data.get("ok") is True and isinstance(det.data.get("product"), dict):
                        row = det.data["product"]
            products.append(row)

    guides: list[dict[str, Any]] = []
    search_terms = [guide_search] if guide_search else ["guide", "setup", "hjelp", "hjälp", "faq"]
    seen_guide_paths: set[str] = set()
    for term in search_terms:
        if len(guides) >= 4:
            break
        pages_out = tools._list_pages(site_id, {"search": term, "post_type": "page", "per_page": 6}, actor=ctx.actor)
        if pages_out.data.get("live_calls"):
            live_calls = True
        for page_row in pages_out.data.get("pages") or []:
            if not isinstance(page_row, dict):
                continue
            ppath = str(page_row.get("path") or "")
            if not ppath or ppath in seen_guide_paths:
                continue
            seen_guide_paths.add(ppath)
            guides.append(
                {
                    "title": page_row.get("title") or "",
                    "slug": page_row.get("slug") or "",
                    "path": ppath,
                    "link": page_row.get("link") or "",
                    "excerpt": page_row.get("excerpt") or "",
                }
            )
            if len(guides) >= 4:
                break
        if guide_search:
            break

    detected_currency = (
        str(raw_currency or "").strip().upper()
        or next((str(p.get("currency") or "").strip().upper() for p in products if p.get("currency")), "")
        or infer_site_currency(site_obj)
    )
    line_items: list[dict[str, Any]] = []
    subtotal = 0.0
    for idx, prod in enumerate(products):
        qty = 1
        unit_num = tools._parse_numeric_price(prod.get("price"))
        if unit_num is None:
            unit_num = 1490.0 if idx == 0 else 490.0
        line_total = round(unit_num * qty, 2)
        subtotal += line_total
        line_items.append(
            {
                "product_id": prod.get("id"),
                "name": prod.get("name") or f"Exempelprodukt {idx + 1}",
                "slug": prod.get("slug") or "",
                "sku": prod.get("sku") or "",
                "path": prod.get("path") or "",
                "permalink": prod.get("permalink") or "",
                "image_url": prod.get("image_url") or "",
                "quantity": qty,
                "unit_price": f"{int(unit_num) if unit_num.is_integer() else unit_num:.2f}".rstrip("0").rstrip(".")
                + f" {detected_currency}",
                "line_total": f"{int(line_total) if line_total.is_integer() else line_total:.2f}".rstrip("0").rstrip(".")
                + f" {detected_currency}",
            }
        )
    shipping_num = 0.0 if subtotal >= 1000.0 else 99.0
    total_num = round(subtotal + shipping_num, 2)

    def _fmt_amt(val: float) -> str:
        if val == 0.0:
            return f"0 {detected_currency} (Fri frakt)"
        s = f"{int(val)}" if float(val).is_integer() else f"{val:.2f}"
        return f"{s} {detected_currency}"

    is_no = detected_currency == "NOK"
    sample_customer = {
        "name": "Test Kund",
        "email": "test@example.invalid",
        "shipping_address": "Exempelgatan 1, 0150 Oslo" if is_no else "Exempelgatan 1, 111 22 Stockholm",
        "contains_real_pii": False,
    }
    sample_order = {
        "order_number": "AZ-TEST-1042",
        "order_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "status": "Behandlas (exempel)",
        "payment_method": "Klarna / Kort (exempel)",
        "shipping_method": "Standardleverans 2–4 vardagar (exempel)",
        "currency": detected_currency,
        "subtotal": _fmt_amt(subtotal),
        "shipping": _fmt_amt(shipping_num),
        "total": _fmt_amt(total_num),
        "items": line_items,
        "synthetic_data": True,
        "contains_real_pii": False,
    }
    return ToolOutput(
        {
            "ok": True,
            "status": "built",
            "site_id": site_id,
            "scenario": scenario,
            "currency": detected_currency,
            "products": products,
            "guides": guides,
            "sample_order": sample_order,
            "sample_customer": sample_customer,
            "synthetic_data": True,
            "contains_real_pii": False,
            "live_calls": live_calls,
        },
        site_id=site_id,
    )


__all__ = [
    "handle_build_sample_data",
    "handle_get_page",
    "handle_list_pages",
    "normalize_wp_page",
]
