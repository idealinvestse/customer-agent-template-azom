"""Tests for V2.1 order_context changes (tracking endpoint + multi-site)."""

from __future__ import annotations

from ecom_ops.order_context import (
    format_order_context_block,
    resolve_order_context,
    resolve_order_id_from_email,
    resolve_order_panel,
)


def test_resolve_order_context_mock():
    block = resolve_order_context("1001", use_mock=True)
    assert block is not None
    assert "[Order 1001]" in block
    assert "processing" in block


def test_resolve_order_panel_with_tracking_endpoint():
    """P0.1: panel should pick up tracking from /shipment-trackings endpoint."""
    panel = resolve_order_panel("1001", use_mock=True)
    assert panel is not None
    assert panel["ok"] is True
    # Mock order 1001 has tracking in meta_data AND via shipment-trackings endpoint
    assert panel["tracking"] is not None
    assert "JJFI" in panel["tracking"]


def test_resolve_order_panel_tracking_from_endpoint_only():
    """P0.1: order 1002 has no tracking in meta_data but has one via the
    dedicated /shipment-trackings endpoint. Panel should pick it up from
    the endpoint and set tracking_source='endpoint'."""
    panel = resolve_order_panel("1002", use_mock=True)
    assert panel is not None
    assert panel["ok"] is True
    assert panel["tracking"] == "BRING999NO"
    assert panel.get("tracking_source") == "endpoint"


def test_resolve_order_panel_not_found():
    panel = resolve_order_panel("9999", use_mock=True)
    assert panel is not None
    assert panel["ok"] is False
    assert "error" in panel


def test_resolve_order_context_with_domain():
    """P0.2: domain parameter is accepted (mock ignores it)."""
    block = resolve_order_context("1001", use_mock=True, domain="no")
    assert block is not None
    assert "[Order 1001]" in block


def test_resolve_order_id_from_email_single_match():
    oid = resolve_order_id_from_email("customer@example.com", use_mock=True)
    assert oid == "1001"


def test_resolve_order_id_from_email_no_match():
    oid = resolve_order_id_from_email("nobody@example.com", use_mock=True)
    assert oid is None


def test_resolve_order_id_from_email_invalid():
    oid = resolve_order_id_from_email("not-an-email", use_mock=True)
    assert oid is None


def test_resolve_order_id_from_email_with_domain():
    oid = resolve_order_id_from_email(
        "customer@example.com", use_mock=True, domain="se"
    )
    assert oid == "1001"


def test_resolve_order_context_empty_id():
    assert resolve_order_context(None, use_mock=True) is None
    assert resolve_order_context("", use_mock=True) is None


def test_format_order_context_block_includes_tracking():
    from ecom_ops.integrations.woocommerce import client_from_env

    woo = client_from_env(use_mock=True)
    order = woo.get_order("1001")
    block = format_order_context_block(order)
    assert "Tracking:" in block
