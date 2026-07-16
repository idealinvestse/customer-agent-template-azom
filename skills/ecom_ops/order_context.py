"""Resolve compact Woo order context for support drafts and Telegram chat."""

from __future__ import annotations

from typing import Any


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
    return {
        "order_id": str(getattr(order, "id", "") or ""),
        "status": str(getattr(order, "status", "") or ""),
        "total": str(getattr(order, "total", "") or ""),
        "currency": str(getattr(order, "currency", "") or ""),
        "line_items": lines,
        "tracking": tracking,
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
    if panel["line_items"]:
        lines.append("Rader:")
        for item in panel["line_items"]:
            lines.append(f"- {item['quantity']}× {item['name']}")
    if panel.get("tracking"):
        lines.append(f"Tracking: {panel['tracking']}")
    return "\n".join(lines)


def _extract_tracking(raw: dict[str, Any]) -> str | None:
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
) -> str | None:
    """Fetch Woo order and return a compact context block, or None on miss/error."""
    if not order_id:
        return None
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=use_mock)
        order = woo.get_order(str(order_id))
    except Exception:
        return None
    return format_order_context_block(order)


def resolve_order_panel(
    order_id: str | None,
    *,
    use_mock: bool | None = None,
) -> dict[str, Any] | None:
    """Fetch Woo order as structured panel dict, or error stub on miss."""
    if not order_id:
        return None
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=use_mock)
        order = woo.get_order(str(order_id))
        return order_panel_fields(order)
    except Exception as exc:
        return {
            "order_id": str(order_id),
            "status": "",
            "total": "",
            "currency": "",
            "line_items": [],
            "tracking": None,
            "ok": False,
            "error": str(exc)[:160],
        }


def resolve_order_id_from_email(
    email: str | None,
    *,
    use_mock: bool | None = None,
) -> str | None:
    """Return order id only when email maps to exactly one recent Woo order.

    Multiple matches → None (never guess for suggest-approve).
    """
    if not email or "@" not in str(email):
        return None
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        woo = client_from_env(use_mock=use_mock)
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
