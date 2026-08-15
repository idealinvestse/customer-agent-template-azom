"""Shared live-read helpers for GA4 / Google Ads (optional extra + REST)."""

from __future__ import annotations

from typing import Any


class MarketingLiveExtraMissing(RuntimeError):
    """Raised when official Google client libs are required but not installed."""

    def __init__(self, extra: str = "marketing-live") -> None:
        super().__init__(
            f"Install optional extra: pip install 'azom-ecom-ops[{extra}]' "
            "(or use REST via requests if token is present)"
        )
        self.extra = extra


def parse_ga4_run_report(payload: dict[str, Any], *, property_id: str) -> dict[str, Any]:
    """Map GA4 Data API runReport JSON to digest fields."""
    headers = [str(h.get("name") or "") for h in (payload.get("metricHeaders") or [])]
    values: list[str] = []
    rows = payload.get("rows") or []
    if rows and isinstance(rows[0], dict):
        values = [str(v.get("value") or "0") for v in (rows[0].get("metricValues") or [])]
    mapped = {headers[i]: values[i] if i < len(values) else "0" for i in range(len(headers))}

    def _num(key: str, *alts: str) -> float:
        for k in (key, *alts):
            if k in mapped:
                try:
                    return float(mapped[k])
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    purchases = int(_num("ecommercePurchases", "ecommerce_purchases", "purchases"))
    revenue = _num("purchaseRevenue", "purchase_revenue")
    sessions = int(_num("sessions"))
    return {
        "property_id": property_id,
        "source": "ga4_data_api",
        "attribution": "ga4_reporting_identity",
        "ecommerce_purchases": purchases,
        "purchase_revenue": revenue,
        "sessions": sessions,
        "sampled": bool(payload.get("metadata", {}).get("samplingMetadatas")),
        "raw_metrics": mapped,
    }


def parse_ads_search_campaigns(
    payload: dict[str, Any], *, customer_id: str
) -> dict[str, Any]:
    """Map Google Ads searchStream/search JSON to digest fields."""
    results = payload.get("results") or []
    campaigns: list[dict[str, Any]] = []
    cost_micros = 0
    clicks = 0
    conversions = 0.0
    conv_value = 0.0
    for row in results:
        if not isinstance(row, dict):
            continue
        camp = row.get("campaign") or {}
        metrics = row.get("metrics") or {}
        cm = int(metrics.get("costMicros") or metrics.get("cost_micros") or 0)
        cl = int(metrics.get("clicks") or 0)
        conv = float(metrics.get("conversions") or 0)
        cv = float(metrics.get("conversionsValue") or metrics.get("conversions_value") or 0)
        cost_micros += cm
        clicks += cl
        conversions += conv
        conv_value += cv
        campaigns.append(
            {
                "id": str(camp.get("id") or ""),
                "name": str(camp.get("name") or ""),
                "status": str(camp.get("status") or ""),
                "cost": round(cm / 1_000_000, 2),
                "conversions": conv,
            }
        )
    cost = cost_micros / 1_000_000
    roas = (conv_value / cost) if cost > 0 else None
    return {
        "customer_id": customer_id,
        "source": "google_ads_api",
        "attribution": "ads_reported",
        "cost": round(cost, 2),
        "cost_micros": cost_micros,
        "clicks": clicks,
        "conversions": conversions,
        "conversions_value": conv_value,
        "roas_ads_reported": round(roas, 2) if roas is not None else None,
        "campaigns": campaigns,
    }
