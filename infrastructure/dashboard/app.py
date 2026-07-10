"""Flask webbdashboard för Jonatan (lösenordsskyddad, read-only)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
)

app = Flask(__name__)


def _data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def _check_password(password: str) -> bool:
    expected_hash = os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip()
    expected_plain = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if expected_hash:
        got = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(got, expected_hash)
    if expected_plain:
        return secrets.compare_digest(password, expected_plain)
    # Dev fallback only when mock mode is on AND no password configured
    dev = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    if not dev:
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
    return render_template(
        "index.html",
        telemetry=telemetry,
        escalations=escalations,
        cost_usd=round(cost, 4),
        user=os.environ.get("DASHBOARD_USER", "jonatan"),
        role="viewer (read-only)",
    )


@app.route("/health")
def health():
    # Unauthenticated for load balancer / docker / systemd probes
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

        # Jonatan is viewer — support_reply is denied; use agent for draft only
        # and surface that send is operator-only.
        result = SupportService().handle(message, actor="agent", language="sv")
        data = result.to_dict()
        data["note"] = "Draft only; Jonatan cannot send. Operator/Oscar sends."
        return jsonify(data)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    # Prefer localhost on bare-metal Ubuntu; override for containers
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    # Debug off by default
    app.run(host=host, port=port, debug=False)
