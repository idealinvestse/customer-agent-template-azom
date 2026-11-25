"""Safe Telegram Bot API transport and thread-safe per-chat rate limiter.

Security invariants (AGENTS.md / SOUL.md):
- Never include, log, or store the Telegram bot token in exceptions or error
  messages.
- Parse HTTP error responses safely without chaining ``urllib.error.HTTPError``
  URLs into tracebacks.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ecom_ops.model_router.base import MAX_MULTIMODAL_IMAGE_BYTES, detect_image_format

MAX_INBOUND_IMAGE_BYTES = MAX_MULTIMODAL_IMAGE_BYTES
ALLOWED_INBOUND_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
_BOT_TOKEN_RE = re.compile(r"\bbot\d+:[A-Za-z0-9_\-]{8,}\b")
_NOT_MODIFIED_NEEDLE = "message is not modified"
_MISSING_MESSAGE_NEEDLES = (
    "message to edit not found",
    "message can't be edited",
)


def _strip_bot_token(text: str) -> str:
    if not text:
        return ""
    return _BOT_TOKEN_RE.sub("bot[REDACTED]", text)


def _parse_retry_after(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def is_not_modified_error(status_code: int | None, description: str) -> bool:
    """Return True when Telegram signals an idempotent identical edit (HTTP 400)."""
    return status_code == 400 and _NOT_MODIFIED_NEEDLE in (description or "").lower()


def is_missing_message_error(status_code: int | None, description: str) -> bool:
    """Return True when an edited Telegram message no longer exists or cannot be edited."""
    if status_code != 400:
        return False
    low = (description or "").lower()
    return any(needle in low for needle in _MISSING_MESSAGE_NEEDLES)


class TelegramApiError(Exception):
    """Structured Telegram Bot API HTTP error without exposing the bot token."""

    def __init__(
        self,
        status_code: int,
        description: str = "",
        retry_after: float | None = None,
        error_code: int | None = None,
        *,
        method: str = "",
    ) -> None:
        self.status_code = int(status_code)
        self.description = _strip_bot_token(" ".join(str(description or "").split()))[:400]
        self.retry_after = _parse_retry_after(retry_after)
        self.error_code = int(error_code) if error_code is not None else None
        self.method = str(method or "")
        head = f"Telegram API error {self.status_code}"
        if self.method:
            head = f"{head} ({self.method})"
        if self.description:
            head = f"{head}: {self.description}"
        super().__init__(head)


def parse_http_error(exc: urllib.error.HTTPError, *, method: str = "") -> TelegramApiError:
    """Read an ``HTTPError`` body/headers into a token-free ``TelegramApiError``."""
    raw = ""
    try:
        body = exc.read()
        if isinstance(body, bytes):
            raw = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            raw = body
    except Exception:
        raw = ""

    description = ""
    error_code: int | None = None
    retry_after: float | None = None

    if raw:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            description = str(payload.get("description") or "")
            raw_code = payload.get("error_code")
            if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                error_code = raw_code
            parameters = payload.get("parameters")
            if isinstance(parameters, dict):
                retry_after = _parse_retry_after(parameters.get("retry_after"))

    if retry_after is None and getattr(exc, "headers", None) is not None:
        try:
            header_val = exc.headers.get("Retry-After")
        except Exception:
            header_val = None
        retry_after = _parse_retry_after(header_val)

    if not description:
        description = str(getattr(exc, "reason", "") or f"HTTP {exc.code}")

    return TelegramApiError(
        status_code=int(getattr(exc, "code", 0) or 0),
        description=description,
        retry_after=retry_after,
        error_code=error_code,
        method=method,
    )


def telegram_api_post(token: str, method: str, **params: object) -> dict[str, Any]:
    """POST to the Telegram Bot API, normalizing 400 not-modified and HTTP errors."""
    request_timeout = params.pop("_http_timeout", 60)
    api_base = f"https://api.telegram.org/bot{token}"
    encoded_params = {
        k: (json.dumps(v) if isinstance(v, (list, dict)) else v)
        for k, v in params.items()
        if v is not None
    }
    data = urllib.parse.urlencode(encoded_params).encode("utf-8")
    req = urllib.request.Request(f"{api_base}/{method}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(request_timeout)) as resp:
            parsed = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        api_err = parse_http_error(exc, method=method)
        if is_not_modified_error(api_err.status_code, api_err.description):
            return {"ok": True, "not_modified": True, "result": {}}
        # Raise without chaining exc so the request URL containing the token is not attached.
        raise api_err from None

    if isinstance(parsed, dict) and parsed.get("ok") is False:
        desc = str(parsed.get("description") or "")
        code = parsed.get("error_code")
        code_int = int(code) if isinstance(code, int) and not isinstance(code, bool) else 400
        if is_not_modified_error(code_int, desc):
            return {"ok": True, "not_modified": True, "result": {}}
    return parsed if isinstance(parsed, dict) else {}


def _encode_multipart_form_data(
    fields: dict[str, object] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
) -> tuple[bytes, str]:
    boundary = f"----AzomBoundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in (fields or {}).items():
        if value is None:
            continue
        safe_name = str(name).replace('"', "").replace("\r", "").replace("\n", "")
        if isinstance(value, bool):
            encoded_val = "true" if value else "false"
        elif isinstance(value, (list, dict)):
            encoded_val = json.dumps(value)
        else:
            encoded_val = str(value)
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{safe_name}"\r\n\r\n'
                f"{encoded_val}\r\n"
            ).encode()
        )
    for name, spec in (files or {}).items():
        filename, content, content_type = spec
        safe_name = str(name).replace('"', "").replace("\r", "").replace("\n", "")
        safe_filename = str(filename).replace('"', "").replace("\r", "").replace("\n", "")
        safe_ctype = str(content_type or "application/octet-stream").replace("\r", "").replace("\n", "")
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{safe_name}"; filename="{safe_filename}"\r\n'
                f"Content-Type: {safe_ctype}\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def telegram_api_post_multipart(
    token: str,
    method: str,
    *,
    fields: dict[str, object] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    timeout: float = 15.0,
    rate_limiter: PerChatRateLimiter | None = None,
    chat_id: object | None = None,
) -> dict[str, Any]:
    """POST multipart/form-data to the Telegram Bot API with token-safe error handling."""
    effective_chat_id = chat_id if chat_id is not None else (fields or {}).get("chat_id")
    if rate_limiter is not None and effective_chat_id is not None:
        hard_delay = rate_limiter.retry_delay(effective_chat_id, force=True)
        if hard_delay > 0:
            raise TelegramApiError(
                429,
                "Too Many Requests: rate limiter backoff active",
                retry_after=hard_delay,
                error_code=429,
                method=method,
            )

    body, boundary = _encode_multipart_form_data(fields, files)
    api_base = f"https://api.telegram.org/bot{token}"
    req = urllib.request.Request(
        f"{api_base}/{method}",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            parsed = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        api_err = parse_http_error(exc, method=method)
        if is_not_modified_error(api_err.status_code, api_err.description):
            return {"ok": True, "not_modified": True, "result": {}}
        if (
            rate_limiter is not None
            and effective_chat_id is not None
            and api_err.status_code == 429
            and api_err.retry_after
        ):
            rate_limiter.record_retry_after(effective_chat_id, api_err.retry_after)
        # Raise without chaining exc so the request URL containing the token is not attached.
        raise api_err from None

    if isinstance(parsed, dict) and parsed.get("ok") is False:
        desc = str(parsed.get("description") or "")
        code = parsed.get("error_code")
        code_int = int(code) if isinstance(code, int) and not isinstance(code, bool) else 400
        if is_not_modified_error(code_int, desc):
            return {"ok": True, "not_modified": True, "result": {}}
        if code_int == 429 and rate_limiter is not None and effective_chat_id is not None:
            parameters = parsed.get("parameters")
            retry_after = _parse_retry_after(
                parameters.get("retry_after") if isinstance(parameters, dict) else None
            )
            if retry_after:
                rate_limiter.record_retry_after(effective_chat_id, retry_after)
        return parsed

    if (
        isinstance(parsed, dict)
        and parsed.get("ok") is True
        and rate_limiter is not None
        and effective_chat_id is not None
    ):
        rate_limiter.record_edit(effective_chat_id)
    return parsed if isinstance(parsed, dict) else {}


def send_photo(
    token: str,
    chat_id: object,
    photo_path: Path | str,
    *,
    caption: str = "",
    reply_to_message_id: int | None = None,
    allow_sending_without_reply: bool = True,
    timeout: float = 15.0,
    rate_limiter: PerChatRateLimiter | None = None,
) -> dict[str, Any]:
    """Upload a local PNG photo via Telegram ``sendPhoto`` using multipart/form-data."""
    path = Path(photo_path)
    fields: dict[str, object] = {"chat_id": str(chat_id)}
    clean_caption = str(caption or "")[:200]
    if clean_caption:
        fields["caption"] = clean_caption
    if (
        isinstance(reply_to_message_id, int)
        and not isinstance(reply_to_message_id, bool)
        and reply_to_message_id > 0
    ):
        fields["reply_to_message_id"] = reply_to_message_id
        if allow_sending_without_reply:
            fields["allow_sending_without_reply"] = True
    filename = path.name.replace('"', "").replace("\r", "").replace("\n", "")
    files = {"photo": (filename, path.read_bytes(), "image/png")}
    return telegram_api_post_multipart(
        token,
        "sendPhoto",
        fields=fields,
        files=files,
        timeout=timeout,
        rate_limiter=rate_limiter,
        chat_id=chat_id,
    )


def validate_telegram_file_path(file_path: str) -> str:
    """Validate a Telegram Bot API ``file_path`` against traversal and smuggling."""
    if not isinstance(file_path, str):
        raise ValueError("Invalid Telegram file_path")
    cleaned = file_path.strip()
    if not cleaned or len(cleaned) > 512:
        raise ValueError("Invalid Telegram file_path")
    candidates = [cleaned]
    unquoted = urllib.parse.unquote(cleaned)
    candidates.append(unquoted)
    candidates.append(urllib.parse.unquote(unquoted))
    for candidate in candidates:
        if any(ch in candidate for ch in ("\r", "\n", "\x00", "\\", "?", "#")):
            raise ValueError("Invalid Telegram file_path")
        if candidate.startswith("/") or "://" in candidate:
            raise ValueError("Invalid Telegram file_path")
        segments = candidate.split("/")
        if any(seg in {"", ".", ".."} for seg in segments):
            raise ValueError("Path traversal rejected in Telegram file_path")
    return cleaned


def get_file_path(token: str, file_id: str, *, timeout: float = 15.0) -> str:
    """Resolve a Telegram ``file_id`` to a validated relative ``file_path`` via ``getFile``."""
    fid = str(file_id or "").strip()
    if not fid or len(fid) > 256 or any(ch.isspace() or ord(ch) < 32 for ch in fid):
        raise ValueError("Invalid Telegram file_id")
    resp = telegram_api_post(token, "getFile", file_id=fid, _http_timeout=timeout)
    if not isinstance(resp, dict) or resp.get("ok") is not True:
        code = resp.get("error_code") if isinstance(resp, dict) else 400
        code_int = int(code) if isinstance(code, int) and not isinstance(code, bool) else 400
        desc = str(resp.get("description") or "getFile failed") if isinstance(resp, dict) else "getFile failed"
        raise TelegramApiError(code_int, desc, error_code=code_int, method="getFile")
    result = resp.get("result")
    if not isinstance(result, dict):
        raise ValueError("Telegram getFile response missing result")
    raw_path = result.get("file_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("Telegram getFile response missing file_path")
    return validate_telegram_file_path(raw_path)


def download_file_bytes(
    token: str,
    file_path: str,
    max_bytes: int = MAX_INBOUND_IMAGE_BYTES,
    *,
    timeout: float = 20.0,
) -> bytes:
    """Download an inbound Telegram image with size, path, and magic-byte validation.

    Security invariants:
    - Refuses path traversal / ``..`` in ``file_path`` before issuing any HTTP request.
    - Enforces ``max_bytes`` (default 5 MiB) both via Content-Length and streaming cap.
    - Validates PNG, JPEG, or WEBP magic bytes only.
    - Never chains raw ``urllib`` exceptions that could expose the bot token URL.
    """
    validated_path = validate_telegram_file_path(file_path)
    limit = int(max_bytes)
    if limit <= 0:
        raise ValueError("max_bytes must be positive")
    quoted_path = urllib.parse.quote(validated_path, safe="/")
    url = f"https://api.telegram.org/file/bot{token}/{quoted_path}"
    req = urllib.request.Request(url, method="GET")
    chunks: list[bytes] = []
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            headers = getattr(resp, "headers", None)
            if headers is not None:
                raw_len = headers.get("Content-Length")
                if raw_len is not None:
                    try:
                        if int(str(raw_len).strip()) > limit:
                            raise ValueError("Inbound image exceeds max_bytes limit")
                    except ValueError as exc:
                        if "max_bytes" in str(exc):
                            raise
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError("Inbound image exceeds max_bytes limit")
                chunks.append(chunk)
    except ValueError:
        raise
    except urllib.error.HTTPError as exc:
        api_err = parse_http_error(exc, method="downloadFile")
        raise api_err from None
    except urllib.error.URLError as exc:
        reason = _strip_bot_token(str(getattr(exc, "reason", "") or "network error"))
        raise TelegramApiError(0, reason, method="downloadFile") from None

    data = b"".join(chunks)
    if not data:
        raise ValueError("Downloaded Telegram image is empty")
    if detect_image_format(data) is None:
        raise ValueError("Unsupported image format; only PNG, JPEG, and WEBP are allowed")
    return data


def call_telegram_api(
    api_fn: Callable[..., dict[str, Any]],
    token: str,
    method: str,
    **params: object,
) -> dict[str, Any]:
    """Invoke a Telegram API callable with uniform HTTPError/not-modified handling."""
    try:
        result = api_fn(token, method, **params)
    except urllib.error.HTTPError as exc:
        api_err = parse_http_error(exc, method=method)
        if is_not_modified_error(api_err.status_code, api_err.description):
            return {"ok": True, "not_modified": True, "result": {}}
        raise api_err from None
    except TelegramApiError as exc:
        if is_not_modified_error(exc.status_code, exc.description):
            return {"ok": True, "not_modified": True, "result": {}}
        raise

    if isinstance(result, dict) and result.get("ok") is False:
        desc = str(result.get("description") or "")
        code = result.get("error_code")
        code_int = int(code) if isinstance(code, int) and not isinstance(code, bool) else 400
        if is_not_modified_error(code_int, desc):
            return {"ok": True, "not_modified": True, "result": {}}
    return result if isinstance(result, dict) else {}


class PerChatRateLimiter:
    """Thread-safe per-chat edit governor respecting Telegram 429 retry_after."""

    PRIVATE_MIN_INTERVAL = 1.0
    GROUP_MIN_INTERVAL = 3.2

    def __init__(
        self,
        *,
        private_min_interval: float = PRIVATE_MIN_INTERVAL,
        group_min_interval: float = GROUP_MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.private_min_interval = max(0.0, float(private_min_interval))
        self.group_min_interval = max(0.0, float(group_min_interval))
        self._clock = clock
        self._lock = threading.Lock()
        self._last_edit: dict[str, float] = {}
        self._chat_blocked_until: dict[str, float] = {}
        self._global_blocked_until: float = 0.0

    @staticmethod
    def _chat_key(chat_id: object) -> str:
        return str(chat_id).strip()

    def min_interval_for(self, chat_id: object) -> float:
        key = self._chat_key(chat_id)
        if key.startswith("-"):
            return self.group_min_interval
        try:
            if int(key) > 0:
                return self.private_min_interval
        except ValueError:
            pass
        return self.private_min_interval

    def blocked_until(self, chat_id: object) -> float:
        key = self._chat_key(chat_id)
        with self._lock:
            return max(self._global_blocked_until, self._chat_blocked_until.get(key, 0.0))

    def record_retry_after(
        self,
        chat_id: object,
        retry_after: float,
        *,
        global_scope: bool = False,
        now: float | None = None,
    ) -> float:
        """Record a Telegram 429 retry_after window for a chat (or globally)."""
        ts = self._clock() if now is None else float(now)
        until = ts + max(0.0, float(retry_after))
        key = self._chat_key(chat_id)
        with self._lock:
            current = self._chat_blocked_until.get(key, 0.0)
            self._chat_blocked_until[key] = max(current, until)
            if global_scope:
                self._global_blocked_until = max(self._global_blocked_until, until)
            return max(self._global_blocked_until, self._chat_blocked_until[key])

    def record_edit(self, chat_id: object, *, now: float | None = None) -> None:
        ts = self._clock() if now is None else float(now)
        key = self._chat_key(chat_id)
        with self._lock:
            self._last_edit[key] = ts

    def retry_delay(
        self,
        chat_id: object,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> float:
        """Return seconds to wait before editing ``chat_id`` (0.0 if allowed now)."""
        ts = self._clock() if now is None else float(now)
        key = self._chat_key(chat_id)
        min_interval = self.min_interval_for(chat_id)
        with self._lock:
            hard_until = max(self._global_blocked_until, self._chat_blocked_until.get(key, 0.0))
            if ts < hard_until:
                return hard_until - ts
            if force:
                return 0.0
            last = self._last_edit.get(key)
            if last is not None:
                elapsed = ts - last
                if elapsed < min_interval:
                    return min_interval - elapsed
            return 0.0

    def can_edit_now(
        self,
        chat_id: object,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> bool:
        """Return True if an edit for ``chat_id`` may proceed immediately."""
        return self.retry_delay(chat_id, force=force, now=now) <= 0.0

    def try_acquire_edit(
        self,
        chat_id: object,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> tuple[bool, float]:
        """Atomically check rate limits and mark an edit if allowed."""
        ts = self._clock() if now is None else float(now)
        key = self._chat_key(chat_id)
        min_interval = self.min_interval_for(chat_id)
        with self._lock:
            hard_until = max(self._global_blocked_until, self._chat_blocked_until.get(key, 0.0))
            if ts < hard_until:
                return False, hard_until - ts
            if not force:
                last = self._last_edit.get(key)
                if last is not None:
                    elapsed = ts - last
                    if elapsed < min_interval:
                        return False, min_interval - elapsed
            self._last_edit[key] = ts
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._last_edit.clear()
            self._chat_blocked_until.clear()
            self._global_blocked_until = 0.0
