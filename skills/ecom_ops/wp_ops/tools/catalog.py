"""Read-only product catalog tools and normalization/price helpers (zero HTTP I/O)."""

from __future__ import annotations

import re
import sys
import urllib.parse
from typing import Any

from ecom_ops.wp_ops.file_changes import rest_reads_allowed as _default_rest_reads_allowed
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.review_workspace import ReviewError
from ecom_ops.wp_ops.tools.html_parse import (
    extract_json_ld_image_url,
    extract_json_ld_products,
    extract_product_dom_fields,
    parse_product_links,
    strip_html_plain,
)
from ecom_ops.wp_ops.tools.registry import count_outcome, register_tool
from ecom_ops.wp_ops.tools.types import (
    PRODUCT_ID_RE,
    PRODUCT_LINK_PATH_RE,
    PRODUCT_SLUG_RE,
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


def parse_product_id(value: Any) -> int | None:
    if type(value) is int:
        return value if 1 <= value <= 999_999_999_999 else None
    if isinstance(value, str):
        cleaned = value.strip()
        if PRODUCT_ID_RE.fullmatch(cleaned):
            return int(cleaned)
    return None


def slug_from_text(text: str) -> str:
    lowered = str(text or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug[:100]


def slug_from_product_path(path: str) -> str:
    parts = [p for p in str(path or "").strip().split("/") if p]
    if not parts:
        return ""
    cand = parts[-1].lower()
    return cand if PRODUCT_SLUG_RE.fullmatch(cand) else ""


def format_store_price(prices: dict[str, Any], field: str) -> str:
    raw_val = prices.get(field)
    if raw_val in (None, ""):
        return ""
    try:
        minor_unit = int(prices.get("currency_minor_unit") or 0)
        num = int(str(raw_val).strip())
        if minor_unit > 0:
            scaled = num / (10**minor_unit)
            formatted = f"{scaled:.{minor_unit}f}"
        else:
            formatted = str(num)
    except (TypeError, ValueError):
        formatted = str(raw_val).strip()
    currency = str(prices.get("currency_code") or prices.get("currency_symbol") or "").strip()
    return f"{formatted} {currency}".strip() if currency else formatted


def infer_site_currency(site_obj: Site, price_text: str = "") -> str:
    text_up = str(price_text or "").upper()
    for code in ("NOK", "SEK", "DKK", "EUR", "USD"):
        if code in text_up:
            return code
    host = (urllib.parse.urlparse(str(site_obj.canonical_url or "")).hostname or "").lower()
    if host.endswith(".no"):
        return "NOK"
    if host.endswith(".dk"):
        return "DKK"
    return "SEK"


def extract_id_list(raw: Any, *, limit: int = 6) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        if type(item) is int and item > 0 and item not in out:
            out.append(item)
        elif isinstance(item, str) and PRODUCT_ID_RE.fullmatch(item.strip()):
            val = int(item.strip())
            if val not in out:
                out.append(val)
        if len(out) >= limit:
            break
    return out


def parse_numeric_price(raw_price: Any) -> float | None:
    text = str(raw_price or "").strip()
    if not text:
        return None
    if re.search(r"(?:^|[\s(])[-−]\s*\d", text):
        return None
    cleaned = re.sub(r"[^0-9.,]", "", text)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")
    try:
        val = float(cleaned)
        return val if val >= 0 else None
    except ValueError:
        return None


def product_path_from_url(tools: Any, url_or_path: Any, *, fallback_slug: str = "") -> str:
    raw = str(url_or_path or "").strip()
    if raw:
        parsed = urllib.parse.urlparse(raw)
        cand = parsed.path if parsed.scheme else raw
        if cand and not cand.startswith("/"):
            cand = "/" + cand
        try:
            clean = tools._validate_preview_path(cand)
            if clean != "/":
                return clean
        except ReviewError:
            pass
    slug = fallback_slug.strip().lower()
    if slug and PRODUCT_SLUG_RE.fullmatch(slug):
        return f"/produkt/{slug}/"
    return "/"


def safe_catalog_image_url(tools: Any, site_obj: Site, raw_url: Any) -> str:
    if not isinstance(raw_url, str):
        return ""
    cand = raw_url.strip()
    if not cand or len(cand) > 400 or any(ord(ch) < 32 or ord(ch) == 127 for ch in cand):
        return ""
    if cand.startswith("/") and not cand.startswith("//"):
        base = str(site_obj.canonical_url or "").rstrip("/")
        return f"{base}{cand}" if base else cand
    parsed = urllib.parse.urlparse(cand)
    if parsed.scheme != "https":
        return ""
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return ""
    site_host = (urllib.parse.urlparse(str(site_obj.canonical_url or "")).hostname or "").lower()
    allowed = tools._allowed_site_hosts(site_host) | tools._static_asset_hosts()
    if host in allowed or (not _rest_reads_allowed() and host.endswith(".example.test")):
        return cand
    return ""


def normalize_product_summary(tools: Any, site_obj: Site, row: dict[str, Any]) -> dict[str, Any]:
    pid = parse_product_id(row.get("id"))
    raw_name = row.get("name")
    if raw_name is None and isinstance(row.get("title"), dict):
        raw_name = row["title"].get("rendered")
    name = tools._scrub_bounded(site_obj, strip_html_plain(raw_name), max_len=160)
    raw_slug = str(row.get("slug") or "").strip().lower()
    slug = raw_slug if PRODUCT_SLUG_RE.fullmatch(raw_slug) else slug_from_text(name)
    link = str(row.get("permalink") or row.get("link") or "").strip()
    path = product_path_from_url(tools, link, fallback_slug=slug)
    if not slug:
        slug = slug_from_product_path(path)
    canonical_base = str(site_obj.canonical_url or "").rstrip("/")
    permalink = link if link.startswith("https://") else (f"{canonical_base}{path}" if canonical_base else path)
    prices = row.get("prices") if isinstance(row.get("prices"), dict) else None
    if prices:
        price = format_store_price(prices, "price")
        currency = str(prices.get("currency_code") or "").strip().upper() or infer_site_currency(site_obj, price)
    else:
        price = str(row.get("price") or row.get("regular_price") or "").strip()
        currency = str(row.get("currency") or "").strip().upper() or infer_site_currency(site_obj, price)
    raw_image = ""
    images = row.get("images")
    if isinstance(images, list) and images:
        first_img = images[0]
        if isinstance(first_img, dict):
            raw_image = str(first_img.get("src") or first_img.get("thumbnail") or "")
        elif isinstance(first_img, str):
            raw_image = first_img
    if not raw_image and isinstance(row.get("image_url"), str):
        raw_image = row["image_url"]
    image_url = safe_catalog_image_url(tools, site_obj, raw_image)
    sku = tools._scrub_bounded(site_obj, row.get("sku") or "", max_len=64)
    status = tools._scrub_bounded(site_obj, row.get("status") or "publish", max_len=32)
    return {
        "id": pid,
        "name": name,
        "slug": slug[:120],
        "path": path,
        "permalink": permalink[:300],
        "price": tools._scrub_bounded(site_obj, price, max_len=40),
        "currency": currency[:8],
        "image_url": image_url,
        "sku": sku,
        "status": status or "publish",
        "upsell_ids": extract_id_list(row.get("upsell_ids")),
        "cross_sell_ids": extract_id_list(row.get("cross_sell_ids")),
    }


def normalize_product_detail(tools: Any, site_obj: Site, row: dict[str, Any]) -> dict[str, Any]:
    summary = normalize_product_summary(tools, site_obj, row)
    prices = row.get("prices") if isinstance(row.get("prices"), dict) else None
    if prices:
        price = format_store_price(prices, "price")
        regular_price = format_store_price(prices, "regular_price")
        sale_price = format_store_price(prices, "sale_price")
    else:
        price = str(row.get("price") or row.get("regular_price") or "").strip()
        regular_price = str(row.get("regular_price") or "").strip()
        sale_price = str(row.get("sale_price") or "").strip()
    raw_short = row.get("short_description")
    if raw_short is None and isinstance(row.get("excerpt"), dict):
        raw_short = row["excerpt"].get("rendered")
    raw_desc = row.get("description")
    if raw_desc is None and isinstance(row.get("content"), dict):
        raw_desc = row["content"].get("rendered")
    add_to_cart = row.get("add_to_cart") if isinstance(row.get("add_to_cart"), dict) else {}
    button_text = tools._scrub_bounded(site_obj, add_to_cart.get("text") or "", max_len=120)
    raw_cats = row.get("categories") if isinstance(row.get("categories"), list) else []
    categories: list[str] = []
    for cat in raw_cats[:15]:
        if isinstance(cat, dict) and cat.get("name"):
            val = tools._scrub_bounded(site_obj, strip_html_plain(cat.get("name")), max_len=80)
            if val and val not in categories:
                categories.append(val)
        elif isinstance(cat, str) and cat.strip():
            val = tools._scrub_bounded(site_obj, cat.strip(), max_len=80)
            if val and val not in categories:
                categories.append(val)
    return {
        **summary,
        "name": tools._scrub_bounded(site_obj, summary["name"], max_len=200),
        "price": tools._scrub_bounded(site_obj, price, max_len=64),
        "regular_price": tools._scrub_bounded(site_obj, regular_price, max_len=64),
        "sale_price": tools._scrub_bounded(site_obj, sale_price, max_len=64),
        "short_description": tools._scrub_bounded(site_obj, strip_html_plain(raw_short), max_len=1000),
        "description": tools._scrub_bounded(site_obj, strip_html_plain(raw_desc), max_len=3000),
        "button_text": button_text,
        "categories": categories,
    }


def extract_products_from_html(
    tools: Any,
    site_obj: Site,
    raw_html: str,
    *,
    search: str = "",
    limit: int = 15,
) -> list[dict[str, Any]]:
    site_host = (urllib.parse.urlparse(str(site_obj.canonical_url or "")).hostname or "").lower()
    allowed_hosts = tools._allowed_site_hosts(site_host)

    needle = search.strip().lower()
    candidates = parse_product_links(raw_html)
    seen_paths: dict[str, dict[str, Any]] = {}
    for href, label in candidates:
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            continue
        if parsed.hostname and parsed.hostname.lower() not in allowed_hosts:
            continue
        cand_path = parsed.path or ""
        match = PRODUCT_LINK_PATH_RE.fullmatch(cand_path)
        if not match:
            continue
        slug = match.group(1).lower()
        if ".." in slug:
            continue
        try:
            clean_path = tools._validate_preview_path(cand_path)
        except ReviewError:
            continue
        if not clean_path.endswith("/"):
            clean_path += "/"
        clean_label = tools._scrub_bounded(site_obj, label, max_len=160)
        fallback_name = slug.replace("-", " ").replace("_", " ").title()
        name = clean_label if len(clean_label) >= 2 else fallback_name
        if needle and needle not in name.lower() and needle not in slug and needle not in clean_path.lower():
            continue
        existing = seen_paths.get(clean_path)
        if existing is None or (len(existing["name"]) < len(clean_label) and len(clean_label) <= 120):
            seen_paths[clean_path] = {
                "id": None,
                "name": name,
                "slug": slug,
                "path": clean_path,
                "price": "",
                "sku": "",
                "status": "publish",
            }
        if len(seen_paths) >= limit * 2:
            break
    return list(seen_paths.values())[:limit]


def extract_product_detail_from_html(
    tools: Any,
    site_obj: Site,
    raw_html: str,
    *,
    path: str,
) -> dict[str, Any]:
    slug = slug_from_product_path(path)
    name = ""
    price = ""
    regular_price = ""
    sale_price = ""
    short_desc = ""
    desc = ""
    sku = ""
    button_text = ""
    image_url = ""
    currency = ""
    categories: list[str] = []

    for node in extract_json_ld_products(raw_html):
        if not name and isinstance(node.get("name"), str):
            name = strip_html_plain(node["name"])
        if not sku and node.get("sku"):
            sku = str(node["sku"]).strip()
        if not desc and isinstance(node.get("description"), str):
            desc = strip_html_plain(node["description"])
        if not image_url:
            cand_img = extract_json_ld_image_url(node)
            image_url = safe_catalog_image_url(tools, site_obj, cand_img)
        offers = node.get("offers")
        offer = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offer, dict) and not price:
            p_val = offer.get("price") or offer.get("lowPrice") or ""
            curr = str(offer.get("priceCurrency") or "").strip().upper()
            if curr:
                currency = curr
            if p_val not in ("", None):
                price = f"{p_val} {curr}".strip() if curr else str(p_val)

    dom = extract_product_dom_fields(raw_html)
    if dom["h1"]:
        name = dom["h1"]
    if not name and dom["title"]:
        name = dom["title"]
    if dom["short_description"]:
        short_desc = dom["short_description"]
    if dom["tab_description"] and not desc:
        desc = dom["tab_description"]
    if dom["regular_price"]:
        regular_price = dom["regular_price"]
    if dom["sale_price"]:
        sale_price = dom["sale_price"]
    if not price and dom["price"]:
        price = dom["price"]
    if dom["button_text"]:
        button_text = dom["button_text"]
    if not sku and dom["sku"]:
        sku = dom["sku"]
    for c_raw in dom["categories"]:
        c_txt = tools._scrub_bounded(site_obj, c_raw, max_len=80)
        if c_txt and c_txt not in categories and len(categories) < 15:
            categories.append(c_txt)
    if not image_url and dom["og_image"]:
        image_url = safe_catalog_image_url(tools, site_obj, dom["og_image"])
    canonical_base = str(site_obj.canonical_url or "").rstrip("/")
    permalink = f"{canonical_base}{path}" if canonical_base else path
    resolved_currency = currency or infer_site_currency(site_obj, price)

    return {
        "id": None,
        "name": tools._scrub_bounded(site_obj, name, max_len=200),
        "slug": slug,
        "path": path,
        "permalink": permalink[:300],
        "price": tools._scrub_bounded(site_obj, price, max_len=64),
        "regular_price": tools._scrub_bounded(site_obj, regular_price or price, max_len=64),
        "sale_price": tools._scrub_bounded(site_obj, sale_price, max_len=64),
        "currency": resolved_currency[:8],
        "image_url": image_url,
        "short_description": tools._scrub_bounded(site_obj, short_desc, max_len=1000),
        "description": tools._scrub_bounded(site_obj, desc, max_len=3000),
        "sku": tools._scrub_bounded(site_obj, sku, max_len=64),
        "button_text": tools._scrub_bounded(site_obj, button_text, max_len=120),
        "categories": categories,
        "upsell_ids": [],
        "cross_sell_ids": [],
        "status": "publish",
    }


def _get_product_outcome(data: dict[str, Any], _reads: int | None) -> str:
    prod = data.get("product")
    if isinstance(prod, dict) and prod.get("slug"):
        return f"produkt: {short_text(prod.get('slug'), 40)}"
    return "produktdetaljer hämtade"


@register_tool(
    name="wp_list_products",
    group="file",
    label="Lista produkter",
    short_label="Produktlista (sandbox)",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "search": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        "additionalProperties": False,
    },
    outcome=count_outcome("products", "produkter", "produkter listade"),
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="preview_catalog",
)
def handle_list_products(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    site_obj, denied_out = tools._site_for_preview(site_id, ctx.actor)
    if denied_out is not None:
        return denied_out
    assert site_obj is not None

    raw_search = arguments.get("search", "")
    if raw_search is not None and (not isinstance(raw_search, str) or len(raw_search) > 80):
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    search = (raw_search or "").strip()

    raw_limit = arguments.get("limit", 15)
    if type(raw_limit) is not int or raw_limit < 1 or raw_limit > 30:
        return ToolOutput(error_payload("InvalidToolArguments"), site_id)
    limit = raw_limit

    if not _rest_reads_allowed():
        client = tools._mock_woo_client()
        rows = client.list_products(per_page=limit, search=search or None)
        items = [normalize_product_summary(tools, site_obj, row) for row in rows[:limit] if isinstance(row, dict)]
        return ToolOutput(
            {
                "ok": True,
                "status": "listed",
                "site_id": site_id,
                "source": "mock",
                "count": len(items),
                "products": items,
                "live_calls": False,
            },
            site_id=site_id,
        )

    woo = tools._try_woo_client_read(site_obj)
    if woo is not None:
        try:
            rows = woo.list_products(per_page=limit, search=search or None)
            if isinstance(rows, list) and rows:
                items = [
                    normalize_product_summary(tools, site_obj, row)
                    for row in rows[:limit]
                    if isinstance(row, dict)
                ]
                return ToolOutput(
                    {
                        "ok": True,
                        "status": "listed",
                        "site_id": site_id,
                        "source": "woo_rest",
                        "count": len(items),
                        "products": items,
                        "live_calls": True,
                    },
                    site_id=site_id,
                )
        except Exception:
            pass

    for source_name, endpoint, use_auth in (
        ("wc_store_api", "/wp-json/wc/store/v1/products", False),
        ("wp_rest", "/wp-json/wp/v2/product", True),
    ):
        rows = tools._safe_site_get_json(
            site_obj,
            endpoint,
            params={"per_page": limit, "search": search or None},
            use_wp_auth=use_auth,
        )
        if isinstance(rows, list) and rows:
            items = [
                normalize_product_summary(tools, site_obj, row)
                for row in rows[:limit]
                if isinstance(row, dict)
            ]
            return ToolOutput(
                {
                    "ok": True,
                    "status": "listed",
                    "site_id": site_id,
                    "source": source_name,
                    "count": len(items),
                    "products": items,
                    "live_calls": True,
                },
                site_id=site_id,
            )

    collected: dict[str, dict[str, Any]] = {}
    for candidate_path in ("/butikk/", "/shop/", "/", "/product-sitemap.xml"):
        try:
            fetched = tools._fetch_live_html(site_obj, candidate_path)
        except ReviewError:
            continue
        extracted = extract_products_from_html(
            tools,
            site_obj,
            str(fetched.get("html") or ""),
            search=search,
            limit=limit,
        )
        for prod in extracted:
            if prod["path"] not in collected:
                collected[prod["path"]] = prod
        if collected:
            break
    items = list(collected.values())[:limit]
    return ToolOutput(
        {
            "ok": True,
            "status": "listed",
            "site_id": site_id,
            "source": "live_html" if items else "unavailable",
            "count": len(items),
            "products": items,
            "live_calls": True,
        },
        site_id=site_id,
    )


def _lookup_live_product_rest(
    tools: Any,
    site_obj: Site,
    *,
    product_id: int | None,
    slug: str,
) -> tuple[dict[str, Any] | None, str]:
    woo = tools._try_woo_client_read(site_obj)
    if woo is not None:
        try:
            if product_id is not None:
                row = woo.get_product(product_id)
                if isinstance(row, dict) and row:
                    return normalize_product_detail(tools, site_obj, row), "woo_rest"
            elif slug:
                rows = woo.transport.request(
                    "GET",
                    woo._url("/wp-json/wc/v3/products"),
                    auth=woo._auth(),
                    params={"slug": slug, "per_page": 1},
                    timeout=10.0,
                )
                if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                    return normalize_product_detail(tools, site_obj, rows[0]), "woo_rest"
        except Exception:
            pass

    for source_name, base_ep, use_auth in (
        ("wc_store_api", "/wp-json/wc/store/v1/products", False),
        ("wp_rest", "/wp-json/wp/v2/product", True),
    ):
        if product_id is not None:
            row = tools._safe_site_get_json(site_obj, f"{base_ep}/{product_id}", use_wp_auth=use_auth)
            if isinstance(row, dict) and row.get("id"):
                return normalize_product_detail(tools, site_obj, row), source_name
        elif slug:
            rows = tools._safe_site_get_json(
                site_obj,
                base_ep,
                params={"slug": slug, "per_page": 1},
                use_wp_auth=use_auth,
            )
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return normalize_product_detail(tools, site_obj, rows[0]), source_name
    return None, ""


@register_tool(
    name="wp_get_product",
    group="file",
    label="Hämta produktdetaljer",
    short_label="Produktdetalj (sandbox)",
    schema={
        "type": "object",
        "properties": {
            "site_id": {"type": "string"},
            "product_id": {"type": ["integer", "string"]},
            "slug": {"type": "string"},
            "path": {"type": "string"},
        },
        "additionalProperties": False,
    },
    outcome=_get_product_outcome,
    in_file_schemas=True,
    in_extra_file_tools=True,
    untrusted_output=True,
    capability_group="preview_catalog",
)
def handle_get_product(tools: Any, ctx: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    site_id = str(ctx.site_id)
    site_obj, denied_out = tools._site_for_preview(site_id, ctx.actor)
    if denied_out is not None:
        return denied_out
    assert site_obj is not None

    has_pid = "product_id" in arguments
    has_slug = "slug" in arguments
    has_path = "path" in arguments
    if not (has_pid or has_slug or has_path):
        err = error_payload("InvalidToolArguments")
        err["hint"] = "Ange minst ett av product_id, slug eller path (t.ex. '/produkt/...')."
        return ToolOutput(err, site_id)

    product_id: int | None = None
    if has_pid:
        product_id = parse_product_id(arguments.get("product_id"))
        if product_id is None:
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)

    slug = ""
    if has_slug:
        raw_slug = arguments.get("slug")
        if not isinstance(raw_slug, str) or not PRODUCT_SLUG_RE.fullmatch(raw_slug.strip().lower()):
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        slug = raw_slug.strip().lower()

    clean_path = ""
    if has_path:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolOutput(error_payload("InvalidToolArguments"), site_id)
        try:
            clean_path = tools._validate_preview_path(raw_path)
        except ReviewError as exc:
            return ToolOutput(error_payload(exc.error_class), site_id)
        if not slug:
            slug = slug_from_product_path(clean_path)

    if not _rest_reads_allowed():
        client = tools._mock_woo_client()
        matched_row: dict[str, Any] | None = None
        if product_id is not None:
            try:
                row = client.get_product(product_id)
                if isinstance(row, dict):
                    matched_row = row
            except Exception:
                matched_row = None
        if matched_row is None and (slug or clean_path):
            for row in client.list_products(per_page=30):
                if not isinstance(row, dict):
                    continue
                norm = normalize_product_summary(tools, site_obj, row)
                if (slug and norm["slug"] == slug) or (
                    clean_path and norm["path"].rstrip("/") == clean_path.rstrip("/")
                ):
                    matched_row = row
                    break
        if matched_row is None:
            err = error_payload("ProductNotFound")
            err["recovery_tool"] = "wp_list_products"
            err["hint"] = "Produkten hittades inte. Använd wp_list_products för att lista tillgängliga produkter."
            return ToolOutput(err, site_id)
        detail = normalize_product_detail(tools, site_obj, matched_row)
        return ToolOutput(
            {
                "ok": True,
                "status": "read",
                "site_id": site_id,
                "source": "mock",
                "product": detail,
                "live_calls": False,
            },
            site_id=site_id,
        )

    detail, source = _lookup_live_product_rest(tools, site_obj, product_id=product_id, slug=slug)

    html_paths: list[str] = []
    if clean_path:
        html_paths.append(clean_path)
    elif detail and detail.get("path") and detail["path"] != "/":
        html_paths.append(str(detail["path"]))
    elif slug:
        html_paths.extend([f"/produkt/{slug}/", f"/product/{slug}/"])

    if html_paths and (
        detail is None or has_path or not detail.get("button_text") or not detail.get("price")
    ):
        for cand_path in html_paths:
            try:
                fetched = tools._fetch_live_html(site_obj, cand_path)
            except ReviewError:
                continue
            html_detail = extract_product_detail_from_html(
                tools, site_obj, str(fetched.get("html") or ""), path=cand_path
            )
            if detail is None:
                detail = html_detail
                source = "live_html"
            else:
                for key in (
                    "name",
                    "price",
                    "regular_price",
                    "sale_price",
                    "short_description",
                    "description",
                    "sku",
                    "button_text",
                ):
                    if not detail.get(key) and html_detail.get(key):
                        detail[key] = html_detail[key]
                if not detail.get("categories") and html_detail.get("categories"):
                    detail["categories"] = html_detail["categories"]
                if (not detail.get("path") or detail["path"] == "/") and html_detail.get("path"):
                    detail["path"] = html_detail["path"]
            break

    if detail is None:
        err = error_payload("ProductNotFound")
        err["recovery_tool"] = "wp_list_products"
        err["hint"] = "Produkten hittades inte. Kör wp_list_products eller kontrollera sökvägen."
        return ToolOutput(err, site_id)

    return ToolOutput(
        {
            "ok": True,
            "status": "read",
            "site_id": site_id,
            "source": source or "live_html",
            "product": detail,
            "live_calls": True,
        },
        site_id=site_id,
    )


__all__ = [
    "extract_id_list",
    "extract_product_detail_from_html",
    "extract_products_from_html",
    "format_store_price",
    "handle_get_product",
    "handle_list_products",
    "infer_site_currency",
    "normalize_product_detail",
    "normalize_product_summary",
    "parse_numeric_price",
    "parse_product_id",
    "product_path_from_url",
    "safe_catalog_image_url",
    "slug_from_product_path",
    "slug_from_text",
]
