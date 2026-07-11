"""Flask webbdashboard för Jonatan (viewer) och Oscar (full_admin)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

_ROOT = Path(__file__).resolve().parents[2]
_DASH_DIR = Path(__file__).resolve().parent
if str(_ROOT / "skills") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills"))
if str(_DASH_DIR) not in sys.path:
    sys.path.insert(0, str(_DASH_DIR))

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


def _password_ok(expected_plain: str, expected_hash: str, password: str) -> bool:
    if expected_hash:
        got = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(got, expected_hash)
    if expected_plain:
        return secrets.compare_digest(password, expected_plain)
    return False


def _authenticate(username: str, password: str) -> dict | None:
    """Return actor dict or None."""
    apply_env_overlays()
    user = (username or "").strip().lower()
    if user == "jonatan":
        plain = os.environ.get("DASHBOARD_PASSWORD", "").strip()
        hashed = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
        if _password_ok(plain, hashed, password):
            return {"name": "jonatan", "role": "viewer", "is_oscar": False}
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


def _auth_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        actor = None
        if auth is not None:
            actor = _authenticate(auth.username or "", auth.password or "")
        if actor is None:
            return Response(
                "Authentication required (jonatan or oscar)",
                401,
                {"WWW-Authenticate": 'Basic realm="Azom Dashboard"'},
            )
        g.actor = actor
        return view(*args, **kwargs)

    return wrapper


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


def _dashboard_context(**extra) -> dict:
    apply_env_overlays()
    runtime = runtime_status()
    actor = getattr(g, "actor", {"name": "?", "role": "?", "is_oscar": False})
    ctx = {
        "user": actor["name"],
        "role": actor["role"],
        "is_oscar": actor.get("is_oscar", False),
        "runtime": runtime,
        "flash": _flash_from_query(),
    }
    ctx.update(extra)
    return ctx


@app.before_request
def _load_overlays():
    apply_env_overlays()


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
    open_esc = sum(1 for e in escalations if e.get("status", "open") == "open")
    cap = float(runtime.get("openrouter_cap") or 100)
    cost_pct = min(100, int((cost / cap) * 100)) if cap else 0
    open_cases = 0
    escalated_cases = 0
    try:
        from ecom_ops.cases.service import CaseService

        cs = CaseService()
        open_cases = cs.store.count_by_status("open")
        escalated_cases = cs.store.count_by_status("escalated")
    except Exception:
        pass
    return render_template(
        "index.html",
        **_dashboard_context(
            telemetry=telemetry,
            escalations=escalations,
            cost_usd=round(cost, 4),
            openrouter_cap=runtime.get("openrouter_cap", 100),
            budget_cap_llm=runtime.get("budget_cap_llm", 80),
            open_escalations=open_esc,
            cost_pct=cost_pct,
            open_cases=open_cases,
            escalated_cases=escalated_cases,
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
    from ecom_ops.cases.service import CaseService

    status = request.args.get("status", "open,escalated")
    mailbox_id = request.args.get("mailbox") or None
    category = request.args.get("category") or None
    svc = CaseService()
    rows = svc.store.list_cases(
        status=status if status != "all" else None,
        mailbox_id=mailbox_id,
        category=category,
        limit=100,
    )
    return render_template(
        "cases.html",
        **_dashboard_context(
            cases=rows,
            status_filter=status,
            mailbox_filter=mailbox_id or "",
            category_filter=category or "",
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


@app.route("/cases/<case_id>", methods=["GET", "POST"])
@_auth_required
def case_detail(case_id: str):
    from ecom_ops.cases.service import CaseService

    svc = CaseService()
    if request.method == "POST":
        action = request.form.get("action", "reply")
        body = request.form.get("body") or ""
        if action == "reply":
            result = svc.approve_and_send(
                case_id, actor=g.actor["name"], body_override=body or None
            )
            if result.ok:
                return redirect(
                    url_for("case_detail", case_id=case_id) + "?msg=Skickat"
                )
            return redirect(
                url_for("case_detail", case_id=case_id) + f"?err={result.message}"
            )
        if action == "save_draft":
            result = svc.save_draft(case_id, body, actor=g.actor["name"])
            if result.ok:
                return redirect(
                    url_for("case_detail", case_id=case_id) + "?msg=Draft+sparad"
                )
            return redirect(
                url_for("case_detail", case_id=case_id) + f"?err={result.message}"
            )
        if action == "close":
            result = svc.close(case_id, actor=g.actor["name"], reason="dashboard")
            if result.ok:
                return redirect(url_for("cases_list") + "?msg=Stängt")
            return redirect(
                url_for("case_detail", case_id=case_id) + f"?err={result.message}"
            )

    case = svc.get(case_id)
    if not case:
        return Response("Case not found", 404)
    msgs = svc.store.messages(case_id)
    return render_template(
        "case_detail.html",
        **_dashboard_context(case=case, messages=msgs),
    )


@app.route("/oscar")
@_oscar_required
def oscar_home():
    esc = _tail_jsonl(_data_dir() / "escalations.jsonl", 100)
    open_esc = [e for e in esc if e.get("status", "open") == "open"]
    secs = secrets_status()
    present = sum(1 for s in secs if s["present"])
    missing = sum(1 for s in secs if not s["present"])
    from ecom_ops.oauth.gmail import GmailOAuthStore

    store = GmailOAuthStore()
    return render_template(
        "oscar.html",
        **_dashboard_context(
            open_escalations=open_esc,
            open_count=len(open_esc),
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
    return render_template(
        "oscar_secrets.html",
        **_dashboard_context(
            secrets=secrets_status(),
            editable_keys=EDITABLE_SECRET_KEYS,
        ),
    )


@app.route("/oscar/escalations", methods=["GET", "POST"])
@_oscar_required
def oscar_escalations():
    if request.method == "POST":
        ticket_id = request.form.get("ticket_id", "").strip()
        if ticket_id and resolve_escalation(ticket_id):
            return redirect(url_for("oscar_escalations") + "?msg=Markerad+som+löst")
        return redirect(url_for("oscar_escalations") + "?err=Ticket+hittades+inte")
    rows = _tail_jsonl(_data_dir() / "escalations.jsonl", 200)
    return render_template(
        "oscar_escalations.html",
        **_dashboard_context(rows=rows),
    )


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
    return jsonify({"ok": True, "service": "azom-dashboard"})


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
