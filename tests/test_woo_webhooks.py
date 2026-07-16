"""Tests for V2.1 WooCommerce webhook receiver (HMAC-SHA256)."""

from __future__ import annotations

import hashlib
import hmac
import json

from ecom_ops.integrations.webhooks import (
    WebhookReceiver,
    parse_webhook_topic,
    verify_webhook_signature,
)

SECRET = "test_secret_123"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #


def test_valid_signature():
    body = b'{"id": 1001, "status": "processing"}'
    sig = _sign(body)
    assert verify_webhook_signature(body, sig, SECRET) is True


def test_invalid_signature():
    body = b'{"id": 1001}'
    assert verify_webhook_signature(body, "wrong_signature", SECRET) is False


def test_empty_secret():
    assert verify_webhook_signature(b"body", "somesig", "") is False


def test_empty_signature():
    assert verify_webhook_signature(b"body", "", SECRET) is False


def test_tampered_body():
    body = b'{"id": 1001}'
    sig = _sign(body)
    tampered = b'{"id": 9999}'
    assert verify_webhook_signature(tampered, sig, SECRET) is False


def test_signature_case_insensitive():
    body = b'{"id": 1001}'
    sig = _sign(body).upper()
    assert verify_webhook_signature(body, sig, SECRET) is True


# --------------------------------------------------------------------------- #
# Topic parsing
# --------------------------------------------------------------------------- #


def test_parse_topic_order_updated():
    assert parse_webhook_topic("order.updated") == ("order", "updated")


def test_parse_topic_product_created():
    assert parse_webhook_topic("product.created") == ("product", "created")


def test_parse_topic_invalid_no_dot():
    assert parse_webhook_topic("orderupdated") is None


def test_parse_topic_empty():
    assert parse_webhook_topic("") is None


# --------------------------------------------------------------------------- #
# Receiver dispatch
# --------------------------------------------------------------------------- #


def test_handle_raw_valid():
    body = json.dumps({"id": 1001, "status": "completed"}).encode()
    sig = _sign(body)
    received = []
    receiver = WebhookReceiver(secret=SECRET)
    receiver.on("order.updated", lambda e: received.append(e))
    ok = receiver.handle_raw(body, sig, "order.updated")
    assert ok is True
    assert len(received) == 1
    event = received[0]
    assert event.topic == "order.updated"
    assert event.resource == "order"
    assert event.action == "updated"
    assert event.resource_id == "1001"


def test_handle_raw_invalid_signature():
    body = b'{"id": 1001}'
    received = []
    receiver = WebhookReceiver(secret=SECRET)
    receiver.on("order.updated", lambda e: received.append(e))
    ok = receiver.handle_raw(body, "wrong_sig", "order.updated")
    assert ok is False
    assert received == []


def test_resource_level_handler():
    body = json.dumps({"id": 501}).encode()
    sig = _sign(body)
    received = []
    receiver = WebhookReceiver(secret=SECRET)
    receiver.on("product", lambda e: received.append(e))
    ok = receiver.handle_raw(body, sig, "product.updated")
    assert ok is True
    assert len(received) == 1
    assert received[0].resource == "product"


def test_topic_handler_takes_precedence():
    body = json.dumps({"id": 1001}).encode()
    sig = _sign(body)
    received = []
    receiver = WebhookReceiver(secret=SECRET)
    receiver.on("order", lambda e: received.append(("resource", e)))
    receiver.on("order.updated", lambda e: received.append(("topic", e)))
    receiver.handle_raw(body, sig, "order.updated")
    assert received[0][0] == "topic"
    assert received[1][0] == "resource"
    assert len(received) == 2


def test_handler_exception_does_not_propagate():
    """Handler errors are logged but don't break the receiver (Woo disables
    webhooks after 5 failed deliveries — return 200 quickly)."""
    body = json.dumps({"id": 1001}).encode()
    sig = _sign(body)

    def bad_handler(e):
        raise RuntimeError("boom")

    receiver = WebhookReceiver(secret=SECRET)
    receiver.on("order.updated", bad_handler)
    receiver.on("order.updated", lambda e: None)
    ok = receiver.handle_raw(body, sig, "order.updated")
    assert ok is True  # signature was valid, handler error logged


def test_handle_raw_invalid_json():
    body = b"not json"
    sig = _sign(body)
    receiver = WebhookReceiver(secret=SECRET)
    ok = receiver.handle_raw(body, sig, "order.updated")
    assert ok is False


def test_handle_raw_empty_body():
    body = b""
    sig = _sign(body)
    receiver = WebhookReceiver(secret=SECRET)
    ok = receiver.handle_raw(body, sig, "order.updated")
    assert ok is True  # valid signature, empty payload -> empty event


# --------------------------------------------------------------------------- #
# Flask-style request
# --------------------------------------------------------------------------- #


class _FakeRequest:
    def __init__(self, data, headers):
        self.data = data
        self.headers = headers


def test_handle_request_flask_style():
    body = json.dumps({"id": 1001}).encode()
    sig = _sign(body)
    req = _FakeRequest(
        data=body,
        headers={"X-WC-Webhook-Signature": sig, "X-WC-Webhook-Topic": "order.created"},
    )
    received = []
    receiver = WebhookReceiver(secret=SECRET)
    receiver.on("order", lambda e: received.append(e))
    ok = receiver.handle_request(req)
    assert ok is True
    assert len(received) == 1
    assert received[0].action == "created"


def test_handle_request_missing_signature():
    req = _FakeRequest(data=b'{"id":1}', headers={})
    receiver = WebhookReceiver(secret=SECRET)
    ok = receiver.handle_request(req)
    assert ok is False
