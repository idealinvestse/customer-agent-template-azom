"""Ops readiness markers (last case poll, stale thresholds)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _data_dir() -> Path:
    base = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def last_case_poll_path() -> Path:
    return _data_dir() / "last_case_poll.json"


def write_last_case_poll(
    *,
    ok: bool,
    errors: int = 0,
    created: int = 0,
    polled_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    path = last_case_poll_path()
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "errors": int(errors),
        "created": int(created),
        "polled_at": polled_at
        or datetime.now(UTC).isoformat(),
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def read_last_case_poll() -> dict[str, Any] | None:
    path = last_case_poll_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


MAIL_POLL_STUCK_RUNBOOK = "docs/runbooks/mail-poll-stuck.md"


def readiness_brief_actions(readiness: dict[str, Any]) -> list[str]:
    """Build prioritized daily-brief proposed_actions from readiness slice."""
    actions: list[str] = []
    if readiness.get("partial"):
        actions.append(
            "Poll delvis misslyckad — vissa mailboxes failade. "
            f"Se runbook: {MAIL_POLL_STUCK_RUNBOOK}"
        )
    elif readiness.get("stale") and not readiness.get("ok"):
        actions.append(
            "Cases poll stale or missing — check azom-cases-poll.timer"
        )
    return actions


def poll_stale_threshold_sec() -> int:
    raw = os.environ.get("AZOM_POLL_STALE_SEC", "").strip()
    if raw.isdigit():
        return max(60, int(raw))
    # Default: 15 minutes (timer is 5 min)
    return 900


def readiness_from_last_poll() -> dict[str, Any]:
    """Build readiness slice for /health."""
    mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    marker = read_last_case_poll()
    threshold = poll_stale_threshold_sec()
    if marker is None:
        # In mock/dev, missing poll is not a hard failure
        return {
            "ok": mock,
            "stale": not mock,
            "last_poll_at": None,
            "last_poll_age_sec": None,
            "last_poll_ok": None,
            "threshold_sec": threshold,
            "detail": "no poll marker yet",
        }

    polled_at = str(marker.get("polled_at") or "")
    age: float | None = None
    try:
        raw = polled_at.replace("Z", "+00:00")
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        age = max(0.0, (datetime.now(UTC) - ts).total_seconds())
    except Exception:
        age = None

    last_ok = bool(marker.get("ok", False))
    errors = int(marker.get("errors") or 0)
    partial = bool(marker.get("partial")) or (last_ok and errors > 0)
    stale = age is None or age > threshold
    ready = last_ok and not stale and not partial
    detail = None
    if partial and not stale:
        detail = f"partial poll failure ({errors} mailbox error(s))"
    elif (not last_ok) and errors > 0:
        detail = f"last poll failed ({errors} error(s))"
    elif stale:
        detail = "poll stale or missing"
    return {
        "ok": ready,
        "stale": stale,
        "partial": partial,
        "last_poll_at": polled_at or None,
        "last_poll_age_sec": None if age is None else round(age, 1),
        "last_poll_ok": last_ok,
        "threshold_sec": threshold,
        "errors": marker.get("errors"),
        "created": marker.get("created"),
        "detail": detail,
        "failures": marker.get("failures"),
    }


def web_search_readiness() -> dict[str, Any]:
    """Build non-secret readiness and provider status for web search & research."""
    from ecom_ops.integrations.search.router import LIVE_ROUTE_IDS, get_search_router
    from ecom_ops.research.config import (
        effective_route_order,
        is_mock_mode,
        kill_switch_active,
        load_research_config,
        research_enabled,
        web_fetch_enabled,
        web_search_enabled,
    )

    mock_mode = is_mock_mode()
    cfg = load_research_config()
    search_kill = kill_switch_active("search")
    research_kill = kill_switch_active("research")
    search_en = web_search_enabled()
    fetch_en = web_fetch_enabled()
    research_en = research_enabled()

    router = get_search_router()
    routes = router.routes()
    providers: dict[str, dict[str, bool]] = {}
    for rid in LIVE_ROUTE_IDS:
        configured = bool((routes.get(rid) or {}).get("configured"))
        if rid == "searxng":
            route_enabled = configured
        else:
            p_cfg = cfg.providers.get(rid)
            route_enabled = not (isinstance(p_cfg, dict) and p_cfg.get("enabled") is False)
        providers[rid] = {
            "configured": configured,
            "enabled": route_enabled,
        }

    any_configured = any(p["configured"] and p["enabled"] for p in providers.values())
    if mock_mode:
        status = "mock"
        ready = True
    elif search_kill:
        status = "kill_switch"
        ready = False
    elif not search_en:
        status = "disabled"
        ready = False
    elif any_configured:
        status = "ready"
        ready = True
    else:
        status = "unconfigured"
        ready = False

    return {
        "ready": ready,
        "status": status,
        "mock_mode": mock_mode,
        "enabled": search_en,
        "fetch_enabled": fetch_en,
        "research_enabled": research_en,
        "kill_switch_search": search_kill,
        "kill_switch_research": research_kill,
        "any_configured": any_configured,
        "route_order": list(effective_route_order(cfg)),
        "providers": providers,
    }


def health_extras() -> dict[str, Any]:
    """Additive /health fields: null-send, budget ratio, last poll errors, web search status."""
    from ecom_ops.budget import budget_status
    from ecom_ops.runtime_profile import null_send_label

    readiness = readiness_from_last_poll()
    budget = budget_status()
    ws = web_search_readiness()
    return {
        "null_send": null_send_label(),
        "budget_ratio": budget.get("used_ratio"),
        "budget_pacing_warn": bool(budget.get("pacing_warn")),
        "last_poll_errors": int(readiness.get("errors") or 0),
        "web_search_status": ws.get("status"),
    }
