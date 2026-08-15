"""Support-loop KPI aggregation from telemetry (Sprint A SA5)."""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ecom_ops.telemetry import Telemetry

BASELINE_SCHEMA_KEYS = (
    "start_date",
    "hours_per_week_or_proxy",
    "source",
    "notes",
)


def load_baseline(path: Path | str) -> dict[str, Any]:
    """Load a human-filled baseline YAML/JSON. Agents must not invent numbers."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    data: Any
    if p.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        data = yaml.safe_load(raw) or {}
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("baseline file must be a mapping")
    return data


def compare_kpis_to_baseline(
    kpis: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Compute deltas only. Never fills missing human baseline fields."""
    hours = baseline.get("hours_per_week_or_proxy")
    tta_base = baseline.get("median_time_to_approve_sec")
    tta_now = kpis.get("median_time_to_approve_sec")
    deltas: dict[str, Any] = {}
    if tta_base is not None and tta_now is not None:
        try:
            base_f = float(tta_base)
            now_f = float(tta_now)
            deltas["median_time_to_approve_sec"] = round(now_f - base_f, 2)
            if base_f:
                deltas["median_time_to_approve_ratio"] = round(now_f / base_f, 4)
        except (TypeError, ValueError):
            pass
    missing = [k for k in BASELINE_SCHEMA_KEYS if not str(baseline.get(k) or "").strip()]
    return {
        "ok": True,
        "baseline_present": bool(baseline),
        "baseline_hours_per_week_or_proxy": hours,
        "baseline_source": baseline.get("source"),
        "kpis": kpis,
        "deltas": deltas,
        "missing_human_fields": missing,
        "baseline_complete": False,
        "message": (
            "KPI vs baseline (human-owned numbers; agents do not mark 50% goal done)."
        ),
    }


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
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
    cutoff = (now or datetime.now(UTC)) - timedelta(days=max(1, int(days)))
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
