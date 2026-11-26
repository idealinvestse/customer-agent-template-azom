"""Tests for ReviewPlans.run_checks() with sandbox disabled vs enabled and agent tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import (
    MOCK_REGISTRY,
    make_png,
)

from ecom_ops.integrations import wp_browser_smoke
from ecom_ops.wp_ops import review_plan as review_plan_mod
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.files import LocalTree
from ecom_ops.wp_ops.review_checks import JOBS
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import (
    VerifiedPlugin,
    VerifiedTheme,
    workspace_root,
)
from ecom_ops.wp_ops.service import WpOpsService


def _setup_mock_staging_theme(tmp_path: Path, slug: str = "twentytwentyfive") -> Path:
    theme_dir = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / slug
    theme_dir.mkdir(parents=True, exist_ok=True)
    (theme_dir / "style.css").write_text("/* Theme Name: Test */\n", encoding="utf-8")
    (theme_dir / "functions.php").write_text("<?php\n", encoding="utf-8")
    (theme_dir / "index.php").write_text("<?php\necho 'ok';\n", encoding="utf-8")
    return theme_dir


def _setup_mock_staging_plugin(tmp_path: Path, slug: str = "my-plugin") -> Path:
    plugin_dir = tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "plugins" / slug
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / f"{slug}.php").write_text(
        "<?php\n/*\nPlugin Name: My Plugin\n*/\n",
        encoding="utf-8",
    )
    return plugin_dir


def test_review_plans_run_checks_sandbox_off_vs_on_and_agent_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _setup_mock_staging_theme(tmp_path)
    _setup_mock_staging_plugin(tmp_path)

    plans = ReviewPlans(
        data_dir=tmp_path,
        registry_path=MOCK_REGISTRY,
        transport_factory=lambda site: LocalTree(site.files_root, tmp_path / "wp_files_mock" / site.id),
    )
    opened = plans.open(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        actor="oscar",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfour"),
    )
    assert opened.ok
    plan_id = opened.data["plan_id"]

    # AZOM_WP_SANDBOX=0 -> run_catalog used, _run_sandbox_catalog NOT called
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    catalog_calls: list[str] = []

    def _spy_catalog(kind, files, risk, *, artifact_dir=None):
        catalog_calls.append(kind)
        return [
            {
                "job": job,
                "status": "unavailable",
                "summary": f"{job} unavailable",
                "command": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "artifact_path": None,
                "executed": False,
                "simulated": False,
            }
            for job in JOBS
        ]

    monkeypatch.setattr(review_plan_mod, "run_catalog", _spy_catalog)
    monkeypatch.setattr(
        ReviewPlans,
        "_run_sandbox_catalog",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run sandbox catalog")),
    )
    res_off = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert res_off.ok
    assert catalog_calls == ["theme"]
    assert res_off.data["screenshots"] == []

    # AZOM_WP_SANDBOX=1 -> delegates to _run_sandbox_catalog + run_vertical_theme_render
    monkeypatch.undo()
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")

    png_bytes = make_png(4, 4)

    def _fake_theme_render(**kwargs):
        adir = Path(kwargs["artifact_dir"])
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "work-desktop.png").write_bytes(png_bytes)
        (adir / "work-mobile.png").write_bytes(png_bytes)
        (adir / "evil.png").write_bytes(png_bytes)
        (adir / "base-desktop.png").write_bytes(b"not-a-png")
        return {
            "checks": [
                {
                    "job": job,
                    "status": "passed" if job in {"php_lint", "browser_smoke", "egress_probe"} else "unavailable",
                    "summary": f"{job}",
                    "command": ["cmd", job] if job in {"php_lint", "browser_smoke", "egress_probe"} else None,
                    "started_at": "2026-11-24T00:00:00+00:00",
                    "finished_at": "2026-11-24T00:00:01+00:00",
                    "exit_code": 0 if job in {"php_lint", "browser_smoke", "egress_probe"} else None,
                    "artifact_path": None,
                    "executed": job in {"php_lint", "browser_smoke", "egress_probe"},
                    "simulated": False,
                }
                for job in JOBS
            ]
        }

    monkeypatch.setattr(wp_browser_smoke, "run_vertical_theme_render", _fake_theme_render)

    service = WpOpsService(
        registry_path=MOCK_REGISTRY,
        data_dir=tmp_path,
        files_factory=lambda site: LocalTree(site.files_root, tmp_path / "wp_files_mock" / site.id),
    )
    tools = WpAgentTools(service)
    tool_out = tools.execute(
        "wp_workspace_run_checks",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="oscar",
    )
    assert tool_out.data["ok"] is True
    assert tool_out.data["screenshots"] == ["work-desktop.png", "work-mobile.png"]
    serialized = json.dumps(tool_out.data)
    assert "screenshot_paths" not in serialized
    adir = workspace_root(tmp_path, "mock-staging", plan_id) / "artifacts"
    assert str(adir) not in serialized
    assert tool_out.attachments == (
        str(adir / "work-desktop.png"),
        str(adir / "work-mobile.png"),
    )

    # Also verify plugin plan delegates to run_vertical_plugin_smoke
    plugin_opened = plans.open(
        "mock-staging",
        "plugin",
        "my-plugin",
        actor="oscar",
        verified_plugin=VerifiedPlugin("my-plugin"),
    )
    assert plugin_opened.ok
    plugin_calls: list[str] = []

    def _fake_plugin_smoke(**kwargs):
        plugin_calls.append(kwargs["slug"])
        return {
            "checks": [
                {
                    "job": job,
                    "status": "passed" if job in {"php_lint", "egress_probe"} else "unavailable",
                    "summary": job,
                    "command": ["cmd", job] if job in {"php_lint", "egress_probe"} else None,
                    "started_at": "2026-11-24T00:00:00+00:00",
                    "finished_at": "2026-11-24T00:00:01+00:00",
                    "exit_code": 0 if job in {"php_lint", "egress_probe"} else None,
                    "artifact_path": None,
                    "executed": job in {"php_lint", "egress_probe"},
                    "simulated": False,
                }
                for job in JOBS
            ]
        }

    monkeypatch.setattr(wp_browser_smoke, "run_vertical_plugin_smoke", _fake_plugin_smoke)
    plugin_res = plans.run_checks("mock-staging", plugin_opened.data["plan_id"], actor="oscar")
    assert plugin_res.ok
    assert plugin_calls == ["my-plugin"]
