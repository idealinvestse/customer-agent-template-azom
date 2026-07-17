"""SB3: richer safe order_context panel + text block."""

from __future__ import annotations

from types import SimpleNamespace

from ecom_ops.order_context import (
    format_order_context_block,
    order_panel_fields,
    resolve_order_panel,
)


def test_panel_includes_payment_shipping_note_place():
    order = SimpleNamespace(
        id="1001",
        status="processing",
        total="499.00",
        currency="SEK",
        raw={
            "date_created": "2026-07-10T12:00:00",
            "payment_method_title": "Klarna",
            "customer_note": "Ring på dörren",
            "billing": {"city": "Stockholm", "country": "SE", "address_1": "SECRET ST 1"},
            "shipping_lines": [{"method_title": "PostNord"}],
            "line_items": [{"name": "Headset", "quantity": 1}],
            "meta_data": [{"key": "tracking_number", "value": "JJFI1"}],
        },
    )
    panel = order_panel_fields(order)
    assert panel["ok"] is True
    assert panel["date_created"] == "2026-07-10T12:00:00"
    assert panel["payment_method"] == "Klarna"
    assert panel["shipping_method"] == "PostNord"
    assert panel["customer_note"] == "Ring på dörren"
    assert panel["billing_place"] == "Stockholm, SE"
    assert "SECRET" not in (panel["billing_place"] or "")
    assert panel["tracking"] == "JJFI1"

    block = format_order_context_block(order)
    assert "Betalning: Klarna" in block
    assert "Frakt: PostNord" in block
    assert "Leveransort: Stockholm, SE" in block
    assert "Kundnotering: Ring på dörren" in block
    assert "Tracking: JJFI1" in block
    assert "SECRET" not in block


def test_resolve_order_panel_mock_rich(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    panel = resolve_order_panel("1001", use_mock=True)
    assert panel and panel["ok"]
    assert panel["payment_method"] == "Klarna"
    assert panel["shipping_method"] == "PostNord"
    assert panel["billing_place"] == "Stockholm, SE"
    assert panel["tracking"]
