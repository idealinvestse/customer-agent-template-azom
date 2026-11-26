"""Wave 4 integration tests: WP toolchains, wr-* research attachment, sub-budgets, RBAC, and audit."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from _helpers import MOCK_REGISTRY, snapshot_tree, tool_call

from ecom_ops.research import get_research_service
from ecom_ops.wp_ops import agent_tools
from ecom_ops.wp_ops.agent_tools import (
    FILE_LOOP_MAX_CALLS,
    FILE_LOOP_MAX_READS,
    FILE_LOOP_MAX_SEARCHES,
    FILE_LOOP_MAX_SECONDS,
    FILE_LOOP_MAX_TOTAL_BYTES,
    WP_TOOL_NAMES,
    run_bounded_file_tool_loop,
    run_bounded_wp_tool_loop,
)
from ecom_ops.wp_ops.page_review_loop import PAGE_REVIEW_TOOLS, run_bounded_page_review_loop
from ecom_ops.wp_ops.review.risk import tree_risk
from ecom_ops.wp_ops.review.workspace_io import (
    read_diff,
    read_manifest,
    read_research_snapshot,
    unified_diff_text,
    workspace_root,
)
from ecom_ops.wp_ops.review_plan import ReviewPlans


def _forbid_live_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Live network socket is prohibited in mock WP research tests")

    monkeypatch.setattr(socket, "create_connection", _boom)


def test_end_to_end_thankyou_page_research_attach_fork_and_hitl_invariants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    _forbid_live_sockets(monkeypatch)
    _service, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        with_child_theme=True,
        reset_router=True,
    )

    # Run one completed mock Deep Research job for the thank-you page redesign
    rsvc = get_research_service(tmp_path)
    job = rsvc.start_research(
        "WooCommerce tacksida UX konvertering",
        actor="oscar",
        profile_id="design_inspiration",
        depth="quick",
        target_ref="wp_site:mock-staging",
        start_worker=False,
    )
    from ecom_ops.research.engine import ResearchEngine

    ResearchEngine(store=rsvc.store, search_router=rsvc.search_router, fetcher=rsvc.fetcher).run_job(
        job.research_id
    )
    res_id = job.research_id

    captured_prompts: list[list[dict[str, str]]] = []
    loop_steps = iter(
        [
            tool_call(
                "web_fetch_extract",
                {
                    "url": "https://baymard.com/blog/order-confirmation-page-ux",
                    "profile": "design_inspiration",
                },
            ),
            tool_call(
                "wp_build_sample_data",
                {"site_id": "mock-staging", "scenario": "thankyou", "product_count": 2},
            ),
            tool_call(
                "wp_open_review_workspace",
                {
                    "site_id": "mock-staging",
                    "kind": "theme",
                    "slug": "azom-child",
                    "summary": "Ny tacksida med inspirationsresearch",
                    "variant_group": "tacksida-v1",
                },
            ),
            "Tacksidan är förberedd som wr-arbetsyta med inspirationsstöd.",
        ]
    )

    def _complete(msgs: list[dict[str, str]]) -> tuple[str, float]:
        captured_prompts.append(list(msgs))
        return next(loop_steps), 0.0

    loop_res = run_bounded_file_tool_loop(
        tool_call(
            "web_search",
            {
                "query": "woocommerce thank you page UX post purchase conversion",
                "profile": "design_inspiration",
                "locale": "en",
            },
        ),
        [],
        complete=_complete,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert loop_res.handled is True
    assert loop_res.calls == 4
    plan_a = loop_res.active_review_meta["latest_plan_id"]
    assert plan_a.startswith("wr-")

    # Create zero-PII thank-you page draft in wr-A
    draft = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_a,
            "slug": "tacksida",
            "title": "Tack för din beställning",
            "page_type": "thankyou",
            "body_html": (
                '<section class="ty-card"><h1>Tack för din beställning #AZ-TEST-1042</h1>'
                "<p>Leveransuppdatering skickas till test@example.invalid.</p>"
                '<a href="/setup-guide/">Monteringsguide</a></section>'
            ),
            "page_css": ".ty-card { max-width: 960px; margin: 24px auto; }",
            "focus_selector": ".ty-card",
            "synthetic_context": {
                "order_number": "AZ-TEST-1042",
                "customer_email": "test@example.invalid",
            },
        },
        actor="oscar",
    )
    assert draft.data["ok"] is True
    assert draft.data["risk"] == "low"

    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    before_plan = plans.get(plan_a, site_id="mock-staging")
    assert before_plan is not None
    before_sha = before_plan["work_sha256"]
    ws_a = workspace_root(tmp_path, "mock-staging", plan_a)
    before_diff = unified_diff_text(snapshot_tree(ws_a / "base"), snapshot_tree(ws_a / "work"))
    before_diff_file = read_diff(ws_a)
    before_risk = tree_risk("azom-child", sorted(snapshot_tree(ws_a / "work").items()))

    # Attach completed research brief via wp_workspace_attach_research inside a file-tool turn
    attach_steps = iter(["Research kopplad till arbetsytan."])
    attach_loop = run_bounded_file_tool_loop(
        tool_call(
            "wp_workspace_attach_research",
            {"site_id": "mock-staging", "plan_id": plan_a, "research_id": res_id},
        ),
        [],
        complete=lambda _m: (next(attach_steps), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert attach_loop.handled is True
    assert attach_loop.active_review_meta is not None
    assert res_id in attach_loop.active_review_meta["research_ids"]

    # Sibling research/ dir exists outside work/, while work_sha256, tree_risk, diff, and HITL stay untouched
    after_plan = plans.get(plan_a, site_id="mock-staging")
    assert after_plan is not None
    assert after_plan["work_sha256"] == before_sha
    assert after_plan["research_ids"] == [res_id]
    assert unified_diff_text(snapshot_tree(ws_a / "base"), snapshot_tree(ws_a / "work")) == before_diff
    assert read_diff(ws_a) == before_diff_file
    assert tree_risk("azom-child", sorted(snapshot_tree(ws_a / "work").items())) == before_risk
    assert (ws_a / "research" / f"{res_id}.json").is_file()
    assert not (ws_a / "work" / "research").exists()
    assert read_manifest(ws_a)["research_ids"] == [res_id]
    snap = read_research_snapshot(ws_a, res_id)
    assert snap is not None and snap["research_id"] == res_id

    # Staging HITL gates remain intact and unaffected
    nl_denied = plans.decide_text("ja godkänn", actor="oscar")
    assert nl_denied.ok is False and nl_denied.data["error_class"] == "TextNotApproval"
    desc_a = plans.describe_plan("mock-staging", plan_a, actor="oscar")
    assert desc_a.ok is True and desc_a.data["plan"]["research_ids"] == [res_id]
    ready, _reason = plans.staging_readiness(desc_a.data["plan"], changed_count=1)
    assert ready is False  # tests_incomplete in mock mode; research attachment does not bypass checks
    blocked_stage = plans.approve_staging(
        "mock-staging",
        plan_a,
        actor="oscar",
        base_sha256=after_plan["base_sha256"],
        work_sha256=after_plan["work_sha256"],
    )
    assert blocked_stage.ok is False and blocked_stage.data["error_class"] == "TestsNotPassed"

    # Fork inherits research_ids and sibling research/ directory
    forked = tools.execute(
        "wp_workspace_fork",
        {
            "site_id": "mock-staging",
            "plan_id": plan_a,
            "summary": "Tacksida variant B",
            "variant_group": "tacksida-v1",
        },
        actor="oscar",
    )
    assert forked.data["ok"] is True
    plan_b = forked.data["plan_id"]
    assert forked.data["research_ids"] == [res_id]
    ws_b = workspace_root(tmp_path, "mock-staging", plan_b)
    assert (ws_b / "research" / f"{res_id}.json").is_file()
    plan_b_row = plans.get(plan_b, site_id="mock-staging")
    assert plan_b_row is not None and plan_b_row["research_ids"] == [res_id]


def test_web_sub_budget_isolation_cache_and_remaining_time_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    _forbid_live_sockets(monkeypatch)
    assert (
        FILE_LOOP_MAX_CALLS,
        FILE_LOOP_MAX_READS,
        FILE_LOOP_MAX_SEARCHES,
        FILE_LOOP_MAX_SECONDS,
        FILE_LOOP_MAX_TOTAL_BYTES,
    ) == (16, 10, 5, 60.0, 81920)

    _service, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        reset_router=True,
    )

    executed: list[tuple[str, dict[str, Any]]] = []
    orig_execute = tools.execute

    def _spy_execute(name: str, arguments: Any, **kwargs: Any) -> Any:
        executed.append((name, dict(arguments or {})))
        return orig_execute(name, arguments, **kwargs)

    monkeypatch.setattr(tools, "execute", _spy_execute)

    # Part A: within-turn web_cache skips duplicate backend execution
    cache_steps = iter(
        [
            tool_call("web_search", {"query": "query one ux", "profile": "design_inspiration"}),
            tool_call(
                "web_fetch_extract",
                {"url": "https://baymard.com/blog/order-confirmation-page-ux", "profile": "design_inspiration"},
            ),
            tool_call(
                "web_fetch_extract",
                {"url": "https://baymard.com/blog/order-confirmation-page-ux", "profile": "design_inspiration"},
            ),
            "Cache klar.",
        ]
    )
    cache_res = run_bounded_file_tool_loop(
        tool_call("web_search", {"query": "query one ux", "profile": "design_inspiration"}),
        [],
        complete=lambda _m: (next(cache_steps), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert cache_res.handled is True
    assert [args["query"] for name, args in executed if name == "web_search"] == ["query one ux"]
    assert [args["url"] for name, args in executed if name == "web_fetch_extract"] == [
        "https://baymard.com/blog/order-confirmation-page-ux"
    ]

    # Part B: 3 distinct web_search succeed + 4th hits WebSearchBudgetExceeded;
    # 2 distinct web_fetch_extract succeed + 3rd hits WebFetchBudgetExceeded
    executed.clear()
    steps = iter(
        [
            tool_call("web_search", {"query": "query two ux", "profile": "design_inspiration"}),
            tool_call("web_search", {"query": "query three ux", "profile": "design_inspiration"}),
            tool_call("web_search", {"query": "query four ux", "profile": "design_inspiration"}),
            tool_call(
                "web_fetch_extract",
                {"url": "https://baymard.com/blog/order-confirmation-page-ux", "profile": "design_inspiration"},
            ),
            tool_call(
                "web_fetch_extract",
                {"url": "https://www.nngroup.com/articles/confirmation-pages/", "profile": "design_inspiration"},
            ),
            tool_call(
                "web_fetch_extract",
                {"url": "https://www.smashingmagazine.com/2025/02/nordic-ecommerce-post-purchase-ux/", "profile": "design_inspiration"},
            ),
            "Klar inom sub-budget.",
        ]
    )
    res = run_bounded_file_tool_loop(
        tool_call("web_search", {"query": "query one ux", "profile": "design_inspiration"}),
        [],
        complete=lambda _m: (next(steps), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert res.handled is True
    search_execs = [args["query"] for name, args in executed if name == "web_search"]
    assert search_execs == ["query one ux", "query two ux", "query three ux"]
    fetch_execs = [args["url"] for name, args in executed if name == "web_fetch_extract"]
    assert len(fetch_execs) == 2

    errors = [row.get("error_class") for row in res.results if not row.get("ok")]
    assert "WebSearchBudgetExceeded" in errors
    assert "WebFetchBudgetExceeded" in errors

    # Remaining-time guard (< 8.0s left): blocks web_search with WebTimeBudgetInsufficient while local tools still work
    ticks = iter([100.0, 154.0, 154.0, 154.0])
    monkeypatch.setattr(agent_tools.time, "monotonic", lambda: next(ticks, 154.0))
    time_steps = iter(
        [
            tool_call("web_search", {"query": "late web search", "profile": "design_inspiration"}),
            "Avslutade utan webbsökning.",
        ]
    )
    time_res = run_bounded_file_tool_loop(
        tool_call("wp_list_workspaces", {"site_id": "mock-staging"}),
        [],
        complete=lambda _m: (next(time_steps), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert time_res.handled is True
    assert time_res.results[0]["ok"] is True
    assert time_res.results[1]["ok"] is False
    assert time_res.results[1]["error_class"] == "WebTimeBudgetInsufficient"
    assert "web_research_start" in str(time_res.results[1].get("hint") or "")


def test_untrusted_output_nonce_framing_for_web_and_component_intel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    _forbid_live_sockets(monkeypatch)
    _service, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        reset_router=True,
    )

    seen_messages: list[list[dict[str, str]]] = []
    steps = iter(
        [
            tool_call("wp_component_intel", {"kind": "plugin", "slug": "woocommerce"}),
            "Färdig med inramade verktygssvar.",
        ]
    )

    def _complete(msgs: list[dict[str, str]]) -> tuple[str, float]:
        seen_messages.append([dict(m) for m in msgs])
        return next(steps), 0.0

    res = run_bounded_file_tool_loop(
        tool_call("web_search", {"query": "woocommerce order confirmation hooks", "profile": "technical_docs"}),
        [],
        complete=_complete,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert res.handled is True
    final_msgs = seen_messages[-1]

    web_msgs = [m for m in final_msgs if m.get("role") == "user" and "tool=web_search" in m.get("content", "")]
    intel_msgs = [
        m for m in final_msgs if m.get("role") == "user" and "tool=wp_component_intel" in m.get("content", "")
    ]
    assert len(web_msgs) == 1
    assert len(intel_msgs) == 1
    for msg in (web_msgs[0], intel_msgs[0]):
        content = msg["content"]
        assert "<<<UNTRUSTED_TOOL_DATA " in content
        assert "<<<END_UNTRUSTED_TOOL_DATA " in content
        assert "nonce=" in content
    # Must NEVER be injected as system messages
    assert not any(
        m.get("role") == "system" and "UNTRUSTED_TOOL_DATA" in m.get("content", "")
        for m in final_msgs
    )


def test_audit_log_is_metadata_only_for_web_and_research_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    _forbid_live_sockets(monkeypatch)
    _service, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        reset_router=True,
    )
    secret_query = "unique-audit-sentinel-query-xyzzy"
    out = tools.execute(
        "web_search",
        {"query": secret_query, "profile": "design_inspiration"},
        actor="oscar",
    )
    assert out.data["ok"] is True

    audit_file = tmp_path / "audit.jsonl"
    raw_audit = audit_file.read_text(encoding="utf-8")
    assert secret_query not in raw_audit
    assert "baymard.com/blog/order-confirmation-page-ux" not in raw_audit

    entries = [json.loads(line) for line in raw_audit.splitlines() if line.strip()]
    wp_entry = next(e for e in entries if e["action"] == "wp_agent_tool_web_search")
    expected_hash = hashlib.sha256(secret_query.encode("utf-8")).hexdigest()[:16]
    assert wp_entry["details"]["query_hash"] == expected_hash
    assert wp_entry["details"]["profile_id"] == "design_inspiration"
    assert wp_entry["details"]["result_count"] >= 1
    assert wp_entry["details"]["evidence_status"]


def test_jonatan_viewer_rbac_denies_wp_research_and_attach_allows_general_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    _forbid_live_sockets(monkeypatch)
    _service, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        with_child_theme=True,
        reset_router=True,
    )

    # Jonatan (viewer) can perform general non-WP web_search
    gen_search = tools.execute(
        "web_search",
        {"query": "postnord leveranstider", "profile": "support_fact_check"},
        actor="jonatan",
    )
    assert gen_search.data["ok"] is True

    # Oscar opens a workspace + starts a wp:* research job
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Test"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]

    wp_job = tools.execute(
        "web_research_start",
        {
            "topic": "WooCommerce thankyou hooks",
            "profile": "technical_docs",
            "target_ref": f"wp:mock-staging:{plan_id}",
        },
        actor="oscar",
    )
    assert wp_job.data["ok"] is True
    wp_rid = wp_job.data["research_id"]

    # Jonatan is strictly denied wp:* and wp_site:* research start, viewing wp:* job, and attach_research
    for bad_ref in (f"wp:mock-staging:{plan_id}", "wp_site:mock-staging"):
        denied_start = tools.execute(
            "web_research_start",
            {"topic": "WP research", "target_ref": bad_ref},
            actor="jonatan",
        )
        assert denied_start.data["ok"] is False
        assert denied_start.data["error_class"] == "AccessDenied"

    for read_tool in ("web_research_status", "web_research_get"):
        denied_view = tools.execute(read_tool, {"research_id": wp_rid}, actor="jonatan")
        assert denied_view.data["ok"] is False
        assert denied_view.data["error_class"] == "NotFoundOrDenied"

    denied_attach = tools.execute(
        "wp_workspace_attach_research",
        {"site_id": "mock-staging", "plan_id": plan_id, "research_id": wp_rid},
        actor="jonatan",
    )
    assert denied_attach.data["ok"] is False
    assert denied_attach.data["error_class"] == "AccessDenied"


def test_drift_and_page_review_loops_remain_untouched_with_zero_web_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wp_service_tools
) -> None:
    _forbid_live_sockets(monkeypatch)
    _service, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        reset_router=True,
    )

    web_names = {
        "web_search",
        "web_fetch_extract",
        "web_research_start",
        "web_research_status",
        "web_research_get",
        "wp_workspace_attach_research",
    }
    assert web_names.isdisjoint(WP_TOOL_NAMES)
    assert web_names.isdisjoint(PAGE_REVIEW_TOOLS)

    # Drift loop does not invoke web_search as a valid drift tool
    drift_res = run_bounded_wp_tool_loop(
        tool_call("web_search", {"query": "should not run in drift loop"}),
        [{"role": "user", "content": "status"}],
        complete=lambda _m: ("Klar.", 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
        wp_intent=True,
    )
    assert drift_res.handled is False
    assert drift_res.calls == 0

    # Page review loop refuses attempted web_search and runs only the 4 PAGE_REVIEW_TOOLS
    page_res = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        data_dir=tmp_path,
        attempted=[("web_search", {"query": "should not run"})],
    )
    assert len(page_res.steps) == len(PAGE_REVIEW_TOOLS)
    assert len(page_res.refused) == 1
    assert page_res.refused[0]["tool"] == "web_search"
    assert page_res.refused[0]["executed"] is False
    assert page_res.refused[0]["reason"] == "tool_refused"
