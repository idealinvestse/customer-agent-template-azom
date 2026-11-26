"""Focused unit tests for wp_ops/tools/loop_state.py and wp_ops/tools/loops.py."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ecom_ops.wp_ops import agent_tools
from ecom_ops.wp_ops.tools.loop_state import (
    AttachmentTracker,
    LoopBudget,
    LoopPolicy,
    LoopResultBuilder,
    ReviewMetaTracker,
    compact_older_file_messages,
    primary_attachment,
    read_cache_key,
)
from ecom_ops.wp_ops.tools.loops import (
    run_bounded_file_tool_loop,
    run_bounded_wp_tool_loop,
    tool_name_from,
)
from ecom_ops.wp_ops.tools.registry import SEARCH_BUDGET_TOOLS
from ecom_ops.wp_ops.tools.types import ToolOutput


def test_loop_budget_limits_recovery_and_bytes() -> None:
    policy = LoopPolicy(
        max_calls=3,
        max_seconds=10.0,
        max_result_bytes=120,
        max_total_bytes=200,
        max_reads=1,
        max_searches=1,
        max_recovery_retries=2,
        step_prefix="wp-file",
    )
    budget = LoopBudget(policy=policy)

    # Steers cap at 3 cumulative across calls.
    assert budget.accept_steers(["a", "b"]) == ["a", "b"]
    assert budget.accept_steers(["c", "d"]) == ["c"]
    assert budget.accept_steers(["e"]) == []

    # Recovery errors consume recovery_retries, not main calls/reads.
    assert budget.record_file_call(
        "wp_read_theme_plugin_file",
        ok=False,
        error_class="FileMissing",
        elapsed=0.5,
        using_preview_bonus=False,
    )
    assert budget.recovery_retries == 1
    assert budget.calls == 0
    assert budget.reads == 0

    # Normal read increments calls and reads.
    assert not budget.record_file_call(
        "wp_read_theme_plugin_file",
        ok=True,
        error_class=None,
        elapsed=0.5,
        using_preview_bonus=False,
    )
    assert budget.calls == 1
    assert budget.reads == 1
    pre = budget.check_file_pre_call("wp_read_theme_plugin_file")
    assert pre is not None and "läsgräns" in pre[0] and pre[2] is False

    # Normal search increments searches and triggers search limit on next check.
    assert SEARCH_BUDGET_TOOLS
    search_tool = sorted(SEARCH_BUDGET_TOOLS)[0]
    budget.record_file_call(
        search_tool,
        ok=True,
        error_class=None,
        elapsed=0.2,
        using_preview_bonus=False,
    )
    pre_search = budget.check_file_pre_call(search_tool)
    assert pre_search is not None and "sökgräns" in pre_search[0]

    # Preview bonus does not increment calls or recovery_retries.
    budget.record_file_call(
        "wp_preview_page",
        ok=True,
        error_class=None,
        elapsed=0.3,
        using_preview_bonus=True,
    )
    assert budget.calls == 2

    # Result size and total byte caps fail closed.
    big_out, _ = budget.enforce_result_size(
        ToolOutput({"ok": True, "blob": "x" * 200}, site_id="azom-se"),
        fallback_site_id="azom-se",
    )
    assert big_out.data["error_class"] == "ToolResultTooLarge"
    budget.total_bytes = 190
    over_out, _ = budget.enforce_result_size(
        ToolOutput({"ok": True, "small": "payload"}, site_id="azom-se"),
        fallback_site_id="azom-se",
    )
    assert over_out.data["error_class"] == "ToolResultBudgetExceeded"


def test_review_meta_and_attachment_trackers(tmp_path: Path) -> None:
    comp_file = tmp_path / "comparison.png"
    comp_file.write_bytes(b"png")

    class _FakePlans:
        def get(self, plan_id: str, *, site_id: str):
            return {"plan_id": plan_id, "site_id": site_id, "variant_group": "hero-ab"}

        def build_variant_comparison(self, site_id: str, plan_ids: list[str]) -> Path:
            assert site_id == "azom-se"
            assert len(plan_ids) == 2
            return comp_file

    meta = ReviewMetaTracker()
    assert meta.build(site_id="azom-se", attachments=[]) is None

    meta.record(
        "wp_open_review_workspace",
        {"plan_id": "wr-0123456789ab", "kind": "theme", "slug": "astra-child"},
        {
            "ok": True,
            "plan_id": "wr-0123456789ab",
            "variant_group": "hero-ab",
            "product_id": 42,
            "changed_files": [{"path": "style.css"}],
        },
        site_id="azom-se",
        review_plans_fn=_FakePlans,
    )
    meta.record(
        "wp_preview_page",
        {"plan_id": "wr-fedcba987654", "focus_selector": ".hero-cta"},
        {
            "ok": True,
            "plan_id": "wr-fedcba987654",
            "path": "/produkt/test/",
            "page_draft": {"slug": "kampanj", "title": "Kampanj", "page_type": "landing"},
        },
        site_id="azom-se",
        review_plans_fn=_FakePlans,
    )
    built = meta.build(site_id="azom-se", attachments=["a.png"])
    assert built is not None
    assert built["plan_ids"] == ["wr-0123456789ab", "wr-fedcba987654"]
    assert built["latest_plan_id"] == "wr-fedcba987654"
    assert built["variant_group"] == "hero-ab"
    assert built["product_id"] == 42
    assert built["preview_path"] == "/produkt/test/"
    assert built["focus_selector"] == ".hero-cta"
    assert built["theme_slug"] == "astra-child"
    assert built["changed_files"] == ["style.css"]
    assert built["page_draft"]["slug"] == "kampanj"

    att = AttachmentTracker()
    att.record(
        "wp_preview_page",
        {"plan_id": "wr-0123456789ab"},
        {"ok": True, "plan_id": "wr-0123456789ab"},
        ("/tmp/work-desktop.png", "/tmp/compare-desktop.png"),
        site_id="azom-se",
        variant_group=meta.variant_group,
        plan_variant_groups=meta.plan_variant_groups,
        review_plans_fn=_FakePlans,
    )
    assert primary_attachment(["/tmp/work-desktop.png", "/tmp/compare-desktop.png"]) == "/tmp/compare-desktop.png"
    att.record(
        "wp_preview_page",
        {"plan_id": "wr-fedcba987654"},
        {"ok": True, "plan_id": "wr-fedcba987654"},
        ("/tmp/plan2-preview-desktop.png",),
        site_id="azom-se",
        variant_group=meta.variant_group,
        plan_variant_groups=meta.plan_variant_groups,
        review_plans_fn=_FakePlans,
    )
    assert att.attachments[0] == str(comp_file)
    assert "/tmp/compare-desktop.png" in att.attachments
    assert "/tmp/plan2-preview-desktop.png" in att.attachments


def test_loop_result_builder_invalidates_review_stage_on_mutation() -> None:
    policy = LoopPolicy(max_calls=5, max_seconds=30.0, max_result_bytes=4096, max_total_bytes=8192)
    builder = LoopResultBuilder(budget=LoopBudget(policy=policy), site_id="azom-se", include_file_fields=True)
    builder.update_from_output(
        "wp_workspace_run_checks",
        ToolOutput(
            {"ok": True, "plan_id": "wr-0123456789ab"},
            site_id="azom-se",
            review_stage_plan_id="wr-0123456789ab",
        ),
        track_stage_plans=True,
    )
    assert builder.review_stage_plan_id == "wr-0123456789ab"

    builder.update_from_output(
        "wp_workspace_patch_file",
        ToolOutput({"ok": True, "plan_id": "wr-0123456789ab"}, site_id="azom-se"),
        track_stage_plans=True,
    )
    assert builder.review_stage_plan_id is None

    res = builder.build(True, "Klart", model_error=True, exc=RuntimeError("boom"))
    assert res.model_error is True
    assert res.model_error_class == "RuntimeError"
    assert res.review_stage_plan_id is None


def test_facade_monkeypatch_contracts_wp_max_seconds_and_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert agent_tools.run_bounded_wp_tool_loop is run_bounded_wp_tool_loop
    assert agent_tools.run_bounded_file_tool_loop is run_bounded_file_tool_loop
    assert agent_tools.tool_name_from is tool_name_from
    assert read_cache_key("azom-se", {"path": "style.css", "limit": True}) is None

    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": "Untrusted file-tool data. " + '{"k":"' + ("a" * 1500) + '"}'}
        for _ in range(12)
    ]
    compact_older_file_messages(msgs)
    assert "komprimerad tidigare läsning" in msgs[1]["content"]
    assert "komprimerad tidigare läsning" not in msgs[-1]["content"]

    ticks = iter([100.0, 105.0])
    monkeypatch.setattr(agent_tools, "WP_TOOL_MAX_SECONDS", 1.0)
    monkeypatch.setattr(agent_tools.time, "monotonic", lambda: next(ticks))

    fake_tools = SimpleNamespace(
        execute=lambda *a, **kw: ToolOutput({"ok": True, "sites": []}, site_id="azom-se")
    )
    result = agent_tools.run_bounded_wp_tool_loop(
        '<tool_call>{"name":"wp_list_sites","arguments":{}}</tool_call>',
        [{"role": "user", "content": "lista sajter"}],
        complete=lambda _m: ("klart", 0.0),
        actor="oscar",
        channel="dashboard",
        conversation_id="c-1",
        tools=fake_tools,
        wp_intent=True,
    )
    assert result.handled is True
    assert result.results[-1]["error_class"] == "ToolTimeBudgetExceeded"
    assert result.tool_seconds == pytest.approx(5.0)
