"""Progress detail/outcome formatting, provenance labels, and review notices for WP tools."""

from __future__ import annotations

from typing import Any

from ecom_ops.wp_ops.playbooks import PLAYBOOK_TOPICS
from ecom_ops.wp_ops.review_knowledge import TEMPLATE_CONTEXTS
from ecom_ops.wp_ops.tools.registry import (
    ALL_FILE_TOOLS,
    TOOL_REGISTRY,
    WORKSPACE_TOOLS,
    WP_TOOL_NAMES,
    default_outcome,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_PATH_RE,
    FILE_PLAN_RE,
    INTEL_SLUG_RE,
    PREVIEW_FOCUS_SECTIONS,
    PRODUCT_ID_RE,
    REVIEW_PLAN_RE,
    SITE_ID_RE,
)
from ecom_ops.wp_ops.tools.validation import error_class_token
from ecom_ops.wp_ops.tools.workspace_io import safe_sandbox_preview_url


def safe_tool_detail(
    name: str,
    arguments: dict[str, Any],
    site_id: str | None,
) -> str | None:
    """Build a safe status detail (site_id + validated path/plan_id/slug/topic). Never files_root or code."""
    parts: list[str] = []
    raw_site = arguments.get("site_id") if isinstance(arguments.get("site_id"), str) else site_id
    if isinstance(raw_site, str) and SITE_ID_RE.fullmatch(raw_site):
        parts.append(raw_site)
    if name in ALL_FILE_TOOLS or name in WP_TOOL_NAMES:
        raw_plan = arguments.get("plan_id") or arguments.get("source_plan_id") or arguments.get("base_plan_id")
        if isinstance(raw_plan, str) and (
            REVIEW_PLAN_RE.fullmatch(raw_plan) or FILE_PLAN_RE.fullmatch(raw_plan)
        ):
            parts.append(raw_plan)
        raw_path = arguments.get("path")
        raw_prod_path = arguments.get("product_path")
        if isinstance(raw_path, str) and raw_path and FILE_PATH_RE.fullmatch(raw_path):
            parts.append(raw_path[:60])
        elif isinstance(raw_prod_path, str) and raw_prod_path.startswith("/") and len(raw_prod_path) <= 80:
            parts.append(raw_prod_path[:60])
        else:
            kind = arguments.get("kind")
            slug = arguments.get("slug")
            theme_slug = arguments.get("theme_slug")
            if kind in {"theme", "plugin"} and isinstance(slug, str) and INTEL_SLUG_RE.fullmatch(slug):
                parts.append(f"{kind}/{slug}")
            elif isinstance(slug, str) and INTEL_SLUG_RE.fullmatch(slug):
                parts.append(slug)
            elif isinstance(theme_slug, str) and INTEL_SLUG_RE.fullmatch(theme_slug):
                parts.append(theme_slug)
        raw_pid = arguments.get("product_id")
        if type(raw_pid) is int and raw_pid > 0:
            parts.append(f"#{raw_pid}")
        elif isinstance(raw_pid, str) and PRODUCT_ID_RE.fullmatch(raw_pid.strip()):
            parts.append(f"#{raw_pid.strip()}")
        topic = arguments.get("topic")
        if isinstance(topic, str) and topic in PLAYBOOK_TOPICS:
            parts.append(topic)
        ctx = arguments.get("context")
        if isinstance(ctx, str) and ctx in TEMPLATE_CONTEXTS:
            parts.append(ctx)
        focus_sec = arguments.get("focus_section")
        if isinstance(focus_sec, str) and focus_sec.strip().lower() in PREVIEW_FOCUS_SECTIONS:
            parts.append(focus_sec.strip().lower())
    return " · ".join(parts) if parts else None


def safe_tool_outcome(
    name: str,
    data: dict[str, Any],
    *,
    reads: int | None = None,
) -> str:
    """Summarize a tool result in safe Swedish without source excerpts or secrets."""
    if not isinstance(data, dict):
        return "resultat mottaget"
    if data.get("ok") is not True:
        return error_class_token(data.get("error_class")) or "ToolFailed"
    spec = TOOL_REGISTRY.get(name)
    if spec is not None:
        return spec.outcome(data, reads)
    return default_outcome(data, reads)


def provenance_for(name: str, data: dict[str, Any] | None) -> str:
    payload = data if isinstance(data, dict) else {}
    if bool(payload.get("live_calls")):
        return "live"
    if payload.get("ssh") is True or payload.get("source") == "sftp":
        return "live·sftp"
    if name in WORKSPACE_TOOLS or name == "wp_open_review_workspace":
        return "arbetsyta"
    return "mock"


def file_status_detail(arguments: dict[str, Any], sticky: str | None) -> str:
    site = arguments.get("site_id") if isinstance(arguments.get("site_id"), str) else (sticky or "")
    path = arguments.get("path") if isinstance(arguments.get("path"), str) else ""
    site = site.replace("\n", " ")[:64]
    path = path.replace("\n", " ").replace("\r", " ")[:80]
    return "|".join(part for part in (site, path) if part)


def review_notice(results: list[dict[str, Any]]) -> str:
    from ecom_ops.bot.dashboard_links import review_plan_url

    entries: dict[tuple[str, str], str] = {}
    previews: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        plan_id = result.get("plan_id")
        site = result.get("site_id") if isinstance(result.get("site_id"), str) else "sajten"
        path = result.get("review_path")
        if isinstance(path, str) and path.startswith("/wp/") and isinstance(plan_id, str):
            risk = result.get("risk") if isinstance(result.get("risk"), str) else "okänd"
            key = (site, plan_id)
            existing = entries.get(key)
            if existing is None or risk != "okänd":
                entries[key] = risk
        if isinstance(plan_id, str) and REVIEW_PLAN_RE.fullmatch(plan_id):
            pv_url = (
                result.get("preview_url")
                if isinstance(result.get("preview_url"), str) and result.get("preview_url")
                else None
            )
            pv_path = (
                result.get("preview_path")
                if isinstance(result.get("preview_path"), str) and result.get("preview_path", "").startswith("/wp/")
                else None
            )
            pv_ready = result.get("preview_html_ready") is True
            nested = result.get("preview")
            if isinstance(nested, dict):
                if pv_url is None and isinstance(nested.get("preview_url"), str) and nested.get("preview_url"):
                    pv_url = nested["preview_url"]
                if (
                    pv_path is None
                    and isinstance(nested.get("preview_path"), str)
                    and nested.get("preview_path", "").startswith("/wp/")
                ):
                    pv_path = nested["preview_path"]
                if nested.get("preview_html_ready") is True:
                    pv_ready = True
            if pv_url or pv_path or pv_ready:
                resolved_url = pv_url or safe_sandbox_preview_url(site, plan_id)
                resolved_path = pv_path or f"/wp/{site}/reviews/{plan_id}/preview"
                previews[(site, plan_id)] = (resolved_url, resolved_path)
    lines: list[str] = []
    for (site, plan_id), risk in entries.items():
        url = review_plan_url(site, plan_id)
        link = f"{url}" if url else f"/wp/{site}/reviews/{plan_id} (kräver konfigurerad HTTPS-dashboard)"
        lines.append(
            f"Granskning {plan_id} på {site}, risk {risk}. "
            f"Öppna {link}; länken kräver inloggning och är inte en behörighet. Den godkänner inget."
        )
        if (site, plan_id) in previews:
            pv_url, pv_path = previews.pop((site, plan_id))
            if pv_url:
                lines.append(f"Testa redigerad sandboxsida ({plan_id}): {pv_url}")
            elif pv_path:
                lines.append(f"Testa redigerad sandboxsida ({plan_id}): {pv_path} (kräver inloggning i dashboarden)")
    for (_site, plan_id), (pv_url, pv_path) in previews.items():
        if pv_url:
            lines.append(f"Testa redigerad sandboxsida ({plan_id}): {pv_url}")
        elif pv_path:
            lines.append(f"Testa redigerad sandboxsida ({plan_id}): {pv_path} (kräver inloggning i dashboarden)")
    return "\n".join(lines)


def with_review_notice(content: str, results: list[dict[str, Any]]) -> str:
    notice = review_notice(results)
    if not notice:
        return content
    return f"{content.rstrip()}\n{notice}"


def format_file_results_sv(results: list[dict[str, Any]], reason: str) -> str:
    lines = [reason]
    for result in results:
        site_id = result.get("site_id") or "sajten"
        status = result.get("status") or result.get("error_class") or "unknown"
        path = ""
        plan = result.get("plan")
        if isinstance(plan, dict):
            path = str(plan.get("relative_path") or "")
        path = path or str(result.get("directory") or "")
        suffix = f" {path}" if path else ""
        lines.append(f"Filverktyg för {site_id}{suffix}: {status}.")
    return "\n".join(lines)


def stage_plan_id(data: dict[str, Any]) -> str | None:
    plan_id = data.get("plan_id")
    if (
        data.get("approval_required") is True
        and data.get("executed") is False
        and data.get("environment") == "staging"
        and data.get("risk") != "high"
        and isinstance(plan_id, str)
        and FILE_PLAN_RE.fullmatch(plan_id)
    ):
        return plan_id
    return None


__all__ = [
    "file_status_detail",
    "format_file_results_sv",
    "provenance_for",
    "review_notice",
    "safe_sandbox_preview_url",
    "safe_tool_detail",
    "safe_tool_outcome",
    "stage_plan_id",
    "with_review_notice",
]
