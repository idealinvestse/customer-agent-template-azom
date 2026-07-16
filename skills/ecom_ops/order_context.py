"""Resolve compact Woo order context for support drafts and Telegram chat."""

from __future__ import annotations

from typing import Any


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


def order_panel_fields(order: Any) -> dict[str, Any]:
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
    tracking = _extract_tracking(raw)
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
        "date_created": date_created,
        "payment_method": payment,
        "shipping_method": shipping,
        "customer_note": note,
        "billing_place": place,
        "ok": True,
        "error": None,
    }


def format_order_context_block(order: Any) -> str:
    """Safe fields only — never invent tracking numbers."""
    panel = order_panel_fields(order)
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
        lines.append(f"Tracking: {panel['tracking']}")
    return "\n".join(lines)


def _extract_tracking(raw: dict[str, Any]) -> str | None:
    """Fallback tracking extraction from order meta_data (legacy installs).

    V2.1: prefer ``WooCommerceClient.list_shipment_trackings`` for installs
    that expose the dedicated endpoint. This heuristic remains as a fallback
    for stores without the shipment-trackings plugin/endpoint.
    """
    meta = raw.get("meta_data") or []
    if isinstance(meta, list):
        for row in meta:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").lower()
            if "tracking" in key or key in {"_tracking_number", "tracking_number"}:
                val = str(row.get("value") or "").strip()
                if val:
                    return val[:120]
    for ship in raw.get("shipping_lines") or []:
        if not isinstance(ship, dict):
            continue
        for row in ship.get("meta_data") or []:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").lower()
            if "tracking" in key:
                val = str(row.get("value") or "").strip()
                if val:
                    return val[:120]
    return None


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
    except Exception:
        return None
    return format_order_context_block(order)


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
        panel = order_panel_fields(order)
        # P0.1: prefer dedicated shipment-trackings endpoint
        if not panel.get("tracking"):
            try:
                trackings = woo.list_shipment_trackings(str(order_id))
                if trackings:
                    panel["tracking"] = trackings[0].tracking_number
                    panel["tracking_source"] = "endpoint"
            except Exception:
                pass
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
