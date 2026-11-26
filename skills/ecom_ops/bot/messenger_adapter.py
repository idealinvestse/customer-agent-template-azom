"""Meta Messenger Platform adapter — webhook parse + Send API."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ecom_ops.bot.reply import ActionButton, ActionMarkup, BotReply, as_reply
from ecom_ops.model_router.base import detect_image_format
from ecom_ops.runtime_profile import null_send_active

log = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"
_MAX_INBOUND_IMAGES = 4
_MAX_OUTBOUND_IMAGES = 4
_MAX_QUICK_REPLIES = 13
_MAX_MESSENGER_IMAGE_BYTES = 5 * 1024 * 1024
_TRUE_FLAGS = frozenset({"1", "true", "yes", "on"})
_FALSE_FLAGS = frozenset({"0", "false", "no", "off"})


@dataclass
class InboundEvent:
    peer_id: str
    text: str | None = None
    postback: str | None = None
    mid: str | None = None
    image_urls: tuple[str, ...] = ()
    image_paths: tuple[str, ...] = ()
    kind: str = ""
    payload: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.image_urls, list):
            self.image_urls = tuple(self.image_urls)
        if isinstance(self.image_paths, list):
            self.image_paths = tuple(self.image_paths)
        if not self.payload and self.postback:
            self.payload = self.postback
        if not self.postback and self.payload and self.kind == "callback":
            self.postback = self.payload
        if not self.kind:
            self.kind = "callback" if self.postback else "message"

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)


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
    if (
        mode == "subscribe"
        and token is not None
        and hmac.compare_digest(str(token), expected)
        and challenge is not None
    ):
        return str(challenge)
    return None


def verify_signature(
    raw_body: bytes, signature_header: str | None, *, app_secret: str | None = None
) -> bool:
    """Validate X-Hub-Signature-256 header."""
    secret = (app_secret or os.environ.get("MESSENGER_APP_SECRET") or "").strip()
    if not secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header.split("=", 1)[1].strip()
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def _extract_image_urls(msg: dict[str, Any]) -> tuple[str, ...]:
    urls: list[str] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        if str(att.get("type") or "").strip().lower() != "image":
            continue
        att_payload = att.get("payload") or {}
        if not isinstance(att_payload, dict):
            continue
        raw_url = str(att_payload.get("url") or "").strip()
        if raw_url.lower().startswith("https://"):
            urls.append(raw_url)
            if len(urls) >= _MAX_INBOUND_IMAGES:
                break
    return tuple(urls)


def parse_webhook_payload(payload: dict[str, Any]) -> list[InboundEvent]:
    """Extract text, quick_reply, image attachment, and postback events."""
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
                mid = str(msg.get("mid") or "") or None
                qr = msg.get("quick_reply")
                if isinstance(qr, dict) and qr.get("payload"):
                    qr_payload = str(qr["payload"])
                    events.append(
                        InboundEvent(
                            peer_id=peer,
                            text=str(msg.get("text")) if msg.get("text") else None,
                            postback=qr_payload,
                            mid=mid,
                            kind="callback",
                            payload=qr_payload,
                        )
                    )
                else:
                    text = msg.get("text")
                    image_urls = _extract_image_urls(msg)
                    if text or image_urls:
                        events.append(
                            InboundEvent(
                                peer_id=peer,
                                text=str(text) if text else None,
                                mid=mid,
                                image_urls=image_urls,
                                kind="message",
                            )
                        )
            if "postback" in messaging and isinstance(messaging["postback"], dict):
                pb = messaging["postback"]
                payload_s = pb.get("payload")
                if payload_s:
                    pb_str = str(payload_s)
                    events.append(
                        InboundEvent(
                            peer_id=peer,
                            postback=pb_str,
                            mid=str(pb.get("mid") or "") or None,
                            kind="callback",
                            payload=pb_str,
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
        if p in {"action:yes", "escalate:yes"} or p.startswith(
            ("order:set", "product:desc", "wpfile:stage", "wpreview:stage", "wpreview:push_draft")
        ):
            return (1, 1)
        if p.startswith(("clarify:go:", "clarify:pick:")):
            return (1, 2)
        if p.startswith("clarify:other:"):
            return (3, 5)
        if (
            "list" in p
            or p.endswith(":cancel")
            or p.startswith("clarify:cancel:")
            or p in {"action:no", "escalate:no"}
        ):
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


def actions_to_messenger_quick_replies(actions: ActionMarkup | None) -> list[dict[str, Any]]:
    """Convert ActionMarkup postbacks into Messenger Quick Replies (max 13)."""
    if not actions or not actions.rows:
        return []
    quick_replies: list[dict[str, Any]] = []
    for row in actions.rows:
        for btn in row:
            if not btn.payload:
                continue
            title = " ".join((btn.label or "").split())[:20] or "Val"
            quick_replies.append(
                {
                    "content_type": "text",
                    "title": title,
                    "payload": btn.payload[:1000],
                }
            )
            if len(quick_replies) >= _MAX_QUICK_REPLIES:
                return quick_replies
    return quick_replies


def _should_use_quick_replies(actions: ActionMarkup | None) -> bool:
    if not actions or not actions.rows:
        return False
    postbacks = [btn for row in actions.rows for btn in row if btn.payload]
    if not postbacks:
        return False
    if any((btn.payload or "").lower().startswith("clarify:") for btn in postbacks):
        return True
    return len(postbacks) > 3


_MESSENGER_CHUNK_LIMIT = 1950


def _chunk_messenger_text(text: str, *, limit: int = _MESSENGER_CHUNK_LIMIT) -> list[str]:
    """Split Messenger reply into readable chunks <= ``limit`` without cutting words."""
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= limit:
        return [clean]
    chunks: list[str] = []
    rest = clean
    while len(rest) > limit:
        window = rest[:limit]
        split_at = window.rfind("\n\n")
        if split_at < limit // 3:
            split_at = window.rfind("\n")
        if split_at < limit // 3:
            split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = limit
        chunks.append(rest[:split_at].rstrip())
        rest = rest[split_at:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def reply_to_messenger_messages(reply: BotReply | str) -> list[dict[str, Any]]:
    """Build Send API message objects from BotReply with paragraph-aware chunking."""
    from ecom_ops.bot.dashboard_links import link_footer
    from ecom_ops.bot.reply import telegram_markup_to_actions

    br = as_reply(reply)
    text = (br.text or "").strip()
    actions = br.actions
    if not actions and br.reply_markup:
        actions = telegram_markup_to_actions(br.reply_markup)

    if _should_use_quick_replies(actions):
        quick_replies = actions_to_messenger_quick_replies(actions)
        if actions:
            for b in (btn for row in actions.rows for btn in row if btn.url):
                foot = link_footer(b.url)
                if foot and foot not in text:
                    text = f"{text.rstrip()}\n\n{foot}".strip()
        chunks = _chunk_messenger_text(text) or ["Välj:"]
        messages: list[dict[str, Any]] = [{"text": chunk} for chunk in chunks[:-1]]
        messages.append({"text": chunks[-1], "quick_replies": quick_replies})
        return messages

    buttons = actions_to_messenger_buttons(actions)
    # If we had a URL action that didn't fit, append text footer
    if actions:
        urls = [b.url for row in actions.rows for b in row if b.url]
        shown_urls = {b["url"] for b in buttons if b.get("type") == "web_url"}
        for u in urls:
            if u and u not in shown_urls:
                foot = link_footer(u)
                if foot and foot not in text:
                    text = f"{text.rstrip()}\n\n{foot}".strip()
                break
    chunks = _chunk_messenger_text(text)
    if not buttons:
        return [{"text": chunk} for chunk in chunks] or [{"text": "(tomt svar)"}]

    if not chunks:
        return [
            {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "button",
                        "text": "Välj:",
                        "buttons": buttons,
                    },
                }
            }
        ]
    messages = [{"text": chunk} for chunk in chunks[:-1]]
    last_chunk = chunks[-1]
    if len(last_chunk) > 640:
        messages.append({"text": last_chunk})
        button_text = "Åtgärd:"
    else:
        button_text = last_chunk or "Välj:"
    messages.append(
        {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": button_text,
                    "buttons": buttons,
                },
            }
        }
    )
    return messages


def _is_mock_mode() -> bool:
    return os.environ.get("AZOM_USE_MOCK", "").strip().lower() in _TRUE_FLAGS


def _messenger_send_disabled() -> bool:
    return os.environ.get("MESSENGER_SEND_ENABLED", "").strip().lower() in _FALSE_FLAGS


def _host_is_public_ip(host: str) -> bool:
    clean = (host or "").strip().lower().rstrip(".")
    if not clean or clean == "localhost" or clean.endswith((".local", ".internal", ".invalid", ".example")):
        return False
    try:
        ip = ipaddress.ip_address(clean)
        return ip.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(clean, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return False
        ip_str = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _validate_public_https_image_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw or "\\" in raw:
        return None
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https":
        return None
    if parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port not in (None, 443):
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not _host_is_public_ip(host):
        return None
    return raw


class _SafeHttpsImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urllib.parse.urljoin(req.full_url, str(newurl or ""))
        if _validate_public_https_image_url(resolved) is None:
            raise urllib.error.URLError("Unsafe redirect target blocked")
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def download_messenger_image(
    url: str,
    *,
    peer_id: str,
    data_dir: Path | None = None,
    max_bytes: int = _MAX_MESSENGER_IMAGE_BYTES,
) -> Path | None:
    """Safely download an inbound Messenger image under ``messenger_inbox_media/<peer_id>/``."""
    limit = max(1, int(max_bytes))
    safe_url = _validate_public_https_image_url(url)
    if safe_url is None:
        return None

    opener = urllib.request.build_opener(_SafeHttpsImageRedirectHandler())
    req = urllib.request.Request(
        safe_url,
        headers={"User-Agent": "AzomEcomOps/3.0"},
        method="GET",
    )
    chunks: list[bytes] = []
    total = 0
    try:
        with opener.open(req, timeout=15) as resp:
            final_url = str(resp.geturl() or safe_url)
            if _validate_public_https_image_url(final_url) is None:
                return None
            raw_len = resp.headers.get("Content-Length") if resp.headers else None
            if raw_len:
                try:
                    if int(str(raw_len).strip()) > limit:
                        return None
                except ValueError:
                    pass
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    return None
                chunks.append(chunk)
    except Exception as exc:
        log.warning("Messenger inbound image download failed (%s)", type(exc).__name__)
        return None

    data = b"".join(chunks)
    fmt = detect_image_format(data)
    if fmt is None:
        return None
    _mime, ext = fmt

    root_raw = data_dir or os.environ.get("AZOM_DATA_DIR", ".azom-data") or ".azom-data"
    root = Path(root_raw).resolve()
    safe_peer = re.sub(r"[^A-Za-z0-9_-]", "_", str(peer_id or "unknown").strip())[:64] or "unknown"
    target_dir = root / "messenger_inbox_media" / safe_peer
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:32]
    dest = target_dir / f"{digest}{ext}"
    tmp_path = target_dir / f".{digest}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp"
    try:
        tmp_path.write_bytes(data)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, dest)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log.warning("Messenger inbound image save failed (%s)", type(exc).__name__)
        return None
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return dest


def send_sender_action(
    peer_id: str,
    action: str = "typing_on",
    *,
    page_token: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Best-effort Messenger sender_action (e.g. typing_on). Never raises."""
    token = (page_token or os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()
    body = {"recipient": {"id": str(peer_id)}, "sender_action": str(action)}
    if dry_run or not token or _is_mock_mode() or _messenger_send_disabled() or null_send_active():
        return {"ok": False, "dry_run": True, "body": body}
    url = f"{GRAPH_API}/me/messages"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"ok": True}
    except Exception as exc:
        log.debug("Messenger sender_action failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}


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
    if dry_run or not token or _is_mock_mode() or _messenger_send_disabled() or null_send_active():
        return {
            "ok": False,
            "dry_run": True,
            "error": "MESSENGER_PAGE_ACCESS_TOKEN missing or outbound suppressed",
            "body": body,
        }
    url = f"{GRAPH_API}/me/messages"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
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


def send_messenger_image(
    peer_id: str,
    image_path: str | Path,
    *,
    page_token: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload a local PNG image via Messenger Graph Send API multipart form-data."""
    path = Path(image_path)
    if path.is_symlink() or not path.is_file():
        return {"ok": False, "error": "invalid_attachment"}
    try:
        raw_bytes = path.read_bytes()
    except OSError:
        return {"ok": False, "error": "unreadable_attachment"}
    if detect_image_format(raw_bytes) is None:
        return {"ok": False, "error": "invalid_image_format"}

    token = (page_token or os.environ.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()
    message_obj = {"attachment": {"type": "image", "payload": {"is_reusable": False}}}
    if dry_run or not token or _is_mock_mode() or _messenger_send_disabled() or null_send_active():
        return {
            "ok": False,
            "dry_run": True,
            "error": "outbound_image_suppressed",
            "file": path.name,
            "message": message_obj,
        }

    boundary = f"----azom-{uuid.uuid4().hex}"
    fields = {
        "recipient": json.dumps({"id": str(peer_id)}),
        "messaging_type": "RESPONSE",
        "message": json.dumps(message_obj),
    }
    parts: list[bytes] = []
    for key, val in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{val}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="filedata"; filename="{path.name}"\r\n'
        f"Content-Type: image/png\r\n\r\n".encode()
        + raw_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{GRAPH_API}/me/messages",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        log.warning("Messenger image send failed: %s %s", exc.code, err_body)
        return {"ok": False, "error": err_body, "status": exc.code}
    except Exception as exc:
        log.warning("Messenger image send error: %s", type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}


def send_bot_reply(
    peer_id: str,
    reply: BotReply | str,
    *,
    page_token: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Send all message parts and validated workspace image attachments for a BotReply."""
    from ecom_ops.bot.reply import BotReply as BR
    from ecom_ops.bot.reply import as_reply, safe_workspace_attachments

    br = as_reply(reply)
    if dry_run:
        note = (
            "\n\n⚠️ Messenger-svar skickades inte (MESSENGER_PAGE_ACCESS_TOKEN saknas). "
            "Be Oscar konfigurera page token."
        )
        if note.strip() not in (br.text or ""):
            br = BR(
                text=f"{(br.text or '').rstrip()}{note}",
                actions=br.actions,
                reply_markup=br.reply_markup,
                attachments=br.attachments,
            )
    results = []
    for msg in reply_to_messenger_messages(br):
        results.append(
            send_messenger_message(
                peer_id, msg, page_token=page_token, dry_run=dry_run
            )
        )
    if br.attachments:
        for img_path in safe_workspace_attachments(br.attachments)[:_MAX_OUTBOUND_IMAGES]:
            results.append(
                send_messenger_image(
                    peer_id, img_path, page_token=page_token, dry_run=dry_run
                )
            )
    return results


def _handle_accepts_image_paths(handler: Any) -> bool:
    try:
        sig = inspect.signature(handler.handle)
    except (TypeError, ValueError):
        return False
    return "image_paths" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def process_inbound(
    event: InboundEvent,
    handler: Any,
) -> BotReply:
    """Route InboundEvent through BotHandler."""
    if event.postback:
        return handler.handle_callback(event.peer_id, event.postback)
    if event.image_paths and _handle_accepts_image_paths(handler):
        return handler.handle(
            event.peer_id,
            event.text or "",
            image_paths=list(event.image_paths),
        )
    return handler.handle(event.peer_id, event.text or "")


def mid_already_seen(
    data_dir: str | os.PathLike[str], mid: str | None, *, ttl_sec: int = 86400
) -> bool:
    """Return True if ``mid`` was already processed (and record it if new).

    Persists a small JSON map of mid -> unix ts under ``AZOM_DATA_DIR``.
    """
    if not mid:
        return False
    import time

    path = Path(data_dir) / "messenger_mids.json"
    now = time.time()
    seen: dict[str, float] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                seen = {
                    str(k): float(v)
                    for k, v in raw.items()
                    if isinstance(v, (int, float))
                }
        except Exception:
            seen = {}
    # prune
    seen = {k: ts for k, ts in seen.items() if now - ts < ttl_sec}
    if mid in seen:
        try:
            path.write_text(json.dumps(seen), encoding="utf-8")
        except Exception:
            pass
        return True
    seen[mid] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen), encoding="utf-8")
    except Exception:
        pass
    return False
