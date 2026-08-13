"""Support-loop KPI aggregation from telemetry (Sprint A SA5 + FAQ retrieve)."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ecom_ops.telemetry import Telemetry


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))


def _hit_rate(n_hit: int, n_total: int) -> float | None:
    if n_total <= 0:
        return None
    return round(n_hit / n_total, 4)


def _faq_cat_bucket(
    buckets: dict[str, dict[str, int]], category: str
) -> dict[str, int]:
    key = category.strip() or "unknown"
    if key not in buckets:
        buckets[key] = {"n_retrieve": 0, "n_hit": 0, "n_miss": 0}
    return buckets[key]


def support_kpis_last_days(
    *,
    telemetry: Telemetry | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate case approve / draft edit / FAQ retrieve KPIs from telemetry.

    Looks at ``case_replied`` for time_to_approve_sec and draft_edit_distance,
    ``case_draft_saved`` for edit distances on saves, and ``faq_retrieve``
    (plus ``faq_retrieve_error``) for lexical FAQ hit rates per category.
    """
    # Fresh Telemetry() so AZOM_DATA_DIR / AZOM_TELEMETRY_PATH apply (tests + CLI).
    tel = telemetry or Telemetry()
    path = Path(tel.path)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(1, int(days)))
    tta: list[float] = []
    edit_on_reply: list[float] = []
    edit_on_save: list[float] = []
    n_replied = 0
    n_suggest_meta = 0
    n_faq_retrieve = 0
    n_faq_hit = 0
    n_faq_miss = 0
    n_faq_retrieve_error = 0
    faq_cats: dict[str, dict[str, int]] = {}

    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(ev.get("created_at"))
                if ts is not None and ts < cutoff:
                    continue
                action = str(ev.get("action") or "")
                meta = ev.get("meta") or {}
                if not isinstance(meta, dict):
                    meta = {}
                if action == "case_replied":
                    n_replied += 1
                    if meta.get("time_to_approve_sec") is not None:
                        try:
                            tta.append(float(meta["time_to_approve_sec"]))
                        except (TypeError, ValueError):
                            pass
                    if meta.get("draft_edit_distance") is not None:
                        try:
                            edit_on_reply.append(float(meta["draft_edit_distance"]))
                        except (TypeError, ValueError):
                            pass
                    if meta.get("suggest_approve") or meta.get("suggested"):
                        n_suggest_meta += 1
                elif action == "case_draft_saved":
                    if meta.get("draft_edit_distance") is not None:
                        try:
                            edit_on_save.append(float(meta["draft_edit_distance"]))
                        except (TypeError, ValueError):
                            pass
                elif action == "faq_retrieve":
                    n_faq_retrieve += 1
                    try:
                        hit_count = int(meta.get("hit_count") or 0)
                    except (TypeError, ValueError):
                        hit_count = 0
                    bucket = _faq_cat_bucket(
                        faq_cats, str(meta.get("category") or "")
                    )
                    bucket["n_retrieve"] += 1
                    if hit_count > 0:
                        n_faq_hit += 1
                        bucket["n_hit"] += 1
                    else:
                        n_faq_miss += 1
                        bucket["n_miss"] += 1
                elif action == "faq_retrieve_error":
                    n_faq_retrieve_error += 1

    median_tta = _median(tta)
    mean_edit = _mean(edit_on_reply) if edit_on_reply else _mean(edit_on_save)
    faq_by_category = {
        cat: {
            "n_retrieve": counts["n_retrieve"],
            "n_hit": counts["n_hit"],
            "n_miss": counts["n_miss"],
            "hit_rate": _hit_rate(counts["n_hit"], counts["n_retrieve"]),
        }
        for cat, counts in sorted(faq_cats.items())
    }
    faq_rate = _hit_rate(n_faq_hit, n_faq_retrieve)
    tta_part = (
        f", median TTA {median_tta:.0f}s"
        if median_tta is not None
        else ", no TTA samples"
    )
    faq_part = (
        f", FAQ {n_faq_hit}/{n_faq_retrieve} hits"
        if n_faq_retrieve
        else ", no FAQ retrieve samples"
    )
    return {
        "ok": True,
        "days": int(days),
        "n_case_approved": n_replied,
        "n_with_time_to_approve": len(tta),
        "median_time_to_approve_sec": (
            round(median_tta, 2) if median_tta is not None else None
        ),
        "mean_draft_edit_distance": (
            round(mean_edit, 4) if mean_edit is not None else None
        ),
        "n_draft_saves_with_edit": len(edit_on_save),
        "n_replied_with_suggest_meta": n_suggest_meta,
        "n_faq_retrieve": n_faq_retrieve,
        "n_faq_hit": n_faq_hit,
        "n_faq_miss": n_faq_miss,
        "n_faq_retrieve_error": n_faq_retrieve_error,
        "faq_hit_rate": faq_rate,
        "faq_by_category": faq_by_category,
        "message": f"Last {days}d: {n_replied} approves{tta_part}{faq_part}",
    }
