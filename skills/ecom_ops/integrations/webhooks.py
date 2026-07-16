"""WooCommerce webhook receiver + HMAC-SHA256 verification.

V2.1 (see docs/solutions/2026-07-17-woo-wordpress-capacity-review.md §P2.6):
- ``verify_webhook_signature`` validates the ``X-WC-Webhook-Signature``
  header against the HMAC-SHA256 of the raw body using the webhook secret.
- ``WebhookEvent`` dataclass normalizes the payload (topic, resource, action,
  order/product/customer/coupon id).
- ``WebhookReceiver`` dispatches verified events to registered handlers.
- Woo disables webhooks after 5 failed deliveries — handlers should be fast
  and idempotent; failures are logged but not propagated to the Woo sender
  (return 200 quickly, process async).

Usage in a Flask app (e.g. dashboard):

    receiver = WebhookReceiver(secret=os.environ["WOO_WEBHOOK_SECRET"])
    @app.route("/webhooks/woo", methods=["POST"])
    def woo_webhook():
        ok = receiver.handle_request(request)
        return ("", 200) if ok else ("", 401)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Event model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WebhookEvent:
    """Normalized WooCommerce webhook event."""

    topic: str  # e.g. "order.updated"
    resource: str  # "order" | "product" | "customer" | "coupon"
    action: str  # "created" | "updated" | "deleted" | "restored"
    resource_id: str | None = None
    raw: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #


def verify_webhook_signature(
    raw_body: bytes,
    signature: str,
    secret: str,
) -> bool:
    """Verify WooCommerce webhook HMAC-SHA256 signature.

    Woo computes ``hmac_sha256(raw_body, secret)`` and sends it as
    ``X-WC-Webhook-Signature`` (hex lowercase).
    """
    if not secret or not signature:
        return False
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def parse_webhook_topic(topic: str) -> tuple[str, str] | None:
    """Parse ``order.updated`` → ``("order", "updated")``."""
    if not topic or "." not in topic:
        return None
    parts = topic.split(".", 1)
    if len(parts) != 2:
        return None
    resource, action = parts
    if not resource or not action:
        return None
    return resource, action


# --------------------------------------------------------------------------- #
# Receiver
# --------------------------------------------------------------------------- #


HandlerFn = Callable[[WebhookEvent], None]


class WebhookReceiver:
    """Receive + verify + dispatch WooCommerce webhooks.

    Handlers are registered per resource (``order``, ``product``, etc.) or
    per full topic (``order.updated``). Topic-specific handlers take
    precedence over resource-level handlers.
    """

    def __init__(self, *, secret: str) -> None:
        self.secret = secret
        self._topic_handlers: dict[str, list[HandlerFn]] = {}
        self._resource_handlers: dict[str, list[HandlerFn]] = {}

    def on(self, topic: str, handler: HandlerFn) -> None:
        """Register a handler for a topic (``order.updated``) or resource (``order``)."""
        if "." in topic:
            self._topic_handlers.setdefault(topic, []).append(handler)
        else:
            self._resource_handlers.setdefault(topic, []).append(handler)

    def handle_request(self, request: Any) -> bool:
        """Handle a Flask/Django-style request object.

        Expects ``request.data`` (raw bytes), ``request.headers`` (dict-like
        with ``X-WC-Webhook-Signature`` and ``X-WC-Webhook-Topic``).

        Returns True if signature verified (handlers run), False otherwise.
        """
        raw_body = getattr(request, "data", None) or b""
        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")
        headers = getattr(request, "headers", {}) or {}
        signature = headers.get("X-WC-Webhook-Signature", "")
        topic = headers.get("X-WC-Webhook-Topic", "")
        return self.handle_raw(raw_body, signature, topic)

    def handle_raw(
        self,
        raw_body: bytes,
        signature: str,
        topic: str,
    ) -> bool:
        """Verify + parse + dispatch. Returns True if signature valid."""
        if not verify_webhook_signature(raw_body, signature, self.secret):
            logger.warning("Woo webhook signature mismatch (topic=%s)", topic)
            return False
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Woo webhook body parse error: %s", exc)
            return False
        event = self._build_event(topic, payload)
        self._dispatch(event)
        return True

    def _build_event(self, topic: str, payload: dict[str, Any]) -> WebhookEvent:
        parsed = parse_webhook_topic(topic)
        resource = parsed[0] if parsed else ""
        action = parsed[1] if parsed else ""
        resource_id = None
        if isinstance(payload, dict):
            resource_id = str(payload.get("id") or "")
        return WebhookEvent(
            topic=topic,
            resource=resource,
            action=action,
            resource_id=resource_id or None,
            raw=payload if isinstance(payload, dict) else {},
        )

    def _dispatch(self, event: WebhookEvent) -> None:
        handlers: list[HandlerFn] = []
        if event.topic:
            handlers.extend(self._topic_handlers.get(event.topic, []))
        if event.resource:
            handlers.extend(self._resource_handlers.get(event.resource, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception(
                    "Woo webhook handler error (topic=%s, id=%s)",
                    event.topic,
                    event.resource_id,
                )
