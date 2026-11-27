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


def _https_dashboard_origin() -> str | None:
    """Return configured public dashboard base URL only if strict HTTPS origin."""
    base = dashboard_public_base()
    if not base:
        return None
    try:
        parsed = urlsplit(base)
        _ = parsed.port
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
    return base


def review_plan_url(site_id: str, plan_id: str) -> str | None:
    """Build an authenticated review URL only from a configured HTTPS origin."""
    site = str(site_id or "")
    plan = str(plan_id or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", site):
        return None
    if not re.fullmatch(r"wr-[a-f0-9]{12}", plan):
        return None
    if not _https_dashboard_origin():
        return None
    return dashboard_url(f"/wp/{site}/reviews/{plan}")


def changes_url(
    site_id: str,
    change_id: str | None = None,
    *,
    category: str | None = None,
    batch_id: str | None = None,
) -> str | None:
    """Build an authenticated change ledger / rollback URL from a strict HTTPS origin."""
    site = str(site_id or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", site):
        return None
    if not _https_dashboard_origin():
        return None
    if change_id is not None:
        cid = str(change_id or "").strip().lower()
        if not re.fullmatch(r"chg-[a-f0-9]{12}", cid):
            return None
        return dashboard_url(f"/wp/{site}/changes/{cid}")
    query: dict[str, str] = {}
    if category is not None:
        cat = str(category or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", cat):
            return None
        query["category"] = cat
    if batch_id is not None:
        bid = str(batch_id or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,40}", bid):
            return None
        query["batch_id"] = bid
    return dashboard_url(f"/wp/{site}/changes", query=query or None)


def wp_plugins_url(site_id: str) -> str | None:
    """Build an authenticated WordPress plugins console URL from a strict HTTPS origin."""
    site = str(site_id or "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", site):
        return None
    if not _https_dashboard_origin():
        return None
    return dashboard_url(f"/wp/{site}/plugins")


def research_brief_url(research_id: str) -> str | None:
    """Build an authenticated research brief URL only from a configured HTTPS origin."""
    rid = str(research_id or "")
    if not re.fullmatch(r"res-[0-9a-f]{12}", rid):
        return None
    if not _https_dashboard_origin():
        return None
    return dashboard_url(f"/research/{rid}")


def seo_dashboard_url(
    site_id: str | None = None,
    *,
    section: str | None = None,
) -> str | None:
    """Build an authenticated SEO Studio URL only from a configured HTTPS origin."""
    if not _https_dashboard_origin():
        return None
    sec = str(section or "").strip().lower()
    if sec in {"strategies", "campaigns", "link-packages", "parity", "digest", "vendors"}:
        path = f"/seo/{sec}"
    else:
        path = "/seo"
    query: dict[str, str] | None = None
    if site_id is not None:
        site = str(site_id or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", site):
            return None
        query = {"site_id": site}
    return dashboard_url(path, query=query)


def seo_vendors_url(market: str | None = None) -> str | None:
    """Build an authenticated SEO Studio vetted vendors URL from a strict HTTPS origin."""
    if not _https_dashboard_origin():
        return None
    query: dict[str, str] | None = None
    if market is not None:
        mkt = str(market or "").strip().lower()
        if mkt not in {"se", "no", "dk"}:
            return None
        query = {"market": mkt}
    return dashboard_url("/seo/vendors", query=query)


def seo_parity_url(site_id: str | None = None) -> str | None:
    """Build an authenticated SEO Studio Nordic parity URL from a strict HTTPS origin."""
    return seo_dashboard_url(site_id, section="parity")


def seo_digest_url(site_id: str | None = None) -> str | None:
    """Build an authenticated SEO + plugin drift digest URL from a strict HTTPS origin."""
    return seo_dashboard_url(site_id, section="digest")


def seo_plan_url(plan_id: str) -> str | None:
    """Build an authenticated SEO Studio entity/plan URL from a strict HTTPS origin."""
    pid = str(plan_id or "").strip().lower()
    if not _https_dashboard_origin():
        return None
    if re.fullmatch(r"seoa-[a-f0-9]{12}", pid):
        return dashboard_url(f"/seo/audit/{pid}")
    if re.fullmatch(r"seol-[a-f0-9]{12}", pid):
        return dashboard_url(f"/seo/link-packages/{pid}")
    if re.fullmatch(r"seos-[a-f0-9]{12}", pid):
        return dashboard_url("/seo/strategies", query={"id": pid})
    if re.fullmatch(r"seoc-[a-f0-9]{12}", pid):
        return dashboard_url("/seo/campaigns", query={"id": pid})
    if re.fullmatch(r"seom-[a-f0-9]{12}", pid):
        return dashboard_url("/seo", query={"plan_id": pid})
    if re.fullmatch(r"seob-[a-f0-9]{12}", pid):
        return dashboard_url("/seo", query={"batch_id": pid})
    return None


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
