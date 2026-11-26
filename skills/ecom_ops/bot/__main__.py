"""Run Telegram long-poll bot: python -m ecom_ops.bot"""

from __future__ import annotations

import inspect
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecom_ops.bot.chat_agent import ChatCancelled
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import JOB_LEASE_SECONDS, TelegramJobStore
from ecom_ops.bot.openclaw_commands import TELEGRAM_MENU_COMMANDS
from ecom_ops.bot.progress_events import (
    LiveStatusBoard,
    ProgressEvent,
    format_progress_line,
    mark_event_consumer,
    sanitize_status_fragment,
    utf16_length,
)
from ecom_ops.bot.reply import (
    BotReply,
    as_reply,
    safe_workspace_attachments,
    telegram_markup_to_actions,
)
from ecom_ops.bot.store import TelegramConversationStore
from ecom_ops.bot.telegram_transport import (
    ALLOWED_INBOUND_IMAGE_MIMES,
    MAX_INBOUND_IMAGE_BYTES,
    PerChatRateLimiter,
    TelegramApiError,
    call_telegram_api,
    detect_image_format,
    download_file_bytes,
    get_file_path,
    is_missing_message_error,
    send_photo,
    telegram_api_post,
)

_INBOX_MEDIA_TTL_SECONDS = 24 * 3600
_SAFE_CHAT_DIR_RE = re.compile(r"[^A-Za-z0-9_-]")

_ACTION_MESSAGE = re.compile(
    r"^/cases\s+(?:approve|send|close|regenerate|regen|redraft)\b", re.I
)
_ACTION_CALLBACK = re.compile(
    r"^(?:cases:approve:[0-9a-f]{8}|cases:regen:[0-9a-f]{8}|"
    r"order:set:\d{1,12}:[a-z][a-z-]{0,31}|"
    r"product:desc:(?:0|[1-9]\d{0,11}):[01]|action:yes|escalate:yes|"
    r"wpfile:stage:fe-[a-f0-9]{12}|wpreview:stage:wr-[a-f0-9]{12}|"
    r"wpreview:push_draft:wr-[a-f0-9]{12}|"
    r"proposal:yes:pr-[0-9a-f]{12})$",
    re.I,
)
_PROPOSAL_CALLBACK = re.compile(r"^proposal:yes:(pr-[0-9a-f]{12})$", re.I)

_DEFAULT_RATE_LIMITER = PerChatRateLimiter()
_XAI_WATCH_LOCK = threading.Lock()
_XAI_WATCHING: set[str] = set()


def _maybe_watch_xai_login(
    token: str,
    chat_id: object,
    *,
    stopped: threading.Event | None = None,
) -> None:
    """Notify Telegram once in background when a pending xAI device login finishes."""
    from ecom_ops.bot.telegram_oauth import format_xai_login_reply
    from ecom_ops.model_router.xai_device_login import device_login_status

    if device_login_status().get("status") != "pending":
        return
    key = str(chat_id)
    with _XAI_WATCH_LOCK:
        if key in _XAI_WATCHING:
            return
        _XAI_WATCHING.add(key)

    def _watch() -> None:
        deadline = time.monotonic() + 180.0
        try:
            while time.monotonic() < deadline and (stopped is None or not stopped.is_set()):
                if stopped is not None:
                    stopped.wait(2.0)
                else:
                    time.sleep(2.0)
                snap = device_login_status()
                if snap.get("status") in {"finished", "failed"}:
                    try:
                        _send_reply(token, chat_id, format_xai_login_reply(snap, started=False))
                    except Exception:
                        pass
                    return
        finally:
            with _XAI_WATCH_LOCK:
                _XAI_WATCHING.discard(key)

    threading.Thread(target=_watch, daemon=True).start()


def _handle_ephemeral_oauth_update(
    token: str,
    handler: BotHandler,
    store: TelegramJobStore,
    update: dict,
) -> bool:
    """Process pasted OAuth callback URLs without saving code/state to SQLite."""
    from ecom_ops.bot.telegram_oauth import is_ephemeral_oauth_message

    msg = update.get("message") or {}
    text = msg.get("text")
    chat_id = (msg.get("chat") or {}).get("id")
    update_id = update.get("update_id")
    if not (
        isinstance(text, str)
        and chat_id is not None
        and update_id is not None
        and is_ephemeral_oauth_message(text)
    ):
        return False
    try:
        uid = int(update_id)
    except (TypeError, ValueError):
        return False
    if not store.acknowledge_ephemeral_update(uid):
        return True
    msg_id = _valid_message_id(msg.get("message_id"))
    if msg_id is not None:
        try:
            _api(
                token,
                "deleteMessage",
                _http_timeout=4,
                chat_id=chat_id,
                message_id=msg_id,
            )
        except Exception:
            pass
    try:
        reply = handler.handle_ephemeral_oauth_callback(chat_id, text)
    except Exception:
        reply = BotReply(text="⚠️ ChatGPT OAuth misslyckades.")
    try:
        _send_reply(token, chat_id, reply)
    except Exception as exc:
        print(f"Telegram OAuth reply failed ({type(exc).__name__})", file=sys.stderr)
    return True


def _valid_message_id(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _inbox_media_root(data_dir: Path | None = None) -> Path:
    base = (
        Path(data_dir)
        if data_dir is not None
        else Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))
    )
    return base / "telegram_inbox_media"


def cleanup_old_inbox_media(
    *,
    data_dir: Path | None = None,
    max_age_seconds: float = _INBOX_MEDIA_TTL_SECONDS,
    now: float | None = None,
) -> int:
    """Remove inbound Telegram media files older than ``max_age_seconds``."""
    root = _inbox_media_root(data_dir)
    if not root.is_dir():
        return 0
    ts = time.time() if now is None else float(now)
    cutoff = ts - max(0.0, float(max_age_seconds))
    removed = 0
    for child in list(root.rglob("*")):
        try:
            if child.is_symlink() or not child.is_file():
                continue
            if child.stat().st_mtime < cutoff:
                child.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def extract_inbound_image_candidate(
    message: dict[str, Any],
    *,
    max_bytes: int = MAX_INBOUND_IMAGE_BYTES,
) -> tuple[str | None, str, str | None]:
    """Return ``(file_id, caption, error_reason)`` for a Telegram photo or image document."""
    if not isinstance(message, dict):
        return None, "", None
    caption = str(message.get("caption") or "").strip()[:8000]

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        eligible: list[tuple[int, int, int, str]] = []
        saw_photo = False
        for idx, item in enumerate(photos):
            if not isinstance(item, dict):
                continue
            fid = str(item.get("file_id") or "").strip()
            if not fid:
                continue
            saw_photo = True
            raw_size = item.get("file_size")
            size_val = 0
            if raw_size is not None:
                try:
                    size_val = int(raw_size)
                except (TypeError, ValueError):
                    size_val = 0
                if size_val > max_bytes:
                    continue
            try:
                width = max(0, int(item.get("width") or 0))
            except (TypeError, ValueError):
                width = 0
            try:
                height = max(0, int(item.get("height") or 0))
            except (TypeError, ValueError):
                height = 0
            eligible.append((width * height, size_val, idx, fid))
        if eligible:
            eligible.sort(key=lambda row: (row[0], row[1], row[2]))
            return eligible[-1][3], caption, None
        if saw_photo:
            return None, caption, "too_large"

    doc = message.get("document")
    if isinstance(doc, dict):
        mime = str(doc.get("mime_type") or "").strip().lower()
        if mime in ALLOWED_INBOUND_IMAGE_MIMES:
            fid = str(doc.get("file_id") or "").strip()
            if not fid:
                return None, caption, None
            raw_size = doc.get("file_size")
            if raw_size is not None:
                try:
                    if int(raw_size) > max_bytes:
                        return None, caption, "too_large"
                except (TypeError, ValueError):
                    pass
            return fid, caption, None
    return None, "", None


def save_inbound_telegram_image(
    chat_id: str | int,
    data: bytes,
    *,
    data_dir: Path | None = None,
    max_bytes: int = MAX_INBOUND_IMAGE_BYTES,
) -> Path:
    """Validate and save inbound Telegram image bytes under ``AZOM_DATA_DIR/telegram_inbox_media/<chat_id>/``."""
    if not isinstance(data, (bytes, bytearray)) or len(data) <= 0 or len(data) > max_bytes:
        raise ValueError("Inbound image is empty or exceeds max_bytes limit")
    fmt = detect_image_format(bytes(data))
    if fmt is None:
        raise ValueError("Unsupported image format; only PNG, JPEG, and WEBP are allowed")
    _mime, ext = fmt
    safe_chat = _SAFE_CHAT_DIR_RE.sub("_", str(chat_id).strip()) or "unknown"
    target_dir = _inbox_media_root(data_dir) / safe_chat
    target_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_inbox_media(data_dir=data_dir)
    dest = target_dir / f"{uuid.uuid4().hex}{ext}"
    dest.write_bytes(bytes(data))
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest.resolve()


def _ingest_inbound_media(
    token: str,
    handler: BotHandler,
    store: TelegramJobStore,
    update: dict[str, Any],
) -> tuple[list[str], bool]:
    """Download and validate any inbound photo/image document on ``update``.

    Returns ``(image_paths, handled_early)``. When ``handled_early`` is True, the
    update has already been acknowledged and replied to (e.g. rejected oversize or
    invalid image) and should not be passed to ``store.accept_update``.
    """
    msg = update.get("message")
    if not isinstance(msg, dict):
        return [], False
    file_id, _caption, err_reason = extract_inbound_image_candidate(msg)
    if file_id is None and err_reason is None:
        return [], False

    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id is None:
        return [], False
    update_id = update.get("update_id")
    msg_id = _valid_message_id(msg.get("message_id"))

    def _ack_and_reply(reply: BotReply) -> tuple[list[str], bool]:
        if update_id is not None:
            try:
                if not store.acknowledge_ephemeral_update(int(update_id)):
                    return [], True
            except Exception:
                pass
        try:
            _send_reply(token, chat_id, reply, reply_to_message_id=msg_id)
        except Exception as exc:
            print(f"Telegram media error reply failed ({type(exc).__name__})", file=sys.stderr)
        return [], True

    denied = _authorized_control(handler, str(chat_id))
    if denied is not None:
        return _ack_and_reply(denied)

    if err_reason == "too_large":
        return _ack_and_reply(
            BotReply(
                text="Bilden är för stor (max 5 MB). Skicka en mindre skärmdump i PNG, JPEG eller WEBP."
            )
        )

    assert file_id is not None
    try:
        remote_path = get_file_path(token, file_id)
        raw_bytes = download_file_bytes(token, remote_path, max_bytes=MAX_INBOUND_IMAGE_BYTES)
        saved = save_inbound_telegram_image(chat_id, raw_bytes)
    except Exception as exc:
        print(f"Telegram inbound image rejected ({type(exc).__name__})", file=sys.stderr)
        return _ack_and_reply(
            BotReply(
                text=(
                    "Kunde inte läsa in bilden. Endast PNG, JPEG eller WEBP upp till 5 MB stöds."
                )
            )
        )
    return [str(saved)], False


def _call_supported(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke ``fn`` with only the keyword arguments accepted by its signature."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    supported = {k: v for k, v in kwargs.items() if k in params}
    return fn(*args, **supported)


def _api(token: str, method: str, **params: object) -> dict:
    return telegram_api_post(token, method, **params)


def _register_commands(token: str) -> None:
    try:
        commands = [
            *TELEGRAM_MENU_COMMANDS,
            {"command": "jobs", "description": "Telegram background jobs"},
            {"command": "steer", "description": "Steer a running task"},
            {"command": "task", "description": "Start a parallel task"},
            {"command": "cancel", "description": "Cancel a background job"},
            {"command": "reconcile", "description": "Oscar: reconcile uncertain action"},
        ]
        _api(token, "setMyCommands", _http_timeout=10, commands=commands)
        print(f"Registered {len(commands)} Telegram menu commands")
    except Exception as exc:
        print(f"setMyCommands skipped ({type(exc).__name__})", file=sys.stderr)


def _send_photo(
    token: str,
    chat_id: object,
    photo_path: Path,
    *,
    caption: str = "",
    reply_to_message_id: int | None = None,
    rate_limiter: PerChatRateLimiter | None = None,
) -> dict:
    return send_photo(
        token,
        chat_id,
        photo_path,
        caption=caption,
        reply_to_message_id=reply_to_message_id,
        rate_limiter=rate_limiter if rate_limiter is not None else _DEFAULT_RATE_LIMITER,
    )


def _send_attachments(
    token: str,
    chat_id: object,
    attachments: tuple[str, ...],
    *,
    reply_to_message_id: int | None = None,
    rate_limiter: PerChatRateLimiter | None = None,
) -> None:
    if not attachments:
        return
    try:
        validated = safe_workspace_attachments(attachments)
    except Exception:
        return
    reply_to = _valid_message_id(reply_to_message_id)
    limiter = rate_limiter if rate_limiter is not None else _DEFAULT_RATE_LIMITER
    for index, item in enumerate(validated):
        photo_path = Path(item)
        if photo_path.suffix.lower() != ".png" or not photo_path.is_file():
            continue
        try:
            _call_supported(
                _send_photo,
                token,
                chat_id,
                photo_path,
                caption=f"Sandlåda: {photo_path.name}",
                reply_to_message_id=reply_to if index == 0 else None,
                rate_limiter=limiter,
            )
        except TelegramApiError as exc:
            if exc.status_code == 429 and exc.retry_after:
                limiter.record_retry_after(chat_id, exc.retry_after)
            continue
        except Exception:
            continue


def _send_reply(
    token: str,
    chat_id: object,
    reply: BotReply,
    *,
    reply_to_message_id: int | None = None,
) -> None:
    chunks = _telegram_safe_chunks(reply.text)
    reply_to = _valid_message_id(reply_to_message_id)
    for i, part in enumerate(chunks):
        params: dict[str, object] = {
            "chat_id": chat_id,
            "text": part or "(tomt svar)",
            "_http_timeout": 10,
        }
        if i == 0 and reply_to is not None:
            params["reply_to_message_id"] = reply_to
            params["allow_sending_without_reply"] = True
        # Attach keyboard only on the last chunk
        if reply.reply_markup and i == len(chunks) - 1:
            params["reply_markup"] = reply.reply_markup
        call_telegram_api(_api, token, "sendMessage", **params)
    if reply.attachments:
        _send_attachments(
            token,
            chat_id,
            reply.attachments,
            reply_to_message_id=reply_to,
            rate_limiter=_DEFAULT_RATE_LIMITER,
        )


def _utf16_length(text: str) -> int:
    return utf16_length(text)


def _take_telegram_chunk(text: str, limit: int) -> tuple[str, str]:
    units = 0
    index = 0
    last_newline = -1
    while index < len(text):
        char_units = 2 if ord(text[index]) > 0xFFFF else 1
        if units + char_units > limit:
            break
        units += char_units
        if text[index] == "\n":
            last_newline = index
        index += 1
    if index == len(text):
        return text, ""
    cut = last_newline + 1 if last_newline >= len(text[:index]) // 2 else index
    if cut <= 0:
        cut = max(index, 1)
    return text[:cut], text[cut:]


def _telegram_safe_chunks(text: str, *, limit: int = 3900) -> list[str]:
    rest = text or ""
    if not rest:
        return [""]
    chunks: list[str] = []
    while _utf16_length(rest) > limit:
        head, rest = _take_telegram_chunk(rest, limit)
        chunks.append(head)
    chunks.append(rest)
    return chunks


class _LiveTelegramTurn:
    """Keep a short status draft and a separately streamed answer draft."""

    _STATUS_DELAY = 3.0
    _STATUS_DELAY_LLM = 1.5
    _EDIT_INTERVAL = 0.75
    _CHUNK_LIMIT = 3900
    _STATUS_CLIP = 420
    _PARALLEL_STREAM_SUFFIX = "\n\n⏳ (fortsättning levereras när svaret är klart…)"

    def __init__(
        self,
        token: str,
        chat_id: object,
        *,
        job_store: TelegramJobStore | None = None,
        job_id: str | None = None,
        status_message_id: int | None = None,
        answer_message_ids: list[int] | None = None,
        task_id: str | None = None,
        kind: str = "read_turn",
        title: str | None = None,
        reply_to_message_id: int | None = None,
        rate_limiter: PerChatRateLimiter | None = None,
        heartbeat_interval: float = 8.0,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.job_store = job_store
        self.job_id = job_id
        self.task_id = task_id
        self.kind = kind or "read_turn"
        self.title = title
        self.reply_to_message_id = _valid_message_id(reply_to_message_id)
        self.rate_limiter = (
            rate_limiter if rate_limiter is not None else _DEFAULT_RATE_LIMITER
        )
        self.heartbeat_interval = float(heartbeat_interval)
        self.lock = threading.RLock()
        self.started_at = time.monotonic()
        self.finished = False
        self.on_progress_accepts_event = True
        self.status_delay = (
            self._STATUS_DELAY_LLM
            if self.kind in {"read_turn", "page_review"}
            else self._STATUS_DELAY
        )
        self.board = LiveStatusBoard(
            job_id=job_id,
            task_id=task_id,
            kind=self.kind,
            title=title,
            started_at=self.started_at,
        )
        self.status_lines = ["⏳ Tar hand om din fråga…"]
        self.status_message_id = status_message_id
        self.last_status_edit = 0.0
        self._last_rendered_status = ""
        self._route_switch_reported = False
        self.stream_text = ""
        self.stream_chunks: list[str] = []
        self.stream_current = ""
        self.answer_message_ids = list(answer_message_ids or [])
        self.answer_chunk_texts: list[str] = [""] * len(self.answer_message_ids)
        self.last_answer_edit = 0.0
        self.last_answer_length = 0
        self._flush_timer: threading.Timer | None = None
        self._heartbeat_timer: threading.Timer | None = None
        self.timer = threading.Timer(self.status_delay, self._show_initial_status)
        self.timer.daemon = True
        self.timer.start()

    @staticmethod
    def _message_id(result: dict) -> int | None:
        message = result.get("result") if isinstance(result, dict) else None
        value = message.get("message_id") if isinstance(message, dict) else None
        return value if isinstance(value, int) else None

    def _safe_api(self, method: str, **params: object) -> dict:
        try:
            result = call_telegram_api(
                _api,
                self.token,
                method,
                _http_timeout=4,
                **params,
            )
        except TelegramApiError as exc:
            if exc.status_code == 429 and exc.retry_after:
                self.rate_limiter.record_retry_after(self.chat_id, exc.retry_after)
            if method == "editMessageText" and is_missing_message_error(
                exc.status_code, exc.description
            ):
                return {"ok": False, "missing_message": True}
            return {}
        except Exception:
            return {}

        if isinstance(result, dict) and result.get("ok"):
            return result
        if isinstance(result, dict):
            raw_code = result.get("error_code")
            code = (
                int(raw_code)
                if isinstance(raw_code, int) and not isinstance(raw_code, bool)
                else None
            )
            description = str(result.get("description") or "")
            if code == 429:
                parameters = result.get("parameters")
                retry_after = (
                    parameters.get("retry_after")
                    if isinstance(parameters, dict)
                    else None
                )
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    self.rate_limiter.record_retry_after(self.chat_id, float(retry_after))
            if method == "editMessageText" and is_missing_message_error(
                code if code is not None else 400, description
            ):
                return {"ok": False, "missing_message": True}
        return {}

    def _status_text(self) -> str:
        return self.board.render()

    def _persist_ids(self) -> None:
        if self.job_store and self.job_id:
            try:
                self.job_store.save_message_ids(
                    self.job_id,
                    status_message_id=self.status_message_id,
                    answer_message_ids=self.answer_message_ids,
                )
            except Exception:
                pass

    def _cancel_timers_locked(self) -> None:
        self.timer.cancel()
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self._heartbeat_timer is not None:
            self._heartbeat_timer.cancel()
            self._heartbeat_timer = None

    def _schedule_flush_locked(self, delay: float) -> None:
        if self.finished or self._flush_timer is not None:
            return
        wait = max(float(delay), 0.15)
        timer = threading.Timer(wait, self._run_flush)
        timer.daemon = True
        self._flush_timer = timer
        timer.start()

    def _run_flush(self) -> None:
        with self.lock:
            self._flush_timer = None
            if self.finished:
                return
            self._refresh_status(force=False)

    def _ensure_heartbeat_locked(self) -> None:
        if self.finished or self.heartbeat_interval <= 0:
            return
        if self._heartbeat_timer is not None:
            return
        timer = threading.Timer(self.heartbeat_interval, self._heartbeat_tick)
        timer.daemon = True
        self._heartbeat_timer = timer
        timer.start()

    def _heartbeat_tick(self) -> None:
        with self.lock:
            self._heartbeat_timer = None
            if self.finished:
                return
            if not self.answer_message_ids:
                self._safe_api("sendChatAction", chat_id=self.chat_id, action="typing")
            self._refresh_status()
            if not self.finished:
                self._ensure_heartbeat_locked()

    def _show_initial_status(self) -> None:
        with self.lock:
            if not self.finished:
                self._refresh_status(force=True)
                self._ensure_heartbeat_locked()

    def _send_status_message_locked(self, text: str, now: float) -> None:
        params: dict[str, object] = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if self.reply_to_message_id is not None:
            params["reply_to_message_id"] = self.reply_to_message_id
            params["allow_sending_without_reply"] = True
        result = self._safe_api("sendMessage", **params)
        self.status_message_id = self._message_id(result)
        self.last_status_edit = now
        if self.status_message_id is not None:
            self._last_rendered_status = text
            self.rate_limiter.record_edit(self.chat_id)
            self._persist_ids()
            if not self.finished:
                self._ensure_heartbeat_locked()

    def _refresh_status(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if self.status_message_id is None:
            if not force:
                if now - self.started_at < getattr(self, "status_delay", self._STATUS_DELAY):
                    return
                rate_delay = self.rate_limiter.retry_delay(self.chat_id, force=False)
                if rate_delay > 0:
                    self._schedule_flush_locked(rate_delay)
                    return
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            self._send_status_message_locked(self._status_text(), now)
            return

        if not force:
            edit_wait = (
                max(0.0, self._EDIT_INTERVAL - (now - self.last_status_edit))
                if self.last_status_edit > 0.0
                else 0.0
            )
            rate_wait = self.rate_limiter.retry_delay(self.chat_id, force=False)
            wait = max(edit_wait, rate_wait)
            if wait > 0:
                self._schedule_flush_locked(wait)
                return

        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None

        text = self._status_text()
        if not force and text == self._last_rendered_status:
            return

        result = self._safe_api(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=self.status_message_id,
            text=text,
        )
        if result.get("missing_message"):
            self.status_message_id = None
            self._send_status_message_locked(text, now)
            return
        self.last_status_edit = now
        if result.get("ok"):
            self._last_rendered_status = text
            self.rate_limiter.record_edit(self.chat_id)
        if not self.finished:
            self._ensure_heartbeat_locked()

    @mark_event_consumer
    def on_progress(self, item: ProgressEvent | str) -> None:
        if isinstance(item, ProgressEvent):
            with self.lock:
                if self.finished:
                    return
                line = self.board.record(item)
                if line and (not self.status_lines or self.status_lines[-1] != line):
                    self.status_lines.append(line)
                self._refresh_status()
            return
        value = " ".join(str(item or "").split())[: self._STATUS_CLIP]
        if not value:
            return
        with self.lock:
            if self.finished:
                return
            if not self.status_lines or self.status_lines[-1] != value:
                self.status_lines.append(value)
            self.board.record(value)
            self._refresh_status()

    def _append_stream_delta(self, delta: str) -> None:
        self.stream_text += delta
        self.stream_current += delta
        while _utf16_length(self.stream_current) > self._CHUNK_LIMIT:
            head, self.stream_current = _take_telegram_chunk(
                self.stream_current, self._CHUNK_LIMIT
            )
            self.stream_chunks.append(head)

    def _visible_stream_chunks(self) -> list[str]:
        chunks = list(self.stream_chunks)
        if self.stream_current:
            chunks.append(self.stream_current)
        return chunks

    def _has_parallel_siblings(self) -> bool:
        active_jobs = getattr(self.job_store, "active_jobs", None)
        if not callable(active_jobs):
            return False
        try:
            return len(active_jobs(self.chat_id)) > 1
        except Exception:
            return False

    def _send_answer_chunk_locked(
        self, index: int, chunk: str, *, markup: dict | None = None
    ) -> int | None:
        params: dict[str, object] = {
            "chat_id": self.chat_id,
            "text": chunk or "(tomt svar)",
        }
        if index == 0 and self.reply_to_message_id is not None:
            params["reply_to_message_id"] = self.reply_to_message_id
            params["allow_sending_without_reply"] = True
        if markup:
            params["reply_markup"] = markup
        result = self._safe_api("sendMessage", **params)
        return self._message_id(result)

    def _sync_answer_chunks(
        self, chunks: list[str], *, reply_markup: dict | None = None
    ) -> bool:
        chunks = chunks or ["(tomt svar)"]
        synced = True
        for index, chunk in enumerate(chunks):
            markup = reply_markup if index == len(chunks) - 1 else None
            if index < len(self.answer_message_ids):
                if self.answer_chunk_texts[index] == chunk and not markup:
                    continue
                params: dict[str, object] = {
                    "chat_id": self.chat_id,
                    "message_id": self.answer_message_ids[index],
                    "text": chunk or "(tomt svar)",
                }
                if markup:
                    params["reply_markup"] = markup
                result = self._safe_api("editMessageText", **params)
                if result.get("ok"):
                    self.answer_chunk_texts[index] = chunk
                    continue
                if result.get("missing_message"):
                    replacement_id = self._send_answer_chunk_locked(
                        index, chunk, markup=markup
                    )
                    if replacement_id is not None:
                        self.answer_message_ids[index] = replacement_id
                        self.answer_chunk_texts[index] = chunk
                        self._persist_ids()
                    else:
                        synced = False
                    continue
                synced = False
                continue
            message_id = self._send_answer_chunk_locked(index, chunk, markup=markup)
            if message_id is not None:
                self.answer_message_ids.append(message_id)
                self.answer_chunk_texts.append(chunk)
                self._persist_ids()
            else:
                synced = False
                break
        for message_id in self.answer_message_ids[len(chunks) :]:
            self._safe_api(
                "deleteMessage", chat_id=self.chat_id, message_id=message_id
            )
        self.answer_message_ids = self.answer_message_ids[: len(chunks)]
        self.answer_chunk_texts = self.answer_chunk_texts[: len(chunks)]
        self._persist_ids()
        return synced

    def _clear_stream_locked(self) -> None:
        for message_id in self.answer_message_ids:
            self._safe_api(
                "deleteMessage", chat_id=self.chat_id, message_id=message_id
            )
        self.answer_message_ids.clear()
        self.answer_chunk_texts.clear()
        self._persist_ids()
        self.stream_text = ""
        self.stream_chunks.clear()
        self.stream_current = ""
        self.last_answer_length = 0

    def clear_answer_drafts(self) -> None:
        with self.lock:
            for message_id in self.answer_message_ids:
                self._safe_api(
                    "deleteMessage", chat_id=self.chat_id, message_id=message_id
                )
            self.answer_message_ids.clear()
            self.answer_chunk_texts.clear()
            self._persist_ids()

    def on_text_delta(self, delta: str) -> None:
        if not delta:
            return
        with self.lock:
            if self.finished:
                return
            self._append_stream_delta(delta)
            stripped = self.stream_text.strip()
            if not stripped:
                return
            now = time.monotonic()
            # Wait briefly for a readable first snippet (>=20 chars or newline) before initial send.
            if not self.answer_message_ids and len(stripped) < 20 and "\n" not in self.stream_text:
                return
            if self.answer_message_ids and self.rate_limiter.blocked_until(self.chat_id) > now:
                return
            if (
                self.answer_message_ids
                and now - self.last_answer_edit < self._EDIT_INTERVAL
                and len(self.stream_text) - self.last_answer_length < 80
            ):
                return
            chunks = self._visible_stream_chunks()
            if len(chunks) > 1 and self._has_parallel_siblings():
                suffix = self._PARALLEL_STREAM_SUFFIX
                max_head = max(1, self._CHUNK_LIMIT - _utf16_length(suffix))
                head, _ = _take_telegram_chunk(chunks[0], max_head)
                chunks = [head.rstrip() + suffix]
            self._sync_answer_chunks(chunks)
            self.last_answer_edit = now
            self.last_answer_length = len(self.stream_text)

    def on_stream_clear(self) -> None:
        with self.lock:
            if self.finished:
                return
            self._clear_stream_locked()

    def on_phase_change(self, info: dict[str, Any]) -> None:
        if not isinstance(info, dict):
            return
        with self.lock:
            if self.finished:
                return
            line = self.board.set_model_phase(info)
            phase = str(info.get("phase") or "").strip().lower()
            if phase == "route_switch":
                self._route_switch_reported = True
                if line and (not self.status_lines or self.status_lines[-1] != line):
                    self.status_lines.append(line)
            elif phase == "model_fallback" and line:
                if not self.status_lines or self.status_lines[-1] != line:
                    self.status_lines.append(line)
            self._refresh_status()

    def on_text_reset(self) -> None:
        with self.lock:
            if self.finished:
                return
            self._clear_stream_locked()
            if not self._route_switch_reported:
                msg = "↻ Byter modellrutt och börjar om…"
                self.status_lines.append(msg)
                self.board.record(msg)
            self._route_switch_reported = False
            self._refresh_status()

    def finish(
        self,
        reply: BotReply,
        *,
        ok: bool = True,
        cancelled: bool = False,
        error_class: str | None = None,
    ) -> bool:
        with self.lock:
            if self.finished:
                return False
            self.finished = True
            self._cancel_timers_locked()
            self.board.mark_finished(
                ok=ok, cancelled=cancelled, error_class=error_class
            )
            if cancelled:
                self.status_lines.append("🛑 Arbetet avbröts")
            elif not ok:
                self.status_lines.append("⚠️ Arbetet avbröts av ett tekniskt fel")
            else:
                self.status_lines.append("✓ Svar klart")
            if self.status_message_id is not None:
                self._refresh_status(force=True)
            canonical_chunks = _telegram_safe_chunks(
                reply.text, limit=self._CHUNK_LIMIT
            )
            synced = self._sync_answer_chunks(
                canonical_chunks, reply_markup=reply.reply_markup
            )
            if synced and reply.attachments:
                _send_attachments(
                    self.token,
                    self.chat_id,
                    reply.attachments,
                    reply_to_message_id=self.reply_to_message_id,
                    rate_limiter=self.rate_limiter,
                )
            return synced

    def fail(self, *, error_class: str | None = None) -> None:
        with self.lock:
            if self.finished:
                return
            self.finished = True
            self._cancel_timers_locked()
            self.board.mark_finished(ok=False, error_class=error_class)
            self.status_lines.append("⚠️ Arbetet avbröts av ett tekniskt fel")
            if self.status_message_id is not None:
                self._refresh_status(force=True)


_ACTION_LOCKS: dict[str, threading.Lock] = {}
_ACTION_LOCKS_GUARD = threading.Lock()


def _action_lock_key(job: dict, handler: BotHandler) -> str:
    payload = job.get("payload") or {}
    data = str(payload.get("data") or "")
    if data.startswith(("wpfile:stage:", "wpreview:stage:")):
        return "wp-file-edits"
    if data == "action:yes":
        state = handler.store.get(job.get("chat_id")) or {}
        pending = (state.get("slots") or {}).get("pending") or {}
        if pending.get("kind") in {"page_update", "post_update", "site_update", "plugin_status"}:
            site = ((state.get("session") or {}).get("wp_ops_context") or {}).get("site_id")
            if site:
                return f"wp-site:{site}"
    return "approved-actions"


def _action_lock(key: str) -> threading.Lock:
    with _ACTION_LOCKS_GUARD:
        return _ACTION_LOCKS.setdefault(key, threading.Lock())


def _invoke_resource_lock_fn(fn: Any, lock_key: str, job_id: str) -> Any:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(lock_key, job_id)
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2:
        return fn(lock_key, job_id)
    if "job_id" in sig.parameters:
        return fn(lock_key, job_id=job_id)
    if "owner" in sig.parameters:
        return fn(lock_key, owner=job_id)
    return fn(lock_key)


def _result_for_reply(reply: BotReply) -> dict:
    return {
        "text": reply.text,
        "reply_markup": reply.reply_markup,
        "needs_typing": bool(reply.needs_typing),
        "attachments": list(reply.attachments),
    }


def _reply_from_result(value: dict | None) -> BotReply:
    result = value if isinstance(value, dict) else {}
    markup = result.get("reply_markup")
    markup = markup if isinstance(markup, dict) else None
    return BotReply(
        text=str(result.get("text") or "Arbetet avslutades utan ett svar."),
        reply_markup=markup,
        actions=telegram_markup_to_actions(markup),
        needs_typing=bool(result.get("needs_typing")),
        attachments=tuple(
            str(x) for x in (result.get("attachments") or []) if isinstance(x, str)
        ),
    )


def _deliver_job_result(token: str, store: TelegramJobStore, job: dict) -> bool:
    reply = _reply_from_result(job.get("result"))
    payload = job.get("payload") or {}
    job_image_paths = [
        str(p).strip()
        for p in (payload.get("image_paths") or [])
        if isinstance(p, str) and str(p).strip()
    ]
    title = sanitize_status_fragment(
        payload.get("text")
        or payload.get("data")
        or ("Skärmdump" if job_image_paths else ""),
        max_len=72,
    )
    live = _LiveTelegramTurn(
        token,
        job["chat_id"],
        job_store=store,
        job_id=job["job_id"],
        status_message_id=job.get("status_message_id"),
        answer_message_ids=job.get("answer_message_ids") or [],
        task_id=job.get("task_id"),
        kind=str(job.get("kind") or "read_turn"),
        title=title,
        reply_to_message_id=payload.get("source_message_id"),
        rate_limiter=_DEFAULT_RATE_LIMITER,
    )
    try:
        for ev in store.events(job["job_id"], limit=10):
            summary = str(ev.get("summary") or "").strip()
            if summary:
                live.board.record(summary)
    except Exception:
        pass
    job_state = str(job.get("state") or "completed")
    synced = live.finish(
        reply,
        ok=(job_state == "completed"),
        cancelled=(job_state == "cancelled"),
        error_class=job.get("failure_class"),
    )
    if synced:
        store.mark_delivered(job["job_id"])
    else:
        record_attempt = getattr(store, "record_delivery_attempt", None)
        if callable(record_attempt):
            try:
                _call_supported(record_attempt, job["job_id"])
            except Exception:
                pass
    return synced


def _preflight_action_job(
    handler: BotHandler,
    job: dict,
    store: TelegramJobStore | None = None,
) -> str | None:
    """Fail closed on malformed, stale, or unauthorized queued actions."""
    payload = job.get("payload")
    if not isinstance(payload, dict):
        return "Ogiltig åtgärdskö. Ingen ändring utfördes."
    chat_id = str(job.get("chat_id") or "")
    if not chat_id or str(payload.get("chat_id") or "") != chat_id:
        return "Åtgärdens chatt stämmer inte. Ingen ändring utfördes."

    payload_type = payload.get("type")
    requires_snapshot = False
    proposal_id: str | None = None
    if payload_type == "callback":
        data = str(payload.get("data") or "").strip()
        if not _ACTION_CALLBACK.fullmatch(data):
            return "Åtgärdsknappen kunde inte valideras. Ingen ändring utfördes."
        prop_match = _PROPOSAL_CALLBACK.fullmatch(data)
        if prop_match:
            proposal_id = prop_match.group(1).lower()
        else:
            requires_snapshot = data in {"action:yes", "escalate:yes"} or data.startswith(
                ("wpfile:stage:", "wpreview:stage:", "wpreview:push_draft:")
            )
    elif payload_type == "message":
        if not _ACTION_MESSAGE.match(str(payload.get("text") or "").strip()):
            return "Åtgärdskommandot kunde inte valideras. Ingen ändring utfördes."
    else:
        return "Okänd åtgärdstyp. Ingen ändring utfördes."

    try:
        if not handler._peer_allowed(chat_id):
            return "Chatten saknar behörighet. Ingen ändring utfördes."
        handler._resolve_actor(chat_id)
    except Exception:
        return "Actor-behörigheten kunde inte verifieras. Ingen ändring utfördes."

    if proposal_id is not None:
        get_proposal = getattr(store, "get_proposal", None) if store is not None else None
        if callable(get_proposal):
            try:
                proposal = _call_supported(get_proposal, proposal_id, chat_id=chat_id)
            except Exception:
                proposal = None
            if not isinstance(proposal, dict):
                return "Förslaget hittades inte eller är inaktuellt. Ingen ändring utfördes."
            prop_chat = proposal.get("chat_id")
            if prop_chat is not None and str(prop_chat) != chat_id:
                return "Förslaget tillhör en annan chatt. Ingen ändring utfördes."
            if str(proposal.get("state") or "") != "open":
                return "Förslaget är inte längre öppet. Ingen ändring utfördes."
        return None

    snapshot_required = payload.get("approval_snapshot_required") is True
    if requires_snapshot and not snapshot_required:
        return "Bekräftelsen saknar aktuell kontroll. Granska och bekräfta igen."
    if snapshot_required:
        expected = str(payload.get("approval_snapshot") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            return "Bekräftelsen saknar aktuell kontroll. Granska och bekräfta igen."
        if expected != TelegramJobStore.approval_snapshot(handler.store.get(chat_id)):
            return "Bekräftelsen är inaktuell. Granska åtgärden och bekräfta igen."
    return None


def _process_job(
    token: str,
    handler: BotHandler,
    store: TelegramJobStore,
    job: dict,
    *,
    stopped: threading.Event | None = None,
) -> None:
    job_id = str(job["job_id"])
    chat_id = job["chat_id"]
    payload = job.get("payload") or {}
    job_image_paths = [
        str(p).strip()
        for p in (payload.get("image_paths") or [])
        if isinstance(p, str) and str(p).strip()
    ]
    title = sanitize_status_fragment(
        payload.get("text")
        or payload.get("data")
        or ("Skärmdump" if job_image_paths else ""),
        max_len=72,
    )
    live = _LiveTelegramTurn(
        token,
        chat_id,
        job_store=store,
        job_id=job_id,
        status_message_id=job.get("status_message_id"),
        task_id=job.get("task_id"),
        kind=str(job.get("kind") or "read_turn"),
        title=title,
        reply_to_message_id=payload.get("source_message_id"),
        rate_limiter=_DEFAULT_RATE_LIMITER,
    )
    # A read job recovered after a crash is safe to replay. Remove its partial
    # answer draft and retain the status message so the user sees one resumed job.
    if job.get("answer_message_ids"):
        live.answer_message_ids = list(job["answer_message_ids"])
        live.answer_chunk_texts = [""] * len(live.answer_message_ids)
        live.clear_answer_drafts()

    @mark_event_consumer
    def progress(item: ProgressEvent | str) -> None:
        if isinstance(item, ProgressEvent):
            safe_text = format_progress_line(item)[: _LiveTelegramTurn._STATUS_CLIP]
        else:
            safe_text = " ".join(str(item or "").split())[: _LiveTelegramTurn._STATUS_CLIP]
        if not safe_text:
            return
        try:
            store.add_event(job_id, "progress", safe_text)
        except Exception:
            pass
        live.on_progress(item)

    def checkpoint() -> list[str]:
        steers, cancelled = store.consume_steers(job_id)
        if cancelled:
            raise ChatCancelled()
        return steers

    finish_ok = True
    finish_cancelled = False
    finish_error_class: str | None = None
    try:
        try:
            _api(token, "sendChatAction", _http_timeout=4, chat_id=chat_id, action="typing")
        except Exception:
            pass
        progress("Arbetet körs i bakgrunden.")
        action_started = False
        if job.get("kind") == "action":
            # Respect cancellation while the action is queued or waiting for
            # its serialization lock; after this checkpoint it is too late.
            checkpoint()
            lock_key = _action_lock_key(job, handler)
            acquire_lock = getattr(store, "acquire_resource_lock", None)
            release_lock = getattr(store, "release_resource_lock", None)
            with _action_lock(lock_key):
                checkpoint()
                db_lock_acquired = False
                if callable(acquire_lock):
                    deadline = time.monotonic() + 30.0
                    while not db_lock_acquired:
                        checkpoint()
                        db_lock_acquired = bool(
                            _invoke_resource_lock_fn(acquire_lock, lock_key, job_id)
                        )
                        if db_lock_acquired or time.monotonic() >= deadline:
                            break
                        time.sleep(0.1)
                try:
                    if callable(acquire_lock) and not db_lock_acquired:
                        reply = BotReply(
                            text=(
                                "En annan godkänd åtgärd på samma resurs pågår redan. "
                                "Försök igen om en stund."
                            )
                        )
                    else:
                        progress("Kontrollerar åtgärd, behörighet och aktuell bekräftelse.")
                        preflight_failure = _preflight_action_job(
                            handler, job, store=store
                        )
                        if preflight_failure:
                            reply = BotReply(text=preflight_failure)
                        else:
                            if not store.mark_side_effect_started(job_id):
                                raise ChatCancelled()
                            action_started = True
                            progress("Godkänd åtgärd körs.")
                            if payload.get("type") == "callback":
                                reply = as_reply(
                                    _call_supported(
                                        handler.handle_callback,
                                        chat_id,
                                        str(payload.get("data") or ""),
                                        on_progress=progress,
                                        task_id=job.get("task_id"),
                                    )
                                )
                            else:
                                reply = as_reply(
                                    _call_supported(
                                        handler.handle,
                                        chat_id,
                                        str(payload.get("text") or ""),
                                        on_progress=progress,
                                        on_stream_clear=live.on_stream_clear,
                                        on_phase_change=live.on_phase_change,
                                        task_id=job.get("task_id"),
                                        image_paths=job_image_paths or None,
                                    )
                                )
                finally:
                    if db_lock_acquired and callable(release_lock):
                        try:
                            _invoke_resource_lock_fn(release_lock, lock_key, job_id)
                        except Exception:
                            pass
        elif job.get("kind") == "page_review":
            from ecom_ops.bot.page_review_job import run_page_review_job

            reply = as_reply(
                run_page_review_job(
                    handler,
                    job,
                    progress=progress,
                    on_checkpoint=checkpoint,
                )
            )
        elif payload.get("type") == "callback":
            reply = as_reply(
                _call_supported(
                    handler.handle_callback,
                    chat_id,
                    str(payload.get("data") or ""),
                    on_progress=progress,
                    on_text_delta=live.on_text_delta,
                    on_text_reset=live.on_text_reset,
                    on_stream_clear=live.on_stream_clear,
                    on_phase_change=live.on_phase_change,
                    on_checkpoint=checkpoint,
                    task_id=job.get("task_id"),
                )
            )
        else:
            reply = as_reply(
                _call_supported(
                    handler.handle,
                    chat_id,
                    str(payload.get("text") or ""),
                    on_progress=progress,
                    on_text_delta=live.on_text_delta,
                    on_text_reset=live.on_text_reset,
                    on_stream_clear=live.on_stream_clear,
                    on_phase_change=live.on_phase_change,
                    on_checkpoint=checkpoint,
                    task_id=job.get("task_id"),
                    image_paths=job_image_paths or None,
                )
            )
        cancelled = store.cancel_requested(job_id)
        if cancelled:
            reply = BotReply(text="Arbetet avbröts vid en säker kontrollpunkt.")
            state = "cancelled"
            finish_ok = False
            finish_cancelled = True
        elif job.get("kind") == "action" and reply.text.strip().lower().startswith("misslyckades"):
            state = "needs_reconciliation"
            finish_ok = False
        else:
            state = "completed"
            finish_ok = True
        store.complete_job(job_id, _result_for_reply(reply), state=state)
    except ChatCancelled:
        reply = BotReply(text="Arbetet avbröts vid en säker kontrollpunkt.")
        store.complete_job(job_id, _result_for_reply(reply), state="cancelled")
        finish_ok = False
        finish_cancelled = True
    except Exception as exc:
        # Keep provider response bodies, arguments, and credentials out of logs.
        print(f"Telegram job {job_id[:8]} failed ({type(exc).__name__})", file=sys.stderr)
        uncertain = job.get("kind") == "action" and locals().get("action_started", False)
        reply = BotReply(
            text=(
                "Ett godkänt jobb stoppades efter att åtgärden kan ha startat. "
                "Utfallet behöver kontrolleras manuellt; systemet kör inte om den automatiskt."
                if uncertain
                else "Ett tekniskt fel stoppade jobbet. Försök igen eller kontakta Oscar."
            )
        )
        finish_ok = False
        finish_cancelled = False
        finish_error_class = type(exc).__name__[:80]
        store.complete_job(
            job_id,
            _result_for_reply(reply),
            state="needs_reconciliation" if uncertain else "failed",
            failure_class=finish_error_class,
        )
    try:
        synced = live.finish(
            reply,
            ok=finish_ok,
            cancelled=finish_cancelled,
            error_class=finish_error_class,
        )
        if synced:
            store.mark_delivered(job_id)
        else:
            record_attempt = getattr(store, "record_delivery_attempt", None)
            if callable(record_attempt):
                try:
                    _call_supported(record_attempt, job_id)
                except Exception:
                    pass
    except Exception:
        # Result and Telegram message ids remain in SQLite for outbox retry.
        print(f"Telegram job {job_id[:8]} delivery deferred", file=sys.stderr)

    trigger_txt = str(payload.get("data") or payload.get("text") or "").lower()
    if "oauth" in trigger_txt or "xai" in trigger_txt or "x.ai" in trigger_txt or "grok" in trigger_txt:
        try:
            _maybe_watch_xai_login(token, chat_id, stopped=stopped)
        except Exception:
            pass


def _worker(token: str, handler: BotHandler, store: TelegramJobStore, stopped: threading.Event) -> None:
    while not stopped.is_set():
        try:
            job = store.claim_next(lease_seconds=JOB_LEASE_SECONDS)
            if job is None:
                stopped.wait(0.25)
                continue
            _process_job(token, handler, store, job, stopped=stopped)
        except Exception as exc:
            print(f"Telegram worker loop error ({type(exc).__name__})", file=sys.stderr)
            _record_poller_error("telegram_worker", exc, label="worker_loop")
            stopped.wait(1)


def _authorized_control(handler: BotHandler, chat_id: str) -> BotReply | None:
    if not handler._peer_allowed(chat_id):
        return handler._deny_allowlist()
    try:
        handler._resolve_actor(chat_id)
    except Exception:
        return handler._deny_actor()
    return None


def _active_jobs_for_chat(store: TelegramJobStore, chat_id: str) -> list[dict[str, Any]]:
    active_jobs_fn = getattr(store, "active_jobs", None)
    if callable(active_jobs_fn):
        return list(active_jobs_fn(chat_id) or [])
    active = store.active_job(chat_id)
    return [active] if active else []


def _control_reply(
    token: str, handler: BotHandler, store: TelegramJobStore, control: dict
) -> None:
    chat_id = str(control.get("chat_id") or "")
    update_id = int(control.get("update_id") or 0)
    action = str(control.get("action") or "").lower()
    args = str(control.get("args") or "").strip()
    denied = _authorized_control(handler, chat_id)
    if denied:
        reply = denied
    elif action == "status":
        active_list = _active_jobs_for_chat(store, chat_id)
        jobs = store.conversation_jobs(chat_id)
        queued_count = sum(1 for item in jobs if item.get("state") == "queued")
        if active_list:
            lines = [f"Pågående jobb ({len(active_list)}):"]
            for item in active_list:
                jid = str(item.get("job_id") or "")
                events = store.events(jid, limit=3)
                phase = events[-1]["summary"] if events else "Arbetet pågår"
                lines.append(
                    f"• #{jid[:8]} ({item.get('kind', 'read_turn')}): {phase}"
                )
            if queued_count:
                lines.append(f"Köade jobb: {queued_count}")
            reply = BotReply(text="\n".join(lines))
        elif jobs:
            blocked = next((item for item in jobs if item["state"] == "needs_reconciliation"), None)
            if blocked:
                reply = BotReply(
                    text=(
                        f"Jobb {blocked['job_id'][:8]} behöver stämmas av efter omstart. "
                        "Systemet kör inte om den godkända åtgärden automatiskt."
                    )
                )
            else:
                reply = BotReply(text=f"{len(jobs)} jobb väntar i kön. Skriv /jobs för detaljer.")
        else:
            reply = as_reply(handler.handle(chat_id, "/status"))
    elif action == "jobs":
        jobs = store.conversation_jobs(chat_id)
        if not jobs:
            reply = BotReply(text="Inga aktiva eller väntande jobb i den här chatten.")
        else:
            lines = ["Jobb i den här chatten:"]
            for item in jobs:
                lines.append(f"{item['job_id'][:8]} · {item['kind']} · {item['state']}")
            reply = BotReply(text="\n".join(lines))
    elif action in {"stop", "cancel"}:
        result = store.request_cancel(chat_id, args or None)
        messages = {
            "requested": "Avbrytning begärd. Jobbet stannar vid nästa säkra kontrollpunkt.",
            "too_late": "Åtgärden har redan startat och kan inte avbrytas säkert. Jag rapporterar utfallet.",
            "cancelled_queued": "Jobbet avbröts innan det startade.",
            "not_running": "Det jobbet körs inte längre.",
            "not_found": "Inget körande eller väntande jobb hittades.",
            "ambiguous": "Flera jobb matchar id:t. Ange ett längre jobb-id.",
        }
        if result == "ambiguous_running":
            active_list = _active_jobs_for_chat(store, chat_id)
            lines = ["Flera jobb kör samtidigt. Ange vilket du vill avbryta:"]
            for item in active_list:
                jid = str(item.get("job_id") or "")
                lines.append(f"• /cancel {jid[:8]} ({item.get('kind', 'read_turn')})")
            reply = BotReply(text="\n".join(lines))
        elif result == "not_found" and not args:
            reply = as_reply(handler.handle(chat_id, "/stop"))
        else:
            reply = BotReply(text=messages.get(result, "Avbrytning kunde inte registreras."))
    elif action == "reconcile":
        from ecom_ops.rbac import Permission, resolve_actor

        try:
            actor_name = handler._resolve_actor(chat_id)
            actor = resolve_actor(actor_name)
        except Exception:
            actor = None
            actor_name = ""
        if not actor or not actor.has(Permission.ADMIN):
            reply = BotReply(text="Endast Oscar kan stämma av ett osäkert åtgärdsjobb.")
        else:
            parts = args.split()
            if len(parts) != 2 or parts[1].lower() not in {"done", "not_applied"}:
                reply = BotReply(
                    text="Använd /reconcile <job-id> done|not_applied efter att du kontrollerat utfallet."
                )
            else:
                result = store.resolve_reconciliation(
                    chat_id,
                    parts[0],
                    outcome=parts[1].lower(),
                    actor=actor_name,
                )
                replies = {
                    "resolved": "Jobbet är avstämt. Ingen åtgärd kördes igen.",
                    "not_found": "Hittade inget avstämningsjobb med det id:t i den här chatten.",
                    "invalid": "Ogiltigt jobb-id eller utfall.",
                }
                reply = BotReply(text=replies[result])
    else:
        reply = BotReply(text="Okänt kontrollkommando.")
    try:
        _send_reply(token, chat_id, reply)
        store.mark_control_handled(update_id)
    except Exception as exc:
        print(f"Telegram control delivery deferred ({type(exc).__name__})", file=sys.stderr)


def _service_pending_controls(token: str, handler: BotHandler, store: TelegramJobStore) -> None:
    for control in store.pending_controls():
        _control_reply(token, handler, store, control)


def _delivery_worker(token: str, store: TelegramJobStore, stopped: threading.Event) -> None:
    while not stopped.is_set():
        for item in store.pending_reconciliation():
            try:
                _send_reply(
                    token,
                    item["chat_id"],
                    BotReply(
                        text=(
                            f"Godkänt jobb {item['job_id'][:8]} kan ha hunnit ändra ett externt system "
                            "före omstart. Det körs inte om automatiskt; kontrollera utfallet manuellt."
                        )
                    ),
                )
                store.mark_reconciliation_notified(item["job_id"])
            except Exception:
                pass
        for job in store.undelivered_results(limit=5):
            try:
                _deliver_job_result(token, store, job)
            except Exception:
                pass
        stopped.wait(2)


_LEASE_HEARTBEAT_INTERVAL_S = 25.0
_OPS_GC_INTERVAL_S = 1800.0


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_poll_error(label: str, exc: BaseException) -> str:
    """Format a timestamped, token-safe error line (never includes tokens, URLs, or raw bodies)."""
    details = [type(exc).__name__]
    if isinstance(exc, TelegramApiError):
        details.append(f"status={exc.status_code}")
        if exc.method:
            details.append(f"method={exc.method}")
        if exc.retry_after is not None:
            details.append(f"retry_after={exc.retry_after:g}")
    return f"{_utc_now_iso()} {label} ({' '.join(details)})"


def _record_poller_error(source: str, exc: BaseException, *, label: str = "") -> None:
    """Record a sanitized poller/worker error in ``runtime_errors.jsonl`` (never raises)."""
    try:
        from ecom_ops.runtime_health import record_runtime_error

        parts: list[str] = []
        if label:
            parts.append(label)
        status_code: int | None = None
        if isinstance(exc, TelegramApiError):
            status_code = exc.status_code
            if exc.method:
                parts.append(f"method={exc.method}")
            if exc.retry_after is not None:
                parts.append(f"retry_after={exc.retry_after:g}")
        record_runtime_error(
            source,
            type(exc).__name__,
            detail=" ".join(parts),
            status_code=status_code,
        )
    except Exception:
        pass


def _maybe_run_periodic_gc(
    last_gc_at: float,
    *,
    now: float | None = None,
    interval_seconds: float | None = None,
) -> float:
    """Run ``run_ops_gc(dry_run=False)`` if ``interval_seconds`` has elapsed since ``last_gc_at``."""
    now_val = time.monotonic() if now is None else float(now)
    interval = _OPS_GC_INTERVAL_S if interval_seconds is None else max(1.0, float(interval_seconds))
    if (now_val - float(last_gc_at)) < interval:
        return last_gc_at
    try:
        from ecom_ops.ops_gc import run_ops_gc

        res = run_ops_gc(dry_run=False)
        errs = res.get("errors") or []
        if errs:
            try:
                from ecom_ops.runtime_health import record_runtime_error

                record_runtime_error(
                    "ops_gc",
                    "PartialGcError",
                    detail=f"errors={len(errs)}",
                )
            except Exception:
                pass
    except Exception as exc:
        print(_format_poll_error("Periodic ops GC error", exc), file=sys.stderr)
        _record_poller_error("ops_gc", exc, label="periodic_gc")
    return now_val


def _run_poller_maintenance(
    store: TelegramJobStore,
    lease_lost: threading.Event,
    *,
    lease_seconds: int = JOB_LEASE_SECONDS,
    run_cleanup: bool = True,
) -> bool:
    """Renew poller + active worker leases; optionally recover expired jobs and purge."""
    if not store.renew_poller_lease():
        lease_lost.set()
        print(
            f"{_utc_now_iso()} Telegram poller lease lost; stopping intake.",
            file=sys.stderr,
        )
        try:
            from ecom_ops.runtime_health import record_runtime_error

            record_runtime_error("telegram_poller", "LeaseLost", detail="poller_lease_lost")
        except Exception:
            pass
        return False
    store.renew_leases(lease_seconds=lease_seconds)
    if run_cleanup:
        store.recover_expired_jobs()
        store.purge()
        try:
            cleanup_old_inbox_media()
        except Exception:
            pass
    return True


def _lease_heartbeat_loop(
    store: TelegramJobStore,
    stopped: threading.Event,
    lease_lost: threading.Event,
    *,
    interval_seconds: float | None = None,
    lease_seconds: int = JOB_LEASE_SECONDS,
) -> None:
    """Keep poller and active worker leases fresh even while getUpdates blocks or fails."""
    wait_s = max(
        0.05,
        float(
            interval_seconds
            if interval_seconds is not None
            else _LEASE_HEARTBEAT_INTERVAL_S
        ),
    )
    while not stopped.wait(wait_s):
        try:
            ok = _run_poller_maintenance(
                store,
                lease_lost,
                lease_seconds=lease_seconds,
                run_cleanup=False,
            )
            if not ok:
                stopped.set()
                return
        except Exception as exc:
            print(_format_poll_error("Telegram lease heartbeat error", exc), file=sys.stderr)
            _record_poller_error("telegram_poller", exc, label="lease_heartbeat")


def main() -> int:
    from ecom_ops.runtime_env import bootstrap_runtime

    bootstrap_runtime()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN missing; dry-run exit.")
        return 0

    store = TelegramJobStore()
    if not store.acquire_poller_lease():
        print(f"{_utc_now_iso()} Another Telegram poller owns the database lease.", file=sys.stderr)
        return 2
    try:
        recovered = store.recover_after_restart()
        print(
            "Telegram jobs recovered: "
            f"queued={recovered['queued']} reconciliation={recovered['reconciliation']}"
        )
        legacy_path = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data")) / "telegram_state.json"
        handler = _call_supported(
            BotHandler,
            store=TelegramConversationStore(store.path, legacy_path=legacy_path),
            proposal_store=store,
        )
        _register_commands(token)
        try:
            worker_count = int(os.environ.get("AZOM_TELEGRAM_WORKERS", "2") or 2)
        except ValueError:
            print("Invalid AZOM_TELEGRAM_WORKERS; using 2", file=sys.stderr)
            worker_count = 2
    except Exception:
        store.release_poller_lease()
        raise
    worker_count = max(1, min(worker_count, 4))
    stopped = threading.Event()
    shutdown_requested = threading.Event()
    lease_lost = threading.Event()
    workers = [
        threading.Thread(target=_worker, args=(token, handler, store, stopped), daemon=True)
        for _ in range(worker_count)
    ]
    for worker in workers:
        worker.start()
    delivery = threading.Thread(
        target=_delivery_worker, args=(token, store, stopped), daemon=True
    )
    delivery.start()
    heartbeat = threading.Thread(
        target=_lease_heartbeat_loop,
        args=(store, stopped, lease_lost),
        daemon=True,
    )
    heartbeat.start()
    print(f"Telegram poller started with {worker_count} background workers")
    _service_pending_controls(token, handler, store)
    last_maintenance = 0.0
    last_ops_gc = time.monotonic()

    def request_shutdown(_signum, _frame) -> None:
        shutdown_requested.set()
        stopped.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        while not stopped.is_set():
            try:
                payload = _api(
                    token,
                    "getUpdates",
                    offset=store.update_offset(),
                    timeout=50,
                )
                if stopped.is_set():
                    break
                for update in payload.get("result") or []:
                    if _handle_ephemeral_oauth_update(token, handler, store, update):
                        continue
                    inbound_images, media_handled = _ingest_inbound_media(
                        token, handler, store, update
                    )
                    if media_handled:
                        continue
                    accepted = _call_supported(
                        store.accept_update,
                        update,
                        image_paths=inbound_images or None,
                    )
                    callback = update.get("callback_query") or {}
                    callback_id = callback.get("id")
                    if callback_id:
                        try:
                            _api(
                                token,
                                "answerCallbackQuery",
                                _http_timeout=4,
                                callback_query_id=callback_id,
                            )
                        except Exception:
                            pass
                    if accepted.get("disposition") == "control" and accepted.get("control"):
                        _control_reply(token, handler, store, accepted["control"])
                    elif accepted.get("disposition") == "steer":
                        try:
                            id8 = str(accepted.get("job_id") or "")[:8]
                            _send_reply(
                                token,
                                accepted["chat_id"],
                                BotReply(
                                    text=(
                                        f"↪ Styrning köad till pågående uppgift #{id8} "
                                        "(nästa säkra kontrollpunkt)."
                                    )
                                ),
                                reply_to_message_id=accepted.get("source_message_id"),
                            )
                        except Exception:
                            pass
                    elif accepted.get("disposition") == "queued" and accepted.get("parallel"):
                        try:
                            id8 = str(accepted.get("job_id") or "")[:8]
                            _send_reply(
                                token,
                                accepted["chat_id"],
                                BotReply(
                                    text=(
                                        f"⚡ Startar ny parallell uppgift #{id8} "
                                        "bredvid pågående körning."
                                    )
                                ),
                                reply_to_message_id=accepted.get("source_message_id"),
                            )
                        except Exception:
                            pass
                    elif accepted.get("disposition") == "overloaded":
                        msg = update.get("message") or {}
                        chat_id = (msg.get("chat") or {}).get("id")
                        if chat_id is not None:
                            _send_reply(
                                token,
                                chat_id,
                                BotReply(text="Jobbkön är full för den här chatten. Försök igen när ett jobb är klart."),
                            )
                _service_pending_controls(token, handler, store)
            except urllib.error.URLError as exc:
                print(_format_poll_error("Telegram network error", exc), file=sys.stderr)
                _record_poller_error("telegram_poller", exc, label="network_error")
                stopped.wait(3)
            except KeyboardInterrupt:
                shutdown_requested.set()
                print("Telegram bot stopped")
                return 0
            except Exception as exc:
                print(_format_poll_error("Telegram poll loop error", exc), file=sys.stderr)
                _record_poller_error("telegram_poller", exc, label="poll_loop")
                stopped.wait(2)
            finally:
                if not stopped.is_set() and not lease_lost.is_set():
                    now = time.monotonic()
                    if now - last_maintenance >= 30:
                        try:
                            if not _run_poller_maintenance(
                                store,
                                lease_lost,
                                lease_seconds=JOB_LEASE_SECONDS,
                                run_cleanup=True,
                            ):
                                stopped.set()
                            else:
                                last_maintenance = now
                                last_ops_gc = _maybe_run_periodic_gc(last_ops_gc, now=now)
                        except Exception as maint_exc:
                            print(
                                _format_poll_error("Telegram maintenance error", maint_exc),
                                file=sys.stderr,
                            )
                            _record_poller_error("telegram_poller", maint_exc, label="maintenance")
    finally:
        stopped.set()
        heartbeat.join(timeout=1.0)
        store.release_poller_lease()
    if lease_lost.is_set():
        return 2
    if shutdown_requested.is_set():
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
