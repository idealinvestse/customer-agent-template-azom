"""Read-tool rounds for several Woo orders. Mock only. No live model call."""

from __future__ import annotations

from ecom_ops.bot.chat_agent import run_chat
from ecom_ops.model_router.errors import ModelUnavailableError


class _Ready:
    def any_configured(self):
        return True

    def has_route_outside_openrouter_budget(self):
        return True

    def preferred_route_id(self):
        return "xai_grok_build"


class _Tel:
    def within_budget(self, cap):
        del cap
        return True

    def sum_cost_usd(self):
        return 0.0

    def record(self, **kwargs):
        del kwargs


def _listed(ids: list[str]) -> list[dict[str, str]]:
    lines = ["Senaste ordrar:"]
    for oid in ids:
        lines.append(f"- {oid} | processing | 2026-07-10 | 10 SEK")
    return [{"role": "assistant", "content": "\n".join(lines)}]


def _bind(monkeypatch, chat):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_TELEGRAM_MODEL_ROUTE", "xai_grok_build")
    monkeypatch.setenv("XAI_MODEL", "grok-4.7")
    monkeypatch.setattr("ecom_ops.bot.chat_agent.get_model_router", lambda: _Ready())
    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", chat)


def test_known_list_fetches_five_orders_before_one_fast_summary(monkeypatch):
    seen: list[dict] = []
    progress: list[str] = []

    def chat(messages, **kwargs):
        seen.append({"messages": messages, "kwargs": kwargs})
        blob = "\n".join(item.get("content") or "" for item in messages)
        for oid in ("1001", "1002", "1003", "1004", "1005"):
            assert oid in blob
        return "Alla fem är hämtade.", 0.0

    _bind(monkeypatch, chat)
    result = run_chat(
        "Ta fram orderdetaljer från alla 5",
        history=_listed(["1001", "1002", "1003", "1004", "1005"]),
        on_progress=progress.append,
        telemetry=_Tel(),
    )
    assert result.text == "Alla fem är hämtade."
    assert len(seen) == 1
    kwargs = seen[0]["kwargs"]
    assert kwargs["model"] == "grok-4.3"
    assert kwargs["timeout"] == 20
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["hard_timeout"] == 45
    assert kwargs["model"] != "grok-4.7"
    joined = "\n".join(progress)
    assert "Hämtar order 1001 (1/5) från Woo." in joined
    assert "Hämtar order 1005 (5/5) från Woo." in joined
    assert "Order 1001 (1/5): Woo svarade." in joined
    assert "Väntar på den snabba modellen" in joined
    assert "SEK" not in joined
    assert "customer@" not in joined
    assert "499" not in joined


def test_model_can_request_two_lookups(monkeypatch):
    progress: list[str] = []
    calls = {"n": 0}

    def chat(messages, **kwargs):
        calls["n"] += 1
        assert kwargs["model"] == "grok-4.3"
        assert kwargs["timeout"] == 20
        assert kwargs["reasoning_effort"] == "low"
        blob = "\n".join(item.get("content") or "" for item in messages)
        if calls["n"] == 1:
            assert "Tool result" not in blob
            return (
                '<tool_call>{"name":"lookup_order","arguments":{"order_id":"1001"}}</tool_call>',
                0.0,
            )
        if calls["n"] == 2:
            assert "1001" in blob
            return (
                '<tool_call>{"name":"lookup_order","arguments":{"order_id":"1002"}}</tool_call>',
                0.0,
            )
        assert "1002" in blob
        return "Första och andra ordern är lästa.", 0.0

    _bind(monkeypatch, chat)
    result = run_chat(
        "jämför ordrarna",
        history=[],
        on_progress=progress.append,
        telemetry=_Tel(),
    )
    assert result.text == "Första och andra ordern är lästa."
    assert "<tool_call>" not in result.text
    assert calls["n"] == 3
    joined = "\n".join(progress)
    assert "för order 1001" in joined
    assert "för order 1002" in joined
    assert "Order 1001: Woo svarade." in joined
    assert "Order 1002: Woo svarade." in joined
    assert "customer@" not in joined


def test_model_death_keeps_the_first_order(monkeypatch):
    progress: list[str] = []

    def chat(messages, **kwargs):
        del kwargs
        blob = "\n".join(item.get("content") or "" for item in messages)
        if "Tool result [lookup_order]" not in blob:
            return (
                '<tool_call>{"name":"lookup_order","arguments":{"order_id":"1001"}}</tool_call>',
                0.0,
            )
        raise ModelUnavailableError("Grok Build CLI failed", code="exit_1")

    _bind(monkeypatch, chat)
    result = run_chat(
        "jämför ordrarna",
        history=[],
        on_progress=progress.append,
        telemetry=_Tel(),
    )
    assert "1001" in result.text
    assert "Sammanfattningen kunde inte skrivas" in result.text
    assert "Kunde inte nå LLM" not in result.text
    assert "<tool_call>" not in result.text
    assert "Modellrundan avbröts" in "\n".join(progress)
    assert "1002" not in result.text


def test_status_line_keeps_the_step_sentence():
    from ecom_ops.bot.__main__ import _LiveTelegramTurn

    turn = _LiveTelegramTurn("token", 1)
    turn._refresh_status = lambda force=False: None
    message = "Hämtar order 1001 (1/5) från Woo. " + ("steg " * 40)
    turn.on_progress(message)
    kept = turn.status_lines[-1]
    assert kept.startswith("Hämtar order 1001 (1/5) från Woo.")
    assert len(kept) > 180
