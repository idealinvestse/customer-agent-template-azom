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

    # Web search (mock fixture in --mock; live probe only when web_search_enabled())
    if use_mock:
        try:
            from ecom_ops.integrations.search.base import SearchRequest
            from ecom_ops.integrations.search.router import get_search_router

            router = get_search_router()
            res = router.search(
                SearchRequest(
                    query="woocommerce order confirmation",
                    profile_id="technical_docs",
                    max_results=2,
                    mode="quick",
                )
            )
            ok = bool(res.ok and len(res.hits) >= 1 and res.provenance in {"mock", "mock_fixture"})
            checks.append(
                {
                    "name": "web_search",
                    "ok": ok,
                    "status": "mock",
                    "detail": f"mock fixture ({len(res.hits)} hit(s))",
                    "mock": True,
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "web_search",
                    "ok": False,
                    "status": "error",
                    "detail": type(exc).__name__,
                    "mock": True,
                }
            )
    else:
        try:
            from ecom_ops.research.config import kill_switch_active, web_search_enabled

            if kill_switch_active("search"):
                checks.append(
                    {
                        "name": "web_search",
                        "ok": True,
                        "status": "skipped_disabled",
                        "detail": "skipped_disabled (kill switch active)",
                        "mock": False,
                    }
                )
            elif not web_search_enabled():
                checks.append(
                    {
                        "name": "web_search",
                        "ok": True,
                        "status": "skipped_disabled",
                        "detail": "skipped_disabled (AZOM_WEB_SEARCH_ENABLED not enabled)",
                        "mock": False,
                    }
                )
            else:
                from ecom_ops.integrations.search.router import get_search_router

                router = get_search_router()
                routes_info = router.routes()
                configured = [
                    rid
                    for rid in router.ordered_route_ids()
                    if bool((routes_info.get(rid) or {}).get("configured"))
                ]
                if not configured:
                    checks.append(
                        {
                            "name": "web_search",
                            "ok": False,
                            "status": "missing",
                            "detail": "no search provider configured",
                            "mock": False,
                        }
                    )
                else:
                    ok_count = 0
                    parts: list[str] = []
                    for rid in configured:
                        probe = router.test_route(rid)
                        if probe.get("ok"):
                            ok_count += 1
                            parts.append(f"{rid}:ok")
                        else:
                            code = probe.get("error_code") or probe.get("error_class") or probe.get("status") or "error"
                            parts.append(f"{rid}:{code}")
                    checks.append(
                        {
                            "name": "web_search",
                            "ok": ok_count > 0,
                            "status": "ok" if ok_count > 0 else "error",
                            "detail": f"{ok_count}/{len(configured)} route(s) ok ({', '.join(parts)})",
                            "mock": False,
                        }
                    )
        except Exception as exc:
            checks.append(
                {
                    "name": "web_search",
                    "ok": False,
                    "status": "error",
                    "detail": type(exc).__name__,
                    "mock": False,
                }
            )

    all_ok = all(c.get("ok") for c in checks)
    return {"ok": all_ok, "skipped": False, "mock": use_mock, "checks": checks}
