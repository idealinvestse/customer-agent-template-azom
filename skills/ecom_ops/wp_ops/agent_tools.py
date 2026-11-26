"""Bounded, typed WordPress operations tools for the chat agent.

Every tool resolves a registered site through ``WpOpsService`` and runs under
its server-side RBAC checks. This module is a thin compatibility facade over
``ecom_ops.wp_ops.tools``; it exposes reads and non-executable plan previews
only, with no shell, PHP, update, or restore execution path.
"""

from __future__ import annotations

import os
import time  # noqa: F401 — preserved monkeypatch target (agent_tools.time.monotonic)
from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.wp_ops.file_changes import (
    rest_reads_allowed as rest_reads_allowed,
)
from ecom_ops.wp_ops.file_policy import (
    is_safe_pure_css_file as is_safe_pure_css_file,
)
from ecom_ops.wp_ops.html_sanitize import (
    UNSAFE_PRODUCT_HTML_RE as _UNSAFE_PRODUCT_HTML_RE,  # noqa: F401
)
from ecom_ops.wp_ops.html_sanitize import (
    sanitize_page_draft_html as sanitize_page_draft_html,
)
from ecom_ops.wp_ops.models import Site, WpOpsResult
from ecom_ops.wp_ops.pii import contains_real_pii, normalize_pii_text
from ecom_ops.wp_ops.registry import load_sites, site_by_id
from ecom_ops.wp_ops.service import WpOpsService
from ecom_ops.wp_ops.tools.catalog import (
    handle_get_product,
    handle_list_products,
    parse_numeric_price,
    parse_product_id,
)
from ecom_ops.wp_ops.tools.components import (
    component_rest_source as component_rest_source,
)
from ecom_ops.wp_ops.tools.components import (
    has_site_rest_credentials as _has_site_rest_credentials,  # noqa: F401
)
from ecom_ops.wp_ops.tools.components import (
    load_live_plugin_rows as load_live_plugin_rows,
)
from ecom_ops.wp_ops.tools.components import (
    load_live_theme_rows as load_live_theme_rows,
)
from ecom_ops.wp_ops.tools.components import (
    same_configured_host as _same_configured_host,
)
from ecom_ops.wp_ops.tools.components import (
    walk_component_tree,
)
from ecom_ops.wp_ops.tools.context import (
    _valid_context as _valid_context,  # noqa: F401
)
from ecom_ops.wp_ops.tools.context import (
    context_for_storage as context_for_storage,
)
from ecom_ops.wp_ops.tools.context import (
    context_matches as context_matches,
)
from ecom_ops.wp_ops.tools.drift import (
    PLAN_LOCK as _PLAN_LOCK,  # noqa: F401
)
from ecom_ops.wp_ops.tools.drift import (
    load_plans as _drift_load_plans,
)
from ecom_ops.wp_ops.tools.drift import (
    read_plan_rows as _drift_read_plan_rows,
)
from ecom_ops.wp_ops.tools.drift import (
    store_plan as _drift_store_plan,
)
from ecom_ops.wp_ops.tools.guides import (
    WP_DRIFT_TOOL_GUIDE as WP_DRIFT_TOOL_GUIDE,
)
from ecom_ops.wp_ops.tools.guides import (
    WP_TOOL_GUIDE as WP_TOOL_GUIDE,
)
from ecom_ops.wp_ops.tools.html_parse import (
    extract_html_by_selector,
    strip_html_plain,
)
from ecom_ops.wp_ops.tools.loop_state import (
    MAX_MULTI_VARIANT_ATTACHMENTS as _MAX_MULTI_VARIANT_ATTACHMENTS,  # noqa: F401
)
from ecom_ops.wp_ops.tools.loop_state import (
    PRIMARY_SHOT_ORDER as _PRIMARY_SHOT_ORDER,  # noqa: F401
)
from ecom_ops.wp_ops.tools.loop_state import (
    compact_older_file_messages as _compact_older_file_messages,  # noqa: F401
)
from ecom_ops.wp_ops.tools.loop_state import (
    primary_attachment as _primary_attachment,  # noqa: F401
)
from ecom_ops.wp_ops.tools.loop_state import (
    read_cache_key as _read_cache_key,  # noqa: F401
)
from ecom_ops.wp_ops.tools.loops import (
    run_bounded_file_tool_loop as run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.tools.loops import (
    run_bounded_wp_tool_loop as run_bounded_wp_tool_loop,
)
from ecom_ops.wp_ops.tools.loops import (
    tool_name_from as tool_name_from,
)
from ecom_ops.wp_ops.tools.pages import (
    handle_get_page,
    handle_list_pages,
)
from ecom_ops.wp_ops.tools.preview import (
    site_for_preview,
    workspace_product_path,
)
from ecom_ops.wp_ops.tools.progress import (
    file_status_detail as _file_status_detail,  # noqa: F401
)
from ecom_ops.wp_ops.tools.progress import (
    format_file_results_sv as _format_file_results_sv,  # noqa: F401
)
from ecom_ops.wp_ops.tools.progress import (
    provenance_for as _provenance_for,  # noqa: F401
)
from ecom_ops.wp_ops.tools.progress import (
    review_notice as _review_notice,  # noqa: F401
)
from ecom_ops.wp_ops.tools.progress import (
    safe_sandbox_preview_url as _safe_sandbox_preview_url,  # noqa: F401
)
from ecom_ops.wp_ops.tools.progress import (
    safe_tool_detail as _safe_tool_detail,  # noqa: F401
)
from ecom_ops.wp_ops.tools.progress import (
    safe_tool_outcome as _safe_tool_outcome,  # noqa: F401
)
from ecom_ops.wp_ops.tools.progress import (
    stage_plan_id as _stage_plan_id,  # noqa: F401
)
from ecom_ops.wp_ops.tools.progress import (
    with_review_notice as _with_review_notice,  # noqa: F401
)
from ecom_ops.wp_ops.tools.registry import (
    ALL_FILE_TOOLS as ALL_FILE_TOOLS,
)
from ecom_ops.wp_ops.tools.registry import (
    DEEP_PHASE_TOOLS as _DEEP_PHASE_TOOLS,  # noqa: F401
)
from ecom_ops.wp_ops.tools.registry import (
    EXTRA_FILE_TOOLS as EXTRA_FILE_TOOLS,
)
from ecom_ops.wp_ops.tools.registry import (
    FE_PLAN_TOOLS as _FE_PLAN_TOOLS,  # noqa: F401
)
from ecom_ops.wp_ops.tools.registry import (
    FILE_TOOL_NAMES as FILE_TOOL_NAMES,
)
from ecom_ops.wp_ops.tools.registry import (
    FILE_TOOL_SCHEMAS as FILE_TOOL_SCHEMAS,
)
from ecom_ops.wp_ops.tools.registry import (
    READ_BUDGET_TOOLS as _READ_BUDGET_TOOLS,  # noqa: F401
)
from ecom_ops.wp_ops.tools.registry import (
    SEARCH_BUDGET_TOOLS as _SEARCH_BUDGET_TOOLS,  # noqa: F401
)
from ecom_ops.wp_ops.tools.registry import (
    TOOL_REGISTRY,
)
from ecom_ops.wp_ops.tools.registry import (
    WORKSPACE_MUTATING_TOOLS as _WORKSPACE_MUTATING_TOOLS,  # noqa: F401
)
from ecom_ops.wp_ops.tools.registry import (
    WORKSPACE_TOOLS as _WORKSPACE_TOOLS,  # noqa: F401
)
from ecom_ops.wp_ops.tools.registry import (
    WP_TOOL_LABELS as _WP_TOOL_LABELS,  # noqa: F401
)
from ecom_ops.wp_ops.tools.registry import (
    WP_TOOL_NAMES as WP_TOOL_NAMES,
)
from ecom_ops.wp_ops.tools.registry import (
    WP_TOOL_SCHEMAS as WP_TOOL_SCHEMAS,
)
from ecom_ops.wp_ops.tools.scrub import (
    scrub_bounded,
    scrub_keep_newlines,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_LOOP_MAX_CALLS as FILE_LOOP_MAX_CALLS,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_LOOP_MAX_READS as FILE_LOOP_MAX_READS,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_LOOP_MAX_RECOVERY_RETRIES as FILE_LOOP_MAX_RECOVERY_RETRIES,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_LOOP_MAX_RESULT_BYTES as FILE_LOOP_MAX_RESULT_BYTES,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_LOOP_MAX_SEARCHES as FILE_LOOP_MAX_SEARCHES,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_LOOP_MAX_SECONDS as FILE_LOOP_MAX_SECONDS,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_LOOP_MAX_TOTAL_BYTES as FILE_LOOP_MAX_TOTAL_BYTES,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_PLAN_RE as _FILE_PLAN,
)
from ecom_ops.wp_ops.tools.types import (
    FILE_READ_DEFAULT as _FILE_READ_DEFAULT,  # noqa: F401
)
from ecom_ops.wp_ops.tools.types import (
    FILE_READ_MAX as _FILE_READ_MAX,  # noqa: F401
)
from ecom_ops.wp_ops.tools.types import (
    INTEL_SLUG_RE as _INTEL_SLUG,  # noqa: F401
)
from ecom_ops.wp_ops.tools.types import (
    PRODUCT_ID_RE as _PRODUCT_ID_RE,  # noqa: F401
)
from ecom_ops.wp_ops.tools.types import (
    RECOVERY_ERROR_CLASSES as _RECOVERY_ERROR_CLASSES,  # noqa: F401
)
from ecom_ops.wp_ops.tools.types import (
    REVIEW_PLAN_RE as _REVIEW_PLAN,
)
from ecom_ops.wp_ops.tools.types import (
    SITE_ID_RE as _SITE_ID,
)
from ecom_ops.wp_ops.tools.types import (
    VARIANT_GROUP_RE as _VARIANT_GROUP,  # noqa: F401
)
from ecom_ops.wp_ops.tools.types import (
    WP_TOOL_MAX_AGE_SECONDS as WP_TOOL_MAX_AGE_SECONDS,
)
from ecom_ops.wp_ops.tools.types import (
    WP_TOOL_MAX_CALLS as WP_TOOL_MAX_CALLS,
)
from ecom_ops.wp_ops.tools.types import (
    WP_TOOL_MAX_RESULT_BYTES as WP_TOOL_MAX_RESULT_BYTES,
)
from ecom_ops.wp_ops.tools.types import (
    WP_TOOL_MAX_SECONDS as WP_TOOL_MAX_SECONDS,
)
from ecom_ops.wp_ops.tools.types import (
    WP_TOOL_MAX_TOTAL_BYTES as WP_TOOL_MAX_TOTAL_BYTES,
)
from ecom_ops.wp_ops.tools.types import (
    ToolContext,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.types import (
    ToolLoopResult as ToolLoopResult,
)
from ecom_ops.wp_ops.tools.validation import (
    _error,
    _error_class,
    _safe_failure,
)
from ecom_ops.wp_ops.tools.validation import (
    _format_results_sv as _format_results_sv,  # noqa: F401
)
from ecom_ops.wp_ops.tools.validation import (
    _parse_tool_call as _parse_tool_call,  # noqa: F401
)
from ecom_ops.wp_ops.tools.validation import (
    _utf8_window as _utf8_window,  # noqa: F401
)
from ecom_ops.wp_ops.tools.workspace import (
    read_workspace_text_files,
    workspace_parent_slug,
)


class WpAgentTools:
    """Server-side WordPress tool dispatcher with strict argument checks."""

    def __init__(self, service: WpOpsService | None = None) -> None:
        self.service = service or WpOpsService()

    def execute(
        self,
        name: str,
        arguments: Any,
        *,
        actor: str,
        sticky_site_id: str | None = None,
        channel: str = "",
        conversation_id: str = "",
    ) -> ToolOutput:
        output = self._execute(
            name,
            arguments,
            actor=actor,
            sticky_site_id=sticky_site_id,
            channel=channel,
            conversation_id=conversation_id,
        )
        args = arguments if isinstance(arguments, dict) else {}
        site_id = output.site_id or args.get("site_id") or sticky_site_id
        target_id = site_id if isinstance(site_id, str) and _SITE_ID.fullmatch(site_id) else None
        plan = output.data.get("plan") if isinstance(output.data, dict) else None
        known = name in WP_TOOL_SCHEMAS or name in ALL_FILE_TOOLS
        plan_id = plan.get("plan_id") if isinstance(plan, dict) else None
        if plan_id is None and isinstance(output.data.get("plan_id"), str):
            plan_id = output.data.get("plan_id")
        try:
            log_action(
                actor=actor,
                action=f"wp_agent_tool_{name}" if known else "wp_agent_tool_rejected",
                target="wp_sites",
                target_id=target_id,
                success=output.data.get("ok") is True,
                details={
                    "tool": name if known else "unknown",
                    "result_class": output.data.get("error_class") or output.data.get("status"),
                    "plan_id": plan_id,
                    "live_calls": output.data.get("live_calls") is True,
                },
            )
        except Exception:
            return ToolOutput(_error("AuditUnavailable"), output.site_id)
        return output

    def _execute(
        self,
        name: str,
        arguments: Any,
        *,
        actor: str,
        sticky_site_id: str | None = None,
        channel: str = "",
        conversation_id: str = "",
    ) -> ToolOutput:
        if not isinstance(arguments, dict):
            return ToolOutput(_error("InvalidToolRequest"))
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            return ToolOutput(_error("InvalidToolRequest"))
        if spec.in_file_schemas:
            return self._execute_file(
                name,
                arguments,
                actor=actor,
                sticky_site_id=sticky_site_id,
            )
        allowed = set(spec.schema.get("properties", {}))
        if set(arguments) - allowed:
            return ToolOutput(_error("InvalidToolArguments"))
        if not spec.requires_site:
            ctx = ToolContext(
                actor=actor,
                sticky_site_id=sticky_site_id,
                channel=channel,
                conversation_id=conversation_id,
            )
            return spec.handler(self, ctx, arguments)

        site_id = arguments.get("site_id", sticky_site_id)
        if not isinstance(site_id, str) or not _SITE_ID.fullmatch(site_id):
            return self._site_required_error(actor)

        site_result = self.service.show_site(site_id, actor=actor)
        if not site_result.ok:
            return ToolOutput(_safe_failure(site_result))
        site_record = (site_result.data or {}).get("site")
        if not isinstance(site_record, dict):
            return ToolOutput(_error("SiteUnavailable"))

        ctx = ToolContext(
            actor=actor,
            site_id=site_id,
            sticky_site_id=sticky_site_id,
            channel=channel,
            conversation_id=conversation_id,
        )
        return spec.handler(self, ctx, arguments, site_result=site_result, site_record=site_record)

    def _execute_file(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: str,
        sticky_site_id: str | None,
    ) -> ToolOutput:
        spec = TOOL_REGISTRY.get(name)
        if spec is None or not spec.in_file_schemas:
            return ToolOutput(_error("InvalidToolRequest"))
        schema = spec.schema

        def _invalid_args() -> ToolOutput:
            err = _error("InvalidToolArguments")
            err["allowed_arguments"] = sorted(schema.get("properties", {}).keys())
            if schema.get("required"):
                err["required_arguments"] = list(schema.get("required", []))
            return ToolOutput(err)

        allowed_keys = set(schema.get("properties", {}))
        if set(arguments) - allowed_keys:
            return _invalid_args()
        required_keys = set(schema.get("required", []))
        cleaned_args = {
            key: value
            for key, value in arguments.items()
            if not (value is None and key not in required_keys)
        }
        missing = [key for key in schema.get("required", []) if key not in cleaned_args]
        if missing:
            return _invalid_args()
        if "plan_id" in cleaned_args:
            plan_id = cleaned_args.get("plan_id")
            expected_plan = _FILE_PLAN if spec.plan_kind == "fe" else _REVIEW_PLAN
            if not isinstance(plan_id, str):
                return _invalid_args()
            cleaned_pid = plan_id.strip().strip("`")
            if not expected_plan.fullmatch(cleaned_pid):
                return _invalid_args()
            cleaned_args["plan_id"] = cleaned_pid

        if not spec.requires_site:
            ctx = ToolContext(actor=actor, sticky_site_id=sticky_site_id)
            return spec.handler(self, ctx, cleaned_args)

        site_id = cleaned_args.get("site_id", sticky_site_id)
        if not isinstance(site_id, str) or not _SITE_ID.fullmatch(site_id):
            return self._site_required_error(actor)
        ctx = ToolContext(actor=actor, site_id=site_id, sticky_site_id=sticky_site_id)
        if spec.workspace_tool:
            return spec.handler(self, ctx, cleaned_args, ops=self._review_plans())
        return spec.handler(self, ctx, cleaned_args)

    # --- Preserved compatibility & monkeypatch delegation hooks ---

    def _site_model(self, site_id: str) -> Site | None:
        try:
            sites = load_sites(
                getattr(self.service, "registry_path", None),
                data_dir=getattr(self.service, "data_dir", None),
            )
        except Exception:
            return None
        return site_by_id(sites, site_id)

    def _site_required_error(self, actor: str) -> ToolOutput:
        err = _error("SiteRequired")
        available: list[str] = []
        try:
            listed = self.service.list_sites(actor=actor)
            if listed.ok and isinstance(listed.data, dict):
                for row in listed.data.get("sites") or []:
                    if isinstance(row, dict):
                        sid = row.get("id")
                        if isinstance(sid, str) and _SITE_ID.fullmatch(sid):
                            available.append(sid)
        except Exception:
            available = []
        if available:
            err["available_sites"] = available[:10]
            if len(available) == 1:
                err["hint"] = (
                    f"Ange site_id='{available[0]}' i nästa verktygsanrop (enda registrerade sajten)."
                )
            else:
                err["hint"] = (
                    f"Ange site_id (tillgängliga: {', '.join(available[:10])}) i nästa verktygsanrop."
                )
        else:
            err["recovery_tool"] = "wp_list_sites"
            err["hint"] = "Ange site_id eller kör wp_list_sites först."
        return ToolOutput(err)

    def _review_plans(self):
        from ecom_ops.wp_ops.review_plan import ReviewPlans

        return ReviewPlans(
            data_dir=self.service.data_dir,
            registry_path=self.service.registry_path,
            clock=self.service.clock,
            transport_factory=self.service.files_factory,
        )

    def _file_result(self, result: WpOpsResult, site_id: str | None) -> ToolOutput:
        if not result.ok:
            failure = _safe_failure(result)
            data = result.data or {}
            if data.get("error_class"):
                failure["error_class"] = _error_class(data.get("error_class")) or failure["error_class"]
            review_path = data.get("review_path")
            if isinstance(review_path, str) and review_path.startswith("/wp/"):
                for key in ("review_path", "plan_id", "site_id", "risk", "login_required", "status"):
                    if key not in data:
                        continue
                    if key == "status" and failure.get("status") == "denied":
                        continue
                    failure[key] = data[key]
            return ToolOutput(failure, site_id)
        data = dict(result.data or {})
        data.setdefault("ok", True)
        for hidden in ("diff", "new_text", "source", "work_text", "files", "comments"):
            data.pop(hidden, None)
        plan = data.get("plan")
        if isinstance(plan, dict):
            data["plan"] = {
                key: value for key, value in plan.items() if key not in {"path", "diff", "new_text"}
            }
        out_site = (
            data.get("site_id")
            if isinstance(data.get("site_id"), str) and _SITE_ID.fullmatch(data["site_id"])
            else site_id
        )
        return ToolOutput(data, site_id=out_site)

    def _scrub_bounded(self, site_obj: Site | None, text: Any, *, max_len: int) -> str:
        return scrub_bounded(site_obj, text, max_len=max_len)

    def _scrub_keep_newlines(self, site_obj: Site | None, text: Any, *, max_bytes: int) -> tuple[str, bool]:
        return scrub_keep_newlines(site_obj, text, max_bytes=max_bytes)

    def _site_for_preview(self, site_id: str, actor: str) -> tuple[Site | None, ToolOutput | None]:
        return site_for_preview(self, site_id, actor)

    def _workspace_product_path(self, site_id: str, plan_id: str) -> str | None:
        return workspace_product_path(self.service.data_dir, site_id, plan_id)

    @staticmethod
    def _extract_html_by_selector(raw_html: str, selector: str) -> tuple[str, int]:
        return extract_html_by_selector(raw_html, selector)

    @staticmethod
    def _strip_html_plain(value: Any) -> str:
        return strip_html_plain(value)

    @staticmethod
    def _parse_product_id(value: Any) -> int | None:
        return parse_product_id(value)

    @staticmethod
    def _parse_numeric_price(raw_price: Any) -> float | None:
        return parse_numeric_price(raw_price)

    @staticmethod
    def _normalize_pii_text(raw: str) -> str:
        return normalize_pii_text(raw)

    @staticmethod
    def _contains_real_pii(value: Any) -> bool:
        """Detect real customer PII (non-synthetic emails, Nordic phones, national IDs) in keys & values."""
        return contains_real_pii(value)

    def _workspace_parent_slug(self, site_id: str, plan_id: str, *, actor: str) -> str | None:
        return workspace_parent_slug(self, site_id, plan_id, actor=actor)

    def _read_workspace_text_files(
        self,
        site_id: str,
        plan_id: str,
        *,
        rel_prefix: str = "",
        allowed_exts: frozenset[str] | None = None,
        site_obj: Site | None = None,
    ) -> tuple[dict[str, str], bool]:
        return read_workspace_text_files(
            self,
            site_id,
            plan_id,
            rel_prefix=rel_prefix,
            allowed_exts=allowed_exts,
            site_obj=site_obj,
        )

    @staticmethod
    def _walk_component_tree(
        transport: Any,
        base_dir: str,
        *,
        max_depth: int,
        max_entries: int,
        max_woo_overrides: int = 0,
    ) -> tuple[list[str], list[dict[str, str]], list[str], bool, bool]:
        return walk_component_tree(
            transport,
            base_dir,
            max_depth=max_depth,
            max_entries=max_entries,
            max_woo_overrides=max_woo_overrides,
        )

    def _safe_site_get_json(
        self,
        site_obj: Site,
        endpoint_path: str,
        *,
        params: dict[str, Any] | None = None,
        use_wp_auth: bool = False,
        timeout: float = 10.0,
    ) -> Any | None:
        """Read-only SSRF-safe HTTPS GET to a registered site's REST endpoint."""
        from ecom_ops.integrations.wp.http_guard import fetch_site_rest_json

        return fetch_site_rest_json(
            site_obj,
            endpoint_path,
            params=params,
            use_wp_auth=use_wp_auth,
            timeout=timeout,
        )

    def _try_woo_client_read(self, site_obj: Site) -> Any | None:
        """Build a live WooCommerceClient for strictly read-only GETs when configured."""
        if not rest_reads_allowed():
            return None
        ck = (os.environ.get("WOO_CONSUMER_KEY") or "").strip()
        cs = (os.environ.get("WOO_CONSUMER_SECRET") or "").strip()
        if not ck or not cs:
            return None
        configured_base = (os.environ.get("WOO_BASE_URL") or "").strip()
        if configured_base and not _same_configured_host(site_obj.canonical_url):
            return None
        try:
            from ecom_ops.integrations.woocommerce import WooCommerceClient

            return WooCommerceClient(
                base_url=site_obj.canonical_url,
                consumer_key=ck,
                consumer_secret=cs,
                timeout=10.0,
            )
        except Exception:
            return None

    @staticmethod
    def _mock_woo_client() -> Any:
        from ecom_ops.integrations.woocommerce import client_from_env

        return client_from_env(use_mock=True)

    @staticmethod
    def _mock_wp_client() -> Any:
        from ecom_ops.integrations import wp_client_from_env

        return wp_client_from_env(use_mock=True)

    @staticmethod
    def _fetch_live_html(site_obj: Site, path: str, *, use_cache: bool = False) -> dict[str, Any]:
        from ecom_ops.integrations.wp_live_preview import fetch_live_html

        if use_cache:
            return fetch_live_html(site_obj, path, use_cache=True)
        return fetch_live_html(site_obj, path)

    @staticmethod
    def _validate_preview_path(path: str) -> str:
        from ecom_ops.integrations.wp_live_preview import validate_preview_path

        return validate_preview_path(path)

    @staticmethod
    def _allowed_site_hosts(site_host: str) -> frozenset[str]:
        from ecom_ops.integrations.wp_live_preview import _allowed_site_hosts

        return _allowed_site_hosts(site_host)

    @staticmethod
    def _static_asset_hosts() -> frozenset[str]:
        from ecom_ops.integrations.wp_live_preview import _STATIC_ASSET_HOSTS

        return _STATIC_ASSET_HOSTS

    def _list_products(self, site_id: str, arguments: dict[str, Any], *, actor: str) -> ToolOutput:
        return handle_list_products(self, ToolContext(actor=actor, site_id=site_id), arguments)

    def _get_product(self, site_id: str, arguments: dict[str, Any], *, actor: str) -> ToolOutput:
        return handle_get_product(self, ToolContext(actor=actor, site_id=site_id), arguments)

    def _list_pages(self, site_id: str, arguments: dict[str, Any], *, actor: str) -> ToolOutput:
        return handle_list_pages(self, ToolContext(actor=actor, site_id=site_id), arguments)

    def _get_page(self, site_id: str, arguments: dict[str, Any], *, actor: str) -> ToolOutput:
        return handle_get_page(self, ToolContext(actor=actor, site_id=site_id), arguments)

    def _store_plan(self, record: dict[str, Any]) -> None:
        _drift_store_plan(self.service.data_dir, record)

    def _load_plans(self) -> list[dict[str, Any]]:
        return _drift_load_plans(self.service.data_dir)

    @staticmethod
    def _read_plan_rows(path: Path) -> list[dict[str, Any]]:
        return _drift_read_plan_rows(path)
