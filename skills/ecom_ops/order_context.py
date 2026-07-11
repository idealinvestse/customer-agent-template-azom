"""Resolve compact Woo order context for support drafts and Telegram chat."""

from __future__ import annotations

from typing import Any


def format_order_context_block(order: Any) -> str:
    """Safe fields only — never invent tracking numbers."""
    lines = [
        f"[Order {order.id}]",
        f"Status: {order.status}",
        f"Total: {order.total} {order.currency}",
    ]
    raw = getattr(order, "raw", None) or {}
    if isinstance(raw, dict):
        items = raw.get("line_items") or []
        if isinstance(items, list) and items:
            lines.append("Rader:")
            for item in items[:5]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "artikel").strip()
                qty = item.get("quantity", 1)
                lines.append(f"- {qty}× {name}")
        # Only surface tracking when Woo actually provides it
        tracking = _extract_tracking(raw)
        if tracking:
            lines.append(f"Tracking: {tracking}")
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


def draft_has_order_block(draft: str | None, order_id: str | None = None) -> bool:
    """True when draft already includes an order context prepend block."""
    text = (draft or "").strip()
    if not text:
        return False
    if order_id and f"[Order {order_id}]" in text:
        return True
    return text.startswith("[Order ")
