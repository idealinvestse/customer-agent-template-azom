"""WooCommerce REST API client (order + product)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

import requests

from ecom_ops.security import (
    SecurityError,
    get_env,
    validate_order_id,
    validate_order_status,
)


@dataclass(frozen=True)
class WooOrder:
    id: str
    status: str
    currency: str
    total: str
    billing_email: str | None = None
    raw: dict[str, Any] | None = None


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any: ...


class RequestsTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        resp = requests.request(
            method,
            url,
            auth=auth,
            json=json,
            params=params,
            timeout=timeout,
        )
        if resp.status_code >= 400:
            raise SecurityError(
                f"WooCommerce API error {resp.status_code}: {resp.text[:300]}"
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


class InMemoryWooTransport:
    """Test double for WooCommerce without network."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {
            "1001": {
                "id": 1001,
                "status": "processing",
                "currency": "SEK",
                "total": "499.00",
                "billing": {"email": "customer@example.com"},
            },
            "1002": {
                "id": 1002,
                "status": "pending",
                "currency": "NOK",
                "total": "299.00",
                "billing": {"email": "no@example.com"},
            },
        }
        self.products: dict[str, dict[str, Any]] = {
            "501": {
                "id": 501,
                "name": "Azom Pro Headset",
                "description": "",
                "short_description": "",
            }
        }
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        self.calls.append((method, url, json))
        # /wp-json/wc/v3/orders or /wp-json/wc/v3/orders/1001
        if "/orders" in url and "/products" not in url:
            path_tail = url.split("/orders", 1)[-1]
            oid = path_tail.lstrip("/").split("?")[0].strip("/")
            if not oid:
                if method.upper() == "GET":
                    return list(self.orders.values())[: int((params or {}).get("per_page") or 10)]
                raise SecurityError(f"Unhandled mock URL: {method} {url}")
            if method.upper() == "GET":
                if oid not in self.orders:
                    raise SecurityError(f"WooCommerce API error 404: order {oid}")
                return self.orders[oid]
            if method.upper() == "PUT":
                if oid not in self.orders:
                    raise SecurityError(f"WooCommerce API error 404: order {oid}")
                self.orders[oid] = {**self.orders[oid], **(json or {})}
                return self.orders[oid]
        if "/products/" in url:
            pid = url.rstrip("/").split("/")[-1].split("?")[0]
            if method.upper() == "GET":
                if pid not in self.products:
                    raise SecurityError(f"WooCommerce API error 404: product {pid}")
                return self.products[pid]
            if method.upper() == "PUT":
                if pid not in self.products:
                    raise SecurityError(f"WooCommerce API error 404: product {pid}")
                self.products[pid] = {**self.products[pid], **(json or {})}
                return self.products[pid]
        raise SecurityError(f"Unhandled mock URL: {method} {url}")


class WooCommerceClient:
    def __init__(
        self,
        *,
        base_url: str,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        transport: HttpTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.consumer_key = consumer_key or get_env("WOO_CONSUMER_KEY", "")
        self.consumer_secret = consumer_secret or get_env("WOO_CONSUMER_SECRET", "")
        self.transport = transport or RequestsTransport()
        if not isinstance(self.transport, InMemoryWooTransport):
            if not self.consumer_key or not self.consumer_secret:
                raise SecurityError(
                    "WOO_CONSUMER_KEY and WOO_CONSUMER_SECRET are required"
                )

    def _auth(self) -> tuple[str, str] | None:
        if isinstance(self.transport, InMemoryWooTransport):
            return None
        return (self.consumer_key or "", self.consumer_secret or "")

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def get_order(self, order_id: str | int) -> WooOrder:
        oid = validate_order_id(order_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wc/v3/orders/{oid}"),
            auth=self._auth(),
        )
        return self._to_order(data)

    def list_orders(self, *, per_page: int = 1) -> list[WooOrder]:
        """Lightweight connectivity check / listing (no hardcoded order id)."""
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/orders"),
            auth=self._auth(),
            params={"per_page": max(1, min(int(per_page), 100))},
        )
        if not isinstance(data, list):
            return []
        return [self._to_order(row) for row in data if isinstance(row, dict)]

    def update_order_status(self, order_id: str | int, status: str) -> WooOrder:
        oid = validate_order_id(order_id)
        st = validate_order_status(status)
        data = self.transport.request(
            "PUT",
            self._url(f"/wp-json/wc/v3/orders/{oid}"),
            auth=self._auth(),
            json={"status": st},
        )
        return self._to_order(data)

    def get_product(self, product_id: str | int) -> dict[str, Any]:
        pid = validate_order_id(product_id)  # same digit rules
        return self.transport.request(
            "GET",
            self._url(f"/wp-json/wc/v3/products/{pid}"),
            auth=self._auth(),
        )

    def update_product_description(
        self,
        product_id: str | int,
        *,
        description: str,
        short_description: str | None = None,
    ) -> dict[str, Any]:
        pid = validate_order_id(product_id)
        payload: dict[str, Any] = {"description": description}
        if short_description is not None:
            payload["short_description"] = short_description
        return self.transport.request(
            "PUT",
            self._url(f"/wp-json/wc/v3/products/{pid}"),
            auth=self._auth(),
            json=payload,
        )

    @staticmethod
    def _to_order(data: dict[str, Any]) -> WooOrder:
        billing = data.get("billing") or {}
        return WooOrder(
            id=str(data.get("id", "")),
            status=str(data.get("status", "")),
            currency=str(data.get("currency", "")),
            total=str(data.get("total", "")),
            billing_email=billing.get("email"),
            raw=data,
        )


def client_from_env(
    *,
    base_url: str | None = None,
    use_mock: bool | None = None,
) -> WooCommerceClient:
    mock = use_mock
    if mock is None:
        mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    if mock:
        return WooCommerceClient(
            base_url=base_url or "https://mock.local",
            transport=InMemoryWooTransport(),
        )
    return WooCommerceClient(base_url=base_url or get_env("WOO_BASE_URL", "https://localhost") or "https://localhost")
