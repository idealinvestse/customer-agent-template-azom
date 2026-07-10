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


def _api(token: str, method: str, **params: object) -> dict:
    api_base = f"https://api.telegram.org/bot{token}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{api_base}/{method}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN missing; dry-run exit.")
        return 0

    handler = BotHandler()
    offset = 0
    print("Telegram long-poll loop started (conversation state enabled)")
    while True:
        try:
            payload = _api(token, "getUpdates", offset=offset, timeout=50)
            for update in payload.get("result") or []:
                offset = int(update["update_id"]) + 1
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text") or ""
                if chat_id is None:
                    continue
                reply = handler.handle(chat_id, text)
                _api(token, "sendMessage", chat_id=chat_id, text=reply)
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
