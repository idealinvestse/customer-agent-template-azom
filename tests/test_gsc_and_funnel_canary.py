"""Tests for Read-Only GSC Connector and WooCommerce Funnel Canary plugin updates."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from ecom_ops.integrations.gsc import (
    GSC_READONLY_SCOPE,
    GoogleSearchConsoleClient,
    gsc_client_from_env,
    gsc_enabled,
    gsc_kill_active,
    gsc_status,
)
from ecom_ops.integrations.wp.bridge_client import InMemoryWpBridgeTransport, WpBridgeClient
from ecom_ops.runtime_env import PROTECTED_OVERLAY_KEYS, overlay_key_allowed
from ecom_ops.security import SecurityError
from ecom_ops.wp_ops.change_ledger import ChangeLedger
from ecom_ops.wp_ops.plugin_service import (
    DEFAULT_CANARY_PATHS,
    WOO_FUNNEL_CANARY_PATHS,
    canary_paths_for_plugin,
)
from ecom_ops.wp_ops.service import WpOpsService


def _clear_gsc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AZOM_GSC_ENABLED",
        "AZOM_GSC_KILL",
        "AZOM_GSC_PROPERTY_URL",
        "AZOM_GSC_PROPERTY_URL_NO",
        "AZOM_GSC_PROPERTY_URL_SE",
        "AZOM_GSC_PROPERTY_URL_DK",
        "GSC_PROPERTY_URL",
        "GSC_OAUTH_CLIENT_ID",
        "GSC_OAUTH_CLIENT_SECRET",
        "GSC_OAUTH_REFRESH_TOKEN",
        "GSC_OAUTH_ACCESS_TOKEN",
        "AZOM_GSC_CLIENT_ID",
        "AZOM_GSC_CLIENT_SECRET",
        "AZOM_GSC_REFRESH_TOKEN",
        "AZOM_GSC_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_gsc_runtime_env_posture_and_overlay_guards() -> None:
    assert "AZOM_GSC_ENABLED" in PROTECTED_OVERLAY_KEYS
    assert "AZOM_GSC_KILL" in PROTECTED_OVERLAY_KEYS
    assert overlay_key_allowed("AZOM_GSC_ENABLED") is False
    assert overlay_key_allowed("AZOM_GSC_KILL") is False
    # Non-posture GSC secrets/config are allowed in overlay
    assert overlay_key_allowed("GSC_OAUTH_REFRESH_TOKEN") is True
    assert overlay_key_allowed("GSC_OAUTH_CLIENT_ID") is True
    assert overlay_key_allowed("AZOM_GSC_PROPERTY_URL_SE") is True


def test_gsc_status_flags_and_credential_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gsc_env(monkeypatch)
    assert GSC_READONLY_SCOPE == "https://www.googleapis.com/auth/webmasters.readonly"

    # Mock mode default
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    assert gsc_kill_active() is False
    assert gsc_enabled() is False
    st_mock = gsc_status("https://azom.se")
    assert st_mock["connected"] is True
    assert st_mock["mode"] == "mock"
    assert st_mock["property_url"] == "https://azom.se/"
    assert st_mock["note"]

    # Kill switch blocks even mock status
    monkeypatch.setenv("AZOM_GSC_KILL", "1")
    assert gsc_kill_active() is True
    assert gsc_status("https://azom.se")["connected"] is False
    assert gsc_status("https://azom.se")["mode"] == "disabled"
    monkeypatch.delenv("AZOM_GSC_KILL", raising=False)

    # Live mode without AZOM_GSC_ENABLED stays fail-closed
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    assert gsc_enabled() is False
    st_off = gsc_status("https://azom.no")
    assert st_off["connected"] is False
    assert st_off["mode"] == "disabled"

    # Enabling live GSC must NOT reuse Gmail / Google Marketing OAuth tokens
    monkeypatch.setenv("AZOM_GSC_ENABLED", "1")
    monkeypatch.setenv("GOOGLE_OAUTH_ACCESS_TOKEN", "marketing-token-should-be-ignored")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "gmail-token-should-be-ignored")
    assert gsc_enabled() is True
    st_nocreds = gsc_status("https://azom.no")
    assert st_nocreds["connected"] is False
    assert st_nocreds["mode"] == "live"

    # Supplying isolated GSC OAuth creds connects live status
    monkeypatch.setenv("GSC_OAUTH_CLIENT_ID", "gsc-client-id")
    monkeypatch.setenv("GSC_OAUTH_CLIENT_SECRET", "gsc-client-secret")
    monkeypatch.setenv("GSC_OAUTH_REFRESH_TOKEN", "gsc-refresh-token")
    st_live = gsc_status("https://azom.no")
    assert st_live["connected"] is True
    assert st_live["mode"] == "live"


@pytest.mark.parametrize(
    ("market", "site_url"),
    [
        ("se", "https://azom.se"),
        ("no", "https://azom.no"),
        ("dk", "https://azom.dk"),
    ],
)
def test_gsc_mock_fixtures_cover_strike_distance_low_ctr_and_cannibalization(
    monkeypatch: pytest.MonkeyPatch, market: str, site_url: str
) -> None:
    _clear_gsc_env(monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    client = gsc_client_from_env(use_mock=True, market=market, site_url=site_url)
    res = client.query_search_analytics(site_url=site_url, market=market, days=28, row_limit=100)
    assert res["ok"] is True
    assert res["mode"] == "mock"
    assert res["days"] == 28
    assert res["property_url"] == f"{site_url}/"
    rows = res["rows"]
    assert len(rows) >= 8

    for row in rows:
        assert {"query", "page", "clicks", "impressions", "ctr", "position"} <= set(row.keys())
        assert row["page"].startswith(site_url)

    # Strike-distance queries (pos 4-15)
    strike = [r for r in rows if 4.0 <= float(r["position"]) <= 15.0]
    assert len(strike) >= 4

    # Low-CTR high-impression pages (impressions >= 1000, ctr < 0.02)
    low_ctr = [r for r in rows if int(r["impressions"]) >= 1000 and float(r["ctr"]) < 0.02]
    assert len(low_ctr) >= 2

    # Cannibalization pairs (same query appearing on >= 2 distinct pages)
    by_query: dict[str, set[str]] = {}
    for r in rows:
        by_query.setdefault(r["query"], set()).add(r["page"])
    cannibalized = [q for q, pages in by_query.items() if len(pages) >= 2]
    assert len(cannibalized) >= 2
    assert sum(Counter(r["query"] for r in rows).values()) == len(rows)


def test_gsc_live_oauth_refresh_query_and_fail_closed_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gsc_env(monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_GSC_ENABLED", "1")
    monkeypatch.setenv("GSC_OAUTH_CLIENT_ID", "gsc-cid")
    monkeypatch.setenv("GSC_OAUTH_CLIENT_SECRET", "gsc-csecret-super")
    monkeypatch.setenv("GSC_OAUTH_REFRESH_TOKEN", "gsc-rtok-super")

    calls: list[dict[str, Any]] = []

    def fake_http(url: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes, str]:
        calls.append({"url": url, **kwargs})
        if "oauth2.googleapis.com/token" in url:
            body = json.dumps({"access_token": "fresh-gsc-access-token", "expires_in": 3600}).encode()
            return 200, {"content-type": "application/json"}, body, url
        body = json.dumps(
            {
                "rows": [
                    {
                        "keys": ["trådløs carplay", "https://azom.no/produkt/carplay-adapter-pro/"],
                        "clicks": 42,
                        "impressions": 1500,
                        "ctr": 0.028,
                        "position": 6.4,
                    }
                ]
            }
        ).encode()
        return 200, {"content-type": "application/json"}, body, url

    client = gsc_client_from_env(use_mock=False, site_url="https://azom.no/", http_request=fake_http)
    res = client.query_search_analytics(site_url="https://azom.no/", market="no", days=14, row_limit=50)
    assert res["ok"] is True
    assert res["mode"] == "live"
    assert res["days"] == 14
    assert len(res["rows"]) == 1
    assert res["rows"][0]["query"] == "trådløs carplay"
    assert res["rows"][0]["clicks"] == 42

    # First call refreshed token; second call queried Search Analytics with encoded property URL
    assert len(calls) == 2
    assert calls[0]["url"] == "https://oauth2.googleapis.com/token"
    assert quote("https://azom.no/", safe="") in calls[1]["url"]
    assert calls[1]["headers"]["Authorization"] == "Bearer fresh-gsc-access-token"

    # Second query reuses cached access token (only 1 additional HTTP call)
    client.query_search_analytics(site_url="https://azom.no/", market="no", days=7, row_limit=10)
    assert len(calls) == 3

    # Kill switch blocks immediately without HTTP calls
    monkeypatch.setenv("AZOM_GSC_KILL", "1")
    with pytest.raises(SecurityError, match="AZOM_GSC_KILL"):
        client.query_search_analytics(site_url="https://azom.no/", market="no")
    assert len(calls) == 3
    monkeypatch.delenv("AZOM_GSC_KILL", raising=False)

    # Disabling AZOM_GSC_ENABLED blocks live client
    monkeypatch.delenv("AZOM_GSC_ENABLED", raising=False)
    with pytest.raises(SecurityError, match="AZOM_GSC_ENABLED"):
        client.query_search_analytics(site_url="https://azom.no/", market="no")

    # Missing isolated credentials raises SecurityError
    monkeypatch.setenv("AZOM_GSC_ENABLED", "1")
    no_cred_client = GoogleSearchConsoleClient(use_mock=False, http_request=fake_http)
    with pytest.raises(SecurityError, match="Isolated GSC OAuth credentials missing"):
        no_cred_client.query_search_analytics(site_url="https://azom.no/", market="no")


def test_plugin_service_woocommerce_funnel_canary_paths_and_auto_revert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "secret-staging")

    # Helper selects full WooCommerce funnel for Woo/critical/theme-touching plugins, "/" otherwise
    assert canary_paths_for_plugin({"slug": "woocommerce", "is_critical": True}) == list(
        WOO_FUNNEL_CANARY_PATHS
    )
    assert canary_paths_for_plugin({"slug": "klarna-checkout-for-woocommerce"}) == list(
        WOO_FUNNEL_CANARY_PATHS
    )
    assert canary_paths_for_plugin({"slug": "elementor"}) == list(WOO_FUNNEL_CANARY_PATHS)
    assert canary_paths_for_plugin({"slug": "wordpress-seo"}) == list(DEFAULT_CANARY_PATHS)

    tr = InMemoryWpBridgeTransport(site_id="mock-staging", base_url="https://staging.example.test")
    svc = WpOpsService(
        data_dir=tmp_path,
        bridge_factory=lambda site: WpBridgeClient(site_id=site.id, transport=tr),
    )

    # Non-funnel plugin plan attaches ["/"] in preconditions
    seo_plan = svc.plan_plugin_updates(
        "mock-staging", actor="oscar", selection_mode="single", slugs=["wordpress-seo"]
    )
    assert seo_plan.ok is True
    assert seo_plan.data["canary_paths"] == ["/"]
    assert seo_plan.data["preconditions"]["canary_paths"] == ["/"]

    # WooCommerce critical plan attaches full funnel paths in preconditions
    woo_plan = svc.plan_plugin_updates(
        "mock-staging",
        actor="oscar",
        selection_mode="single",
        slugs=["woocommerce"],
        include_critical=True,
    )
    assert woo_plan.ok is True
    assert woo_plan.data["canary_paths"] == ["/", "/shop/", "/cart/", "/checkout/"]
    assert woo_plan.data["preconditions"]["canary_paths"] == ["/", "/shop/", "/cart/", "/checkout/"]
    assert woo_plan.data["preconditions"]["per_plugin_canary_paths"]["woocommerce"] == [
        "/",
        "/shop/",
        "/cart/",
        "/checkout/",
    ]

    # Simulated WSOD / PHP fatal on /checkout/ auto-reverts WooCommerce and surfaces canary_results
    tr.fail_canary_paths_for_plugins["woocommerce/woocommerce.php"] = {
        "/checkout/": {"status": 200, "fatal_detected": True}
    }
    halted = svc.execute_plugin_update_plan(
        "mock-staging",
        woo_plan.data["plan_id"],
        actor="oscar",
        expected_sha256=woo_plan.data["plan_sha256"],
        allow_critical=True,
    )
    assert halted.ok is False
    assert halted.data["error_class"] == "CanaryUpdateHalted"
    assert tr.plugins["woocommerce/woocommerce.php"]["version"] == "9.4.2"
    checkout_probe = next(
        cr for cr in halted.data["canary_results"] if cr["path"] == "/checkout/"
    )
    assert checkout_probe["ok"] is False
    assert checkout_probe["fatal_detected"] is True
    assert halted.data["results"][0]["auto_reverted"] is True

    # Clearing simulated /checkout/ failure allows update to succeed with full passing canary_results
    tr.fail_canary_paths_for_plugins.clear()
    woo_plan2 = svc.plan_plugin_updates(
        "mock-staging",
        actor="oscar",
        selection_mode="single",
        slugs=["woocommerce"],
        include_critical=True,
    )
    ok_exec = svc.execute_plugin_update_plan(
        "mock-staging",
        woo_plan2.data["plan_id"],
        actor="oscar",
        expected_sha256=woo_plan2.data["plan_sha256"],
        allow_critical=True,
    )
    assert ok_exec.ok is True
    assert tr.plugins["woocommerce/woocommerce.php"]["version"] == "9.5.1"
    paths_checked = [cr["path"] for cr in ok_exec.data["canary_results"]]
    assert paths_checked == ["/", "/shop/", "/cart/", "/checkout/"]
    assert all(cr["ok"] is True and cr["fatal_detected"] is False for cr in ok_exec.data["canary_results"])

    # Change Ledger row carries batch_id=plan_id and canary_results in metadata
    ledger = ChangeLedger(data_dir=tmp_path)
    chg = ledger.get_change("mock-staging", ok_exec.data["change_ids"][0])
    assert chg is not None
    assert chg["batch_id"] == woo_plan2.data["plan_id"]
    assert len(chg["metadata"]["canary_results"]) == 4
