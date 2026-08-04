"""GA4 Data / Admin / Measurement Protocol clients (mock-first)."""

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
    """Minimal live stub — raises until Oscar wires OAuth access token use."""

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    def run_report(self, property_id: str, *, days: int) -> dict[str, Any]:
        raise NotImplementedError(
            "Live GA4 Data API requires google-analytics-data + OAuth; "
            "use AZOM_USE_MOCK=1 or extend LiveGA4Transport"
        )

    def event_counts(self, property_id: str, *, days: int) -> dict[str, int]:
        raise NotImplementedError("Live GA4 event counts not wired")

    def key_events(self, property_id: str) -> list[str]:
        raise NotImplementedError("Live GA4 Admin key events not wired")

    def ads_linked(self, property_id: str) -> bool:
        raise NotImplementedError("Live GA4 Admin Ads link not wired")

    def landing_pages(
        self, property_id: str, *, days: int
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("Live GA4 landing pages not wired")

    def send_mp_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("Live Measurement Protocol not wired")


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
    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN", "").strip()
    if not token:
        from ecom_ops.oauth.google_marketing import GoogleMarketingOAuthStore

        bundle = GoogleMarketingOAuthStore().load_tokens()
        token = (bundle.access_token if bundle else "") or ""
    return GA4Client(
        transport=LiveGA4Transport(access_token=token),
        property_ids=cfg.ga4_property_ids,
    )
