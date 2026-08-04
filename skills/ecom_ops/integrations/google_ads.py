"""Google Ads API client (GAQL + mutate) — mock-first."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from ecom_ops.marketing.config import load_marketing_config


def _use_mock(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


class GoogleAdsTransport(Protocol):
    def campaign_performance(
        self, customer_id: str, *, days: int
    ) -> dict[str, Any]: ...

    def search_term_waste(
        self, customer_id: str, *, days: int, min_cost_micros: int
    ) -> list[dict[str, Any]]: ...

    def budget_pacing(
        self, customer_id: str
    ) -> list[dict[str, Any]]: ...

    def shopping_products(
        self, customer_id: str
    ) -> list[dict[str, Any]]: ...

    def final_urls(
        self, customer_id: str
    ) -> list[str]: ...

    def change_events(
        self, customer_id: str, *, days: int
    ) -> list[dict[str, Any]]: ...

    def mutate(
        self, customer_id: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]: ...


@dataclass
class InMemoryGoogleAdsTransport:
    cost_micros: int = 48_000_000_00  # 480 SEK if micros
    clicks: int = 920
    conversions: float = 38.0
    conversions_value: float = 98_500.0
    currency: str = "SEK"
    mutates: list[dict[str, Any]] = field(default_factory=list)

    def campaign_performance(
        self, customer_id: str, *, days: int
    ) -> dict[str, Any]:
        end = date.today()
        start = end - timedelta(days=max(1, days) - 1)
        cost = self.cost_micros / 1_000_000
        roas = (
            (self.conversions_value / cost) if cost > 0 else None
        )
        return {
            "customer_id": customer_id,
            "source": "google_ads_api",
            "attribution": "ads_reported",
            "date_range": {"start": start.isoformat(), "end": end.isoformat()},
            "cost": round(cost, 2),
            "cost_micros": self.cost_micros,
            "clicks": self.clicks,
            "conversions": self.conversions,
            "conversions_value": self.conversions_value,
            "roas_ads_reported": round(roas, 2) if roas is not None else None,
            "currency": self.currency,
            "campaigns": [
                {
                    "id": "111",
                    "name": "SE Search Brand",
                    "status": "ENABLED",
                    "channel": "SEARCH",
                    "cost": round(cost * 0.4, 2),
                    "conversions": 20,
                },
                {
                    "id": "222",
                    "name": "SE PMax",
                    "status": "ENABLED",
                    "channel": "PERFORMANCE_MAX",
                    "cost": round(cost * 0.6, 2),
                    "conversions": 18,
                },
            ],
        }

    def search_term_waste(
        self, customer_id: str, *, days: int, min_cost_micros: int
    ) -> list[dict[str, Any]]:
        _ = customer_id, days
        rows = [
            {
                "search_term": "gratis frakt konkurrent",
                "cost_micros": 12_000_000,
                "conversions": 0,
                "campaign_id": "222",
            },
            {
                "search_term": "jobb stockholm",
                "cost_micros": 8_500_000,
                "conversions": 0,
                "campaign_id": "111",
            },
            {
                "search_term": "azom orderstatus",
                "cost_micros": 2_000_000,
                "conversions": 3,
                "campaign_id": "111",
            },
        ]
        return [
            r
            for r in rows
            if r["conversions"] == 0 and int(r["cost_micros"]) >= min_cost_micros
        ]

    def budget_pacing(self, customer_id: str) -> list[dict[str, Any]]:
        _ = customer_id
        return [
            {
                "campaign_id": "222",
                "campaign_name": "SE PMax",
                "budget_amount": 200.0,
                "cost_to_date": 180.0,
                "days_elapsed": 5,
                "days_in_period": 7,
                "pacing_ratio": 1.26,
            }
        ]

    def shopping_products(self, customer_id: str) -> list[dict[str, Any]]:
        _ = customer_id
        return [
            {
                "item_id": "SKU-1001",
                "title": "Demo produkt",
                "status": "ELIGIBLE",
                "issues": [],
            },
            {
                "item_id": "SKU-1002",
                "title": "Saknad bild",
                "status": "NOT_ELIGIBLE",
                "issues": ["image_missing"],
            },
        ]

    def final_urls(self, customer_id: str) -> list[str]:
        _ = customer_id
        return [
            "https://azom.se/produkt/demo-sku",
            "https://azom.se/kampanj/404-test",
        ]

    def change_events(
        self, customer_id: str, *, days: int
    ) -> list[dict[str, Any]]:
        _ = customer_id, days
        return [
            {
                "change_date_time": "2026-08-01T10:00:00Z",
                "user_email": "oscar@example.com",
                "resource": "campaign",
                "old_resource": "ENABLED",
                "new_resource": "PAUSED",
            }
        ]

    def mutate(
        self, customer_id: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        entry = {"customer_id": customer_id, "operations": operations}
        self.mutates.append(entry)
        return {"ok": True, "applied": len(operations), "mock": True}


class LiveGoogleAdsTransport:
    def __init__(self, *, developer_token: str, access_token: str) -> None:
        self.developer_token = developer_token
        self.access_token = access_token

    def campaign_performance(
        self, customer_id: str, *, days: int
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "Live Google Ads GAQL requires google-ads client; use mock"
        )

    def search_term_waste(
        self, customer_id: str, *, days: int, min_cost_micros: int
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("Live search terms not wired")

    def budget_pacing(self, customer_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError("Live budget pacing not wired")

    def shopping_products(self, customer_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError("Live shopping_product not wired")

    def final_urls(self, customer_id: str) -> list[str]:
        raise NotImplementedError("Live final URLs not wired")

    def change_events(
        self, customer_id: str, *, days: int
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("Live change_event not wired")

    def mutate(
        self, customer_id: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        raise NotImplementedError("Live Ads mutate not wired")


@dataclass
class GoogleAdsClient:
    transport: GoogleAdsTransport
    customer_ids: tuple[str, ...]

    def assert_allowed(self, customer_id: str | None = None) -> str:
        cfg = load_marketing_config()
        allowed = self.customer_ids or cfg.google_ads_customer_ids
        # Fail-closed from env mock flag — never bypass via transport type alone
        mock = _use_mock(None)
        if not allowed and not mock:
            raise PermissionError(
                "AZOM_GADS_CUSTOMER_IDS empty — fail-closed in live"
            )
        cid = (
            customer_id or (allowed[0] if allowed else "1234567890")
        ).replace("-", "")
        if allowed and cid not in {a.replace("-", "") for a in allowed} and not mock:
            raise PermissionError(f"Ads customer {cid} not in allowlist")
        return cid

    def digest(
        self, *, days: int = 7, customer_id: str | None = None
    ) -> dict[str, Any]:
        cid = self.assert_allowed(customer_id)
        return self.transport.campaign_performance(cid, days=days)

    def waste_report(
        self,
        *,
        days: int = 7,
        customer_id: str | None = None,
        min_cost_micros: int | None = None,
    ) -> list[dict[str, Any]]:
        cfg = load_marketing_config()
        cid = self.assert_allowed(customer_id)
        return self.transport.search_term_waste(
            cid,
            days=days,
            min_cost_micros=min_cost_micros or cfg.waste_min_cost_micros,
        )

    def pacing(
        self, *, customer_id: str | None = None
    ) -> list[dict[str, Any]]:
        cid = self.assert_allowed(customer_id)
        return self.transport.budget_pacing(cid)

    def shopping(
        self, *, customer_id: str | None = None
    ) -> list[dict[str, Any]]:
        cid = self.assert_allowed(customer_id)
        return self.transport.shopping_products(cid)

    def final_urls(self, *, customer_id: str | None = None) -> list[str]:
        cid = self.assert_allowed(customer_id)
        return self.transport.final_urls(cid)

    def change_history(
        self, *, days: int = 7, customer_id: str | None = None
    ) -> list[dict[str, Any]]:
        cid = self.assert_allowed(customer_id)
        return self.transport.change_events(cid, days=days)

    def mutate(
        self,
        operations: list[dict[str, Any]],
        *,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        cid = self.assert_allowed(customer_id)
        return self.transport.mutate(cid, operations)


def client_from_env(
    *,
    use_mock: bool | None = None,
    transport: GoogleAdsTransport | None = None,
) -> GoogleAdsClient:
    cfg = load_marketing_config()
    if transport is not None:
        return GoogleAdsClient(
            transport=transport, customer_ids=cfg.google_ads_customer_ids
        )
    if _use_mock(use_mock):
        return GoogleAdsClient(
            transport=InMemoryGoogleAdsTransport(),
            customer_ids=cfg.google_ads_customer_ids or ("1234567890",),
        )
    dev = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
    if not token:
        from ecom_ops.oauth.google_marketing import GoogleMarketingOAuthStore

        bundle = GoogleMarketingOAuthStore().load_tokens()
        token = (bundle.access_token if bundle else "") or ""
    return GoogleAdsClient(
        transport=LiveGoogleAdsTransport(
            developer_token=dev, access_token=token
        ),
        customer_ids=cfg.google_ads_customer_ids,
    )
