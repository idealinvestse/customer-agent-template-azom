"""Live Google Ads read client — parse fixtures + injected HTTP."""

from __future__ import annotations

import pytest

from ecom_ops.integrations.google_ads import LiveGoogleAdsTransport
from ecom_ops.integrations.marketing_live import parse_ads_search_campaigns


def test_parse_ads_search_campaigns():
    payload = {
        "results": [
            {
                "campaign": {"id": "111", "name": "SE Search", "status": "ENABLED"},
                "metrics": {
                    "costMicros": "4000000",
                    "clicks": "10",
                    "conversions": "2",
                    "conversionsValue": "500",
                },
            }
        ]
    }
    parsed = parse_ads_search_campaigns(payload, customer_id="123")
    assert parsed["cost"] == 4.0
    assert parsed["clicks"] == 10
    assert parsed["campaigns"][0]["name"] == "SE Search"


def test_live_ads_campaign_performance_http_hook():
    def fake_post(url: str, body: dict) -> dict:
        assert "googleAds:search" in url
        assert "query" in body
        return {
            "results": [
                {
                    "campaign": {"id": "1", "name": "PMax"},
                    "metrics": {"costMicros": "1000000", "clicks": "3"},
                }
            ]
        }

    transport = LiveGoogleAdsTransport(
        developer_token="dev", access_token="tok", http_post=fake_post
    )
    dig = transport.campaign_performance("1234567890", days=7)
    assert dig["cost"] == 1.0
    assert dig["clicks"] == 3


def test_live_ads_mutate_still_refused():
    transport = LiveGoogleAdsTransport(
        developer_token="dev", access_token="tok", http_post=lambda u, b: {}
    )
    with pytest.raises(NotImplementedError):
        transport.mutate("123", [{"op": "pause"}])
