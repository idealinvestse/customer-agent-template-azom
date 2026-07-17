"""Flask webbdashboard för Jonatan (viewer) och Oscar (full_admin)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

_ROOT = Path(__file__).resolve().parents[2]
_DASH_DIR = Path(__file__).resolve().parent
if str(_ROOT / "skills") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills"))
if str(_DASH_DIR) not in sys.path:
    sys.path.insert(0, str(_DASH_DIR))

from secret_probes import (  # noqa: E402
    run_all_probes,
    run_probe,
)
from settings_store import (  # noqa: E402
    EDITABLE_SECRET_KEYS,
    apply_env_overlays,
    load_settings_view,
    resolve_escalation,
    save_secrets,
    save_settings,
    secrets_status,
)
from status import health_probe, runtime_status  # noqa: E402

app = Flask(__name__)


def _case_age(iso: str | None) -> str:
    """Human age from ISO timestamp for case queue."""
    if not iso:
        return ""
    try:
        from datetime import datetime, timezone

        raw = str(iso).replace("Z", "+00:00")
        created = datetime.fromisoformat(raw)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"
    except Exception:
        return ""


app.jinja_env.globals["case_age"] = _case_age


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _is_mock() -> bool:
    apply_env_overlays()
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


def _configure_secret_key() -> None:
    """Ensure Flask sessions work for CSRF tokens."""
    apply_env_overlays()
    key = os.environ.get("DASHBOARD_SECRET_KEY", "").strip()
    if not key:
        material = (
            os.environ.get("DASHBOARD_PASSWORD_HASH")
            or os.environ.get("DASHBOARD_OSCAR_PASSWORD_HASH")
            or os.environ.get("DASHBOARD_PASSWORD")
            or os.environ.get("DASHBOARD_OSCAR_PASSWORD")
            or ""
        )
        if material:
            key = hashlib.sha256(f"azom-dash:{material}".encode()).hexdigest()
        elif _is_mock():
            key = "azom-mock-dashboard-secret"
        else:
            key = secrets.token_hex(32)
    app.secret_key = key


# Bootstrap so the first request can open a session.
_configure_secret_key()


def _password_ok(expected_plain: str, expected_hash: str, password: str) -> bool:
    if expected_hash:
        # Werkzeug / modern salted hashes
        if expected_hash.startswith(("pbkdf2:", "scrypt:", "argon2:")):
            try:
                return check_password_hash(expected_hash, password)
            except Exception:
                return False
        # Legacy unsalted SHA-256 hex digest
        got = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(got, expected_hash)
    if expected_plain:
        return secrets.compare_digest(password, expected_plain)
    return False


def _authenticate(username: str, password: str) -> dict | None:
    """Return actor dict or None. Mock default passwords only when AZOM_USE_MOCK.

    Uses ``ecom_ops.rbac`` as the single source of truth for roles/permissions
    (P5.1). The returned dict carries the RBAC role name so downstream code
    can call ``require_permission`` consistently.
    """
    apply_env_overlays()
    user = (username or "").strip().lower()
    if user == "jonatan":
        plain = os.environ.get("DASHBOARD_PASSWORD", "").strip()
        hashed = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
        if _password_ok(plain, hashed, password):
            return {"name": "jonatan", "role": "viewer", "is_oscar": False}
        # Explicit mock-only fallback — never in live/prod
        if not plain and not hashed and _is_mock() and password == "jonatan":
            return {"name": "jonatan", "role": "viewer", "is_oscar": False}
        return None
    if user == "oscar":
        plain = os.environ.get("DASHBOARD_OSCAR_PASSWORD", "").strip()
        hashed = os.environ.get("DASHBOARD_OSCAR_PASSWORD_HASH", "").strip()
        if _password_ok(plain, hashed, password):
            return {"name": "oscar", "role": "full_admin", "is_oscar": True}
        if not plain and not hashed and _is_mock() and password == "oscar":
            return {"name": "oscar", "role": "full_admin", "is_oscar": True}
        return None
    return None


def _dashboard_actor(actor_dict: dict) -> Any:
    """Build an ``ecom_ops.rbac.Actor`` from the dashboard auth dict (P5.1).

    Ensures the dashboard uses the same RBAC engine as the rest of the system
    when calling ``require_permission``.
    """
    from ecom_ops.rbac import Actor as _Actor

    return _Actor(name=actor_dict["name"], role=actor_dict["role"])


def _ensure_csrf_token() -> str:
    _configure_secret_key()
    tok = session.get("csrf_token")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf_token"] = tok
    return str(tok)


def _validate_csrf() -> Response | None:
    expected = session.get("csrf_token")
    got = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("_csrf")
        or ""
    )
    if not got:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            got = str(body.get("_csrf") or "")
    if (
        not expected
        or not got
        or not secrets.compare_digest(str(got), str(expected))
    ):
        return Response("CSRF validation failed", 400)
    return None


def _auth_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        _configure_secret_key()
        auth = request.authorization
        actor = None
        if auth is not None:
            ip = request.remote_addr or "unknown"
            if _login_rate_limited(ip):
                return Response(
                    "Too many failed login attempts. Try again later.",
                    429,
                    {"Retry-After": str(_LOGIN_WINDOW_SEC)},
                )
            actor = _authenticate(auth.username or "", auth.password or "")
            if actor is None:
                _record_login_failure(ip)
        if actor is None:
            return Response(
                "Authentication required (jonatan or oscar)",
                401,
                {"WWW-Authenticate": 'Basic realm="Azom Dashboard"'},
            )
        g.actor = actor
        _ensure_csrf_token()
        # Session timeout (P2.7): expire after 8h of inactivity
        from datetime import datetime, timedelta, timezone

        session_timeout_h = int(os.environ.get("DASHBOARD_SESSION_TIMEOUT_H", "8"))
        last_seen = session.get("_last_seen")
        now = datetime.now(timezone.utc)
        if last_seen:
            try:
                ls = datetime.fromisoformat(last_seen)
                if ls.tzinfo is None:
                    ls = ls.replace(tzinfo=timezone.utc)
                if now - ls > timedelta(hours=session_timeout_h):
                    session.clear()
                    return Response(
                        "Session expired — please re-authenticate",
                        401,
                        {"WWW-Authenticate": 'Basic realm="Azom Dashboard"'},
                    )
            except Exception:
                pass
        session["_last_seen"] = now.isoformat()
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            failed = _validate_csrf()
            if failed is not None:
                return failed
        return view(*args, **kwargs)

    return wrapper


@app.context_processor
def _inject_csrf() -> dict[str, str]:
    try:
        if getattr(g, "actor", None):
            return {"csrf_token": _ensure_csrf_token()}
    except Exception:
        pass
    return {"csrf_token": ""}


def _oscar_required(view):
    @wraps(view)
    @_auth_required
    def wrapper(*args, **kwargs):
        if not g.actor.get("is_oscar"):
            return Response("Oscar only", 403)
        return view(*args, **kwargs)

    return wrapper


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
    """Cheap counts for nav badges / overview (no live probes)."""
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
    """Shared queue filters for list → detail → next navigation."""
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
    """URL query fragment for flash messages (always quoted)."""
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
    from datetime import datetime, timezone

    path = _probe_last_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": [r.to_dict() if hasattr(r, "to_dict") else r for r in results],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _merge_probe_last(result) -> list:
    """Merge a single probe result into cached probe_last.json; return full list."""
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


def _load_probe_last() -> dict | None:
    path = _probe_last_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _presence_integrations(runtime: dict) -> dict:
    """Presence-only integration summary (no live HTTP probes)."""
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

    # Runtime-derived extras
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


def _dashboard_context(**extra) -> dict:
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


@app.before_request
def _load_overlays():
    apply_env_overlays()


# --------------------------------------------------------------------------- #
# Security headers (P2.3) + session timeout (P2.7)
# --------------------------------------------------------------------------- #


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    # HSTS only over HTTPS (avoid breaking local HTTP dev)
    if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # CSP: allow inline (Alpine + templates) + self + CDN scripts/styles
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'",
    )
    return resp


# --------------------------------------------------------------------------- #
# App-level login rate limiting (P2.2)
# --------------------------------------------------------------------------- #
from collections import defaultdict  # noqa: E402
from time import time as _time  # noqa: E402

_LOGIN_WINDOW_SEC = 300  # 5 min
_LOGIN_MAX_FAILURES = 10  # per IP per window
_login_failures: dict[str, list[float]] = defaultdict(list)


def _login_rate_limited(ip: str) -> bool:
    now = _time()
    cutoff = now - _LOGIN_WINDOW_SEC
    recent = [t for t in _login_failures[ip] if t > cutoff]
    _login_failures[ip] = recent
    return len(recent) >= _LOGIN_MAX_FAILURES


def _record_login_failure(ip: str) -> None:
    _login_failures[ip].append(_time())


@app.route("/metrics")
def prometheus_metrics():
    """Prometheus-format metrics endpoint (P7.3). No auth — scrape from localhost only."""
    from ecom_ops.budget import budget_status
    from ecom_ops.kpis import support_kpis_last_days

    lines: list[str] = []
    # Budget
    bs = budget_status()
    lines.append("# TYPE azom_budget_used_usd gauge")
    lines.append(f"azom_budget_used_usd {bs.get('used_usd', 0)}")
    lines.append(f"azom_budget_cap_usd {bs.get('cap_usd', 0)}")
    lines.append(f"azom_budget_used_ratio {bs.get('used_ratio', 0)}")
    # KPIs
    kpis = support_kpis_last_days(days=7)
    lines.append("# TYPE azom_case_approved_total gauge")
    lines.append(f"azom_case_approved_total {kpis.get('n_case_approved', 0)}")
    if kpis.get("median_time_to_approve_sec") is not None:
        lines.append(f"azom_median_time_to_approve_sec {kpis['median_time_to_approve_sec']}")
    if kpis.get("mean_draft_edit_distance") is not None:
        lines.append(f"azom_mean_draft_edit_distance {kpis['mean_draft_edit_distance']}")
    # Case counts
    import sqlite3

    db_path = _data_dir() / "cases.db"
    if db_path.is_file():
        try:
            conn = sqlite3.connect(str(db_path))
            for status in ("open", "escalated", "replied", "closed"):
                count = conn.execute(
                    "SELECT COUNT(*) FROM cases WHERE status = ?", (status,)
                ).fetchone()[0]
                lines.append(f'azom_cases{{status="{status}"}} {count}')
            conn.close()
        except Exception:
            pass
    # Poll age
    probe_path = _data_dir() / "probe_last.json"
    if probe_path.is_file():
        try:
            import json as _json

            data = _json.loads(probe_path.read_text(encoding="utf-8"))
            lines.append(f"azom_probe_last_ts {data.get('checked_at', '')!r}")
        except Exception:
            pass
    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")


@app.route("/")
@_auth_required
def index():
    data = _data_dir()
    telemetry = _tail_jsonl(data / "telemetry.jsonl", 20)
    escalations = _tail_jsonl(data / "escalations.jsonl", 20)
    cost = 0.0
    for ev in telemetry:
        try:
            cost += float(ev.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    runtime = runtime_status()
    cap = float(runtime.get("openrouter_cap") or 100)
    cost_pct = min(100, int((cost / cap) * 100)) if cap else 0
    presence = _presence_integrations(runtime)
    last_probe = _load_probe_last()
    budget = None
    try:
        from ecom_ops.budget import budget_status
        from ecom_ops.telemetry import Telemetry

        budget = budget_status(telemetry=Telemetry(path=_data_dir() / "telemetry.jsonl"))
        # Prefer total from full telemetry file for progress when available
        if budget.get("used_usd") is not None:
            cost = float(budget["used_usd"])
            cost_pct = min(100, int((cost / cap) * 100)) if cap else 0
    except Exception:
        budget = None
    kpis = None
    try:
        from ecom_ops.kpis import support_kpis_last_days
        from ecom_ops.telemetry import Telemetry as _Tel

        kpis = support_kpis_last_days(
            telemetry=_Tel(path=_data_dir() / "telemetry.jsonl"),
            days=7,
        )
    except Exception:
        kpis = None
    readiness = None
    try:
        from ecom_ops.ops_status import readiness_from_last_poll

        readiness = readiness_from_last_poll()
    except Exception:
        readiness = None
    return render_template(
        "index.html",
        **_dashboard_context(
            telemetry=telemetry,
            escalations=escalations,
            cost_usd=round(cost, 4),
            openrouter_cap=runtime.get("openrouter_cap", 100),
            budget_cap_llm=runtime.get("budget_cap_llm", 80),
            cost_pct=cost_pct,
            integrations=presence,
            last_probe=last_probe,
            budget=budget,
            support_kpis=kpis,
            readiness=readiness,
        ),
    )


@app.route("/onboarding")
@_auth_required
def onboarding():
    return render_template(
        "onboarding.html",
        **_dashboard_context(secrets=secrets_status(), health=health_probe()),
    )


@app.route("/onboarding/status")
@_auth_required
def onboarding_status():
    return jsonify(
        {
            "runtime": runtime_status(),
            "secrets": secrets_status(),
            "health": health_probe(),
        }
    )


@app.route("/settings", methods=["GET", "POST"])
@_auth_required
def settings():
    if request.method == "POST":
        form = request.form.to_dict()
        # checkboxes: missing = false
        for flag in (
            "email_enabled",
            "email_smtp",
            "email_imap",
            "email_pop3",
            "mailcow",
            "order_api",
            "selenium",
            "woocommerce_api",
            "wordpress_api",
            "smart_handling",
            "full_agent_tools",
            "mock_mode",
        ):
            form[flag] = "1" if flag in request.form else "0"
        try:
            save_settings(form)
            return redirect(url_for("settings") + "?msg=Sparat")
        except ValueError as exc:
            return redirect(url_for("settings") + f"?err={exc}")
    return render_template(
        "settings.html",
        **_dashboard_context(settings=load_settings_view()),
    )


@app.route("/secrets", methods=["GET", "POST"])
@_auth_required
def secrets_page():
    if request.method == "POST":
        if g.actor.get("is_oscar"):
            return redirect(url_for("oscar_secrets"))
        keys_raw = request.form.get("keys", "")
        note = request.form.get("note", "").strip()[:500]
        keys = [k.strip() for k in keys_raw.replace(";", ",").split(",") if k.strip()]
        if not keys:
            keys = [k for k in request.form.getlist("key") if k]
        if not keys:
            return redirect(url_for("secrets_page") + "?err=Välj+minst+en+nyckel")
        from ecom_ops.escalation import EscalationService

        EscalationService().escalate_critical(
            "Dashboard secret update requested by Jonatan",
            details={
                "requested_keys": keys,
                "note": note,
                "actor": g.actor["name"],
            },
        )
        return redirect(url_for("secrets_page") + "?msg=Begäran+skickad+till+Oscar")
    return render_template(
        "secrets.html",
        **_dashboard_context(secrets=secrets_status()),
    )


@app.route("/data/telemetry")
@_auth_required
def data_telemetry():
    rows = _tail_jsonl(_data_dir() / "telemetry.jsonl", 200)
    return render_template(
        "data_telemetry.html",
        **_dashboard_context(rows=rows),
    )


@app.route("/data/escalations")
@_auth_required
def data_escalations():
    rows = _tail_jsonl(_data_dir() / "escalations.jsonl", 200)
    return render_template(
        "data_escalations.html",
        **_dashboard_context(rows=rows),
    )


@app.route("/interact", methods=["GET", "POST"])
@_auth_required
def interact():
    result = None
    error = None
    if request.method == "POST":
        action = request.form.get("action", "draft")
        if action == "escalate":
            draft = (request.form.get("draft") or "").strip()[:2000]
            category = (request.form.get("category") or "").strip()[:80]
            from ecom_ops.escalation import EscalationService

            EscalationService().escalate_critical(
                "Interact draft escalated from dashboard",
                details={
                    "actor": g.actor["name"],
                    "category": category,
                    "draft": draft,
                },
            )
            return redirect(url_for("interact") + "?msg=Eskalerat+till+Oscar")

        message = (
            request.form.get("message")
            or (request.get_json(silent=True) or {}).get("message")
            or ""
        ).strip()
        if not message:
            if request.is_json:
                return jsonify({"ok": False, "error": "message required"}), 400
            error = "Meddelande krävs"
        else:
            try:
                from ecom_ops.actions.support import SupportService

                out = SupportService().handle(message, actor="agent", language="sv")
                result = out.to_dict()
                result["note"] = "Draft only; send kräver operator/Oscar."
                if request.is_json:
                    return jsonify(result)
            except Exception as exc:
                if request.is_json:
                    return jsonify({"ok": False, "error": str(exc)}), 500
                error = str(exc)
    if request.method == "GET" and request.accept_mimetypes.best == "application/json":
        return jsonify({"hint": "POST message for support draft"})
    return render_template(
        "interact.html",
        **_dashboard_context(result=result, error=error),
    )


@app.route("/cases")
@_auth_required
def cases_list():
    from ecom_ops.cases.mailboxes import enabled_mailboxes
    from ecom_ops.cases.service import CaseService

    status = request.args.get("status", "open,escalated")
    mailbox_id = request.args.get("mailbox") or None
    category = request.args.get("category") or None
    suggest_raw = (request.args.get("suggest") or "").strip().lower()
    suggest_only = suggest_raw in {"1", "true", "yes", "on"}
    svc = CaseService()
    rows = svc.store.list_cases(
        status=status if status != "all" else None,
        mailbox_id=mailbox_id or None,
        category=category or None,
        suggest_approve=True if suggest_only else None,
        limit=100,
    )
    # Stable multi-key: escalated → high → suggest-approve → newest
    rows.sort(key=lambda c: c.created_at or "", reverse=True)
    rows.sort(key=lambda c: 0 if getattr(c, "suggest_approve", False) else 1)
    rows.sort(key=lambda c: 0 if (c.priority or "") == "high" else 1)
    rows.sort(key=lambda c: 0 if c.status == "escalated" else 1)
    return render_template(
        "cases.html",
        **_dashboard_context(
            cases=rows,
            status_filter=status,
            mailbox_filter=mailbox_id or "",
            category_filter=category or "",
            suggest_filter=suggest_only,
            mailboxes=enabled_mailboxes(),
        ),
    )


@app.route("/cases/poll", methods=["POST"])
@_auth_required
def cases_poll():
    from ecom_ops.cases.service import CaseService

    result = CaseService().poll(actor=g.actor["name"], use_mock=_is_mock() or None)
    if result.ok:
        return redirect(
            url_for("cases_list") + f"?msg=Skapade+{result.created}+ärenden"
        )
    return redirect(url_for("cases_list") + f"?err={result.message}")


@app.route("/cases/bulk-close", methods=["POST"])
@_auth_required
def cases_bulk_close():
    from ecom_ops.cases.service import CaseService

    ids_raw = request.form.get("case_ids", "")
    case_ids = [c.strip() for c in ids_raw.split(",") if c.strip()]
    if not case_ids:
        return redirect(url_for("cases_list") + "?err=Inga+ärenden+valda")
    result = CaseService().bulk_close(
        case_ids, actor=g.actor["name"], reason="bulk-close-dashboard"
    )
    if result["ok"]:
        return redirect(
            url_for("cases_list")
            + f"?msg=Stängde+{result['closed']}+ärenden"
        )
    return redirect(
        url_for("cases_list")
        + f"?err={result['message']}"
    )


@app.route("/cases/<case_id>", methods=["GET", "POST"])
@_auth_required
def case_detail(case_id: str):
    from ecom_ops.cases.service import CaseService
    from ecom_ops.order_context import resolve_order_panel

    svc = CaseService()
    filt = _queue_filter_from_request()
    if request.method == "POST":
        action = request.form.get("action", "reply")
        body = request.form.get("body") or ""
        go_next = action in {"reply_next", "close_next", "next_only"}
        if action == "next_only":
            return _redirect_next_or_list(svc, case_id, filt=filt, msg="Nästa")
        if action in {"reply", "reply_next"}:
            # Resolve next before send — current case leaves the open queue on success.
            nxt_before = None
            if go_next or action == "reply_next":
                nxt_before = svc.next_in_queue(
                    case_id,
                    status=str(filt.get("status") or "open,escalated"),
                    mailbox_id=str(filt.get("mailbox") or "") or None,
                    category=str(filt.get("category") or "") or None,
                    suggest_only=bool(filt.get("suggest_only")),
                )
            result = svc.approve_and_send(
                case_id, actor=g.actor["name"], body_override=body or None
            )
            if result.ok:
                if go_next or action == "reply_next":
                    q = _case_queue_query(filt)
                    if nxt_before:
                        return redirect(
                            url_for("case_detail", case_id=nxt_before.id)
                            + f"?{q}&{_flash_q('Skickat')}"
                        )
                    return redirect(
                        url_for("cases_list") + f"?{q}&{_flash_q('Skickat')}"
                    )
                q = _case_queue_query(filt)
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{q}&{_flash_q('Skickat')}"
                )
            return redirect(
                url_for("case_detail", case_id=case_id)
                + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
            )
        if action == "save_draft":
            result = svc.save_draft(case_id, body, actor=g.actor["name"])
            if result.ok:
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q('Draft sparad')}"
                )
            return redirect(
                url_for("case_detail", case_id=case_id)
                + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
            )
        if action in {"close", "close_next"}:
            nxt_before = None
            if go_next or action == "close_next":
                nxt_before = svc.next_in_queue(
                    case_id,
                    status=str(filt.get("status") or "open,escalated"),
                    mailbox_id=str(filt.get("mailbox") or "") or None,
                    category=str(filt.get("category") or "") or None,
                    suggest_only=bool(filt.get("suggest_only")),
                )
            result = svc.close(case_id, actor=g.actor["name"], reason="dashboard")
            if result.ok:
                if go_next or action == "close_next":
                    q = _case_queue_query(filt)
                    if nxt_before:
                        return redirect(
                            url_for("case_detail", case_id=nxt_before.id)
                            + f"?{q}&{_flash_q('Stängt')}"
                        )
                    return redirect(
                        url_for("cases_list") + f"?{q}&{_flash_q('Stängt')}"
                    )
                return redirect(
                    url_for("cases_list")
                    + f"?{_case_queue_query(filt)}&{_flash_q('Stängt')}"
                )
            return redirect(
                url_for("case_detail", case_id=case_id)
                + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
            )
        if action == "regenerate":
            result = svc.regenerate_draft(
                case_id, actor=g.actor["name"], use_mock=_is_mock() or None
            )
            if result.ok:
                return redirect(
                    url_for("case_detail", case_id=case_id)
                    + f"?{_case_queue_query(filt)}&{_flash_q('Utkast regenererat')}"
                )
            return redirect(
                url_for("case_detail", case_id=case_id)
                + f"?{_case_queue_query(filt)}&{_flash_q(err=result.message)}"
            )

    case = svc.get(case_id)
    if not case:
        return Response("Case not found", 404)
    msgs = svc.store.messages(case_id)
    order_panel = None
    if case.order_id:
        order_panel = resolve_order_panel(case.order_id, use_mock=_is_mock() or None)
    return render_template(
        "case_detail.html",
        **_dashboard_context(
            case=case,
            messages=msgs,
            order_panel=order_panel,
            queue_status=filt["status"],
            queue_mailbox=filt.get("mailbox") or "",
            queue_category=filt.get("category") or "",
            queue_suggest=bool(filt.get("suggest_only")),
        ),
    )


@app.route("/oscar")
@_oscar_required
def oscar_home():
    esc = _tail_jsonl(_data_dir() / "escalations.jsonl", 100)
    open_esc_rows = [e for e in esc if e.get("status", "open") == "open"]
    secs = secrets_status()
    present = sum(1 for s in secs if s["present"])
    missing = sum(1 for s in secs if not s["present"])
    from ecom_ops.oauth.gmail import GmailOAuthStore

    store = GmailOAuthStore()
    return render_template(
        "oscar.html",
        **_dashboard_context(
            open_escalation_tickets=open_esc_rows,
            open_count=len(open_esc_rows),
            secrets_present=present,
            secrets_missing=missing,
            gmail_connected=store.has_tokens(),
            settings=load_settings_view(),
        ),
    )


@app.route("/oscar/secrets", methods=["GET", "POST"])
@_oscar_required
def oscar_secrets():
    if request.method == "POST":
        updates = {}
        for key in EDITABLE_SECRET_KEYS:
            if key in request.form:
                updates[key] = request.form.get(key, "")
        saved = save_secrets(updates)
        msg = f"Sparade+{len(saved)}+nycklar" if saved else "Inga+ändringar"
        return redirect(url_for("oscar_secrets") + f"?msg={msg}")
    last = _load_probe_last()
    results = (last or {}).get("results") or [r.to_dict() for r in run_all_probes()]
    # If no cache, run once and save
    if not last:
        live = run_all_probes()
        _save_probe_last(live)
        results = [r.to_dict() for r in live]
    return render_template(
        "oscar_secrets.html",
        **_dashboard_context(
            secrets=secrets_status(),
            editable_keys=EDITABLE_SECRET_KEYS,
            probe_results=results,
        ),
    )


@app.route("/oscar/secrets/test", methods=["POST"])
@_oscar_required
def oscar_secrets_test():
    apply_env_overlays()
    probe = (request.form.get("probe") or "all").strip().lower()
    if probe in {"", "all"}:
        results = run_all_probes()
        _save_probe_last(results)
    else:
        single = run_probe(probe)
        results = [single]
        _merge_probe_last(single)
    ok_n = sum(1 for r in results if r.status == "ok")
    err_n = sum(1 for r in results if r.status in {"error", "missing"})
    msg = f"Test+klart:+{ok_n}+ok,+{err_n}+problem"
    if request.headers.get("Accept") == "application/json" or request.is_json:
        return jsonify(
            {
                "ok": err_n == 0,
                "results": [r.to_dict() for r in results],
            }
        )
    return render_template(
        "oscar_secrets.html",
        **_dashboard_context(
            secrets=secrets_status(),
            editable_keys=EDITABLE_SECRET_KEYS,
            probe_results=[r.to_dict() for r in results],
            flash=f"ok:{msg.replace('+', ' ')}",
        ),
    )


@app.route("/oscar/escalations", methods=["GET", "POST"])
@_oscar_required
def oscar_escalations():
    if request.method == "POST":
        ticket_id = request.form.get("ticket_id", "").strip()
        show = request.args.get("show", "open")
        if ticket_id and resolve_escalation(ticket_id):
            return redirect(
                url_for("oscar_escalations", show=show) + "?msg=Markerad+som+löst"
            )
        return redirect(
            url_for("oscar_escalations", show=show) + "?err=Ticket+hittades+inte"
        )
    show = request.args.get("show", "open")
    rows = _tail_jsonl(_data_dir() / "escalations.jsonl", 200)
    if show != "all":
        rows = [e for e in rows if e.get("status", "open") == "open"]
    return render_template(
        "oscar_escalations.html",
        **_dashboard_context(rows=rows, show_filter=show),
    )


@app.route("/oscar/gdpr/delete", methods=["POST"])
@_oscar_required
def oscar_gdpr_delete():
    """GDPR Art 17: delete all cases + messages for a given email (P8.2)."""
    email = (request.form.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "message": "email required"}), 400
    from ecom_ops.security import validate_email

    try:
        validate_email(email)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    import sqlite3

    from ecom_ops.audit import log_action

    db_path = _data_dir() / "cases.db"
    if not db_path.is_file():
        return jsonify({"ok": False, "message": "cases.db not found"}), 404
    conn = sqlite3.connect(str(db_path))
    try:
        # Find cases by from_addr
        rows = conn.execute(
            "SELECT id FROM cases WHERE from_addr = ?", (email,)
        ).fetchall()
        case_ids = [r[0] for r in rows]
        if not case_ids:
            return jsonify({"ok": True, "deleted": 0, "message": "No cases found for this email"})
        placeholders = ",".join("?" * len(case_ids))
        conn.execute(f"DELETE FROM case_messages WHERE case_id IN ({placeholders})", case_ids)
        conn.execute(f"DELETE FROM cases WHERE id IN ({placeholders})", case_ids)
        conn.commit()
        log_action(
            actor=g.actor["name"],
            action="gdpr_delete",
            target="cases",
            target_id=email,
            details={"deleted_count": len(case_ids)},
        )
        return jsonify({"ok": True, "deleted": len(case_ids), "message": f"Deleted {len(case_ids)} cases for {email}"})
    except Exception as exc:
        conn.rollback()
        return jsonify({"ok": False, "message": str(exc)[:200]}), 500
    finally:
        conn.close()


@app.route("/oscar/gdpr/export", methods=["GET"])
@_oscar_required
def oscar_gdpr_export():
    """GDPR Art 20: export all data for a given email as JSON (P8.2)."""
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"ok": False, "message": "email required"}), 400
    import sqlite3

    db_path = _data_dir() / "cases.db"
    if not db_path.is_file():
        return jsonify({"ok": False, "message": "cases.db not found"}), 404
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cases = [dict(r) for r in conn.execute(
            "SELECT * FROM cases WHERE from_addr = ?", (email,)
        ).fetchall()]
        if not cases:
            return jsonify({"ok": True, "email": email, "cases": [], "messages": [], "message": "No data found"})
        case_ids = [c["id"] for c in cases]
        placeholders = ",".join("?" * len(case_ids))
        messages = [dict(r) for r in conn.execute(
            f"SELECT * FROM case_messages WHERE case_id IN ({placeholders})", case_ids
        ).fetchall()]
        from ecom_ops.audit import log_action

        log_action(
            actor=g.actor["name"],
            action="gdpr_export",
            target="cases",
            target_id=email,
            details={"case_count": len(cases)},
        )
        return jsonify({"ok": True, "email": email, "cases": cases, "messages": messages})
    finally:
        conn.close()


@app.route("/oauth/gmail/start")
@_auth_required
def oauth_gmail_start():
    from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured

    store = GmailOAuthStore()
    if _is_mock():
        store.mock_connect()
        return redirect(url_for("onboarding") + "?msg=Gmail+kopplad+(mock)")
    if not gmail_oauth_configured():
        return redirect(url_for("secrets_page") + "?err=MAIL_OAUTH_CLIENT_ID/SECRET+saknas")
    state = store.create_state()
    return redirect(store.build_authorize_url(state=state))


@app.route("/oauth/gmail/callback")
def oauth_gmail_callback():
    from ecom_ops.oauth.gmail import GmailOAuthStore

    error = request.args.get("error")
    if error:
        return Response(f"OAuth error: {error}", 400)
    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    if not code or not state:
        return Response("Missing code or state", 400)
    store = GmailOAuthStore()
    if not store.validate_state(state):
        return Response("Invalid or expired OAuth state", 400)
    try:
        store.exchange_code(code)
    except Exception as exc:
        return Response(f"Token exchange failed: {exc}", 502)
    finally:
        store.clear_state()
    return redirect(url_for("onboarding") + "?msg=Gmail+kopplad")


@app.route("/oauth/gmail/status")
@_auth_required
def oauth_gmail_status():
    from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured

    store = GmailOAuthStore()
    bundle = store.load_tokens()
    return jsonify(
        {
            "configured": gmail_oauth_configured(),
            "connected": store.has_tokens(),
            "email": bundle.email if bundle else None,
            "mock_mode": _is_mock(),
        }
    )


@app.route("/health")
def health():
    """Liveness always 200; readiness reflects last case-poll freshness."""
    from ecom_ops.ops_status import readiness_from_last_poll

    readiness = readiness_from_last_poll()
    return jsonify(
        {
            "ok": True,
            "service": "azom-dashboard",
            "liveness": True,
            "readiness": readiness,
        }
    )


@app.route("/live")
def liveness():
    """Kubernetes liveness probe (P6.5). Always 200 if the process is up."""
    return jsonify({"ok": True, "liveness": True}), 200


@app.route("/ready")
def readiness():
    """Kubernetes readiness probe (P6.5). 503 if last poll is stale."""
    from ecom_ops.ops_status import readiness_from_last_poll

    rd = readiness_from_last_poll()
    ok = bool(rd.get("ready", True))
    return jsonify({"ok": ok, "readiness": rd}), (200 if ok else 503)


@app.route("/webhooks/woo", methods=["POST"])
def woo_webhook():
    """WooCommerce webhook receiver (V2.1).

    Verifies HMAC-SHA256 signature against WOO_WEBHOOK_SECRET.
    Returns 200 quickly on valid signature (handlers run inline);
    returns 401 on invalid/missing signature. No auth required —
    signature verification is the auth mechanism.
    """
    import os

    secret = os.environ.get("WOO_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"ok": False, "error": "WOO_WEBHOOK_SECRET not configured"}), 503

    # Cache receiver instance — avoid recreating on every request
    receiver = _get_woo_webhook_receiver(secret)
    ok = receiver.handle_request(request)
    if not ok:
        return jsonify({"ok": False, "error": "Invalid signature"}), 401
    return jsonify({"ok": True}), 200


_woo_webhook_receiver: object | None = None
_woo_webhook_secret_hash: str | None = None


def _get_woo_webhook_receiver(secret: str):
    """Return cached WebhookReceiver, recreating only if secret changed."""
    global _woo_webhook_receiver, _woo_webhook_secret_hash
    import hashlib

    secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    if _woo_webhook_receiver is None or _woo_webhook_secret_hash != secret_hash:
        from ecom_ops.integrations.webhooks import WebhookReceiver

        _woo_webhook_receiver = WebhookReceiver(secret=secret)
        _woo_webhook_secret_hash = secret_hash
    return _woo_webhook_receiver


@app.route("/logs")
@_auth_required
def logs():
    data = _data_dir()
    return jsonify(
        {
            "telemetry": _tail_jsonl(data / "telemetry.jsonl", 100),
            "escalations": _tail_jsonl(data / "escalations.jsonl", 100),
        }
    )


@app.route("/telemetry")
@_auth_required
def telemetry_json():
    return jsonify(_tail_jsonl(_data_dir() / "telemetry.jsonl", 200))


@app.route("/escalations")
@_auth_required
def escalations_json():
    return jsonify(_tail_jsonl(_data_dir() / "escalations.jsonl", 200))


@app.route("/manage")
@_auth_required
def manage():
    if g.actor.get("is_oscar"):
        return jsonify(
            {
                "message": "Oscar full_admin — use /oscar for admin boxes",
                "allowed": ["settings", "secrets", "escalations", "all"],
            }
        )
    return jsonify(
        {
            "message": "Manage is limited for Jonatan. Escalate secret writes to Oscar.",
            "allowed": ["view_health", "view_logs", "view_telemetry", "edit_settings"],
            "denied": ["order_update", "product_publish", "mail_send", "ssh_write", "set_secrets"],
        }
    )


if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    app.run(host=host, port=port, debug=False)
