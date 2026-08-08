"""Live Google Ads / GA4 / Merchant transports — HTTP mocked (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses

from ecom_ops.integrations.ga4 import LiveGA4Transport
from ecom_ops.integrations.google_ads import LiveGoogleAdsTransport
from ecom_ops.integrations.merchant import LiveMerchantTransport
from ecom_ops.marketing.config import clear_marketing_config_cache
from ecom_ops.oauth.google_marketing import (
    GoogleMarketingOAuthStore,
    GoogleMarketingTokenBundle,
    ensure_fresh_access_token,
)


@pytest.fixture(autouse=True)
def _mkt_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "AZOM_CONFIG_DIR", str(Path(__file__).resolve().parents[1] / "config")
    )
    monkeypatch.setenv("GOOGLE_ADS_DEVELOPER_TOKEN", "dev-token")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GOOGLE_MERCHANT_ID", "999888")
    monkeypatch.setenv("GA4_MEASUREMENT_ID", "G-TEST123")
    monkeypatch.setenv("GA4_MEASUREMENT_API_SECRET", "mp-secret")
    clear_marketing_config_cache()
    yield
    clear_marketing_config_cache()


def test_live_ads_requires_developer_token():
    t = LiveGoogleAdsTransport(developer_token="", access_token="tok")
    with pytest.raises(RuntimeError, match="DEVELOPER_TOKEN"):
        t.campaign_performance("123", days=1)


def test_live_ads_requires_access_token():
    t = LiveGoogleAdsTransport(developer_token="dev", access_token="")
    with pytest.raises(RuntimeError, match="access_token"):
        t.campaign_performance("123", days=1)


@responses.activate
def test_live_ads_campaign_performance():
    cid = "1234567890"
    responses.add(
        responses.POST,
        f"https://googleads.googleapis.com/v17/customers/{cid}/googleAds:search",
        json={
            "results": [
                {
                    "campaign": {
                        "id": "111",
                        "name": "SE Brand",
                        "status": "ENABLED",
                        "advertisingChannelType": "SEARCH",
                    },
                    "metrics": {
                        "costMicros": "1000000",
                        "clicks": "10",
                        "conversions": 2.0,
                        "conversionsValue": 500.0,
                    },
                }
            ]
        },
        status=200,
    )
    t = LiveGoogleAdsTransport(developer_token="dev", access_token="tok")
    dig = t.campaign_performance(cid, days=7)
    assert dig["source"] == "google_ads_api"
    assert dig["cost"] == 1.0
    assert dig["clicks"] == 10
    assert dig["campaigns"][0]["name"] == "SE Brand"
    assert responses.calls[0].request.headers["developer-token"] == "dev"


@responses.activate
def test_live_ads_search_term_waste():
    cid = "1234567890"
    responses.add(
        responses.POST,
        f"https://googleads.googleapis.com/v17/customers/{cid}/googleAds:search",
        json={
            "results": [
                {
                    "searchTermView": {"searchTerm": "gratis jobb"},
                    "metrics": {"costMicros": "9000000", "conversions": 0.0},
                    "campaign": {"id": "222"},
                },
                {
                    "searchTermView": {"searchTerm": "azom"},
                    "metrics": {"costMicros": "1000000", "conversions": 1.0},
                    "campaign": {"id": "111"},
                },
            ]
        },
        status=200,
    )
    t = LiveGoogleAdsTransport(developer_token="dev", access_token="tok")
    rows = t.search_term_waste(cid, days=7, min_cost_micros=5_000_000)
    assert len(rows) == 1
    assert rows[0]["search_term"] == "gratis jobb"


@responses.activate
def test_live_ads_mutate_pause():
    cid = "1234567890"
    responses.add(
        responses.POST,
        f"https://googleads.googleapis.com/v17/customers/{cid}/googleAds:mutate",
        json={"mutateOperationResponses": [{}]},
        status=200,
    )
    t = LiveGoogleAdsTransport(developer_token="dev", access_token="tok")
    result = t.mutate(cid, [{"op": "pause_campaign", "campaign_id": "111"}])
    assert result["ok"] is True
    body = json.loads(responses.calls[0].request.body)
    assert "mutateOperations" in body


@responses.activate
def test_live_ga4_run_report():
    pid = "987654321"
    responses.add(
        responses.POST,
        f"https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport",
        json={
            "rows": [
                {
                    "metricValues": [
                        {"value": "100"},
                        {"value": "5"},
                        {"value": "1200.5"},
                    ]
                }
            ],
            "metadata": {"currencyCode": "SEK"},
        },
        status=200,
    )
    t = LiveGA4Transport(access_token="tok")
    dig = t.run_report(pid, days=7)
    assert dig["sessions"] == 100
    assert dig["ecommerce_purchases"] == 5
    assert dig["purchase_revenue"] == 1200.5
    assert dig["source"] == "ga4_data_api"


@responses.activate
def test_live_ga4_mp_event():
    responses.add(
        responses.POST,
        "https://www.google-analytics.com/mp/collect",
        body=b"",
        status=204,
        content_type="text/plain",
    )
    t = LiveGA4Transport(access_token="tok")
    result = t.send_mp_event(
        {"client_id": "c1", "events": [{"name": "purchase", "params": {}}]}
    )
    assert result["ok"] is True


@responses.activate
def test_live_merchant_list():
    mid = "999888"
    responses.add(
        responses.GET,
        f"https://shoppingcontent.googleapis.com/content/v2.1/{mid}/products",
        json={
            "resources": [
                {
                    "id": "online:sv:SE:SKU-1",
                    "offerId": "SKU-1",
                    "title": "Prod",
                    "availability": "in stock",
                }
            ]
        },
        status=200,
    )
    t = LiveMerchantTransport(access_token="tok", merchant_id=mid)
    products = t.list_products()
    assert len(products) == 1
    assert products[0]["offerId"] == "SKU-1"


@responses.activate
def test_ensure_fresh_access_token_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    store = GoogleMarketingOAuthStore(data_dir=tmp_path / "data")
    store.save_tokens(
        GoogleMarketingTokenBundle(
            access_token="old",
            refresh_token="refresh-me",
            expires_at=1.0,  # expired
            token_type="Bearer",
            scope="x",
        )
    )
    responses.add(
        responses.POST,
        "https://oauth2.googleapis.com/token",
        json={
            "access_token": "new-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        status=200,
    )
    token = ensure_fresh_access_token()
    assert token == "new-token"
    reloaded = store.load_tokens()
    assert reloaded is not None
    assert reloaded.access_token == "new-token"
