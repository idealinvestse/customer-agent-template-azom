"""LLM quality trend analysis (P7.4).

Aggregates classify confidence and draft edit distance over time buckets
(daily) to detect quality degradation. Returns data suitable for
dashboard trend graphs.

Run via CLI: ``python -m ecom_ops trends --days 30``
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
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


def _day_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def quality_trends(
    *,
    telemetry: Telemetry | None = None,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate daily confidence + edit distance trends (P7.4)."""
    tel = telemetry or Telemetry()
    path = Path(tel.path)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(1, int(days)))

    daily_conf: dict[str, list[float]] = defaultdict(list)
    daily_edit: dict[str, list[float]] = defaultdict(list)
    daily_classify_count: dict[str, int] = defaultdict(int)
    daily_draft_count: dict[str, int] = defaultdict(int)

    if path.is_file():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    ev = json.loads(s)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(ev.get("created_at"))
                if ts is None or ts < cutoff:
                    continue
                day = _day_key(ts)
                action = str(ev.get("action") or "")
                meta = ev.get("meta") or {}
                if not isinstance(meta, dict):
                    meta = {}
                if action == "llm_support_classify":
                    daily_classify_count[day] += 1
                    conf = meta.get("confidence")
                    if conf is not None:
                        try:
                            daily_conf[day].append(float(conf))
                        except (TypeError, ValueError):
                            pass
                elif action in ("case_replied", "case_draft_saved"):
                    daily_draft_count[day] += 1
                    ed = meta.get("draft_edit_distance")
                    if ed is not None:
                        try:
                            daily_edit[day].append(float(ed))
                        except (TypeError, ValueError):
                            pass

    # Build sorted daily series
    all_days = sorted(set(list(daily_conf.keys()) + list(daily_edit.keys())))
    conf_series = []
    edit_series = []
    for day in all_days:
        confs = daily_conf.get(day, [])
        edits = daily_edit.get(day, [])
        conf_series.append({
            "date": day,
            "mean": round(statistics.mean(confs), 4) if confs else None,
            "median": round(statistics.median(confs), 4) if confs else None,
            "count": len(confs),
        })
        edit_series.append({
            "date": day,
            "mean": round(statistics.mean(edits), 4) if edits else None,
            "median": round(statistics.median(edits), 4) if edits else None,
            "count": len(edits),
        })

    # Detect degradation: compare last 7d vs previous 7d
    recent_conf = [d for d in conf_series[-7:] if d["mean"] is not None]
    prev_conf = [d for d in conf_series[-14:-7] if d["mean"] is not None]
    recent_edit = [d for d in edit_series[-7:] if d["mean"] is not None]
    prev_edit = [d for d in edit_series[-14:-7] if d["mean"] is not None]

    conf_degradation = None
    if recent_conf and prev_conf:
        recent_avg = statistics.mean(d["mean"] for d in recent_conf)
        prev_avg = statistics.mean(d["mean"] for d in prev_conf)
        if prev_avg > 0:
            conf_degradation = round((prev_avg - recent_avg) / prev_avg, 4)

    edit_degradation = None
    if recent_edit and prev_edit:
        recent_avg = statistics.mean(d["mean"] for d in recent_edit)
        prev_avg = statistics.mean(d["mean"] for d in prev_edit)
        if prev_avg > 0:
            edit_degradation = round((recent_avg - prev_avg) / prev_avg, 4)

    return {
        "ok": True,
        "days": int(days),
        "confidence_trend": conf_series,
        "edit_distance_trend": edit_series,
        "classify_counts": dict(daily_classify_count),
        "draft_counts": dict(daily_draft_count),
        "confidence_degradation_pct": conf_degradation,
        "edit_distance_increase_pct": edit_degradation,
        "message": f"Trends: {len(all_days)} days, conf degradation {conf_degradation or 0:.1%}",
    }
