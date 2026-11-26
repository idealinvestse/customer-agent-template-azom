"""Shared dashboard helpers (paths, queue filters, template context)."""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import (
    Response,
    g,
    get_flashed_messages,
    redirect,
    request,
    url_for,
)
from flask import (
    flash as _flask_flash,
)

from settings_store import apply_env_overlays, secrets_status
from status import runtime_status

_FLASH_LEVELS = frozenset({"ok", "info", "warn", "error"})


def _fmt_dt(value: Any) -> str:
    """Format an ISO timestamp or Unix epoch seconds as YYYY-MM-DD HH:MM (UTC)."""
    if value is None or value == "" or isinstance(value, bool):
        return ""
    try:
        if isinstance(value, (int, float)):
            num = float(value)
            if not math.isfinite(num) or num <= 0:
                return ""
            return datetime.fromtimestamp(num, tz=UTC).strftime("%Y-%m-%d %H:%M")
        raw = str(value).strip()
        if not raw or raw in {"0", "0.0"}:
            return ""
        if raw.replace(".", "", 1).isdigit() and len(raw.split(".", 1)[0]) >= 9:
            num = float(raw)
            if math.isfinite(num) and num > 0:
                return datetime.fromtimestamp(num, tz=UTC).strftime("%Y-%m-%d %H:%M")
            return ""
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        s = str(value).strip()
        return s[:16].replace("T", " ") if len(s) >= 16 else s


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _is_mock() -> bool:
    from ecom_ops.runtime_env import env_flag

    apply_env_overlays()
    return env_flag("AZOM_USE_MOCK", default=False)


def _flash(message: str, category: str = "ok") -> None:
    level = category if category in _FLASH_LEVELS else "ok"
    _flask_flash(str(message), level)


def _tail_jsonl(
    path: Path,
    limit: int = 50,
    *,
    chunk_size: int = 65536,
    max_bytes: int = 2 * 1024 * 1024,
) -> list[dict]:
    if limit <= 0 or not path.is_file():
        return []
    out: list[dict] = []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            bytes_read = 0
            carry = b""
            while pos > 0 and len(out) < limit and bytes_read < max_bytes:
                step = min(chunk_size, pos, max_bytes - bytes_read)
                pos -= step
                fh.seek(pos)
                chunk = fh.read(step)
                if not chunk:
                    break
                bytes_read += len(chunk)
                buf = chunk + carry
                parts = buf.splitlines()
                if pos > 0:
                    if buf.startswith((b"\n", b"\r")):
                        carry = b""
                    else:
                        carry = parts[0] if parts else b""
                        parts = parts[1:]
                else:
                    carry = b""
                for raw_line in reversed(parts):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        out.append({"raw": line[:500]})
                    if len(out) >= limit:
                        break
    except OSError:
        return []
    return out


def _normalize_flash_level(category: str) -> str:
    cat = (category or "").strip().lower()
    if cat in {"error", "danger", "err"}:
        return "error"
    if cat in {"warn", "warning"}:
        return "warn"
    if cat == "info":
        return "info"
    return "ok"


def _collect_flashes() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    try:
        for cat, txt in get_flashed_messages(with_categories=True):
            level = _normalize_flash_level(cat)
            text = str(txt or "").strip()
            if text and (level, text) not in seen:
                seen.add((level, text))
                out.append({"level": level, "text": text})
    except RuntimeError:
        pass
    for param, level in (
        ("err", "error"),
        ("warn", "warn"),
        ("info", "info"),
        ("msg", "ok"),
    ):
        val = (request.args.get(param) or "").strip()
        if val and (level, val) not in seen:
            seen.add((level, val))
            out.append({"level": level, "text": val})
    return out


def _flash_from_query() -> str | None:
    err = request.args.get("err")
    warn = request.args.get("warn")
    info = request.args.get("info")
    msg = request.args.get("msg")
    if err:
        return f"error:{err}"
    if warn:
        return f"warn:{warn}"
    if info:
        return f"info:{info}"
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


def _flash_q(
    msg: str | None = None,
    *,
    err: str | None = None,
    warn: str | None = None,
    info: str | None = None,
) -> str:
    if err is not None:
        return f"err={quote(str(err), safe='')}"
    if warn is not None:
        return f"warn={quote(str(warn), safe='')}"
    if info is not None:
        return f"info={quote(str(info), safe='')}"
    return f"msg={quote(str(msg or ''), safe='')}"


def _normalize_escalation(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize an escalation JSONL record for unified UI rendering."""
    status = str(raw.get("status") or "open").strip().lower()
    if status not in {"open", "resolved"}:
        status = "open"
    details = raw.get("details")
    return {
        "id": str(raw.get("id") or ""),
        "status": status,
        "severity": str(raw.get("severity") or ""),
        "reason": str(raw.get("reason") or ""),
        "summary": str(raw.get("summary") or ""),
        "details": details if isinstance(details, dict) else ({"detail": details} if details else {}),
        "created_at": str(raw.get("created_at") or ""),
        "resolved_at": str(raw.get("resolved_at") or ""),
        "resolved_by": str(raw.get("resolved_by") or ""),
    }


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

    rows: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for group, items in by_group.items():
        gid = group.lower().replace(" ", "_")
        if gid == "telegram":
            st = "ok" if runtime.get("telegram_configured") else "missing"
        else:
            st = group_status([bool(i.get("present")) for i in items])
        seen_ids.add(gid)
        rows.append({"id": gid, "label": group, "status": st})

    if "gmail_oauth" not in seen_ids:
        rows.append(
            {
                "id": "gmail_oauth",
                "label": "Gmail OAuth",
                "status": "ok"
                if runtime.get("gmail_tokens_stored")
                else ("partial" if runtime.get("gmail_oauth_configured") else "missing"),
            }
        )
    if "telegram" not in seen_ids:
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
    flashes = _collect_flashes()
    explicit_flash = extra.get("flash")
    if isinstance(explicit_flash, str) and ":" in explicit_flash:
        lvl, _, txt = explicit_flash.partition(":")
        lvl = _normalize_flash_level(lvl)
        if txt and not any(f["level"] == lvl and f["text"] == txt for f in flashes):
            flashes.insert(0, {"level": lvl, "text": txt})
    first_flash = (
        explicit_flash
        if explicit_flash
        else (f"{flashes[0]['level']}:{flashes[0]['text']}" if flashes else None)
    )
    ctx = {
        "user": actor["name"],
        "role": actor["role"],
        "is_oscar": actor.get("is_oscar", False),
        "runtime": runtime,
        "flash": first_flash,
        "flashes": flashes,
        "open_cases": counts["open_cases"],
        "escalated_cases": counts["escalated_cases"],
        "suggest_cases": counts.get("suggest_cases", 0),
        "open_escalations": counts["open_escalations"],
        "queue_cases": counts["queue_cases"],
        "gmail_connected": bool(runtime.get("gmail_tokens_stored")),
    }
    ctx.update(extra)
    return ctx
