"""Auto-send guardrails (Path B).

Rails only: eligibility + kill-switch + daily cap. Production default keeps
``auto_send_enabled: false``. Poll / ingest must not call this to send mail;
human ``approve_and_send`` remains the live path until an Oscar-flagged
experiment explicitly wires a sender.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ecom_ops.cases.suggest import CasesAiConfig, load_cases_ai_config

# Reserved telemetry action — unused until an experiment wires a sender.
AUTO_SEND_TELEMETRY_ACTION = "case_auto_sent"


def explain_auto_send(
    *,
    category: str,
    confidence: float,
    order_id: str | None,
    escalated: bool,
    auto_sends_today: int = 0,
    config: CasesAiConfig | None = None,
) -> tuple[bool, str]:
    """Return (eligible, reason_code) for FU9 rails without sending mail.

    Reason codes: auto_send_disabled, kill_switch, escalated,
    never_suggest_category, category_not_allowed, low_confidence,
    missing_order_id, daily_cap, or eligible.
    """
    cfg = config or load_cases_ai_config()
    if not cfg.auto_send_enabled:
        return False, "auto_send_disabled"
    if cfg.kill_switch_active():
        return False, "kill_switch"
    if escalated:
        return False, "escalated"
    cat = (category or "").strip().lower()
    if cat in {c.lower() for c in cfg.never_suggest_categories}:
        return False, "never_suggest_category"
    if cat not in {c.lower() for c in cfg.auto_send_categories}:
        return False, "category_not_allowed"
    if confidence < cfg.auto_send_min_confidence:
        return False, "low_confidence"
    if not (order_id or "").strip():
        return False, "missing_order_id"
    if auto_sends_today >= cfg.max_auto_sends_per_day:
        return False, "daily_cap"
    return True, "eligible"


def should_auto_send(
    *,
    category: str,
    confidence: float,
    order_id: str | None,
    escalated: bool,
    auto_sends_today: int = 0,
    config: CasesAiConfig | None = None,
) -> bool:
    """Return True only when every auto-send rail passes.

    Deny-by-default: missing/disabled config, kill-switch, allowlist miss,
    low confidence, missing order_id, escalated cases, or daily cap.
    Does not send mail — callers must not treat True as permission to send
    unless an explicit Oscar experiment also enables the live sender path.

    P1.2: when an experiment is live (``auto_send_experiment_name`` set in
    config), callers should tag telemetry with that name so auto-send
    outcomes are attributable to a specific experiment.
    """
    eligible, _reason = explain_auto_send(
        category=category,
        confidence=confidence,
        order_id=order_id,
        escalated=escalated,
        auto_sends_today=auto_sends_today,
        config=config,
    )
    return eligible


def active_experiment_name(config: CasesAiConfig | None = None) -> str:
    """Return the current auto-send experiment name, or empty string (P1.2)."""
    cfg = config or load_cases_ai_config()
    if not cfg.auto_send_enabled or cfg.kill_switch_active():
        return ""
    return cfg.auto_send_experiment_name


class AutoSendDayCounter:
    """File-backed daily counter hook for ``max_auto_sends_per_day``.

    Not wired into poll/send. Future experiments can inject this (or another
    backend) when counting real auto-sends.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            import os

            base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
            path = base / "auto_send_day_count.json"
        self.path = Path(path)

    def _today(self) -> str:
        return date.today().isoformat()

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"date": self._today(), "count": 0}
        try:
            with self.path.open(encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                return {"date": self._today(), "count": 0}
            return raw
        except (OSError, json.JSONDecodeError):
            return {"date": self._today(), "count": 0}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(self.path)

    def count_today(self) -> int:
        data = self._read()
        if str(data.get("date") or "") != self._today():
            return 0
        try:
            return max(0, int(data.get("count") or 0))
        except (TypeError, ValueError):
            return 0

    def increment(self) -> int:
        today = self._today()
        data = self._read()
        count = 0
        if str(data.get("date") or "") == today:
            try:
                count = max(0, int(data.get("count") or 0))
            except (TypeError, ValueError):
                count = 0
        count += 1
        self._write(
            {
                "date": today,
                "count": count,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return count
