"""Review evidence stays bound to the run that produced it.

A stored status, a static Woo pre-check, or an older staging flag does not
open preview or local staging.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ecom_ops.integrations.wp_review_process import run_php_lint
from ecom_ops.wp_ops.review_checks import requirements, suite_status
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import VerifiedTheme, workspace_root

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"
RUN = "cr-001122334455"
PLAN = "wr-001122334455"
BASE = "a" * 64
WORK = "b" * 64


def _passed(job: str, **extra: object) -> dict:
    row: dict = {
        "job": job,
        "status": "passed",
        "command": ["fixed-tool", job],
        "started_at": "2026-11-24T00:00:00+00:00",
        "finished_at": "2026-11-24T00:00:01+00:00",
        "exit_code": 0,
        "executed": True,
        "simulated": False,
        "run_id": RUN,
        "site_id": "mock-staging",
        "plan_id": PLAN,
        "base_sha256": BASE,
        "work_sha256": WORK,
        "policy": "required",
        "profile": "wp-review-catalog",
        "profile_version": "1",
    }
    row.update(extra)
    return row


def _theme_rows() -> list[dict]:
    return [_passed(job) for job in ("php_lint", "wpcs_php", "browser_smoke", "egress_probe")]


def _plans(tmp_path: Path, **extra: object) -> ReviewPlans:
    return ReviewPlans(data_dir=tmp_path / "data", registry_path=REGISTRY, **extra)


def _theme_files(tmp_path: Path) -> Path:
    folder = tmp_path / "data" / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "twentytwentyfive"
    folder.mkdir(parents=True)
    (folder / "style.css").write_text("/* base */\n", encoding="utf-8")
    (folder / "functions.php").write_text("<?php\n", encoding="utf-8")
    return folder


def _open_copy(plans: ReviewPlans):
    return plans.open(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        actor="oscar",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfour"),
    )


def test_suite_rejects_non_passes_duplicates_and_forged_applicability():
    files = [("index.php", "<?php\n")]
    assert suite_status("theme", files, "low", _theme_rows()) == "tests_passed"
    timed = _theme_rows()
    timed[0] = _passed("php_lint", status="timeout", exit_code=124)
    assert suite_status("theme", files, "low", timed) == "checks_incomplete"
    cancelled = _theme_rows()
    cancelled[3] = _passed("egress_probe", status="cancelled", executed=False, exit_code=None)
    assert suite_status("theme", files, "low", cancelled) == "checks_incomplete"
    simulated = _theme_rows()
    simulated[1]["simulated"] = True
    assert suite_status("theme", files, "low", simulated) == "checks_incomplete"
    missing = [row for row in _theme_rows() if row["job"] != "browser_smoke"]
    assert suite_status("theme", files, "low", missing) == "checks_incomplete"
    duplicate = _theme_rows() + [_passed("php_lint")]
    assert suite_status("theme", files, "low", duplicate) == "checks_incomplete"
    text = [("readme.txt", "hello")]
    forged = [_passed("plugin_check"), _passed("egress_probe"), _passed("php_lint")]
    assert requirements("plugin", text, "low")["php_lint"] == "not_applicable"
    assert suite_status("plugin", text, "low", forged) == "checks_incomplete"


def test_woo_static_pass_is_a_static_precheck_only():
    files = [("azom.php", "<?php\n * WC tested up to: 9.0\nFeaturesUtil::declare_compatibility('custom_order_tables');\n")]
    needed = requirements("plugin", files, "high")
    assert needed["woo_static"] == "required"
    rows = [
        _passed(job)
        for job, level in needed.items()
        if level == "required" and job != "woo_static"
    ]
    bare = _passed("woo_static")
    assert suite_status("plugin", files, "high", rows + [bare]) == "checks_incomplete"
    runtime = _passed("woo_static", evidence_class="static_precheck", runtime_proof=True)
    assert suite_status("plugin", files, "high", rows + [runtime]) == "checks_incomplete"
    static = _passed(
        "woo_static",
        evidence_class="static_precheck",
        runtime_proof=False,
        command="internal:woo_static",
    )
    assert suite_status("plugin", files, "high", rows + [static]) == "tests_passed"
    from ecom_ops.wp_ops.review_checks import run_is_publishable

    binding = {
        "published": True,
        "run_id": RUN,
        "site_id": "mock-staging",
        "plan_id": PLAN,
        "base_sha256": BASE,
        "work_sha256": WORK,
        "profile": "wp-review-catalog",
        "profile_version": "1",
        "policy": needed,
    }
    assert run_is_publishable("plugin", files, "high", rows + [static], binding) is True
    forged_rows = [
        {**row, "profile": "hand-edited", "profile_version": "9"} for row in rows + [static]
    ]
    forged_binding = {**binding, "profile": "hand-edited", "profile_version": "9"}
    assert run_is_publishable("plugin", files, "high", forged_rows, forged_binding) is False
    binding["policy"] = {**needed, "woo_static": "not_applicable"}
    assert run_is_publishable("plugin", files, "high", rows + [static], binding) is False


def test_php_lint_timeout_is_not_passed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_review_process.shutil.which",
        lambda name: "php" if name == "php" else None,
    )

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["php", "-l"], timeout=8)

    monkeypatch.setattr("ecom_ops.integrations.wp_review_process.subprocess.run", _timeout)
    row = run_php_lint([("a.php", "<?php\n")], tmp_path)
    assert row["status"] == "timeout"
    assert row["executed"] is True
    assert row["simulated"] is False
    assert row["status"] != "passed"

    calls = {"n": 0}

    def _mixed(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return subprocess.CompletedProcess(args=["php", "-l"], returncode=255, stdout="syntax", stderr="")
        raise subprocess.TimeoutExpired(cmd=["php", "-l"], timeout=8)

    monkeypatch.setattr("ecom_ops.integrations.wp_review_process.subprocess.run", _mixed)
    mixed = run_php_lint([("a.php", "<?php\n"), ("b.php", "<?php\n")], tmp_path)
    assert mixed["status"] == "failed"
    assert mixed["status"] != "passed"


def test_run_checks_records_a_bound_snapshot_and_discards_a_mid_run_edit(monkeypatch, tmp_path: Path):
    _theme_files(tmp_path)
    plans = _plans(tmp_path)
    opened = _open_copy(plans)
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    root = workspace_root(tmp_path / "data", "mock-staging", plan_id)

    def _edit(kind, files, risk, artifact_dir=None):
        del kind, files, risk, artifact_dir
        (root / "work" / "functions.php").write_text("<?php\n/* mid-run */\n", encoding="utf-8")
        return _theme_rows()

    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", _edit)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.ok is True
    assert checked.data["status"] == "checks_incomplete"
    shown = plans.show("mock-staging", plan_id, actor="oscar")
    plan = shown.data["plan"]
    assert plan["status"] == "checks_incomplete"
    assert plan["error_class"] == "RevisionChanged"
    run = plan["check_run"]
    assert run["published"] is False
    assert run["site_id"] == "mock-staging"
    assert run["plan_id"] == plan_id
    assert run["profile"] == "wp-review-catalog"
    assert run["profile_version"] == "1"
    assert run["work_sha256"] != plan["work_sha256"]
    assert plan["checks"]
    assert {row["run_id"] for row in plan["checks"]} == {run["run_id"]}
    preview = plans.preview("mock-staging", plan_id, actor="oscar")
    assert preview.ok is False
    assert preview.data["error_class"] == "PreviewBlocked"


def test_refresh_during_checks_keeps_the_newer_plan(monkeypatch, tmp_path: Path):
    _theme_files(tmp_path)
    plans = _plans(tmp_path)
    opened = _open_copy(plans)
    plan_id = opened.data["plan_id"]

    def _refresh(kind, files, risk, artifact_dir=None):
        del kind, files, risk, artifact_dir
        plans.place(
            "mock-staging",
            plan_id,
            "functions.php",
            b"<?php\n/* newer */\n",
            actor="oscar",
        )
        return _theme_rows()

    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", _refresh)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.ok is False
    assert checked.data["error_class"] == "StaleRun"
    shown = plans.show("mock-staging", plan_id, actor="oscar")
    plan = shown.data["plan"]
    assert plan["status"] == "validated"
    assert plan["checks"] == []
    assert plan["status"] != "tests_passed"


def test_expired_plan_is_not_revived_by_a_late_run(monkeypatch, tmp_path: Path):
    clock = {"now": datetime(2026, 11, 24, tzinfo=UTC)}
    _theme_files(tmp_path)
    plans = _plans(tmp_path, clock=lambda: clock["now"])
    opened = _open_copy(plans)
    plan_id = opened.data["plan_id"]

    def _expire(kind, files, risk, artifact_dir=None):
        del kind, files, risk, artifact_dir
        clock["now"] = clock["now"] + timedelta(hours=13)
        return _theme_rows()

    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", _expire)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.ok is False
    assert checked.data["error_class"] == "Expired"
    shown = plans.show("mock-staging", plan_id, actor="jonatan")
    assert shown.ok is False
    assert shown.data["status"] == "expired"


def test_old_staging_approval_and_unbound_pass_do_not_open_gates(tmp_path: Path):
    theme = _theme_files(tmp_path)
    before = (theme / "style.css").read_text(encoding="utf-8")
    plans = _plans(tmp_path)
    opened = _open_copy(plans)
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    placed = plans.place(
        "mock-staging",
        plan_id,
        "style.css",
        b"/* base */\n/* patch */\n",
        actor="oscar",
    )
    assert placed.ok is True
    shown = plans.show("mock-staging", plan_id, actor="oscar")
    plan = shown.data["plan"]
    saved = json.loads((tmp_path / "data" / "wp_review_plans.json").read_text(encoding="utf-8"))
    saved["plans"][0]["status"] = "approved_for_staging"
    saved["plans"][0]["checks"] = []
    (tmp_path / "data" / "wp_review_plans.json").write_text(json.dumps(saved), encoding="utf-8")
    staged = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=plan["base_sha256"],
        work_sha256=plan["work_sha256"],
    )
    assert staged.ok is False
    assert staged.data["error_class"] == "EvidenceMissing"
    assert (theme / "style.css").read_text(encoding="utf-8") == before

    saved = json.loads((tmp_path / "data" / "wp_review_plans.json").read_text(encoding="utf-8"))
    saved["plans"][0]["status"] = "tests_passed"
    saved["plans"][0]["checks"] = [
        {
            "job": "php_lint",
            "status": "passed",
            "executed": True,
            "simulated": False,
            "exit_code": 0,
            "command": ["php", "-l"],
            "started_at": "2026-11-24T00:00:00+00:00",
            "finished_at": "2026-11-24T00:00:01+00:00",
        }
    ]
    (tmp_path / "data" / "wp_review_plans.json").write_text(json.dumps(saved), encoding="utf-8")
    preview = plans.preview("mock-staging", plan_id, actor="oscar")
    assert preview.ok is False
    assert preview.data["error_class"] == "EvidenceMissing"
    readable = plans.show("mock-staging", plan_id, actor="jonatan")
    assert readable.ok is True
    assert readable.data["plan"]["status"] == "tests_passed"
    assert readable.data["plan"]["checks"][0]["job"] == "php_lint"


def test_change_after_a_published_run_blocks_preview(monkeypatch, tmp_path: Path):
    _theme_files(tmp_path)
    plans = _plans(tmp_path)
    opened = _open_copy(plans)
    plan_id = opened.data["plan_id"]
    root = workspace_root(tmp_path / "data", "mock-staging", plan_id)

    def _pass(kind, files, risk, artifact_dir=None):
        del files, artifact_dir
        needed = requirements(kind, [("functions.php", "<?php\n"), ("style.css", "/* base */\n")], risk)
        return [_passed(job, policy=level) for job, level in needed.items() if level == "required"]

    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", _pass)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.data["status"] == "tests_passed"
    assert checked.data["check_run"]["published"] is True
    (root / "work" / "style.css").write_text("/* base */\n/* after */\n", encoding="utf-8")
    preview = plans.preview("mock-staging", plan_id, actor="oscar")
    assert preview.ok is False
    assert preview.data["error_class"] == "RevisionStale"
    staged = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=checked.data["check_run"]["base_sha256"],
        work_sha256=checked.data["check_run"]["work_sha256"],
    )
    assert staged.ok is False
    assert staged.data["error_class"] in {"DigestMismatch", "RevisionStale", "EvidenceMissing"}


def test_published_evidence_does_not_open_an_unavailable_preview(monkeypatch, tmp_path: Path):
    _theme_files(tmp_path)
    plans = _plans(tmp_path)
    opened = _open_copy(plans)
    plan_id = opened.data["plan_id"]

    def _pass(kind, files, risk, artifact_dir=None):
        del files, artifact_dir
        needed = requirements(kind, [("functions.php", "<?php\n"), ("style.css", "/* base */\n")], risk)
        return [_passed(job, policy=level) for job, level in needed.items() if level == "required"]

    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", _pass)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.data["status"] == "tests_passed"
    preview = plans.preview("mock-staging", plan_id, actor="oscar")
    assert preview.ok is False
    assert preview.data["error_class"] == "PreviewBlocked"
    shown = plans.show("mock-staging", plan_id, actor="oscar")
    assert shown.data["plan"]["sandbox"]["state"] != "running"
    assert shown.data["plan"]["sandbox"]["container"] != "running"


def test_forged_profile_does_not_open_staging(monkeypatch, tmp_path: Path):
    theme = _theme_files(tmp_path)
    before = (theme / "style.css").read_text(encoding="utf-8")
    plans = _plans(tmp_path)
    opened = _open_copy(plans)
    plan_id = opened.data["plan_id"]
    placed = plans.place(
        "mock-staging",
        plan_id,
        "style.css",
        b"/* base */\n/* patch */\n",
        actor="oscar",
    )
    assert placed.ok is True

    def _pass(kind, files, risk, artifact_dir=None):
        del files, artifact_dir
        needed = requirements(kind, [("functions.php", "<?php\n"), ("style.css", "/* base */\n")], risk)
        return [_passed(job, policy=level) for job, level in needed.items() if level == "required"]

    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", _pass)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.data["status"] == "tests_passed"
    saved = json.loads((tmp_path / "data" / "wp_review_plans.json").read_text(encoding="utf-8"))
    plan = saved["plans"][0]
    plan["check_run"]["profile"] = "hand-edited"
    plan["check_run"]["profile_version"] = "9"
    for row in plan["checks"]:
        row["profile"] = "hand-edited"
        row["profile_version"] = "9"
    (tmp_path / "data" / "wp_review_plans.json").write_text(json.dumps(saved), encoding="utf-8")
    shown = plans.show("mock-staging", plan_id, actor="oscar")
    staged = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=shown.data["plan"]["base_sha256"],
        work_sha256=shown.data["plan"]["work_sha256"],
    )
    assert staged.ok is False
    assert staged.data["error_class"] == "EvidenceMissing"
    assert (theme / "style.css").read_text(encoding="utf-8") == before
    preview = plans.preview("mock-staging", plan_id, actor="oscar")
    assert preview.ok is False
    assert preview.data["error_class"] == "EvidenceMissing"


def test_missing_work_tree_is_not_resumable(tmp_path: Path):
    _theme_files(tmp_path)
    plans = _plans(tmp_path)
    opened = _open_copy(plans)
    plan_id = opened.data["plan_id"]
    root = workspace_root(tmp_path / "data", "mock-staging", plan_id)
    shutil.rmtree(root / "work")
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.ok is False
    assert checked.data["error_class"] == "WorkspaceNotResumable"
    shown = plans.show("mock-staging", plan_id, actor="jonatan")
    assert shown.ok is True
    assert shown.data["plan"]["status"] == "not_resumable"
