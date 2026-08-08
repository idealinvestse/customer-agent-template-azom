"""Google Ads API client (GAQL + mutate) — mock-first; live via REST."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

import requests

from ecom_ops.marketing.config import load_marketing_config

ADS_API_VERSION = "v17"
ADS_API_BASE = f"https://googleads.googleapis.com/{ADS_API_VERSION}"


def _use_mock(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


def _date_range_clause(days: int) -> str:
    end = date.today()
    start = end - timedelta(days=max(1, days) - 1)
    return f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"


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
    """Google Ads REST (search + mutate). Needs developer token + OAuth."""

    def __init__(
        self,
        *,
        developer_token: str,
        access_token: str,
        login_customer_id: str = "",
        session: requests.Session | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.developer_token = (developer_token or "").strip()
        self.access_token = (access_token or "").strip()
        self.login_customer_id = (login_customer_id or "").replace("-", "").strip()
        self.session = session or requests.Session()
        self.timeout = timeout

    def _require_creds(self) -> None:
        if not self.developer_token:
            raise RuntimeError(
                "GOOGLE_ADS_DEVELOPER_TOKEN required for live Google Ads"
            )
        if not self.access_token:
            raise RuntimeError(
                "Google marketing OAuth access_token required for live Ads"
            )

    def _headers(self) -> dict[str, str]:
        self._require_creds()
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "developer-token": self.developer_token,
            "Content-Type": "application/json",
        }
        if self.login_customer_id:
            headers["login-customer-id"] = self.login_customer_id
        return headers

    def _search(self, customer_id: str, query: str) -> list[dict[str, Any]]:
        self._require_creds()
        cid = customer_id.replace("-", "")
        url = f"{ADS_API_BASE}/customers/{cid}/googleAds:search"
        resp = self.session.post(
            url,
            headers=self._headers(),
            json={"query": query},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        return [r for r in results if isinstance(r, dict)]

    def campaign_performance(
        self, customer_id: str, *, days: int
    ) -> dict[str, Any]:
        end = date.today()
        start = end - timedelta(days=max(1, days) - 1)
        query = (
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.advertising_channel_type, metrics.cost_micros, "
            "metrics.clicks, metrics.conversions, metrics.conversions_value "
            f"FROM campaign WHERE {_date_range_clause(days)}"
        )
        rows = self._search(customer_id, query)
        cost_micros = 0
        clicks = 0
        conversions = 0.0
        conversions_value = 0.0
        campaigns: list[dict[str, Any]] = []
        for row in rows:
            camp = row.get("campaign") or {}
            metrics = row.get("metrics") or {}
            c_micros = int(metrics.get("costMicros") or metrics.get("cost_micros") or 0)
            conv = float(metrics.get("conversions") or 0)
            cost_micros += c_micros
            clicks += int(metrics.get("clicks") or 0)
            conversions += conv
            conversions_value += float(
                metrics.get("conversionsValue")
                or metrics.get("conversions_value")
                or 0
            )
            campaigns.append(
                {
                    "id": str(camp.get("id") or ""),
                    "name": str(camp.get("name") or ""),
                    "status": str(camp.get("status") or ""),
                    "channel": str(
                        camp.get("advertisingChannelType")
                        or camp.get("advertising_channel_type")
                        or ""
                    ),
                    "cost": round(c_micros / 1_000_000, 2),
                    "conversions": conv,
                }
            )
        cost = cost_micros / 1_000_000
        roas = (conversions_value / cost) if cost > 0 else None
        return {
            "customer_id": customer_id.replace("-", ""),
            "source": "google_ads_api",
            "attribution": "ads_reported",
            "date_range": {"start": start.isoformat(), "end": end.isoformat()},
            "cost": round(cost, 2),
            "cost_micros": cost_micros,
            "clicks": clicks,
            "conversions": conversions,
            "conversions_value": conversions_value,
            "roas_ads_reported": round(roas, 2) if roas is not None else None,
            "currency": load_marketing_config().default_currency,
            "campaigns": campaigns,
        }

    def search_term_waste(
        self, customer_id: str, *, days: int, min_cost_micros: int
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT search_term_view.search_term, campaign.id, "
            "metrics.cost_micros, metrics.conversions "
            f"FROM search_term_view WHERE {_date_range_clause(days)}"
        )
        rows = self._search(customer_id, query)
        out: list[dict[str, Any]] = []
        for row in rows:
            st = row.get("searchTermView") or row.get("search_term_view") or {}
            metrics = row.get("metrics") or {}
            camp = row.get("campaign") or {}
            cost_m = int(metrics.get("costMicros") or metrics.get("cost_micros") or 0)
            conv = float(metrics.get("conversions") or 0)
            if conv != 0 or cost_m < min_cost_micros:
                continue
            out.append(
                {
                    "search_term": str(st.get("searchTerm") or st.get("search_term") or ""),
                    "cost_micros": cost_m,
                    "conversions": conv,
                    "campaign_id": str(camp.get("id") or ""),
                }
            )
        return out

    def budget_pacing(self, customer_id: str) -> list[dict[str, Any]]:
        query = (
            "SELECT campaign.id, campaign.name, campaign_budget.amount_micros, "
            "metrics.cost_micros "
            "FROM campaign WHERE campaign.status = 'ENABLED'"
        )
        rows = self._search(customer_id, query)
        today = date.today()
        days_in_period = 7
        days_elapsed = min(today.day, days_in_period) or 1
        out: list[dict[str, Any]] = []
        for row in rows:
            camp = row.get("campaign") or {}
            budget = row.get("campaignBudget") or row.get("campaign_budget") or {}
            metrics = row.get("metrics") or {}
            budget_amount = int(
                budget.get("amountMicros") or budget.get("amount_micros") or 0
            ) / 1_000_000
            cost_to_date = int(
                metrics.get("costMicros") or metrics.get("cost_micros") or 0
            ) / 1_000_000
            expected = budget_amount * (days_elapsed / days_in_period) if budget_amount else 0
            ratio = (cost_to_date / expected) if expected > 0 else 0.0
            out.append(
                {
                    "campaign_id": str(camp.get("id") or ""),
                    "campaign_name": str(camp.get("name") or ""),
                    "budget_amount": round(budget_amount, 2),
                    "cost_to_date": round(cost_to_date, 2),
                    "days_elapsed": days_elapsed,
                    "days_in_period": days_in_period,
                    "pacing_ratio": round(ratio, 2),
                }
            )
        return out

    def shopping_products(self, customer_id: str) -> list[dict[str, Any]]:
        query = (
            "SELECT shopping_product.item_id, shopping_product.title, "
            "shopping_product.status, shopping_product.issues "
            "FROM shopping_product LIMIT 100"
        )
        try:
            rows = self._search(customer_id, query)
        except requests.HTTPError:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            sp = row.get("shoppingProduct") or row.get("shopping_product") or {}
            issues = sp.get("issues") or []
            if not isinstance(issues, list):
                issues = [issues]
            out.append(
                {
                    "item_id": str(sp.get("itemId") or sp.get("item_id") or ""),
                    "title": str(sp.get("title") or ""),
                    "status": str(sp.get("status") or ""),
                    "issues": [str(i) for i in issues],
                }
            )
        return out

    def final_urls(self, customer_id: str) -> list[str]:
        query = (
            "SELECT ad_group_ad.ad.final_urls FROM ad_group_ad "
            "WHERE ad_group_ad.status != 'REMOVED' LIMIT 200"
        )
        try:
            rows = self._search(customer_id, query)
        except requests.HTTPError:
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for row in rows:
            ad = (
                (row.get("adGroupAd") or row.get("ad_group_ad") or {}).get("ad")
                or {}
            )
            for u in ad.get("finalUrls") or ad.get("final_urls") or []:
                s = str(u)
                if s and s not in seen:
                    seen.add(s)
                    urls.append(s)
        return urls

    def change_events(
        self, customer_id: str, *, days: int
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT change_event.change_date_time, change_event.user_email, "
            "change_event.resource_change_operation, "
            "change_event.old_resource, change_event.new_resource "
            f"FROM change_event WHERE {_date_range_clause(days)} "
            "ORDER BY change_event.change_date_time DESC LIMIT 50"
        )
        try:
            rows = self._search(customer_id, query)
        except requests.HTTPError:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            ev = row.get("changeEvent") or row.get("change_event") or {}
            out.append(
                {
                    "change_date_time": str(
                        ev.get("changeDateTime") or ev.get("change_date_time") or ""
                    ),
                    "user_email": str(ev.get("userEmail") or ev.get("user_email") or ""),
                    "resource": str(
                        ev.get("resourceChangeOperation")
                        or ev.get("resource_change_operation")
                        or ""
                    ),
                    "old_resource": str(ev.get("oldResource") or ev.get("old_resource") or ""),
                    "new_resource": str(ev.get("newResource") or ev.get("new_resource") or ""),
                }
            )
        return out

    def _op_to_mutate(self, customer_id: str, op: dict[str, Any]) -> dict[str, Any] | None:
        cid = customer_id.replace("-", "")
        kind = str(op.get("op") or "")
        if kind == "pause_campaign":
            camp_id = str(op.get("campaign_id") or "")
            if not camp_id:
                return None
            return {
                "campaignOperation": {
                    "update": {
                        "resourceName": f"customers/{cid}/campaigns/{camp_id}",
                        "status": "PAUSED",
                    },
                    "updateMask": "status",
                }
            }
        if kind == "add_negative_keyword":
            camp_id = str(op.get("campaign_id") or "")
            term = str(op.get("search_term") or "").strip()
            if not camp_id or not term:
                return None
            return {
                "campaignCriterionOperation": {
                    "create": {
                        "campaign": f"customers/{cid}/campaigns/{camp_id}",
                        "negative": True,
                        "keyword": {"text": term, "matchType": "PHRASE"},
                    }
                }
            }
        if kind == "adjust_budget":
            budget_rn = str(op.get("budget_resource_name") or "")
            amount_micros = op.get("amount_micros")
            if not budget_rn or amount_micros is None:
                # Soft-ack when suggest only carries review hint
                return None
            return {
                "campaignBudgetOperation": {
                    "update": {
                        "resourceName": budget_rn,
                        "amountMicros": str(int(amount_micros)),
                    },
                    "updateMask": "amountMicros",
                }
            }
        return None

    def mutate(
        self, customer_id: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        cid = customer_id.replace("-", "")
        mutate_ops: list[dict[str, Any]] = []
        skipped: list[str] = []
        for op in operations:
            mapped = self._op_to_mutate(cid, op)
            if mapped is None:
                skipped.append(str(op.get("op") or "unknown"))
                continue
            mutate_ops.append(mapped)
        if not mutate_ops:
            return {
                "ok": False,
                "applied": 0,
                "skipped": skipped,
                "error": "no_mappable_mutate_operations",
            }
        url = f"{ADS_API_BASE}/customers/{cid}/googleAds:mutate"
        resp = self.session.post(
            url,
            headers=self._headers(),
            json={"mutateOperations": mutate_ops},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "ok": True,
            "applied": len(mutate_ops),
            "skipped": skipped,
            "response": data,
        }


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
    from ecom_ops.oauth.google_marketing import ensure_fresh_access_token

    dev = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    token = ensure_fresh_access_token()
    return GoogleAdsClient(
        transport=LiveGoogleAdsTransport(
            developer_token=dev,
            access_token=token,
            login_customer_id=cfg.google_ads_login_customer_id,
        ),
        customer_ids=cfg.google_ads_customer_ids,
    )
