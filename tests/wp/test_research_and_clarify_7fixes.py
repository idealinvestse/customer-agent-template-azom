"""Tests for web_research_start target_ref normalization, playbook updates, and clarify mojibake repair."""

from __future__ import annotations

from types import SimpleNamespace

from ecom_ops.bot.change_clarifier import _repair_mojibake, _scrub_text
from ecom_ops.research.tools import WEB_RESEARCH_TOOL_GUIDE
from ecom_ops.wp_ops.playbooks import PLAYBOOKS
from ecom_ops.wp_ops.tools.research import handle_web_research_start
from ecom_ops.wp_ops.tools.types import ToolContext


def _fake_tools(tmp_path):
    return SimpleNamespace(
        service=SimpleNamespace(data_dir=tmp_path),
        _site_model=lambda _site_id: None,
    )


def test_handle_web_research_start_normalizes_site_and_workspace_target_refs(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    captured: list[dict] = []

    def fake_execute(name, arguments, **_kwargs):
        captured.append({"name": name, "arguments": dict(arguments)})
        return {"ok": True, "research_id": "res-0123456789ab", "provenance": "mock"}

    monkeypatch.setattr("ecom_ops.research.tools.execute_research_tool", fake_execute)

    tools = _fake_tools(tmp_path)
    ctx_site = ToolContext(actor="oscar", sticky_site_id="mock-shop")
    orig_site_args = {"topic": "CarPlay design", "target_ref": "azom-no"}
    out_site = handle_web_research_start(tools, ctx_site, orig_site_args)
    assert out_site.data.get("ok") is True
    assert out_site.data.get("error_class") != "SearchPolicyError"
    assert orig_site_args["target_ref"] == "azom-no"
    assert captured[-1]["arguments"]["target_ref"] == "wp_site:azom-no"

    ctx_wr = ToolContext(actor="oscar", sticky_site_id="mock-shop")
    orig_wr_args = {"topic": "Thank you page", "target_ref": "wr-abcdef012345"}
    out_wr = handle_web_research_start(tools, ctx_wr, orig_wr_args)
    assert out_wr.data.get("ok") is True
    assert out_wr.data.get("error_class") != "SearchPolicyError"
    assert orig_wr_args["target_ref"] == "wr-abcdef012345"
    assert captured[-1]["arguments"]["target_ref"] == "wp:mock-shop:wr-abcdef012345"


def test_repair_mojibake_fixes_double_encoded_utf8_and_preserves_normal_swedish() -> None:
    assert _repair_mojibake("Ingen Ã¤ndring pÃ¥ liveservern") == "Ingen ändring på liveservern"
    assert _repair_mojibake("GÃ¶ra en ny tacksida") == "Göra en ny tacksida"
    assert _scrub_text("Ingen Ã¤ndring pÃ¥ liveservern", max_len=120) == "Ingen ändring på liveservern"

    normal = "Ingen ändring på liveservern — gör ett utkast"
    assert _repair_mojibake(normal) == normal
    assert _scrub_text(normal, max_len=120) == normal


def test_new_page_and_sample_data_playbook_and_tool_guide() -> None:
    pb = PLAYBOOKS["new_page_and_sample_data"]
    steps_text = " ".join(pb["steps"])
    assert "web_fetch_extract" in steps_text
    assert "web_search" in steps_text
    assert "image_url" in steps_text
    assert "web_fetch_extract" in pb["tools"]
    assert "web_search" in pb["tools"]

    assert "web_search" in WEB_RESEARCH_TOOL_GUIDE
    assert "web_fetch_extract" in WEB_RESEARCH_TOOL_GUIDE
    assert "wp_site:<site_id>" in WEB_RESEARCH_TOOL_GUIDE
