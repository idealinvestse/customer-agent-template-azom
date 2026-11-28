"""Regression tests for actionable InvalidToolArguments hints in WP sandbox tools and playbooks."""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import MOCK_REGISTRY as REGISTRY
from _helpers import REPO_ROOT as ROOT

from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.playbooks import PLAYBOOK_TOPICS, get_playbook
from ecom_ops.wp_ops.service import WpOpsService
from ecom_ops.wp_ops.tools.types import SAMPLE_SCENARIOS


def _tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpAgentTools:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "0")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()
    return WpAgentTools(WpOpsService(registry_path=REGISTRY, data_dir=tmp_path))


def test_open_review_workspace_invalid_kind_returns_actionable_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(tmp_path, monkeypatch)
    out = tools._execute_file(
        "wp_open_review_workspace",
        {"site_id": "mock-shop", "kind": "page", "slug": "takk-for-bestillingen", "summary": "Ny tacksida"},
        actor="oscar",
        sticky_site_id=None,
    )
    assert out.data["ok"] is False
    assert out.data["error_class"] == "InvalidToolArguments"
    assert out.data["allowed_kinds"] == ["theme", "plugin", "mu_plugin"]
    assert "kind='theme'" in out.data["hint"]


def test_open_review_workspace_long_summary_returns_hint_with_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(tmp_path, monkeypatch)
    out = tools._execute_file(
        "wp_open_review_workspace",
        {"site_id": "mock-shop", "kind": "theme", "summary": "x" * 201},
        actor="oscar",
        sticky_site_id=None,
    )
    assert out.data["ok"] is False
    assert out.data["error_class"] == "InvalidToolArguments"
    assert out.data["allowed_kinds"] == ["theme", "plugin", "mu_plugin"]
    assert "200" in out.data["hint"]


@pytest.mark.parametrize(
    ("extra_args", "expected_fragment"),
    [
        ({"slug": "Invalid Slug!"}, "slug"),
        ({"variant_group": "Bad Group!"}, "variant_group"),
        ({"base_plan_id": "not-a-plan"}, "base_plan_id"),
    ],
)
def test_open_review_workspace_other_invalid_args_include_specific_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: dict[str, object],
    expected_fragment: str,
) -> None:
    tools = _tools(tmp_path, monkeypatch)
    args = {"site_id": "mock-shop", "kind": "theme", "summary": "Test", **extra_args}
    out = tools._execute_file("wp_open_review_workspace", args, actor="oscar", sticky_site_id=None)
    assert out.data["ok"] is False
    assert out.data["error_class"] == "InvalidToolArguments"
    assert expected_fragment in out.data["hint"]


def test_build_sample_data_invalid_scenario_returns_actionable_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(tmp_path, monkeypatch)
    out = tools._execute_file(
        "wp_build_sample_data",
        {"site_id": "mock-shop", "scenario": "order_confirmation"},
        actor="oscar",
        sticky_site_id=None,
    )
    assert out.data["ok"] is False
    assert out.data["error_class"] == "InvalidToolArguments"
    assert out.data["allowed_scenarios"] == sorted(SAMPLE_SCENARIOS)
    assert "thankyou" in out.data["hint"]


@pytest.mark.parametrize(
    ("extra_args", "expected_fragment"),
    [
        ({"product_count": 9}, "product_count"),
        ({"product_ids": [1, 2, 3, 4, 5]}, "product_ids"),
        ({"product_ids": ["abc"]}, "product_ids"),
        ({"guide_search": "g" * 61}, "guide_search"),
        ({"currency": "TOOLONGCURRENCY"}, "currency"),
    ],
)
def test_build_sample_data_other_invalid_args_include_specific_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: dict[str, object],
    expected_fragment: str,
) -> None:
    tools = _tools(tmp_path, monkeypatch)
    args = {"site_id": "mock-shop", "scenario": "thankyou", **extra_args}
    out = tools._execute_file("wp_build_sample_data", args, actor="oscar", sticky_site_id=None)
    assert out.data["ok"] is False
    assert out.data["error_class"] == "InvalidToolArguments"
    assert expected_fragment in out.data["hint"]


def test_new_page_and_sample_data_playbook_documents_theme_kind() -> None:
    assert "new_page_and_sample_data" in PLAYBOOK_TOPICS
    playbook = get_playbook("new_page_and_sample_data")
    assert playbook is not None
    steps_text = " ".join(playbook["steps"])
    assert "kind='theme'" in steps_text
    assert "kind='page'" in steps_text
    assert "wp_workspace_page_draft" in steps_text
