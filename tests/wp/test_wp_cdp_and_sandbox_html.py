"""Unit tests for sandbox_html, preview_scripts, cdp_browser, and wp_live_preview facade hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _helpers import site_row

from ecom_ops.integrations import wp_live_preview
from ecom_ops.integrations.wp import cdp_browser, preview_scripts, sandbox_html
from ecom_ops.integrations.wp.cdp_browser import (
    HeadlessBrowserSession,
    LoopbackCdpClient,
    ReadOnlyFetchInterceptor,
    collect_cdp_diagnostics,
)
from ecom_ops.integrations.wp.sandbox_html import (
    build_interactive_sandbox_html,
    build_sandbox_guard_script,
)
from ecom_ops.wp_ops.html_sanitize import harden_preview_html
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.review_workspace import ReviewError


def test_build_sandbox_guard_script_direct_and_harden_preview_html_roundtrip() -> None:
    plain = build_sandbox_guard_script(
        highlight_token="Ny knapp",
        focus_text="Köp nu",
        focus_selector=".single_add_to_cart_button",
    )
    assert plain.startswith('<script data-azom-sandbox="guard" data-azom-guard="1">')
    assert "nonce=" not in plain
    assert 'var highlightToken = "Ny knapp";' in plain
    assert 'var focusText = "Köp nu";' in plain
    assert 'var focusSelector = ".single_add_to_cart_button";' in plain
    assert "Blockerad i sandbox-läget" in plain

    with_nonce = build_sandbox_guard_script(
        nonce="abc123XYZ",
        highlight_token="</script><img onerror=alert(1)>",
        focus_text="Köp",
        focus_selector=".btn",
    )
    assert with_nonce.startswith('<script nonce="abc123XYZ" data-azom-sandbox="guard" data-azom-guard="1">')
    assert "</script><img" not in with_nonce
    assert "<\\/script>" in with_nonce

    # Full inverted-dependency roundtrip with wp_ops.html_sanitize.harden_preview_html
    raw_sandbox = build_interactive_sandbox_html(
        '<html><body><p onclick="evil()">Hej</p><script>alert(1)</script></body></html>',
        site_canonical_url="https://azom.no",
        site_id="azom-no",
        plan_id="wr-0123456789ab",
        path="/",
        highlight_token="Hej",
        focus_selector=".btn",
    )
    hardened = harden_preview_html(
        raw_sandbox,
        nonce="nonce-777",
        guard_script_builder=build_sandbox_guard_script,
    )
    assert hardened.lower().count("<script") == 1
    assert '<script nonce="nonce-777" data-azom-sandbox="guard" data-azom-guard="1">' in hardened
    assert 'var highlightToken = "Hej";' in hardened
    assert 'var focusSelector = ".btn";' in hardened
    assert "alert(1)" not in hardened
    assert "onclick=" not in hardened.lower()


def test_loopback_cdp_client_eval_json_and_non_loopback_refusal() -> None:
    with pytest.raises(ReviewError) as exc_info:
        LoopbackCdpClient("ws://evil.example:9222/devtools/page/1")
    assert exc_info.value.error_class == "NonLoopbackUrlRefused"

    # Exercise eval_json without real sockets
    client = object.__new__(LoopbackCdpClient)
    responses = [
        {"result": {"value": json.dumps({"found": True, "count": 3})}},
        {"result": {"value": "not-json"}},
        {"result": {"value": {"already": "dict"}}},
        {"result": {"value": "123"}},
    ]
    client.call = lambda method, params=None, *, timeout_s=15.0: responses.pop(0)  # type: ignore[method-assign]

    assert client.eval_json("expr1") == {"found": True, "count": 3}
    assert client.eval_json("expr2") == {}
    assert client.eval_json("expr3") == {"already": "dict"}
    assert client.eval_json("expr4") == {}


def test_headless_browser_session_lifecycle_and_cleanup_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {"closed": False, "terminated": False, "calls": [], "command": None}

    class _FakeProc:
        pid = 99999

        def poll(self) -> None:
            return None

    class _FakeCdp:
        def __init__(self, ws_url: str, timeout_s: float = 15.0) -> None:
            state["ws_url"] = ws_url
            self.events: list[dict[str, Any]] = []
            self.event_hook = None

        def call(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 15.0) -> dict[str, Any]:
            del timeout_s
            state["calls"].append((method, params or {}))
            return {}

        def send(self, method: str, params: dict[str, Any] | None = None) -> int:
            state["calls"].append((method, params or {}))
            return 1

        def close(self) -> None:
            state["closed"] = True

    def _fake_popen(command: list[str], **_kwargs: Any) -> _FakeProc:
        state["command"] = command
        return _FakeProc()

    monkeypatch.setattr(cdp_browser.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(cdp_browser, "wait_cdp_page_ws", lambda port, proc, timeout_s=15.0: f"ws://127.0.0.1:{port}/devtools/page/1")
    monkeypatch.setattr(cdp_browser, "LoopbackCdpClient", _FakeCdp)
    monkeypatch.setattr(cdp_browser, "terminate_process_tree", lambda proc, timeout_s=5.0: state.__setitem__("terminated", True))

    created_profile: Path | None = None
    with pytest.raises(RuntimeError, match="boom"):
        with HeadlessBrowserSession(
            "fake-chrome",
            extra_flags=("--custom-flag",),
            profile_prefix="azom-test-cdp-",
            timeout_s=10.0,
        ) as session:
            created_profile = session.user_data_dir
            assert created_profile is not None and created_profile.is_dir()
            interceptor = ReadOnlyFetchInterceptor(
                site_host="azom.no",
                resolved_target="https://azom.no/",
            )
            session.attach_fetch_interceptor(interceptor)
            raise RuntimeError("boom")

    assert state["closed"] is True
    assert state["terminated"] is True
    assert created_profile is not None and not created_profile.exists()
    assert "--custom-flag" in (state["command"] or [])
    assert ("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}) in state["calls"]


def test_read_only_fetch_interceptor_and_cdp_diagnostics() -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    class _StubCdp:
        def __init__(self) -> None:
            self.event_hook = None

        def send(self, method: str, params: dict[str, Any] | None = None) -> int:
            sent.append((method, params or {}))
            return len(sent)

    cdp = _StubCdp()
    interceptor = ReadOnlyFetchInterceptor(site_host="azom.no", resolved_target="https://azom.no/")
    interceptor.bind(cdp)  # type: ignore[arg-type]
    interceptor.set_document_html("<html><body>Preview</body></html>")
    interceptor.arm_main_document()

    # Main document gets fulfilled once
    assert interceptor.handle_event(
        {
            "method": "Fetch.requestPaused",
            "params": {
                "requestId": "r1",
                "resourceType": "Document",
                "request": {"url": "https://azom.no/", "method": "GET"},
            },
        }
    )
    assert sent[-1][0] == "Fetch.fulfillRequest"
    assert interceptor.allowed_count == 1

    # Allowed static CDN GET continues
    assert interceptor.handle_event(
        {
            "method": "Fetch.requestPaused",
            "params": {
                "requestId": "r2",
                "resourceType": "Stylesheet",
                "request": {"url": "https://fonts.googleapis.com/css2?family=Inter", "method": "GET"},
            },
        }
    )
    assert sent[-1][0] == "Fetch.continueRequest"
    assert interceptor.allowed_count == 2

    # POST and blocked WP path are failed
    for rid, method, url in (
        ("r3", "POST", "https://azom.no/"),
        ("r4", "GET", "https://azom.no/kasse/"),
        ("r5", "GET", "https://evil.example/a.js"),
    ):
        assert interceptor.handle_event(
            {
                "method": "Fetch.requestPaused",
                "params": {
                    "requestId": rid,
                    "resourceType": "Fetch",
                    "request": {"url": url, "method": method},
                },
            }
        )
        assert sent[-1][0] == "Fetch.failRequest"
    assert interceptor.blocked_counts["method_blocked"] == 1
    assert interceptor.blocked_counts["site_path_blocked:kasse"] == 1
    assert interceptor.blocked_counts["foreign_host_blocked"] == 1

    # collect_cdp_diagnostics handles both preview (ignore blocked) and sandbox (track egress) modes
    events = [
        {"method": "Network.requestWillBeSent", "params": {"requestId": "1", "request": {"url": "https://evil.example/x.js"}}},
        {"method": "Network.responseReceived", "params": {"type": "Document", "frameId": "f1", "response": {"status": 200, "url": "http://127.0.0.1:9999/"}}},
        {"method": "Network.loadingFailed", "params": {"requestId": "1", "errorText": "net::ERR_BLOCKED_BY_CLIENT"}},
        {"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [{"value": "Boom"}]}},
        {"method": "Runtime.exceptionThrown", "params": {"exceptionDetails": {"text": "Uncaught", "exception": {"description": "TypeError"}}}},
    ]
    preview_diag = collect_cdp_diagnostics(events, ignore_blocked_by_client=True)
    assert preview_diag["doc_status"] == 200
    assert preview_diag["network_errors"] == []
    assert preview_diag["console_errors"] == ["Boom", "Uncaught"]

    sandbox_diag = collect_cdp_diagnostics(events, frame_id="f1", ignore_blocked_by_client=False, track_egress_source="browser_desktop")
    assert sandbox_diag["doc_status"] == 200
    assert len(sandbox_diag["egress_attempts"]) == 1
    assert sandbox_diag["egress_attempts"][0]["host"] == "evil.example"
    assert sandbox_diag["js_exceptions"] == ["Uncaught: TypeError"]


def test_wp_live_preview_facade_preserves_inspect_element_fetch_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = parse_site(site_row("azom-no", canonical_url="https://azom.no/"))
    called: list[str] = []

    def _fake_fetch(s: Any, path: str = "/", **_kw: Any) -> dict[str, Any]:
        called.append(f"{s.id}:{path}")
        raise ReviewError("StopAfterHook")

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.fetch_live_html", _fake_fetch)
    with pytest.raises(ReviewError) as exc_info:
        wp_live_preview.inspect_element(site, path="/produkt/test/", selector=".btn")
    assert exc_info.value.error_class == "StopAfterHook"
    assert called == ["azom-no:/produkt/test/"]
    assert "const isVisible =" in preview_scripts.focus_and_highlight_script(focus_selector=".btn")
    assert "const isVisible =" in preview_scripts.inspect_element_script(selector=".btn")
    assert wp_live_preview.build_sandbox_guard_script is sandbox_html.build_sandbox_guard_script
