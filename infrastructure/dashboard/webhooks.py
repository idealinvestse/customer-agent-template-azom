"""Woo + Messenger webhook routes (registered on the Flask app)."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from flask import Flask, Response, jsonify, request

_woo_webhook_receiver: object | None = None
_woo_webhook_secret_hash: str | None = None


def _get_woo_webhook_receiver(secret: str) -> Any:
    global _woo_webhook_receiver, _woo_webhook_secret_hash

    secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    if _woo_webhook_receiver is None or _woo_webhook_secret_hash != secret_hash:
        from ecom_ops.integrations.webhooks import WebhookReceiver

        _woo_webhook_receiver = WebhookReceiver(secret=secret)
        _woo_webhook_secret_hash = secret_hash
    return _woo_webhook_receiver


def register_webhook_routes(app: Flask) -> None:
    @app.route("/webhooks/woo", methods=["POST"])
    def woo_webhook():
        secret = os.environ.get("WOO_WEBHOOK_SECRET", "")
        if not secret:
            return jsonify({"ok": False, "error": "WOO_WEBHOOK_SECRET not configured"}), 503
        receiver = _get_woo_webhook_receiver(secret)
        ok = receiver.handle_request(request)
        if not ok:
            return jsonify({"ok": False, "error": "Invalid signature"}), 401
        return jsonify({"ok": True}), 200

    @app.route("/webhooks/messenger", methods=["GET", "POST"])
    def messenger_webhook():
        from pathlib import Path

        from ecom_ops.bot.handlers import BotHandler
        from ecom_ops.bot.messenger_adapter import (
            mid_already_seen,
            parse_webhook_payload,
            process_inbound,
            send_bot_reply,
            verify_signature,
            verify_webhook_challenge,
        )
        from ecom_ops.bot.store import ConversationStore

        if request.method == "GET":
            challenge = verify_webhook_challenge(
                mode=request.args.get("hub.mode"),
                token=request.args.get("hub.verify_token"),
                challenge=request.args.get("hub.challenge"),
            )
            if challenge is None:
                return "Forbidden", 403
            return Response(challenge, mimetype="text/plain")

        raw = request.get_data() or b""
        if not verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
            return jsonify({"ok": False, "error": "Invalid signature"}), 401

        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            payload = {}
        events = parse_webhook_payload(payload if isinstance(payload, dict) else {})
        data_dir = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
        store = ConversationStore(path=data_dir / "messenger_state.json")
        handler = BotHandler(store=store, channel="messenger")
        page_token = (os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()
        dry = not page_token
        if dry:
            app.logger.error(
                "MESSENGER_PAGE_ACCESS_TOKEN missing — outbound Messenger replies suppressed"
            )
        handled = 0
        errors = 0
        skipped = 0
        for ev in events:
            try:
                if mid_already_seen(data_dir, ev.mid):
                    skipped += 1
                    continue
                reply = process_inbound(ev, handler)
                send_bot_reply(ev.peer_id, reply, dry_run=dry, page_token=page_token or None)
                handled += 1
            except Exception as exc:
                errors += 1
                app.logger.warning("Messenger event error: %s", exc)
        return jsonify(
            {
                "ok": errors == 0,
                "handled": handled,
                "errors": errors,
                "skipped": skipped,
            }
        ), 200
