"""OpenRouter budget headroom / soft-alarm helpers."""

from __future__ import annotations

from typing import Any

from ecom_ops.config import load_app_config
from ecom_ops.telemetry import Telemetry, default_telemetry


def budget_status(
    *,
    telemetry: Telemetry | None = None,
    cap: float | None = None,
    warn_ratio: float | None = None,
) -> dict[str, Any]:
    """Return spend vs OpenRouter cap with soft warn flag (FU5)."""
    cfg = None
    try:
        cfg = load_app_config()
    except Exception:
        pass
    openrouter_cap = float(
        cap if cap is not None else (cfg.limits.openrouter_cap if cfg else 100.0)
    )
    ratio = float(
        warn_ratio
        if warn_ratio is not None
        else (cfg.limits.openrouter_warn_ratio if cfg else 0.8)
    )
    if ratio <= 0 or ratio > 1:
        ratio = 0.8
    tel = telemetry or default_telemetry
    used = float(tel.sum_cost_usd())
    used_ratio = (used / openrouter_cap) if openrouter_cap > 0 else 0.0
    near_cap = used_ratio >= ratio
    at_cap = used_ratio >= 1.0
    return {
        "used_usd": round(used, 6),
        "cap_usd": openrouter_cap,
        "used_ratio": round(used_ratio, 4),
        "warn_ratio": ratio,
        "near_cap": near_cap,
        "at_cap": at_cap,
        "warn": near_cap,
        "message": (
            "OpenRouter budget at/over cap"
            if at_cap
            else (
                f"OpenRouter budget near cap ({used_ratio:.0%} of ${openrouter_cap:g})"
                if near_cap
                else "OpenRouter budget OK"
            )
        ),
    }
