"""Run Telegram long-poll bot: python -m ecom_ops.bot"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.openclaw_commands import TELEGRAM_MENU_COMMANDS
from ecom_ops.bot.reply import BotReply, as_reply, chunk_text


def _api(token: str, method: str, **params: object) -> dict:
    api_base = f"https://api.telegram.org/bot{token}"
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in params.items()}
    ).encode()
    req = urllib.request.Request(f"{api_base}/{method}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _register_commands(token: str) -> None:
    try:
        _api(token, "setMyCommands", commands=TELEGRAM_MENU_COMMANDS)
        print(f"Registered {len(TELEGRAM_MENU_COMMANDS)} Telegram menu commands")
    except Exception as exc:
        print(f"setMyCommands skipped: {exc}", file=sys.stderr)


def _send_reply(token: str, chat_id: object, reply: BotReply) -> None:
    chunks = chunk_text(reply.text)
    for i, part in enumerate(chunks):
        params: dict[str, object] = {"chat_id": chat_id, "text": part or "(tomt svar)"}
        # Attach keyboard only on the last chunk
        if reply.reply_markup and i == len(chunks) - 1:
            params["reply_markup"] = reply.reply_markup
        _api(token, "sendMessage", **params)


def _handle_update(token: str, handler: BotHandler, update: dict) -> None:
    # Inline keyboard callbacks
    cq = update.get("callback_query")
    if cq:
        cq_id = cq.get("id")
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        # Ack button tap immediately so Telegram feels responsive
        if cq_id:
            try:
                _api(token, "answerCallbackQuery", callback_query_id=cq_id)
            except Exception as exc:
                print(f"answerCallbackQuery: {exc}", file=sys.stderr)
        if chat_id is not None:
            try:
                _api(token, "sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass
            reply = as_reply(handler.handle_callback(chat_id, data))
            _send_reply(token, chat_id, reply)
        return

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""
    if chat_id is None:
        return
    # Typing indicator before (potentially slow) work
    try:
        _api(token, "sendChatAction", chat_id=chat_id, action="typing")
    except Exception:
        pass
    reply = as_reply(handler.handle(chat_id, text))
    # Refresh typing once more if the reply was LLM-bound (long work)
    if reply.needs_typing:
        try:
            _api(token, "sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass
    _send_reply(token, chat_id, reply)


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN missing; dry-run exit.")
        return 0

    _register_commands(token)
    handler = BotHandler()
    offset = 0
    print("Telegram long-poll loop started (OpenClaw hybrid chat)")
    while True:
        try:
            payload = _api(token, "getUpdates", offset=offset, timeout=50)
            for update in payload.get("result") or []:
                offset = int(update["update_id"]) + 1
                try:
                    _handle_update(token, handler, update)
                except Exception as exc:
                    print(f"Update error: {exc}", file=sys.stderr)
        except urllib.error.URLError as exc:
            print(f"Network error: {exc}", file=sys.stderr)
            time.sleep(5)
        except KeyboardInterrupt:
            print("Bot stopped")
            return 0
        except Exception as exc:
            print(f"Loop error: {exc}", file=sys.stderr)
            time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
