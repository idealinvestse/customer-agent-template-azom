#!/usr/bin/env bash
# Dedikerad Telegram-bot för Jonatan (engagement + read-only ops).
# Requires TELEGRAM_BOT_TOKEN in env or .env
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/skills${PYTHONPATH:+:$PYTHONPATH}"
export AZOM_CONFIG_DIR="${AZOM_CONFIG_DIR:-$ROOT/config}"
export AZOM_DATA_DIR="${AZOM_DATA_DIR:-$ROOT/.azom-data}"

cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  # shellcheck disable=SC1090
  source "$ROOT/.env"
  set +a
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "TELEGRAM_BOT_TOKEN saknas – bot startar i dry-run läge."
  echo "Bot startad – engagement fokus (dry-run, ingen token)"
  exit 0
fi

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python not found" >&2
  exit 1
fi

echo "Bot startad – engagement fokus"
exec "$PY" - <<'PY'
"""Minimal long-polling Telegram bot (read-only commands for Jonatan)."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    print("No token; exiting dry-run.")
    sys.exit(0)

API = f"https://api.telegram.org/bot{TOKEN}"


def api(method: str, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def handle(text: str) -> str:
    t = (text or "").strip().lower()
    if t in {"/start", "/help"}:
        return (
            "Azom Ops Bot (Jonatan read-only)\n"
            "/help – denna hjälp\n"
            "/health – SSH health (mock/live)\n"
            "/brief – daily KPI brief snapshot\n"
        )
    if t == "/health":
        try:
            from ecom_ops.actions.ssh_ops import SSHOpsService

            results = SSHOpsService().health(actor="jonatan")
            lines = []
            for r in results:
                ok = "ok" if r.ok else "fail"
                cmd = r.result.command if r.result else "?"
                lines.append(f"{cmd}: {ok}")
            return "SSH health:\n" + "\n".join(lines)
        except Exception as exc:
            return f"Health error: {exc}"
    if t == "/brief":
        try:
            from ecom_ops.config import load_app_config
            from ecom_ops.telemetry import Telemetry

            cfg = load_app_config()
            cost = Telemetry().sum_cost_usd()
            return (
                f"Customer: {cfg.customer.customer}\n"
                f"Domains: {', '.join(cfg.customer.domains)}\n"
                f"LLM cost USD: {cost:.4f} / cap {cfg.limits.openrouter_cap}"
            )
        except Exception as exc:
            return f"Brief error: {exc}"
    return "Okänt kommando. /help för lista."


def main() -> int:
    offset = 0
    print("Telegram long-poll loop started")
    while True:
        try:
            payload = api("getUpdates", offset=offset, timeout=50)
            for update in payload.get("result") or []:
                offset = int(update["update_id"]) + 1
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text") or ""
                if chat_id is None:
                    continue
                reply = handle(text)
                api("sendMessage", chat_id=chat_id, text=reply)
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
PY
