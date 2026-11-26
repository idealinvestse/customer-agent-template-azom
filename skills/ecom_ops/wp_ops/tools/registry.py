"""Declarative WordPress agent tool registry (single source of truth for all 54 tools).

Every WordPress tool registers a frozen :class:`ToolSpec` via :func:`register_tool`.
Schemas, tool-name sets, labels, capability groups, and outcome formatters are all
derived from ``TOOL_REGISTRY`` so drift between dispatch, schemas, progress labels,
and chat capabilities is impossible.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ecom_ops.wp_ops.tools.types import FILE_LOOP_MAX_READS, ToolOutput
from ecom_ops.wp_ops.tools.validation import short_text

OutcomeFn = Callable[[dict[str, Any], int | None], str]


@dataclass(frozen=True)
class ToolSpec:
    """Immutable metadata and handler binding for one WordPress agent tool."""

    name: str
    group: Literal["drift", "file", "workspace"]
    label: str
    short_label: str
    schema: dict[str, Any]
    handler: Callable[..., ToolOutput]
    outcome: OutcomeFn
    in_wp_schemas: bool = False
    in_file_schemas: bool = False
    in_core_file_tools: bool = False
    in_extra_file_tools: bool = False
    requires_site: bool = True
    plan_kind: Literal["fe", "wr"] | None = None
    workspace_tool: bool = False
    mutates_workspace: bool = False
    read_budget: bool = False
    search_budget: bool = False
    deep_phase: bool = False
    untrusted_output: bool = False
    capability_group: Literal["drift_analysis", "workspace", "fe_plan", "preview_catalog", "other"] = "other"


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def count_outcome(
    key: str,
    noun: str,
    fallback: str,
    *,
    truncated_suffix: bool = False,
) -> OutcomeFn:
    """Build an outcome formatter reporting ``len(data[key]) <noun>``."""

    def _fmt(data: dict[str, Any], _reads: int | None) -> str:
        items = data.get(key)
        if isinstance(items, list):
            suffix = " (avkortad)" if truncated_suffix and data.get("truncated") is True else ""
            return f"{len(items)} {noun}{suffix}"
        return fallback

    return _fmt


def status_outcome(prefix: str, *, default_status: str = "uppdaterad") -> OutcomeFn:
    """Build an outcome formatter reporting ``<prefix>: <data['status']>``."""

    def _fmt(data: dict[str, Any], _reads: int | None) -> str:
        st = short_text(data.get("status"), 24) or default_status
        return f"{prefix}: {st}"

    return _fmt


def read_budget_outcome(
    verb: str,
    *,
    fallback: str | None = None,
    with_truncated: bool = False,
    parenthesized: bool = False,
) -> OutcomeFn:
    """Build an outcome formatter including the read-budget counter when available."""

    def _fmt(data: dict[str, Any], reads: int | None) -> str:
        if reads is not None:
            if parenthesized:
                base = f"{verb} (läsning {reads}/{FILE_LOOP_MAX_READS})"
            else:
                base = f"{verb} {reads}/{FILE_LOOP_MAX_READS}"
        else:
            base = fallback or f"{verb} klar"
        if with_truncated and data.get("truncated") is True:
            base += " (avkortad)"
        return base

    return _fmt


def plan_id_outcome(template: str, fallback: str, *, nested_plan: bool = False) -> OutcomeFn:
    """Build an outcome formatter interpolating ``plan_id``."""

    def _fmt(data: dict[str, Any], _reads: int | None) -> str:
        raw_pid = data.get("plan_id")
        if not raw_pid and nested_plan:
            plan = data.get("plan")
            if isinstance(plan, dict):
                raw_pid = plan.get("plan_id")
        pid = short_text(raw_pid, 32) if raw_pid else ""
        return template.format(pid=pid) if pid else fallback

    return _fmt


def default_outcome(data: dict[str, Any], _reads: int | None) -> str:
    st = short_text(data.get("status"), 24)
    return f"status: {st}" if st else "resultat mottaget"


def register_tool(
    *,
    name: str,
    group: Literal["drift", "file", "workspace"],
    label: str,
    short_label: str,
    schema: dict[str, Any],
    outcome: OutcomeFn = default_outcome,
    in_wp_schemas: bool = False,
    in_file_schemas: bool = False,
    in_core_file_tools: bool = False,
    in_extra_file_tools: bool = False,
    requires_site: bool = True,
    plan_kind: Literal["fe", "wr"] | None = None,
    workspace_tool: bool = False,
    mutates_workspace: bool = False,
    read_budget: bool = False,
    search_budget: bool = False,
    deep_phase: bool = False,
    untrusted_output: bool = False,
    capability_group: Literal["drift_analysis", "workspace", "fe_plan", "preview_catalog", "other"] = "other",
) -> Callable[[Callable[..., ToolOutput]], Callable[..., ToolOutput]]:
    """Register a WordPress tool handler in ``TOOL_REGISTRY``."""

    def _decorator(fn: Callable[..., ToolOutput]) -> Callable[..., ToolOutput]:
        if name in TOOL_REGISTRY:
            raise ValueError(f"Duplicate WP tool registration: {name}")
        TOOL_REGISTRY[name] = ToolSpec(
            name=name,
            group=group,
            label=label,
            short_label=short_label,
            schema=schema,
            handler=fn,
            outcome=outcome,
            in_wp_schemas=in_wp_schemas,
            in_file_schemas=in_file_schemas,
            in_core_file_tools=in_core_file_tools,
            in_extra_file_tools=in_extra_file_tools,
            requires_site=requires_site,
            plan_kind=plan_kind,
            workspace_tool=workspace_tool,
            mutates_workspace=mutates_workspace,
            read_budget=read_budget,
            search_budget=search_budget,
            deep_phase=deep_phase,
            untrusted_output=untrusted_output,
            capability_group=capability_group,
        )
        return fn

    return _decorator


_DOMAIN_MODULES = (
    "ecom_ops.wp_ops.tools.drift",
    "ecom_ops.wp_ops.tools.components",
    "ecom_ops.wp_ops.tools.workspace",
    "ecom_ops.wp_ops.tools.preview",
    "ecom_ops.wp_ops.tools.catalog",
    "ecom_ops.wp_ops.tools.pages",
    "ecom_ops.wp_ops.tools.sandbox_edits",
)


def _load_domain_modules() -> None:
    for mod_name in _DOMAIN_MODULES:
        importlib.import_module(mod_name)


_load_domain_modules()

WP_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    spec.name: spec.schema for spec in TOOL_REGISTRY.values() if spec.in_wp_schemas
}
WP_TOOL_NAMES: frozenset[str] = frozenset(WP_TOOL_SCHEMAS)

FILE_TOOL_NAMES: frozenset[str] = frozenset(
    spec.name for spec in TOOL_REGISTRY.values() if spec.in_core_file_tools
)
EXTRA_FILE_TOOLS: frozenset[str] = frozenset(
    spec.name for spec in TOOL_REGISTRY.values() if spec.in_extra_file_tools
)
ALL_FILE_TOOLS: frozenset[str] = FILE_TOOL_NAMES | EXTRA_FILE_TOOLS
FILE_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    spec.name: spec.schema for spec in TOOL_REGISTRY.values() if spec.in_file_schemas
}

FE_PLAN_TOOLS: frozenset[str] = frozenset(
    spec.name for spec in TOOL_REGISTRY.values() if spec.plan_kind == "fe"
)
WORKSPACE_TOOLS: frozenset[str] = frozenset(
    spec.name for spec in TOOL_REGISTRY.values() if spec.workspace_tool
)
WORKSPACE_MUTATING_TOOLS: frozenset[str] = frozenset(
    spec.name for spec in TOOL_REGISTRY.values() if spec.mutates_workspace
)
READ_BUDGET_TOOLS: frozenset[str] = frozenset(
    spec.name for spec in TOOL_REGISTRY.values() if spec.read_budget
)
SEARCH_BUDGET_TOOLS: frozenset[str] = frozenset(
    spec.name for spec in TOOL_REGISTRY.values() if spec.search_budget
)
DEEP_PHASE_TOOLS: frozenset[str] = frozenset(
    spec.name for spec in TOOL_REGISTRY.values() if spec.deep_phase
)
WP_TOOL_LABELS: dict[str, str] = {spec.name: spec.label for spec in TOOL_REGISTRY.values()}
WP_TOOL_SHORT_LABELS: dict[str, str] = {spec.name: spec.short_label for spec in TOOL_REGISTRY.values()}

TOOL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    group: tuple(spec.name for spec in TOOL_REGISTRY.values() if spec.capability_group == group)
    for group in ("drift_analysis", "workspace", "fe_plan", "preview_catalog")
}

__all__ = [
    "ALL_FILE_TOOLS",
    "DEEP_PHASE_TOOLS",
    "EXTRA_FILE_TOOLS",
    "FE_PLAN_TOOLS",
    "FILE_TOOL_NAMES",
    "FILE_TOOL_SCHEMAS",
    "OutcomeFn",
    "READ_BUDGET_TOOLS",
    "SEARCH_BUDGET_TOOLS",
    "TOOL_CAPABILITIES",
    "TOOL_REGISTRY",
    "ToolSpec",
    "WORKSPACE_MUTATING_TOOLS",
    "WORKSPACE_TOOLS",
    "WP_TOOL_LABELS",
    "WP_TOOL_NAMES",
    "WP_TOOL_SCHEMAS",
    "WP_TOOL_SHORT_LABELS",
    "count_outcome",
    "default_outcome",
    "plan_id_outcome",
    "read_budget_outcome",
    "register_tool",
    "status_outcome",
]
