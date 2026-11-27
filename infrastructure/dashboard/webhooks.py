"""Woo + Messenger webhook routes (registered on the Flask app)."""

from __future__ import annotations

import hashlib
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

_woo_webhook_receiver: object | None = None
_woo_webhook_secret_hash: str | None = None
_woo_receiver_lock = threading.Lock()
_MAX_WEBHOOK_BODY_BYTES = 1_048_576
_messenger_stores_lock = threading.Lock()
_messenger_stores: dict[str, Any] = {}
_messenger_proposal_stores_lock = threading.Lock()
_messenger_proposal_stores: dict[str, Any] = {}
_pending_images_lock = threading.Lock()
_pending_image_urls: dict[str, deque[tuple[str, list[str]]]] = {}
_MAX_PENDING_PEERS = 256
_MAX_PENDING_PER_PEER = 32


def _messenger_peer_authorized(peer_id: str) -> bool:
    """Fail-closed check: both Messenger PSID allowlist and actor map must authorize ``peer_id``."""
    pid = str(peer_id or "").strip()
    if not pid:
        return False
    from ecom_ops.bot.actors import channel_peer_allowed, resolve_channel_actor

    if not channel_peer_allowed("messenger", pid):
        return False
    try:
        actor = resolve_channel_actor("messenger", pid)
    except Exception:
        return False
    return bool(str(actor or "").strip())


def _get_messenger_store(data_dir: Path) -> Any:
    from ecom_ops.bot.store import ConversationStore

    store_path = (data_dir / "messenger_state.json").resolve()
    key = str(store_path)
    with _messenger_stores_lock:
        store = _messenger_stores.get(key)
        if store is None:
            store = ConversationStore(path=store_path)
            _messenger_stores[key] = store
        return store


def _get_messenger_proposal_store(data_dir: Path) -> Any:
    from ecom_ops.bot.job_store import TelegramJobStore

    db_path = (data_dir / "messenger_proposals.sqlite3").resolve()
    key = str(db_path)
    with _messenger_proposal_stores_lock:
        store = _messenger_proposal_stores.get(key)
        if store is None:
            store = TelegramJobStore(path=db_path)
            _messenger_proposal_stores[key] = store
        return store


def _extract_event_image_urls(ev: Any) -> list[str]:
    raw = ev.get("image_urls") if isinstance(ev, dict) else getattr(ev, "image_urls", None)
    if not raw or not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        url = str(item or "").strip()
        if url.startswith("https://"):
            out.append(url)
        if len(out) >= 4:
            break
    return out


def _enqueue_pending_images(peer_id: str, text: str, urls: list[str]) -> None:
    if not peer_id:
        return
    with _pending_images_lock:
        if peer_id not in _pending_image_urls and len(_pending_image_urls) >= _MAX_PENDING_PEERS:
            oldest = next(iter(_pending_image_urls))
            _pending_image_urls.pop(oldest, None)
        bucket = _pending_image_urls.setdefault(peer_id, deque(maxlen=_MAX_PENDING_PER_PEER))
        bucket.append((str(text or ""), list(urls[:4])))


def _pop_pending_images(peer_id: str, text: str) -> list[str]:
    if not peer_id:
        return []
    expected_text = str(text or "")
    with _pending_images_lock:
        bucket = _pending_image_urls.get(peer_id)
        if not bucket:
            return []
        if bucket[0][0] == expected_text:
            _, urls = bucket.popleft()
            if not bucket:
                _pending_image_urls.pop(peer_id, None)
            return urls
        for idx, (queued_text, urls) in enumerate(bucket):
            if queued_text == expected_text:
                del bucket[idx]
                if not bucket:
                    _pending_image_urls.pop(peer_id, None)
                return urls
        return []


class _MessengerImageHandlerProxy:
    """Thin proxy forwarding downloaded inbound Messenger images into BotHandler.handle()."""

    def __init__(self, inner: Any, *, data_dir: Path) -> None:
        self._inner = inner
        self._data_dir = data_dir

    def handle(
        self,
        chat_id: str,
        text: str,
        *args: Any,
        image_paths: list[str] | None = None,
        image_urls: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        urls = list(image_urls[:4]) if image_urls else _pop_pending_images(str(chat_id), text)
        resolved_paths = list(image_paths) if image_paths else []
        if urls and not resolved_paths and _messenger_peer_authorized(str(chat_id)):
            from ecom_ops.bot import messenger_adapter

            downloader = getattr(messenger_adapter, "download_messenger_image", None)
            if callable(downloader):
                for url in urls[:4]:
                    try:
                        saved = downloader(url, peer_id=str(chat_id), data_dir=self._data_dir)
                    except Exception:
                        saved = None
                    if saved is not None:
                        resolved_paths.append(str(saved))
        if resolved_paths:
            return self._inner.handle(
                chat_id,
                text,
                *args,
                image_paths=resolved_paths,
                **kwargs,
            )
        return self._inner.handle(chat_id, text, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _get_woo_webhook_receiver(secret: str) -> Any:
    global _woo_webhook_receiver, _woo_webhook_secret_hash

    secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    with _woo_receiver_lock:
        if _woo_webhook_receiver is None or _woo_webhook_secret_hash != secret_hash:
            from ecom_ops.integrations.webhooks import WebhookReceiver

            _woo_webhook_receiver = WebhookReceiver(secret=secret)
            _woo_webhook_secret_hash = secret_hash
        return _woo_webhook_receiver


def register_webhook_routes(app: Flask) -> None:
    @app.route("/webhooks/woo", methods=["POST"])
    def woo_webhook():
        if request.content_length and request.content_length > _MAX_WEBHOOK_BODY_BYTES:
            return jsonify({"ok": False, "error": "Payload too large"}), 413
        raw_check = request.get_data(cache=True) or b""
        if len(raw_check) > _MAX_WEBHOOK_BODY_BYTES:
            return jsonify({"ok": False, "error": "Payload too large"}), 413
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
        from ecom_ops.bot.handlers import BotHandler
        from ecom_ops.bot.messenger_adapter import (
            parse_webhook_payload,
            verify_signature,
            verify_webhook_challenge,
        )
        from ecom_ops.bot.messenger_queue import get_messenger_queue

        if request.method == "GET":
            challenge = verify_webhook_challenge(
                mode=request.args.get("hub.mode"),
                token=request.args.get("hub.verify_token"),
                challenge=request.args.get("hub.challenge"),
            )
            if challenge is None:
                return "Forbidden", 403
            return Response(challenge, mimetype="text/plain")

        if request.content_length and request.content_length > _MAX_WEBHOOK_BODY_BYTES:
            return jsonify({"ok": False, "error": "Payload too large"}), 413
        raw = request.get_data() or b""
        if len(raw) > _MAX_WEBHOOK_BODY_BYTES:
            return jsonify({"ok": False, "error": "Payload too large"}), 413
        if not verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
            return jsonify({"ok": False, "error": "Invalid signature"}), 401

        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            payload = {}
        events = parse_webhook_payload(payload if isinstance(payload, dict) else {})
        data_dir = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
        page_token = (os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()
        dry = not page_token
        if dry:
            app.logger.error(
                "MESSENGER_PAGE_ACCESS_TOKEN missing — outbound Messenger replies suppressed"
            )

        def _make_handler() -> Any:
            store = _get_messenger_store(data_dir)
            proposal_store = _get_messenger_proposal_store(data_dir)
            inner = BotHandler(store=store, proposal_store=proposal_store, channel="messenger")
            return _MessengerImageHandlerProxy(inner, data_dir=data_dir)

        queue = get_messenger_queue(data_dir)
        queued = 0
        skipped = 0
        for ev in events:
            enqueued, _ = queue.enqueue(ev)
            if enqueued:
                queued += 1
                if not getattr(ev, "postback", None) and _messenger_peer_authorized(str(ev.peer_id)):
                    img_urls = _extract_event_image_urls(ev)
                    if img_urls:
                        _enqueue_pending_images(str(ev.peer_id), str(ev.text or ""), img_urls)
            else:
                skipped += 1

        run_sync = bool(app.config.get("TESTING")) and os.environ.get(
            "AZOM_MESSENGER_ASYNC_IN_TESTS"
        ) != "1"
        if run_sync:
            handled, errors = queue.drain_pending_sync(
                handler_factory=_make_handler,
                page_token=page_token or None,
                dry_run=dry,
            )
            return jsonify(
                {
                    "ok": errors == 0,
                    "handled": handled,
                    "errors": errors,
                    "skipped": skipped,
                }
            ), 200

        if queued:
            queue.ensure_started(
                handler_factory=_make_handler,
                page_token=page_token or None,
                dry_run=dry,
            )
        return jsonify(
            {
                "ok": True,
                "handled": queued,
                "queued": queued,
                "errors": 0,
                "skipped": skipped,
            }
        ), 200
