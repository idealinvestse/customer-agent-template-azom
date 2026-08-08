"""GA4 Data / Admin / Measurement Protocol clients (mock-first; live via REST)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import requests

from ecom_ops.marketing.config import load_marketing_config

GA4_DATA_BASE = "https://analyticsdata.googleapis.com/v1beta"
GA4_ADMIN_BASE = "https://analyticsadmin.googleapis.com/v1beta"
MP_COLLECT_URL = "https://www.google-analytics.com/mp/collect"


def _use_mock(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


def _property_path(property_id: str) -> str:
    pid = property_id.strip()
    if pid.startswith("properties/"):
        return pid
    return f"properties/{pid}"


class GA4Transport(Protocol):
    def run_report(
        self, property_id: str, *, days: int
    ) -> dict[str, Any]: ...

    def event_counts(
        self, property_id: str, *, days: int
    ) -> dict[str, int]: ...

    def key_events(self, property_id: str) -> list[str]: ...

    def ads_linked(self, property_id: str) -> bool: ...

    def landing_pages(
        self, property_id: str, *, days: int
    ) -> list[dict[str, Any]]: ...

    def send_mp_event(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class InMemoryGA4Transport:
    """Fixture GA4 data for mock / CI."""

    purchases: int = 42
    purchase_revenue: float = 125_400.0
    sessions: int = 3_200
    currency: str = "SEK"
    events: dict[str, int] = field(
        default_factory=lambda: {
            "view_item": 8900,
            "add_to_cart": 1200,
            "begin_checkout": 480,
            "purchase": 42,
            "refund": 2,
        }
    )
    key_event_names: list[str] = field(
        default_factory=lambda: ["purchase", "add_to_cart"]
    )
    google_ads_linked: bool = True
    mp_sent: list[dict[str, Any]] = field(default_factory=list)

    def run_report(self, property_id: str, *, days: int) -> dict[str, Any]:
        end = date.today()
        start = end - timedelta(days=max(1, days) - 1)
        return {
            "property_id": property_id,
            "source": "ga4_data_api",
            "attribution": "ga4_reporting_identity",
            "date_range": {"start": start.isoformat(), "end": end.isoformat()},
            "sessions": self.sessions,
            "ecommerce_purchases": self.purchases,
            "purchase_revenue": self.purchase_revenue,
            "currency": self.currency,
            "sampled": False,
            "consent_modeled": True,
        }

    def event_counts(self, property_id: str, *, days: int) -> dict[str, int]:
        _ = property_id, days
        return dict(self.events)

    def key_events(self, property_id: str) -> list[str]:
        _ = property_id
        return list(self.key_event_names)

    def ads_linked(self, property_id: str) -> bool:
        _ = property_id
        return self.google_ads_linked

    def landing_pages(
        self, property_id: str, *, days: int
    ) -> list[dict[str, Any]]:
        _ = property_id, days
        return [
            {
                "landing_page": "/produkt/demo-sku",
                "sessions": 400,
                "purchases": 12,
            },
            {
                "landing_page": "/kampanj/404-test",
                "sessions": 80,
                "purchases": 0,
            },
        ]

    def send_mp_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.mp_sent.append(payload)
        return {"ok": True, "validationMessages": []}


class LiveGA4Transport:
    """GA4 Data/Admin REST + Measurement Protocol. Needs OAuth access token."""

    def __init__(
        self,
        access_token: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 60.0,
        measurement_id: str | None = None,
        mp_api_secret: str | None = None,
    ) -> None:
        self.access_token = (access_token or "").strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.measurement_id = (
            measurement_id
            or os.environ.get("GA4_MEASUREMENT_ID", "")
        ).strip()
        self.mp_api_secret = (
            mp_api_secret
            or os.environ.get("GA4_MEASUREMENT_API_SECRET", "")
        ).strip()

    def _require_token(self) -> None:
        if not self.access_token:
            raise RuntimeError(
                "Google marketing OAuth access_token required for live GA4"
            )

    def _headers(self) -> dict[str, str]:
        self._require_token()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _date_bounds(self, days: int) -> tuple[date, date]:
        end = date.today()
        start = end - timedelta(days=max(1, days) - 1)
        return start, end

    def run_report(self, property_id: str, *, days: int) -> dict[str, Any]:
        start, end = self._date_bounds(days)
        path = _property_path(property_id)
        url = f"{GA4_DATA_BASE}/{path}:runReport"
        body = {
            "dateRanges": [
                {"startDate": start.isoformat(), "endDate": end.isoformat()}
            ],
            "metrics": [
                {"name": "sessions"},
                {"name": "ecommercePurchases"},
                {"name": "purchaseRevenue"},
            ],
        }
        resp = self.session.post(
            url, headers=self._headers(), json=body, timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("rows") or []
        sessions = 0
        purchases = 0
        revenue = 0.0
        if rows:
            metrics = (rows[0].get("metricValues") or [])
            if len(metrics) >= 1:
                sessions = int(float(metrics[0].get("value") or 0))
            if len(metrics) >= 2:
                purchases = int(float(metrics[1].get("value") or 0))
            if len(metrics) >= 3:
                revenue = float(metrics[2].get("value") or 0)
        currency = (
            (data.get("metadata") or {}).get("currencyCode")
            or load_marketing_config().default_currency
        )
        return {
            "property_id": property_id.strip().removeprefix("properties/"),
            "source": "ga4_data_api",
            "attribution": "ga4_reporting_identity",
            "date_range": {"start": start.isoformat(), "end": end.isoformat()},
            "sessions": sessions,
            "ecommerce_purchases": purchases,
            "purchase_revenue": revenue,
            "currency": currency,
            "sampled": bool((data.get("metadata") or {}).get("dataLossFromOtherRow")),
            "consent_modeled": True,
        }

    def event_counts(self, property_id: str, *, days: int) -> dict[str, int]:
        start, end = self._date_bounds(days)
        path = _property_path(property_id)
        url = f"{GA4_DATA_BASE}/{path}:runReport"
        body = {
            "dateRanges": [
                {"startDate": start.isoformat(), "endDate": end.isoformat()}
            ],
            "dimensions": [{"name": "eventName"}],
            "metrics": [{"name": "eventCount"}],
            "dimensionFilter": {
                "filter": {
                    "fieldName": "eventName",
                    "inListFilter": {
                        "values": [
                            "view_item",
                            "add_to_cart",
                            "begin_checkout",
                            "purchase",
                            "refund",
                        ]
                    },
                }
            },
        }
        resp = self.session.post(
            url, headers=self._headers(), json=body, timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json()
        counts: dict[str, int] = {}
        for row in data.get("rows") or []:
            dims = row.get("dimensionValues") or []
            mets = row.get("metricValues") or []
            if not dims or not mets:
                continue
            name = str(dims[0].get("value") or "")
            counts[name] = int(float(mets[0].get("value") or 0))
        return counts

    def key_events(self, property_id: str) -> list[str]:
        path = _property_path(property_id)
        url = f"{GA4_ADMIN_BASE}/{path}/keyEvents"
        resp = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        names: list[str] = []
        for item in data.get("keyEvents") or []:
            n = str(item.get("eventName") or item.get("name") or "")
            if "/" in n:
                n = n.rsplit("/", 1)[-1]
            if n:
                names.append(n)
        return names

    def ads_linked(self, property_id: str) -> bool:
        path = _property_path(property_id)
        url = f"{GA4_ADMIN_BASE}/{path}/googleAdsLinks"
        resp = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        links = data.get("googleAdsLinks") or []
        return bool(links)

    def landing_pages(
        self, property_id: str, *, days: int
    ) -> list[dict[str, Any]]:
        start, end = self._date_bounds(days)
        path = _property_path(property_id)
        url = f"{GA4_DATA_BASE}/{path}:runReport"
        body = {
            "dateRanges": [
                {"startDate": start.isoformat(), "endDate": end.isoformat()}
            ],
            "dimensions": [{"name": "landingPage"}],
            "metrics": [
                {"name": "sessions"},
                {"name": "ecommercePurchases"},
            ],
            "limit": 50,
            "orderBys": [
                {"metric": {"metricName": "sessions"}, "desc": True}
            ],
        }
        resp = self.session.post(
            url, headers=self._headers(), json=body, timeout=self.timeout
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[dict[str, Any]] = []
        for row in data.get("rows") or []:
            dims = row.get("dimensionValues") or []
            mets = row.get("metricValues") or []
            out.append(
                {
                    "landing_page": str(dims[0].get("value") if dims else ""),
                    "sessions": int(float(mets[0].get("value") or 0)) if mets else 0,
                    "purchases": int(float(mets[1].get("value") or 0))
                    if len(mets) > 1
                    else 0,
                }
            )
        return out

    def send_mp_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.measurement_id or not self.mp_api_secret:
            raise RuntimeError(
                "GA4_MEASUREMENT_ID and GA4_MEASUREMENT_API_SECRET required "
                "for Measurement Protocol"
            )
        qs = urlencode(
            {
                "measurement_id": self.measurement_id,
                "api_secret": self.mp_api_secret,
            }
        )
        url = f"{MP_COLLECT_URL}?{qs}"
        resp = self.session.post(
            url, json=payload, timeout=self.timeout, stream=True
        )
        try:
            # MP returns 204 on success; some proxies return 200
            if resp.status_code not in {200, 204}:
                resp.raise_for_status()
                return {"ok": False, "status_code": resp.status_code}
            return {
                "ok": True,
                "validationMessages": [],
                "status_code": resp.status_code,
            }
        finally:
            resp.close()


@dataclass
class GA4Client:
    transport: GA4Transport
    property_ids: tuple[str, ...]

    def assert_allowed(self, property_id: str | None = None) -> str:
        cfg = load_marketing_config()
        allowed = self.property_ids or cfg.ga4_property_ids
        # Fail-closed from env mock flag — never bypass via transport type alone
        mock = _use_mock(None)
        if not allowed and not mock:
            raise PermissionError(
                "AZOM_GA4_PROPERTY_IDS empty — fail-closed in live"
            )
        pid = (property_id or (allowed[0] if allowed else "mock-property")).strip()
        if allowed and pid not in allowed and not mock:
            raise PermissionError(f"GA4 property {pid} not in allowlist")
        return pid

    def digest(self, *, days: int = 7, property_id: str | None = None) -> dict[str, Any]:
        pid = self.assert_allowed(property_id)
        return self.transport.run_report(pid, days=days)

    def funnel_events(
        self, *, days: int = 7, property_id: str | None = None
    ) -> dict[str, Any]:
        pid = self.assert_allowed(property_id)
        counts = self.transport.event_counts(pid, days=days)
        expected = (
            "view_item",
            "add_to_cart",
            "begin_checkout",
            "purchase",
            "refund",
        )
        missing = [e for e in expected if counts.get(e, 0) <= 0]
        return {
            "property_id": pid,
            "source": "ga4_data_api",
            "events": counts,
            "missing_expected": missing,
            "ok": "purchase" in counts and counts.get("purchase", 0) > 0,
        }

    def conversion_health(
        self, *, property_id: str | None = None
    ) -> dict[str, Any]:
        pid = self.assert_allowed(property_id)
        keys = self.transport.key_events(pid)
        linked = self.transport.ads_linked(pid)
        return {
            "property_id": pid,
            "source": "ga4_admin_api",
            "key_events": keys,
            "purchase_is_key_event": "purchase" in {k.lower() for k in keys},
            "google_ads_linked": linked,
        }

    def landing_pages(
        self, *, days: int = 7, property_id: str | None = None
    ) -> list[dict[str, Any]]:
        pid = self.assert_allowed(property_id)
        return self.transport.landing_pages(pid, days=days)

    def send_measurement_protocol(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self.transport.send_mp_event(payload)


def client_from_env(
    *,
    use_mock: bool | None = None,
    transport: GA4Transport | None = None,
) -> GA4Client:
    cfg = load_marketing_config()
    if transport is not None:
        return GA4Client(transport=transport, property_ids=cfg.ga4_property_ids)
    if _use_mock(use_mock):
        return GA4Client(
            transport=InMemoryGA4Transport(),
            property_ids=cfg.ga4_property_ids or ("mock-property",),
        )
    from ecom_ops.oauth.google_marketing import ensure_fresh_access_token

    token = ensure_fresh_access_token()
    return GA4Client(
        transport=LiveGA4Transport(access_token=token),
        property_ids=cfg.ga4_property_ids,
    )
