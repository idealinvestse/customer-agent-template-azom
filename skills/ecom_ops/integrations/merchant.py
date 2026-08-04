"""Google Merchant / Content API client — mock-first."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol


def _use_mock(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


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
    def list_products(self) -> list[dict[str, Any]]:
        raise NotImplementedError("Live Merchant API list not wired")

    def upsert_product(self, product: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Live Merchant upsert not wired")

    def delete_product(self, product_id: str) -> dict[str, Any]:
        raise NotImplementedError("Live Merchant delete not wired")


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
    return MerchantClient(transport=LiveMerchantTransport())
