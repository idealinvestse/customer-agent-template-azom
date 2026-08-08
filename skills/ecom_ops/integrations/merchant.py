"""Google Merchant / Content API client — mock-first; live via REST."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import requests


def _use_mock(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


CONTENT_API_BASE = "https://shoppingcontent.googleapis.com/content/v2.1"


class MerchantTransport(Protocol):
    def list_products(self) -> list[dict[str, Any]]: ...

    def upsert_product(self, product: dict[str, Any]) -> dict[str, Any]: ...

    def delete_product(self, product_id: str) -> dict[str, Any]: ...


@dataclass
class InMemoryMerchantTransport:
    products: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "id": "online:sv:SE:SKU-1001",
                "offerId": "SKU-1001",
                "title": "Demo produkt",
                "availability": "in stock",
                "channel": "online",
            }
        ]
    )
    writes: list[dict[str, Any]] = field(default_factory=list)

    def list_products(self) -> list[dict[str, Any]]:
        return list(self.products)

    def upsert_product(self, product: dict[str, Any]) -> dict[str, Any]:
        self.writes.append({"op": "upsert", "product": product})
        oid = str(product.get("offerId") or product.get("id") or "")
        replaced = False
        for i, p in enumerate(self.products):
            if p.get("offerId") == oid or p.get("id") == product.get("id"):
                self.products[i] = {**p, **product}
                replaced = True
                break
        if not replaced:
            self.products.append(product)
        return {"ok": True, "mock": True, "product": product}

    def delete_product(self, product_id: str) -> dict[str, Any]:
        self.writes.append({"op": "delete", "id": product_id})
        self.products = [
            p
            for p in self.products
            if p.get("id") != product_id and p.get("offerId") != product_id
        ]
        return {"ok": True, "mock": True, "deleted": product_id}


class LiveMerchantTransport:
    """Content API for Shopping — needs OAuth + ``GOOGLE_MERCHANT_ID``."""

    def __init__(
        self,
        *,
        access_token: str,
        merchant_id: str,
        session: requests.Session | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.access_token = (access_token or "").strip()
        self.merchant_id = (merchant_id or "").strip()
        self.session = session or requests.Session()
        self.timeout = timeout

    def _require_creds(self) -> None:
        if not self.access_token:
            raise RuntimeError(
                "Google marketing OAuth access_token required for live Merchant"
            )
        if not self.merchant_id:
            raise RuntimeError("GOOGLE_MERCHANT_ID required for live Merchant")

    def _headers(self) -> dict[str, str]:
        self._require_creds()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _base(self) -> str:
        self._require_creds()
        return f"{CONTENT_API_BASE}/{self.merchant_id}"

    def list_products(self) -> list[dict[str, Any]]:
        url = f"{self._base()}/products"
        resp = self.session.get(
            url, headers=self._headers(), timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json()
        resources = data.get("resources") or data.get("products") or []
        return [r for r in resources if isinstance(r, dict)]

    def upsert_product(self, product: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base()}/products"
        resp = self.session.post(
            url,
            headers=self._headers(),
            json=product,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        return {"ok": True, "product": data or product}

    def delete_product(self, product_id: str) -> dict[str, Any]:
        encoded = quote(product_id, safe="")
        url = f"{self._base()}/products/{encoded}"
        resp = self.session.delete(
            url, headers=self._headers(), timeout=self.timeout
        )
        if resp.status_code not in {200, 204}:
            resp.raise_for_status()
        return {"ok": True, "deleted": product_id}


@dataclass
class MerchantClient:
    transport: MerchantTransport

    def list_products(self) -> list[dict[str, Any]]:
        return self.transport.list_products()

    def upsert_product(self, product: dict[str, Any]) -> dict[str, Any]:
        return self.transport.upsert_product(product)

    def delete_product(self, product_id: str) -> dict[str, Any]:
        return self.transport.delete_product(product_id)


def client_from_env(
    *,
    use_mock: bool | None = None,
    transport: MerchantTransport | None = None,
) -> MerchantClient:
    if transport is not None:
        return MerchantClient(transport=transport)
    if _use_mock(use_mock):
        return MerchantClient(transport=InMemoryMerchantTransport())
    from ecom_ops.oauth.google_marketing import ensure_fresh_access_token

    token = ensure_fresh_access_token()
    merchant_id = os.environ.get("GOOGLE_MERCHANT_ID", "").strip()
    return MerchantClient(
        transport=LiveMerchantTransport(
            access_token=token, merchant_id=merchant_id
        )
    )
