"""Tests for Wave 1 live-site REST/transport audit fixes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
import requests
from infrastructure.dashboard.secret_probes import _scrub_error, probe_wordpress

from ecom_ops.config import woo_base_url_for_domain
from ecom_ops.integrations._http_transport import RequestsTransport
from ecom_ops.integrations.webhooks import WebhookReceiver
from ecom_ops.integrations.woocommerce import (
    SecurityError,
    WooCommerceClient,
    client_from_env,
)
from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient
from ecom_ops.order_context import resolve_order_panel


class _StubHttpResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.content = self.text.encode("utf-8")
        self.headers = headers or {}
        self.url = "https://shop.example.test/wp-json/test"

    def json(self) -> Any:
        return self._payload


def test_woo_base_url_for_domain_prefers_domain_env_and_avoids_cross_market_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WOO_BASE_URL_DK", raising=False)
    monkeypatch.delenv("WOO_DK_BASE_URL", raising=False)
    monkeypatch.setenv("WOO_BASE_URL", "https://azom.no")
    # Never route DK to azom.no fallback URL
    assert woo_base_url_for_domain("dk") == "https://azom.dk"

    monkeypatch.setenv("WOO_DK_BASE_URL", "https://dk-legacy.azom.dk")
    assert woo_base_url_for_domain("dk") == "https://dk-legacy.azom.dk"

    monkeypatch.setenv("WOO_BASE_URL_DK", "https://azom.dk/")
    assert woo_base_url_for_domain("dk") == "https://azom.dk"


def test_requests_transport_retry_after_capped_and_post_not_retried_on_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("ecom_ops.integrations._http_transport.time.sleep", lambda s: sleeps.append(s))
    calls: list[tuple[str, str]] = []

    class _FakeSession:
        def request(self, method: str, url: str, **_kwargs: Any) -> _StubHttpResponse:
            calls.append((method, url))
            if method == "POST":
                return _StubHttpResponse(502, {"message": "bad gateway"})
            if len(calls) == 1:
                return _StubHttpResponse(429, {"message": "slow down"}, headers={"Retry-After": "999"})
            return _StubHttpResponse(200, {"ok": True})

    tr = RequestsTransport(
        session=_FakeSession(),  # type: ignore[arg-type]
        max_retries=2,
        backoff_base=1.0,
        max_retry_after=30.0,
    )
    res_get = tr.request("GET", "https://azom.no/wp-json/wc/v3/orders")
    assert res_get == {"ok": True}
    assert sleeps == [30.0]

    calls.clear()
    with pytest.raises(SecurityError):
        tr.request("POST", "https://azom.no/wp-json/wc/v3/orders", json={"status": "processing"})
    assert len(calls) == 1


def test_requests_transport_retries_post_on_429_and_redacts_secrets_in_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ecom_ops.integrations._http_transport.time.sleep", lambda _s: None)
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "cs_supersecret_value_123")
    calls = {"n": 0}

    class _FakeSession:
        def request(self, method: str, url: str, **_kwargs: Any) -> Any:
            calls["n"] += 1
            if method == "POST" and calls["n"] == 1:
                return _StubHttpResponse(429, {"message": "limit"}, headers={"Retry-After": "0"})
            if method == "GET":
                raise requests.ConnectionError("failed auth ghp_0123456789abcdef0123456789abcdef0123")
            return _StubHttpResponse(201, {"id": 99})

    tr = RequestsTransport(
        session=_FakeSession(),  # type: ignore[arg-type]
        max_retries=1,
    )
    res_post = tr.request("POST", "https://azom.no/wp-json/wc/v3/orders", json={"status": "processing"})
    assert res_post == {"id": 99}
    assert calls["n"] == 2

    with pytest.raises(SecurityError) as exc_info:
        tr.request("GET", "https://azom.no/wp-json/wc/v3/orders")
    assert "ghp_0123456789abcdef0123456789abcdef0123" not in str(exc_info.value)


def test_woo_system_status_real_json_tracking_provider_and_pagination() -> None:
    recorded: list[tuple[str, str, dict[str, Any] | None]] = []

    class _FakeWooTransport:
        def request(
            self,
            method: str,
            url: str,
            *,
            auth: Any = None,
            params: dict[str, Any] | None = None,
            json: Any = None,
            timeout: float = 30,
        ) -> Any:
            del auth, timeout
            recorded.append((method, url, json if method == "POST" else params))
            if url.endswith("/system_status"):
                return {
                    "environment": {"wp_version": "6.7.2", "version": "9.6.1"},
                    "database": {"wc_database_version": "9.6.1"},
                    "active_plugins": [{"name": "WooCommerce"}],
                    "theme": {"name": "Azom Child"},
                }
            if url.endswith("/shipment-trackings"):
                return {
                    "tracking_id": "trk_1",
                    "tracking_number": "JJFI999",
                    "tracking_provider": "PostNord",
                    "tracking_link": "https://track.example/JJFI999",
                }
            return []

    client = WooCommerceClient(
        base_url="https://azom.no",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        transport=_FakeWooTransport(),  # type: ignore[arg-type]
    )
    status = client.get_system_status()
    assert status.wordpress_version == "6.7.2"
    assert status.version == "9.6.1"

    trk = client.add_shipment_tracking("1001", tracking_number="JJFI999", carrier="PostNord")
    assert trk.carrier == "PostNord"
    post_call = next(item for item in recorded if item[0] == "POST")
    assert post_call[2]["tracking_provider"] == "PostNord"

    client.list_product_variations(55, per_page=25, page=2)
    client.list_order_notes(1001, per_page=15, page=2)
    client.list_refunds(1001, per_page=10, page=1)
    client.list_webhooks(per_page=50, page=2)
    var_call = next(item for item in recorded if "/variations" in item[1])
    assert var_call[2] == {"per_page": 25, "page": 2}


def test_wordpress_client_revisions_plugin_404_fallback_and_cross_host_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wp = WordPressClient(base_url="https://mock.local", transport=InMemoryWpTransport())
    page = wp.get_page(2, context="edit")
    assert page.id == 2
    revs = wp.list_revisions("pages", 2)
    assert isinstance(revs, list) and len(revs) >= 1
    drafts = wp.list_pages(status="draft")
    assert isinstance(drafts, list)

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("WP_USER", "oscar")
    monkeypatch.setenv("WP_APP_PASSWORD", "xxxx xxxx")
    monkeypatch.setenv("WP_BASE_URL", "https://azom.no")
    wp_live = WordPressClient(
        base_url="https://azom.no",
        username="oscar",
        app_password="pw",
    )
    with pytest.raises(ValueError, match="Cross-host"):
        wp_live.create_page(title="T", content="C", domain="dk")

    calls: list[str] = []

    class _ApacheFallbackTransport:
        def request(self, method: str, url: str, **_kwargs: Any) -> Any:
            calls.append(f"{method} {url}")
            if "%2F" in url:
                raise SecurityError("HTTP 404 rest_no_route")
            return {"plugin": "akismet/akismet", "status": "active"}

    wp_apache = WordPressClient(
        base_url="https://azom.no",
        username="oscar",
        app_password="pw",
        transport=_ApacheFallbackTransport(),  # type: ignore[arg-type]
    )
    updated = wp_apache.set_plugin_status("akismet/akismet", "active")
    assert updated["status"] == "active"
    assert len(calls) == 2 and "%2F" in calls[0] and "/plugins/akismet/akismet" in calls[1]


def test_order_context_parses_wc_shipment_tracking_items_and_endpoint_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    woo = client_from_env(use_mock=True)
    monkeypatch.setattr("ecom_ops.integrations.woocommerce.client_from_env", lambda **_kw: woo)
    panel = resolve_order_panel("1001", use_mock=True)
    assert panel is not None
    assert panel["tracking_source"] == "endpoint"
    assert "JJFI123456789SE" in str(panel["tracking"])

    class _MetaOrderWoo(WooCommerceClient):
        def list_shipment_trackings(self, _order_id: str | int) -> list[Any]:
            return []

        def get_order(self, _order_id: str | int) -> dict[str, Any]:
            from ecom_ops.integrations.woocommerce import WooOrder

            return WooOrder(
                id=2002,
                status="completed",
                total="1990.00",
                currency="NOK",
                raw={
                    "id": 2002,
                    "status": "completed",
                    "billing": {"first_name": "Kund"},
                    "meta_data": [
                        {
                            "key": "_wc_shipment_tracking_items",
                            "value": [
                                {
                                    "tracking_provider": "PostNord",
                                    "tracking_number": "PN987654",
                                    "tracking_link": "https://track.postnord.no/PN987654",
                                }
                            ],
                        }
                    ],
                },
            )

    woo_meta = _MetaOrderWoo(
        base_url="https://mock.local",
        consumer_key="ck_m",
        consumer_secret="cs_m",
        transport=woo.transport,
    )
    monkeypatch.setattr("ecom_ops.integrations.woocommerce.client_from_env", lambda **_kw: woo_meta)
    panel_meta = resolve_order_panel("2002", use_mock=True)
    assert panel_meta is not None
    assert "PN987654" in str(panel_meta["tracking"])


def test_probe_wordpress_checks_auth_and_scrubs_bridge_and_webhook_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrubbed = _scrub_error("bad WOO_WEBHOOK_SECRET=whsec_abc and WP_BRIDGE_SECRET_NO=topsecret999")
    assert "whsec_abc" not in scrubbed and "topsecret999" not in scrubbed

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("WP_USERNAME", "oscar")
    monkeypatch.setenv("WP_APP_PASSWORD", "abcd")
    monkeypatch.setenv("WP_BASE_URL", "https://azom.no")

    class _FakeWp:
        def get_settings(self) -> dict[str, Any]:
            raise RuntimeError("HTTP 401 unauthorized Authorization stripped")

        def list_posts(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("HTTP 403 rest_forbidden")

    monkeypatch.setattr("ecom_ops.integrations.wordpress.wp_client_from_env", lambda **_kw: _FakeWp())
    res = probe_wordpress()
    assert res.status == "error"


def test_webhook_receiver_activation_ping_and_signature() -> None:
    from flask import Flask

    app = Flask(__name__)
    seen: list[str] = []
    recv = WebhookReceiver(secret="wh_secret_12345")
    recv.on("order.updated", lambda ev: seen.append(ev.topic))

    assert recv.is_activation_ping(b"webhook_id=42") is True
    with app.test_request_context(
        "/webhooks/woo",
        method="POST",
        data="webhook_id=42",
        content_type="application/x-www-form-urlencoded",
    ):
        from flask import request as fl_req

        assert recv.handle_request(fl_req) is True

    body = json.dumps({"id": 1001, "status": "completed"}).encode("utf-8")
    sig = hmac.new(b"wh_secret_12345", body, hashlib.sha256).digest()

    with app.test_request_context(
        "/webhooks/woo",
        method="POST",
        data=body,
        content_type="application/json",
        headers={
            "X-WC-Webhook-Topic": "order.updated",
            "X-WC-Webhook-Signature": base64.b64encode(sig).decode("ascii"),
            "X-WC-Webhook-Delivery-ID": "deliv-1",
        },
    ):
        from flask import request as fl_req

        assert recv.handle_request(fl_req) is True
        assert seen == ["order.updated"]


def test_wave2_rest_creds_transport_guard_tracking_webhook_dedup_and_redact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flask import Flask

    from ecom_ops.integrations import _http_guard
    from ecom_ops.order_context import _extract_tracking
    from ecom_ops.security import redact_secrets

    # V-20: per-market Woo credentials
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("WOO_CONSUMER_KEY_DK", "ck_dk_123")
    monkeypatch.setenv("WOO_CONSUMER_SECRET_DK", "cs_dk_456")
    woo_dk = client_from_env(domain="dk", use_mock=False)
    assert woo_dk.consumer_key == "ck_dk_123" and woo_dk.consumer_secret == "cs_dk_456"

    # V-21: RequestsTransport does not retry PUT/DELETE/POST 5xx and disables redirects
    seen_calls: list[tuple[str, bool]] = []

    class _Sess:
        trust_env = True

        def request(self, method: str, url: str, **kwargs: Any) -> _StubHttpResponse:
            del url
            seen_calls.append((method, bool(kwargs.get("allow_redirects", True))))
            return _StubHttpResponse(503, {"error": "down"})

    monkeypatch.setattr("requests.Session", lambda: _Sess())
    monkeypatch.setattr("time.sleep", lambda _s: None)
    tr = RequestsTransport(max_retries=2)
    with pytest.raises(SecurityError):
        tr.request("PUT", "https://azom.no/wp-json/wc/v3/products/1")
    with pytest.raises(SecurityError):
        tr.request("POST", "https://azom.no/wp-json/wc/v3/orders/1")
    assert len(seen_calls) == 2
    assert all(allow_red is False for _, allow_red in seen_calls)

    # V-22: _http_guard sticky auth_stripped across cross-origin -> origin-A bounce
    seen_auth: list[tuple[str, str | None]] = []

    class _RedirConn:
        def __init__(self, host: str, **_kw: Any) -> None:
            self.host = host
            self._hop = len(seen_auth)

        def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> None:
            del method, path, body
            seen_auth.append((self.host, (headers or {}).get("Authorization")))

        def getresponse(self) -> Any:
            if self._hop == 0:
                hdrs = {"Location": "https://other.example.com/step2"}

                class _R0:
                    status = 302
                    headers = hdrs

                    @staticmethod
                    def getheaders() -> list[tuple[str, str]]:
                        return list(hdrs.items())

                    @staticmethod
                    def getheader(name: str, default: str | None = None) -> str | None:
                        return hdrs.get(name, default)

                    @staticmethod
                    def read(_n: int = 0) -> bytes:
                        return b""

                    @staticmethod
                    def close() -> None:
                        pass

                return _R0()
            if self._hop == 1:
                hdrs = {"Location": "https://azom.no/final"}

                class _R1:
                    status = 302
                    headers = hdrs

                    @staticmethod
                    def getheaders() -> list[tuple[str, str]]:
                        return list(hdrs.items())

                    @staticmethod
                    def getheader(name: str, default: str | None = None) -> str | None:
                        return hdrs.get(name, default)

                    @staticmethod
                    def read(_n: int = 0) -> bytes:
                        return b""

                    @staticmethod
                    def close() -> None:
                        pass

                return _R1()
            hdrs = {"Content-Type": "application/json"}

            class _R2:
                status = 200
                headers = hdrs
                _done = False

                @staticmethod
                def getheaders() -> list[tuple[str, str]]:
                    return list(hdrs.items())

                @staticmethod
                def getheader(name: str, default: str | None = None) -> str | None:
                    return hdrs.get(name, default)

                @classmethod
                def read(cls, _n: int = 0) -> bytes:
                    if cls._done:
                        return b""
                    cls._done = True
                    return b"{}"

                @staticmethod
                def close() -> None:
                    pass

            return _R2()

        def close(self) -> None:
            pass

    monkeypatch.setattr(_http_guard, "resolve_and_validate_public_ips", lambda _h, _p=443, **_kw: ["93.184.216.34"])
    monkeypatch.setattr(_http_guard, "PinnedHTTPSConnection", _RedirConn)
    _http_guard.pinned_https_request(
        "https://azom.no/start",
        headers={"Authorization": "Bearer secret-tok"},
        allowed_auth_origins={"https://azom.no"},
    )
    assert seen_auth[0][1] == "Bearer secret-tok"
    assert seen_auth[1][1] is None and seen_auth[2][1] is None

    # V-24: _extract_tracking skips serialized blobs and requires tracking key match
    assert _extract_tracking({"meta_data": [{"key": "billing_phone", "value": "12345678"}]}) is None
    assert _extract_tracking({"meta_data": [{"key": "tracking_number", "value": 'a:1:{i:0;s:4:"AB12";}'}]}) is None
    assert _extract_tracking({"meta_data": [{"key": "tracking_number", "value": "JJFI778899"}]}) == "JJFI778899"

    # V-25: WebhookReceiver dedupes delivery ID and rejects non-dict JSON
    app = Flask(__name__)
    seen_ev: list[str] = []
    recv = WebhookReceiver(secret="wh_secret_12345")
    recv.on("order.updated", lambda ev: seen_ev.append(ev.topic))

    bad_list = b"[1, 2, 3]"
    sig_bad = hmac.new(b"wh_secret_12345", bad_list, hashlib.sha256).digest()
    with app.test_request_context(
        "/webhooks/woo",
        method="POST",
        data=bad_list,
        content_type="application/json",
        headers={"X-WC-Webhook-Topic": "order.updated", "X-WC-Webhook-Signature": base64.b64encode(sig_bad).decode("ascii")},
    ):
        from flask import request as fl_req

        assert recv.handle_request(fl_req) is False

    body = json.dumps({"id": 99}).encode("utf-8")
    sig = hmac.new(b"wh_secret_12345", body, hashlib.sha256).digest()
    hdrs = {
        "X-WC-Webhook-Topic": "order.updated",
        "X-WC-Webhook-Signature": base64.b64encode(sig).decode("ascii"),
        "X-WC-Webhook-Delivery-ID": "deliv-dup-1",
    }
    for _ in range(2):
        with app.test_request_context("/webhooks/woo", method="POST", data=body, content_type="application/json", headers=hdrs):
            from flask import request as fl_req

            recv.handle_request(fl_req)
    assert seen_ev == ["order.updated"]

    # V-38: redact_secrets scrubs Basic tokens and env secret values from plain strings
    scrubbed = redact_secrets("Authorization: Basic YWRtaW46c2VjcmV0 WOO_CONSUMER_SECRET=cs_secret_val")
    assert "YWRtaW46c2VjcmV0" not in scrubbed and "cs_secret_val" not in scrubbed
