"""Interactive read-only sandbox HTML and canonical guard-script builder."""

from __future__ import annotations

import html as html_lib
import json
import re
import urllib.parse
from typing import Any

from ecom_ops.integrations.wp.http_guard import BLOCKED_WP_PATHS
from ecom_ops.wp_ops.html_sanitize import _neutralize_overlay_fragment

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
_NOSCRIPT_TAG_RE = re.compile(r"<noscript\b[^>]*>[\s\S]*?</noscript\s*>", re.IGNORECASE)
_BASE_TAG_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BODY_OPEN_TAG_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_BODY_CLOSE_TAG_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_HTML_OPEN_TAG_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)


def _safe_js_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _strip_sandbox_scripts_and_trackers(html: str) -> str:
    """Strip untrusted ``<script>`` tags, inline ``on*=`` handlers, ``javascript:``, and tracker ``<noscript>`` tags."""

    def _drop_if_tracker(match: re.Match[str]) -> str:
        low = match.group(0).lower()
        if any(tok in low for tok in _SANDBOX_TRACKER_TOKENS):
            return ""
        return match.group(0)

    cleaned = _neutralize_overlay_fragment(html)
    return _NOSCRIPT_TAG_RE.sub(_drop_if_tracker, cleaned)


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


def build_sandbox_guard_script(
    *,
    nonce: str | None = None,
    highlight_token: str | None = None,
    focus_text: str | None = None,
    focus_selector: str | None = None,
) -> str:
    """Build the canonical read-only sandbox guard ``<script>`` tag directly (no regex scraping).

    Compatible with :func:`ecom_ops.wp_ops.html_sanitize.harden_preview_html`'s
    ``guard_script_builder(nonce=nonce, **guard_params)`` contract.
    """
    hi_js = _safe_js_json(str(highlight_token or ""))
    focus_js = _safe_js_json(str(focus_text or ""))
    sel_js = _safe_js_json(str(focus_selector or ""))
    blocked_paths_js = _safe_js_json(list(BLOCKED_WP_PATHS))
    nonce_attr = f' nonce="{html_lib.escape(nonce, quote=True)}"' if nonce else ""
    return (
        f'<script{nonce_attr} data-azom-sandbox="guard" data-azom-guard="1">\n'
        "(function(){\n"
        f"  var highlightToken = {hi_js};\n"
        f"  var focusText = {focus_js};\n"
        f"  var focusSelector = {sel_js};\n"
        "  var BLOCKED_MSG = 'Blockerad i sandbox-läget — inga ändringar skickas till live';\n"
        f"  var BLOCKED_PATHS = {blocked_paths_js};\n"
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
        "  function findBySelector(sel){\n"
        "    if(!sel || !document.body) return null;\n"
        "    try {\n"
        "      var list = document.querySelectorAll(sel);\n"
        "      for(var i=0;i<list.length;i++){ if(isVisible(list[i])) return list[i]; }\n"
        "      return list.length ? list[0] : null;\n"
        "    } catch(e){ return null; }\n"
        "  }\n"
        "  var targetEl = null;\n"
        "  function applyHighlight(){\n"
        "    targetEl = (highlightToken && findByText(highlightToken)) || (focusSelector && findBySelector(focusSelector)) || (focusText && findByText(focusText)) || null;\n"
        "    if(targetEl){\n"
        "      targetEl.style.setProperty('outline', '3px solid #22c55e', 'important');\n"
        "      targetEl.style.setProperty('outline-offset', '4px', 'important');\n"
        "      if(!(focusSelector && !highlightToken)){\n"
        "        targetEl.style.setProperty('background-color', 'rgba(34, 197, 94, 0.16)', 'important');\n"
        "      }\n"
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


def build_interactive_sandbox_html(
    overlay_html: str,
    *,
    site_canonical_url: str,
    site_id: str,
    plan_id: str,
    path: str,
    highlight_token: str | None = None,
    focus_text: str | None = None,
    focus_selector: str | None = None,
    standalone_page: bool = False,
    synthetic_data: bool = False,
) -> str:
    """Build a standalone, read-only interactive sandbox HTML page for browser review."""
    html = _strip_sandbox_scripts_and_trackers(str(overlay_html or ""))
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
    if standalone_page:
        pill_label = "SANDBOX · NY SIDA (EXEMPELDATA)" if synthetic_data else "SANDBOX · NY SIDA"
        banner_text = (
            f"AZOM SANDBOX · {safe_plan} · Ny sida ({safe_path}) · "
            "Syntetisk exempeldata — inga ändringar på live"
        )
    else:
        pill_label = "SANDBOX"
        banner_text = (
            f"AZOM SANDBOX · {safe_plan} · {safe_path} · "
            "Skrivskyddad förhandsvisning — inga ändringar på live"
        )
    toolbar_html = (
        f'<div id="azom-sandbox-toolbar" data-azom-sandbox-toolbar="1" data-site-id="{safe_site}" '
        f'data-plan-id="{safe_plan}" role="region" aria-label="Azom sandbox-förhandsvisning">'
        f'<div class="az-sb-meta"><span class="az-sb-pill">{pill_label}</span>'
        f"<span>{banner_text}</span></div>"
        f'<button type="button" id="azom-sandbox-jump">Hoppa till ändringen</button>'
        "</div>"
        '<div id="azom-sandbox-toast" role="status" aria-live="polite"></div>'
    )

    guard_script = build_sandbox_guard_script(
        highlight_token=highlight_token,
        focus_text=focus_text,
        focus_selector=focus_selector,
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
