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
from collections.abc import Callable
from typing import Any

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
