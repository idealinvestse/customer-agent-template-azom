"""Oscar-only connection probes for secrets / integrations (never return secret values)."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ProbeResult:
    id: str
    label: str
    status: str  # ok | missing | error | skipped
    message: str
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _result(pid: str, label: str, status: str, message: str) -> ProbeResult:
    return ProbeResult(
        id=pid, label=label, status=status, message=message, checked_at=_now()
    )


def _env(*keys: str) -> bool:
    return all(bool(os.environ.get(k, "").strip()) for k in keys)


def probe_woocommerce() -> ProbeResult:
    label = "WooCommerce"
    needed = ("WOO_BASE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET")
    if not _env(*needed):
        missing = [k for k in needed if not os.environ.get(k, "").strip()]
        return _result("woocommerce", label, "missing", f"Saknas: {', '.join(missing)}")
    try:
        from ecom_ops.integrations.woocommerce import client_from_env

        use_mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
        client = client_from_env(use_mock=use_mock if use_mock else None)
        # List one order — validates credentials without requiring a fixed order id
        orders = client.list_orders(per_page=1)
        if orders:
            order = orders[0]
            return _result(
                "woocommerce",
                label,
                "ok",
                f"API ok · sample order {order.id} (status={order.status})",
            )
        return _result("woocommerce", label, "ok", "API ok · orders endpoint reachable")
    except Exception as exc:
        return _result("woocommerce", label, "error", str(exc)[:200])


def probe_mail() -> ProbeResult:
    label = "Mail"
    if not (
        os.environ.get("MAIL_USERNAME", "").strip()
        or os.environ.get("MAIL_FROM", "").strip()
    ):
        return _result("mail", label, "missing", "MAIL_USERNAME/MAIL_FROM saknas")
    try:
        from ecom_ops.integrations.mail import client_from_env

        use_mock = os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}
        client = client_from_env(use_mock=True if use_mock else None)
        # Prefer mock-safe fetch; live may fail without network — still validates client build
        msgs = client.fetch(folder="INBOX", unread_only=True, limit=1)
        return _result(
            "mail",
            label,
            "ok",
            f"Client ok · fetch returned {len(msgs)} message(s)",
        )
    except Exception as exc:
        return _result("mail", label, "error", str(exc)[:200])


def probe_telegram() -> ProbeResult:
    label = "Telegram"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return _result("telegram", label, "missing", "TELEGRAM_BOT_TOKEN saknas")
    if os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}:
        return _result("telegram", label, "ok", "Token present (mock — skip getMe)")
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        req = Request(url, method="GET")
        with urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        if '"ok":true' in raw.replace(" ", "") or '"ok": true' in raw:
            return _result("telegram", label, "ok", "getMe OK")
        return _result("telegram", label, "error", "getMe failed")
    except Exception as exc:
        return _result("telegram", label, "error", str(exc)[:200])


def probe_openrouter() -> ProbeResult:
    label = "OpenRouter"
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return _result("openrouter", label, "missing", "OPENROUTER_API_KEY saknas")
    if os.environ.get("AZOM_USE_MOCK", "").lower() in {"1", "true", "yes"}:
        return _result("openrouter", label, "ok", "Key present (mock — skip HTTP)")
    try:
        req = Request(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            method="GET",
        )
        with urlopen(req, timeout=10) as resp:
            code = resp.status
        if 200 <= code < 300:
            return _result("openrouter", label, "ok", f"models endpoint HTTP {code}")
        return _result("openrouter", label, "error", f"HTTP {code}")
    except Exception as exc:
        return _result("openrouter", label, "error", str(exc)[:200])


def probe_ssh() -> ProbeResult:
    label = "SSH"
    if not os.environ.get("SSH_HOST", "").strip() and os.environ.get(
        "AZOM_USE_MOCK", ""
    ).lower() not in {"1", "true", "yes"}:
        # Local mock SSH still works without SSH_HOST
        pass
    try:
        from ecom_ops.actions.ssh_ops import SSHOpsService

        results = SSHOpsService().health(actor="jonatan")
        ok_n = sum(1 for r in results if r.ok)
        total = len(results)
        if total == 0:
            return _result("ssh", label, "skipped", "Inga health-kommandon")
        if ok_n == total:
            return _result("ssh", label, "ok", f"{ok_n}/{total} commands ok")
        return _result("ssh", label, "error", f"{ok_n}/{total} commands ok")
    except Exception as exc:
        return _result("ssh", label, "error", str(exc)[:200])


def probe_gmail_oauth() -> ProbeResult:
    label = "Gmail OAuth"
    try:
        from ecom_ops.oauth.gmail import GmailOAuthStore, gmail_oauth_configured

        configured = gmail_oauth_configured()
        store = GmailOAuthStore()
        has = store.has_tokens()
        if has:
            return _result("gmail_oauth", label, "ok", "Tokens stored")
        if configured:
            return _result(
                "gmail_oauth",
                label,
                "missing",
                "Client configured but no tokens — run /oauth/gmail/start",
            )
        return _result(
            "gmail_oauth",
            label,
            "missing",
            "MAIL_OAUTH_CLIENT_ID/SECRET saknas",
        )
    except Exception as exc:
        return _result("gmail_oauth", label, "error", str(exc)[:200])


PROBES: dict[str, Callable[[], ProbeResult]] = {
    "woocommerce": probe_woocommerce,
    "mail": probe_mail,
    "telegram": probe_telegram,
    "openrouter": probe_openrouter,
    "ssh": probe_ssh,
    "gmail_oauth": probe_gmail_oauth,
}


def run_probe(probe_id: str) -> ProbeResult:
    fn = PROBES.get(probe_id)
    if not fn:
        return _result(probe_id, probe_id, "error", f"Okänd probe: {probe_id}")
    return fn()


def run_all_probes() -> list[ProbeResult]:
    return [fn() for fn in PROBES.values()]


def probe_summary(results: list[ProbeResult] | None = None) -> dict[str, Any]:
    rows = results if results is not None else run_all_probes()
    counts = {"ok": 0, "missing": 0, "error": 0, "skipped": 0}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {
        "ok": counts.get("error", 0) == 0 and counts.get("missing", 0) == 0,
        "counts": counts,
        "results": [r.to_dict() for r in rows],
    }
