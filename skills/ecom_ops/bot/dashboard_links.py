"""Public dashboard deep links for Messenger/Telegram handoff."""

from __future__ import annotations

import os
from urllib.parse import urlencode


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
