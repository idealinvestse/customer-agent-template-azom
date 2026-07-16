"""Tests for V2.1 WooCommerce client extensions.

Covers: shipment-trackings endpoint, multi-site domain resolution,
pagination, extended endpoints (notes/refunds/customers/coupons/reports/
variations/system_status/webhooks), retry/backoff transport.
"""

from __future__ import annotations

import pytest

from ecom_ops.integrations.woocommerce import (
    RequestsTransport,
    SecurityError,
    WooSystemStatus,
    client_from_env,
)

# --------------------------------------------------------------------------- #
# Shipment trackings (P0.1)
# --------------------------------------------------------------------------- #


def test_list_shipment_trackings(woo):
    trackings = woo.list_shipment_trackings("1001")
    assert len(trackings) == 1
    assert trackings[0].tracking_number == "JJFI123456789SE"
    assert trackings[0].carrier == "PostNord"
    assert trackings[0].link == "https://postnord.no/track/JJFI123456789SE"


def test_list_shipment_trackings_empty(woo):
    trackings = woo.list_shipment_trackings("9999")
    assert trackings == []


def test_add_shipment_tracking(woo):
    result = woo.add_shipment_tracking(
        "1002",
        tracking_number="NO123456",
        carrier="Bring",
        tracking_link="https://bring.no/track/NO123456",
        date_shipped="2026-07-15",
    )
    assert result.tracking_number == "NO123456"
    assert result.carrier == "Bring"
    # Verify it persists
    trackings = woo.list_shipment_trackings("1002")
    assert any(t.tracking_number == "NO123456" for t in trackings)


def test_delete_shipment_tracking(woo):
    woo.add_shipment_tracking("1002", tracking_number="TEMP123")
    trackings = woo.list_shipment_trackings("1002")
    trk_id = trackings[0].tracking_id
    result = woo.delete_shipment_tracking("1002", trk_id)
    assert result.get("deleted") is True


def test_shipment_tracking_invalid_order_id(woo):
    with pytest.raises(SecurityError):
        woo.list_shipment_trackings("not-a-number")


# --------------------------------------------------------------------------- #
# Multi-site domain resolution (P0.2)
# --------------------------------------------------------------------------- #


def test_client_from_env_domain_mock():
    """Mock mode ignores domain but stores it."""
    c = client_from_env(use_mock=True, domain="no")
    assert c.domain == "no"
    assert c.base_url == "https://mock.local"


def test_client_from_env_domain_live(monkeypatch):
    """Live mode resolves base_url via woo_base_url_for_domain."""
    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.setenv("WOO_CONSUMER_KEY", "ck_test")
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "cs_test")
    monkeypatch.setenv("WOO_BASE_URL_NO", "https://azom.no")
    c = client_from_env(use_mock=False, domain="no")
    assert c.base_url == "https://azom.no"
    assert c.domain == "no"


def test_client_from_env_domain_convention(monkeypatch):
    """Live mode falls back to https://azom.{domain} convention."""
    monkeypatch.delenv("AZOM_USE_MOCK", raising=False)
    monkeypatch.delenv("WOO_BASE_URL", raising=False)
    monkeypatch.delenv("WOO_BASE_URL_DK", raising=False)
    monkeypatch.setenv("WOO_CONSUMER_KEY", "ck_test")
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "cs_test")
    c = client_from_env(use_mock=False, domain="dk")
    assert c.base_url == "https://azom.dk"


# --------------------------------------------------------------------------- #
# Pagination (P2.7)
# --------------------------------------------------------------------------- #


def test_list_orders_with_page_param(woo):
    page1 = woo.list_orders(per_page=1, page=1)
    page2 = woo.list_orders(per_page=1, page=2)
    assert len(page1) == 1
    assert len(page2) == 1
    assert page1[0].id != page2[0].id


def test_list_all_orders_pagination(woo):
    all_orders = list(woo.list_all_orders(per_page=1, max_pages=10))
    assert len(all_orders) == 2  # mock has 2 orders


def test_list_all_products_pagination(woo):
    all_products = list(woo.list_all_products(per_page=1, max_pages=10))
    assert len(all_products) == 1  # mock has 1 product


def test_list_products_search(woo):
    results = woo.list_products(search="Headset")
    assert len(results) == 1
    assert results[0]["name"] == "Azom Pro Headset"


# --------------------------------------------------------------------------- #
# Extended endpoints (P2.8)
# --------------------------------------------------------------------------- #


def test_list_order_notes(woo):
    notes = woo.list_order_notes("1001")
    assert len(notes) == 1
    assert notes[0]["note"] == "Order mottagen"


def test_add_order_note(woo):
    result = woo.add_order_note("1001", "Kund kontaktad", customer_note=True)
    assert result["note"] == "Kund kontaktad"
    assert result["customer_note"] is True
    notes = woo.list_order_notes("1001")
    assert any(n["note"] == "Kund kontaktad" for n in notes)


def test_list_refunds_empty(woo):
    refunds = woo.list_refunds("1001")
    assert refunds == []


def test_list_customers(woo):
    customers = woo.list_customers()
    assert len(customers) == 1
    assert customers[0]["email"] == "customer@example.com"


def test_list_coupons(woo):
    coupons = woo.list_coupons()
    assert coupons == []


def test_list_reports(woo):
    reports = woo.list_reports()
    assert len(reports) == 1
    assert reports[0]["slug"] == "sales"


def test_list_product_variations(woo):
    variations = woo.list_product_variations("501")
    assert variations == []


# --------------------------------------------------------------------------- #
# System status / version detection (P3.9)
# --------------------------------------------------------------------------- #


def test_get_system_status(woo):
    status = woo.get_system_status()
    assert isinstance(status, WooSystemStatus)
    assert status.version == "9.4.2"
    assert status.wordpress_version == "6.5"
    assert "WooCommerce Shipment Tracking" in status.active_plugins
    assert "Google Listings & Ads" in status.active_plugins


# --------------------------------------------------------------------------- #
# Webhook management
# --------------------------------------------------------------------------- #


def test_create_and_list_webhook(woo):
    created = woo.create_webhook(
        topic="order.updated",
        delivery_url="https://agent.example/webhooks/woo",
        secret="secret123",
        name="Azom agent",
    )
    assert created["topic"] == "order.updated"
    assert created["delivery_url"] == "https://agent.example/webhooks/woo"
    webhooks = woo.list_webhooks()
    assert len(webhooks) == 1
    assert webhooks[0]["topic"] == "order.updated"


def test_delete_webhook(woo):
    created = woo.create_webhook(
        topic="order.created",
        delivery_url="https://agent.example/webhooks/woo",
    )
    wid = str(created["id"])
    result = woo.delete_webhook(wid)
    assert result.get("deleted") is True
    assert woo.list_webhooks() == []


# --------------------------------------------------------------------------- #
# Retry/backoff transport (P1.4 + P1.5)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code, headers=None, json_data=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._json = json_data
        self.text = text
        self.content = text.encode() if text else b"{}"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    """Fake requests.Session that returns scripted responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self._responses:
            raise RuntimeError("No more scripted responses")
        return self._responses.pop(0)


def test_retry_on_429_then_success(monkeypatch):
    """RequestsTransport retries on 429 with Retry-After, then succeeds."""
    session = _FakeSession([
        _FakeResponse(429, headers={"Retry-After": "0"}),
        _FakeResponse(200, json_data={"ok": True}),
    ])
    transport = RequestsTransport(session=session, max_retries=3, backoff_base=0.01)
    result = transport.request("GET", "https://example.com/api")
    assert result == {"ok": True}
    assert len(session.calls) == 2


def test_retry_on_503_then_success(monkeypatch):
    session = _FakeSession([
        _FakeResponse(503),
        _FakeResponse(200, json_data={"ok": True}),
    ])
    transport = RequestsTransport(session=session, max_retries=3, backoff_base=0.01)
    result = transport.request("GET", "https://example.com/api")
    assert result == {"ok": True}


def test_retry_exhausted_raises():
    session = _FakeSession([
        _FakeResponse(429, headers={"Retry-After": "0"}),
        _FakeResponse(429, headers={"Retry-After": "0"}),
        _FakeResponse(429, headers={"Retry-After": "0"}),
        _FakeResponse(429, headers={"Retry-After": "0"}),
    ])
    transport = RequestsTransport(session=session, max_retries=2, backoff_base=0.01)
    with pytest.raises(SecurityError, match="after 2 retries"):
        transport.request("GET", "https://example.com/api")


def test_no_retry_on_404():
    session = _FakeSession([_FakeResponse(404, text="not found")])
    transport = RequestsTransport(session=session, max_retries=3, backoff_base=0.01)
    with pytest.raises(SecurityError, match="404"):
        transport.request("GET", "https://example.com/api")
    assert len(session.calls) == 1


def test_rate_limit_retry_after_header():
    """RateLimit-Retry-After header is honored."""
    session = _FakeSession([
        _FakeResponse(429, headers={"RateLimit-Retry-After": "0"}),
        _FakeResponse(200, json_data={"ok": True}),
    ])
    transport = RequestsTransport(session=session, max_retries=3, backoff_base=0.01)
    result = transport.request("GET", "https://example.com/api")
    assert result == {"ok": True}


def test_204_returns_empty_dict():
    session = _FakeSession([_FakeResponse(204)])
    transport = RequestsTransport(session=session, max_retries=3, backoff_base=0.01)
    result = transport.request("DELETE", "https://example.com/api")
    assert result == {}


def test_non_json_response_raises():
    session = _FakeSession([_FakeResponse(200, text="not json")])
    transport = RequestsTransport(session=session, max_retries=0, backoff_base=0.01)
    with pytest.raises(SecurityError, match="non-JSON"):
        transport.request("GET", "https://example.com/api")
