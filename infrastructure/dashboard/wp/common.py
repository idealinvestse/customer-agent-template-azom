"""Shared stateless RBAC, validation, scrubbing, and HTTP response helpers for WP routes."""

from __future__ import annotations

import re

from flask import Response, g, redirect, url_for

from support import _flash, _flash_q

PLAN_ID_RE = re.compile(r"^fe-[a-f0-9]{12}$")
REVIEW_ID_RE = re.compile(r"^wr-[a-f0-9]{12}$")
CHANGE_ID_RE = re.compile(r"^chg-[a-f0-9]{12}$")
SITE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
KINDS = frozenset({"theme", "plugin"})

_PLAN_ID = PLAN_ID_RE
_REVIEW_ID = REVIEW_ID_RE
_SITE_ID = SITE_ID_RE
_KINDS = KINDS


def wp_service():
    """Return a fresh stateless ``WpOpsService`` instance for the current request."""
    from ecom_ops.wp_ops import WpOpsService

    return WpOpsService()


def review_ops():
    """Return a fresh stateless ``ReviewPlans`` instance for the current request."""
    from ecom_ops.wp_ops.review_plan import ReviewPlans

    return ReviewPlans()


def current_actor():
    from ecom_ops.rbac import Actor

    return Actor(name=g.actor["name"], role=g.actor["role"])


def has_permissions(*perms) -> bool:
    actor = current_actor()
    return all(actor.has(perm) for perm in perms)


def can_read_wp() -> bool:
    from ecom_ops.rbac import Permission

    return has_permissions(Permission.WP_OPS_READ)


def can_drill() -> bool:
    from ecom_ops.rbac import Permission

    return has_permissions(Permission.WP_OPS_OPERATE)


def can_save_ssh() -> bool:
    from ecom_ops.rbac import Permission

    return has_permissions(Permission.WP_OPS_ADMIN)


def file_admin() -> bool:
    from ecom_ops.rbac import Permission

    return has_permissions(Permission.WP_OPS_ADMIN, Permission.CODE_EDIT)


def can_restore() -> bool:
    from ecom_ops.rbac import Permission

    return has_permissions(Permission.WP_OPS_RESTORE, Permission.WP_OPS_ADMIN)


def can_run(site: dict | None) -> bool:
    if not site:
        return False
    from ecom_ops.rbac import Permission

    if site.get("environment") == "production":
        return has_permissions(Permission.WP_OPS_ADMIN)
    return has_permissions(Permission.WP_OPS_OPERATE)


def can_edit(site: dict | None) -> bool:
    if not site:
        return False
    from ecom_ops.rbac import Permission

    if site.get("environment") == "production":
        return has_permissions(Permission.WP_OPS_ADMIN, Permission.CODE_EDIT)
    return has_permissions(Permission.WP_OPS_OPERATE)


def is_denied(result: object) -> bool:
    data = getattr(result, "data", None) or {}
    return data.get("error_class") == "AccessDenied"


def file_block(site_id: str, result: object) -> Response | None:
    del site_id
    if is_denied(result):
        return Response("Saknar behörighet", 403)
    data = getattr(result, "data", None) or {}
    if not getattr(result, "ok", False) and data.get("error_class") == "UnknownSite":
        return Response("Okänd webbplats", 404)
    return None


def redirect_site(site_id: str, result: object):
    blocked = file_block(site_id, result)
    if blocked is not None:
        return blocked
    message = getattr(result, "message", "")
    ok = getattr(result, "ok", False)
    _flash(message, "ok" if ok else "error")
    frag = _flash_q(message) if ok else _flash_q(err=message)
    return redirect(url_for("wp_site_detail", site_id=site_id) + f"?{frag}")


def review_http(result: object) -> Response | None:
    data = getattr(result, "data", None) or {}
    kind = data.get("error_class")
    if kind == "AccessDenied" or is_denied(result):
        return Response("Saknar behörighet", 403)
    if kind in {"UnknownSite", "UnknownPlan", "StalePlan"}:
        return Response("Okänd plan", 404)
    if kind == "Expired":
        return Response("Planen har gått ut", 403)
    return None


def scrub_posted(site_id: str, text: str) -> str:
    from ecom_ops.wp_ops.credentials import bind_credential, scrub_secrets
    from ecom_ops.wp_ops.registry import load_sites, site_by_id

    try:
        site = site_by_id(load_sites(), site_id)
    except ValueError:
        return text
    if site is None:
        return text
    bound = bind_credential(site)
    if not bound.present:
        return text
    cleaned = scrub_secrets(text, [bound.value])
    return cleaned if isinstance(cleaned, str) else text


def safe_review_id(value: object) -> str:
    text = str(value or "").strip()
    return text if REVIEW_ID_RE.fullmatch(text) else ""


def safe_variant_group(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return ""
    return re.sub(r"[\r\n\t]+", " ", text)


def safe_http_url(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith(("https://", "http://")) and not any(ch in text for ch in "\r\n\t\"'<> "):
        return text[:400]
    return ""


def sanitize_draft_page(site_id: str, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    edit_url = safe_http_url(raw.get("edit_url"))
    link = safe_http_url(raw.get("link"))
    if not (raw.get("id") or edit_url or link or raw.get("slug")):
        return {}
    return {
        "id": raw.get("id"),
        "slug": scrub_posted(site_id, str(raw.get("slug") or "").strip())[:120],
        "status": scrub_posted(site_id, str(raw.get("status") or "draft").strip())[:40],
        "edit_url": edit_url,
        "link": link,
        "pushed_at": str(raw.get("pushed_at") or "").strip()[:40],
        "pushed_by": scrub_posted(site_id, str(raw.get("pushed_by") or "").strip())[:60],
    }


def scrub_preview_meta(site_id: str, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or not raw:
        return {}
    preview_kind = str(raw.get("preview_kind") or "")[:40]
    synthetic_data = raw.get("synthetic_data") is True or preview_kind == "standalone_page"
    return {
        "preview_kind": preview_kind,
        "path": scrub_posted(site_id, str(raw.get("path") or ""))[:200],
        "focus_target": scrub_posted(site_id, str(raw.get("focus_target") or ""))[:160],
        "focus_matched": raw.get("focus_matched") is True,
        "synthetic_data": synthetic_data,
    }


def scrub_page_draft_meta(site_id: str, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or not raw:
        return {}
    shell_raw = str(raw.get("shell_path") or "/").strip() or "/"
    return {
        "slug": scrub_posted(site_id, str(raw.get("slug") or "").strip())[:120],
        "title": scrub_posted(site_id, str(raw.get("title") or "").strip())[:160],
        "page_type": scrub_posted(site_id, str(raw.get("page_type") or "").strip())[:60],
        "shell_path": scrub_posted(site_id, shell_raw)[:200],
        "template_file": scrub_posted(site_id, str(raw.get("template_file") or "").strip())[:160],
        "synthetic_data": raw.get("synthetic_data") is not False,
    }
