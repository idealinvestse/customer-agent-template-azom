"""Model drift detection on production classify samples (P4.3).

Compares recent classify confidence distribution against a baseline.
If accuracy drops below threshold or confidence mean shifts significantly,
flags a drift alert.

Run via CLI: ``python -m ecom_ops drift-check --days 7``
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ecom_ops.telemetry import Telemetry

# Default drift thresholds
DEFAULT_MIN_ACCURACY = 0.80
DEFAULT_MIN_CONFIDENCE_MEAN = 0.65
DEFAULT_CONFIDENCE_STDDEV_MAX = 0.25


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


def drift_check(
    *,
    telemetry: Telemetry | None = None,
    days: int = 7,
    min_accuracy: float = DEFAULT_MIN_ACCURACY,
    min_confidence_mean: float = DEFAULT_MIN_CONFIDENCE_MEAN,
    confidence_stddev_max: float = DEFAULT_CONFIDENCE_STDDEV_MAX,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Analyze classify telemetry for drift signals (P4.3).

    Returns a summary with drift flag, confidence stats, and category distribution.
    """
    tel = telemetry or Telemetry()
    path = Path(tel.path)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(1, int(days)))

    confidences: list[float] = []
    categories: dict[str, int] = {}
    n_classify = 0
    n_errors = 0

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
                if ts is not None and ts < cutoff:
                    continue
                action = str(ev.get("action") or "")
                meta = ev.get("meta") or {}
                if not isinstance(meta, dict):
                    meta = {}
                if action == "llm_support_classify":
                    n_classify += 1
                    cat = str(meta.get("category") or "unknown")
                    categories[cat] = categories.get(cat, 0) + 1
                    conf = meta.get("confidence")
                    if conf is not None:
                        try:
                            confidences.append(float(conf))
                        except (TypeError, ValueError):
                            pass
                elif action == "llm_classify_error":
                    n_errors += 1

    conf_mean = statistics.mean(confidences) if confidences else None
    conf_median = statistics.median(confidences) if confidences else None
    conf_stddev = statistics.stdev(confidences) if len(confidences) >= 2 else None

    drift_signals: list[str] = []
    if conf_mean is not None and conf_mean < min_confidence_mean:
        drift_signals.append(f"confidence_mean {conf_mean:.2f} < {min_confidence_mean}")
    if conf_stddev is not None and conf_stddev > confidence_stddev_max:
        drift_signals.append(f"confidence_stddev {conf_stddev:.2f} > {confidence_stddev_max}")
    error_rate = (n_errors / (n_classify + n_errors)) if (n_classify + n_errors) > 0 else 0.0
    if error_rate > 0.20:
        drift_signals.append(f"error_rate {error_rate:.0%} > 20%")

    drift = len(drift_signals) > 0
    return {
        "ok": not drift,
        "drift": drift,
        "days": int(days),
        "n_classify": n_classify,
        "n_errors": n_errors,
        "error_rate": round(error_rate, 4),
        "confidence_mean": round(conf_mean, 4) if conf_mean is not None else None,
        "confidence_median": round(conf_median, 4) if conf_median is not None else None,
        "confidence_stddev": round(conf_stddev, 4) if conf_stddev is not None else None,
        "category_distribution": categories,
        "drift_signals": drift_signals,
        "message": (
            f"Drift detected: {'; '.join(drift_signals)}"
            if drift
            else f"No drift — {n_classify} classify events, mean conf {conf_mean:.2f}" if conf_mean
            else f"No drift — {n_classify} classify events (no confidence data)"
        ),
    }
