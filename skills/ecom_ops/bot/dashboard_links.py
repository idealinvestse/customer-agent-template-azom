"""Public dashboard deep links for Messenger/Telegram handoff."""

from __future__ import annotations

import os
import re
from urllib.parse import urlencode, urlsplit


def dashboard_public_base() -> str | None:
    """Return configured public dashboard base URL, or None if unset."""
    raw = (os.environ.get("AZOM_DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/")
    if not raw:
        return None
    return raw


def dashboard_url(path: str = "/", *, query: dict[str, str] | None = None) -> str | None:
    """Build absolute dashboard URL. ``path`` should start with ``/``."""
    base = dashboard_public_base()
    if not base:
        return None
    p = path if path.startswith("/") else f"/{path}"
    url = f"{base}{p}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


def review_plan_url(site_id: str, plan_id: str) -> str | None:
    """Build an authenticated review URL only from a configured HTTPS origin."""
    site = str(site_id or "")
    plan = str(plan_id or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", site):
        return None
    if not re.fullmatch(r"wr-[a-f0-9]{12}", plan):
        return None
    base = dashboard_public_base()
    if not base:
        return None
    try:
        parsed = urlsplit(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return None
    except ValueError:
        return None
    return dashboard_url(f"/wp/{site}/reviews/{plan}")


def sandbox_preview_url(site_id: str, plan_id: str) -> str | None:
    """Build an authenticated interactive sandbox preview URL.

    Allows ``https://`` for any host, or ``http://`` strictly for loopback
    (``127.0.0.1`` / ``localhost``). Rejects non-loopback HTTP, userinfo,
    query strings, and fragments.
    """
    site = str(site_id or "")
    plan = str(plan_id or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", site):
        return None
    if not re.fullmatch(r"wr-[a-f0-9]{12}", plan):
        return None

    preview_path = f"/wp/{site}/reviews/{plan}/preview"
    raw_preview = (os.environ.get("AZOM_SANDBOX_PREVIEW_BASE_URL") or "").strip().rstrip("/")
    raw_public = (os.environ.get("AZOM_DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/")
    configured = raw_preview or raw_public
    if configured:
        try:
            parsed = urlsplit(configured)
            _ = parsed.port
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if (
            not host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return None
        if parsed.scheme == "https":
            return f"{configured}{preview_path}"
        if parsed.scheme == "http" and host in {"127.0.0.1", "localhost"}:
            return f"{configured}{preview_path}"
        return None

    dash_host = (os.environ.get("DASHBOARD_HOST") or "").strip().lower()
    dash_port = (os.environ.get("DASHBOARD_PORT") or "").strip()
    if dash_host in {"127.0.0.1", "localhost"} and dash_port.isdigit():
        port_num = int(dash_port)
        if 1 <= port_num <= 65535:
            return f"http://{dash_host}:{port_num}{preview_path}"
    return None


def case_detail_url(case_id: str, *, from_messenger: bool = True) -> str | None:
    cid = str(case_id or "").strip()
    if not cid:
        return None
    q = {"from": "messenger"} if from_messenger else None
    return dashboard_url(f"/cases/{cid}", query=q)


def cases_list_url(*, suggest: bool = False, from_messenger: bool = True) -> str | None:
    q: dict[str, str] = {}
    if from_messenger:
        q["from"] = "messenger"
    if suggest:
        q["suggest"] = "1"
    return dashboard_url("/cases", query=q or None)


def home_url(*, from_messenger: bool = True) -> str | None:
    q = {"from": "messenger"} if from_messenger else None
    return dashboard_url("/", query=q)


def link_footer(url: str | None, *, label: str = "Öppna i dashboard") -> str:
    """Plain-text footer for Telegram / when URL buttons unavailable."""
    if not url:
        return ""
    return f"{label}: {url}"
