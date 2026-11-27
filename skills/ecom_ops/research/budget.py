"""Dedicated web search / fetch quota and monthly external USD cap tracking.

Strictly isolated from OpenRouter's ``$100`` ``cost_usd`` budget by reading
``meta.estimated_search_usd`` rather than ``cost_usd``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ecom_ops.research.config import ResearchConfig, load_research_config
from ecom_ops.telemetry import Telemetry, default_telemetry

_SEARCH_COUNTED_ACTIONS = frozenset({"web_search_selected", "web_search"})
_FETCH_COUNTED_ACTIONS = frozenset({"web_fetch_page", "web_fetch"})


def _iter_events_since(telemetry: Telemetry, since_iso: str) -> list[dict[str, Any]]:
    path = telemetry.path
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                ts = str(ev.get("created_at") or "")
                if ts and ts < since_iso:
                    continue
                events.append(ev)
    except OSError:
        return []
    return events


def research_budget_status(
    *,
    telemetry: Telemetry | None = None,
    config: ResearchConfig | None = None,
) -> dict[str, Any]:
    """Return daily query/fetch usage and monthly external USD spend vs caps."""
    cfg = config or load_research_config()
    tel = telemetry or default_telemetry
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    events = _iter_events_since(tel, month_start)
    queries_today = 0
    fetches_today = 0
    monthly_usd = 0.0

    for ev in events:
        action = str(ev.get("action") or "")
        meta = ev.get("meta") if isinstance(ev.get("meta"), dict) else {}
        cost_kind = str(meta.get("cost_kind") or "")
        if cost_kind == "mock":
            continue
        ts = str(ev.get("created_at") or "")
        is_cached = bool(meta.get("cached")) or cost_kind == "cached"

        try:
            monthly_usd += float(meta.get("estimated_search_usd") or 0.0)
        except (TypeError, ValueError):
            pass

        if not ts or ts >= day_start:
            if not is_cached and action in _SEARCH_COUNTED_ACTIONS:
                queries_today += 1
            elif not is_cached and action in _FETCH_COUNTED_ACTIONS:
                fetches_today += 1

    max_q = cfg.budgets.max_queries_per_day
    max_f = cfg.budgets.max_fetches_per_day
    usd_cap = cfg.budgets.monthly_external_usd_cap
    warn_ratio = cfg.budgets.warn_ratio

    q_ratio = (queries_today / max_q) if max_q > 0 else 1.0
    f_ratio = (fetches_today / max_f) if max_f > 0 else 1.0
    u_ratio = (monthly_usd / usd_cap) if usd_cap > 0 else 1.0
    max_ratio = max(q_ratio, f_ratio, u_ratio)

    near_cap = max_ratio >= warn_ratio
    at_cap = (queries_today >= max_q) or (fetches_today >= max_f) or (monthly_usd >= usd_cap)
    search_allowed = (queries_today < max_q) and (monthly_usd < usd_cap)
    fetch_allowed = (fetches_today < max_f) and (monthly_usd < usd_cap)

    if monthly_usd >= usd_cap:
        message = f"Research monthly external USD cap reached (${monthly_usd:.2f}/${usd_cap:g})"
    elif queries_today >= max_q:
        message = f"Research daily query cap reached ({queries_today}/{max_q})"
    elif fetches_today >= max_f:
        message = f"Research daily fetch cap reached ({fetches_today}/{max_f})"
    elif near_cap:
        message = f"Research budget near cap ({max_ratio:.0%})"
    else:
        message = "Research budget OK"

    return {
        "queries_today": queries_today,
        "max_queries_per_day": max_q,
        "fetches_today": fetches_today,
        "max_fetches_per_day": max_f,
        "monthly_external_usd": round(monthly_usd, 6),
        "monthly_external_usd_cap": usd_cap,
        "warn_ratio": warn_ratio,
        "used_ratio": round(max_ratio, 4),
        "near_cap": near_cap,
        "at_cap": at_cap,
        "search_allowed": search_allowed,
        "fetch_allowed": fetch_allowed,
        "message": message,
    }


def can_run_search(
    *,
    telemetry: Telemetry | None = None,
    config: ResearchConfig | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for an external search query."""
    st = research_budget_status(telemetry=telemetry, config=config)
    if st["monthly_external_usd"] >= st["monthly_external_usd_cap"]:
        return False, "search_monthly_usd_cap"
    if st["queries_today"] >= st["max_queries_per_day"]:
        return False, "search_daily_query_cap"
    return True, "eligible"


def can_run_fetch(
    *,
    telemetry: Telemetry | None = None,
    config: ResearchConfig | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for an external page fetch."""
    st = research_budget_status(telemetry=telemetry, config=config)
    if st["monthly_external_usd"] >= st["monthly_external_usd_cap"]:
        return False, "search_monthly_usd_cap"
    if st["fetches_today"] >= st["max_fetches_per_day"]:
        return False, "search_daily_fetch_cap"
    return True, "eligible"


__all__ = [
    "can_run_fetch",
    "can_run_search",
    "research_budget_status",
]
