"""Unit tests for PR 4.3 split review plan modules and public read-model API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ecom_ops.wp_ops import review_plan
from ecom_ops.wp_ops.review import (
    check_executor,
    deploy,
    plans,
    preview_coordinator,
    store,
)
from ecom_ops.wp_ops.review_workspace import VerifiedTheme, workspace_root


def test_review_plan_facade_and_supported_kwargs_shim() -> None:
    assert review_plan.ReviewPlans is plans.ReviewPlans
    assert review_plan.PlanStore is store.PlanStore
    assert review_plan.safe_preview_html_path is preview_coordinator.safe_preview_html_path
    assert review_plan._safe_preview_html_path is preview_coordinator.safe_preview_html_path
    assert hasattr(deploy, "approve_staging")
    assert hasattr(deploy, "push_page_draft")
    assert hasattr(deploy, "request_production")

    # Narrow lambda without **kwargs only receives supported params
    narrow = lambda site, path: (site, path)  # noqa: E731
    assert preview_coordinator._call_with_supported_kwargs(
        narrow, "s1", "/home", optional_kwargs={"use_cache": True}
    ) == ("s1", "/home")

    # Wider callable receives optional kwargs
    wide = lambda site, path, use_cache=False: (site, path, use_cache)  # noqa: E731
    assert preview_coordinator._call_with_supported_kwargs(
        wide, "s1", "/home", optional_kwargs={"use_cache": True, "ignored": 1}
    ) == ("s1", "/home", True)


def test_review_plans_public_read_model_and_staging_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    now = [datetime(2026, 11, 26, 12, 0, tzinfo=UTC)]
    ops = review_plan.ReviewPlans(data_dir=tmp_path, clock=lambda: now[0])

    opened = ops.open(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        actor="oscar",
        summary="Hero polish",
        verified_theme=VerifiedTheme(stylesheet="twentytwentyfive", template="twentytwentyfive"),
    )
    assert opened.ok
    plan_id = str((opened.data or {})["plan_id"])

    # Patch style.css and write standalone preview.html + preview.json
    patched = ops.patch_file(
        "mock-staging",
        plan_id,
        "style.css",
        "Theme Name: Azom Child",
        "Theme Name: Azom Child Updated",
        actor="oscar",
    )
    assert patched.ok

    root = workspace_root(tmp_path, "mock-staging", plan_id)
    art_dir = root / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "preview.html").write_text("<html><body>preview</body></html>", encoding="utf-8")
    (art_dir / "preview.json").write_text(
        json.dumps({"preview_kind": "live_overlay", "path": "/", "focus_matched": True}),
        encoding="utf-8",
    )

    assert ops.safe_preview_html_path("mock-staging", plan_id) == (art_dir / "preview.html").resolve()

    # Before checks pass: staging_readiness is False
    plan_row = ops.get(plan_id, site_id="mock-staging")
    assert plan_row is not None
    ready, reason = ops.staging_readiness(plan_row, changed_count=1)
    assert ready is False
    assert "validated" in reason

    # Facade monkeypatch on review_plan.run_catalog is honored by check_executor
    called_jobs: list[str] = []

    def _fake_catalog(kind: str, files: list[tuple[str, str]], risk: str, *, artifact_dir: Path | None = None):
        del artifact_dir
        from ecom_ops.wp_ops.review_checks import requirements

        req = requirements(kind, files, risk)
        out = []
        for job, policy in req.items():
            called_jobs.append(job)
            if policy == "required":
                out.append(
                    {
                        "job": job,
                        "status": "passed",
                        "command": f"mock:{job}",
                        "started_at": now[0].isoformat(),
                        "finished_at": now[0].isoformat(),
                        "exit_code": 0,
                        "executed": True,
                        "simulated": False,
                    }
                )
            else:
                out.append({"job": job, "status": "not_applicable", "executed": False, "simulated": False})
        return out

    monkeypatch.setattr(review_plan, "run_catalog", _fake_catalog)
    checked = ops.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.ok
    assert (checked.data or {})["status"] == "tests_passed"
    assert called_jobs

    # Public verify_current_evidence + staging_readiness + describe_plan + describe_site_plans
    fresh_row = ops.get(plan_id, site_id="mock-staging")
    assert fresh_row is not None
    assert ops.verify_current_evidence(fresh_row) is None
    ready_after, reason_after = ops.staging_readiness(fresh_row, changed_count=1)
    assert ready_after is True
    assert reason_after == "redo för staging"

    described = ops.describe_plan("mock-staging", plan_id, actor="oscar", include_diff=True)
    assert described.ok
    detail = (described.data or {})["plan"]
    assert detail["staging_ready"] is True
    assert detail["has_preview_html"] is True
    assert detail["preview_meta"]["preview_kind"] == "live_overlay"
    assert detail["changed_files"]

    # describe_site_plans defaults to check_preview_html=False (N+1 safe)
    spy_calls = 0

    def _spy_preview(data_dir: Path, site_id: str, pid: str):
        nonlocal spy_calls
        spy_calls += 1
        return preview_coordinator.safe_preview_html_path(data_dir, site_id, pid)

    monkeypatch.setattr(review_plan, "_safe_preview_html_path", _spy_preview)
    listed = ops.describe_site_plans("mock-staging", actor="jonatan")
    assert listed.ok
    assert spy_calls == 0
    assert len((listed.data or {})["plans"]) == 1
    assert (listed.data or {})["plans"][0]["staging_ready"] is True

    # Single-read expiry via PlanStore.get_or_expire
    now[0] = now[0] + timedelta(hours=13)
    expired = ops.store.get_or_expire("mock-staging", plan_id)
    assert expired is not None
    assert expired["status"] == "expired"
    assert check_executor.verify_current_evidence(tmp_path, expired) is not None
