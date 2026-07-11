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
    """
    cfg = config or load_cases_ai_config()
    if not cfg.auto_send_enabled:
        return False
    if cfg.kill_switch_active():
        return False
    if escalated:
        return False
    cat = (category or "").strip().lower()
    if cat in {c.lower() for c in cfg.never_suggest_categories}:
        return False
    if cat not in {c.lower() for c in cfg.auto_send_categories}:
        return False
    if confidence < cfg.auto_send_min_confidence:
        return False
    if not (order_id or "").strip():
        return False
    if auto_sends_today >= cfg.max_auto_sends_per_day:
        return False
    return True


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
