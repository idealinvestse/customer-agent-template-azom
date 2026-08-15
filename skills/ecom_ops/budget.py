"""OpenRouter budget headroom / soft-alarm helpers."""

from __future__ import annotations

import os
from datetime import UTC, datetime
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
    day_start = datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    used_today = float(tel.sum_cost_usd_since(day_start.isoformat()))
    # Project remaining days in a 30-day window from today's rate (alert only).
    projected_30d = used_today * 30.0
    pacing_warn = openrouter_cap > 0 and projected_30d >= (openrouter_cap * ratio)
    return {
        "used_usd": round(used, 6),
        "cap_usd": openrouter_cap,
        "used_ratio": round(used_ratio, 4),
        "warn_ratio": ratio,
        "near_cap": near_cap,
        "at_cap": at_cap,
        "warn": near_cap,
        "used_today_usd": round(used_today, 6),
        "projected_30d_usd": round(projected_30d, 6),
        "pacing_warn": pacing_warn,
        "message": (
            "OpenRouter budget at/over cap"
            if at_cap
            else (
                f"OpenRouter budget near cap ({used_ratio:.0%} of ${openrouter_cap:g})"
                if near_cap
                else (
                    f"OpenRouter pacing warn (today ${used_today:.2f} → ~${projected_30d:.0f}/30d)"
                    if pacing_warn
                    else "OpenRouter budget OK"
                )
            )
        ),
    }


def send_budget_alarm(
    *,
    status: dict[str, Any] | None = None,
    chat_id_env: str = "AZOM_BUDGET_ALARM_TELEGRAM_CHAT_ID",
) -> bool:
    """Send an active budget alarm to Oscar via Telegram (P4.4).

    Only sends when ``near_cap`` or ``at_cap`` is true AND the chat ID env
    is set. Returns True if a notification was sent (or attempted).
    """
    st = status or budget_status()
    if not (st.get("near_cap") or st.get("at_cap")):
        return False
    chat_id = (os.environ.get(chat_id_env) or "").strip()
    if not chat_id:
        # Fall back to escalation chat ID if set
        chat_id = (os.environ.get("AZOM_ESCALATION_TELEGRAM_CHAT_ID") or "").strip()
    if not chat_id:
        return False
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return False
    import urllib.parse
    import urllib.request

    severity = "KRITISKT" if st.get("at_cap") else "VARNING"
    text = (
        f"💰 Budget-alarm ({severity})\n"
        f"Användning: {st.get('used_ratio', 0):.0%} av ${st.get('cap_usd', 0):g}\n"
        f"Spenderat: ${st.get('used_usd', 0):.2f}\n"
        f"Kap: ${st.get('cap_usd', 0):g}"
    )
    url = (
        f"https://api.telegram.org/bot{token}/sendMessage"
        f"?chat_id={chat_id}&text={urllib.parse.quote(text)}"
    )
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 400
    except Exception:
        return False
