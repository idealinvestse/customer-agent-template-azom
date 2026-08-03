"""Oscar CLI summary of FU9 shadow observations (null-send profile)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from ecom_ops.cases.store import CaseStore
from ecom_ops.runtime_profile import null_send_active, null_send_label


def build_shadow_report(
    *,
    days: int = 7,
    store: CaseStore | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Latest-per-case shadow trail from case columns (not raw telemetry history)."""
    days = max(1, int(days or 7))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    st = store or CaseStore()
    cases = st.list_shadow_observed(since_iso=since, limit=limit)

    eligible_n = sum(1 for c in cases if c.shadow_eligible is True)
    denied_n = sum(1 for c in cases if c.shadow_eligible is False)
    reasons: Counter[str] = Counter()
    for c in cases:
        if c.shadow_eligible is False:
            reasons[c.shadow_deny_reason or "unknown"] += 1

    sample = [
        {
            "id": c.id,
            "shadow_eligible": c.shadow_eligible,
            "shadow_deny_reason": c.shadow_deny_reason,
            "category": c.category,
            "status": c.status,
            "updated_at": c.updated_at,
        }
        for c in cases[:25]
    ]

    warnings: list[str] = []
    if not null_send_active():
        warnings.append(
            "null_send=off — trail may be incomplete (soft-soak uses AZOM_NULL_SEND=1)"
        )
    if not cases:
        message = (
            f"Ingen skuggobservation de senaste {days} dagarna "
            f"(null_send={null_send_label()})."
        )
    else:
        message = (
            f"Shadow report: {eligible_n} skulle skickats, {denied_n} nekade "
            f"({len(cases)} ärenden, {days}d, null_send={null_send_label()})."
        )

    return {
        "ok": True,
        "null_send": null_send_label(),
        "days": days,
        "observed_cases": len(cases),
        "eligible": eligible_n,
        "denied": denied_n,
        "deny_reasons": dict(reasons.most_common()),
        "sample": sample,
        "warnings": warnings,
        "message": message,
    }
