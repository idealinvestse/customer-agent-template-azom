"""Regression tests for model routing, ChatGPT OAuth model names, Grok CLI flags, and OpenRouter UTF-8."""

from __future__ import annotations

import json
from pathlib import Path

import responses

from ecom_ops.model_router.base import ModelRequest
from ecom_ops.model_router.openrouter import OPENROUTER_URL, OpenRouterRoute
from ecom_ops.model_router.profiles import load_profiles, reset_profile_cache_for_tests
from ecom_ops.model_router.xai_grok_build import XaiGrokBuildRoute
from ecom_ops.telemetry import Telemetry


def test_model_profiles_has_no_gpt54_mini_and_chatgpt_first(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    reset_profile_cache_for_tests()

    raw_yaml = Path("config/model_profiles.yaml").read_text(encoding="utf-8")
    assert "gpt-5.4-mini" not in raw_yaml

    catalog = load_profiles()
    recommended_ids = [m["id"] for m in catalog.recommended_models["chatgpt_oauth"]]
    assert "gpt-6-luna" in recommended_ids
    assert "gpt-6.2-sol" in recommended_ids
    assert "gpt-6.2-astra" in recommended_ids
    assert "gpt-5.4-mini" not in recommended_ids

    assert catalog.profiles["chat_fast_tools"].mounts[0].model_id == "gpt-6-luna"
    assert catalog.profiles["cases_classify"].mounts[0].recommended_model == "gpt-6-luna"
    assert catalog.profiles["research_planner"].mounts[0].recommended_model == "gpt-6-luna"

    for profile_id, expected_rec in (
        ("wp_tool_router", "gpt-6-luna"),
        ("wp_code_deep", "gpt-6.2-astra"),
    ):
        mounts = catalog.profiles[profile_id].mounts
        assert mounts[0].slot_id == "primary"
        assert mounts[0].route_id == "chatgpt_oauth"
        assert mounts[0].recommended_model == expected_rec
        assert mounts[1].slot_id == "fallback_grok_build"
        assert mounts[1].route_id == "xai_grok_build"


def test_grok_command_disallows_tools_and_omits_permission_mode(tmp_path):
    route = XaiGrokBuildRoute()
    cmd = route._grok_command("grok", "grok-4.7-build-fast", tmp_path / "prompt.txt", "low")

    assert "--disallowed-tools" in cmd
    disallowed_val = cmd[cmd.index("--disallowed-tools") + 1]
    assert disallowed_val == "search_tool,web_search,bash,read,write,edit,glob,grep"
    assert "--disable-web-search" in cmd
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--permission-mode" not in cmd


@responses.activate
def test_openrouter_decodes_utf8_swedish_without_charset_header(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    payload = {
        "choices": [{"message": {"content": "Hej! Återbetalning för order är på väg — åäöÅÄÖ."}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 10, "total_tokens": 22},
    }
    responses.add(
        responses.POST,
        OPENROUTER_URL,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        status=200,
        content_type="application/json",
    )

    route = OpenRouterRoute()
    result = route.complete(
        ModelRequest(messages=[{"role": "user", "content": "Var är min order?"}]),
        telemetry=Telemetry(path=tmp_path / "tel.jsonl"),
    )
    assert result.content == "Hej! Återbetalning för order är på väg — åäöÅÄÖ."


@responses.activate
def test_openrouter_stream_decodes_utf8_swedish_without_charset_header(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"Återbetalning "}}]}',
        'data: {"choices":[{"delta":{"content":"klar för åäöÅÄÖ."}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":4,"total_tokens":9}}',
        "data: [DONE]",
    ]
    responses.add(
        responses.POST,
        OPENROUTER_URL,
        body=("\n".join(sse_lines) + "\n").encode("utf-8"),
        status=200,
        content_type="text/event-stream",
    )

    deltas: list[str] = []
    route = OpenRouterRoute()
    result = route.complete(
        ModelRequest(
            messages=[{"role": "user", "content": "Hej"}],
            on_text_delta=deltas.append,
        ),
        telemetry=Telemetry(path=tmp_path / "tel.jsonl"),
    )
    assert result.content == "Återbetalning klar för åäöÅÄÖ."
    assert "".join(deltas) == "Återbetalning klar för åäöÅÄÖ."
