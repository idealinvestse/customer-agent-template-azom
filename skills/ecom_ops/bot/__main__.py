"""Run Telegram long-poll bot: python -m ecom_ops.bot"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ecom_ops.bot.chat_agent import ChatCancelled
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import JOB_LEASE_SECONDS, TelegramJobStore
from ecom_ops.bot.openclaw_commands import TELEGRAM_MENU_COMMANDS
from ecom_ops.bot.reply import BotReply, as_reply, telegram_markup_to_actions
from ecom_ops.bot.store import TelegramConversationStore

_ACTION_MESSAGE = re.compile(
    r"^/cases\s+(?:approve|send|close|regenerate|regen|redraft)\b", re.I
)
_ACTION_CALLBACK = re.compile(
    r"^(?:cases:approve:[0-9a-f]{8}|cases:regen:[0-9a-f]{8}|"
    r"order:set:\d{1,12}:[a-z][a-z-]{0,31}|"
    r"product:desc:(?:0|[1-9]\d{0,11}):[01]|action:yes|escalate:yes|"
    r"wpfile:stage:fe-[a-f0-9]{12})$",
    re.I,
)


def _api(token: str, method: str, **params: object) -> dict:
    request_timeout = params.pop("_http_timeout", 60)
    api_base = f"https://api.telegram.org/bot{token}"
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in params.items()}
    ).encode()
    req = urllib.request.Request(f"{api_base}/{method}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=float(request_timeout)) as resp:
        return json.loads(resp.read().decode())


def _register_commands(token: str) -> None:
    try:
        commands = [
            *TELEGRAM_MENU_COMMANDS,
            {"command": "jobs", "description": "Telegram background jobs"},
            {"command": "cancel", "description": "Cancel a background job"},
            {"command": "reconcile", "description": "Oscar: reconcile uncertain action"},
        ]
        _api(token, "setMyCommands", _http_timeout=10, commands=commands)
        print(f"Registered {len(commands)} Telegram menu commands")
    except Exception as exc:
        print(f"setMyCommands skipped ({type(exc).__name__})", file=sys.stderr)


def _send_reply(token: str, chat_id: object, reply: BotReply) -> None:
    chunks = _telegram_safe_chunks(reply.text)
    for i, part in enumerate(chunks):
        params: dict[str, object] = {
            "chat_id": chat_id,
            "text": part or "(tomt svar)",
            "_http_timeout": 10,
        }
        # Attach keyboard only on the last chunk
        if reply.reply_markup and i == len(chunks) - 1:
            params["reply_markup"] = reply.reply_markup
        _api(token, "sendMessage", **params)


def _utf16_length(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


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
    _EDIT_INTERVAL = 0.75
    _CHUNK_LIMIT = 3900
    _STATUS_CLIP = 420

    def __init__(
        self,
        token: str,
        chat_id: object,
        *,
        job_store: TelegramJobStore | None = None,
        job_id: str | None = None,
        status_message_id: int | None = None,
        answer_message_ids: list[int] | None = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.job_store = job_store
        self.job_id = job_id
        self.lock = threading.RLock()
        self.started_at = time.monotonic()
        self.finished = False
        self.status_lines = ["⏳ Tar hand om din fråga…"]
        self.status_message_id = status_message_id
        self.last_status_edit = 0.0
        self.stream_text = ""
        self.stream_chunks: list[str] = []
        self.stream_current = ""
        self.answer_message_ids = list(answer_message_ids or [])
        self.answer_chunk_texts: list[str] = [""] * len(self.answer_message_ids)
        self.last_answer_edit = 0.0
        self.last_answer_length = 0
        self.timer = threading.Timer(self._STATUS_DELAY, self._show_initial_status)
        self.timer.daemon = True
        self.timer.start()

    @staticmethod
    def _message_id(result: dict) -> int | None:
        message = result.get("result") if isinstance(result, dict) else None
        value = message.get("message_id") if isinstance(message, dict) else None
        return value if isinstance(value, int) else None

    def _safe_api(self, method: str, **params: object) -> dict:
        try:
            result = _api(
                self.token,
                method,
                _http_timeout=4,
                **params,
            )
        except Exception:
            return {}
        if isinstance(result, dict) and result.get("ok"):
            return result
        description = str(result.get("description") or "").lower() if isinstance(result, dict) else ""
        if "message is not modified" in description:
            return {"ok": True, "result": {}}
        return {}

    def _status_text(self) -> str:
        return "\n".join(self.status_lines[-10:])[:4200]

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

    def _show_initial_status(self) -> None:
        with self.lock:
            if not self.finished:
                self._refresh_status(force=True)

    def _refresh_status(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if self.status_message_id is None:
            if not force and now - self.started_at < self._STATUS_DELAY:
                return
            result = self._safe_api(
                "sendMessage", chat_id=self.chat_id, text=self._status_text()
            )
            self.status_message_id = self._message_id(result)
            self.last_status_edit = now
            self._persist_ids()
            return
        if not force and now - self.last_status_edit < self._EDIT_INTERVAL:
            return
        self._safe_api(
            "editMessageText",
            chat_id=self.chat_id,
            message_id=self.status_message_id,
            text=self._status_text(),
        )
        self.last_status_edit = now

    def on_progress(self, message: str) -> None:
        value = " ".join(str(message or "").split())[: self._STATUS_CLIP]
        if not value:
            return
        with self.lock:
            if self.finished:
                return
            if not self.status_lines or self.status_lines[-1] != value:
                self.status_lines.append(value)
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
                if result:
                    self.answer_chunk_texts[index] = chunk
                else:
                    synced = False
                continue
            params = {"chat_id": self.chat_id, "text": chunk or "(tomt svar)"}
            if markup:
                params["reply_markup"] = markup
            result = self._safe_api("sendMessage", **params)
            message_id = self._message_id(result)
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

    def clear_answer_drafts(self) -> None:
        with self.lock:
            for message_id in self.answer_message_ids:
                self._safe_api("deleteMessage", chat_id=self.chat_id, message_id=message_id)
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
            if not self.stream_text.strip():
                return
            now = time.monotonic()
            if (
                self.answer_message_ids
                and now - self.last_answer_edit < self._EDIT_INTERVAL
                and len(self.stream_text) - self.last_answer_length < 80
            ):
                return
            self._sync_answer_chunks(self._visible_stream_chunks())
            self.last_answer_edit = now
            self.last_answer_length = len(self.stream_text)

    def on_text_reset(self) -> None:
        with self.lock:
            if self.finished:
                return
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
            self.status_lines.append("↻ Byter modellrutt och börjar om…")
            self._refresh_status()

    def finish(self, reply: BotReply) -> bool:
        with self.lock:
            if self.finished:
                return False
            self.finished = True
            self.timer.cancel()
            if self.status_message_id is not None:
                self.status_lines.append("✓ Svar klart")
                self._refresh_status(force=True)
            canonical_chunks = _telegram_safe_chunks(
                reply.text, limit=self._CHUNK_LIMIT
            )
            return self._sync_answer_chunks(
                canonical_chunks, reply_markup=reply.reply_markup
            )

    def fail(self) -> None:
        with self.lock:
            if self.finished:
                return
            self.finished = True
            self.timer.cancel()
            self.status_lines.append("⚠️ Arbetet avbröts av ett tekniskt fel")
            if self.status_message_id is not None:
                self._refresh_status(force=True)


_ACTION_LOCKS: dict[str, threading.Lock] = {}
_ACTION_LOCKS_GUARD = threading.Lock()


def _action_lock_key(job: dict, handler: BotHandler) -> str:
    payload = job.get("payload") or {}
    data = str(payload.get("data") or "")
    if data.startswith("wpfile:stage:"):
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


def _result_for_reply(reply: BotReply) -> dict:
    return {
        "text": reply.text,
        "reply_markup": reply.reply_markup,
        "needs_typing": bool(reply.needs_typing),
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
    )


def _deliver_job_result(token: str, store: TelegramJobStore, job: dict) -> bool:
    reply = _reply_from_result(job.get("result"))
    live = _LiveTelegramTurn(
        token,
        job["chat_id"],
        job_store=store,
        job_id=job["job_id"],
        status_message_id=job.get("status_message_id"),
        answer_message_ids=job.get("answer_message_ids") or [],
    )
    synced = live.finish(reply)
    if synced:
        store.mark_delivered(job["job_id"])
    return synced


def _preflight_action_job(handler: BotHandler, job: dict) -> str | None:
    """Fail closed on malformed, stale, or unauthorized queued actions."""
    payload = job.get("payload")
    if not isinstance(payload, dict):
        return "Ogiltig åtgärdskö. Ingen ändring utfördes."
    chat_id = str(job.get("chat_id") or "")
    if not chat_id or str(payload.get("chat_id") or "") != chat_id:
        return "Åtgärdens chatt stämmer inte. Ingen ändring utfördes."

    payload_type = payload.get("type")
    requires_snapshot = False
    if payload_type == "callback":
        data = str(payload.get("data") or "").strip()
        if not _ACTION_CALLBACK.fullmatch(data):
            return "Åtgärdsknappen kunde inte valideras. Ingen ändring utfördes."
        requires_snapshot = data in {"action:yes", "escalate:yes"} or data.startswith("wpfile:stage:")
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


def _process_job(token: str, handler: BotHandler, store: TelegramJobStore, job: dict) -> None:
    job_id = str(job["job_id"])
    chat_id = job["chat_id"]
    payload = job.get("payload") or {}
    live = _LiveTelegramTurn(
        token,
        chat_id,
        job_store=store,
        job_id=job_id,
        status_message_id=job.get("status_message_id"),
    )
    # A read job recovered after a crash is safe to replay. Remove its partial
    # answer draft and retain the status message so the user sees one resumed job.
    if job.get("answer_message_ids"):
        live.answer_message_ids = list(job["answer_message_ids"])
        live.answer_chunk_texts = [""] * len(live.answer_message_ids)
        live.clear_answer_drafts()

    def progress(message: str) -> None:
        safe = " ".join(str(message or "").split())[:_LiveTelegramTurn._STATUS_CLIP]
        if not safe:
            return
        try:
            store.add_event(job_id, "progress", safe)
        except Exception:
            pass
        live.on_progress(safe)

    def checkpoint() -> list[str]:
        steers, cancelled = store.consume_steers(job_id)
        if cancelled:
            raise ChatCancelled()
        return steers

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
            with _action_lock(_action_lock_key(job, handler)):
                checkpoint()
                progress("Kontrollerar åtgärd, behörighet och aktuell bekräftelse.")
                preflight_failure = _preflight_action_job(handler, job)
                if preflight_failure:
                    reply = BotReply(text=preflight_failure)
                else:
                    if not store.mark_side_effect_started(job_id):
                        raise ChatCancelled()
                    action_started = True
                    progress("Godkänd åtgärd körs.")
                    if payload.get("type") == "callback":
                        reply = as_reply(
                            handler.handle_callback(chat_id, str(payload.get("data") or ""))
                        )
                    else:
                        reply = as_reply(handler.handle(chat_id, str(payload.get("text") or "")))
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
            reply = as_reply(handler.handle_callback(chat_id, str(payload.get("data") or "")))
        else:
            reply = as_reply(
                handler.handle(
                    chat_id,
                    str(payload.get("text") or ""),
                    on_progress=progress,
                    on_text_delta=live.on_text_delta,
                    on_text_reset=live.on_text_reset,
                    on_checkpoint=checkpoint,
                )
            )
        cancelled = store.cancel_requested(job_id)
        if cancelled:
            reply = BotReply(text="Arbetet avbröts vid en säker kontrollpunkt.")
            state = "cancelled"
        elif job.get("kind") == "action" and reply.text.strip().lower().startswith("misslyckades"):
            state = "needs_reconciliation"
        else:
            state = "completed"
        store.complete_job(job_id, _result_for_reply(reply), state=state)
    except ChatCancelled:
        reply = BotReply(text="Arbetet avbröts vid en säker kontrollpunkt.")
        store.complete_job(job_id, _result_for_reply(reply), state="cancelled")
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
        store.complete_job(
            job_id,
            _result_for_reply(reply),
            state="needs_reconciliation" if uncertain else "failed",
            failure_class=type(exc).__name__[:80],
        )
    try:
        if live.finish(reply):
            store.mark_delivered(job_id)
    except Exception:
        # Result and Telegram message ids remain in SQLite for outbox retry.
        print(f"Telegram job {job_id[:8]} delivery deferred", file=sys.stderr)


def _worker(token: str, handler: BotHandler, store: TelegramJobStore, stopped: threading.Event) -> None:
    while not stopped.is_set():
        try:
            job = store.claim_next(lease_seconds=JOB_LEASE_SECONDS)
            if job is None:
                stopped.wait(0.25)
                continue
            _process_job(token, handler, store, job)
        except Exception as exc:
            print(f"Telegram worker loop error ({type(exc).__name__})", file=sys.stderr)
            stopped.wait(1)


def _authorized_control(handler: BotHandler, chat_id: str) -> BotReply | None:
    if not handler._peer_allowed(chat_id):
        return handler._deny_allowlist()
    try:
        handler._resolve_actor(chat_id)
    except Exception:
        return handler._deny_actor()
    return None


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
        active = store.active_job(chat_id)
        jobs = store.conversation_jobs(chat_id)
        if active:
            events = store.events(active["job_id"], limit=3)
            phase = events[-1]["summary"] if events else "Arbetet pågår"
            reply = BotReply(
                text=f"Pågående jobb {active['job_id'][:8]} ({active['kind']}): {phase}."
            )
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
        if result == "not_found" and not args:
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


def main() -> int:
    from ecom_ops.runtime_env import bootstrap_runtime

    bootstrap_runtime()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN missing; dry-run exit.")
        return 0

    store = TelegramJobStore()
    if not store.acquire_poller_lease():
        print("Another Telegram poller owns the database lease.", file=sys.stderr)
        return 2
    try:
        recovered = store.recover_after_restart()
        print(
            "Telegram jobs recovered: "
            f"queued={recovered['queued']} reconciliation={recovered['reconciliation']}"
        )
        legacy_path = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data")) / "telegram_state.json"
        handler = BotHandler(store=TelegramConversationStore(store.path, legacy_path=legacy_path))
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
    print(f"Telegram poller started with {worker_count} background workers")
    _service_pending_controls(token, handler, store)
    last_maintenance = 0.0
    def request_shutdown(_signum, _frame) -> None:
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
                    accepted = store.accept_update(update)
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
                            _send_reply(
                                token,
                                accepted["chat_id"],
                                BotReply(text="Följdfrågan är köad till nästa säkra punkt i jobbet."),
                            )
                        except Exception:
                            pass
                    elif accepted.get("disposition") == "overloaded":
                        msg = update.get("message") or {}
                        chat_id = ((msg.get("chat") or {}).get("id"))
                        if chat_id is not None:
                            _send_reply(
                                token,
                                chat_id,
                                BotReply(text="Jobbkön är full för den här chatten. Försök igen när ett jobb är klart."),
                            )
                _service_pending_controls(token, handler, store)
                now = time.monotonic()
                if now - last_maintenance >= 30:
                    if not store.renew_poller_lease():
                        print("Telegram poller lease lost; stopping intake.", file=sys.stderr)
                        break
                    store.renew_leases(lease_seconds=JOB_LEASE_SECONDS)
                    store.recover_expired_jobs()
                    store.purge()
                    last_maintenance = now
            except urllib.error.URLError as exc:
                print(f"Telegram network error ({type(exc).__name__})", file=sys.stderr)
                time.sleep(3)
            except KeyboardInterrupt:
                print("Telegram bot stopped")
                return 0
            except Exception as exc:
                print(f"Telegram poll loop error ({type(exc).__name__})", file=sys.stderr)
                time.sleep(2)
    finally:
        stopped.set()
        store.release_poller_lease()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
