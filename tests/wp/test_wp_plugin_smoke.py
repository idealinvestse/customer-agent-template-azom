"""Tests for vertical plugin smoke checks in local WordPress sandbox."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ecom_ops.integrations import wp_browser_smoke
from ecom_ops.integrations.wp_browser_smoke import run_vertical_plugin_smoke
from ecom_ops.integrations.wp_local_runtime import (
    LocalWordPressSandbox,
)
from ecom_ops.wp_ops.review_checks import JOBS
from ecom_ops.wp_ops.review_workspace import ReviewError


def _plugin_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    base_dir = tmp_path / "base"
    work_dir = tmp_path / "work"
    artifact_dir = tmp_path / "artifacts"
    for folder in (base_dir, work_dir):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "my-plugin.php").write_text("<?php\n/* Plugin Name: Demo */\n", encoding="utf-8")
    return base_dir, work_dir, artifact_dir


def test_run_vertical_plugin_smoke_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    base_dir, work_dir, artifact_dir = _plugin_dirs(tmp_path)
    with pytest.raises(ReviewError) as raised:
        run_vertical_plugin_smoke(
            site=SimpleNamespace(id="mock-staging"),
            site_id="mock-staging",
            plan_id="wr-0123456789ab",
            slug="my-plugin",
            base_dir=base_dir,
            work_dir=work_dir,
            artifact_dir=artifact_dir,
        )
    assert raised.value.error_class == "SandboxDisabled"


def test_run_vertical_plugin_smoke_pass_and_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")
    monkeypatch.setattr(
        wp_browser_smoke,
        "resolve_local_runtime",
        lambda _rt: {
            "php_path": "/usr/bin/php",
            "wordpress_path": str(tmp_path / "wp"),
            "wordpress_version": "7.1.5",
            "php_version": "8.3.14",
            "system_php": True,
        },
    )

    def _fake_start(self):
        self.port = 18080
        self.url = "http://127.0.0.1:18080/"
        return {
            "engine": "local_php_wp",
            "database": "synthetic_sqlite",
            "network": "loopback_only",
            "env_names": ["AZOM_REVIEW_SYNTHETIC"],
            "state": "running",
            "process_alive": True,
            "bind_host": "127.0.0.1",
            "port": None,
            "published_port": False,
            "docker_socket": False,
            "broad_host_mount": False,
            "payments_enabled": False,
            "checkout_enabled": False,
            "admin_password_stored": False,
            "pid": 9999,
            "plan_id": self.plan_id,
        }

    monkeypatch.setattr(LocalWordPressSandbox, "start", _fake_start)
    monkeypatch.setattr(LocalWordPressSandbox, "stop", lambda self: None)
    monkeypatch.setattr(LocalWordPressSandbox, "read_php_errors", lambda self: [])
    monkeypatch.setattr(
        wp_browser_smoke,
        "run_browser_render",
        lambda url, adir, **kw: {
            "ok": True,
            "executed": True,
            "simulated": False,
            "label": kw.get("label", "work"),
            "command": ["chrome", "--headless=new"],
            "viewports": {"desktop": {"ok": True}},
            "egress_attempts": [],
        },
    )

    site = SimpleNamespace(
        id="mock-staging",
        php_version="8.3.37",
        wordpress_version="7.1.5",
        woocommerce=False,
    )

    # Clean pass
    base_dir, work_dir, artifact_dir = _plugin_dirs(tmp_path / "clean")
    monkeypatch.setattr(LocalWordPressSandbox, "read_egress_attempts", lambda self: [])
    monkeypatch.setattr(
        wp_browser_smoke,
        "run_php_lint",
        lambda files, adir, **kw: {
            "job": "php_lint",
            "status": "passed",
            "summary": "php_lint passed",
            "command": ["php", "-l", "my-plugin.php"],
            "started_at": "2026-11-24T00:00:00+00:00",
            "finished_at": "2026-11-24T00:00:01+00:00",
            "exit_code": 0,
            "artifact_path": None,
            "executed": True,
            "simulated": False,
        },
    )
    clean_out = run_vertical_plugin_smoke(
        site=site,
        site_id="mock-staging",
        plan_id="wr-0123456789ab",
        slug="my-plugin",
        base_dir=base_dir,
        work_dir=work_dir,
        artifact_dir=artifact_dir,
    )
    assert clean_out["plugin_boot_ok"] is True
    by_job = {row["job"]: row for row in clean_out["checks"]}
    assert set(by_job) == set(JOBS)
    assert by_job["php_lint"]["status"] == "passed"
    assert by_job["php_lint"]["executed"] is True
    assert by_job["php_lint"]["simulated"] is False
    assert by_job["egress_probe"]["status"] == "passed"
    assert by_job["egress_probe"]["executed"] is True
    assert by_job["egress_probe"]["simulated"] is False
    for unavail_job in ("wpcs_php", "plugin_check"):
        assert by_job[unavail_job]["status"] == "unavailable"
        assert by_job[unavail_job]["executed"] is False
        assert by_job[unavail_job]["simulated"] is False

    # PHP lint failure
    base2, work2, adir2 = _plugin_dirs(tmp_path / "lint_fail")
    monkeypatch.setattr(
        wp_browser_smoke,
        "run_php_lint",
        lambda files, adir, **kw: {
            "job": "php_lint",
            "status": "failed",
            "summary": "php_lint failed",
            "command": ["php", "-l", "my-plugin.php"],
            "started_at": "2026-11-24T00:00:00+00:00",
            "finished_at": "2026-11-24T00:00:01+00:00",
            "exit_code": 1,
            "artifact_path": None,
            "executed": True,
            "simulated": False,
        },
    )
    lint_fail_out = run_vertical_plugin_smoke(
        site=site,
        site_id="mock-staging",
        plan_id="wr-0123456789ac",
        slug="my-plugin",
        base_dir=base2,
        work_dir=work2,
        artifact_dir=adir2,
    )
    assert lint_fail_out["status"] == "tests_failed"
    fail_by_job = {row["job"]: row for row in lint_fail_out["checks"]}
    assert fail_by_job["php_lint"]["status"] == "failed"
    assert fail_by_job["php_lint"]["executed"] is True
    assert fail_by_job["wpcs_php"]["status"] == "unavailable"
    assert fail_by_job["plugin_check"]["status"] == "unavailable"

    # Egress failure
    base3, work3, adir3 = _plugin_dirs(tmp_path / "egress_fail")
    monkeypatch.setattr(
        wp_browser_smoke,
        "run_php_lint",
        lambda files, adir, **kw: {
            "job": "php_lint",
            "status": "passed",
            "summary": "php_lint passed",
            "command": ["php", "-l", "my-plugin.php"],
            "started_at": "2026-11-24T00:00:00+00:00",
            "finished_at": "2026-11-24T00:00:01+00:00",
            "exit_code": 0,
            "artifact_path": None,
            "executed": True,
            "simulated": False,
        },
    )
    monkeypatch.setattr(
        LocalWordPressSandbox,
        "read_egress_attempts",
        lambda self: [{"url": "https://evil.example/leak", "host": "evil.example", "source": "wordpress_http"}],
    )
    egress_fail_out = run_vertical_plugin_smoke(
        site=site,
        site_id="mock-staging",
        plan_id="wr-0123456789ad",
        slug="my-plugin",
        base_dir=base3,
        work_dir=work3,
        artifact_dir=adir3,
    )
    assert egress_fail_out["status"] == "tests_failed"
    eg_by_job = {row["job"]: row for row in egress_fail_out["checks"]}
    assert eg_by_job["egress_probe"]["status"] == "failed"
    assert eg_by_job["egress_probe"]["exit_code"] == 1
    assert eg_by_job["egress_probe"]["executed"] is True
    assert eg_by_job["egress_probe"]["simulated"] is False
    assert eg_by_job["wpcs_php"]["executed"] is False
    assert eg_by_job["plugin_check"]["executed"] is False
