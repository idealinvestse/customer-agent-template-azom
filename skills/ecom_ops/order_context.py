"""Resolve compact Woo order context for support drafts and Telegram chat."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_WOO_DOMAIN_MARKETS = frozenset({"se", "no", "dk", "fi"})


def woo_domain_from_market(market: str | None) -> str | None:
    """Map case/mailbox market code to Woo ``domain=`` (se/no/dk/fi)."""
    code = (market or "").strip().lower()
    if not code:
        return None
    if code in _WOO_DOMAIN_MARKETS:
        return code
    # Accept azom.se style values
    for suffix in _WOO_DOMAIN_MARKETS:
        if code.endswith(f".{suffix}") or code == suffix:
            return suffix
    return None


def _truncate(value: str | None, *, max_len: int = 200) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _shipping_method(raw: dict[str, Any]) -> str | None:
    ships = raw.get("shipping_lines") or []
    if not isinstance(ships, list):
        return None
    names: list[str] = []
    for ship in ships[:3]:
        if not isinstance(ship, dict):
            continue
        method = str(ship.get("method_title") or ship.get("method_id") or "").strip()
        if method:
            names.append(method[:80])
    return ", ".join(names) if names else None


def _billing_place(raw: dict[str, Any]) -> str | None:
    """City + country only — avoid full street/PII dump in panel/drafts."""
    billing = raw.get("billing") or {}
    if not isinstance(billing, dict):
        return None
    city = str(billing.get("city") or "").strip()
    country = str(billing.get("country") or "").strip()
    parts = [p for p in (city, country) if p]
    return ", ".join(parts) if parts else None


def order_panel_fields(order: Any, *, domain: str | None = None) -> dict[str, Any]:
    """Structured safe fields for dashboard order panel (never invent tracking)."""
    raw = getattr(order, "raw", None) or {}
    if not isinstance(raw, dict):
        raw = {}
    lines: list[dict[str, Any]] = []
    items = raw.get("line_items") or []
    if isinstance(items, list):
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(
                {
                    "name": str(item.get("name") or "artikel").strip(),
                    "quantity": item.get("quantity", 1),
                }
            )
    trackings = _extract_trackings_list(raw)
    tracking = trackings[0] if trackings else None
    date_created = _truncate(str(raw.get("date_created") or "") or None, max_len=32)
    payment = _truncate(
        str(raw.get("payment_method_title") or raw.get("payment_method") or "")
        or None,
        max_len=80,
    )
    shipping = _shipping_method(raw)
    note = _truncate(str(raw.get("customer_note") or "") or None, max_len=200)
    place = _billing_place(raw)
    return {
        "order_id": str(getattr(order, "id", "") or ""),
        "status": str(getattr(order, "status", "") or ""),
        "total": str(getattr(order, "total", "") or ""),
        "currency": str(getattr(order, "currency", "") or ""),
        "line_items": lines,
        "tracking": tracking,
        "trackings": trackings,
        "tracking_link": _extract_tracking_link(raw),
        "domain": woo_domain_from_market(domain),
        "date_created": date_created,
        "payment_method": payment,
        "shipping_method": shipping,
        "customer_note": note,
        "billing_place": place,
        "ok": True,
        "error": None,
    }


def _order_status_link(order_id: str, domain: str | None = None) -> str:
    dom = woo_domain_from_market(domain)
    if dom:
        return f"https://azom.{dom}/order-status/?order_id={order_id}"
    from ecom_ops.profile import load_profile

    return load_profile().order_status_url(order_id)


def format_order_context_block(order: Any, *, domain: str | None = None) -> str:
    """Safe fields only — never invent tracking numbers."""
    panel = order_panel_fields(order, domain=domain)
    lines = [
        f"[Order {panel['order_id']}]",
        f"Status: {panel['status']}",
        f"Total: {panel['total']} {panel['currency']}",
    ]
    if panel.get("date_created"):
        lines.append(f"Skapad: {panel['date_created']}")
    if panel.get("payment_method"):
        lines.append(f"Betalning: {panel['payment_method']}")
    if panel.get("shipping_method"):
        lines.append(f"Frakt: {panel['shipping_method']}")
    if panel.get("billing_place"):
        lines.append(f"Leveransort: {panel['billing_place']}")
    if panel.get("customer_note"):
        lines.append(f"Kundnotering: {panel['customer_note']}")
    if panel["line_items"]:
        lines.append("Rader:")
        for item in panel["line_items"]:
            lines.append(f"- {item['quantity']}× {item['name']}")
    if panel.get("tracking"):
        if len(panel.get("trackings") or []) > 1:
            lines.append(f"Tracking: {', '.join(panel['trackings'])}")
        else:
            lines.append(f"Tracking: {panel['tracking']}")
    # Self-service link (P1.4): include order tracking page URL if available
    tracking_link = panel.get("tracking_link")
    if tracking_link:
        lines.append(f"Spårning: {tracking_link}")
    elif panel.get("order_id"):
        lines.append(
            f"Se orderstatus: {_order_status_link(str(panel['order_id']), domain=panel.get('domain'))}"
        )
    return "\n".join(lines)


_ANALYTICS_META_PREFIXES = (
    "_ga",
    "ga_",
    "_fb",
    "fb_",
    "_pys",
    "pys_",
    "utm_",
    "_utm",
    "_pixel",
    "pixel_",
    "_analytics",
    "analytics_",
    "_wc_order_attribution",
)
_TRACKING_META_KEYS = frozenset(
    {
        "_wc_shipment_tracking_items",
        "wc_shipment_tracking_items",
        "_tracking_number",
        "tracking_number",
        "_tracking_numbers",
        "tracking_numbers",
        "_shipment_tracking",
        "shipment_tracking",
        "_aftership_tracking_number",
        "_postnord_tracking_number",
        "postnord_tracking_number",
        "_bring_tracking_number",
        "bring_tracking_number",
        "_dhl_tracking_number",
        "dhl_tracking_number",
        "tracking_id",
        "_tracking_id",
        "tracking",
    }
)


def _is_tracking_meta_key(key: str) -> bool:
    clean = str(key or "").strip().lower()
    if not clean or any(clean.startswith(pref) for pref in _ANALYTICS_META_PREFIXES):
        return False
    if clean in _TRACKING_META_KEYS:
        return True
    if "tracking_link" in clean or "tracking_url" in clean:
        return False
    return clean.endswith(("_tracking_number", "tracking_number", "_tracking_code", "tracking_code"))


def _extract_from_php_serialized(text: str, field: str) -> list[str]:
    import re

    pat = re.compile(rf's:\d+:"{re.escape(field)}";s:\d+:"([^"]+)"')
    return [m.group(1).strip() for m in pat.finditer(text) if m.group(1).strip()]


def _extract_tracking_link(raw: dict[str, Any]) -> str | None:
    """Extract a tracking URL from order meta_data or nested shipment items (P1.4)."""
    meta = raw.get("meta_data") or []
    if not isinstance(meta, list):
        return None
    for row in meta:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").lower()
        val_obj = row.get("value")
        if "tracking_link" in key or "tracking_url" in key or key == "_tracking_link":
            val = str(val_obj or "").strip()
            if val.startswith("http"):
                return val
        if _is_tracking_meta_key(key):
            if isinstance(val_obj, dict):
                for lk in ("tracking_link", "custom_tracking_link", "tracking_url"):
                    cand = str(val_obj.get(lk) or "").strip()
                    if cand.startswith("http"):
                        return cand
            elif isinstance(val_obj, list):
                for item in val_obj:
                    if isinstance(item, dict):
                        for lk in ("tracking_link", "custom_tracking_link", "tracking_url"):
                            cand = str(item.get(lk) or "").strip()
                            if cand.startswith("http"):
                                return cand
            elif isinstance(val_obj, str) and val_obj.lstrip().startswith(("a:", "O:", "s:")):
                for lk in ("tracking_link", "custom_tracking_link", "tracking_url"):
                    found = _extract_from_php_serialized(val_obj, lk)
                    for cand in found:
                        if cand.startswith("http"):
                            return cand
    return None


def _tracking_values_from_meta(value: Any) -> list[str]:
    """Extract clean tracking strings from scalar, list, dict, or PHP-serialized meta."""
    out: list[str] = []
    if isinstance(value, str):
        clean = value.strip()
        if not clean:
            return []
        if clean.lstrip().startswith(("a:", "O:", "s:")):
            return [t[:120] for t in _extract_from_php_serialized(clean, "tracking_number")]
        if clean.startswith("{") and len(clean) > 120:
            return []
        return [clean[:120]]
    if isinstance(value, dict):
        num = str(value.get("tracking_number") or value.get("tracking") or "").strip()
        return [num[:120]] if num else []
    if isinstance(value, list):
        for item in value:
            out.extend(_tracking_values_from_meta(item))
        return out
    if value is not None:
        clean = str(value).strip()
        return [clean[:120]] if clean else []
    return []


def _tracking_value_from_meta(value: Any) -> str | None:
    vals = _tracking_values_from_meta(value)
    return vals[0] if vals else None


def _extract_trackings_list(raw: dict[str, Any]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def _add(vals: list[str]) -> None:
        for v in vals:
            if v and v not in seen:
                seen.add(v)
                found.append(v)

    meta = raw.get("meta_data") or []
    if isinstance(meta, list):
        for row in meta:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").lower()
            if _is_tracking_meta_key(key):
                _add(_tracking_values_from_meta(row.get("value")))
    for ship in raw.get("shipping_lines") or []:
        if not isinstance(ship, dict):
            continue
        for row in ship.get("meta_data") or []:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").lower()
            if _is_tracking_meta_key(key):
                _add(_tracking_values_from_meta(row.get("value")))
    return found


def _extract_tracking(raw: dict[str, Any]) -> str | None:
    """Fallback tracking extraction from order meta_data (legacy installs)."""
    vals = _extract_trackings_list(raw)
    return vals[0] if vals else None


def _attach_endpoint_tracking(woo: Any, order_id: str, panel: dict[str, Any]) -> None:
    """Prefer dedicated ``/shipment-trackings`` endpoint; keep meta_data fallback."""
    try:
        trackings = woo.list_shipment_trackings(str(order_id))
    except Exception as exc:
        logger.debug(
            "shipment-trackings endpoint failed for order %s: %s "
            "(falling back to meta_data if available)",
            order_id,
            exc,
        )
        return
    if not trackings:
        return
    nums = [t.tracking_number for t in trackings if getattr(t, "tracking_number", None)]
    if nums:
        panel["tracking"] = nums[0]
        panel["trackings"] = nums
        panel["tracking_source"] = "endpoint"
    for item in trackings:
        if getattr(item, "link", None) and not panel.get("tracking_link"):
            panel["tracking_link"] = item.link
            break


def resolve_order_context(
    order_id: str | None,
    *,
    use_mock: bool | None = None,
    domain: str | None = None,
) -> str | None:
    """Fetch Woo order and return a compact context block, or None on miss/error.

    V2.1: ``domain`` enables multi-site per-call resolution (.no/.se/.dk).
    """
    if not order_id:
        return None
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=use_mock, domain=domain)
        order = woo.get_order(str(order_id))
        panel = order_panel_fields(order, domain=domain)
        _attach_endpoint_tracking(woo, str(order_id), panel)
    except Exception:
        return None
    lines = [
        f"[Order {panel['order_id']}]",
        f"Status: {panel['status']}",
        f"Total: {panel['total']} {panel['currency']}",
    ]
    if panel.get("date_created"):
        lines.append(f"Skapad: {panel['date_created']}")
    if panel.get("payment_method"):
        lines.append(f"Betalning: {panel['payment_method']}")
    if panel.get("shipping_method"):
        lines.append(f"Frakt: {panel['shipping_method']}")
    if panel.get("billing_place"):
        lines.append(f"Leveransort: {panel['billing_place']}")
    if panel.get("customer_note"):
        lines.append(f"Kundnotering: {panel['customer_note']}")
    if panel["line_items"]:
        lines.append("Rader:")
        for item in panel["line_items"]:
            lines.append(f"- {item['quantity']}× {item['name']}")
    if panel.get("tracking"):
        if len(panel.get("trackings") or []) > 1:
            lines.append(f"Tracking: {', '.join(panel['trackings'])}")
        else:
            lines.append(f"Tracking: {panel['tracking']}")
    tracking_link = panel.get("tracking_link")
    if tracking_link:
        lines.append(f"Spårning: {tracking_link}")
    elif panel.get("order_id"):
        lines.append(
            f"Se orderstatus: {_order_status_link(str(panel['order_id']), domain=panel.get('domain'))}"
        )
    return "\n".join(lines)


def resolve_order_panel(
    order_id: str | None,
    *,
    use_mock: bool | None = None,
    domain: str | None = None,
) -> dict[str, Any] | None:
    """Fetch Woo order as structured panel dict, or error stub on miss.

    V2.1: ``domain`` enables multi-site per-call resolution (.no/.se/.dk).
    Tracking is resolved via the dedicated ``/shipment-trackings`` endpoint
    first, with meta_data heuristics as fallback for older installs.
    """
    if not order_id:
        return None
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=use_mock, domain=domain)
        order = woo.get_order(str(order_id))
        panel = order_panel_fields(order, domain=domain)
        _attach_endpoint_tracking(woo, str(order_id), panel)
        return panel
    except Exception as exc:
        return {
            "order_id": str(order_id),
            "status": "",
            "total": "",
            "currency": "",
            "line_items": [],
            "tracking": None,
            "date_created": None,
            "payment_method": None,
            "shipping_method": None,
            "customer_note": None,
            "billing_place": None,
            "ok": False,
            "error": str(exc)[:160],
        }


def resolve_order_id_from_email(
    email: str | None,
    *,
    use_mock: bool | None = None,
    domain: str | None = None,
) -> str | None:
    """Return order id only when email maps to exactly one recent Woo order.

    Multiple matches → None (never guess for suggest-approve).
    V2.1: ``domain`` enables multi-site per-call resolution.
    """
    if not email or "@" not in str(email):
        return None
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=use_mock, domain=domain)
        found = woo.find_orders_by_email(str(email).strip(), per_page=5)
    except Exception:
        return None
    if len(found) == 1:
        return str(found[0].id)
    return None


def draft_has_order_block(draft: str | None, order_id: str | None = None) -> bool:
    """True when draft already includes an order context prepend block."""
    text = (draft or "").strip()
    if not text:
        return False
    if order_id and f"[Order {order_id}]" in text:
        return True
    return text.startswith("[Order ")
