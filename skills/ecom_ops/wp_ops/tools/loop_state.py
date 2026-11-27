"""Pure state, budget, attachment, and review-metadata trackers for WP tool loops."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ecom_ops.wp_ops.tools.progress import stage_plan_id, with_review_notice
from ecom_ops.wp_ops.tools.registry import (
    READ_BUDGET_TOOLS,
    SEARCH_BUDGET_TOOLS,
    WEB_FETCH_BUDGET_TOOLS,
    WEB_SEARCH_BUDGET_TOOLS,
    WORKSPACE_MUTATING_TOOLS,
)
from ecom_ops.wp_ops.tools.types import (
    INTEL_SLUG_RE,
    PRODUCT_ID_RE,
    RECOVERY_ERROR_CLASSES,
    RESEARCH_ID_RE,
    REVIEW_PLAN_RE,
    SITE_ID_RE,
    VARIANT_GROUP_RE,
    WEB_FETCH_MAX_RESULT_BYTES,
    WEB_RESEARCH_GET_MAX_RESULT_BYTES,
    WEB_SEARCH_MAX_RESULT_BYTES,
    ToolLoopResult,
    ToolOutput,
)
from ecom_ops.wp_ops.tools.validation import error_payload

TOOL_RESULT_BYTE_CAPS: dict[str, int] = {
    "web_search": WEB_SEARCH_MAX_RESULT_BYTES,
    "web_fetch_extract": WEB_FETCH_MAX_RESULT_BYTES,
    "web_research_get": WEB_RESEARCH_GET_MAX_RESULT_BYTES,
}

MAX_STEERS = 3
MAX_MULTI_VARIANT_ATTACHMENTS = 6
PRIMARY_SHOT_ORDER = (
    "compare-desktop.png",
    "focus-desktop.png",
    "change-desktop.png",
    "preview-desktop.png",
    "work-desktop.png",
)


def primary_attachment(paths: list[str]) -> str | None:
    """Return the highest-priority screenshot attachment from a plan's paths."""
    if not paths:
        return None
    for preferred in PRIMARY_SHOT_ORDER:
        for item in paths:
            if item.replace("\\", "/").endswith(f"/{preferred}") or item == preferred:
                return item
    return paths[0]


def read_cache_key(
    site_id: str | None,
    arguments: dict[str, Any],
) -> tuple[str | int | None, ...] | None:
    """Build a hashable cache key for identical within-turn file reads."""
    parts = (
        site_id,
        arguments.get("kind"),
        arguments.get("slug"),
        arguments.get("path"),
        arguments.get("offset"),
        arguments.get("limit"),
        arguments.get("start_line"),
        arguments.get("end_line"),
    )
    if all(
        part is None or (isinstance(part, (str, int)) and not isinstance(part, bool))
        for part in parts
    ):
        return parts
    return None


def web_cache_key(
    name: str,
    arguments: dict[str, Any],
) -> tuple[Any, ...] | None:
    """Build a hashable cache key for identical within-turn web search/fetch calls."""
    if name == "web_search":
        hints = arguments.get("domain_hints")
        if isinstance(hints, list):
            norm_hints: Any = tuple(str(d).strip().lower() for d in hints)
        elif isinstance(hints, str):
            norm_hints = hints.strip().lower()
        elif hints is None:
            norm_hints = None
        else:
            return None
        parts: tuple[Any, ...] = (
            "web_search",
            str(arguments.get("query") or "").strip().lower(),
            arguments.get("profile") or "deep_general",
            arguments.get("locale"),
            arguments.get("freshness_days"),
            arguments.get("max_results", 5),
            norm_hints,
        )
    elif name == "web_fetch_extract":
        parts = (
            "web_fetch_extract",
            str(arguments.get("url") or "").strip(),
            arguments.get("profile") or "deep_general",
            arguments.get("focus_question"),
            arguments.get("max_chars", 5000),
        )
    else:
        return None
    if all(
        part is None or isinstance(part, (str, int, tuple)) and not isinstance(part, bool)
        for part in parts
    ):
        return parts
    return None


_UNTRUSTED_TAG_LINE_RE = re.compile(r"^\s*<<<\s*(?:END_)?UNTRUSTED_[A-Z0-9_]+[^>]*>>>\s*$")
_UNTRUSTED_TAG_ANY_RE = re.compile(r"<<<\s*(?:END_)?UNTRUSTED_[A-Z0-9_]+[^>]*>>>")


def compact_older_file_messages(messages: list[dict[str, str]]) -> None:
    """Trim older consumed file-tool and web/research payloads after step 10 while keeping the last 4 tool results intact."""
    if len(messages) <= 10:
        return
    cutoff = len(messages) - 8
    for idx in range(1, cutoff):
        msg = messages[idx]
        content = str(msg.get("content") or "")
        if len(content) <= 1200:
            continue
        if content.startswith("Untrusted file-tool data."):
            head, _, tail = content.partition("{")
            msg["content"] = f"{head.strip()} [komprimerad tidigare läsning: {{{tail[:450]}...]"
        elif content.startswith("Untrusted tool data for "):
            first_line, _, rest = content.partition("\n")
            lines = [ln for ln in rest.splitlines() if ln.strip()]
            open_tag = lines[0].strip() if lines and _UNTRUSTED_TAG_LINE_RE.match(lines[0]) else ""
            close_tag = (
                lines[-1].strip()
                if len(lines) >= 2 and _UNTRUSTED_TAG_LINE_RE.match(lines[-1])
                else ""
            )
            inner_lines = lines[1:-1] if (open_tag and close_tag) else lines
            cleaned_body = _UNTRUSTED_TAG_ANY_RE.sub("", "\n".join(inner_lines)).strip()
            snippet = cleaned_body[:450]
            if open_tag and close_tag:
                msg["content"] = (
                    f"{first_line.strip()}\n"
                    f"{open_tag}\n"
                    f"[komprimerad tidigare data: {snippet}...]\n"
                    f"{close_tag}"
                )
            else:
                msg["content"] = f"{first_line.strip()} [komprimerad tidigare data: {snippet}...]"
        elif msg.get("role") == "assistant":
            msg["content"] = content[:800] + "\n...[kortad historik]"


@dataclass(frozen=True)
class LoopPolicy:
    """Immutable per-run budget limits for a bounded WordPress tool loop."""

    max_calls: int
    max_seconds: float
    max_result_bytes: int
    max_total_bytes: int
    max_reads: int | None = None
    max_searches: int | None = None
    max_web_searches: int | None = None
    max_web_fetches: int | None = None
    max_web_seconds: float | None = None
    min_web_remaining_seconds: float = 0.0
    max_recovery_retries: int = 0
    step_prefix: str = "wp"


@dataclass
class LoopBudget:
    """Mutable call, byte, time, recovery, cost, and steer accounting for one loop run."""

    policy: LoopPolicy
    calls: int = 0
    reads: int = 0
    searches: int = 0
    web_searches: int = 0
    web_fetches: int = 0
    web_seconds: float = 0.0
    recovery_retries: int = 0
    total_attempts: int = 0
    total_bytes: int = 0
    tool_seconds: float = 0.0
    followup_cost: float = 0.0
    steer_count: int = 0

    def accept_steers(self, steers: list[str]) -> list[str]:
        """Accept up to ``MAX_STEERS`` cumulative user steers in this turn."""
        if not steers or self.steer_count >= MAX_STEERS:
            return []
        accepted = steers[: max(0, MAX_STEERS - self.steer_count)]
        self.steer_count += len(accepted)
        return accepted

    def add_cost(self, cost: float | None) -> None:
        self.followup_cost += float(cost or 0.0)

    def record_wp_call(self, elapsed: float) -> bool:
        """Record one WP-loop call and return True when the time budget is exceeded."""
        self.tool_seconds += elapsed
        self.calls += 1
        return self.tool_seconds > self.policy.max_seconds

    def check_file_pre_call(self, name: str) -> tuple[str, str, bool] | None:
        """Check file-loop read, search, and wall-time budgets before executing a tool."""
        if (
            self.policy.max_reads is not None
            and name in READ_BUDGET_TOOLS
            and self.reads >= self.policy.max_reads
        ):
            return (
                "Filverktygets läsgräns är nådd. Ingen fil ändrades.",
                f"max {self.policy.max_reads} läsningar",
                False,
            )
        if (
            self.policy.max_searches is not None
            and name in SEARCH_BUDGET_TOOLS
            and self.searches >= self.policy.max_searches
        ):
            return (
                "Filverktygets sökgräns är nådd. Ingen fil ändrades.",
                f"max {self.policy.max_searches} sökningar",
                False,
            )
        if self.tool_seconds > self.policy.max_seconds:
            return (
                "Tidsgränsen för filverktyg nåddes. Ingen fil ändrades.",
                f"max {int(self.policy.max_seconds)} sekunder",
                True,
            )
        return None

    def check_web_pre_call(self, name: str) -> dict[str, Any] | None:
        """Guard web search/fetch sub-budgets and remaining wall-time before executing."""
        is_web_search = name in WEB_SEARCH_BUDGET_TOOLS
        is_web_fetch = name in WEB_FETCH_BUDGET_TOOLS
        if not (is_web_search or is_web_fetch):
            return None
        remaining = self.policy.max_seconds - self.tool_seconds
        if self.policy.min_web_remaining_seconds > 0 and remaining < self.policy.min_web_remaining_seconds:
            err = error_payload("WebTimeBudgetInsufficient")
            err["hint"] = (
                "Mindre än 8 sekunder kvar i denna verktygsvända. "
                "Starta asynkron research med web_research_start eller fortsätt direkt med insamlad data."
            )
            return err
        if self.policy.max_web_seconds is not None and self.web_seconds >= self.policy.max_web_seconds:
            err = error_payload("WebTimeBudgetExceeded")
            err["hint"] = (
                f"Webbsökningens tidsbudget ({int(self.policy.max_web_seconds)}s) är förbrukad. "
                "Fortsätt med insamlad data eller starta web_research_start."
            )
            return err
        if (
            is_web_search
            and self.policy.max_web_searches is not None
            and self.web_searches >= self.policy.max_web_searches
        ):
            err = error_payload("WebSearchBudgetExceeded")
            err["hint"] = (
                f"Max antal webbsökningar i denna vända ({self.policy.max_web_searches}) är nått. "
                "Fortsätt med insamlad data eller starta web_research_start."
            )
            return err
        if (
            is_web_fetch
            and self.policy.max_web_fetches is not None
            and self.web_fetches >= self.policy.max_web_fetches
        ):
            err = error_payload("WebFetchBudgetExceeded")
            err["hint"] = (
                f"Max antal webbsidhämtningar i denna vända ({self.policy.max_web_fetches}) är nått. "
                "Fortsätt med insamlad data eller starta web_research_start."
            )
            return err
        return None

    def record_file_call(
        self,
        name: str,
        *,
        ok: bool,
        error_class: str | None,
        elapsed: float,
        using_preview_bonus: bool,
        web_cache_hit: bool = False,
    ) -> bool:
        """Record one file-loop tool execution with recovery-retry and preview-bonus exemptions."""
        self.tool_seconds += elapsed
        is_recovery = (
            not ok
            and error_class in RECOVERY_ERROR_CLASSES
            and self.recovery_retries < self.policy.max_recovery_retries
            and not using_preview_bonus
        )
        if is_recovery:
            self.recovery_retries += 1
        elif not using_preview_bonus:
            self.calls += 1
            if name in READ_BUDGET_TOOLS:
                self.reads += 1
            if name in SEARCH_BUDGET_TOOLS:
                self.searches += 1
            if not web_cache_hit:
                if name in WEB_SEARCH_BUDGET_TOOLS:
                    self.web_searches += 1
                    self.web_seconds += elapsed
                if name in WEB_FETCH_BUDGET_TOOLS:
                    self.web_fetches += 1
                    self.web_seconds += elapsed
        return is_recovery

    def enforce_result_size(
        self,
        output: ToolOutput,
        *,
        fallback_site_id: str | None,
        tool_name: str | None = None,
    ) -> tuple[ToolOutput, str]:
        """Enforce per-result, per-tool, and cumulative UTF-8 byte caps, returning (output, encoded_json)."""
        encoded = json.dumps(output.data, ensure_ascii=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        max_allowed = self.policy.max_result_bytes
        if tool_name in TOOL_RESULT_BYTE_CAPS:
            max_allowed = min(max_allowed, TOOL_RESULT_BYTE_CAPS[tool_name])
        if size > max_allowed:
            output = ToolOutput(error_payload("ToolResultTooLarge"), site_id=output.site_id)
            encoded = json.dumps(output.data, separators=(",", ":"))
            size = len(encoded.encode("utf-8"))
        if self.total_bytes + size > self.policy.max_total_bytes:
            output = ToolOutput(error_payload("ToolResultBudgetExceeded"), site_id=fallback_site_id)
            encoded = json.dumps(output.data, separators=(",", ":"))
            size = len(encoded.encode("utf-8"))
        self.total_bytes += size
        return output, encoded


@dataclass
class FileLoopState:
    """Mutable per-turn caches and preview/workspace state for ``run_bounded_file_tool_loop``."""

    read_cache: dict[tuple[str | int | None, ...], ToolOutput] = field(default_factory=dict)
    web_cache: dict[tuple[Any, ...], ToolOutput] = field(default_factory=dict)
    patched_plan_id: str | None = None
    preview_ran: bool = False
    preview_bonus_used: bool = False


@dataclass
class ReviewMetaTracker:
    """Tracks active review plan metadata across a file-tool turn."""

    plan_ids: list[str] = field(default_factory=list)
    latest_plan_id: str | None = None
    variant_group: str | None = None
    product_id: int | None = None
    preview_path: str | None = None
    focus_selector: str | None = None
    theme_slug: str | None = None
    changed_files: list[str] = field(default_factory=list)
    research_ids: list[str] = field(default_factory=list)
    page_draft: dict[str, Any] | None = None
    plan_variant_groups: dict[str, str] = field(default_factory=dict)

    def record(
        self,
        tool_name: str,
        args: dict[str, Any],
        data: dict[str, Any],
        *,
        site_id: str | None,
        review_plans_fn: Callable[[], Any] | None = None,
    ) -> None:
        if data.get("ok") is not True:
            return
        self._record_research_ids(args, data)
        raw_pid = self._extract_plan_id(args, data)
        if raw_pid:
            if raw_pid not in self.plan_ids:
                self.plan_ids.append(raw_pid)
            self.latest_plan_id = raw_pid
        self._record_variant_group(
            tool_name,
            args,
            data,
            raw_pid=raw_pid,
            site_id=site_id,
            review_plans_fn=review_plans_fn,
        )
        self._record_product_and_path(tool_name, args, data)
        self._record_focus_and_theme(tool_name, args, data)
        self._record_changed_files(tool_name, args, data)
        self._record_page_draft(data)

    @staticmethod
    def _extract_plan_id(args: dict[str, Any], data: dict[str, Any]) -> str | None:
        raw_pid = data.get("plan_id")
        if isinstance(raw_pid, str) and REVIEW_PLAN_RE.fullmatch(raw_pid):
            return raw_pid
        arg_pid = args.get("plan_id")
        if isinstance(arg_pid, str) and REVIEW_PLAN_RE.fullmatch(arg_pid):
            return arg_pid
        return None

    def _record_variant_group(
        self,
        tool_name: str,
        args: dict[str, Any],
        data: dict[str, Any],
        *,
        raw_pid: str | None,
        site_id: str | None,
        review_plans_fn: Callable[[], Any] | None,
    ) -> None:
        raw_vg = data.get("variant_group") or args.get("variant_group")
        if isinstance(raw_vg, str) and VARIANT_GROUP_RE.fullmatch(raw_vg.strip()):
            clean_vg = raw_vg.strip()
            self.variant_group = clean_vg
            if raw_pid:
                self.plan_variant_groups[raw_pid] = clean_vg
            return
        if (
            raw_pid
            and raw_pid not in self.plan_variant_groups
            and tool_name == "wp_preview_page"
            and review_plans_fn is not None
            and isinstance(site_id, str)
            and SITE_ID_RE.fullmatch(site_id)
        ):
            try:
                prow = review_plans_fn().get(raw_pid, site_id=site_id)
                if isinstance(prow, dict):
                    stored_vg = str(prow.get("variant_group") or "").strip()
                    if stored_vg and VARIANT_GROUP_RE.fullmatch(stored_vg):
                        self.plan_variant_groups[raw_pid] = stored_vg
                        if not self.variant_group:
                            self.variant_group = stored_vg
            except Exception:
                pass

    def _record_product_and_path(
        self,
        tool_name: str,
        args: dict[str, Any],
        data: dict[str, Any],
    ) -> None:
        raw_prod = data.get("product_id") or args.get("product_id")
        if raw_prod is None and isinstance(data.get("product"), dict):
            raw_prod = data["product"].get("id")
        if type(raw_prod) is int and raw_prod > 0:
            self.product_id = raw_prod
        elif isinstance(raw_prod, str) and PRODUCT_ID_RE.fullmatch(raw_prod.strip()):
            self.product_id = int(raw_prod.strip())

        candidate_path = None
        if tool_name in {"wp_preview_page", "wp_inspect_element", "wp_fetch_page_html"}:
            candidate_path = data.get("path")
        if not candidate_path:
            candidate_path = data.get("preview_path") or args.get("product_path")
        if (
            isinstance(candidate_path, str)
            and candidate_path.startswith("/")
            and not candidate_path.startswith("/wp/")
            and len(candidate_path) <= 240
        ):
            self.preview_path = candidate_path

    def _record_focus_and_theme(
        self,
        tool_name: str,
        args: dict[str, Any],
        data: dict[str, Any],
    ) -> None:
        raw_focus = data.get("focus_selector") or args.get("focus_selector")
        if not raw_focus and tool_name == "wp_inspect_element":
            raw_focus = args.get("selector")
        if isinstance(raw_focus, str) and raw_focus.strip() and len(raw_focus.strip()) <= 200:
            self.focus_selector = raw_focus.strip()

        comp_kind = data.get("component_kind") or args.get("kind")
        if comp_kind == "theme" or tool_name == "wp_theme_overview":
            raw_slug = (
                data.get("component_slug")
                or data.get("active_theme")
                or data.get("slug")
                or args.get("slug")
                or args.get("theme_slug")
            )
            if isinstance(raw_slug, str) and INTEL_SLUG_RE.fullmatch(raw_slug):
                self.theme_slug = raw_slug

    def _record_changed_files(
        self,
        tool_name: str,
        args: dict[str, Any],
        data: dict[str, Any],
    ) -> None:
        raw_changed = data.get("changed_files")
        if isinstance(raw_changed, list):
            for entry in raw_changed:
                rel = entry.get("path") if isinstance(entry, dict) else entry
                if isinstance(rel, str) and rel and rel not in self.changed_files:
                    self.changed_files.append(rel)
        elif tool_name in WORKSPACE_MUTATING_TOOLS:
            mut_file = data.get("path") or args.get("path")
            if isinstance(mut_file, str) and mut_file and mut_file not in self.changed_files:
                self.changed_files.append(mut_file)

    def _record_research_ids(self, args: dict[str, Any], data: dict[str, Any]) -> None:
        candidates: list[Any] = [data.get("research_id"), args.get("research_id")]
        raw_list = data.get("research_ids")
        if isinstance(raw_list, list):
            candidates.extend(raw_list)
        plan_obj = data.get("plan")
        if isinstance(plan_obj, dict) and isinstance(plan_obj.get("research_ids"), list):
            candidates.extend(plan_obj["research_ids"])
        for item in candidates:
            if isinstance(item, str):
                rid = item.strip().lower()
                if RESEARCH_ID_RE.fullmatch(rid) and rid not in self.research_ids:
                    self.research_ids.append(rid)

    def _record_page_draft(self, data: dict[str, Any]) -> None:
        raw_draft = data.get("page_draft")
        if isinstance(raw_draft, dict) and raw_draft.get("slug"):
            self.page_draft = {
                "slug": str(raw_draft.get("slug") or ""),
                "title": str(raw_draft.get("title") or ""),
                "page_type": str(raw_draft.get("page_type") or "custom"),
                "preview_kind": str(data.get("preview_kind") or "standalone_page"),
            }

    def build(self, *, site_id: str | None, attachments: list[str]) -> dict[str, Any] | None:
        if not self.plan_ids:
            return None
        meta_out: dict[str, Any] = {
            "site_id": site_id if isinstance(site_id, str) else None,
            "plan_ids": list(self.plan_ids[-10:]),
            "latest_plan_id": self.latest_plan_id or self.plan_ids[-1],
            "variant_group": self.variant_group,
            "product_id": self.product_id,
            "preview_path": self.preview_path,
            "focus_selector": self.focus_selector,
            "theme_slug": self.theme_slug,
            "changed_files": list(self.changed_files[-20:]),
            "preview_images": list(attachments),
        }
        if self.research_ids:
            meta_out["research_ids"] = list(self.research_ids[-10:])
        if self.page_draft is not None:
            meta_out["page_draft"] = dict(self.page_draft)
        return meta_out


@dataclass
class AttachmentTracker:
    """Tracks preview screenshot attachments, multi-variant deduplication, and comparison sheets."""

    attachments: list[str] = field(default_factory=list)
    plan_attachments: dict[str, list[str]] = field(default_factory=dict)
    other_attachments: list[str] = field(default_factory=list)

    def record(
        self,
        tool_name: str,
        args: dict[str, Any],
        data: dict[str, Any],
        new_paths: tuple[str, ...],
        *,
        site_id: str | None = None,
        variant_group: str | None = None,
        plan_variant_groups: dict[str, str] | None = None,
        review_plans_fn: Callable[[], Any] | None = None,
    ) -> None:
        if not new_paths:
            return
        raw_plan = data.get("plan_id") or args.get("plan_id")
        if isinstance(raw_plan, str) and REVIEW_PLAN_RE.fullmatch(raw_plan):
            if tool_name == "wp_preview_page" or raw_plan not in self.plan_attachments:
                self.plan_attachments[raw_plan] = list(new_paths)
            else:
                merged_plan = list(self.plan_attachments[raw_plan])
                for item in new_paths:
                    if item not in merged_plan:
                        merged_plan.append(item)
                self.plan_attachments[raw_plan] = merged_plan
        else:
            for item in new_paths:
                if item not in self.other_attachments:
                    self.other_attachments.append(item)
        self.rebuild(
            tool_name,
            new_paths,
            is_standalone_preview=(data.get("preview_kind") == "standalone_page"),
            site_id=site_id,
            variant_group=variant_group,
            plan_variant_groups=plan_variant_groups,
            review_plans_fn=review_plans_fn,
        )

    def rebuild(
        self,
        latest_tool: str,
        latest_paths: tuple[str, ...],
        *,
        is_standalone_preview: bool = False,
        site_id: str | None = None,
        variant_group: str | None = None,
        plan_variant_groups: dict[str, str] | None = None,
        review_plans_fn: Callable[[], Any] | None = None,
    ) -> None:
        if len(self.plan_attachments) > 1:
            self.attachments = self._merge_multi_plan(
                site_id=site_id,
                variant_group=variant_group,
                plan_variant_groups=plan_variant_groups or {},
                review_plans_fn=review_plans_fn,
            )
            return
        if latest_tool == "wp_preview_page":
            cap = MAX_MULTI_VARIANT_ATTACHMENTS if is_standalone_preview else 4
            self.attachments = (
                list(latest_paths) + [p for p in self.attachments if p not in latest_paths]
            )[:cap]
            return
        for item in latest_paths:
            if len(self.attachments) >= 4:
                break
            if item not in self.attachments:
                self.attachments.append(item)

    def _merge_multi_plan(
        self,
        *,
        site_id: str | None,
        variant_group: str | None,
        plan_variant_groups: dict[str, str],
        review_plans_fn: Callable[[], Any] | None,
    ) -> list[str]:
        merged: list[str] = []
        if (
            isinstance(site_id, str)
            and SITE_ID_RE.fullmatch(site_id)
            and variant_group
            and review_plans_fn is not None
        ):
            grouped_plans = [
                pid
                for pid in self.plan_attachments
                if plan_variant_groups.get(pid) == variant_group
            ]
            if len(grouped_plans) >= 2:
                try:
                    comp_path = review_plans_fn().build_variant_comparison(site_id, grouped_plans)
                    if comp_path is not None and comp_path.is_file():
                        merged.append(str(comp_path))
                except Exception:
                    pass
        for shots in self.plan_attachments.values():
            primary = primary_attachment(shots)
            if primary and primary not in merged and len(merged) < MAX_MULTI_VARIANT_ATTACHMENTS:
                merged.append(primary)
        for shots in reversed(list(self.plan_attachments.values())):
            for item in shots:
                if len(merged) >= MAX_MULTI_VARIANT_ATTACHMENTS:
                    break
                if item not in merged:
                    merged.append(item)
        for item in self.other_attachments:
            if len(merged) >= MAX_MULTI_VARIANT_ATTACHMENTS:
                break
            if item not in merged:
                merged.append(item)
        return merged[:MAX_MULTI_VARIANT_ATTACHMENTS]


@dataclass
class LoopResultBuilder:
    """Assembles ``ToolLoopResult`` instances from shared loop state without duplication."""

    budget: LoopBudget
    site_id: str | None = None
    operation_ids: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    stage_plan_id: str | None = None
    review_stage_plan_id: str | None = None
    attachments: AttachmentTracker = field(default_factory=AttachmentTracker)
    review_meta: ReviewMetaTracker = field(default_factory=ReviewMetaTracker)
    include_file_fields: bool = False

    @property
    def safe_site_id(self) -> str | None:
        return self.site_id if isinstance(self.site_id, str) else None

    def update_from_output(
        self,
        name: str,
        output: ToolOutput,
        *,
        track_stage_plans: bool = False,
    ) -> None:
        self.results.append(output.data)
        if track_stage_plans:
            chosen = stage_plan_id(output.data)
            if chosen:
                self.stage_plan_id = chosen
            if output.review_stage_plan_id and REVIEW_PLAN_RE.fullmatch(output.review_stage_plan_id):
                self.review_stage_plan_id = output.review_stage_plan_id
            elif (
                name in WORKSPACE_MUTATING_TOOLS
                and output.data.get("ok") is True
                and not (name == "wp_workspace_bulk_replace" and output.data.get("dry_run") is True)
                and self.review_stage_plan_id is not None
                and output.data.get("plan_id") == self.review_stage_plan_id
            ):
                # Invalidating staged approval when a workspace mutation modifies the same plan.
                self.review_stage_plan_id = None
        if output.site_id:
            self.site_id = output.site_id
        if output.operation_ids:
            self.operation_ids = list(output.operation_ids[:20])

    def build(
        self,
        handled: bool,
        content: str,
        *,
        model_error: bool = False,
        exc: Exception | None = None,
        append_review_notice: bool = False,
    ) -> ToolLoopResult:
        final_content = with_review_notice(content, self.results) if append_review_notice else content
        err_cls = type(exc).__name__ if exc is not None else None
        err_code = (str(getattr(exc, "code", "") or "") or None) if exc is not None else None
        if not self.include_file_fields:
            return ToolLoopResult(
                handled,
                final_content,
                tuple(self.results),
                self.site_id,
                tuple(self.operation_ids),
                self.budget.calls,
                self.budget.tool_seconds,
                self.budget.followup_cost,
                model_error,
                model_error_class=err_cls,
                model_error_code=err_code,
            )
        return ToolLoopResult(
            handled,
            final_content,
            tuple(self.results),
            self.site_id,
            tuple(self.operation_ids),
            self.budget.calls,
            self.budget.tool_seconds,
            self.budget.followup_cost,
            model_error,
            self.stage_plan_id,
            attachments=tuple(self.attachments.attachments),
            review_stage_plan_id=self.review_stage_plan_id,
            model_error_class=err_cls,
            model_error_code=err_code,
            active_review_meta=self.review_meta.build(
                site_id=self.site_id,
                attachments=self.attachments.attachments,
            ),
        )


__all__ = [
    "AttachmentTracker",
    "FileLoopState",
    "LoopBudget",
    "LoopPolicy",
    "LoopResultBuilder",
    "MAX_MULTI_VARIANT_ATTACHMENTS",
    "MAX_STEERS",
    "PRIMARY_SHOT_ORDER",
    "ReviewMetaTracker",
    "TOOL_RESULT_BYTE_CAPS",
    "compact_older_file_messages",
    "primary_attachment",
    "read_cache_key",
    "web_cache_key",
]
