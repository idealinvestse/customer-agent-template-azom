"""Shared editor-site authorization and workspace JSON upsert helpers for WP agent tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecom_ops.rbac import Permission
from ecom_ops.wp_ops.models import Site, WpOpsResult
from ecom_ops.wp_ops.review_workspace import ReviewError, workspace_root


def authorize_editor_site(
    service: Any,
    site_id: str,
    actor: str,
    *,
    write: bool = False,
    extra_permission: Permission | None = None,
    action: str = "wp_ops_file_edit",
) -> tuple[Any, Site | None, WpOpsResult | None]:
    """Resolve ``FileEditor`` + ``Site`` and enforce optional extra RBAC permission."""
    editor = service._file_editor()
    site_obj, denied = editor._site(site_id, actor, write=write)
    if denied is not None:
        return editor, None, denied
    if extra_permission is not None and site_obj is not None:
        extra_denied = editor.need(actor, extra_permission, site=site_obj, action=action)
        if extra_denied is not None:
            return editor, None, extra_denied
    return editor, site_obj, None


def read_workspace_json(
    ops: Any,
    data_dir: Path,
    site_id: str,
    plan_id: str,
    filename: str,
    *,
    actor: str,
) -> tuple[bool, str, dict[str, Any], WpOpsResult | None]:
    """Read an optional JSON file in ``work/<filename>`` via ``ReviewPlans``.

    Returns ``(exists, raw_text, parsed_dict, error_result)``. A missing file
    returns ``(False, "", {}, None)``; any other error returns ``error_result``.
    """
    read_res = ops.workspace_read_file(site_id, plan_id, filename, actor=actor)
    if not read_res.ok:
        err_cls = (read_res.data or {}).get("error_class")
        if err_cls == "FileMissing":
            return False, "", {}, None
        return False, "", {}, read_res
    existing_raw = ""
    existing_data: dict[str, Any] = {}
    try:
        ws_file = workspace_root(data_dir, site_id, plan_id) / "work" / filename
        existing_raw = ws_file.read_text(encoding="utf-8")
        parsed = json.loads(existing_raw)
        if isinstance(parsed, dict):
            existing_data = parsed
    except (ReviewError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        existing_data = {}
    return True, existing_raw, existing_data, None


def upsert_workspace_json(
    ops: Any,
    data_dir: Path,
    site_id: str,
    plan_id: str,
    filename: str,
    new_json: str,
    *,
    actor: str,
    existing_state: tuple[bool, str] | None = None,
) -> WpOpsResult:
    """Create, patch, or refresh ``work/<filename>`` with ``new_json``."""
    if existing_state is None:
        file_exists, existing_raw, _parsed, read_err = read_workspace_json(
            ops, data_dir, site_id, plan_id, filename, actor=actor
        )
        if read_err is not None:
            return read_err
    else:
        file_exists, existing_raw = existing_state

    if file_exists:
        if new_json == existing_raw:
            return ops.refresh(site_id, plan_id, actor=actor)
        return ops.patch_file(site_id, plan_id, filename, existing_raw, new_json, actor=actor)
    return ops.create_file(site_id, plan_id, filename, new_json, actor=actor)


__all__ = [
    "authorize_editor_site",
    "read_workspace_json",
    "upsert_workspace_json",
]
