"""Support-loop KPI aggregation from telemetry (Sprint A SA5)."""

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


def support_kpis_last_days(
    *,
    telemetry: Telemetry | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate case approve / draft edit KPIs from telemetry JSONL.

    Looks at ``case_replied`` for time_to_approve_sec and draft_edit_distance,
    and ``case_draft_saved`` for edit distances on saves.
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

    median_tta = _median(tta)
    mean_edit = _mean(edit_on_reply) if edit_on_reply else _mean(edit_on_save)
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
        "message": (
            f"Last {days}d: {n_replied} approves"
            + (
                f", median TTA {median_tta:.0f}s"
                if median_tta is not None
                else ", no TTA samples"
            )
        ),
    }
