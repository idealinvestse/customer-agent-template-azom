"""Model profiles, capability filtering, slug isolation, and prompt caching."""

from __future__ import annotations

import json

import pytest
import responses

from ecom_ops.bot import chat_agent
from ecom_ops.bot.progress_events import ProgressEvent
from ecom_ops.model_router.base import ModelRequest
from ecom_ops.model_router.errors import ModelConfigError, ModelRouteError
from ecom_ops.model_router.openai_chatgpt import build_responses_payload
from ecom_ops.model_router.openrouter import OPENROUTER_URL, estimate_cost_usd
from ecom_ops.model_router.profiles import (
    load_profiles,
    reset_profile_cache_for_tests,
    reset_profile_override,
    save_profile_override,
)
from ecom_ops.model_router.router import ModelRouter, reset_model_router
from ecom_ops.model_router.xai_api_key import XAI_CHAT_URL
from ecom_ops.telemetry import Telemetry
from ecom_ops.wp_ops.agent_tools import WP_TOOL_GUIDE, run_bounded_file_tool_loop


def test_load_profiles_and_override_hot_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    reset_profile_cache_for_tests()
    catalog = load_profiles()
    assert set(catalog.profiles) >= {
        "cases_classify",
        "cases_draft",
        "product_copy",
        "chat_fast_tools",
        "chat_general",
        "wp_tool_router",
        "wp_code_deep",
    }
    saved = save_profile_override(
        "cases_classify",
        {
            "generation": {"max_output_tokens": 640, "temperature": 0.1},
            "mounts": [
                {"slot_id": "primary", "route_id": "openrouter", "model_id": "anthropic/claude-haiku-5.5", "enabled": True}
            ],
        },
    )
    assert saved.has_override is True
    assert saved.generation.max_output_tokens == 640
    assert saved.mounts[0].model_id == "anthropic/claude-haiku-5.5"

    # Hot reload without calling reset
    reloaded = load_profiles()
    assert reloaded.profiles["cases_classify"].mounts[0].model_id == "anthropic/claude-haiku-5.5"

    reset_profile_override("cases_classify")
    after_reset = load_profiles()
    assert after_reset.profiles["cases_classify"].has_override is False


def test_profile_override_rejects_secrets_and_isolated_test(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    reset_profile_cache_for_tests()
    with pytest.raises(ValueError):
        save_profile_override("isolated_test", {"generation": {"max_output_tokens": 100}})
    with pytest.raises(ValueError):
        save_profile_override("cases_classify", {"api_key": "sk-secret"})
    with pytest.raises(ValueError):
        save_profile_override("cases_classify", {"prompt_ref": "evil_prompt"})


@responses.activate
def test_profile_prevents_cross_provider_slug_leak_on_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XAI_API_KEY", "xai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "xai_api_key,openrouter")
    reset_model_router()

    # xai_api_key fails with 503 using grok-4.1-fast; openrouter must get anthropic/claude-haiku-5.5, NOT grok-4.1-fast!
    responses.add(responses.POST, XAI_CHAT_URL, json={"error": "down"}, status=503)
    responses.add(
        responses.POST,
        OPENROUTER_URL,
        json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
        status=200,
    )
    tel = Telemetry(path=tmp_path / "data" / "tel.jsonl")
    router = ModelRouter()
    res = router.complete_profile(
        "chat_fast_tools",
        ModelRequest(messages=[{"role": "user", "content": "hej"}], model="grok-4.3"),
        telemetry=tel,
    )
    assert res.content == "ok"
    assert res.fallback_used is True
    assert res.primary_route == "xai_api_key"
    assert res.primary_error_class == "ModelUnavailableError"
    or_sent = json.loads(responses.calls[1].request.body)
    assert or_sent["model"] == "anthropic/claude-haiku-5.5"
    assert "grok" not in or_sent["model"]

    events = [json.loads(line) for line in tel.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    actions = [ev["action"] for ev in events]
    assert "model_route_error" in actions
    selected = [ev for ev in events if ev["action"] == "model_route_selected"][-1]
    assert selected["meta"]["fallback_used"] is True
    assert selected["meta"]["primary_error_class"] == "ModelUnavailableError"
    # Token counters must remain numeric (never ***REDACTED***)
    assert selected["meta"]["prompt_tokens"] == 10
    assert selected["meta"]["completion_tokens"] == 5
    assert selected["meta"]["total_tokens"] == 15


@responses.activate
def test_schema_error_escalates_once_then_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XAI_API_KEY", "xai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "xai_api_key,openrouter")
    reset_model_router()

    responses.add(
        responses.POST,
        XAI_CHAT_URL,
        json={"choices": [{"message": {"content": "not json"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
        status=200,
    )
    responses.add(
        responses.POST,
        OPENROUTER_URL,
        json={
            "choices": [{"message": {"content": '{"category":"shipping","confidence":0.9}'}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        },
        status=200,
    )
    router = ModelRouter()
    res = router.complete_profile(
        "cases_classify",
        ModelRequest(messages=[{"role": "user", "content": "var är paketet?"}]),
        telemetry=Telemetry(path=tmp_path / "data" / "tel.jsonl"),
        validator=lambda txt: txt.startswith("{"),
    )
    assert res.route_id == "openrouter_api_key"
    assert res.fallback_used is True
    assert res.primary_error_class == "ModelSchemaError"


@responses.activate
def test_complete_slot_is_isolated_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XAI_API_KEY", "xai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    reset_model_router()

    responses.add(responses.POST, XAI_CHAT_URL, json={"error": "bad"}, status=503)
    router = ModelRouter()
    with pytest.raises(ModelRouteError):
        router.complete_slot(
            "chat_fast_tools",
            "fallback_xai_api",
            ModelRequest(messages=[{"role": "user", "content": "PONG"}]),
        )
    assert len(responses.calls) == 1


def test_openrouter_per_model_and_actual_usage_cost():
    assert estimate_cost_usd(1_000_000, 1_000_000, model="anthropic/claude-sonnet-5.5") == pytest.approx(12.0)
    assert estimate_cost_usd(1_000_000, 1_000_000, model="deepseek/deepseek-v4.1-flash-1017") == pytest.approx(0.75)
    assert estimate_cost_usd(100, 100, model="anthropic/claude-sonnet-5.5", actual_cost=0.42) == pytest.approx(0.42)


def test_openai_responses_payload_keeps_static_instructions_prefix():
    payload = build_responses_payload(
        ModelRequest(
            messages=[
                {"role": "system", "content": "STATIC_SYSTEM"},
                {"role": "user", "content": "Fråga 1"},
                {"role": "system", "content": "DYNAMIC_TOOL_RESULT"},
                {"role": "user", "content": "Fråga 2"},
            ],
            reasoning_effort="low",
            response_format={"type": "json_schema", "json_schema": {"name": "c", "schema": {"type": "object"}, "strict": True}},
        ),
        "gpt-6.2-sol",
    )
    assert payload["instructions"] == "STATIC_SYSTEM"
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["name"] == "c"
    assert [m["role"] for m in payload["input"]] == ["user", "developer", "user"]
    assert payload["input"][1]["content"] == "DYNAMIC_TOOL_RESULT"


def test_chat_omits_wp_guide_on_non_wp_and_isolates_telegram_route(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_TELEGRAM_MODEL_ROUTE", "xai_api_key")
    monkeypatch.setenv("XAI_API_KEY", "xai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr("ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, cap: True)
    seen: list[dict] = []

    def fake_chat(messages, **kwargs):
        seen.append({"messages": messages, "kwargs": kwargs})
        return "Hej från Messenger!", 0.0

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)
    res = chat_agent.run_chat("Hej hur mår du?", channel="messenger", telemetry=Telemetry(path=tmp_path / "tel.jsonl"))
    assert "Messenger" in res.text
    sys_msg = seen[0]["messages"][0]["content"]
    assert "wp_open_review_workspace" not in sys_msg
    assert WP_TOOL_GUIDE not in sys_msg
    # AZOM_TELEGRAM_MODEL_ROUTE must NOT override Messenger
    assert seen[0]["kwargs"].get("route_id") is None


def test_chat_emits_failed_model_draft_event_on_llm_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setattr("ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, cap: True)

    def boom(messages, **kwargs):
        raise ModelConfigError("down")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", boom)
    events: list[ProgressEvent] = []
    cb = lambda ev: events.append(ev)  # noqa: E731
    cb.accepts_progress_event = True  # type: ignore[attr-defined]
    chat_agent.run_chat("Läget?", on_progress=cb, telemetry=Telemetry(path=tmp_path / "tel.jsonl"))
    failed = [ev for ev in events if isinstance(ev, ProgressEvent) and ev.step_id == "model-draft" and ev.phase == "failed"]
    assert len(failed) == 1


def test_file_loop_promotes_from_fast_router_to_deep_coder(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tiers: list[str] = []
    phases: list[dict] = []

    def fast_complete(msgs):
        tiers.append("fast")
        if len(tiers) == 1:
            return '<tool_call>{"name":"wp_open_review_workspace","arguments":{"site_id":"azom-se","kind":"theme"}}</tool_call>', 0.0
        return "Klart", 0.0

    def deep_complete(msgs):
        tiers.append("deep")
        return "Djup analys klar.", 0.0

    res = run_bounded_file_tool_loop(
        '<tool_call>{"name":"wp_list_workspaces","arguments":{}}</tool_call>',
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "kolla"}],
        complete=fast_complete,
        complete_deep=deep_complete,
        on_phase_change=phases.append,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:1",
    )
    assert res.handled is True
    assert tiers[0] == "fast"
    assert "deep" in tiers
    assert any(p.get("phase") == "profile_switch" and p.get("to_profile") == "wp_code_deep" for p in phases)
