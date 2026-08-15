"""Live GA4 read client — parse fixtures + injected HTTP (no network)."""

from __future__ import annotations

import pytest

from ecom_ops.integrations.ga4 import LiveGA4Transport
from ecom_ops.integrations.marketing_live import (
    MarketingLiveExtraMissing,
    parse_ga4_run_report,
)


def test_parse_ga4_run_report_shape():
    payload = {
        "metricHeaders": [
            {"name": "ecommercePurchases"},
            {"name": "purchaseRevenue"},
            {"name": "sessions"},
        ],
        "rows": [
            {
                "metricValues": [
                    {"value": "42"},
                    {"value": "125400.5"},
                    {"value": "3200"},
                ]
            }
        ],
    }
    parsed = parse_ga4_run_report(payload, property_id="123")
    assert parsed["ecommerce_purchases"] == 42
    assert parsed["purchase_revenue"] == 125400.5
    assert parsed["sessions"] == 3200
    assert parsed["source"] == "ga4_data_api"


def test_live_ga4_run_report_via_http_hook():
    def fake_post(url: str, body: dict) -> dict:
        assert "runReport" in url
        assert "metrics" in body
        return {
            "metricHeaders": [{"name": "ecommercePurchases"}],
            "rows": [{"metricValues": [{"value": "7"}]}],
        }

    transport = LiveGA4Transport("token", http_post=fake_post)
    report = transport.run_report("properties/999", days=7)
    assert report["ecommerce_purchases"] == 7


def test_live_ga4_refuses_mp():
    transport = LiveGA4Transport("token", http_post=lambda u, b: {})
    with pytest.raises(NotImplementedError):
        transport.send_mp_event({"name": "purchase"})


def test_extra_missing_message():
    err = MarketingLiveExtraMissing()
    assert "marketing-live" in str(err)
