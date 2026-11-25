"""Read-only live page fetch and isolated overlay browser preview.

This module performs only credential-free HTTPS GET requests to a site's
canonical host (and optional static CDN assets in headless Chrome) under CDP
Fetch interception. Never sends POSTs, never sends cookies or site credentials,
and never opens checkout, cart, account, or admin paths.
"""

from __future__ import annotations

import base64
import html as html_lib
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp_browser_smoke import (
    _PNG_MAGIC,
    _WP_FATAL_MARKERS,
    _LoopbackCdpClient,
    _pick_loopback_port,
    _png_dimensions,
    _wait_cdp_page_ws,
    find_browser_binary,
)
from ecom_ops.integrations.wp_local_runtime import build_php_child_env
from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
from ecom_ops.wp_ops.file_changes import rest_reads_allowed
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.review_workspace import ReviewError

PREVIEW_VIEWPORTS: tuple[tuple[str, int, int, bool], ...] = (
    ("desktop", 1280, 800, False),
    ("mobile", 390, 844, True),
)
_PREVIEW_PATH_RE = re.compile(r"^/[A-Za-z0-9/_\-.%]{0,200}$")
_REFUSED_PATH_TOKENS = (
    "wp-admin",
    "wp-login",
    "wp-json",
    "rest_route",
    "xmlrpc",
    "wp-cron",
    "admin-ajax",
    "wc-ajax",
    "add-to-cart",
    "/cart",
    "/checkout",
    "/my-account",
    "/handlekurv",
    "/kasse",
    "/min-konto",
)
_SITE_BLOCKED_SUBSTRINGS = (
    "wp-admin",
    "wp-login",
    "admin-ajax",
    "wc-ajax",
    "/wp-json",
    "rest_route",
    "xmlrpc",
    "wp-cron",
    "add-to-cart",
    "/cart",
    "/checkout",
    "/my-account",
    "/handlekurv",
    "/kasse",
    "/min-konto",
)
_STATIC_ASSET_HOSTS = frozenset(
    {
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "ajax.googleapis.com",
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
        "use.typekit.net",
        "p.typekit.net",
        "assets.website-files.com",
        "uploads-ssl.webflow.com",
        "d3e54v103j8qbb.cloudfront.net",
    }
)
_TRACKER_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com",
    "hotjar.com",
    "clarity.ms",
    "tiktok.com",
    "snapchat.com",
    "bing.com",
    "pinterest.com",
)
_PREVIEW_USER_AGENT = "Mozilla/5.0 (AzomOps read-only preview)"
_MAX_HTML_BYTES = 3_000_000
FOCUS_SECTIONS: tuple[str, ...] = ("top", "header", "hero", "main", "footer", "bottom")
_FOCUS_SECTIONS_SET = frozenset(FOCUS_SECTIONS)
_MAX_FOCUS_LEN = 120
_MAX_FULL_PAGE_HEIGHT = 6000
_SAFE_CSS_SELECTOR_RE = re.compile(r'^[A-Za-z0-9_#.\-\s>,+~:()\[\]="\'^*$|/]{1,120}$')
_UNSAFE_SELECTOR_SUBSTRINGS = (
    "javascript:",
    "expression(",
    "url(",
    "<!--",
    "-->",
    "</",
)
_PREFERRED_SHOT_ORDER: tuple[str, ...] = (
    "change-desktop.png",
    "change-mobile.png",
    "focus-desktop.png",
    "focus-mobile.png",
    "preview-desktop.png",
    "live-desktop.png",
    "preview-mobile.png",
    "live-mobile.png",
)
_SANDBOX_TRACKER_TOKENS: tuple[str, ...] = (
    "googletagmanager",
    "google-analytics",
    "facebook.net",
    "doubleclick",
    "hotjar",
    "clarity.ms",
    "cookieyes",
    "wiserreview",
)
_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>[\s\S]*?</script\s*>", re.IGNORECASE)
_NOSCRIPT_TAG_RE = re.compile(r"<noscript\b[^>]*>[\s\S]*?</noscript\s*>", re.IGNORECASE)
_BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BODY_OPEN_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE_TAG_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_HTML_OPEN_TAG_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)


def live_preview_enabled() -> bool:
    """True only when explicit live preview env flag and live reads are enabled."""
    return env_flag("AZOM_WP_LIVE_PREVIEW") and rest_reads_allowed()


def validate_focus_options(
    *,
    focus_section: str | None = None,
    focus_selector: str | None = None,
    focus_text: str | None = None,
    full_page: bool = False,
) -> tuple[str | None, str | None, str | None, bool]:
    """Validate optional targeted-screenshot focus parameters."""
    clean_section: str | None = None
    if focus_section is not None:
        if not isinstance(focus_section, str):
            raise ReviewError("InvalidFocusTarget")
        sec = focus_section.strip().lower()
        if sec:
            if sec not in _FOCUS_SECTIONS_SET:
                raise ReviewError("InvalidFocusTarget")
            clean_section = sec

    clean_selector: str | None = None
    if focus_selector is not None:
        if not isinstance(focus_selector, str):
            raise ReviewError("InvalidFocusTarget")
        sel = focus_selector.strip()
        if sel:
            if len(sel) > _MAX_FOCUS_LEN:
                raise ReviewError("InvalidFocusTarget")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in sel):
                raise ReviewError("InvalidFocusTarget")
            if _SAFE_CSS_SELECTOR_RE.fullmatch(sel) is None:
                raise ReviewError("InvalidFocusTarget")
            lower_sel = sel.lower()
            if any(bad in lower_sel for bad in _UNSAFE_SELECTOR_SUBSTRINGS):
                raise ReviewError("InvalidFocusTarget")
            clean_selector = sel

    clean_text: str | None = None
    if focus_text is not None:
        if not isinstance(focus_text, str):
            raise ReviewError("InvalidFocusTarget")
        txt = " ".join(focus_text.split()).strip()
        if txt:
            if len(txt) > _MAX_FOCUS_LEN:
                raise ReviewError("InvalidFocusTarget")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in txt):
                raise ReviewError("InvalidFocusTarget")
            clean_text = txt

    if not isinstance(full_page, bool):
        raise ReviewError("InvalidFocusTarget")

    return clean_section, clean_selector, clean_text, full_page


def _unquote_twice(value: str) -> str:
    once = urllib.parse.unquote(value)
    return urllib.parse.unquote(once)


def validate_preview_path(path: str | None) -> str:
    """Validate and normalize a public GET preview path."""
    raw = "/" if path is None or str(path).strip() == "" else str(path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    if "?" in raw or "#" in raw or "\\" in raw or ".." in raw or "//" in raw:
        raise ReviewError("PreviewPathRefused")
    if _PREVIEW_PATH_RE.fullmatch(raw) is None:
        raise ReviewError("PreviewPathRefused")
    decoded = _unquote_twice(raw)
    if "?" in decoded or "#" in decoded or "\\" in decoded or ".." in decoded or "//" in decoded:
        raise ReviewError("PreviewPathRefused")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        raise ReviewError("PreviewPathRefused")
    for text in (raw.lower(), decoded.lower()):
        if any(token in text for token in _REFUSED_PATH_TOKENS):
            raise ReviewError("PreviewPathRefused")
    return raw


def _allowed_site_hosts(site_host: str) -> frozenset[str]:
    host = str(site_host or "").strip().lower().rstrip(".")
    if not host:
        return frozenset()
    if host.startswith("www."):
        bare = host[4:]
        return frozenset({host, bare}) if bare else frozenset({host})
    return frozenset({host, f"www.{host}"})


def _host_matches_suffix(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _is_ip_literal(host: str) -> bool:
    raw = host.strip("[]")
    try:
        ipaddress.ip_address(raw)
        return True
    except ValueError:
        return False


def _safe_https_parsed(parsed: urllib.parse.ParseResult) -> tuple[bool, str, str]:
    if parsed.scheme != "https":
        return False, "", "non_https_blocked"
    if parsed.username or parsed.password:
        return False, "", "userinfo_blocked"
    try:
        port = parsed.port
    except ValueError:
        return False, "", "invalid_url"
    if port not in (None, 443):
        return False, "", "port_blocked"
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in {"localhost", "127.0.0.1", "::1"} or _is_ip_literal(host):
        return False, "", "host_blocked"
    if host.endswith(".example.test"):
        return False, "", "synthetic_host_blocked"
    return True, host, ""


def _host_resolves_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return False
        ip_str = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _blocked_site_path_reason(parsed: urllib.parse.ParseResult) -> str | None:
    path_and_query = parsed.path or "/"
    if parsed.query:
        path_and_query = f"{path_and_query}?{parsed.query}"
    decoded_pq = _unquote_twice(path_and_query)
    for candidate in (path_and_query.lower(), decoded_pq.lower()):
        for blocked in _SITE_BLOCKED_SUBSTRINGS:
            if blocked in candidate:
                return f"site_path_blocked:{blocked.strip('/')}"
    return None


def classify_request(method: str, url: str, *, site_host: str) -> tuple[bool, str]:
    """Decide whether headless preview Chrome may issue this subresource request."""
    verb = str(method or "").strip().upper()
    if verb not in {"GET", "HEAD"}:
        return False, "method_blocked"
    raw_url = str(url or "").strip()
    lower_url = raw_url.lower()
    if lower_url.startswith(("data:", "blob:", "about:")):
        return True, "inline"
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except ValueError:
        return False, "invalid_url"
    ok_url, host, err_reason = _safe_https_parsed(parsed)
    if not ok_url:
        return False, err_reason
    if any(_host_matches_suffix(host, tracker) for tracker in _TRACKER_HOSTS):
        return False, "tracker_blocked"

    site_hosts = _allowed_site_hosts(site_host)
    if host in site_hosts:
        blocked_reason = _blocked_site_path_reason(parsed)
        if blocked_reason:
            return False, blocked_reason
        return True, "site_asset"
    if host in _STATIC_ASSET_HOSTS:
        return True, "cdn_asset"
    return False, "foreign_host_blocked"


class _AllowlistedSiteRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects that leave the registered site's allowlisted hosts."""

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        parsed = urllib.parse.urlparse(str(newurl or ""))
        ok_url, host, err_reason = _safe_https_parsed(parsed)
        if not ok_url or host not in self._allowed_hosts:
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"Redirect refused ({err_reason or 'foreign_host'}): {host}",
                headers,
                fp,
            )
        blocked_reason = _blocked_site_path_reason(parsed)
        if blocked_reason:
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"Redirect to blocked WordPress path refused ({blocked_reason})",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_live_html(
    site: Site,
    path: str = "/",
    *,
    timeout: float = 15.0,
    max_bytes: int = _MAX_HTML_BYTES,
) -> dict[str, Any]:
    """Fetch public live HTML via HTTPS GET with no cookies and no site credentials."""
    clean_path = validate_preview_path(path)
    parsed_base = urllib.parse.urlparse(str(site.canonical_url or ""))
    ok_url, site_host, _err = _safe_https_parsed(parsed_base)
    if not ok_url or not _host_resolves_public(site_host):
        raise ReviewError("LiveFetchFailed")

    target_url = urllib.parse.urlunsplit(("https", parsed_base.netloc, clean_path, "", ""))
    allowed_hosts = _allowed_site_hosts(site_host)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _AllowlistedSiteRedirectHandler(allowed_hosts),
    )
    req = urllib.request.Request(
        target_url,
        method="GET",
        headers={
            "User-Agent": _PREVIEW_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with opener.open(req, timeout=float(timeout)) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            final_url = str(resp.geturl() or target_url)
            final_parsed = urllib.parse.urlparse(final_url)
            ok_final, final_host, _ferr = _safe_https_parsed(final_parsed)
            if not ok_final or final_host not in allowed_hosts or _blocked_site_path_reason(final_parsed):
                raise ReviewError("LiveFetchFailed")
            raw = resp.read(max_bytes + 1)
    except ReviewError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, ValueError) as exc:
        raise ReviewError("LiveFetchFailed") from exc

    if len(raw) > max_bytes:
        raise ReviewError("HtmlTooLarge")
    html = raw.decode("utf-8", errors="replace")
    return {
        "status": status,
        "final_url": final_url,
        "html": html,
        "bytes": len(raw),
    }


def _compute_focus_clip(
    abs_top: float,
    rect_height: float,
    scroll_height: float,
    viewport_height: float,
    *,
    is_mobile: bool,
    target_kind: str,
) -> tuple[float, float]:
    """Compute a Telegram-friendly (clip_y, clip_height) window around a focused target."""
    scroll_h = max(float(viewport_height), float(scroll_height or 0.0))
    max_h = 1000.0 if is_mobile else 900.0
    if target_kind == "section:bottom":
        clip_h = min(max_h, max(240.0, scroll_h))
        clip_y = max(0.0, scroll_h - clip_h)
        return round(clip_y, 2), round(clip_h, 2)
    if target_kind == "section:top":
        clip_h = min(max_h, max(240.0, scroll_h))
        return 0.0, round(clip_h, 2)
    if target_kind.startswith("section:"):
        desired_h = min(max_h, max(560.0 if not is_mobile else 680.0, float(rect_height or 0.0) + 80.0))
        clip_y = max(0.0, float(abs_top or 0.0) - 40.0)
        if scroll_h > desired_h and clip_y + desired_h > scroll_h:
            clip_y = max(0.0, scroll_h - desired_h)
        clip_h = min(desired_h, max(240.0, scroll_h - clip_y))
        return round(clip_y, 2), round(clip_h, 2)

    margin_top = 180.0 if is_mobile else 200.0
    desired_h = 844.0 if is_mobile else 760.0
    if float(rect_height or 0.0) > desired_h - 120.0:
        desired_h = min(max_h, float(rect_height or 0.0) + 120.0)
    clip_y = max(0.0, float(abs_top or 0.0) - margin_top)
    if scroll_h > desired_h and clip_y + desired_h > scroll_h:
        clip_y = max(0.0, scroll_h - desired_h)
    clip_h = min(desired_h, max(240.0, scroll_h - clip_y))
    return round(clip_y, 2), round(clip_h, 2)


def _order_screenshots(names: list[str]) -> list[str]:
    seen = set(names)
    ordered = [n for n in _PREFERRED_SHOT_ORDER if n in seen]
    for n in names:
        if n not in ordered:
            ordered.append(n)
    return ordered


def _focus_and_highlight_script(
    *,
    focus_selector: str | None = None,
    focus_text: str | None = None,
    highlight_token: str | None = None,
    focus_section: str | None = None,
) -> str:
    sel_js = json.dumps(focus_selector or "")
    text_js = json.dumps(focus_text or "")
    hi_js = json.dumps(highlight_token or "")
    sec_js = json.dumps(focus_section or "")
    return (
        "(() => {"
        f"  const focusSelector = {sel_js};"
        f"  const focusText = {text_js};"
        f"  const highlightToken = {hi_js};"
        f"  const focusSection = {sec_js};"
        "  if (!document.body) return JSON.stringify({found: false, highlightFound: false});"
        "  const scrollH = Math.max("
        "    document.documentElement ? document.documentElement.scrollHeight : 0,"
        "    document.body ? document.body.scrollHeight : 0"
        "  );"
        "  const vh = window.innerHeight || 800;"
        "  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();"
        "  const isVisible = (el) => {"
        "    if (!el || el.nodeType !== 1) return false;"
        "    const cs = window.getComputedStyle(el);"
        "    if (cs.display === 'none' || cs.visibility === 'hidden') return false;"
        "    const r = el.getBoundingClientRect();"
        "    return (r.width > 0 || r.height > 0);"
        "  };"
        "  const findBySelector = (sel) => {"
        "    if (!sel) return null;"
        "    try {"
        "      const list = Array.from(document.querySelectorAll(sel));"
        "      return list.find(isVisible) || list[0] || null;"
        "    } catch (e) { return null; }"
        "  };"
        "  const findByText = (raw) => {"
        "    const needle = norm(raw);"
        "    if (!needle) return null;"
        "    const skipTags = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);"
        "    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);"
        "    let node;"
        "    while ((node = walker.nextNode())) {"
        "      const parent = node.parentElement;"
        "      if (!parent || skipTags.has(parent.tagName)) continue;"
        "      if (norm(node.nodeValue).includes(needle) && isVisible(parent)) {"
        "        return parent.closest('a, p, span, div, address, li, h1, h2, h3, h4, h5, h6, button, section, footer, header') || parent;"
        "      }"
        "    }"
        "    const candidates = Array.from(document.body.querySelectorAll("
        "      'a, p, span, div, address, li, td, th, h1, h2, h3, h4, h5, h6, button, label, section, footer, header, article'"
        "    ));"
        "    let best = null;"
        "    let bestLen = Infinity;"
        "    for (const el of candidates) {"
        "      if (!isVisible(el)) continue;"
        "      const txt = norm(el.innerText || el.textContent);"
        "      if (txt && txt.includes(needle) && txt.length < bestLen) {"
        "        best = el;"
        "        bestLen = txt.length;"
        "      }"
        "    }"
        "    return best;"
        "  };"
        "  const findBySection = (sec) => {"
        "    if (!sec) return null;"
        "    if (sec === 'top') return {el: null, absTop: 0, rectHeight: vh, kind: 'section:top'};"
        "    if (sec === 'bottom') return {el: null, absTop: Math.max(0, scrollH - vh), rectHeight: vh, kind: 'section:bottom'};"
        "    if (sec === 'footer') {"
        "      const sem = Array.from(document.querySelectorAll('footer, [role=\"contentinfo\"]')).filter((e) => {"
        "        if (!isVisible(e)) return false;"
        "        const r = e.getBoundingClientRect();"
        "        return r.height >= 40 && r.width >= 100;"
        "      });"
        "      if (sem.length) {"
        "        const pick = sem[sem.length - 1];"
        "        const r = pick.getBoundingClientRect();"
        "        return {el: pick, absTop: Math.max(0, r.top + window.scrollY), rectHeight: r.height, kind: 'section:footer'};"
        "      }"
        "      const fallback = Array.from(document.querySelectorAll('[class*=\"footer\"], [id*=\"footer\"]')).filter((e) => {"
        "        if (!isVisible(e)) return false;"
        "        const r = e.getBoundingClientRect();"
        "        return r.height >= 80 && r.width >= 200;"
        "      });"
        "      if (fallback.length) {"
        "        fallback.sort((a, b) => {"
        "          const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();"
        "          return (rb.top + rb.height * 0.5) - (ra.top + ra.height * 0.5);"
        "        });"
        "        const pick = fallback[0];"
        "        const r = pick.getBoundingClientRect();"
        "        return {el: pick, absTop: Math.max(0, r.top + window.scrollY), rectHeight: r.height, kind: 'section:footer'};"
        "      }"
        "      return {el: null, absTop: Math.max(0, scrollH - vh), rectHeight: vh, kind: 'section:bottom'};"
        "    }"
        "    if (sec === 'header') {"
        "      const sem = Array.from(document.querySelectorAll('header, [role=\"banner\"], nav, [class*=\"header\"], [class*=\"nav\"], [id*=\"header\"]')).filter((e) => {"
        "        if (!isVisible(e)) return false;"
        "        const r = e.getBoundingClientRect();"
        "        return r.height >= 30 && r.width >= 100 && (r.top + window.scrollY) < 600;"
        "      });"
        "      if (sem.length) {"
        "        const pick = sem[0];"
        "        const r = pick.getBoundingClientRect();"
        "        return {el: pick, absTop: Math.max(0, r.top + window.scrollY), rectHeight: r.height, kind: 'section:header'};"
        "      }"
        "      return {el: null, absTop: 0, rectHeight: vh, kind: 'section:top'};"
        "    }"
        "    if (sec === 'hero') {"
        "      const cand = Array.from(document.querySelectorAll('[class*=\"hero\"], [id*=\"hero\"], main > section:first-of-type, section:first-of-type, h1')).filter((e) => {"
        "        if (!isVisible(e)) return false;"
        "        const r = e.getBoundingClientRect();"
        "        return (r.height >= 40) && (r.top + window.scrollY) < 1600;"
        "      });"
        "      if (cand.length) {"
        "        let pick = cand[0];"
        "        if (pick.tagName === 'H1') {"
        "          const secWrap = pick.closest('section, [class*=\"hero\"], div');"
        "          if (secWrap && secWrap.getBoundingClientRect().height <= 900) pick = secWrap;"
        "        }"
        "        const r = pick.getBoundingClientRect();"
        "        return {el: pick, absTop: Math.max(0, r.top + window.scrollY), rectHeight: r.height, kind: 'section:hero'};"
        "      }"
        "      return {el: null, absTop: 0, rectHeight: vh, kind: 'section:top'};"
        "    }"
        "    if (sec === 'main') {"
        "      const cand = Array.from(document.querySelectorAll('main, [role=\"main\"], #main, #content, .site-main, .main, section')).filter((e) => {"
        "        if (!isVisible(e)) return false;"
        "        const r = e.getBoundingClientRect();"
        "        return r.height >= 80 && r.width >= 200;"
        "      });"
        "      if (cand.length) {"
        "        const pick = cand[0];"
        "        const r = pick.getBoundingClientRect();"
        "        return {el: pick, absTop: Math.max(0, r.top + window.scrollY), rectHeight: r.height, kind: 'section:main'};"
        "      }"
        "      return {el: null, absTop: 0, rectHeight: vh, kind: 'section:top'};"
        "    }"
        "    return null;"
        "  };"
        "  let hiEl = null;"
        "  let highlightFound = false;"
        "  if (highlightToken) {"
        "    hiEl = findByText(highlightToken);"
        "    if (hiEl) {"
        "      highlightFound = true;"
        "      hiEl.style.setProperty('outline', '3px solid #22c55e', 'important');"
        "      hiEl.style.setProperty('outline-offset', '4px', 'important');"
        "      hiEl.style.setProperty('background-color', 'rgba(34, 197, 94, 0.16)', 'important');"
        "      hiEl.style.setProperty('box-shadow', '0 0 0 6px rgba(34, 197, 94, 0.22)', 'important');"
        "      hiEl.style.setProperty('border-radius', '4px', 'important');"
        "    }"
        "  }"
        "  let chosenEl = null;"
        "  let targetLabel = null;"
        "  let targetKind = '';"
        "  let absTop = 0;"
        "  let rectHeight = 0;"
        "  if (focusSelector) {"
        "    chosenEl = findBySelector(focusSelector);"
        "    if (chosenEl) { targetLabel = 'selector:' + focusSelector; targetKind = 'selector'; }"
        "  }"
        "  if (!targetLabel && focusText) {"
        "    chosenEl = findByText(focusText);"
        "    if (chosenEl) { targetLabel = 'text:' + focusText; targetKind = 'text'; }"
        "  }"
        "  if (!targetLabel && hiEl) {"
        "    chosenEl = hiEl; targetLabel = 'highlight:' + highlightToken; targetKind = 'highlight';"
        "  }"
        "  if (!targetLabel && focusSection) {"
        "    const secMatch = findBySection(focusSection);"
        "    if (secMatch) {"
        "      chosenEl = secMatch.el;"
        "      absTop = secMatch.absTop;"
        "      rectHeight = secMatch.rectHeight;"
        "      targetLabel = 'section:' + focusSection;"
        "      targetKind = secMatch.kind;"
        "    }"
        "  }"
        "  if (!targetLabel) {"
        "    return JSON.stringify({found: false, highlightFound, scrollHeight: scrollH});"
        "  }"
        "  if (chosenEl) {"
        "    const r0 = chosenEl.getBoundingClientRect();"
        "    absTop = Math.max(0, r0.top + window.scrollY);"
        "    rectHeight = r0.height || 0;"
        "  }"
        "  const targetScrollY = Math.max(0, Math.min(Math.max(0, scrollH - vh), absTop - Math.round(vh * 0.25)));"
        "  for (const frac of [0.35, 0.7, 1.0]) {"
        "    window.scrollTo(0, Math.round(targetScrollY * frac));"
        "    window.dispatchEvent(new Event('scroll'));"
        "  }"
        "  if (chosenEl) {"
        "    const blockMode = rectHeight > vh * 0.85 ? 'start' : 'center';"
        "    chosenEl.scrollIntoView({block: blockMode, behavior: 'instant'});"
        "    window.dispatchEvent(new Event('scroll'));"
        "    const r1 = chosenEl.getBoundingClientRect();"
        "    absTop = Math.max(0, r1.top + window.scrollY);"
        "    rectHeight = r1.height || rectHeight;"
        "  }"
        "  const imgs = chosenEl ? chosenEl.querySelectorAll('img[loading=\"lazy\"]') : document.querySelectorAll('img[loading=\"lazy\"]');"
        "  imgs.forEach((img) => { try { img.loading = 'eager'; } catch (e) {} });"
        "  return JSON.stringify({"
        "    found: true,"
        "    highlightFound,"
        "    target: targetLabel,"
        "    targetKind,"
        "    absTop,"
        "    rectHeight,"
        "    scrollHeight: Math.max("
        "      document.documentElement ? document.documentElement.scrollHeight : 0,"
        "      document.body ? document.body.scrollHeight : 0"
        "    )"
        "  });"
        "})()"
    )


def _reveal_region_script(clip_y: float, clip_h: float, *, full_page: bool = False) -> str:
    return (
        "(() => {"
        f"  const clipTop = {float(clip_y)};"
        f"  const clipBottom = {float(clip_y + clip_h)};"
        f"  const fullPage = {'true' if full_page else 'false'};"
        "  if (!document.body) return JSON.stringify({revealed: 0, scrollHeight: 0});"
        "  const skipCls = /\\b(hide|hidden|sr-only|screen-reader|modal|popup|dropdown|tooltip|w-dropdown-list|w-nav-overlay)\\b/i;"
        "  let revealed = 0;"
        "  for (const el of document.body.querySelectorAll('*')) {"
        "    if (el.tagName === 'IMG' && el.loading === 'lazy') {"
        "      try { el.loading = 'eager'; } catch (e) {}"
        "    }"
        "    const cls = typeof el.className === 'string' ? el.className : '';"
        "    if (skipCls.test(cls)) continue;"
        "    const cs = window.getComputedStyle(el);"
        "    if (cs.display === 'none' || cs.visibility === 'hidden') continue;"
        "    const r = el.getBoundingClientRect();"
        "    if (r.width <= 0 || r.height <= 0) continue;"
        "    const elTop = r.top + window.scrollY;"
        "    const elBottom = r.bottom + window.scrollY;"
        "    if (!fullPage && (elBottom < clipTop - 120 || elTop > clipBottom + 120)) continue;"
        "    const op = parseFloat(cs.opacity);"
        "    const isScrollReveal = el.hasAttribute('data-w-id') || el.hasAttribute('data-aos') || "
        "      el.style.opacity !== '' || el.closest('[data-w-id], [data-aos], .wow, footer') !== null;"
        "    if (!Number.isNaN(op) && op < 0.98 && isScrollReveal) {"
        "      el.style.setProperty('transition', 'none', 'important');"
        "      el.style.setProperty('opacity', '1', 'important');"
        "      if (el.style.transform && /translate/i.test(el.style.transform)) {"
        "        el.style.setProperty('transform', 'none', 'important');"
        "      }"
        "      revealed += 1;"
        "    } else if (el.hasAttribute('data-w-id') && el.style.transform && /translate3d\\([^)]*(?:-[1-9]|[1-9])/i.test(el.style.transform)) {"
        "      el.style.setProperty('transition', 'none', 'important');"
        "      el.style.setProperty('transform', 'none', 'important');"
        "      revealed += 1;"
        "    }"
        "  }"
        "  return JSON.stringify({"
        "    revealed,"
        "    scrollHeight: Math.max("
        "      document.documentElement ? document.documentElement.scrollHeight : 0,"
        "      document.body ? document.body.scrollHeight : 0"
        "    )"
        "  });"
        "})()"
    )


def _scroll_sweep_script(max_height: int) -> str:
    return (
        "(() => {"
        f"  const maxH = {int(max_height)};"
        "  const scrollH = Math.min(maxH, Math.max("
        "    document.documentElement ? document.documentElement.scrollHeight : 0,"
        "    document.body ? document.body.scrollHeight : 0"
        "  ));"
        "  for (const frac of [0.25, 0.5, 0.75, 1.0]) {"
        "    window.scrollTo(0, Math.round(scrollH * frac));"
        "    window.dispatchEvent(new Event('scroll'));"
        "  }"
        "  document.querySelectorAll('img[loading=\"lazy\"]').forEach((img) => { try { img.loading = 'eager'; } catch (e) {} });"
        "  return JSON.stringify({scrollHeight: scrollH});"
        "})()"
    )


def _highlight_script(token: str) -> str:
    return _focus_and_highlight_script(highlight_token=token)


def _dom_probe_script() -> str:
    return (
        "(() => {"
        "  const body = document.body;"
        "  const style = body ? window.getComputedStyle(body) : null;"
        "  return JSON.stringify({"
        "    readyState: document.readyState,"
        "    title: document.title || '',"
        "    bodyText: body ? (body.innerText || '').trim().slice(0, 1500) : '',"
        "    outerLen: document.documentElement ? document.documentElement.outerHTML.length : 0,"
        "    bodyBg: style ? style.backgroundColor : ''"
        "  });"
        "})()"
    )


def _finalize_summary(site: Site, summary: dict[str, Any], artifact_dir: Path) -> dict[str, Any]:
    try:
        secret = bind_credential(site).value
    except Exception:
        secret = ""
    clean = scrub_secrets(summary, [secret]) if secret else summary
    assert isinstance(clean, dict)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "preview.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def _safe_js_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _strip_sandbox_trackers(html: str) -> str:
    def _drop_if_tracker(match: re.Match[str]) -> str:
        low = match.group(0).lower()
        if any(tok in low for tok in _SANDBOX_TRACKER_TOKENS):
            return ""
        return match.group(0)

    cleaned = _SCRIPT_TAG_RE.sub(_drop_if_tracker, html)
    cleaned = _NOSCRIPT_TAG_RE.sub(_drop_if_tracker, cleaned)
    return cleaned


def _sandbox_base_href(site_canonical_url: str) -> str:
    parsed = urllib.parse.urlparse(str(site_canonical_url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.strip().lower().rstrip(".")
    if not host or any(ord(ch) < 32 or ch in "<>\"' \t\r\n" for ch in host):
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port not in (None, 443):
        return ""
    return f"https://{host}/"


def build_interactive_sandbox_html(
    overlay_html: str,
    *,
    site_canonical_url: str,
    site_id: str,
    plan_id: str,
    path: str,
    highlight_token: str | None = None,
    focus_text: str | None = None,
) -> str:
    """Build a standalone, read-only interactive sandbox HTML page for browser review."""
    html = _strip_sandbox_trackers(str(overlay_html or ""))
    html = _BASE_TAG_RE.sub("", html)

    base_href = _sandbox_base_href(site_canonical_url)
    base_tag = f'<base href="{html_lib.escape(base_href, quote=True)}">' if base_href else ""

    toolbar_style = (
        '<style data-azom-sandbox="style">'
        "html { scroll-padding-top: 56px; }"
        "body { padding-top: 48px !important; }"
        "#azom-sandbox-toolbar {"
        "  position: fixed; top: 0; left: 0; right: 0; z-index: 2147483640;"
        "  display: flex; align-items: center; justify-content: space-between; gap: 12px;"
        "  min-height: 44px; padding: 6px 14px; box-sizing: border-box;"
        "  background: #0f172a; color: #f8fafc; border-bottom: 2px solid #22c55e;"
        "  font: 600 13px/1.35 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;"
        "  box-shadow: 0 2px 10px rgba(0,0,0,0.35);"
        "}"
        "#azom-sandbox-toolbar .az-sb-meta {"
        "  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0;"
        "}"
        "#azom-sandbox-toolbar .az-sb-pill {"
        "  display: inline-block; padding: 2px 8px; border-radius: 999px;"
        "  background: #16a34a; color: #fff; font-size: 11px; letter-spacing: 0.04em;"
        "}"
        "#azom-sandbox-jump {"
        "  appearance: none; border: 1px solid #22c55e; background: #14532d; color: #ecfdf5;"
        "  border-radius: 6px; padding: 5px 11px; font: 600 12px/1.2 inherit; cursor: pointer;"
        "  white-space: nowrap;"
        "}"
        "#azom-sandbox-jump:hover { background: #166534; }"
        "#azom-sandbox-toast {"
        "  position: fixed; right: 16px; bottom: 16px; z-index: 2147483645;"
        "  max-width: 420px; padding: 10px 14px; border-radius: 8px;"
        "  background: #0f172a; color: #fef08a; border: 1px solid #eab308;"
        "  font: 600 13px/1.35 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;"
        "  box-shadow: 0 6px 24px rgba(0,0,0,0.4); opacity: 0; pointer-events: none;"
        "  transition: opacity 160ms ease;"
        "}"
        "#azom-sandbox-toast.az-visible { opacity: 1; }"
        "</style>"
    )

    head_inject = "\n".join(part for part in (base_tag, toolbar_style) if part)
    head_open = _HEAD_OPEN_RE.search(html)
    if head_open is not None:
        html = html[: head_open.end()] + "\n" + head_inject + "\n" + html[head_open.end() :]
    else:
        html_open = _HTML_OPEN_TAG_RE.search(html)
        if html_open is not None:
            html = html[: html_open.end()] + f"\n<head>\n{head_inject}\n</head>\n" + html[html_open.end() :]
        else:
            html = f"<head>\n{head_inject}\n</head>\n" + html

    safe_site = html_lib.escape(str(site_id or ""), quote=True)
    safe_plan = html_lib.escape(str(plan_id or ""), quote=True)
    safe_path = html_lib.escape(str(path or "/"), quote=True)
    banner_text = (
        f"AZOM SANDBOX · {safe_plan} · {safe_path} · "
        "Skrivskyddad förhandsvisning — inga ändringar på live"
    )
    toolbar_html = (
        f'<div id="azom-sandbox-toolbar" data-azom-sandbox-toolbar="1" data-site-id="{safe_site}" '
        f'data-plan-id="{safe_plan}" role="region" aria-label="Azom sandbox-förhandsvisning">'
        f'<div class="az-sb-meta"><span class="az-sb-pill">SANDBOX</span>'
        f"<span>{banner_text}</span></div>"
        f'<button type="button" id="azom-sandbox-jump">Hoppa till ändringen</button>'
        "</div>"
        '<div id="azom-sandbox-toast" role="status" aria-live="polite"></div>'
    )

    hi_js = _safe_js_json(highlight_token or "")
    focus_js = _safe_js_json(focus_text or "")
    guard_script = (
        '<script data-azom-sandbox="guard">\n'
        "(function(){\n"
        f"  var highlightToken = {hi_js};\n"
        f"  var focusText = {focus_js};\n"
        "  var BLOCKED_MSG = 'Blockerad i sandbox-läget — inga ändringar skickas till live';\n"
        "  var BLOCKED_PATHS = ['wp-admin','wp-login','admin-ajax','wc-ajax','/wp-json','rest_route',"
        "'xmlrpc','wp-cron','add-to-cart','/cart','/checkout','/my-account','/handlekurv','/kasse','/min-konto'];\n"
        "  var toastTimer = null;\n"
        "  function showToast(msg){\n"
        "    var el = document.getElementById('azom-sandbox-toast');\n"
        "    if(!el) return;\n"
        "    el.textContent = msg || BLOCKED_MSG;\n"
        "    el.classList.add('az-visible');\n"
        "    if(toastTimer) clearTimeout(toastTimer);\n"
        "    toastTimer = setTimeout(function(){ el.classList.remove('az-visible'); }, 2800);\n"
        "  }\n"
        "  function isBlockedUrl(u){\n"
        "    var s = String(u || '').toLowerCase();\n"
        "    for(var i=0;i<BLOCKED_PATHS.length;i++){\n"
        "      if(s.indexOf(BLOCKED_PATHS[i]) !== -1) return true;\n"
        "    }\n"
        "    return false;\n"
        "  }\n"
        "  document.addEventListener('submit', function(e){\n"
        "    e.preventDefault(); e.stopPropagation(); showToast(BLOCKED_MSG);\n"
        "  }, true);\n"
        "  document.addEventListener('click', function(e){\n"
        "    var t = e.target;\n"
        "    if(!t || !t.closest) return;\n"
        "    if(t.closest('#azom-sandbox-toolbar')) return;\n"
        "    var link = t.closest('a[href]');\n"
        "    if(link && isBlockedUrl(link.getAttribute('href') || link.href)){\n"
        "      e.preventDefault(); e.stopPropagation(); showToast(BLOCKED_MSG); return;\n"
        "    }\n"
        "    var btn = t.closest('button[type=\"submit\"], input[type=\"submit\"], .single_add_to_cart_button, .add_to_cart_button');\n"
        "    if(btn){\n"
        "      e.preventDefault(); e.stopPropagation(); showToast(BLOCKED_MSG);\n"
        "    }\n"
        "  }, true);\n"
        "  if(typeof window.fetch === 'function'){\n"
        "    var origFetch = window.fetch.bind(window);\n"
        "    window.fetch = function(input, init){\n"
        "      var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();\n"
        "      var url = typeof input === 'string' ? input : (input && input.url ? input.url : '');\n"
        "      if((method !== 'GET' && method !== 'HEAD') || isBlockedUrl(url)){\n"
        "        showToast(BLOCKED_MSG);\n"
        "        return Promise.reject(new Error('Blocked in Azom sandbox preview'));\n"
        "      }\n"
        "      return origFetch(input, init);\n"
        "    };\n"
        "  }\n"
        "  if(typeof window.XMLHttpRequest === 'function'){\n"
        "    var origOpen = XMLHttpRequest.prototype.open;\n"
        "    var origSend = XMLHttpRequest.prototype.send;\n"
        "    XMLHttpRequest.prototype.open = function(method, url){\n"
        "      var m = String(method || 'GET').toUpperCase();\n"
        "      this.__azomBlocked = ((m !== 'GET' && m !== 'HEAD') || isBlockedUrl(url));\n"
        "      if(this.__azomBlocked) return;\n"
        "      return origOpen.apply(this, arguments);\n"
        "    };\n"
        "    XMLHttpRequest.prototype.send = function(){\n"
        "      if(this.__azomBlocked){ showToast(BLOCKED_MSG); return; }\n"
        "      return origSend.apply(this, arguments);\n"
        "    };\n"
        "  }\n"
        "  if(navigator && typeof navigator.sendBeacon === 'function'){\n"
        "    navigator.sendBeacon = function(){ showToast(BLOCKED_MSG); return false; };\n"
        "  }\n"
        "  function norm(s){ return String(s || '').replace(/\\s+/g, ' ').trim().toLowerCase(); }\n"
        "  function isVisible(el){\n"
        "    if(!el || el.nodeType !== 1) return false;\n"
        "    if(el.closest && el.closest('#azom-sandbox-toolbar, #azom-sandbox-toast')) return false;\n"
        "    var cs = window.getComputedStyle(el);\n"
        "    if(cs.display === 'none' || cs.visibility === 'hidden') return false;\n"
        "    var r = el.getBoundingClientRect();\n"
        "    return (r.width > 0 || r.height > 0);\n"
        "  }\n"
        "  function findByText(raw){\n"
        "    var needle = norm(raw);\n"
        "    if(!needle || !document.body) return null;\n"
        "    var skip = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEMPLATE:1};\n"
        "    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);\n"
        "    var node;\n"
        "    while((node = walker.nextNode())){\n"
        "      var p = node.parentElement;\n"
        "      if(!p || skip[p.tagName]) continue;\n"
        "      if(p.closest && p.closest('#azom-sandbox-toolbar, #azom-sandbox-toast')) continue;\n"
        "      if(norm(node.nodeValue).indexOf(needle) !== -1 && isVisible(p)){\n"
        "        return p.closest('h1, h2, h3, p, span, a, button, div, li, section, footer, header') || p;\n"
        "      }\n"
        "    }\n"
        "    var list = document.body.querySelectorAll('h1, h2, h3, p, span, a, button, div, li, section, article');\n"
        "    var best = null, bestLen = Infinity;\n"
        "    for(var i=0;i<list.length;i++){\n"
        "      var el = list[i];\n"
        "      if(!isVisible(el)) continue;\n"
        "      var txt = norm(el.innerText || el.textContent);\n"
        "      if(txt && txt.indexOf(needle) !== -1 && txt.length < bestLen){\n"
        "        best = el; bestLen = txt.length;\n"
        "      }\n"
        "    }\n"
        "    return best;\n"
        "  }\n"
        "  function revealScrollElements(){\n"
        "    if(!document.body) return;\n"
        "    var skipCls = /\\b(hide|hidden|sr-only|screen-reader|modal|popup|dropdown|tooltip|w-dropdown-list|w-nav-overlay)\\b/i;\n"
        "    var all = document.body.querySelectorAll('*');\n"
        "    for(var i=0;i<all.length;i++){\n"
        "      var el = all[i];\n"
        "      if(el.tagName === 'IMG' && el.loading === 'lazy'){ try { el.loading = 'eager'; } catch(e){} }\n"
        "      var cls = typeof el.className === 'string' ? el.className : '';\n"
        "      if(skipCls.test(cls)) continue;\n"
        "      var cs = window.getComputedStyle(el);\n"
        "      if(cs.display === 'none' || cs.visibility === 'hidden') continue;\n"
        "      var op = parseFloat(cs.opacity);\n"
        "      var isReveal = el.hasAttribute('data-w-id') || el.hasAttribute('data-aos') || "
        "el.style.opacity !== '' || (el.closest && el.closest('[data-w-id], [data-aos], .wow, footer') !== null);\n"
        "      if(!isNaN(op) && op < 0.98 && isReveal){\n"
        "        el.style.setProperty('opacity', '1', 'important');\n"
        "        if(el.style.transform && /translate/i.test(el.style.transform)){\n"
        "          el.style.setProperty('transform', 'none', 'important');\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "  var targetEl = null;\n"
        "  function applyHighlight(){\n"
        "    targetEl = (highlightToken && findByText(highlightToken)) || (focusText && findByText(focusText)) || null;\n"
        "    if(targetEl){\n"
        "      targetEl.style.setProperty('outline', '3px solid #22c55e', 'important');\n"
        "      targetEl.style.setProperty('outline-offset', '4px', 'important');\n"
        "      targetEl.style.setProperty('background-color', 'rgba(34, 197, 94, 0.16)', 'important');\n"
        "      targetEl.style.setProperty('box-shadow', '0 0 0 6px rgba(34, 197, 94, 0.22)', 'important');\n"
        "      targetEl.style.setProperty('border-radius', '4px', 'important');\n"
        "    }\n"
        "    return targetEl;\n"
        "  }\n"
        "  function initSandbox(){\n"
        "    revealScrollElements();\n"
        "    applyHighlight();\n"
        "    var jumpBtn = document.getElementById('azom-sandbox-jump');\n"
        "    if(jumpBtn){\n"
        "      jumpBtn.addEventListener('click', function(){\n"
        "        var el = targetEl || applyHighlight();\n"
        "        if(el && el.scrollIntoView){\n"
        "          el.scrollIntoView({behavior: 'smooth', block: 'center'});\n"
        "        } else {\n"
        "          showToast('Ingen specifik ändring kunde markeras på sidan.');\n"
        "        }\n"
        "      });\n"
        "    }\n"
        "  }\n"
        "  if(document.readyState === 'loading'){\n"
        "    document.addEventListener('DOMContentLoaded', initSandbox);\n"
        "  } else {\n"
        "    initSandbox();\n"
        "  }\n"
        "  window.addEventListener('load', function(){ revealScrollElements(); applyHighlight(); });\n"
        "})();\n"
        "</script>"
    )

    body_open = _BODY_OPEN_TAG_RE.search(html)
    if body_open is not None:
        html = html[: body_open.end()] + "\n" + toolbar_html + "\n" + html[body_open.end() :]
    else:
        html = toolbar_html + "\n" + html

    body_close = _BODY_CLOSE_TAG_RE.search(html)
    if body_close is not None:
        html = html[: body_close.start()] + "\n" + guard_script + "\n" + html[body_close.start() :]
    else:
        html = html + "\n" + guard_script

    return html


def capture_preview(
    site: Site,
    *,
    path: str = "/",
    artifact_dir: Path,
    revisions: list[tuple[str, str]],
    highlight_token: str | None = None,
    focus_section: str | None = None,
    focus_selector: str | None = None,
    focus_text: str | None = None,
    full_page: bool = False,
    browser_binary: str | None = None,
    timeout_s: float = 45.0,
    target_url: str | None = None,
) -> dict[str, Any]:
    """Render live / overlay HTML revisions in headless Chrome with read-only Fetch interception."""
    clean_path = validate_preview_path(path)
    clean_section, clean_selector, clean_text, clean_full_page = validate_focus_options(
        focus_section=focus_section,
        focus_selector=focus_selector,
        focus_text=focus_text,
        full_page=full_page,
    )
    parsed_base = urllib.parse.urlparse(str(site.canonical_url or ""))
    ok_base, site_host, _berr = _safe_https_parsed(parsed_base)
    if not ok_base:
        raise ReviewError("LiveFetchFailed")

    resolved_target = target_url or urllib.parse.urlunsplit(("https", parsed_base.netloc, clean_path, "", ""))
    target_parsed = urllib.parse.urlparse(resolved_target)
    ok_target, target_host, _terr = _safe_https_parsed(target_parsed)
    if not ok_target or target_host not in _allowed_site_hosts(site_host) or _blocked_site_path_reason(target_parsed):
        raise ReviewError("PreviewPathRefused")
    if target_parsed.query or target_parsed.fragment:
        raise ReviewError("PreviewPathRefused")
    binary = browser_binary or find_browser_binary()
    if not binary:
        raise ReviewError("BrowserUnavailable")

    allowed_labels = {"live", "preview"}
    clean_revisions: list[tuple[str, str]] = []
    for lbl, html in revisions:
        if lbl not in allowed_labels:
            raise ReviewError("InvalidPreviewRevision")
        clean_revisions.append((lbl, str(html or "")))
    if not clean_revisions:
        raise ReviewError("InvalidPreviewRevision")

    has_preview_revision = any(lbl == "preview" for lbl, _ in clean_revisions)
    has_explicit_focus = bool(clean_section or clean_selector or clean_text)

    artifact_dir = Path(artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cdp_port = _pick_loopback_port()
    env = build_php_child_env()
    user_data_dir = Path(tempfile.mkdtemp(prefix="azom-wp-live-preview-"))
    command = [
        binary,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--no-pings",
        "--disable-domain-reliability",
        "--disable-client-side-phishing-detection",
        "--disable-features=Translate,OptimizationHints,MediaRouter,AutofillServerCommunication",
        "--metrics-recording-only",
        "--mute-audio",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "about:blank",
    ]

    proc: subprocess.Popen[bytes] | None = None
    cdp: _LoopbackCdpClient | None = None
    started_at = time.monotonic()
    allowed_count = 0
    blocked_counts: dict[str, int] = {}
    blocked_sample: list[dict[str, str]] = []
    revisions_meta: dict[str, Any] = {}
    screenshots: list[str] = []
    any_focus_matched = False
    matched_focus_target: str | None = None

    try:
        proc = subprocess.Popen(
            command,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        ws_url = _wait_cdp_page_ws(cdp_port, proc, timeout_s=min(15.0, timeout_s))
        cdp = _LoopbackCdpClient(ws_url, timeout_s=min(20.0, timeout_s))

        current_html_b64 = ""
        main_doc_pending = False

        def _handle_cdp_event(ev: dict[str, Any]) -> bool:
            nonlocal allowed_count, main_doc_pending
            if ev.get("method") != "Fetch.requestPaused":
                return False
            params = ev.get("params") or {}
            req_id = str(params.get("requestId") or "")
            req = params.get("request") or {}
            req_url = str(req.get("url") or "")
            req_method = str(req.get("method") or "GET").upper()
            res_type = str(params.get("resourceType") or "")
            if (
                main_doc_pending
                and res_type == "Document"
                and req_method == "GET"
                and req_url.rstrip("/") == resolved_target.rstrip("/")
            ):
                main_doc_pending = False
                assert cdp is not None
                cdp.send(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": req_id,
                        "responseCode": 200,
                        "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
                        "body": current_html_b64,
                    },
                )
                allowed_count += 1
                return True

            ok, reason = classify_request(req_method, req_url, site_host=site_host)
            assert cdp is not None
            if ok:
                cdp.send("Fetch.continueRequest", {"requestId": req_id})
                allowed_count += 1
            else:
                cdp.send("Fetch.failRequest", {"requestId": req_id, "errorReason": "BlockedByClient"})
                blocked_counts[reason] = blocked_counts.get(reason, 0) + 1
                if len(blocked_sample) < 20:
                    blocked_sample.append({"url": req_url[:200], "reason": reason})
            return True

        cdp.event_hook = _handle_cdp_event
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable")
        cdp.call("Log.enable")
        cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

        for label, html_body in clean_revisions:
            current_html_b64 = base64.b64encode(html_body.encode("utf-8", errors="replace")).decode("ascii")
            viewports_out: dict[str, Any] = {}
            should_run_focus = (
                (label == "preview" and bool(highlight_token or has_explicit_focus))
                or (label == "live" and not has_preview_revision and has_explicit_focus)
            )
            for vp_name, width, height, is_mobile in PREVIEW_VIEWPORTS:
                if time.monotonic() - started_at > timeout_s:
                    break
                cdp.events.clear()
                cdp.call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": 1,
                        "mobile": is_mobile,
                    },
                )
                cdp.call("Network.setCacheDisabled", {"cacheDisabled": True})
                main_doc_pending = True
                cdp.send("Page.navigate", {"url": resolved_target})
                loaded = cdp.wait_for_event("Page.loadEventFired", timeout_s=min(20.0, timeout_s))
                cdp.drain_events(duration_s=1.2)

                eval_res = cdp.call(
                    "Runtime.evaluate",
                    {"expression": _dom_probe_script(), "returnByValue": True},
                )
                raw_dom = (eval_res.get("result") or {}).get("value") or "{}"
                try:
                    dom_info = json.loads(raw_dom) if isinstance(raw_dom, str) else {}
                except json.JSONDecodeError:
                    dom_info = {}

                if clean_full_page:
                    sweep_res = cdp.call(
                        "Runtime.evaluate",
                        {"expression": _scroll_sweep_script(_MAX_FULL_PAGE_HEIGHT), "returnByValue": True},
                    )
                    raw_sweep = (sweep_res.get("result") or {}).get("value") or "{}"
                    try:
                        sweep_info = json.loads(raw_sweep) if isinstance(raw_sweep, str) else {}
                    except json.JSONDecodeError:
                        sweep_info = {}
                    cdp.drain_events(duration_s=0.7)
                    cdp.call(
                        "Runtime.evaluate",
                        {
                            "expression": _reveal_region_script(0.0, float(_MAX_FULL_PAGE_HEIGHT), full_page=True),
                            "returnByValue": True,
                        },
                    )
                    cdp.call(
                        "Runtime.evaluate",
                        {"expression": "window.scrollTo(0, 0);", "returnByValue": True},
                    )
                    cdp.drain_events(duration_s=0.15)
                    full_h = min(
                        float(_MAX_FULL_PAGE_HEIGHT),
                        max(float(height), float(sweep_info.get("scrollHeight") or height)),
                    )
                    shot = cdp.call(
                        "Page.captureScreenshot",
                        {
                            "format": "png",
                            "captureBeyondViewport": True,
                            "clip": {
                                "x": 0,
                                "y": 0,
                                "width": width,
                                "height": full_h,
                                "scale": 1,
                            },
                        },
                    )
                else:
                    shot = cdp.call("Page.captureScreenshot", {"format": "png"})
                png_bytes = base64.b64decode(str(shot.get("data") or ""))
                dims = _png_dimensions(png_bytes)
                shot_name = f"{label}-{vp_name}.png"
                shot_path = artifact_dir / shot_name
                if png_bytes[:8] == _PNG_MAGIC:
                    shot_path.write_bytes(png_bytes)
                    screenshots.append(shot_name)

                highlight_found = False
                vp_focus_matched = False
                vp_focus_target: str | None = None
                change_shot_name: str | None = None
                focus_shot_name: str | None = None
                if should_run_focus:
                    hi_res = cdp.call(
                        "Runtime.evaluate",
                        {
                            "expression": _focus_and_highlight_script(
                                focus_selector=clean_selector,
                                focus_text=clean_text,
                                highlight_token=highlight_token if label == "preview" else None,
                                focus_section=clean_section,
                            ),
                            "returnByValue": True,
                        },
                    )
                    raw_hi = (hi_res.get("result") or {}).get("value") or "{}"
                    try:
                        hi_info = json.loads(raw_hi) if isinstance(raw_hi, str) else {}
                    except json.JSONDecodeError:
                        hi_info = {}
                    highlight_found = hi_info.get("highlightFound") is True
                    if hi_info.get("found") is True:
                        vp_focus_matched = True
                        vp_focus_target = str(hi_info.get("target") or "") or None
                        any_focus_matched = True
                        if matched_focus_target is None and vp_focus_target:
                            matched_focus_target = vp_focus_target
                        target_kind = str(hi_info.get("targetKind") or "")
                        abs_top = float(hi_info.get("absTop") or 0.0)
                        rect_h = float(hi_info.get("rectHeight") or 0.0)
                        scroll_h = float(hi_info.get("scrollHeight") or height)
                        clip_y, clip_h = _compute_focus_clip(
                            abs_top,
                            rect_h,
                            scroll_h,
                            float(height),
                            is_mobile=is_mobile,
                            target_kind=target_kind,
                        )
                        # Wait for scroll-triggered animations/lazy images to settle,
                        # then ensure scroll-reveal opacity is finalized in the capture region.
                        cdp.drain_events(duration_s=0.9)
                        reveal_res = cdp.call(
                            "Runtime.evaluate",
                            {
                                "expression": _reveal_region_script(clip_y, clip_h, full_page=False),
                                "returnByValue": True,
                            },
                        )
                        raw_reveal = (reveal_res.get("result") or {}).get("value") or "{}"
                        try:
                            reveal_info = json.loads(raw_reveal) if isinstance(raw_reveal, str) else {}
                        except json.JSONDecodeError:
                            reveal_info = {}
                        updated_scroll_h = float(reveal_info.get("scrollHeight") or scroll_h)
                        if updated_scroll_h > scroll_h:
                            clip_y, clip_h = _compute_focus_clip(
                                abs_top,
                                rect_h,
                                updated_scroll_h,
                                float(height),
                                is_mobile=is_mobile,
                                target_kind=target_kind,
                            )
                        # Park viewport at top before captureBeyondViewport clip when cropping
                        # below the fold so fixed headers never cover the focused region.
                        if clip_y > 200.0 and target_kind not in {"section:top", "section:header"}:
                            cdp.call(
                                "Runtime.evaluate",
                                {"expression": "window.scrollTo(0, 0);", "returnByValue": True},
                            )
                        cdp.drain_events(duration_s=0.15)
                        crop = cdp.call(
                            "Page.captureScreenshot",
                            {
                                "format": "png",
                                "captureBeyondViewport": True,
                                "clip": {
                                    "x": 0,
                                    "y": clip_y,
                                    "width": width,
                                    "height": clip_h,
                                    "scale": 1,
                                },
                            },
                        )
                        crop_bytes = base64.b64decode(str(crop.get("data") or ""))
                        if crop_bytes[:8] == _PNG_MAGIC:
                            if label == "preview" and highlight_found:
                                change_shot_name = f"change-{vp_name}.png"
                                (artifact_dir / change_shot_name).write_bytes(crop_bytes)
                                screenshots.append(change_shot_name)
                            else:
                                focus_shot_name = f"focus-{vp_name}.png"
                                (artifact_dir / focus_shot_name).write_bytes(crop_bytes)
                                screenshots.append(focus_shot_name)

                console_errors: list[str] = []
                network_errors: list[dict[str, Any]] = []
                doc_status: int | None = None
                request_urls: dict[str, str] = {}
                for ev in cdp.events:
                    method = ev.get("method")
                    params = ev.get("params") or {}
                    if method == "Network.requestWillBeSent":
                        req_id = str(params.get("requestId") or "")
                        req_url = str((params.get("request") or {}).get("url") or "")
                        if req_id:
                            request_urls[req_id] = req_url
                    elif method == "Network.responseReceived":
                        resp = params.get("response") or {}
                        sc = int(resp.get("status") or 0)
                        rtype = str(params.get("type") or "")
                        rurl = str(resp.get("url") or "")
                        if rtype == "Document" and doc_status is None:
                            doc_status = sc
                        if sc >= 400:
                            network_errors.append({"url": rurl[:200], "status": sc, "type": rtype})
                    elif method == "Network.loadingFailed":
                        req_id = str(params.get("requestId") or "")
                        req_url = request_urls.get(req_id, "")
                        if req_url and not req_url.endswith("/favicon.ico") and str(params.get("blockedReason") or "") != "inspector":
                            err_text = str(params.get("errorText") or "")
                            if err_text != "net::ERR_BLOCKED_BY_CLIENT":
                                network_errors.append({"url": req_url[:200], "error_text": err_text[:100]})
                    elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
                        parts = [str(a.get("value") or a.get("description") or "") for a in params.get("args") or []]
                        msg = " ".join(parts).strip()
                        if msg:
                            console_errors.append(msg[:240])
                    elif method == "Log.entryAdded":
                        entry = params.get("entry") or {}
                        if entry.get("level") == "error":
                            txt = str(entry.get("text") or "")
                            if "favicon.ico" not in txt and "ERR_BLOCKED_BY_CLIENT" not in txt:
                                console_errors.append(txt[:240])
                    elif method == "Runtime.exceptionThrown":
                        details = params.get("exceptionDetails") or {}
                        console_errors.append(str(details.get("text") or "Uncaught exception")[:240])

                body_text = str(dom_info.get("bodyText") or "")
                lower_body = body_text.lower()
                fatal_markers = [m for m in _WP_FATAL_MARKERS if m in lower_body]
                viewports_out[vp_name] = {
                    "loaded": loaded,
                    "http_status": doc_status or 200,
                    "title": str(dom_info.get("title") or "")[:200],
                    "screenshot": shot_name,
                    "screenshot_path": str(shot_path),
                    "screenshot_bytes": len(png_bytes),
                    "screenshot_dimensions": list(dims) if dims else None,
                    "change_screenshot": change_shot_name,
                    "change_screenshot_path": str(artifact_dir / change_shot_name) if change_shot_name else None,
                    "focus_screenshot": focus_shot_name,
                    "focus_screenshot_path": str(artifact_dir / focus_shot_name) if focus_shot_name else None,
                    "highlight_found": highlight_found,
                    "focus_matched": vp_focus_matched,
                    "focus_target": vp_focus_target,
                    "console_errors": console_errors[:10],
                    "network_errors": network_errors[:10],
                    "fatal_markers": fatal_markers,
                }
            revisions_meta[label] = viewports_out
    finally:
        if cdp is not None:
            cdp.close()
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
        shutil.rmtree(user_data_dir, ignore_errors=True)

    ordered_screenshots = _order_screenshots(screenshots)
    summary = {
        "preview_kind": "live_overlay" if len(clean_revisions) > 1 else "live_read_only",
        "site_id": site.id,
        "target_url": resolved_target,
        "path": clean_path,
        "allowed_requests": allowed_count,
        "blocked_counts": blocked_counts,
        "blocked_sample": blocked_sample,
        "highlight_token": highlight_token,
        "focus_section": clean_section,
        "focus_selector": clean_selector,
        "focus_text": clean_text,
        "full_page": clean_full_page,
        "focus_matched": any_focus_matched,
        "focus_target": matched_focus_target,
        "revisions": revisions_meta,
        "screenshots": ordered_screenshots,
    }
    return _finalize_summary(site, summary, artifact_dir)
