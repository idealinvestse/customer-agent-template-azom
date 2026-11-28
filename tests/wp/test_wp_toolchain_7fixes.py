"""Tests for the 6 WP toolchain fixes (parsing, recovery retries, hints, sample products, static CSS)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _helpers import MOCK_REGISTRY as REGISTRY
from _helpers import REPO_ROOT as ROOT
from _helpers import tool_call as _call

from ecom_ops.integrations.wp.http_guard import (
    blocked_site_path_reason,
    classify_request,
    validate_preview_path,
)
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import (
    ToolOutput,
    WpAgentTools,
    run_bounded_file_tool_loop,
    run_bounded_wp_tool_loop,
)
from ecom_ops.wp_ops.review_workspace import ReviewError
from ecom_ops.wp_ops.service import WpOpsService
from ecom_ops.wp_ops.tools.types import PAGE_DRAFT_TYPES
from ecom_ops.wp_ops.tools.validation import parse_tool_call


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpOpsService:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "0")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()
    return WpOpsService(registry_path=REGISTRY, data_dir=tmp_path)


# ---------------------------------------------------------------------------
# 1. parse_tool_call: markdown code fences & multiple <tool_call> blocks
# ---------------------------------------------------------------------------


def test_parse_tool_call_strips_markdown_fences_and_picks_first_valid() -> None:
    fenced_json = '<tool_call>\n```json\n{"name": "wp_list_sites", "arguments": {}}\n```\n</tool_call>'
    assert parse_tool_call(fenced_json) == ("wp_list_sites", {})

    fenced_plain = '<tool_call>```\n{"name": "wp_site_status", "arguments": {"site_id": "mock-shop"}}\n```</tool_call>'
    assert parse_tool_call(fenced_plain) == ("wp_site_status", {"site_id": "mock-shop"})

    multi = (
        '<tool_call>{"name": "wp_list_sites", "arguments": {}}</tool_call>\n'
        '<tool_call>{"name": "wp_site_status", "arguments": {"site_id": "mock-shop"}}</tool_call>'
    )
    assert parse_tool_call(multi) == ("wp_list_sites", {})

    broken_then_valid = (
        "<tool_call>{broken json</tool_call>\n"
        '<tool_call>```json\n{"name": "wp_list_sites", "arguments": {"a": 1}}\n```</tool_call>'
    )
    assert parse_tool_call(broken_then_valid) == ("wp_list_sites", {"a": 1})

    # Still rejects malformed JSON, missing name, non-dict arguments
    assert parse_tool_call("<tool_call>{bad}</tool_call>") is None
    assert parse_tool_call('<tool_call>{"arguments": {}}</tool_call>') is None
    assert parse_tool_call('<tool_call>{"name": "wp_list_sites", "arguments": []}</tool_call>') is None
    assert parse_tool_call('<tool_call>{"name": "wp_list_sites"}') is None


# ---------------------------------------------------------------------------
# 2. Malformed tool-call recovery retries in WP & File loops
# ---------------------------------------------------------------------------


def test_wp_loop_recovers_from_malformed_tool_call_and_aborts_after_exhaustion() -> None:
    class FakeTools:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def execute(
            self, name: str, arguments: dict[str, Any], *, actor: str, sticky_site_id: str | None = None,
            channel: str = "", conversation_id: str = "",
        ) -> ToolOutput:
            del arguments, actor, sticky_site_id, channel, conversation_id
            self.calls.append(name)
            return ToolOutput({"ok": True, "status": "ok", "site_id": "mock-shop"}, "mock-shop")

    tools = FakeTools()
    responses = iter(
        [
            _call("wp_list_sites"),
            "Klar!",
        ]
    )
    messages: list[dict[str, str]] = []
    res = run_bounded_wp_tool_loop(
        "<tool_call>{invalid json</tool_call>",
        messages,
        complete=lambda _msgs: (next(responses), 0.001),
        actor="agent",
        channel="telegram",
        conversation_id="telegram:1",
        tools=tools,
        wp_intent=True,
    )
    assert res.handled is True
    assert res.content == "Klar!"
    assert tools.calls == ["wp_list_sites"]
    assert any("ogiltig JSON eller saknade </tool_call>" in m["content"] for m in messages if m["role"] == "user")

    # Exhausting 2 retries aborts cleanly
    exhausted_responses = iter(["<tool_call>{bad1}</tool_call>", "<tool_call>{bad2}</tool_call>"])
    res_fail = run_bounded_wp_tool_loop(
        "<tool_call>{bad0}</tool_call>",
        [],
        complete=lambda _msgs: (next(exhausted_responses), 0.0),
        actor="agent",
        channel="telegram",
        conversation_id="telegram:2",
        tools=tools,
        wp_intent=True,
    )
    assert res_fail.handled is True
    assert "kunde inte valideras" in res_fail.content


def test_file_loop_recovers_from_malformed_tool_call_and_aborts_after_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _setup_env(tmp_path, monkeypatch)
    tools = WpAgentTools(svc)
    responses = iter(
        [
            "<tool_call>{broken json</tool_call>",
            _call("wp_list_sites"),
            "Färdigt!",
        ]
    )
    messages: list[dict[str, str]] = []
    res = run_bounded_file_tool_loop(
        _call("wp_list_sites"),
        messages,
        complete=lambda _msgs: (next(responses), 0.001),
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:10",
        tools=tools,
        allow_wp_first=True,
    )
    assert res.handled is True
    assert res.content == "Färdigt!"
    assert res.calls == 2

    # Exhausting 2 retries in file loop
    exhausted = iter(
        [
            "<tool_call>{broken1</tool_call>",
            "<tool_call>{broken2</tool_call>",
            "<tool_call>{broken3</tool_call>",
        ]
    )
    res_fail = run_bounded_file_tool_loop(
        _call("wp_list_sites"),
        [],
        complete=lambda _msgs: (next(exhausted), 0.0),
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:11",
        tools=tools,
        allow_wp_first=True,
    )
    assert res_fail.handled is True
    assert "kunde inte valideras" in res_fail.content


# ---------------------------------------------------------------------------
# 3. _invalid_args enriches unexpected_arguments, missing_arguments, and hint
# ---------------------------------------------------------------------------


def test_invalid_args_reports_unexpected_and_missing_keys_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _setup_env(tmp_path, monkeypatch)
    tools = WpAgentTools(svc)

    out_unexp = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-shop", "kind": "theme", "summary": "Test", "bogus_key": 123},
        actor="oscar",
    )
    assert out_unexp.data["ok"] is False
    assert out_unexp.data["error_class"] == "InvalidToolArguments"
    assert out_unexp.data["unexpected_arguments"] == ["bogus_key"]
    assert "bogus_key" in out_unexp.data["hint"]

    out_missing = tools.execute(
        "wp_workspace_page_draft",
        {"site_id": "mock-shop", "slug": "tack"},
        actor="oscar",
    )
    assert out_missing.data["ok"] is False
    assert out_missing.data["error_class"] == "InvalidToolArguments"
    assert out_missing.data["missing_arguments"] == ["plan_id", "title", "body_html"]
    assert "plan_id" in out_missing.data["hint"]


# ---------------------------------------------------------------------------
# 4. handle_workspace_page_draft Swedish hints on all InvalidToolArguments
# ---------------------------------------------------------------------------


def test_workspace_page_draft_returns_swedish_hints_for_all_invalid_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _setup_env(tmp_path, monkeypatch)
    tools = WpAgentTools(svc)
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-shop", "kind": "theme", "summary": "Page draft hints"},
        actor="oscar",
    )
    plan_id = opened.data["plan_id"]

    base = {
        "site_id": "mock-shop",
        "plan_id": plan_id,
        "slug": "tack",
        "title": "Tack",
        "body_html": "<p>Hej</p>",
    }

    bad_title = tools.execute("wp_workspace_page_draft", {**base, "title": "   "}, actor="oscar")
    assert bad_title.data["error_class"] == "InvalidToolArguments"
    assert bad_title.data.get("hint")

    bad_type = tools.execute("wp_workspace_page_draft", {**base, "page_type": "invalid_type"}, actor="oscar")
    assert bad_type.data["error_class"] == "InvalidToolArguments"
    assert bad_type.data["allowed_page_types"] == sorted(PAGE_DRAFT_TYPES)
    for ptype in PAGE_DRAFT_TYPES:
        assert ptype in bad_type.data["hint"]

    bad_body = tools.execute("wp_workspace_page_draft", {**base, "body_html": "   "}, actor="oscar")
    assert bad_body.data["error_class"] == "InvalidToolArguments"
    assert bad_body.data.get("hint")

    bad_css = tools.execute("wp_workspace_page_draft", {**base, "page_css": "a\x00b"}, actor="oscar")
    assert bad_css.data["error_class"] == "InvalidToolArguments"
    assert bad_css.data.get("hint")

    bad_focus = tools.execute("wp_workspace_page_draft", {**base, "focus_selector": 123}, actor="oscar")
    assert bad_focus.data["error_class"] == "InvalidToolArguments"
    assert bad_focus.data.get("hint")

    bad_ctx = tools.execute("wp_workspace_page_draft", {**base, "synthetic_context": "not-a-dict"}, actor="oscar")
    assert bad_ctx.data["error_class"] == "InvalidToolArguments"
    assert bad_ctx.data.get("hint")


# ---------------------------------------------------------------------------
# 5. handle_build_sample_data filters custom orders and prioritizes images/prices
# ---------------------------------------------------------------------------


def test_build_sample_data_filters_custom_orders_and_prioritizes_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _setup_env(tmp_path, monkeypatch)
    tools = WpAgentTools(svc)
    seen_limits: list[int] = []

    def fake_list_products(site_id: str, args: dict[str, Any], *, actor: str) -> ToolOutput:
        del site_id, actor
        seen_limits.append(int(args["limit"]))
        return ToolOutput(
            {
                "ok": True,
                "products": [
                    {
                        "id": 10,
                        "name": "Kundebestilling - Spesial",
                        "slug": "kundebestilling",
                        "price": "500",
                        "image_url": "https://mock.invalid/custom.jpg",
                    },
                    {
                        "id": 11,
                        "name": "Custom Order Item",
                        "slug": "custom-order",
                        "price": "999",
                        "image_url": "https://mock.invalid/custom2.jpg",
                    },
                    {
                        "id": 12,
                        "name": "Utan Bild Produkt",
                        "slug": "utan-bild",
                        "price": "490",
                        "image_url": "",
                    },
                    {
                        "id": 13,
                        "name": "Trådlös CarPlay Adapter",
                        "slug": "carplay-adapter",
                        "price": "1490",
                        "image_url": "https://mock.invalid/carplay.jpg",
                    },
                    {
                        "id": 14,
                        "name": "DAB+ Antenn",
                        "slug": "dab-antenn",
                        "price": "390",
                        "image_url": "https://mock.invalid/dab.jpg",
                    },
                ],
            }
        )

    monkeypatch.setattr(tools, "_list_products", fake_list_products)
    monkeypatch.setattr(
        tools,
        "_get_product",
        lambda site_id, args, *, actor: ToolOutput({"ok": False}),
    )

    out = tools.execute("wp_build_sample_data", {"site_id": "mock-shop", "product_count": 2}, actor="jonatan")
    assert out.data["ok"] is True
    assert seen_limits == [12]
    names = [p["name"] for p in out.data["products"]]
    assert names == ["Trådlös CarPlay Adapter", "DAB+ Antenn"]

    # Fallback when ALL products lack images and only custom orders exist
    def fake_only_custom(site_id: str, args: dict[str, Any], *, actor: str) -> ToolOutput:
        del site_id, args, actor
        return ToolOutput(
            {
                "ok": True,
                "products": [
                    {"id": 20, "name": "Spesiell ordre", "slug": "spesiell", "price": "", "image_url": ""},
                ],
            }
        )

    monkeypatch.setattr(tools, "_list_products", fake_only_custom)
    fallback_out = tools.execute(
        "wp_build_sample_data", {"site_id": "mock-shop", "product_count": 1}, actor="jonatan"
    )
    assert fallback_out.data["ok"] is True
    assert fallback_out.data["products"][0]["name"] == "Spesiell ordre"


# ---------------------------------------------------------------------------
# 6. http_guard: static assets under /wp-content/ & /wp-includes/ exempt from /cart & /checkout
# ---------------------------------------------------------------------------


def test_http_guard_allows_wp_static_assets_with_cart_or_checkout_in_name() -> None:
    ok_css, reason_css = classify_request(
        "GET",
        "https://azom.no/wp-content/plugins/cartflows/assets/css/checkout.css?ver=1.2.3",
        site_host="azom.no",
    )
    assert ok_css is True
    assert reason_css == "site_asset"

    ok_js, reason_js = classify_request(
        "GET",
        "https://azom.no/wp-includes/js/cart-fragments.min.js",
        site_host="azom.no",
    )
    assert ok_js is True
    assert reason_js == "site_asset"

    # Admin/AJAX/PHP under /wp-content/ must still be blocked
    ok_ajax, reason_ajax = classify_request(
        "GET",
        "https://azom.no/wp-content/plugins/cartflows/style.css?wc-ajax=1",
        site_host="azom.no",
    )
    assert ok_ajax is False
    assert reason_ajax == "site_path_blocked:wc-ajax"

    ok_php, reason_php = classify_request(
        "GET",
        "https://azom.no/wp-content/plugins/cartflows/checkout.php",
        site_host="azom.no",
    )
    assert ok_php is False
    assert reason_php == "site_path_blocked:cart"

    # HTML document fetches and validate_preview_path remain 100% strict
    ok_doc, reason_doc = classify_request("GET", "https://azom.no/checkout", site_host="azom.no")
    assert ok_doc is False
    assert reason_doc == "site_path_blocked:checkout"

    with pytest.raises(ReviewError):
        validate_preview_path("/wp-content/plugins/cartflows/assets/css/checkout.css")

    import urllib.parse

    assert (
        blocked_site_path_reason(
            urllib.parse.urlparse("https://azom.no/wp-content/plugins/cartflows/style.css")
        )
        == "site_path_blocked:cart"
    )
