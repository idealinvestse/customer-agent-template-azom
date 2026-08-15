"""Shared dashboard helpers (paths, queue filters, template context)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Response, g, redirect, request, url_for

from settings_store import apply_env_overlays, secrets_status
from status import runtime_status


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _is_mock() -> bool:
    apply_env_overlays()
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


def _tail_jsonl(path: Path, limit: int = 50) -> list[dict]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line[:500]})
        if len(out) >= limit:
            break
    return out


def _flash_from_query() -> str | None:
    msg = request.args.get("msg")
    err = request.args.get("err")
    if err:
        return f"error:{err}"
    if msg:
        return f"ok:{msg}"
    return None


def _ops_counts() -> dict[str, int]:
    open_cases = 0
    escalated_cases = 0
    suggest_cases = 0
    try:
        from ecom_ops.cases.service import CaseService

        cs = CaseService()
        open_cases = cs.store.count_by_status("open")
        escalated_cases = cs.store.count_by_status("escalated")
        suggest_cases = cs.store.count_suggest_approve(status="open,escalated")
    except Exception:
        pass
    open_escalations = 0
    try:
        for e in _tail_jsonl(_data_dir() / "escalations.jsonl", 500):
            if e.get("status", "open") == "open":
                open_escalations += 1
    except Exception:
        pass
    return {
        "open_cases": open_cases,
        "escalated_cases": escalated_cases,
        "suggest_cases": suggest_cases,
        "open_escalations": open_escalations,
        "queue_cases": open_cases + escalated_cases,
    }


def _queue_filter_from_request() -> dict[str, str | bool]:
    status = (request.args.get("status") or request.form.get("q_status") or "open,escalated").strip()
    mailbox = (request.args.get("mailbox") or request.form.get("q_mailbox") or "").strip()
    category = (request.args.get("category") or request.form.get("q_category") or "").strip()
    suggest_raw = (
        request.args.get("suggest") or request.form.get("q_suggest") or ""
    ).strip().lower()
    suggest_only = suggest_raw in {"1", "true", "yes", "on"}
    return {
        "status": status or "open,escalated",
        "mailbox": mailbox,
        "category": category,
        "suggest_only": suggest_only,
    }


def _case_queue_query(filt: dict[str, str | bool]) -> str:
    parts: list[str] = [f"status={filt.get('status') or 'open,escalated'}"]
    if filt.get("mailbox"):
        parts.append(f"mailbox={quote(str(filt['mailbox']), safe='')}")
    if filt.get("category"):
        parts.append(f"category={quote(str(filt['category']), safe='')}")
    if filt.get("suggest_only"):
        parts.append("suggest=1")
    return "&".join(parts)


def _flash_q(msg: str | None = None, *, err: str | None = None) -> str:
    if err is not None:
        return f"err={quote(str(err), safe='')}"
    return f"msg={quote(str(msg or ''), safe='')}"


def _redirect_next_or_list(
    svc: object,
    case_id: str,
    *,
    filt: dict[str, str | bool],
    msg: str,
) -> Response:
    from ecom_ops.cases.service import CaseService

    assert isinstance(svc, CaseService)
    nxt = svc.next_in_queue(
        case_id,
        status=str(filt.get("status") or "open,escalated"),
        mailbox_id=str(filt.get("mailbox") or "") or None,
        category=str(filt.get("category") or "") or None,
        suggest_only=bool(filt.get("suggest_only")),
    )
    if nxt:
        q = _case_queue_query(filt)
        return redirect(
            url_for("case_detail", case_id=nxt.id) + f"?{q}&{_flash_q(msg)}"
        )
    return redirect(
        url_for("cases_list") + f"?{_case_queue_query(filt)}&{_flash_q(msg)}"
    )


def _probe_last_path() -> Path:
    return _data_dir() / "probe_last.json"


def _save_probe_last(results: list) -> None:
    from datetime import UTC, datetime

    path = _probe_last_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked_at": datetime.now(UTC).isoformat(),
        "results": [r.to_dict() if hasattr(r, "to_dict") else r for r in results],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_probe_last() -> dict | None:
    path = _probe_last_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _merge_probe_last(result) -> list:
    last = _load_probe_last()
    rows = list((last or {}).get("results") or [])
    row = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    pid = row.get("id")
    replaced = False
    for i, existing in enumerate(rows):
        if isinstance(existing, dict) and existing.get("id") == pid:
            rows[i] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    _save_probe_last(rows)
    return rows


def _presence_integrations(runtime: dict) -> dict:
    secs = secrets_status()
    by_group: dict[str, list] = {}
    for s in secs:
        by_group.setdefault(s.get("group") or "Other", []).append(s)

    def group_status(keys_present: list[bool]) -> str:
        if not keys_present:
            return "missing"
        if all(keys_present):
            return "ok"
        if any(keys_present):
            return "partial"
        return "missing"

    rows = []
    for group, items in by_group.items():
        st = group_status([bool(i.get("present")) for i in items])
        rows.append({"id": group.lower().replace(" ", "_"), "label": group, "status": st})

    rows.append(
        {
            "id": "gmail_oauth",
            "label": "Gmail OAuth",
            "status": "ok" if runtime.get("gmail_tokens_stored") else (
                "partial" if runtime.get("gmail_oauth_configured") else "missing"
            ),
        }
    )
    rows.append(
        {
            "id": "telegram",
            "label": "Telegram",
            "status": "ok" if runtime.get("telegram_configured") else "missing",
        }
    )
    counts = {"ok": 0, "missing": 0, "partial": 0, "error": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"counts": counts, "results": rows, "source": "presence"}


def _dashboard_context(**extra: Any) -> dict:
    apply_env_overlays()
    runtime = runtime_status()
    actor = getattr(g, "actor", {"name": "?", "role": "?", "is_oscar": False})
    counts = _ops_counts()
    ctx = {
        "user": actor["name"],
        "role": actor["role"],
        "is_oscar": actor.get("is_oscar", False),
        "runtime": runtime,
        "flash": _flash_from_query(),
        "open_cases": counts["open_cases"],
        "escalated_cases": counts["escalated_cases"],
        "suggest_cases": counts.get("suggest_cases", 0),
        "open_escalations": counts["open_escalations"],
        "queue_cases": counts["queue_cases"],
        "gmail_connected": bool(runtime.get("gmail_tokens_stored")),
    }
    ctx.update(extra)
    return ctx
