"""Headless Chrome live preview capture and DOM/CSS element inspection."""

from __future__ import annotations

import base64
import json
import re
import sys
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp import http_guard as _http_guard
from ecom_ops.integrations.wp.cdp_browser import (
    DEFAULT_VIEWPORTS,
    PNG_MAGIC,
    PREVIEW_CHROME_FLAGS,
    WP_FATAL_MARKERS,
    HeadlessBrowserSession,
    LoopbackCdpClient,
    ReadOnlyFetchInterceptor,
    collect_cdp_diagnostics,
    find_browser_binary,
    png_dimensions,
)
from ecom_ops.integrations.wp.http_guard import (
    allowed_site_hosts,
    blocked_site_path_reason,
    safe_https_parsed,
    validate_preview_path,
)
from ecom_ops.integrations.wp.preview_scripts import (
    dom_probe_script,
    focus_and_highlight_script,
    inspect_element_script,
    reveal_region_script,
    scroll_sweep_script,
)
from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
from ecom_ops.wp_ops.file_changes import rest_reads_allowed
from ecom_ops.wp_ops.models import Site
from ecom_ops.wp_ops.review_workspace import ReviewError

PREVIEW_VIEWPORTS = DEFAULT_VIEWPORTS
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
    "compare-desktop.png",
    "compare-mobile.png",
    "change-desktop.png",
    "change-mobile.png",
    "focus-desktop.png",
    "focus-mobile.png",
    "preview-desktop.png",
    "live-desktop.png",
    "preview-mobile.png",
    "live-mobile.png",
)


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


def _capture_main_screenshot(
    cdp: LoopbackCdpClient,
    *,
    width: int,
    height: int,
    full_page: bool,
) -> bytes:
    if full_page:
        sweep_info = cdp.eval_json(scroll_sweep_script(_MAX_FULL_PAGE_HEIGHT))
        cdp.drain_events(duration_s=0.7)
        cdp.eval_json(reveal_region_script(0.0, float(_MAX_FULL_PAGE_HEIGHT), full_page=True))
        cdp.call("Runtime.evaluate", {"expression": "window.scrollTo(0, 0);", "returnByValue": True})
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
                "clip": {"x": 0, "y": 0, "width": width, "height": full_h, "scale": 1},
            },
        )
    else:
        shot = cdp.call("Page.captureScreenshot", {"format": "png"})
    return base64.b64decode(str(shot.get("data") or ""))


def _capture_focus_crop(
    cdp: LoopbackCdpClient,
    *,
    label: str,
    vp_name: str,
    width: int,
    height: int,
    is_mobile: bool,
    artifact_dir: Path,
    clean_selector: str | None,
    clean_text: str | None,
    highlight_token: str | None,
    clean_section: str | None,
) -> tuple[bool, bool, str | None, str | None, str | None]:
    hi_info = cdp.eval_json(
        focus_and_highlight_script(
            focus_selector=clean_selector,
            focus_text=clean_text,
            highlight_token=highlight_token if label == "preview" else None,
            focus_section=clean_section,
        )
    )
    highlight_found = hi_info.get("highlightFound") is True
    if hi_info.get("found") is not True:
        return highlight_found, False, None, None, None

    vp_focus_target = str(hi_info.get("target") or "") or None
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
    cdp.drain_events(duration_s=0.9)
    reveal_info = cdp.eval_json(reveal_region_script(clip_y, clip_h, full_page=False))
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
    if clip_y > 200.0 and target_kind not in {"section:top", "section:header"}:
        cdp.call("Runtime.evaluate", {"expression": "window.scrollTo(0, 0);", "returnByValue": True})
    cdp.drain_events(duration_s=0.15)
    crop = cdp.call(
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": clip_y, "width": width, "height": clip_h, "scale": 1},
        },
    )
    crop_bytes = base64.b64decode(str(crop.get("data") or ""))
    change_shot_name: str | None = None
    focus_shot_name: str | None = None
    if crop_bytes[:8] == PNG_MAGIC:
        if label == "preview" and highlight_found:
            change_shot_name = f"change-{vp_name}.png"
            (artifact_dir / change_shot_name).write_bytes(crop_bytes)
        else:
            focus_shot_name = f"focus-{vp_name}.png"
            (artifact_dir / focus_shot_name).write_bytes(crop_bytes)
    return highlight_found, True, vp_focus_target, change_shot_name, focus_shot_name


def _collect_visual_health(
    cdp: LoopbackCdpClient,
    *,
    clean_selector: str | None,
    clean_text: str | None,
    highlight_token: str | None,
    preview_kind_override: str | None,
) -> dict[str, Any] | None:
    try:
        parsed_vh = cdp.eval_json(
            inspect_element_script(
                selector=clean_selector
                or (".azom-page-draft-root" if preview_kind_override == "standalone_page" else None),
                text=clean_text or highlight_token,
                include_matched_rules=False,
            )
        )
        if parsed_vh.get("found") is True:
            return {
                "matched_by": parsed_vh.get("matched_by"),
                "bounding_box": parsed_vh.get("bounding_box"),
                "health_checks": parsed_vh.get("health_checks") or {},
                "warnings": list(parsed_vh.get("warnings") or [])[:8],
            }
    except Exception:
        return None
    return None


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
    preview_kind_override: str | None = None,
    extra_preview_meta: dict[str, Any] | None = None,
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
    ok_base, site_host, _berr = safe_https_parsed(parsed_base)
    if not ok_base:
        raise ReviewError("LiveFetchFailed")

    resolved_target = target_url or urllib.parse.urlunsplit(("https", parsed_base.netloc, clean_path, "", ""))
    target_parsed = urllib.parse.urlparse(resolved_target)
    ok_target, target_host, _terr = safe_https_parsed(target_parsed)
    if not ok_target or target_host not in allowed_site_hosts(site_host) or blocked_site_path_reason(target_parsed):
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
    started_at = time.monotonic()
    revisions_meta: dict[str, Any] = {}
    screenshots: list[str] = []
    any_focus_matched = False
    matched_focus_target: str | None = None
    visual_health: dict[str, Any] = {}
    interceptor = ReadOnlyFetchInterceptor(site_host=site_host, resolved_target=resolved_target)

    with HeadlessBrowserSession(
        binary,
        extra_flags=PREVIEW_CHROME_FLAGS,
        profile_prefix="azom-wp-live-preview-",
        timeout_s=timeout_s,
    ) as session:
        session.attach_fetch_interceptor(interceptor)
        cdp = session.cdp
        for label, html_body in clean_revisions:
            interceptor.set_document_html(html_body)
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
                    {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": is_mobile},
                )
                cdp.call("Network.setCacheDisabled", {"cacheDisabled": True})
                interceptor.arm_main_document()
                cdp.send("Page.navigate", {"url": resolved_target})
                loaded = cdp.wait_for_event("Page.loadEventFired", timeout_s=min(20.0, timeout_s))
                cdp.drain_events(duration_s=1.2)

                dom_info = cdp.eval_json(dom_probe_script())
                png_bytes = _capture_main_screenshot(cdp, width=width, height=height, full_page=clean_full_page)
                dims = png_dimensions(png_bytes)
                shot_name = f"{label}-{vp_name}.png"
                shot_path = artifact_dir / shot_name
                if png_bytes[:8] == PNG_MAGIC:
                    shot_path.write_bytes(png_bytes)
                    screenshots.append(shot_name)

                highlight_found = False
                vp_focus_matched = False
                vp_focus_target: str | None = None
                change_shot_name: str | None = None
                focus_shot_name: str | None = None
                if should_run_focus:
                    (
                        highlight_found,
                        vp_focus_matched,
                        vp_focus_target,
                        change_shot_name,
                        focus_shot_name,
                    ) = _capture_focus_crop(
                        cdp,
                        label=label,
                        vp_name=vp_name,
                        width=width,
                        height=height,
                        is_mobile=is_mobile,
                        artifact_dir=artifact_dir,
                        clean_selector=clean_selector,
                        clean_text=clean_text,
                        highlight_token=highlight_token,
                        clean_section=clean_section,
                    )
                    if vp_focus_matched:
                        any_focus_matched = True
                        if matched_focus_target is None and vp_focus_target:
                            matched_focus_target = vp_focus_target
                    if change_shot_name:
                        screenshots.append(change_shot_name)
                    if focus_shot_name:
                        screenshots.append(focus_shot_name)

                if label == "preview" and (
                    clean_selector or clean_text or highlight_token or preview_kind_override == "standalone_page"
                ):
                    vp_vh = _collect_visual_health(
                        cdp,
                        clean_selector=clean_selector,
                        clean_text=clean_text,
                        highlight_token=highlight_token,
                        preview_kind_override=preview_kind_override,
                    )
                    if vp_vh is not None:
                        visual_health[vp_name] = vp_vh

                diag = collect_cdp_diagnostics(cdp.events, ignore_blocked_by_client=True)
                body_text = str(dom_info.get("bodyText") or "")
                lower_body = body_text.lower()
                fatal_markers = [m for m in WP_FATAL_MARKERS if m in lower_body]
                viewports_out[vp_name] = {
                    "loaded": loaded,
                    "http_status": diag["doc_status"] or 200,
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
                    "console_errors": diag["console_errors"][:10],
                    "network_errors": diag["network_errors"][:10],
                    "fatal_markers": fatal_markers,
                }
            revisions_meta[label] = viewports_out

    ordered_screenshots = _order_screenshots(screenshots)
    agg_warnings: list[str] = []
    for vp_name in ("desktop", "mobile"):
        vp_vh = visual_health.get(vp_name)
        if isinstance(vp_vh, dict):
            for warn in vp_vh.get("warnings") or []:
                label_warn = f"[{vp_name}] {warn}"
                if label_warn not in agg_warnings and len(agg_warnings) < 10:
                    agg_warnings.append(label_warn)
    visual_health_summary: dict[str, Any] | None = None
    if visual_health:
        visual_health_summary = {
            **visual_health,
            "warnings": agg_warnings,
            "ok": len(agg_warnings) == 0,
        }
    resolved_kind = (
        preview_kind_override
        if preview_kind_override in {"live_overlay", "live_read_only", "standalone_page"}
        else ("live_overlay" if len(clean_revisions) > 1 else "live_read_only")
    )
    if resolved_kind == "standalone_page":
        standalone_order = (
            "preview-desktop.png",
            "preview-mobile.png",
            "focus-desktop.png",
            "focus-mobile.png",
            "change-desktop.png",
            "change-mobile.png",
        )
        seen_s = set(ordered_screenshots)
        ordered_screenshots = [s for s in standalone_order if s in seen_s] + [
            s for s in ordered_screenshots if s not in standalone_order
        ]
    summary: dict[str, Any] = {
        "preview_kind": resolved_kind,
        "site_id": site.id,
        "target_url": resolved_target,
        "path": clean_path,
        "allowed_requests": interceptor.allowed_count,
        "blocked_counts": interceptor.blocked_counts,
        "blocked_sample": interceptor.blocked_sample,
        "highlight_token": highlight_token,
        "focus_section": clean_section,
        "focus_selector": clean_selector,
        "focus_text": clean_text,
        "full_page": clean_full_page,
        "focus_matched": any_focus_matched,
        "focus_target": matched_focus_target,
        "revisions": revisions_meta,
        "screenshots": ordered_screenshots,
        "visual_health": visual_health_summary,
    }
    if isinstance(extra_preview_meta, dict):
        for k, v in extra_preview_meta.items():
            if k not in summary and isinstance(k, str):
                summary[k] = v
    return _finalize_summary(site, summary, artifact_dir)


def _resolve_fetch_live_html(override: Callable[..., dict[str, Any]] | None = None) -> Callable[..., dict[str, Any]]:
    if override is not None:
        return override
    facade = sys.modules.get("ecom_ops.integrations.wp_live_preview")
    if facade is not None:
        hook = getattr(facade, "fetch_live_html", None)
        if callable(hook):
            return hook
    return _http_guard.fetch_live_html


def inspect_element(
    site: Site,
    *,
    path: str = "/",
    html_override: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    browser_binary: str | None = None,
    timeout_s: float = 30.0,
    target_url: str | None = None,
    fetch_html_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inspect rendered DOM, computed styles, matched CSS rules, and layout health via read-only CDP."""
    clean_path = validate_preview_path(path)
    _sec, clean_selector, clean_text, _full = validate_focus_options(
        focus_selector=selector,
        focus_text=text,
    )
    if not clean_selector and not clean_text:
        raise ReviewError("InvalidFocusTarget")

    parsed_base = urllib.parse.urlparse(str(site.canonical_url or ""))
    ok_base, site_host, _berr = safe_https_parsed(parsed_base)
    if not ok_base:
        raise ReviewError("LiveFetchFailed")

    if html_override is None:
        fetch_fn = _resolve_fetch_live_html(fetch_html_fn)
        fetched = fetch_fn(site, clean_path, use_cache=True)
        html_body = str(fetched.get("html") or "")
        resolved_target = str(fetched.get("final_url") or "") or urllib.parse.urlunsplit(
            ("https", parsed_base.netloc, clean_path, "", "")
        )
    else:
        html_body = str(html_override or "")
        resolved_target = target_url or urllib.parse.urlunsplit(("https", parsed_base.netloc, clean_path, "", ""))

    target_parsed = urllib.parse.urlparse(resolved_target)
    ok_target, target_host, _terr = safe_https_parsed(target_parsed)
    if not ok_target or target_host not in allowed_site_hosts(site_host) or blocked_site_path_reason(target_parsed):
        raise ReviewError("PreviewPathRefused")

    binary = browser_binary or find_browser_binary()
    if not binary:
        raise ReviewError("BrowserUnavailable")

    started_at = time.monotonic()
    viewports_out: dict[str, Any] = {}
    interceptor = ReadOnlyFetchInterceptor(site_host=site_host, resolved_target=resolved_target)
    interceptor.set_document_html(html_body)

    with HeadlessBrowserSession(
        binary,
        extra_flags=PREVIEW_CHROME_FLAGS,
        profile_prefix="azom-wp-inspect-",
        timeout_s=timeout_s,
        enable_log=False,
    ) as session:
        session.attach_fetch_interceptor(interceptor)
        cdp = session.cdp
        for vp_name, width, height, is_mobile in PREVIEW_VIEWPORTS:
            if time.monotonic() - started_at > timeout_s:
                break
            cdp.events.clear()
            cdp.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": is_mobile},
            )
            cdp.call("Network.setCacheDisabled", {"cacheDisabled": True})
            interceptor.arm_main_document()
            cdp.send("Page.navigate", {"url": resolved_target})
            cdp.wait_for_event("Page.loadEventFired", timeout_s=min(15.0, timeout_s))
            cdp.drain_events(duration_s=0.9)
            viewports_out[vp_name] = cdp.eval_json(
                inspect_element_script(
                    selector=clean_selector,
                    text=clean_text,
                    include_matched_rules=(vp_name == "desktop"),
                )
            )

    desktop = viewports_out.get("desktop") or {}
    mobile = viewports_out.get("mobile") or {}
    primary = desktop if desktop.get("found") else mobile
    warnings: list[str] = []
    for vp_name, vp_data in (("desktop", desktop), ("mobile", mobile)):
        for w in vp_data.get("warnings") or []:
            entry = f"[{vp_name}] {w}"
            if entry not in warnings and len(warnings) < 12:
                warnings.append(entry)

    out: dict[str, Any] = {
        "found": bool(primary.get("found")),
        "site_id": site.id,
        "path": clean_path,
        "target_url": resolved_target,
        "matched_by": primary.get("matched_by"),
        "tag": primary.get("tag"),
        "id": primary.get("id"),
        "classes": primary.get("classes"),
        "outer_html": primary.get("outer_html"),
        "text_content": primary.get("text_content"),
        "bounding_box": primary.get("bounding_box"),
        "computed_styles": primary.get("computed_styles") or {},
        "pseudo_before": primary.get("pseudo_before"),
        "pseudo_after": primary.get("pseudo_after"),
        "children": primary.get("children") or [],
        "matched_rules": desktop.get("matched_rules") or [],
        "health_checks": primary.get("health_checks") or {},
        "mobile_bounding_box": mobile.get("bounding_box"),
        "mobile_health_checks": mobile.get("health_checks") or {},
        "warnings": warnings,
        "allowed_requests": interceptor.allowed_count,
        "blocked_counts": interceptor.blocked_counts,
    }
    try:
        secret = bind_credential(site).value
    except Exception:
        secret = ""
    cleaned = scrub_secrets(out, [secret]) if secret else out
    assert isinstance(cleaned, dict)
    return cleaned
