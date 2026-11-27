"""Regression tests for Wave 4 WP-Research hardening and latent wp_ops fixes (#107, #125)."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from _helpers import MOCK_REGISTRY, tool_call, write_theme_file

from ecom_ops.bot import chat_agent, wp_session, wp_turn
from ecom_ops.research import get_research_service
from ecom_ops.research.engine import ResearchEngine
from ecom_ops.telemetry import Telemetry
from ecom_ops.wp_ops.file_paths import PathRejected, resolve_file
from ecom_ops.wp_ops.file_policy import is_safe_pure_css_file, risk_for
from ecom_ops.wp_ops.html_sanitize import (
    _neutralize_overlay_fragment,
    harden_preview_html,
    strip_tag_unsafe_attrs,
)
from ecom_ops.wp_ops.review.plans import ReviewPlans
from ecom_ops.wp_ops.review.workspace_io import read_manifest, workspace_root
from ecom_ops.wp_ops.review.workspace_ops import _MAX_CHANGES, bulk_replace_work
from ecom_ops.wp_ops.review_workspace import ReviewError
from ecom_ops.wp_ops.tools.loop_state import (
    LoopBudget,
    LoopPolicy,
    compact_older_file_messages,
)
from ecom_ops.wp_ops.tools.loops import _append_file_tool_message, run_bounded_file_tool_loop
from ecom_ops.wp_ops.tools.registry import TOOL_REGISTRY
from ecom_ops.wp_ops.tools.research import _RESEARCH_RESULT_BYTE_CAPS
from ecom_ops.wp_ops.tools.types import (
    WEB_FETCH_MAX_RESULT_BYTES,
    WEB_RESEARCH_GET_MAX_RESULT_BYTES,
    WEB_SEARCH_MAX_RESULT_BYTES,
    ToolOutput,
)


@pytest.fixture(autouse=True)
def _forbid_live_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Live network socket is prohibited in offline WP regression tests")

    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)


@pytest.mark.parametrize(
    ("tool_name", "arguments", "cap"),
    [
        ("web_search", {"query": "woocommerce checkout hooks"}, WEB_SEARCH_MAX_RESULT_BYTES),
        (
            "web_fetch_extract",
            {"url": "https://developer.woocommerce.com/docs/"},
            WEB_FETCH_MAX_RESULT_BYTES,
        ),
        (
            "web_research_get",
            {"research_id": "res-abcdef012345", "section": "summary"},
            WEB_RESEARCH_GET_MAX_RESULT_BYTES,
        ),
    ],
)
def test_wp_research_facade_live_calls_never_overflows_byte_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wp_service_tools,
    tool_name: str,
    arguments: dict[str, Any],
    cap: int,
) -> None:
    """Adding ``live_calls`` in ``_run_research_facade`` must keep payloads <= 5/7/8 KiB."""
    _svc, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        reset_router=True,
    )
    assert _RESEARCH_RESULT_BYTE_CAPS[tool_name] == cap

    head = f"<<<UNTRUSTED_{tool_name.upper()} nonce=abc12345>>>"
    tail = f"<<<END_UNTRUSTED_{tool_name.upper()} nonce=abc12345>>>"
    seed_lines = "Åäö rad\n" * 10
    base_payload: dict[str, Any] = {
        "ok": True,
        "provenance": "mock",
        "content": f"{head}\n{seed_lines}{tail}",
    }
    if tool_name == "web_research_get":
        base_payload["research_id"] = "res-abcdef012345"
        base_payload["section"] = "summary"

    # Grow content right up to ``cap`` BEFORE ``live_calls`` is added (+19 B).
    chunk = "Svensk text med radbrytning åäö 12345.\n"
    inner = "Åäö start\n"
    while True:
        cand = dict(base_payload, content=f"{head}\n{inner}{chunk}\n{tail}")
        raw_len = len(json.dumps(cand, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if raw_len > cap:
            break
        inner += chunk
    while True:
        cand = dict(base_payload, content=f"{head}\n{inner}x\n{tail}")
        raw_len = len(json.dumps(cand, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if raw_len > cap:
            break
        inner += "x"
    base_payload["content"] = f"{head}\n{inner}\n{tail}"
    pre_bytes = len(
        json.dumps(base_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    assert cap - 15 <= pre_bytes <= cap

    monkeypatch.setattr(
        "ecom_ops.research.tools.execute_research_tool",
        lambda *_args, **_kwargs: dict(base_payload),
    )

    out = tools.execute(tool_name, arguments, actor="oscar")
    assert out.data["ok"] is True
    assert out.data["live_calls"] is False

    budget = LoopBudget(
        LoopPolicy(
            max_calls=12,
            max_seconds=45.0,
            max_reads=6,
            max_searches=6,
            max_result_bytes=16384,
            max_total_bytes=65536,
        )
    )
    enforced, encoded = budget.enforce_result_size(out, fallback_site_id=None, tool_name=tool_name)
    assert enforced.data["ok"] is True
    assert enforced.data.get("error_class") != "ToolResultTooLarge"
    assert len(encoded.encode("utf-8")) <= cap
    assert enforced.data["content"].startswith(head)
    assert enforced.data["content"].endswith(tail)


def test_wp_execute_channel_peer_id_worker_starter_and_session_research_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wp_service_tools,
) -> None:
    """Propagate stripped peer_id/channel, wire live worker starter, and keep last_research_id."""
    _svc, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        reset_router=True,
    )

    start_out = tools.execute(
        "web_research_start",
        {"topic": "WooCommerce HPOS hooks", "depth": "quick"},
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:123",
    )
    assert start_out.data["ok"] is True
    rid = start_out.data["research_id"]
    stored = get_research_service(tmp_path).store.get_job(rid)
    assert stored is not None
    assert stored.channel == "telegram"
    assert stored.peer_id == "123"

    # Live mode wiring: on_enqueue invokes ensure_research_worker_started(data_dir=...)
    worker_calls: list[Path | str | None] = []
    captured_exec_kwargs: list[dict[str, Any]] = []
    monkeypatch.setattr("ecom_ops.research.config.is_mock_mode", lambda: False)
    monkeypatch.setattr("ecom_ops.research.config.research_enabled", lambda: True)
    monkeypatch.setattr(
        "ecom_ops.research.worker.ensure_research_worker_started",
        lambda *, data_dir=None: worker_calls.append(data_dir),
    )

    def _fake_exec(_name: str, _args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        captured_exec_kwargs.append(kwargs)
        cb = kwargs.get("on_enqueue")
        if callable(cb):
            cb(object())
        return {
            "ok": True,
            "research_id": "res-112233445566",
            "status": "queued",
            "provenance": "live",
        }

    monkeypatch.setattr("ecom_ops.research.tools.execute_research_tool", _fake_exec)
    live_out = tools.execute(
        "web_research_start",
        {"topic": "WooCommerce HPOS hooks"},
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:123",
    )
    assert live_out.data["ok"] is True
    assert captured_exec_kwargs[-1]["channel"] == "telegram"
    assert captured_exec_kwargs[-1]["peer_id"] == "123"
    assert worker_calls == [tmp_path]

    # wp_session preserves & normalizes research_ids
    merged = wp_session.merge_active_wp_review(
        None,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:123",
        site_id="mock-staging",
        meta={
            "site_id": "mock-staging",
            "latest_plan_id": "wr-aabbccddeeff",
            "research_ids": ["RES-ABCDEF012345", "res-abcdef012345", "invalid-id"],
        },
    )
    assert merged is not None
    assert merged["research_ids"] == ["res-abcdef012345"]
    validated = wp_session.valid_active_wp_review(
        merged, actor="oscar", channel="telegram", conversation_id="telegram:123"
    )
    assert validated is not None
    assert validated["research_ids"] == ["res-abcdef012345"]
    assert "research_ids=res-abcdef012345" in wp_turn.format_active_wp_review_block(validated)

    # wp_turn.dispatch_wp_tool_loop + chat_agent.run_chat preserve last_research_id & research_ids
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, _cap: True)

    class _FakeRouter:
        def any_configured(self) -> bool:
            return True

        def has_route_outside_openrouter_budget(self) -> bool:
            return False

        def preferred_route_id(self) -> str:
            return "openrouter"

    monkeypatch.setattr(chat_agent, "get_model_router", lambda: _FakeRouter())
    monkeypatch.setattr(
        chat_agent,
        "chat_completion",
        lambda *_a, **_kw: (
            '<tool_call>{"name":"wp_workspace_attach_research","arguments":'
            '{"site_id":"mock-staging","plan_id":"wr-aabbccddeeff","research_id":"res-abcdef012345"}}</tool_call>',
            0.0,
        ),
    )

    def _fake_file_loop(_content: str, _messages: list[dict[str, str]], **_kwargs: Any) -> Any:
        return type(
            "_FakeFileLoop",
            (),
            {
                "handled": True,
                "calls": 1,
                "content": "Research kopplad.",
                "followup_cost_usd": 0.0,
                "site_id": "mock-staging",
                "operation_ids": [],
                "results": [
                    {
                        "ok": True,
                        "plan_id": "wr-aabbccddeeff",
                        "research_id": "res-abcdef012345",
                    }
                ],
                "file_stage_plan_id": None,
                "stage_plan_id": None,
                "review_stage_plan_id": "wr-aabbccddeeff",
                "attachments": (),
                "model_error": False,
                "active_review_meta": {
                    "site_id": "mock-staging",
                    "latest_plan_id": "wr-aabbccddeeff",
                    "research_ids": ["res-abcdef012345"],
                },
                "tool_seconds": 0.01,
            },
        )()

    monkeypatch.setattr(chat_agent, "run_bounded_file_tool_loop", _fake_file_loop)

    chat_res = chat_agent.run_chat(
        "Koppla research till mock-staging",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:123",
        telemetry=Telemetry(path=tmp_path / "tel.jsonl"),
    )
    assert chat_res.last_research_id == "res-abcdef012345"
    assert chat_res.active_wp_review is not None
    assert chat_res.active_wp_review["research_ids"] == ["res-abcdef012345"]


def test_web_cache_hit_does_not_burn_web_budget_and_succeeds_after_max_web_searches(
    tmp_path: Path,
    wp_service_tools,
) -> None:
    """Within-turn ``web_cache`` hits skip web_searches/web_fetches budget and pass after cap=3."""
    _svc, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        reset_router=True,
    )

    executed: list[tuple[str, dict[str, Any]]] = []
    orig_execute = tools.execute

    def _spy_execute(name: str, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        executed.append((name, dict(arguments)))
        return orig_execute(name, arguments, **kwargs)

    tools.execute = _spy_execute  # type: ignore[method-assign]

    steps = iter(
        [
            tool_call("web_search", {"query": "woocommerce checkout ux two", "locale": "sv"}),
            tool_call("web_search", {"query": "woocommerce checkout ux three", "locale": "sv"}),
            # 4th web_search repeats query #1 -> served from web_cache after max_web_searches=3
            tool_call("web_search", {"query": "woocommerce checkout ux one", "locale": "sv"}),
            tool_call("web_fetch_extract", {"url": "https://baymard.com/blog/checkout-usability"}),
            tool_call("web_fetch_extract", {"url": "https://baymard.com/blog/checkout-usability"}),
            "Klart utan att slösa webb-budget.",
        ]
    )

    res = run_bounded_file_tool_loop(
        tool_call("web_search", {"query": "woocommerce checkout ux one", "locale": "sv"}),
        [],
        complete=lambda _msgs: (next(steps), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert res.handled is True
    assert len(res.results) == 6
    assert all(r.get("ok") is True for r in res.results)
    assert [n for n, _ in executed] == [
        "web_search",
        "web_search",
        "web_search",
        "web_fetch_extract",
    ]

    budget = LoopBudget(
        LoopPolicy(
            max_calls=12,
            max_seconds=45.0,
            max_reads=6,
            max_searches=6,
            max_result_bytes=16384,
            max_total_bytes=65536,
        )
    )
    budget.record_file_call(
        "web_search", ok=True, error_class=None, elapsed=0.4, using_preview_bonus=False, web_cache_hit=False
    )
    budget.record_file_call(
        "web_search", ok=True, error_class=None, elapsed=0.4, using_preview_bonus=False, web_cache_hit=True
    )
    budget.record_file_call(
        "web_fetch_extract",
        ok=True,
        error_class=None,
        elapsed=0.4,
        using_preview_bonus=False,
        web_cache_hit=True,
    )
    assert budget.web_searches == 1
    assert budget.web_fetches == 0
    assert budget.web_seconds == pytest.approx(0.4)


def test_distinct_data_dir_shared_between_web_research_start_and_attach_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wp_service_tools,
) -> None:
    """Jobs started via ``web_research_start`` live in ``tools.service.data_dir``, not ``AZOM_DATA_DIR``."""
    env_dir = tmp_path / "env_data"
    wp_dir = tmp_path / "wp_data"
    env_dir.mkdir()
    wp_dir.mkdir()

    _svc, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=wp_dir,
        with_child_theme=True,
        reset_router=True,
    )
    monkeypatch.setenv("AZOM_DATA_DIR", str(env_dir))

    started = tools.execute(
        "web_research_start",
        {"topic": "WooCommerce checkout optimization", "depth": "quick"},
        actor="oscar",
    )
    assert started.data["ok"] is True
    rid = started.data["research_id"]

    assert (wp_dir / "research_jobs.sqlite3").is_file()
    assert not (env_dir / "research_jobs.sqlite3").exists()

    rsvc = get_research_service(wp_dir)
    ResearchEngine(store=rsvc.store, search_router=rsvc.search_router, fetcher=rsvc.fetcher).run_job(
        rid
    )

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Layout"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]

    attached = tools.execute(
        "wp_workspace_attach_research",
        {"site_id": "mock-staging", "plan_id": plan_id, "research_id": rid},
        actor="oscar",
    )
    assert attached.data["ok"] is True
    assert rid in attached.data["research_ids"]


def test_append_file_tool_message_avoids_double_framing_and_compaction_keeps_closed_tags() -> None:
    """No double untrusted tags on append, and compacted older messages keep closed tags."""
    messages: list[dict[str, str]] = [{"role": "system", "content": "sys"}]
    inner_encoded = json.dumps(
        {
            "ok": True,
            "content": (
                "<<<UNTRUSTED_WEB_SEARCH nonce=deadbeef>>>\n"
                + ("Resultatrad med fakta.\n" * 80)
                + "<<<END_UNTRUSTED_WEB_SEARCH nonce=deadbeef>>>"
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    _append_file_tool_message(
        messages,
        next_content="<tool_call>{}</tool_call>",
        name="web_search",
        encoded=inner_encoded,
    )
    user_msg = messages[-1]["content"]
    assert "<<<UNTRUSTED_WEB_SEARCH" not in user_msg
    assert "<<<END_UNTRUSTED_WEB_SEARCH" not in user_msg
    assert user_msg.count("<<<UNTRUSTED_TOOL_DATA ") == 1
    assert user_msg.count("<<<END_UNTRUSTED_TOOL_DATA ") == 1

    # Add enough subsequent turns so ``messages[2]`` falls before the compaction cutoff (len - 8)
    for idx in range(5):
        messages.append({"role": "assistant", "content": f"step {idx}"})
        messages.append({"role": "user", "content": f"tool result {idx}"})
    assert len(messages) > 10

    compact_older_file_messages(messages)
    compacted = messages[2]["content"]
    lines = compacted.splitlines()
    assert lines[0].startswith("Untrusted tool data for web_search.")
    assert lines[1].startswith("<<<UNTRUSTED_TOOL_DATA tool=web_search nonce=")
    assert lines[-1].startswith("<<<END_UNTRUSTED_TOOL_DATA tool=web_search nonce=")
    assert "[komprimerad tidigare data:" in compacted


def test_refresh_locked_preserves_high_risk_from_summary(
    tmp_path: Path,
    wp_env,
    mock_child_theme,
) -> None:
    """``_refresh_locked`` must keep ``risk == 'high'`` when ``plan['summary']`` triggers high risk."""
    wp_env(mock="1", files_ssh="0", sandbox="0", live_preview="0", data_dir=tmp_path)
    mock_child_theme(data_dir=tmp_path, style_css="body { color: #111; }\n")
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        summary="Update checkout payment CTA",
        actor="oscar",
    )
    assert opened.ok is True
    assert opened.data["risk"] == "high"
    plan_id = opened.data["plan_id"]

    created = plans.create_file(
        "mock-staging",
        plan_id,
        "style.css",
        "body { color: #111; }\n",
        actor="oscar",
    )
    assert created.ok is True
    assert created.data["risk"] == "high"

    patched = plans.patch_file(
        "mock-staging",
        plan_id,
        "style.css",
        "body { color: #111; }",
        "body { color: #222; }",
        actor="oscar",
    )
    assert patched.ok is True
    assert patched.data["risk"] == "high"

    refreshed = plans.refresh("mock-staging", plan_id, actor="oscar")
    assert refreshed.ok is True
    assert refreshed.data["risk"] == "high"


def test_resolve_file_rejects_sensitive_files_and_tool_denies_read(
    tmp_path: Path,
    wp_service_tools,
) -> None:
    """``resolve_file`` and ``wp_read_theme_plugin_file`` must reject sensitive files."""
    for rel in ("secrets.json", "wp-config.php", "vendor/autoload.php", "api_key.json"):
        with pytest.raises(PathRejected, match="sensitive"):
            resolve_file("/var/www/html", "theme", "azom-child", rel)

    _svc, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        with_child_theme=True,
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "secrets.json",
        '{"secret": "DO_NOT_EXPOSE"}\n',
    )
    res = tools.execute(
        "wp_read_theme_plugin_file",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "path": "secrets.json",
        },
        actor="oscar",
    )
    assert res.data["ok"] is False
    assert "DO_NOT_EXPOSE" not in json.dumps(res.data)


def test_component_searcher_subdirectory_and_skips_dotfiles(
    tmp_path: Path,
    wp_service_tools,
) -> None:
    """``wp_search_component`` searches ``relative='templates'`` and skips dotfiles like ``.eslintrc.json``."""
    _svc, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        with_child_theme=True,
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "templates/single.php",
        "<?php\n// NEEDLE_SUBDIR_OK\n",
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "templates/.eslintrc.json",
        '{"rules": {"NEEDLE_SUBDIR_OK": "off"}}\n',
    )
    res = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "path": "templates",
            "query": "NEEDLE_SUBDIR_OK",
        },
        actor="oscar",
    )
    assert res.data["ok"] is True
    paths = [hit["path"] for hit in res.data["hits"]]
    assert paths == ["templates/single.php"]


def test_live_preview_lock_reload_preserves_concurrent_attach_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wp_env,
    mock_child_theme,
) -> None:
    """``live_preview()`` lock+reload must not clobber concurrent ``attach_research()``."""
    wp_env(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="1",
        data_dir=tmp_path,
    )
    mock_child_theme(data_dir=tmp_path)
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        summary="Hero styling",
        actor="oscar",
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    rid = "res-0123456789ab"

    monkeypatch.setattr(
        plans,
        "build_overlay_html",
        lambda *_args, **_kwargs: (
            {
                "site": type("_S", (), {"id": "mock-staging", "environment": "staging", "canonical_url": "https://staging.azom.example"})(),
                "plan": plans.get(plan_id, site_id="mock-staging"),
                "root": workspace_root(tmp_path, "mock-staging", plan_id),
                "clean_path": "/om-oss/",
                "fetched": {
                    "html": "<html><body><main>Hej</main></body></html>",
                    "fetched_at": "2026-11-27T00:00:00Z",
                    "from_cache": True,
                },
                "overlay": type(
                    "_O",
                    (),
                    {
                        "html": "<html><body><main>Hej</main></body></html>",
                        "highlight_token": "tok",
                        "hunks": [],
                        "notices": [],
                        "injected_css_bytes": 0,
                    },
                )(),
                "mode": "overlay",
            },
            None,
        ),
    )

    def _fake_capture_preview(
        _site: Any, *, artifact_dir: Path, **_kwargs: Any
    ) -> dict[str, Any]:
        # Simulate concurrent attach_research happening while CDP renders
        att = plans.attach_research(
            "mock-staging",
            plan_id,
            rid,
            summary_snapshot={"research_id": rid, "topic": "UX", "summary": "Concurrent note"},
            actor="oscar",
        )
        assert att["ok"] is True
        shot = artifact_dir / "preview-desktop.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        return {
            "desktop": str(shot),
            "mobile": str(shot),
            "visual_health": {"ok": True},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", _fake_capture_preview)
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.build_interactive_sandbox_html",
        lambda html, **_kw: html,
    )

    pv = plans.live_preview("mock-staging", plan_id, actor="oscar", path="/om-oss/")
    assert pv.ok is True
    final_plan = plans.get(plan_id, site_id="mock-staging")
    assert final_plan is not None
    assert final_plan["research_ids"] == [rid]
    assert final_plan["preview_path"] == "/om-oss/"
    manifest = read_manifest(workspace_root(tmp_path, "mock-staging", plan_id))
    assert manifest["research_ids"] == [rid]


def test_place_refresh_and_attach_research_reject_rolled_back_plan(
    tmp_path: Path,
    wp_env,
    mock_child_theme,
) -> None:
    """``place()``, ``refresh()``, and ``attach_research()`` reject ``_NON_EDITABLE`` plans."""
    wp_env(mock="1", files_ssh="0", sandbox="0", live_preview="0", data_dir=tmp_path)
    mock_child_theme(data_dir=tmp_path, style_css="body { color: #111; }\n")
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    opened = plans.open(
        "mock-staging", "theme", "azom-child", summary="Color tweak", actor="oscar"
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    created = plans.create_file(
        "mock-staging",
        plan_id,
        "style.css",
        "body { color: #111; }\n",
        actor="oscar",
    )
    assert created.ok is True

    rolled = plans.rollback("mock-staging", plan_id, actor="oscar")
    assert rolled.ok is True
    assert rolled.data["status"] == "rolled_back"

    work_style = workspace_root(tmp_path, "mock-staging", plan_id) / "work" / "style.css"
    existed_before = work_style.exists()
    before_bytes = work_style.read_bytes() if existed_before else None

    pl = plans.place("mock-staging", plan_id, "style.css", b"body { color: red; }\n", actor="oscar")
    assert pl.ok is False
    if existed_before:
        assert work_style.read_bytes() == before_bytes
    else:
        assert not work_style.exists()

    rf = plans.refresh("mock-staging", plan_id, actor="oscar")
    assert rf.ok is False

    att = plans.attach_research(
        "mock-staging",
        plan_id,
        "res-0123456789ab",
        summary_snapshot={"research_id": "res-0123456789ab", "summary": "Should not attach"},
        actor="oscar",
    )
    assert att["ok"] is False
    assert att["error_class"] == "PlanNotEditable"


def test_bulk_replace_max_changes_in_dry_run_and_preserves_preview_ran(
    tmp_path: Path,
    wp_service_tools,
) -> None:
    """``bulk_replace_work`` enforces ``_MAX_CHANGES`` in dry_run and dry_run keeps ``preview_ran``."""
    ws_root = tmp_path / "ws"
    (ws_root / "base").mkdir(parents=True)
    (ws_root / "work").mkdir(parents=True)
    for idx in range(_MAX_CHANGES + 1):
        rel = f"f_{idx:03d}.css"
        (ws_root / "base" / rel).write_text("/* OLD_TOKEN */\n", encoding="utf-8")
        (ws_root / "work" / rel).write_text("/* OLD_TOKEN */\n", encoding="utf-8")

    with pytest.raises(ReviewError, match="InvalidToolArguments"):
        bulk_replace_work(ws_root, "OLD_TOKEN", "NEW_TOKEN", dry_run=True)

    # Loop-level check: wp_workspace_bulk_replace(dry_run=True) must NOT clear preview_ran / review_stage_plan_id
    wp_data = tmp_path / "wp_data"
    wp_data.mkdir()
    _svc, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=wp_data,
        with_child_theme=True,
    )
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "CSS"},
        actor="oscar",
    )
    plan_id = opened.data["plan_id"]
    patched = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "style.css",
            "old_text": ".btn { background: #111; color: #fff; }",
            "new_text": ".btn { background: #222; color: #fff; }",
        },
        actor="oscar",
    )
    assert patched.data["ok"] is True

    # Seed preview_ran=True and review_stage_plan_id via wp_preview_page, then run bulk_replace dry_run=True
    orig_exec = tools.execute

    def _mock_preview(name: str, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        if name == "wp_preview_page":
            return ToolOutput(
                {
                    "ok": True,
                    "site_id": "mock-staging",
                    "plan_id": plan_id,
                    "visual_health": {"ok": True},
                },
                site_id="mock-staging",
                review_stage_plan_id=plan_id,
            )
        return orig_exec(name, arguments, **kwargs)

    tools.execute = _mock_preview  # type: ignore[method-assign]
    steps = iter(
        [
            tool_call(
                "wp_workspace_bulk_replace",
                {
                    "site_id": "mock-staging",
                    "plan_id": plan_id,
                    "find": "#222",
                    "replace": "#333",
                    "dry_run": True,
                },
            ),
            "Dry run klar med bevarad förhandsvisning.",
        ]
    )
    res = run_bounded_file_tool_loop(
        tool_call("wp_preview_page", {"site_id": "mock-staging", "plan_id": plan_id, "path": "/"}),
        [],
        complete=lambda _msgs: (next(steps), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert res.handled is True
    assert res.review_stage_plan_id == plan_id


def test_wp_list_workspaces_outcome_and_html_css_sanitization() -> None:
    """``wp_list_workspaces`` renders ``plans`` count; HTML entities & CRLF CSS escapes are blocked."""
    spec = TOOL_REGISTRY["wp_list_workspaces"]
    assert spec.outcome({"ok": True, "plans": [{"plan_id": "wr-1"}, {"plan_id": "wr-2"}]}, None) == "2 arbetsytor"

    entity_html = '<a href="&#106;avascript:alert(1)">Klicka</a>'
    hardened = harden_preview_html(entity_html, nonce="n1", guard_script_builder=lambda **_k: "")
    assert "javascript:" not in hardened.lower()
    assert "&#106;" not in hardened
    assert "href=" not in hardened.lower()

    neutralized = _neutralize_overlay_fragment(entity_html)
    assert "javascript:" not in neutralized.lower()
    assert 'href="#"' in neutralized.lower()

    import re

    tag_m = re.match(r"^<([a-zA-Z0-9]+)(.*?)>$", entity_html.split("</a>")[0] + ">")
    assert tag_m is not None
    assert "href" not in strip_tag_unsafe_attrs(tag_m)

    crlf_css = "body { background: \\75\r\nrl(https://evil.example/x.png); }"
    assert is_safe_pure_css_file("style.css", crlf_css) is False
    assert risk_for("style.css", crlf_css) == "high"
