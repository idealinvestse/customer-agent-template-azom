"""Read-only A1 soak pre-flight checks. Never writes the PILOT_OPS outcome log."""

from __future__ import annotations

import os
from typing import Any

from ecom_ops.bot.actors import channel_posture
from ecom_ops.ops_status import readiness_from_last_poll
from ecom_ops.runtime_profile import null_send_label


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _check(
    key: str,
    ok: bool,
    *,
    detail: str,
    required_for_live: bool = True,
) -> dict[str, Any]:
    return {
        "key": key,
        "ok": bool(ok),
        "detail": detail,
        "required_for_live": required_for_live,
    }


def run_soak_preflight(*, mock_ok: bool = True) -> dict[str, Any]:
    """Evaluate PILOT_OPS pre-flight items. Does not mark soak complete."""
    mock = _truthy("AZOM_USE_MOCK")
    tg = channel_posture("telegram")
    ms = channel_posture("messenger")
    readiness = readiness_from_last_poll()
    auto_send_kill = _truthy("AZOM_AUTO_SEND_KILL")
    auto_send_enabled = False
    try:
        from ecom_ops.cases.suggest import load_cases_ai_config

        auto_send_enabled = bool(load_cases_ai_config().auto_send_enabled)
    except Exception:
        auto_send_enabled = False

    checks = [
        _check(
            "use_mock",
            mock if mock_ok else not mock,
            detail=(
                "AZOM_USE_MOCK=1 (dev/soft-soak)"
                if mock
                else "AZOM_USE_MOCK=0 (live posture)"
            ),
            required_for_live=False,
        ),
        _check(
            "telegram_allowlist",
            bool(tg.get("allowlist_set")) or mock,
            detail="TELEGRAM_ALLOWED_CHAT_IDS set"
            if tg.get("allowlist_set")
            else "empty allowlist (fail-closed in live)",
        ),
        _check(
            "telegram_actor_map",
            bool(tg.get("actor_map_set")) or mock,
            detail="TELEGRAM_ACTOR_MAP set"
            if tg.get("actor_map_set")
            else "empty actor map (fail-closed in live)",
        ),
        _check(
            "messenger_allowlist",
            bool(ms.get("allowlist_set")) or mock,
            detail="MESSENGER_ALLOWED_PSIDS set"
            if ms.get("allowlist_set")
            else "empty allowlist (fail-closed in live)",
        ),
        _check(
            "messenger_actor_map",
            bool(ms.get("actor_map_set")) or mock,
            detail="MESSENGER_ACTOR_MAP set"
            if ms.get("actor_map_set")
            else "empty actor map (fail-closed in live)",
        ),
        _check(
            "messenger_tokens",
            (
                _present("MESSENGER_PAGE_ACCESS_TOKEN")
                and _present("MESSENGER_APP_SECRET")
                and _present("MESSENGER_VERIFY_TOKEN")
            )
            or mock,
            detail="Messenger token trio present" if not mock else "skipped in mock",
        ),
        _check(
            "dashboard_public_url",
            _present("AZOM_DASHBOARD_PUBLIC_URL") or mock,
            detail="AZOM_DASHBOARD_PUBLIC_URL set"
            if _present("AZOM_DASHBOARD_PUBLIC_URL")
            else "unset (needed for Messenger deep links in live)",
        ),
        _check(
            "auto_send_off",
            not auto_send_enabled,
            detail="auto_send_enabled is false (required)",
        ),
        _check(
            "auto_send_kill_optional",
            True,
            detail="AZOM_AUTO_SEND_KILL=1" if auto_send_kill else "kill-switch unset (optional belt)",
            required_for_live=False,
        ),
        _check(
            "poll_readiness",
            bool(readiness.get("ok")) or mock,
            detail=str(readiness.get("detail") or "poll marker ok"),
            required_for_live=not mock,
        ),
        _check(
            "null_send_visible",
            True,
            detail=f"null_send={null_send_label()}",
            required_for_live=False,
        ),
    ]

    live_required = [c for c in checks if c["required_for_live"]]
    failed_live = [c for c in live_required if not c["ok"]]
    failed_any = [c for c in checks if not c["ok"]]
    ok = not failed_live
    return {
        "ok": ok,
        "mock": mock,
        "null_send": null_send_label(),
        "checks": checks,
        "failed": [c["key"] for c in failed_any],
        "soak_complete": False,
        "message": (
            "Pre-flight OK for current posture (does not mark A1 soak complete)."
            if ok
            else f"Pre-flight failed: {', '.join(c['key'] for c in failed_live) or ', '.join(c['key'] for c in failed_any)}."
        ),
    }
