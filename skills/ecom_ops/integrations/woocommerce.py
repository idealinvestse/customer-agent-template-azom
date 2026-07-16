"""WooCommerce REST API client (order + product + tracking + reports + webhooks).

V2.1 additions (see docs/solutions/2026-07-17-woo-wordpress-capacity-review.md):
- Multi-site per-call domain resolution via ``client_from_env(domain=...)``.
- Dedicated ``/wc/v3/orders/{id}/shipment-trackings`` endpoint (replaces
  fragile meta_data heuristics in ``order_context._extract_tracking``).
- ``requests.Session`` reuse + configurable timeout + retry/backoff with
  ``RateLimit-*`` / ``Retry-After`` header awareness (429/5xx).
- Pagination helper ``list_all_orders`` / ``list_all_products``.
- Extended endpoint surface: order notes, refunds, customers, coupons,
  reports, stock, product variations, system_status (version detection).
- Webhook management: list/create/delete (delivery receiver lives in
  ``ecom_ops.integrations.webhooks``).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol
from urllib.parse import urljoin

import requests

from ecom_ops.security import (
    SecurityError,
    get_env,
    validate_order_id,
    validate_order_status,
)

# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WooOrder:
    id: str
    status: str
    currency: str
    total: str
    billing_email: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ShipmentTracking:
    tracking_id: str
    tracking_number: str | None
    carrier: str | None
    link: str | None
    date_shipped: str | None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class WooSystemStatus:
    """Subset of /wc/v3/system_status used for version + health checks."""

    version: str | None
    wordpress_version: str | None
    active_plugins: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Transport protocol
# --------------------------------------------------------------------------- #


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
    """Live HTTP transport with session reuse + retry/backoff for 429/5xx.

    Honors Woo Store API ``RateLimit-*`` headers and ``Retry-After`` when
    present. Retries are bounded (default 3) with exponential backoff.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        default_timeout: float = 30.0,
    ) -> None:
        self.session = session or requests.Session()
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.default_timeout = float(default_timeout)

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
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    auth=auth,
                    json=json,
                    params=params,
                    timeout=timeout or self.default_timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                # Network blip — backoff and retry
                if attempt >= self.max_retries:
                    raise SecurityError(f"WooCommerce transport error: {exc}") from exc
                self._sleep_backoff(attempt)
                continue

            # Retryable status codes
            if resp.status_code in {429, 500, 502, 503, 504}:
                if attempt >= self.max_retries:
                    break  # fall through to error raise below
                wait = self._retry_after(resp) or self._sleep_backoff(attempt, return_wait=True)
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise SecurityError(
                    f"WooCommerce API error {resp.status_code}: {resp.text[:300]}"
                )
            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise SecurityError(
                    f"WooCommerce API returned non-JSON body: {resp.text[:200]}"
                ) from exc

        # Exhausted retries on a retryable status
        if last_exc is not None:
            raise SecurityError(f"WooCommerce transport error: {last_exc}")
        raise SecurityError(
            f"WooCommerce API error {resp.status_code} after {self.max_retries} retries: "
            f"{resp.text[:300]}"
        )

    def _retry_after(self, resp: requests.Response) -> float | None:
        """Return seconds to wait from Retry-After / RateLimit-Retry-After."""
        for header in ("Retry-After", "RateLimit-Retry-After"):
            val = resp.headers.get(header)
            if not val:
                continue
            try:
                return max(0.0, float(val))
            except ValueError:
                continue
        return None

    def _sleep_backoff(self, attempt: int, *, return_wait: bool = False) -> float | None:
        wait = self.backoff_base * (2 ** attempt)
        if not return_wait:
            time.sleep(wait)
            return None
        return wait


class InMemoryWooTransport:
    """Test double for WooCommerce without network.

    V2.1: extended to mock shipment-trackings, order notes, refunds,
    customers, coupons, reports, stock, products list, system_status,
    and webhook management endpoints.
    """

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {
            "1001": {
                "id": 1001,
                "status": "processing",
                "currency": "SEK",
                "total": "499.00",
                "date_created": "2026-07-10T12:00:00",
                "payment_method_title": "Klarna",
                "customer_note": "Ring på dörren",
                "billing": {
                    "email": "customer@example.com",
                    "city": "Stockholm",
                    "country": "SE",
                },
                "shipping_lines": [
                    {"method_title": "PostNord", "method_id": "flat_rate"},
                ],
                "line_items": [
                    {"name": "Azom Pro Headset", "quantity": 1},
                ],
                "meta_data": [
                    {"key": "tracking_number", "value": "JJFI123456789SE"},
                ],
            },
            "1002": {
                "id": 1002,
                "status": "pending",
                "currency": "NOK",
                "total": "299.00",
                "date_created": "2026-07-11T09:30:00",
                "payment_method_title": "Vipps",
                "billing": {
                    "email": "no@example.com",
                    "city": "Oslo",
                    "country": "NO",
                },
                "shipping_lines": [
                    {"method_title": "Bring", "method_id": "bring"},
                ],
            },
        }
        self.products: dict[str, dict[str, Any]] = {
            "501": {
                "id": 501,
                "name": "Azom Pro Headset",
                "description": "",
                "short_description": "",
                "type": "simple",
                "stock_status": "instock",
                "stock_quantity": 12,
            }
        }
        # V2.1 mock stores
        self.trackings: dict[str, list[dict[str, Any]]] = {
            "1001": [
                {
                    "id": "trk_1",
                    "tracking_number": "JJFI123456789SE",
                    "carrier": "PostNord",
                    "tracking_link": "https://postnord.no/track/JJFI123456789SE",
                    "date_shipped": "2026-07-12",
                }
            ],
            "1002": [
                {
                    "id": "trk_2",
                    "tracking_number": "BRING999NO",
                    "carrier": "Bring",
                    "tracking_link": "https://bring.no/track/BRING999NO",
                    "date_shipped": "2026-07-13",
                }
            ],
        }
        self.order_notes: dict[str, list[dict[str, Any]]] = {
            "1001": [
                {"id": 9001, "note": "Order mottagen", "customer_note": False},
            ],
        }
        self.refunds: dict[str, list[dict[str, Any]]] = {}
        self.customers: dict[str, dict[str, Any]] = {
            "1": {"id": 1, "email": "customer@example.com", "first_name": "Test"},
        }
        self.coupons: dict[str, dict[str, Any]] = {}
        self.webhooks: dict[str, dict[str, Any]] = {}
        self.system_status_payload: dict[str, Any] = {
            "version": "9.4.2",
            "environment": {
                "version": "6.5",
            },
            "active_plugins": [
                {"name": "WooCommerce Shipment Tracking"},
                {"name": "Google Listings & Ads"},
            ],
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
            # Shipment trackings: /orders/{id}/shipment-trackings[/{trk_id}]
            if "/shipment-trackings" in oid or "shipment-trackings" in path_tail:
                return self._handle_shipment_trackings(method, url, path_tail, json)
            if not oid:
                if method.upper() == "GET":
                    rows = list(self.orders.values())
                    rows = self._apply_list_params(rows, params)
                    return rows
                raise SecurityError(f"Unhandled mock URL: {method} {url}")
            # Order notes: /orders/{id}/notes
            if "/notes" in path_tail:
                return self._handle_order_notes(method, oid.split("/")[0], path_tail, json)
            # Refunds: /orders/{id}/refunds
            if "/refunds" in path_tail:
                return self._handle_refunds(method, oid.split("/")[0], path_tail, json)
            if method.upper() == "GET":
                if oid not in self.orders:
                    raise SecurityError(f"WooCommerce API error 404: order {oid}")
                return self.orders[oid]
            if method.upper() == "PUT":
                if oid not in self.orders:
                    raise SecurityError(f"WooCommerce API error 404: order {oid}")
                self.orders[oid] = {**self.orders[oid], **(json or {})}
                return self.orders[oid]
        if "/products/" in url or url.rstrip("/").endswith("/products"):
            return self._handle_products(method, url, json, params)
        if "/customers" in url:
            return self._handle_simple_collection(method, url, self.customers, json, params)
        if "/coupons" in url:
            return self._handle_simple_collection(method, url, self.coupons, json, params)
        if "/reports" in url:
            return [{"slug": "sales", "total": 12}]
        if "/system_status" in url:
            return self.system_status_payload
        if "/webhooks" in url:
            return self._handle_webhooks(method, url, json, params)
        raise SecurityError(f"Unhandled mock URL: {method} {url}")

    # --- helpers --------------------------------------------------------- #

    def _apply_list_params(
        self, rows: list[dict[str, Any]], params: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        params = params or {}
        search = str(params.get("search") or "").strip().lower()
        if search:
            rows = [
                r
                for r in rows
                if search
                in str((r.get("billing") or {}).get("email") or "").lower()
                or search in str(r.get("id", ""))
            ]
        per_page = int(params.get("per_page") or 10)
        page = int(params.get("page") or 1)
        start = (page - 1) * per_page
        return rows[start : start + per_page]

    def _handle_shipment_trackings(
        self,
        method: str,
        url: str,
        path_tail: str,
        json: dict[str, Any] | None,
    ) -> Any:
        # path_tail like "/1001/shipment-trackings" or "/1001/shipment-trackings/trk_1"
        parts = [p for p in path_tail.split("?")[0].split("/") if p]
        # parts[0] is after /orders, so it is the order id
        oid = parts[0]
        if len(parts) == 2:  # /orders/{id}/shipment-trackings
            if method.upper() == "GET":
                return list(self.trackings.get(oid, []))
            if method.upper() == "POST":
                row = dict(json or {})
                row.setdefault("id", f"trk_{len(self.trackings.get(oid, [])) + 1}")
                self.trackings.setdefault(oid, []).append(row)
                return row
        if len(parts) == 3:  # /orders/{id}/shipment-trackings/{trk_id}
            trk_id = parts[2]
            rows = self.trackings.get(oid, [])
            if method.upper() == "GET":
                for r in rows:
                    if str(r.get("id")) == str(trk_id):
                        return r
                raise SecurityError(f"WooCommerce API error 404: tracking {trk_id}")
            if method.upper() == "DELETE":
                self.trackings[oid] = [r for r in rows if str(r.get("id")) != str(trk_id)]
                return {"deleted": True, "id": trk_id}
        raise SecurityError(f"Unhandled mock tracking URL: {method} {url}")

    def _handle_order_notes(
        self,
        method: str,
        oid: str,
        path_tail: str,
        json: dict[str, Any] | None,
    ) -> Any:
        parts = [p for p in path_tail.split("?")[0].split("/") if p]
        if len(parts) == 2:  # /orders/{id}/notes
            if method.upper() == "GET":
                return list(self.order_notes.get(oid, []))
            if method.upper() == "POST":
                row = dict(json or {})
                row.setdefault("id", 9000 + len(self.order_notes.get(oid, [])) + 1)
                row.setdefault("customer_note", False)
                self.order_notes.setdefault(oid, []).append(row)
                return row
        if len(parts) == 3 and method.upper() == "DELETE":
            note_id = parts[2]
            self.order_notes[oid] = [
                n for n in self.order_notes.get(oid, []) if str(n.get("id")) != str(note_id)
            ]
            return {"deleted": True, "id": note_id}
        raise SecurityError(f"Unhandled mock order-notes URL: {method} notes")

    def _handle_refunds(
        self,
        method: str,
        oid: str,
        path_tail: str,
        json: dict[str, Any] | None,
    ) -> Any:
        parts = [p for p in path_tail.split("?")[0].split("/") if p]
        if len(parts) == 2 and method.upper() == "GET":
            return list(self.refunds.get(oid, []))
        if len(parts) == 2 and method.upper() == "POST":
            row = dict(json or {})
            row.setdefault("id", 7000 + len(self.refunds.get(oid, [])) + 1)
            self.refunds.setdefault(oid, []).append(row)
            return row
        raise SecurityError(f"Unhandled mock refunds URL: {method}")

    def _handle_products(
        self,
        method: str,
        url: str,
        json: dict[str, Any] | None,
        params: dict[str, Any] | None,
    ) -> Any:
        if url.rstrip("/").endswith("/products"):
            if method.upper() == "GET":
                rows = list(self.products.values())
                params = params or {}
                search = str(params.get("search") or "").lower()
                if search:
                    rows = [
                        r
                        for r in rows
                        if search in str(r.get("name", "")).lower()
                        or search in str(r.get("id", ""))
                    ]
                per_page = int(params.get("per_page") or 10)
                page = int(params.get("page") or 1)
                start = (page - 1) * per_page
                return rows[start : start + per_page]
            raise SecurityError(f"Unhandled mock products URL: {method} {url}")
        # /products/{id}[/variations|/...]
        pid = url.rstrip("/").split("/products/", 1)[-1].split("/")[0].split("?")[0]
        if "/variations" in url:
            if method.upper() == "GET":
                return []  # No variations in mock by default
            raise SecurityError(f"Unhandled mock variations URL: {method} {url}")
        if method.upper() == "GET":
            if pid not in self.products:
                raise SecurityError(f"WooCommerce API error 404: product {pid}")
            return self.products[pid]
        if method.upper() == "PUT":
            if pid not in self.products:
                raise SecurityError(f"WooCommerce API error 404: product {pid}")
            self.products[pid] = {**self.products[pid], **(json or {})}
            return self.products[pid]
        raise SecurityError(f"Unhandled mock products URL: {method} {url}")

    def _handle_simple_collection(
        self,
        method: str,
        url: str,
        store: dict[str, dict[str, Any]],
        json: dict[str, Any] | None,
        params: dict[str, Any] | None,
    ) -> Any:
        if url.rstrip("/").endswith("/customers") or url.rstrip("/").endswith("/coupons"):
            if method.upper() == "GET":
                return self._apply_list_params(list(store.values()), params)
            if method.upper() == "POST":
                row = dict(json or {})
                row.setdefault("id", 1000 + len(store) + 1)
                store[str(row["id"])] = row
                return row
        # /customers/{id} or /coupons/{id}
        cid = url.rstrip("/").split("/")[-1].split("?")[0]
        if method.upper() == "GET":
            if cid not in store:
                raise SecurityError(f"WooCommerce API error 404: {cid}")
            return store[cid]
        if method.upper() == "DELETE":
            if store.pop(cid, None):
                return {"deleted": True, "id": cid}
            raise SecurityError(f"WooCommerce API error 404: {cid}")
        raise SecurityError(f"Unhandled mock URL: {method} {url}")

    def _handle_webhooks(
        self,
        method: str,
        url: str,
        json: dict[str, Any] | None,
        params: dict[str, Any] | None,
    ) -> Any:
        if url.rstrip("/").endswith("/webhooks"):
            if method.upper() == "GET":
                return self._apply_list_params(list(self.webhooks.values()), params)
            if method.upper() == "POST":
                row = dict(json or {})
                row.setdefault("id", 5000 + len(self.webhooks) + 1)
                row.setdefault("status", "active")
                row.setdefault("delivery_url", "")
                self.webhooks[str(row["id"])] = row
                return row
        wid = url.rstrip("/").split("/webhooks/", 1)[-1].split("/")[0].split("?")[0]
        if method.upper() == "GET":
            if wid not in self.webhooks:
                raise SecurityError(f"WooCommerce API error 404: webhook {wid}")
            return self.webhooks[wid]
        if method.upper() == "DELETE":
            self.webhooks.pop(wid, None)
            return {"deleted": True, "id": wid}
        raise SecurityError(f"Unhandled mock webhooks URL: {method} {url}")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class WooCommerceClient:
    def __init__(
        self,
        *,
        base_url: str,
        consumer_key: str | None = None,
        consumer_secret: str | None = None,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
        domain: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.domain = domain
        self.consumer_key = consumer_key or get_env("WOO_CONSUMER_KEY", "")
        self.consumer_secret = consumer_secret or get_env("WOO_CONSUMER_SECRET", "")
        self.transport = transport or RequestsTransport(default_timeout=timeout)
        self.timeout = float(timeout)
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

    # --- orders ---------------------------------------------------------- #

    def get_order(self, order_id: str | int) -> WooOrder:
        oid = validate_order_id(order_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wc/v3/orders/{oid}"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return self._to_order(data)

    def list_orders(
        self,
        *,
        per_page: int = 1,
        page: int = 1,
        status: str | None = None,
        search: str | None = None,
    ) -> list[WooOrder]:
        """List orders (single page). Use ``list_all_orders`` for full pagination."""
        params: dict[str, Any] = {
            "per_page": max(1, min(int(per_page), 100)),
            "page": max(1, int(page)),
        }
        if status:
            params["status"] = status
        if search:
            params["search"] = search
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/orders"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        if not isinstance(data, list):
            return []
        return [self._to_order(row) for row in data if isinstance(row, dict)]

    def list_all_orders(
        self,
        *,
        per_page: int = 100,
        max_pages: int = 50,
        status: str | None = None,
    ) -> Iterator[WooOrder]:
        """Paginate through all orders (bounded by ``max_pages``)."""
        for page in range(1, max_pages + 1):
            batch = self.list_orders(per_page=per_page, page=page, status=status)
            if not batch:
                return
            for order in batch:
                yield order
            if len(batch) < per_page:
                return

    def find_orders_by_email(
        self,
        email: str,
        *,
        per_page: int = 5,
    ) -> list[WooOrder]:
        """Search recent orders by billing email (read-only).

        Woo REST: GET /orders?search=<email>&per_page=N (and filter in-memory
        by billing.email for mock / noisy search results).
        """
        email_l = (email or "").strip().lower()
        if not email_l or "@" not in email_l:
            return []
        limit = max(1, min(int(per_page), 20))
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/orders"),
            auth=self._auth(),
            params={
                "search": email_l,
                "per_page": limit,
                "orderby": "date",
                "order": "desc",
            },
            timeout=self.timeout,
        )
        if not isinstance(data, list):
            data = []
        orders = [self._to_order(row) for row in data if isinstance(row, dict)]
        matched = [
            o
            for o in orders
            if (o.billing_email or "").strip().lower() == email_l
        ]
        if matched:
            return matched[:limit]
        if isinstance(self.transport, InMemoryWooTransport):
            all_orders = [
                self._to_order(row) for row in self.transport.orders.values()
            ]
            return [
                o
                for o in all_orders
                if (o.billing_email or "").strip().lower() == email_l
            ][:limit]
        return orders[:limit]

    def update_order_status(self, order_id: str | int, status: str) -> WooOrder:
        oid = validate_order_id(order_id)
        st = validate_order_status(status)
        data = self.transport.request(
            "PUT",
            self._url(f"/wp-json/wc/v3/orders/{oid}"),
            auth=self._auth(),
            json={"status": st},
            timeout=self.timeout,
        )
        return self._to_order(data)

    # --- shipment trackings (P0.1) --------------------------------------- #

    def list_shipment_trackings(self, order_id: str | int) -> list[ShipmentTracking]:
        """List shipment trackings via the dedicated Woo REST endpoint.

        Replaces the fragile meta_data heuristics in ``order_context``.
        Endpoint: GET /wp-json/wc/v3/orders/{id}/shipment-trackings
        """
        oid = validate_order_id(order_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wc/v3/orders/{oid}/shipment-trackings"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        if not isinstance(data, list):
            return []
        return [self._to_tracking(row) for row in data if isinstance(row, dict)]

    def add_shipment_tracking(
        self,
        order_id: str | int,
        *,
        tracking_number: str,
        carrier: str | None = None,
        tracking_link: str | None = None,
        date_shipped: str | None = None,
    ) -> ShipmentTracking:
        """Add a shipment tracking entry (POST /orders/{id}/shipment-trackings)."""
        oid = validate_order_id(order_id)
        payload: dict[str, Any] = {"tracking_number": tracking_number}
        if carrier:
            payload["carrier"] = carrier
        if tracking_link:
            payload["tracking_link"] = tracking_link
        if date_shipped:
            payload["date_shipped"] = date_shipped
        data = self.transport.request(
            "POST",
            self._url(f"/wp-json/wc/v3/orders/{oid}/shipment-trackings"),
            auth=self._auth(),
            json=payload,
            timeout=self.timeout,
        )
        return self._to_tracking(data)

    def delete_shipment_tracking(
        self, order_id: str | int, tracking_id: str
    ) -> dict[str, Any]:
        oid = validate_order_id(order_id)
        return self.transport.request(
            "DELETE",
            self._url(
                f"/wp-json/wc/v3/orders/{oid}/shipment-trackings/{tracking_id}"
            ),
            auth=self._auth(),
            timeout=self.timeout,
        )

    # --- order notes / refunds ------------------------------------------- #

    def list_order_notes(self, order_id: str | int) -> list[dict[str, Any]]:
        oid = validate_order_id(order_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wc/v3/orders/{oid}/notes"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    def add_order_note(
        self,
        order_id: str | int,
        note: str,
        *,
        customer_note: bool = False,
    ) -> dict[str, Any]:
        oid = validate_order_id(order_id)
        data = self.transport.request(
            "POST",
            self._url(f"/wp-json/wc/v3/orders/{oid}/notes"),
            auth=self._auth(),
            json={"note": note, "customer_note": bool(customer_note)},
            timeout=self.timeout,
        )
        return data if isinstance(data, dict) else {}

    def list_refunds(self, order_id: str | int) -> list[dict[str, Any]]:
        oid = validate_order_id(order_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wc/v3/orders/{oid}/refunds"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    # --- products -------------------------------------------------------- #

    def get_product(self, product_id: str | int) -> dict[str, Any]:
        pid = validate_order_id(product_id)  # same digit rules
        return self.transport.request(
            "GET",
            self._url(f"/wp-json/wc/v3/products/{pid}"),
            auth=self._auth(),
            timeout=self.timeout,
        )

    def list_products(
        self,
        *,
        per_page: int = 10,
        page: int = 1,
        search: str | None = None,
        status: str = "publish",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "per_page": max(1, min(int(per_page), 100)),
            "page": max(1, int(page)),
            "status": status,
        }
        if search:
            params["search"] = search
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/products"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    def list_all_products(
        self,
        *,
        per_page: int = 100,
        max_pages: int = 50,
        search: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Paginate through all products (bounded by ``max_pages``)."""
        for page in range(1, max_pages + 1):
            batch = self.list_products(per_page=per_page, page=page, search=search)
            if not batch:
                return
            for product in batch:
                yield product
            if len(batch) < per_page:
                return

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
            timeout=self.timeout,
        )

    def list_product_variations(self, product_id: str | int) -> list[dict[str, Any]]:
        pid = validate_order_id(product_id)
        data = self.transport.request(
            "GET",
            self._url(f"/wp-json/wc/v3/products/{pid}/variations"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    # --- customers / coupons / reports ----------------------------------- #

    def list_customers(
        self, *, per_page: int = 10, page: int = 1, search: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "per_page": max(1, min(int(per_page), 100)),
            "page": max(1, int(page)),
        }
        if search:
            params["search"] = search
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/customers"),
            auth=self._auth(),
            params=params,
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    def list_coupons(
        self, *, per_page: int = 10, page: int = 1
    ) -> list[dict[str, Any]]:
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/coupons"),
            auth=self._auth(),
            params={
                "per_page": max(1, min(int(per_page), 100)),
                "page": max(1, int(page)),
            },
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    def list_reports(self) -> list[dict[str, Any]]:
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/reports"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    # --- system status (P3.9 version detection) ------------------------- #

    def get_system_status(self) -> WooSystemStatus:
        """Fetch /wc/v3/system_status (requires auth). Used for version
        detection and plugin inventory."""
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/system_status"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        if not isinstance(data, dict):
            return WooSystemStatus(version=None, wordpress_version=None, raw=data)
        env = data.get("environment") or {}
        plugins = data.get("active_plugins") or []
        plugin_names: list[str] = []
        if isinstance(plugins, list):
            for p in plugins:
                if isinstance(p, dict):
                    name = p.get("name") or p.get("plugin")
                    if name:
                        plugin_names.append(str(name))
                elif isinstance(p, str):
                    plugin_names.append(p)
        return WooSystemStatus(
            version=str(data.get("version") or "") or None,
            wordpress_version=str(env.get("version") or "") or None,
            active_plugins=plugin_names,
            raw=data,
        )

    # --- webhooks management --------------------------------------------- #

    def list_webhooks(self) -> list[dict[str, Any]]:
        data = self.transport.request(
            "GET",
            self._url("/wp-json/wc/v3/webhooks"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return data if isinstance(data, list) else []

    def create_webhook(
        self,
        *,
        topic: str,
        delivery_url: str,
        secret: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "topic": topic,
            "delivery_url": delivery_url,
            "status": "active",
        }
        if secret:
            payload["secret"] = secret
        if name:
            payload["name"] = name
        data = self.transport.request(
            "POST",
            self._url("/wp-json/wc/v3/webhooks"),
            auth=self._auth(),
            json=payload,
            timeout=self.timeout,
        )
        return data if isinstance(data, dict) else {}

    def delete_webhook(self, webhook_id: str | int) -> dict[str, Any]:
        data = self.transport.request(
            "DELETE",
            self._url(f"/wp-json/wc/v3/webhooks/{webhook_id}"),
            auth=self._auth(),
            timeout=self.timeout,
        )
        return data if isinstance(data, dict) else {}

    # --- converters ------------------------------------------------------ #

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

    @staticmethod
    def _to_tracking(data: dict[str, Any]) -> ShipmentTracking:
        return ShipmentTracking(
            tracking_id=str(data.get("id") or data.get("tracking_id") or ""),
            tracking_number=str(data.get("tracking_number") or "") or None,
            carrier=str(data.get("carrier") or data.get("custom_tracking_provider") or "") or None,
            link=str(data.get("tracking_link") or data.get("custom_tracking_link") or "") or None,
            date_shipped=str(data.get("date_shipped") or "") or None,
            raw=data,
        )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def client_from_env(
    *,
    base_url: str | None = None,
    use_mock: bool | None = None,
    domain: str | None = None,
    timeout: float = 30.0,
) -> WooCommerceClient:
    """Build a WooCommerceClient from env.

    Multi-site (P0.2): when ``domain`` is given (e.g. ``"no"``, ``"se"``,
    ``"dk"``), the base URL is resolved via ``woo_base_url_for_domain`` —
    honoring ``WOO_BASE_URL_{DOMAIN}`` overrides then ``WOO_BASE_URL`` then
    the ``https://azom.{domain}`` convention.
    """
    mock = use_mock
    if mock is None:
        mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    if mock:
        return WooCommerceClient(
            base_url=base_url or "https://mock.local",
            transport=InMemoryWooTransport(),
            timeout=timeout,
            domain=domain,
        )
    # Live: resolve base_url with multi-site support
    if base_url is None:
        if domain:
            from ecom_ops.config import woo_base_url_for_domain

            base_url = woo_base_url_for_domain(domain)
        else:
            base_url = get_env("WOO_BASE_URL", "https://localhost") or "https://localhost"
    return WooCommerceClient(
        base_url=base_url,
        timeout=timeout,
        domain=domain,
    )
