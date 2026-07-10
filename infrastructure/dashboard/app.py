"""Flask webbdashboard för Jonatan (lösenordsskyddad, read-only)."""

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
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

# Allow imports from repo root when run as script
_ROOT = Path(__file__).resolve().parents[2]
_DASH_DIR = Path(__file__).resolve().parent
if str(_ROOT / "skills") not in sys.path:
    sys.path.insert(0, str(_ROOT / "skills"))
if str(_DASH_DIR) not in sys.path:
    sys.path.insert(0, str(_DASH_DIR))

from status import health_probe, runtime_status, secrets_checklist  # noqa: E402

app = Flask(__name__)


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _is_mock() -> bool:
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


def _check_password(password: str) -> bool:
    expected_hash = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
    expected_plain = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if expected_hash:
        got = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(got, expected_hash)
    if expected_plain:
        return secrets.compare_digest(password, expected_plain)
    if not _is_mock():
        return False
    return password == "jonatan"


def _auth_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        user = os.environ.get("DASHBOARD_USER", "jonatan")
        if (
            auth is None
            or auth.username != user
            or not _check_password(auth.password or "")
        ):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Azom Dashboard"'},
            )
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


def _dashboard_context() -> dict:
    runtime = runtime_status()
    return {
        "user": os.environ.get("DASHBOARD_USER", "jonatan"),
        "role": "viewer (read-only)",
        "runtime": runtime,
    }


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
    ctx = _dashboard_context()
    ctx.update(
        {
            "telemetry": telemetry,
            "escalations": escalations,
            "cost_usd": round(cost, 4),
            "openrouter_cap": runtime.get("openrouter_cap", 100),
            "budget_cap_llm": runtime.get("budget_cap_llm", 80),
        }
    )
    return render_template("index.html", **ctx)


@app.route("/onboarding")
@_auth_required
def onboarding():
    probe = health_probe()
    ctx = _dashboard_context()
    ctx.update(
        {
            "secrets": secrets_checklist(),
            "health": probe,
        }
    )
    return render_template("onboarding.html", **ctx)


@app.route("/onboarding/status")
@_auth_required
def onboarding_status():
    return jsonify(
        {
            "runtime": runtime_status(),
            "secrets": secrets_checklist(),
            "health": health_probe(),
        }
    )


@app.route("/oauth/gmail/start")
@_auth_required
def oauth_gmail_start():
    from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured

    store = GmailOAuthStore()
    if _is_mock():
        store.mock_connect()
        return redirect(url_for("onboarding") + "?gmail=connected")

    if not gmail_oauth_configured():
        return Response(
            "Gmail OAuth not configured (MAIL_OAUTH_CLIENT_ID/SECRET)",
            400,
        )
    state = store.create_state()
    url = store.build_authorize_url(state=state)
    return redirect(url)


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

    return redirect(url_for("onboarding") + "?gmail=connected")


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
def telemetry():
    return jsonify(_tail_jsonl(_data_dir() / "telemetry.jsonl", 200))


@app.route("/escalations")
@_auth_required
def escalations():
    return jsonify(_tail_jsonl(_data_dir() / "escalations.jsonl", 200))


@app.route("/manage")
@_auth_required
def manage():
    return jsonify(
        {
            "message": "Manage is read-only for Jonatan. Escalate writes to Oscar.",
            "allowed": ["view_health", "view_logs", "view_telemetry"],
            "denied": ["order_update", "product_publish", "mail_send", "ssh_write"],
        }
    )


@app.route("/interact", methods=["GET", "POST"])
@_auth_required
def interact():
    if request.method == "GET":
        return jsonify(
            {
                "hint": "POST JSON {\"message\": \"...\"} for support draft (no send)",
            }
        )
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400
    try:
        from ecom_ops.actions.support import SupportService

        result = SupportService().handle(message, actor="agent", language="sv")
        data = result.to_dict()
        data["note"] = "Draft only; Jonatan cannot send. Operator/Oscar sends."
        return jsonify(data)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    app.run(host=host, port=port, debug=False)
