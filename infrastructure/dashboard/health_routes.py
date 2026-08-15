"""Public /health /live /ready routes."""

from __future__ import annotations

import os

from flask import Flask, jsonify


def register_health_routes(app: Flask) -> None:
    @app.route("/health")
    def health():
        from ecom_ops.bot.actors import channel_posture
        from ecom_ops.ops_status import health_extras, readiness_from_last_poll

        readiness = readiness_from_last_poll()
        extras = health_extras()
        page_token = bool((os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip())
        posture = channel_posture("messenger")
        messenger = {
            "configured": bool(
                (os.environ.get("MESSENGER_APP_SECRET") or "").strip()
                and (os.environ.get("MESSENGER_VERIFY_TOKEN") or "").strip()
            ),
            "page_token_present": page_token,
            "send_enabled": page_token,
            "allowlist_set": bool(posture.get("allowlist_set")),
            "actor_map_set": bool(posture.get("actor_map_set")),
            "fail_closed": bool(posture.get("fail_closed")),
            "mutations_enabled": page_token
            or (os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}),
        }
        return jsonify(
            {
                "ok": True,
                "service": "azom-dashboard",
                "liveness": True,
                "readiness": readiness,
                "messenger": messenger,
                "null_send": extras.get("null_send"),
                "budget_ratio": extras.get("budget_ratio"),
                "budget_pacing_warn": extras.get("budget_pacing_warn"),
                "last_poll_errors": extras.get("last_poll_errors"),
            }
        )

    @app.route("/live")
    def liveness():
        return jsonify({"ok": True, "liveness": True}), 200

    @app.route("/ready")
    def readiness():
        from ecom_ops.ops_status import readiness_from_last_poll

        rd = readiness_from_last_poll()
        ok = bool(rd.get("ready", True))
        return jsonify({"ok": ok, "readiness": rd}), (200 if ok else 503)
