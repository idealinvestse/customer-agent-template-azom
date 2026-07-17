"""Dashboard status helpers: onboarding checklist + health probes."""

from __future__ import annotations

import os
from typing import Any

from ecom_ops.config import load_app_config
from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured
from ecom_ops.security import get_env


def _secret_present(name: str) -> bool:
    val = os.environ.get(name, "").strip()
    return bool(val)


def secrets_checklist() -> list[dict[str, Any]]:
    """Return env secret names with present/missing (never values)."""
    groups = [
        ("WooCommerce", ["WOO_BASE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET"]),
        ("Mail", ["MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM"]),
        ("Mail OAuth", ["MAIL_OAUTH_CLIENT_ID", "MAIL_OAUTH_CLIENT_SECRET"]),
        ("SSH", ["SSH_HOST"]),
        ("LLM", ["OPENROUTER_API_KEY"]),
        ("Telegram", ["TELEGRAM_BOT_TOKEN"]),
        ("Dashboard", ["DASHBOARD_PASSWORD", "DASHBOARD_PASSWORD_HASH"]),
    ]
    out: list[dict[str, Any]] = []
    for group, keys in groups:
        for key in keys:
            out.append(
                {
                    "group": group,
                    "name": key,
                    "present": _secret_present(key),
                }
            )
    return out


def runtime_status() -> dict[str, Any]:
    mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
    store = GmailOAuthStore()
    try:
        cfg = load_app_config()
        customer = cfg.customer.customer
        domains = list(cfg.customer.domains)
        budget_cap = cfg.customer.budget_cap_llm
        openrouter_cap = cfg.limits.openrouter_cap
    except Exception as exc:
        customer = "unknown"
        domains = []
        budget_cap = 0.0
        openrouter_cap = 100.0
        return {
            "ok": False,
            "error": str(exc),
            "mock_mode": mock,
        }

    return {
        "ok": True,
        "mock_mode": mock,
        "customer": customer,
        "domains": domains,
        "budget_cap_llm": budget_cap,
        "openrouter_cap": openrouter_cap,
        "mail_provider": get_env("MAIL_PROVIDER", "generic_imap") or "generic_imap",
        "gmail_oauth_configured": gmail_oauth_configured(),
        "gmail_tokens_stored": store.has_tokens(),
        "telegram_configured": _secret_present("TELEGRAM_BOT_TOKEN"),
        "dashboard_password_set": _secret_present("DASHBOARD_PASSWORD")
        or _secret_present("DASHBOARD_PASSWORD_HASH"),
    }


def health_probe() -> dict[str, Any]:
    """Lightweight health checks for onboarding wizard."""
    checks: list[dict[str, Any]] = []
    runtime = runtime_status()

    checks.append(
        {
            "name": "config",
            "ok": runtime.get("ok", False),
            "detail": "sites.yaml + rbac loaded" if runtime.get("ok") else runtime.get("error", "fail"),
        }
    )

    try:
        from ecom_ops.actions.ssh_ops import SSHOpsService

        results = SSHOpsService().health(actor="jonatan")
        ssh_ok = all(r.ok for r in results)
        checks.append(
            {
                "name": "ssh_health",
                "ok": ssh_ok,
                "detail": f"{sum(1 for r in results if r.ok)}/{len(results)} commands ok",
            }
        )
    except Exception as exc:
        checks.append({"name": "ssh_health", "ok": False, "detail": str(exc)[:120]})

    try:
        from ecom_ops.integrations.mail import client_from_env

        client_from_env(use_mock=True)
        checks.append({"name": "mail_mock", "ok": True, "detail": "mock client ok"})
    except Exception as exc:
        checks.append({"name": "mail_mock", "ok": False, "detail": str(exc)[:120]})

    all_ok = all(c["ok"] for c in checks)
    return {"ok": all_ok, "checks": checks, "runtime": runtime}
