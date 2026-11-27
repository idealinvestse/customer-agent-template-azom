"""Deterministic analyzers and cross-run drift/security engine for Shop Intelligence.

Pure-Python heuristics: zero LLM calls (``cost_usd=0.0``), zero direct network or
subprocess imports.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from typing import Any

from ecom_ops.shop_intel.models import (
    IntelDiff,
    IntelFact,
    IntelRun,
    SecurityFinding,
    canonical_sha256,
    new_diff_id,
    new_finding_id,
    utc_now_iso,
)
from ecom_ops.shop_intel.store import scrub_payload, scrub_text
from ecom_ops.wp_ops.code_index import index_sources
from ecom_ops.wp_ops.theme_json import _validate_theme_json_doc
from ecom_ops.wp_ops.woo_templates import audit_overrides

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_PRICE_NUM_RE = re.compile(r"\d+(?:[.,]\d{1,2})?")
_SKU_PREFIX_RE = re.compile(r"^([A-Za-z]{2,8}-)")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")

_DU_RE = re.compile(r"\b(?:du|dig|din|ditt|dina|deg)\b", re.I)
_NI_RE = re.compile(r"\b(?:ni|er|ert|era|deres)\b", re.I)

_LANG_MARKERS: dict[str, re.Pattern[str]] = {
    "sv": re.compile(r"\b(?:och|för|med|frakt|öppet\s+köp|ångerrätt|köpvillkor|trådlös|vardagar)\b", re.I),
    "nb": re.compile(r"\b(?:og|for|med|frakt|angrerett|kjøpsvilkår|trådløs|virkedager|nettbutikk|produkter)\b", re.I),
    "da": re.compile(r"\b(?:og|for|med|fragt|fortrydelsesret|handelsbetingelser|trådløs|hverdage)\b", re.I),
    "en": re.compile(r"\b(?:and|for|with|shipping|warranty|checkout|returns|wireless|business\s+days)\b", re.I),
}

_USP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Fri frakt", re.compile(r"\bfri\s+frakt\b|\bfree\s+shipping\b", re.I)),
    ("Plug-and-play", re.compile(r"\bplug[\s-]+and[\s-]+play\b", re.I)),
    ("Nordisk garanti", re.compile(r"\bnordisk\s+garanti\b|\b\d+\s*(?:års|år)\s+garanti\b", re.I)),
    ("Öppet köp", re.compile(r"\b[öo]ppet\s+k[öo]p\b|\b[åa]pent\s+kj[øo]p\b", re.I)),
    ("Snabb leverans", re.compile(r"\bsnabb\s+leverans\b|\brask\s+levering\b|\bfast\s+delivery\b", re.I)),
    ("Trådlös CarPlay", re.compile(r"\btr[åa]dl[öo]s(?:\s+apple)?\s+carplay\b", re.I)),
    ("Trygg betalning", re.compile(r"\b(?:klarna|vipps|swish)\b", re.I)),
)

_CTA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Köp nu", re.compile(r"\bk[öo]p(?:\s+nu)?\b", re.I)),
    ("Lägg i varukorg", re.compile(r"\bl[äa]gg\s+i\s+(?:varukorg|handlekurv)\b", re.I)),
    ("Beställ", re.compile(r"\bbest[äa]ll\b", re.I)),
    ("Uppgradera", re.compile(r"\buppgradera\b|\boppgrader\b", re.I)),
    ("Läs mer", re.compile(r"\bl[äa]s\s+mer\b|\bles\s+mer\b", re.I)),
    ("Kontakta oss", re.compile(r"\bkontakta\s+oss\b|\bkontakt\s+oss\b", re.I)),
)

_POLICY_MATCHERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("angerratt", re.compile(r"\b(?:[åa]ngerr[äa]tt|angrerett|fortrydelsesret|[öo]ppet[\s-]+k[öo]p|oppent[\s-]+kjop)\b", re.I)),
    ("retur", re.compile(r"\b(?:retur\w*|return\w*|byte|byten)\b", re.I)),
    ("frakt", re.compile(r"\b(?:frakt\w*|leverans\w*|levering\w*|shipping|forsendelse)\b", re.I)),
    ("garanti", re.compile(r"\b(?:garanti\w*|warranty|reklamation\w*)\b", re.I)),
    ("kopvillkor", re.compile(r"\b(?:k[öo]pvillkor|kopvillkor|vilk[åa]r|handelsbetingelser|allm[äa]nna\s+villkor|terms)\b", re.I)),
    ("integritet", re.compile(r"\b(?:integritet\w*|personuppgifter|personvern|privacy|gdpr|cookies?)\b", re.I)),
    ("om_oss", re.compile(r"\b(?:om[\s-]+oss|about[\s-]+us|kontakt\w*|contact)\b", re.I)),
    ("faq", re.compile(r"\b(?:faq|vanliga[\s-]+fr[åa]gor|sp[øo]rsm[åa]l)\b", re.I)),
)

_RETURN_DAYS_RE = re.compile(
    r"(\d{1,3})\s*(?:dag(?:ar|ars|ers|e)?|days?)\s*(?:s\s+)?(?:[öo]ppet\s+k[öo]p|retur\w*|return\w*)",
    re.I,
)
_WITHDRAWAL_DAYS_RE = re.compile(
    r"(\d{1,3})\s*(?:dag(?:ar|ars|ers|e)?|days?)\s*(?:s\s+)?(?:lagstadgad\s+)?(?:[åa]ngerr[äa]tt|angrerett|fortrydelsesret)",
    re.I,
)
_WARRANTY_YEARS_RE = re.compile(
    r"(?:(\d{1,2})\s*(?:[åa]r(?:s)?|years?)\s*(?:nordisk\s+)?garanti|garanti\s*(?:i\s*)?(\d{1,2})\s*(?:[åa]r|years?))",
    re.I,
)
_DELIVERY_WINDOW_RE = re.compile(
    r"(\d{1,2}\s*[-–]\s*\d{1,2}\s*(?:vardagar|arbetsdagar|virkedager|hverdage|business\s+days|days))",
    re.I,
)
_FREE_SHIPPING_TEXT_RE = re.compile(
    r"fri\s+frakt\s+(?:[öo]ver|over|fr[åa]n|fra|vid\s+k[öo]p\s+[öo]ver)\s*(\d[\d\s]{1,6})\s*(?:kr|sek|nok|dkk)",
    re.I,
)

_CRITICAL_PLUGIN_KEYWORDS = (
    "woocommerce",
    "klarna",
    "vipps",
    "stripe",
    "svea",
    "azom-ops-bridge",
    "wordfence",
    "hardening",
)

_SEVERITY_PENALTY: dict[str, int] = {
    "critical": 25,
    "high": 15,
    "medium": 7,
    "low": 3,
    "info": 0,
}


def _strip_html(raw: str) -> str:
    text = _HTML_TAG_RE.sub(" ", str(raw or ""))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _parse_price(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(" ", "")
    if not text:
        return None
    m = _PRICE_NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _analyze_platform_rules(signals: dict[str, Any]) -> dict[str, Any]:
    woo = signals.get("woo") or {}
    wp = signals.get("wp") or {}
    bridge = signals.get("bridge") or {}
    site = signals.get("site") or {}

    settings_map: dict[str, dict[str, Any]] = {}
    for group_id, rows in (woo.get("settings_groups") or {}).items():
        group_vals: dict[str, Any] = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    group_vals[str(row["id"])] = row.get("value")
        settings_map[str(group_id)] = group_vals

    gen = settings_map.get("general", {})
    tax_cfg = settings_map.get("tax", {})
    prod_cfg = settings_map.get("products", {})
    chk_cfg = settings_map.get("checkout", {})

    zones_summary: list[dict[str, Any]] = []
    free_shipping_thresholds: list[dict[str, Any]] = []
    carriers: list[str] = []

    for zone in woo.get("shipping_zones") or []:
        if not isinstance(zone, dict):
            continue
        zone_name = str(zone.get("name") or "")
        methods_out: list[dict[str, Any]] = []
        for method in zone.get("methods") or []:
            if not isinstance(method, dict):
                continue
            m_title = str(method.get("title") or method.get("method_title") or "")
            m_id = str(method.get("method_id") or "")
            enabled = bool(method.get("enabled", True))
            m_settings = method.get("settings") if isinstance(method.get("settings"), dict) else {}
            cost_val = (
                m_settings.get("cost", {}).get("value")
                if isinstance(m_settings.get("cost"), dict)
                else m_settings.get("cost")
            )
            min_val = (
                m_settings.get("min_amount", {}).get("value")
                if isinstance(m_settings.get("min_amount"), dict)
                else m_settings.get("min_amount")
            )
            req_val = (
                m_settings.get("requires", {}).get("value")
                if isinstance(m_settings.get("requires"), dict)
                else m_settings.get("requires")
            )
            methods_out.append(
                {
                    "id": method.get("id"),
                    "method_id": m_id,
                    "title": m_title,
                    "enabled": enabled,
                    "cost": cost_val,
                    "min_amount": min_val,
                    "requires": req_val,
                }
            )
            if enabled:
                for carrier_name in ("PostNord", "Bring", "DHL", "Budbee", "Instabox", "Schenker"):
                    if carrier_name.lower() in m_title.lower() and carrier_name not in carriers:
                        carriers.append(carrier_name)
                if m_id == "free_shipping":
                    num_min = _parse_price(min_val)
                    free_shipping_thresholds.append(
                        {
                            "zone": zone_name,
                            "min_amount": num_min if num_min is not None else min_val,
                            "requires": str(req_val or ""),
                        }
                    )
        zones_summary.append({"id": zone.get("id"), "name": zone_name, "methods": methods_out})

    gateways_out: list[dict[str, Any]] = []
    enabled_gateways: list[str] = []
    disabled_gateways: list[str] = []
    for gw in woo.get("payment_gateways") or []:
        if not isinstance(gw, dict):
            continue
        gw_id = str(gw.get("id") or "")
        gw_title = str(gw.get("title") or gw.get("method_title") or gw_id)
        is_en = bool(gw.get("enabled", False))
        gateways_out.append({"id": gw_id, "title": gw_title, "enabled": is_en})
        if is_en:
            enabled_gateways.append(gw_title)
        else:
            disabled_gateways.append(gw_title)

    tax_classes = [
        {
            "slug": str(tc.get("slug") or ""),
            "name": str(tc.get("name") or ""),
            "rate_percent": tc.get("rate_percent"),
        }
        for tc in (woo.get("tax_classes") or [])
        if isinstance(tc, dict)
    ]

    sys_st = woo.get("system_status") or {}
    br_health = bridge.get("health") or {}
    wp_ver = str(
        br_health.get("wp_version")
        or sys_st.get("wordpress_version")
        or site.get("wordpress_version")
        or ""
    ).strip()
    woo_ver = str(
        br_health.get("wc_version")
        or br_health.get("woo_version")
        or sys_st.get("woocommerce_version")
        or ""
    ).strip()
    php_ver = str(
        br_health.get("php_version") or sys_st.get("php_version") or site.get("php_version") or ""
    ).strip()

    return {
        "currency": str(gen.get("woocommerce_currency") or "SEK"),
        "default_country": str(gen.get("woocommerce_default_country") or "SE"),
        "allowed_countries": gen.get("woocommerce_specific_allowed_countries") or [],
        "calc_taxes": str(gen.get("woocommerce_calc_taxes") or "").lower() == "yes",
        "prices_include_tax": str(tax_cfg.get("woocommerce_prices_include_tax") or "").lower() == "yes",
        "default_tax_rate_percent": _parse_price(tax_cfg.get("woocommerce_default_tax_rate_percent")) or 25.0,
        "weight_unit": str(prod_cfg.get("woocommerce_weight_unit") or "kg"),
        "dimension_unit": str(prod_cfg.get("woocommerce_dimension_unit") or "cm"),
        "guest_checkout": str(chk_cfg.get("woocommerce_enable_guest_checkout") or "").lower() == "yes",
        "force_ssl_checkout": str(chk_cfg.get("woocommerce_force_ssl_checkout") or "").lower() == "yes",
        "shipping_zones": zones_summary,
        "carriers": carriers,
        "free_shipping_thresholds": free_shipping_thresholds,
        "enabled_gateways": enabled_gateways,
        "disabled_gateways": disabled_gateways,
        "payment_gateways": gateways_out,
        "tax_classes": tax_classes,
        "versions": {
            "wordpress": wp_ver,
            "woocommerce": woo_ver,
            "php": php_ver,
        },
        "site_language": str((wp.get("settings") or {}).get("language") or ""),
    }


def _analyze_catalog_pricing(signals: dict[str, Any]) -> dict[str, Any]:
    woo = signals.get("woo") or {}
    products = [p for p in (woo.get("products") or []) if isinstance(p, dict)]
    categories = [c for c in (woo.get("categories") or []) if isinstance(c, dict)]
    attributes = [a for a in (woo.get("attributes") or []) if isinstance(a, dict)]

    positive_prices: list[float] = []
    zero_price_products: list[dict[str, Any]] = []
    sale_count = 0
    sku_prefixes: Counter[str] = Counter()
    stock_counts = {"instock": 0, "outofstock": 0, "onbackorder": 0, "low_stock_count": 0}
    upsell_count = 0
    cross_sell_count = 0
    gaps = {
        "missing_description": 0,
        "missing_short_description": 0,
        "missing_categories": 0,
        "missing_images": 0,
    }
    published_count = 0

    for prod in products:
        status = str(prod.get("status") or "publish").lower()
        is_pub = status == "publish"
        if is_pub:
            published_count += 1

        price = _parse_price(prod.get("price"))
        if price is None:
            price = _parse_price(prod.get("regular_price"))
        if price is not None and price > 0:
            positive_prices.append(price)
        elif is_pub:
            zero_price_products.append(
                {
                    "id": prod.get("id"),
                    "name": str(prod.get("name") or ""),
                    "slug": str(prod.get("slug") or ""),
                }
            )

        sale_price = _parse_price(prod.get("sale_price"))
        if bool(prod.get("on_sale")) or (sale_price is not None and sale_price > 0):
            sale_count += 1

        sku = str(prod.get("sku") or "").strip()
        if sku:
            m = _SKU_PREFIX_RE.match(sku)
            if m:
                sku_prefixes[m.group(1).upper()] += 1

        st_status = str(prod.get("stock_status") or "instock").lower()
        if st_status in ("instock", "outofstock", "onbackorder"):
            stock_counts[st_status] += 1
        qty = prod.get("stock_quantity")
        if isinstance(qty, int) and 0 < qty <= 3:
            stock_counts["low_stock_count"] += 1

        if prod.get("upsell_ids"):
            upsell_count += 1
        if prod.get("cross_sell_ids"):
            cross_sell_count += 1

        if not _strip_html(str(prod.get("description") or "")):
            gaps["missing_description"] += 1
        if not _strip_html(str(prod.get("short_description") or "")):
            gaps["missing_short_description"] += 1
        if not prod.get("categories"):
            gaps["missing_categories"] += 1
        if not prod.get("images"):
            gaps["missing_images"] += 1

    total = len(products)
    pricing = {
        "min_price": min(positive_prices) if positive_prices else None,
        "median_price": round(float(statistics.median(positive_prices)), 2) if positive_prices else None,
        "max_price": max(positive_prices) if positive_prices else None,
        "priced_count": len(positive_prices),
        "sale_count": sale_count,
        "sale_ratio": round(sale_count / total, 3) if total else 0.0,
        "zero_price_count": len(zero_price_products),
    }

    return {
        "product_count": total,
        "published_count": published_count,
        "categories": [
            {
                "id": c.get("id"),
                "name": str(c.get("name") or ""),
                "slug": str(c.get("slug") or ""),
                "count": c.get("count", 0),
            }
            for c in categories[:50]
        ],
        "attributes": [
            {
                "id": a.get("id"),
                "name": str(a.get("name") or ""),
                "slug": str(a.get("slug") or ""),
            }
            for a in attributes[:50]
        ],
        "pricing": pricing,
        "zero_price_products": zero_price_products[:25],
        "sku_prefixes": dict(sku_prefixes.most_common(10)),
        "stock_health": stock_counts,
        "upsell_density": round(upsell_count / total, 3) if total else 0.0,
        "cross_sell_density": round(cross_sell_count / total, 3) if total else 0.0,
        "catalog_gaps": gaps,
    }


def _parse_theme_json_summary(theme_files: dict[str, str]) -> dict[str, Any]:
    raw_json = theme_files.get("theme.json") or theme_files.get("_parent/theme.json")
    if not raw_json:
        return {"present": False, "version": None, "palette": [], "font_families": []}
    try:
        doc = json.loads(raw_json)
        version = _validate_theme_json_doc(doc)
    except Exception:
        return {"present": True, "valid": False, "version": None, "palette": [], "font_families": []}

    settings = doc.get("settings") if isinstance(doc.get("settings"), dict) else {}
    color_cfg = settings.get("color") if isinstance(settings.get("color"), dict) else {}
    typo_cfg = settings.get("typography") if isinstance(settings.get("typography"), dict) else {}

    palette = [
        {"slug": str(item.get("slug") or ""), "name": str(item.get("name") or ""), "color": str(item.get("color") or "")}
        for item in (color_cfg.get("palette") or [])
        if isinstance(item, dict)
    ][:30]
    fonts = [
        {"slug": str(item.get("slug") or ""), "name": str(item.get("name") or ""), "fontFamily": str(item.get("fontFamily") or "")}
        for item in (typo_cfg.get("fontFamilies") or [])
        if isinstance(item, dict)
    ][:20]
    return {
        "present": True,
        "valid": True,
        "version": version,
        "palette": palette,
        "font_families": fonts,
    }


def _analyze_theme_architecture(signals: dict[str, Any], *, platform_rules: dict[str, Any]) -> dict[str, Any]:
    wp = signals.get("wp") or {}
    bridge = signals.get("bridge") or {}
    theme_files_info = signals.get("theme_files") or {}
    sftp_source = str(theme_files_info.get("sftp_source") or "unavailable")
    raw_files: dict[str, str] = dict(theme_files_info.get("files") or {})

    active_theme = ""
    active_name = ""
    active_version = ""
    parent_theme = ""
    for row in wp.get("themes") or []:
        if isinstance(row, dict) and str(row.get("status") or "").lower() == "active":
            active_theme = str(row.get("stylesheet") or "")
            active_name = str(row.get("name") or active_theme)
            active_version = str(row.get("version") or "")
            tpl = str(row.get("template") or "")
            parent_theme = tpl if tpl and tpl != active_theme else ""
            break

    templates = [t for t in (wp.get("templates") or []) if isinstance(t, dict)]
    template_slugs = [str(t.get("slug") or t.get("id") or "") for t in templates if t.get("slug") or t.get("id")]

    theme_json_summary = _parse_theme_json_summary(raw_files) if raw_files else {
        "present": False,
        "version": None,
        "palette": [],
        "font_families": [],
    }

    woo_ver = (platform_rules.get("versions") or {}).get("woocommerce") or None
    if raw_files:
        woo_audit = audit_overrides(raw_files, woo_version=woo_ver)
        code_idx = index_sources(raw_files, max_results=50)
        woo_overrides = {
            "total_overrides": woo_audit["total_overrides"],
            "outdated": woo_audit["outdated"],
            "current": woo_audit["current"],
            "critical_overrides": woo_audit["critical_overrides"],
            "items": woo_audit["items"][:30],
        }
        code_summary = {
            "total_files": code_idx["total_files"],
            "total_symbols": code_idx["total_symbols"],
            "counts_by_kind": code_idx["counts_by_kind"],
            "hooks_sample": [
                e["name"] for e in code_idx["entries"] if e.get("kind") == "hook"
            ][:15],
        }
    else:
        woo_overrides = {
            "total_overrides": 0,
            "outdated": 0,
            "current": 0,
            "critical_overrides": [],
            "items": [],
        }
        code_summary = {
            "total_files": 0,
            "total_symbols": 0,
            "counts_by_kind": {},
            "hooks_sample": [],
        }

    has_block_signals = bool(templates) or bool(theme_json_summary.get("present")) or any(
        p.startswith("templates/") and p.endswith(".html") for p in raw_files
    )
    has_classic_signals = any(
        (p.endswith(".php") and p != "functions.php") or p.startswith("woocommerce/") for p in raw_files
    )
    if not has_block_signals and not has_classic_signals and active_theme:
        if active_theme.startswith("twentytwenty"):
            has_block_signals = True
        else:
            has_classic_signals = True

    if has_block_signals and has_classic_signals:
        theme_kind = "hybrid"
    elif has_block_signals:
        theme_kind = "block"
    elif has_classic_signals:
        theme_kind = "classic"
    else:
        theme_kind = "unknown"

    inv = bridge.get("plugin_inventory") or {}
    mu_plugins = [
        {
            "slug": str(mu.get("slug") or ""),
            "name": str(mu.get("name") or ""),
            "version": str(mu.get("version") or ""),
            "is_loader": bool(mu.get("is_loader", False)),
        }
        for mu in (inv.get("mu_plugins") or [])
        if isinstance(mu, dict)
    ]

    return {
        "active_theme": active_theme,
        "active_theme_name": active_name,
        "active_theme_version": active_version,
        "parent_theme": parent_theme or None,
        "is_child_theme": bool(parent_theme),
        "theme_kind": theme_kind,
        "site_editor_templates": {
            "count": len(templates),
            "slugs": template_slugs[:30],
        },
        "theme_json": theme_json_summary,
        "woo_overrides": woo_overrides,
        "code_summary": code_summary,
        "sftp_source": sftp_source,
        "mu_plugins": mu_plugins,
    }


def _analyze_brand_voice_copy(signals: dict[str, Any]) -> dict[str, Any]:
    wp = signals.get("wp") or {}
    woo = signals.get("woo") or {}
    wp_settings = wp.get("settings") or {}
    site_title = str(wp_settings.get("title") or (signals.get("site") or {}).get("display_name") or "").strip()
    site_tagline = str(wp_settings.get("description") or "").strip()
    configured_lang = str(wp_settings.get("language") or "").strip().lower()

    texts: list[str] = []
    raw_html_samples: list[str] = []
    for item in (wp.get("pages") or []) + (wp.get("posts") or []):
        if isinstance(item, dict):
            title = str(item.get("title") or "")
            raw_content = str(item.get("content") or "")
            raw_html_samples.append(raw_content)
            texts.append(f"{title}. {_strip_html(raw_content)}")
    for prod in woo.get("products") or []:
        if isinstance(prod, dict):
            desc = str(prod.get("description") or "")
            short_d = str(prod.get("short_description") or "")
            raw_html_samples.append(desc + " " + short_d)
            texts.append(f"{prod.get('name') or ''}. {_strip_html(short_d)} {_strip_html(desc)}")

    combined = " ".join(t for t in texts if t).strip()
    lang_scores: dict[str, int] = {}
    for code, pat in _LANG_MARKERS.items():
        hits = len(pat.findall(combined))
        if hits > 0:
            lang_scores[code] = hits
    languages = [code for code, _ in sorted(lang_scores.items(), key=lambda kv: (-kv[1], kv[0]))]
    if configured_lang and configured_lang[:2] in {"sv", "nb", "no", "da", "en"}:
        norm_lang = "nb" if configured_lang.startswith(("nb", "no")) else configured_lang[:2]
        if norm_lang not in languages:
            languages.append(norm_lang)

    du_count = len(_DU_RE.findall(combined))
    ni_count = len(_NI_RE.findall(combined))
    if du_count > 0 and ni_count == 0:
        formality = "du"
    elif ni_count > 0 and du_count == 0:
        formality = "ni"
    elif du_count > 0 and ni_count > 0:
        formality = "du" if du_count >= ni_count * 2 else ("ni" if ni_count >= du_count * 2 else "mixed")
    else:
        formality = "neutral"

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(combined) if len(s.strip().split()) >= 2]
    avg_sentence_words = (
        round(sum(len(s.split()) for s in sentences) / len(sentences), 1) if sentences else 0.0
    )
    joined_html = " ".join(raw_html_samples)
    uses_bullets = "<li" in joined_html.lower() or "<ul" in joined_html.lower()
    has_headings = bool(re.search(r"<h[1-4]\b", joined_html, re.I))

    usp_phrases = [label for label, pat in _USP_PATTERNS if pat.search(combined)]
    cta_vocab = [label for label, pat in _CTA_PATTERNS if pat.search(combined)]

    return {
        "site_title": site_title,
        "site_tagline": site_tagline,
        "languages": languages,
        "formality": formality,
        "formality_counts": {"du": du_count, "ni": ni_count},
        "copy_structure": {
            "sample_count": len(texts),
            "avg_sentence_words": avg_sentence_words,
            "uses_bullets": uses_bullets,
            "has_headings": has_headings,
        },
        "usp_phrases": usp_phrases,
        "cta_vocabulary": cta_vocab,
    }


def _extract_policy_rules(text: str) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    ret_m = _RETURN_DAYS_RE.search(text)
    if ret_m:
        rules["return_days"] = int(ret_m.group(1))
    with_m = _WITHDRAWAL_DAYS_RE.search(text)
    if with_m:
        rules["withdrawal_days"] = int(with_m.group(1))
    war_m = _WARRANTY_YEARS_RE.search(text)
    if war_m:
        rules["warranty_years"] = int(war_m.group(1) or war_m.group(2))
    del_m = _DELIVERY_WINDOW_RE.search(text)
    if del_m:
        rules["delivery_window"] = _WHITESPACE_RE.sub(" ", del_m.group(1)).strip()
    fs_m = _FREE_SHIPPING_TEXT_RE.search(text)
    if fs_m:
        digits = fs_m.group(1).replace(" ", "")
        if digits.isdigit():
            rules["free_shipping_threshold"] = int(digits)
    return rules


def _analyze_policies_legal(signals: dict[str, Any]) -> dict[str, Any]:
    wp = signals.get("wp") or {}
    pages = [p for p in (wp.get("pages") or []) if isinstance(p, dict)]

    matched_pages: list[dict[str, Any]] = []
    found_categories: set[str] = set()
    summary_rules: dict[str, Any] = {}

    for page in pages:
        slug = str(page.get("slug") or "").lower()
        title = str(page.get("title") or "")
        plain = _strip_html(str(page.get("content") or ""))
        haystack = f"{slug} {title}".strip()

        cats: list[str] = []
        for cat, pat in _POLICY_MATCHERS:
            if pat.search(haystack):
                cats.append(cat)
        if not cats:
            continue

        for cat in cats:
            found_categories.add(cat)
        extracted = _extract_policy_rules(plain)
        for k, v in extracted.items():
            summary_rules.setdefault(k, v)

        content_hash = canonical_sha256({"slug": slug, "title": title, "text": plain})
        matched_pages.append(
            {
                "category": cats[0],
                "categories": cats,
                "page_id": page.get("id"),
                "slug": slug,
                "title": title,
                "status": str(page.get("status") or "publish"),
                "content_hash": content_hash,
                "extracted_rules": extracted,
            }
        )

    required_core = ["retur", "frakt", "kopvillkor", "integritet", "garanti"]
    missing: list[str] = []
    for req in required_core:
        if req == "retur" and ("retur" in found_categories or "angerratt" in found_categories):
            continue
        if req == "garanti" and (
            "garanti" in found_categories or "warranty_years" in summary_rules
        ):
            continue
        if req not in found_categories:
            missing.append(req)

    return {
        "policy_pages": matched_pages,
        "found_categories": sorted(found_categories),
        "missing_policies": missing,
        "extracted_rules": summary_rules,
    }


def _classify_plugin(slug: str, name: str) -> str:
    text = f"{slug} {name}".lower()
    if slug.startswith("azom-") or "azom" in slug:
        return "custom_azom"
    if any(k in text for k in ("klarna", "vipps", "stripe", "svea", "paypal", "payment")):
        return "payment"
    if any(k in text for k in ("postnord", "bring", "dhl", "shipping", "shipment-tracking", "frakt")):
        return "shipping"
    if any(k in text for k in ("yoast", "wordpress-seo", "rank-math", "aioseo", "seopress", "seo")):
        return "seo"
    if any(k in text for k in ("litespeed", "wp-rocket", "cache", "redis", "autoptimize")):
        return "cache"
    if any(k in text for k in ("wordfence", "ithemes", "solid", "security", "hardening", "akismet")):
        return "security"
    if any(k in text for k in ("elementor", "divi", "bricks", "wpbakery", "oxygen")):
        return "builder"
    if "woocommerce" in text:
        return "ecommerce"
    return "other"


def _analyze_plugins_ecosystem(signals: dict[str, Any]) -> dict[str, Any]:
    wp = signals.get("wp") or {}
    woo = signals.get("woo") or {}
    bridge = signals.get("bridge") or {}

    merged: dict[str, dict[str, Any]] = {}
    inv = bridge.get("plugin_inventory") or {}
    for raw in inv.get("plugins") or []:
        if not isinstance(raw, dict):
            continue
        pfile = str(raw.get("plugin_file") or raw.get("slug") or "")
        slug = str(raw.get("slug") or pfile.split("/", 1)[0]).lower()
        merged[slug] = {
            "slug": slug,
            "plugin_file": pfile,
            "name": str(raw.get("name") or slug),
            "version": str(raw.get("version") or ""),
            "active": bool(raw.get("active", False)),
            "auto_update": bool(raw.get("auto_update", False)),
            "update_available": bool(raw.get("update_available", False)),
            "is_critical": bool(raw.get("is_critical", False)),
            "is_bridge": bool(raw.get("is_bridge", False)),
        }

    if not merged:
        for raw in wp.get("plugins") or []:
            if not isinstance(raw, dict):
                continue
            pfile = str(raw.get("plugin") or "")
            slug = pfile.split("/", 1)[0].lower()
            if not slug:
                continue
            merged[slug] = {
                "slug": slug,
                "plugin_file": pfile,
                "name": str(raw.get("name") or slug),
                "version": str(raw.get("version") or ""),
                "active": str(raw.get("status") or "").lower() == "active",
                "auto_update": False,
                "update_available": False,
                "is_critical": any(k in slug for k in _CRITICAL_PLUGIN_KEYWORDS),
                "is_bridge": "azom-ops-bridge" in slug,
            }

    groups: dict[str, list[str]] = {
        "payment": [],
        "shipping": [],
        "seo": [],
        "cache": [],
        "security": [],
        "builder": [],
        "custom_azom": [],
        "ecommerce": [],
        "other": [],
    }
    plugins_list: list[dict[str, Any]] = []
    custom_azom: list[dict[str, Any]] = []
    active_count = 0
    inactive_count = 0

    for slug in sorted(merged):
        row = merged[slug]
        grp = _classify_plugin(slug, row["name"])
        row["group"] = grp
        groups[grp].append(slug)
        if row["active"]:
            active_count += 1
        else:
            inactive_count += 1
        if grp == "custom_azom":
            custom_azom.append(row)
        plugins_list.append(row)

    return {
        "total_plugins": len(plugins_list),
        "active_count": active_count,
        "inactive_count": inactive_count,
        "plugins": plugins_list,
        "groups": groups,
        "custom_azom_plugins": custom_azom,
        "rest_namespaces": [str(ns) for ns in (wp.get("namespaces") or [])],
        "webhooks": list(woo.get("webhooks") or []),
    }


def _make_finding(
    *,
    run_id: str,
    site_id: str,
    severity: str,
    category: str,
    code: str,
    title: str,
    detail: str,
    recommendation: str,
    evidence: dict[str, Any] | None = None,
) -> SecurityFinding:
    return SecurityFinding(
        finding_id=new_finding_id(),
        run_id=run_id,
        site_id=site_id,
        severity=severity,
        category=category,
        code=code,
        title=scrub_text(title, max_chars=200),
        detail=scrub_text(detail, max_chars=1000),
        recommendation=scrub_text(recommendation, max_chars=500),
        evidence=scrub_payload(evidence or {}),
    )


def _make_diff(
    *,
    site_id: str,
    prev_run_id: str,
    curr_run_id: str,
    severity: str,
    domain: str,
    category: str,
    summary: str,
    changes: list[dict[str, Any]],
) -> IntelDiff:
    return IntelDiff(
        diff_id=new_diff_id(),
        site_id=site_id,
        prev_run_id=prev_run_id,
        curr_run_id=curr_run_id,
        severity=severity,
        domain=domain,
        category=category,
        summary=scrub_text(summary, max_chars=400),
        changes=scrub_payload(changes),
    )


def compute_drift_and_security(
    *,
    run_id: str,
    site_id: str,
    dossier: dict[str, Any],
    signals: dict[str, Any],
    prev_run: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[SecurityFinding], list[IntelDiff]]:
    """Evaluate static security posture + cross-run drift alerts."""
    findings: list[SecurityFinding] = []
    diffs: list[IntelDiff] = []

    plugins_dom = dossier.get("plugins_ecosystem") or {}
    theme_dom = dossier.get("theme_architecture") or {}
    policies_dom = dossier.get("policies_legal") or {}
    catalog_dom = dossier.get("catalog_pricing") or {}
    platform_dom = dossier.get("platform_rules") or {}
    bridge_ov = (signals.get("bridge") or {}).get("seo_overview") or {}

    # --- Static posture checks ---
    inactive_plugins = [p for p in (plugins_dom.get("plugins") or []) if not p.get("active")]
    if inactive_plugins:
        sev = "medium" if len(inactive_plugins) >= 3 else "low"
        findings.append(
            _make_finding(
                run_id=run_id,
                site_id=site_id,
                severity=sev,
                category="plugins",
                code="INACTIVE_PLUGINS_PRESENT",
                title=f"{len(inactive_plugins)} inaktiva plugins installerade",
                detail="Inaktiva plugins ökar attackytan om de inte uppdateras eller städas bort.",
                recommendation="Avinstallera inaktiva plugins som inte längre används.",
                evidence={"slugs": [p.get("slug") for p in inactive_plugins[:20]]},
            )
        )

    woo_ov = theme_dom.get("woo_overrides") or {}
    outdated_count = int(woo_ov.get("outdated") or 0)
    if outdated_count > 0:
        critical_outdated = [
            it.get("path")
            for it in (woo_ov.get("items") or [])
            if it.get("status") == "outdated" and it.get("critical")
        ]
        sev = "high" if critical_outdated else "medium"
        findings.append(
            _make_finding(
                run_id=run_id,
                site_id=site_id,
                severity=sev,
                category="theme",
                code="STALE_WOO_OVERRIDES",
                title=f"{outdated_count} föråldrade WooCommerce-template-overrides",
                detail="Föråldrade WooCommerce-mallar kan orsaka kassa- eller säkerhetsfel efter Woo-uppdatering.",
                recommendation="Jämför och uppdatera markerade WooCommerce-overrides i barntemat.",
                evidence={"outdated": outdated_count, "critical": critical_outdated[:10]},
            )
        )

    blog_pub = bridge_ov.get("blog_public")
    if blog_pub is False or blog_pub == 0:
        findings.append(
            _make_finding(
                run_id=run_id,
                site_id=site_id,
                severity="high",
                category="indexing",
                code="BLOG_PUBLIC_DISABLED",
                title="Sökmotorindexering blockerad (blog_public=0)",
                detail="WordPress inställning 'Avråd sökmotorer från att indexera' är aktiv.",
                recommendation="Verifiera om sajten är live och aktivera indexering om detta är produktion.",
                evidence={"blog_public": blog_pub},
            )
        )

    missing_policies = list(policies_dom.get("missing_policies") or [])
    if missing_policies and "policies_legal" in dossier:
        high_legal = [p for p in missing_policies if p in {"kopvillkor", "integritet"}]
        sev = "medium" if high_legal else "low"
        findings.append(
            _make_finding(
                run_id=run_id,
                site_id=site_id,
                severity=sev,
                category="compliance",
                code="MISSING_LEGAL_PAGES",
                title=f"Saknade policy-/villkorssidor: {', '.join(missing_policies)}",
                detail="Viktiga nordiska e-handelsvillkor eller policysidor hittades inte bland publicerade sidor.",
                recommendation="Publicera och länka saknade villkors- och policysidor i sidfoten.",
                evidence={"missing": missing_policies},
            )
        )

    zero_prods = list(catalog_dom.get("zero_price_products") or [])
    if zero_prods:
        findings.append(
            _make_finding(
                run_id=run_id,
                site_id=site_id,
                severity="high",
                category="catalog",
                code="ZERO_PRICE_PRODUCTS",
                title=f"{len(zero_price_products_list := zero_prods)} publicerade produkter saknar positivt pris",
                detail="Publicerade produkter med pris <= 0 kan leda till felbeställningar eller avvisade flöden.",
                recommendation="Granska och sätt korrekt regular_price på berörda produkter.",
                evidence={"products": zero_price_products_list[:10]},
            )
        )

    # --- Cross-run drift alerts ---
    prev_dossier = (prev_run or {}).get("dossier") if isinstance(prev_run, dict) else None
    prev_run_id = str((prev_run or {}).get("run_id") or "")
    if isinstance(prev_dossier, dict) and prev_run_id:
        # 1. Plugins drift
        prev_plugins = {
            str(p.get("slug")): p
            for p in ((prev_dossier.get("plugins_ecosystem") or {}).get("plugins") or [])
            if isinstance(p, dict) and p.get("slug")
        }
        curr_plugins = {
            str(p.get("slug")): p
            for p in (plugins_dom.get("plugins") or [])
            if isinstance(p, dict) and p.get("slug")
        }
        if prev_plugins and curr_plugins:
            plugin_changes: list[dict[str, Any]] = []
            max_sev = "medium"
            for slug in sorted(set(prev_plugins) | set(curr_plugins)):
                old = prev_plugins.get(slug)
                new = curr_plugins.get(slug)
                is_crit = any(k in slug for k in _CRITICAL_PLUGIN_KEYWORDS)
                if old is None and new is not None:
                    plugin_changes.append({"slug": slug, "change": "added", "version": new.get("version")})
                    if is_crit:
                        max_sev = "high"
                elif new is None and old is not None:
                    plugin_changes.append({"slug": slug, "change": "removed", "version": old.get("version")})
                    if is_crit:
                        max_sev = "high"
                elif old is not None and new is not None:
                    if bool(old.get("active")) != bool(new.get("active")):
                        plugin_changes.append(
                            {
                                "slug": slug,
                                "change": "toggled",
                                "from_active": bool(old.get("active")),
                                "to_active": bool(new.get("active")),
                            }
                        )
                        if is_crit:
                            max_sev = "high"
                    if str(old.get("version") or "") != str(new.get("version") or ""):
                        plugin_changes.append(
                            {
                                "slug": slug,
                                "change": "version_changed",
                                "from_version": old.get("version"),
                                "to_version": new.get("version"),
                            }
                        )
                        if is_crit:
                            max_sev = "high"
            if plugin_changes:
                diffs.append(
                    _make_diff(
                        site_id=site_id,
                        prev_run_id=prev_run_id,
                        curr_run_id=run_id,
                        severity=max_sev,
                        domain="plugins_ecosystem",
                        category="plugin_drift",
                        summary=f"Plugin-ändringar upptäckta ({len(plugin_changes)} st)",
                        changes=plugin_changes,
                    )
                )
                findings.append(
                    _make_finding(
                        run_id=run_id,
                        site_id=site_id,
                        severity=max_sev,
                        category="drift",
                        code="DRIFT_PLUGINS_CHANGED",
                        title="Plugin-landskapet ändrades sedan föregående körning",
                        detail=f"{len(plugin_changes)} plugin-ändringar (tillägg, borttagning, status eller version).",
                        recommendation="Verifiera att ändringen var planerad i Change Vault.",
                        evidence={"changes": plugin_changes[:15]},
                    )
                )

        # 2. Payment gateway drift [high]
        prev_gw = {
            str(g.get("id")): bool(g.get("enabled"))
            for g in ((prev_dossier.get("platform_rules") or {}).get("payment_gateways") or [])
            if isinstance(g, dict) and g.get("id")
        }
        curr_gw = {
            str(g.get("id")): bool(g.get("enabled"))
            for g in (platform_dom.get("payment_gateways") or [])
            if isinstance(g, dict) and g.get("id")
        }
        if prev_gw and curr_gw and prev_gw != curr_gw:
            gw_changes = [
                {
                    "gateway": gid,
                    "from": prev_gw.get(gid),
                    "to": curr_gw.get(gid),
                }
                for gid in sorted(set(prev_gw) | set(curr_gw))
                if prev_gw.get(gid) != curr_gw.get(gid)
            ]
            diffs.append(
                _make_diff(
                    site_id=site_id,
                    prev_run_id=prev_run_id,
                    curr_run_id=run_id,
                    severity="high",
                    domain="platform_rules",
                    category="payment_gateway_drift",
                    summary=f"Betalgateways ändrades ({len(gw_changes)} st)",
                    changes=gw_changes,
                )
            )
            findings.append(
                _make_finding(
                    run_id=run_id,
                    site_id=site_id,
                    severity="high",
                    category="drift",
                    code="DRIFT_PAYMENT_GATEWAY_CHANGED",
                    title="Betalmetod/gateway ändrad sedan föregående körning",
                    detail="Ändring i aktiverade betalningsgateways kan direkt påverka konvertering i kassan.",
                    recommendation="Kontrollera WooCommerce > Inställningar > Betalningar.",
                    evidence={"changes": gw_changes},
                )
            )

        # 3. Shipping rule drift [high]
        prev_ship = (prev_dossier.get("platform_rules") or {}).get("shipping_zones")
        curr_ship = platform_dom.get("shipping_zones")
        if prev_ship is not None and curr_ship is not None and canonical_sha256(prev_ship) != canonical_sha256(curr_ship):
            diffs.append(
                _make_diff(
                    site_id=site_id,
                    prev_run_id=prev_run_id,
                    curr_run_id=run_id,
                    severity="high",
                    domain="platform_rules",
                    category="shipping_rule_drift",
                    summary="Fraktzoner eller fraktmetoder ändrades sedan föregående körning",
                    changes=[
                        {
                            "prev_thresholds": (prev_dossier.get("platform_rules") or {}).get("free_shipping_thresholds"),
                            "curr_thresholds": platform_dom.get("free_shipping_thresholds"),
                        }
                    ],
                )
            )
            findings.append(
                _make_finding(
                    run_id=run_id,
                    site_id=site_id,
                    severity="high",
                    category="drift",
                    code="DRIFT_SHIPPING_RULES_CHANGED",
                    title="Fraktregler eller fri frakt-gräns ändrad",
                    detail="Fraktzoner, priser eller fri frakt-villkor skiljer sig från föregående körning.",
                    recommendation="Verifiera fraktpriser och fri frakt-gräns i WooCommerce.",
                    evidence={"curr_thresholds": platform_dom.get("free_shipping_thresholds")},
                )
            )

        # 4. Theme change [high]
        prev_theme_dom = prev_dossier.get("theme_architecture") or {}
        prev_theme_sig = (
            str(prev_theme_dom.get("active_theme") or ""),
            str(prev_theme_dom.get("active_theme_version") or ""),
        )
        curr_theme_sig = (
            str(theme_dom.get("active_theme") or ""),
            str(theme_dom.get("active_theme_version") or ""),
        )
        if prev_theme_sig[0] and curr_theme_sig[0] and prev_theme_sig != curr_theme_sig:
            theme_change = [
                {
                    "from_theme": prev_theme_sig[0],
                    "from_version": prev_theme_sig[1],
                    "to_theme": curr_theme_sig[0],
                    "to_version": curr_theme_sig[1],
                }
            ]
            diffs.append(
                _make_diff(
                    site_id=site_id,
                    prev_run_id=prev_run_id,
                    curr_run_id=run_id,
                    severity="high",
                    domain="theme_architecture",
                    category="theme_drift",
                    summary=f"Aktivt tema ändrades: {prev_theme_sig[0]} ({prev_theme_sig[1]}) -> {curr_theme_sig[0]} ({curr_theme_sig[1]})",
                    changes=theme_change,
                )
            )
            findings.append(
                _make_finding(
                    run_id=run_id,
                    site_id=site_id,
                    severity="high",
                    category="drift",
                    code="DRIFT_THEME_CHANGED",
                    title="Aktivt WordPress-tema eller version ändrad",
                    detail="Temabyte eller temauppdatering kan påverka checkout, layout och Woo-overrides.",
                    recommendation="Granska startsidan och kassan efter temaändring.",
                    evidence={"change": theme_change[0]},
                )
            )

        # 5. Policy content hash change [medium]
        prev_policies = {
            str(p.get("slug") or p.get("category")): str(p.get("content_hash") or "")
            for p in ((prev_dossier.get("policies_legal") or {}).get("policy_pages") or [])
            if isinstance(p, dict)
        }
        curr_policies = {
            str(p.get("slug") or p.get("category")): str(p.get("content_hash") or "")
            for p in (policies_dom.get("policy_pages") or [])
            if isinstance(p, dict)
        }
        if prev_policies and curr_policies and prev_policies != curr_policies:
            pol_changes = [
                {
                    "slug": slug,
                    "from_hash": prev_policies.get(slug),
                    "to_hash": curr_policies.get(slug),
                }
                for slug in sorted(set(prev_policies) | set(curr_policies))
                if prev_policies.get(slug) != curr_policies.get(slug)
            ]
            diffs.append(
                _make_diff(
                    site_id=site_id,
                    prev_run_id=prev_run_id,
                    curr_run_id=run_id,
                    severity="medium",
                    domain="policies_legal",
                    category="policy_hash_drift",
                    summary=f"Policysidor ändrades ({len(pol_changes)} st)",
                    changes=pol_changes,
                )
            )
            findings.append(
                _make_finding(
                    run_id=run_id,
                    site_id=site_id,
                    severity="medium",
                    category="drift",
                    code="DRIFT_POLICY_HASH_CHANGED",
                    title="Policy- eller villkorstext ändrad sedan föregående körning",
                    detail="Innehållshashen för en eller flera policysidor har ändrats.",
                    recommendation="Kontrollera att uppdaterade frakt-, retur- eller garantivillkor stämmer.",
                    evidence={"changes": pol_changes},
                )
            )

        # 6. >20% median price swing [medium]
        prev_median = ((prev_dossier.get("catalog_pricing") or {}).get("pricing") or {}).get("median_price")
        curr_median = (catalog_dom.get("pricing") or {}).get("median_price")
        if isinstance(prev_median, (int, float)) and isinstance(curr_median, (int, float)) and prev_median > 0:
            swing = abs(curr_median - prev_median) / prev_median
            if swing > 0.20:
                pct = round(swing * 100.0, 1)
                price_change = [
                    {
                        "prev_median_price": prev_median,
                        "curr_median_price": curr_median,
                        "swing_percent": pct,
                    }
                ]
                diffs.append(
                    _make_diff(
                        site_id=site_id,
                        prev_run_id=prev_run_id,
                        curr_run_id=run_id,
                        severity="medium",
                        domain="catalog_pricing",
                        category="price_swing_drift",
                        summary=f"Medianpriset ändrades med {pct}% ({prev_median} -> {curr_median})",
                        changes=price_change,
                    )
                )
                findings.append(
                    _make_finding(
                        run_id=run_id,
                        site_id=site_id,
                        severity="medium",
                        category="drift",
                        code="DRIFT_MEDIAN_PRICE_SWING",
                        title=f"Kraftig prisrörelse i katalogen ({pct}%)",
                        detail=f"Medianpriset rörde sig från {prev_median} till {curr_median} (>20%).",
                        recommendation="Verifiera att prisändringen eller valutakonverteringen är avsiktlig.",
                        evidence=price_change[0],
                    )
                )

    penalty = sum(_SEVERITY_PENALTY.get(f.severity, 0) for f in findings)
    security_score = max(0, min(100, 100 - penalty))

    sev_counts = Counter(f.severity for f in findings)
    posture_summary = {
        "security_score": security_score,
        "findings_count": len(findings),
        "diffs_count": len(diffs),
        "severity_counts": {sev: sev_counts.get(sev, 0) for sev in ("critical", "high", "medium", "low", "info")},
        "blog_public": bridge_ov.get("blog_public"),
        "inactive_plugins": len(inactive_plugins),
        "stale_woo_overrides": outdated_count,
        "missing_policies_count": len(missing_policies),
        "zero_price_products_count": len(zero_prods),
    }
    return posture_summary, findings, diffs


def analyze_shop_signals(
    *,
    run_id: str,
    site_id: str,
    signals: dict[str, Any],
    domains: Sequence[str],
    prev_run: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[IntelFact], list[SecurityFinding], list[IntelDiff]]:
    """Analyze collected signals into structured 7-domain dossier, facts, findings, and diffs."""
    observed_at = utc_now_iso()
    domain_set = set(domains)
    dossier: dict[str, Any] = {}
    facts: list[IntelFact] = []

    platform_rules = _analyze_platform_rules(signals)
    if "platform_rules" in domain_set:
        dossier["platform_rules"] = platform_rules
        for key in ("currency", "default_country", "free_shipping_thresholds", "enabled_gateways", "tax_classes", "versions"):
            facts.append(
                IntelFact(
                    domain="platform_rules",
                    key=key,
                    value=platform_rules.get(key),
                    source="woo_rest",
                    observed_at=observed_at,
                )
            )

    if "catalog_pricing" in domain_set:
        cat_dom = _analyze_catalog_pricing(signals)
        dossier["catalog_pricing"] = cat_dom
        for key in ("product_count", "pricing", "categories", "attributes", "stock_health", "catalog_gaps"):
            facts.append(
                IntelFact(
                    domain="catalog_pricing",
                    key=key,
                    value=cat_dom.get(key),
                    source="woo_rest",
                    observed_at=observed_at,
                )
            )

    if "theme_architecture" in domain_set:
        theme_dom = _analyze_theme_architecture(signals, platform_rules=platform_rules)
        dossier["theme_architecture"] = theme_dom
        for key in ("active_theme", "theme_kind", "site_editor_templates", "theme_json", "woo_overrides", "sftp_source", "mu_plugins"):
            facts.append(
                IntelFact(
                    domain="theme_architecture",
                    key=key,
                    value=theme_dom.get(key),
                    source="wp_sftp" if theme_dom.get("sftp_source") in {"local_tree", "sftp"} else "wp_rest",
                    observed_at=observed_at,
                )
            )

    if "brand_voice_copy" in domain_set:
        voice_dom = _analyze_brand_voice_copy(signals)
        dossier["brand_voice_copy"] = voice_dom
        for key in ("site_title", "site_tagline", "languages", "formality", "usp_phrases", "cta_vocabulary"):
            facts.append(
                IntelFact(
                    domain="brand_voice_copy",
                    key=key,
                    value=voice_dom.get(key),
                    source="wp_rest",
                    observed_at=observed_at,
                )
            )

    if "policies_legal" in domain_set:
        pol_dom = _analyze_policies_legal(signals)
        dossier["policies_legal"] = pol_dom
        for key in ("extracted_rules", "missing_policies", "policy_pages"):
            facts.append(
                IntelFact(
                    domain="policies_legal",
                    key=key,
                    value=pol_dom.get(key),
                    source="wp_rest",
                    observed_at=observed_at,
                )
            )

    if "plugins_ecosystem" in domain_set:
        plug_dom = _analyze_plugins_ecosystem(signals)
        dossier["plugins_ecosystem"] = plug_dom
        for key in ("total_plugins", "active_count", "inactive_count", "groups", "rest_namespaces", "webhooks"):
            facts.append(
                IntelFact(
                    domain="plugins_ecosystem",
                    key=key,
                    value=plug_dom.get(key),
                    source="wp_bridge",
                    observed_at=observed_at,
                )
            )

    posture_summary, findings, diffs = compute_drift_and_security(
        run_id=run_id,
        site_id=site_id,
        dossier=dossier,
        signals=signals,
        prev_run=prev_run,
    )
    if "security_posture" in domain_set:
        dossier["security_posture"] = posture_summary
        facts.append(
            IntelFact(
                domain="security_posture",
                key="posture_summary",
                value=posture_summary,
                source="analyzer",
                observed_at=observed_at,
            )
        )

    return scrub_payload(dossier), facts, findings, diffs


def render_dossier_markdown(
    run: IntelRun,
    *,
    findings: list[SecurityFinding] | None = None,
    diffs: list[IntelDiff] | None = None,
) -> str:
    """Render a concise, zero-PII Markdown dossier for ``latest.md``."""
    d = run.dossier or {}
    lines: list[str] = [
        f"# Shop Intelligence Dossier — {run.site_id}",
        "",
        f"- **Run ID:** `{run.run_id}`",
        f"- **Mode:** `{run.mode}` | **Provenance:** `{run.provenance}` | **Observed:** `{run.created_at}`",
        f"- **Security Score:** `{run.security_score}/100` | **Findings:** `{run.findings_count}` | **Diffs:** `{run.diffs_count}`",
        "",
    ]

    if "platform_rules" in d:
        pr = d["platform_rules"]
        vers = pr.get("versions") or {}
        lines.extend(
            [
                "## Platform Rules",
                f"- Currency: `{pr.get('currency')}` | Country: `{pr.get('default_country')}` | VAT incl: `{pr.get('prices_include_tax')}`",
                f"- Versions: WP `{vers.get('wordpress') or '-'}`, Woo `{vers.get('woocommerce') or '-'}`, PHP `{vers.get('php') or '-'}`",
                f"- Enabled Gateways: {', '.join(pr.get('enabled_gateways') or []) or 'none'}",
                f"- Carriers: {', '.join(pr.get('carriers') or []) or 'none'}",
                "",
            ]
        )

    if "catalog_pricing" in d:
        cp = d["catalog_pricing"]
        pr = cp.get("pricing") or {}
        lines.extend(
            [
                "## Catalog & Pricing",
                f"- Products: `{cp.get('product_count')}` (published `{cp.get('published_count')}`) | Categories: `{len(cp.get('categories') or [])}`",
                f"- Price min/median/max: `{pr.get('min_price')}` / `{pr.get('median_price')}` / `{pr.get('max_price')}`",
                "",
            ]
        )

    if "theme_architecture" in d:
        ta = d["theme_architecture"]
        lines.extend(
            [
                "## Theme Architecture",
                f"- Active Theme: `{ta.get('active_theme')}` (`{ta.get('theme_kind')}`) | SFTP source: `{ta.get('sftp_source')}`",
                f"- Woo Overrides: `{((ta.get('woo_overrides') or {}).get('total_overrides', 0))}` (outdated `{((ta.get('woo_overrides') or {}).get('outdated', 0))}`)",
                "",
            ]
        )

    if "brand_voice_copy" in d:
        bv = d["brand_voice_copy"]
        lines.extend(
            [
                "## Brand Voice & Copy",
                f"- Languages: {', '.join(bv.get('languages') or []) or '-'} | Formality: `{bv.get('formality')}`",
                f"- USPs: {', '.join(bv.get('usp_phrases') or []) or '-'}",
                "",
            ]
        )

    if "policies_legal" in d:
        pl = d["policies_legal"]
        lines.extend(
            [
                "## Policies & Legal",
                f"- Grounded Rules: `{json.dumps(pl.get('extracted_rules') or {}, ensure_ascii=False)}`",
                f"- Missing Policies: {', '.join(pl.get('missing_policies') or []) or 'none'}",
                "",
            ]
        )

    if "plugins_ecosystem" in d:
        pe = d["plugins_ecosystem"]
        lines.extend(
            [
                "## Plugins Ecosystem",
                f"- Plugins: `{pe.get('total_plugins')}` total (`{pe.get('active_count')}` active, `{pe.get('inactive_count')}` inactive)",
                "",
            ]
        )

    if findings:
        lines.append("## Security Findings")
        for f in findings[:20]:
            lines.append(f"- [{f.severity.upper()}] `{f.code}` — {f.title}")
        lines.append("")

    if diffs:
        lines.append("## Drift Alerts")
        for df in diffs[:20]:
            lines.append(f"- [{df.severity.upper()}] `{df.category}` — {df.summary}")
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "analyze_shop_signals",
    "compute_drift_and_security",
    "render_dossier_markdown",
]
