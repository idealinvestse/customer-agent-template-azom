"""Meta Messenger Platform adapter — webhook parse + Send API."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ecom_ops.bot.reply import ActionMarkup, BotReply, as_reply

log = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


@dataclass
class InboundEvent:
    peer_id: str
    text: str | None = None
    postback: str | None = None
    mid: str | None = None


def verify_webhook_challenge(
    *,
    mode: str | None,
    token: str | None,
    challenge: str | None,
    expected_token: str | None = None,
) -> str | None:
    """Return challenge string if GET verify succeeds, else None."""
    expected = (expected_token or os.environ.get("MESSENGER_VERIFY_TOKEN") or "").strip()
    if not expected:
        return None
    if mode == "subscribe" and token == expected and challenge is not None:
        return str(challenge)
    return None


def verify_signature(raw_body: bytes, signature_header: str | None, *, app_secret: str | None = None) -> bool:
    """Validate X-Hub-Signature-256 header."""
    secret = (app_secret or os.environ.get("MESSENGER_APP_SECRET") or "").strip()
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header.split("=", 1)[1].strip()
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def parse_webhook_payload(payload: dict[str, Any]) -> list[InboundEvent]:
    """Extract text/postback events from a Messenger webhook body."""
    events: list[InboundEvent] = []
    if not isinstance(payload, dict):
        return events
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for messaging in entry.get("messaging") or []:
            if not isinstance(messaging, dict):
                continue
            sender = messaging.get("sender") or {}
            peer = str(sender.get("id") or "").strip()
            if not peer:
                continue
            if "message" in messaging and isinstance(messaging["message"], dict):
                msg = messaging["message"]
                if msg.get("is_echo"):
                    continue
                text = msg.get("text")
                if text:
                    events.append(
                        InboundEvent(
                            peer_id=peer,
                            text=str(text),
                            mid=str(msg.get("mid") or "") or None,
                        )
                    )
            if "postback" in messaging and isinstance(messaging["postback"], dict):
                pb = messaging["postback"]
                payload_s = pb.get("payload")
                if payload_s:
                    events.append(
                        InboundEvent(
                            peer_id=peer,
                            postback=str(payload_s),
                            mid=str(pb.get("mid") or "") or None,
                        )
                    )
    return events


def actions_to_messenger_buttons(actions: ActionMarkup | None) -> list[dict[str, Any]]:
    """Flatten ActionMarkup to Messenger buttons (max 3).

    Priority: web_url handoff first, then approve postbacks, then show, then rest.
    Ensures dashboard deep links survive the 3-button cap.
    """
    if not actions:
        return []
    flat: list[ActionButton] = []
    for row in actions.rows:
        flat.extend(row)

    def _rank(btn: ActionButton) -> tuple[int, int]:
        if btn.url:
            return (0, 0)
        p = (btn.payload or "").lower()
        if "approve" in p:
            return (1, 0)
        if "show" in p or p.startswith("cases:regen"):
            return (2, 0)
        if p in {"action:yes", "escalate:yes"} or p.startswith("order:set") or p.startswith(
            "product:desc"
        ):
            return (1, 1)
        if "list" in p or p.endswith(":cancel") or p in {"action:no", "escalate:no"}:
            return (4, 0)
        return (3, 0)

    flat_sorted = sorted(enumerate(flat), key=lambda it: (_rank(it[1]), it[0]))
    buttons: list[dict[str, Any]] = []
    for _, btn in flat_sorted:
        if len(buttons) >= 3:
            break
        if btn.url:
            buttons.append(
                {"type": "web_url", "url": btn.url, "title": btn.label[:20]}
            )
        elif btn.payload:
            buttons.append(
                {
                    "type": "postback",
                    "title": btn.label[:20],
                    "payload": btn.payload[:1000],
                }
            )
    return buttons


def reply_to_messenger_messages(reply: BotReply | str) -> list[dict[str, Any]]:
    """Build Send API message objects from BotReply."""
    from ecom_ops.bot.dashboard_links import link_footer
    from ecom_ops.bot.reply import telegram_markup_to_actions

    br = as_reply(reply)
    text = (br.text or "")[:2000]
    actions = br.actions
    if not actions and br.reply_markup:
        actions = telegram_markup_to_actions(br.reply_markup)
    buttons = actions_to_messenger_buttons(actions)
    # If we had a URL action that didn't fit, append text footer
    if actions:
        urls = [b.url for row in actions.rows for b in row if b.url]
        shown_urls = {b["url"] for b in buttons if b.get("type") == "web_url"}
        for u in urls:
            if u and u not in shown_urls:
                foot = link_footer(u)
                if foot and foot not in text:
                    text = f"{text.rstrip()}\n\n{foot}"
                break
    messages: list[dict[str, Any]] = []
    if buttons:
        messages.append(
            {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "button",
                        "text": text[:640] or "Välj:",
                        "buttons": buttons,
                    },
                }
            }
        )
        if len(text) > 640:
            messages.insert(0, {"text": text})
    else:
        messages.append({"text": text or "(tomt svar)"})
    return messages


def send_messenger_message(
    peer_id: str,
    message: dict[str, Any],
    *,
    page_token: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """POST to Graph Send API. dry_run returns payload without network."""
    token = (page_token or os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()
    body = {
        "recipient": {"id": str(peer_id)},
        "messaging_type": "RESPONSE",
        "message": message,
    }
    if dry_run or not token:
        return {"ok": True, "dry_run": True, "body": body}
    url = f"{GRAPH_API}/me/messages?access_token={token}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        log.warning("Messenger send failed: %s %s", exc.code, err_body)
        return {"ok": False, "error": err_body, "status": exc.code}
    except Exception as exc:
        log.warning("Messenger send error: %s", exc)
        return {"ok": False, "error": str(exc)[:300]}


def send_bot_reply(
    peer_id: str,
    reply: BotReply | str,
    *,
    page_token: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Send all message parts for a BotReply."""
    results = []
    for msg in reply_to_messenger_messages(reply):
        results.append(
            send_messenger_message(
                peer_id, msg, page_token=page_token, dry_run=dry_run
            )
        )
    return results


def process_inbound(
    event: InboundEvent,
    handler: Any,
) -> BotReply:
    """Route InboundEvent through BotHandler."""
    if event.postback:
        return handler.handle_callback(event.peer_id, event.postback)
    return handler.handle(event.peer_id, event.text or "")
