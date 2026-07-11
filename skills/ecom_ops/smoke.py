"""Opt-in live/integration smoke checks (Woo, mail, Telegram)."""

from __future__ import annotations

import os
from typing import Any


def _is_mock() -> bool:
    return os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}


def _live_enabled(*, force: bool = False) -> bool:
    if force:
        return True
    return os.environ.get("AZOM_LIVE_SMOKE", "").lower() in {"1", "true", "yes"}


def run_live_smoke(*, force: bool = False) -> dict[str, Any]:
    """
    Run smoke checks. Without AZOM_LIVE_SMOKE=1 (or force=True), skip.

    When AZOM_USE_MOCK=1, uses in-memory transports (safe for CI).
    When live, hits Woo list_orders, mail fetch, Telegram getMe if configured.
    """
    if not _live_enabled(force=force):
        return {
            "ok": True,
            "skipped": True,
            "reason": "Set AZOM_LIVE_SMOKE=1 or pass force=True to run",
            "checks": [],
        }

    use_mock = _is_mock()
    checks: list[dict[str, Any]] = []

    # WooCommerce
    try:
        from ecom_ops.integrations.woocommerce import client_from_env as woo_from_env

        woo = woo_from_env(use_mock=True if use_mock else None)
        orders = woo.list_orders(per_page=1)
        checks.append(
            {
                "name": "woocommerce",
                "ok": isinstance(orders, list) and len(orders) >= 1,
                "detail": f"{len(orders)} order(s)" if isinstance(orders, list) else "empty",
                "mock": use_mock,
            }
        )
    except Exception as exc:
        checks.append(
            {"name": "woocommerce", "ok": False, "detail": str(exc)[:200], "mock": use_mock}
        )

    # Mail fetch
    try:
        from ecom_ops.integrations.mail import client_from_env as mail_from_env

        mail = mail_from_env(use_mock=True if use_mock else None)
        msgs = mail.fetch(folder="INBOX", unread_only=False, limit=1)
        checks.append(
            {
                "name": "mail",
                "ok": True,
                "detail": f"fetch ok ({len(msgs)} msg)",
                "mock": use_mock,
            }
        )
    except Exception as exc:
        checks.append(
            {"name": "mail", "ok": False, "detail": str(exc)[:200], "mock": use_mock}
        )

    # Telegram getMe — never call live API while mocking
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if use_mock:
        checks.append(
            {
                "name": "telegram",
                "ok": True,
                "detail": "skipped (mock mode)",
                "mock": True,
            }
        )
    elif not token:
        checks.append(
            {
                "name": "telegram",
                "ok": False,
                "detail": "TELEGRAM_BOT_TOKEN missing",
                "mock": False,
            }
        )
    else:
        try:
            import requests

            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getMe", timeout=15
            )
            ok = resp.status_code == 200 and bool((resp.json() or {}).get("ok"))
            checks.append(
                {
                    "name": "telegram",
                    "ok": ok,
                    "detail": f"HTTP {resp.status_code}",
                    "mock": False,
                }
            )
        except Exception as exc:
            checks.append(
                {"name": "telegram", "ok": False, "detail": str(exc)[:200], "mock": False}
            )

    all_ok = all(c.get("ok") for c in checks)
    return {"ok": all_ok, "skipped": False, "mock": use_mock, "checks": checks}
