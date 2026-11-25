"""Opt-in local WordPress + headless browser vertical theme render E2E tests.

The default suite skips this file. Set ``AZOM_WP_RUNTIME_E2E=1`` (with the
pinned local WordPress 7.1.5 + PHP 8.3.37 runtime fetched via
``bin/fetch-wp-runtime.py`` and a local Chromium/Edge binary) to execute real
local renders against synthetic themes only. Never contacts ``azom.no`` or any
customer site.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ecom_ops.integrations.wp_browser_smoke import find_browser_binary, run_vertical_theme_render
from ecom_ops.integrations.wp_local_runtime import runtime_dir_from_env
from ecom_ops.integrations.wp_runtime_fetch import check_runtime
from ecom_ops.wp_ops.review_checks import PROFILE, PROFILE_VERSION, requirements
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import VerifiedTheme, workspace_root

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"

pytestmark = pytest.mark.skipif(
    os.environ.get("AZOM_WP_RUNTIME_E2E") != "1",
    reason=(
        "AZOM_WP_RUNTIME_E2E is unset; no WordPress container or browser run is claimed"
    ),
)


def _site():
    return type(
        "SyntheticSite",
        (),
        {"php_version": "8.3.37", "wordpress_version": "7.1.5", "woocommerce": False},
    )()


def _write_synthetic_theme(folder: Path, *, marker: str, bg_hex: str, extra_php: str = "", extra_html: str = "") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "style.css").write_text(
        (
            "/*\n"
            "Theme Name: Azom Synthetic Fixture\n"
            "Version: 1.0.0\n"
            "*/\n"
            f"body {{ background-color: {bg_hex}; color: #111111; font-family: sans-serif; margin: 0; padding: 24px; }}\n"
            ".azom-card { max-width: 720px; border: 2px solid #222222; padding: 16px; }\n"
        ),
        encoding="utf-8",
    )
    (folder / "functions.php").write_text(
        (
            "<?php\n"
            "add_action('wp_enqueue_scripts', function () {\n"
            "    wp_enqueue_style('azom-synthetic-style', get_stylesheet_uri(), array(), '1.0.0');\n"
            "});\n"
            f"{extra_php}\n"
        ),
        encoding="utf-8",
    )
    (folder / "index.php").write_text(
        (
            "<?php\n"
            "?><!doctype html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\">\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "  <title><?php bloginfo('name'); ?> - " + marker + "</title>\n"
            "  <?php wp_head(); ?>\n"
            "</head>\n"
            "<body>\n"
            "  <main class=\"azom-card\" data-azom-theme-marker=\"" + marker + "\">\n"
            "    <h1>" + marker + "</h1>\n"
            "    <p>WordPress version: <?php echo esc_html(get_bloginfo('version')); ?></p>\n"
            "    <?php if (have_posts()) : while (have_posts()) : the_post(); ?>\n"
            "      <article><h2><?php the_title(); ?></h2><div><?php the_content(); ?></div></article>\n"
            "    <?php endwhile; endif; ?>\n"
            f"    {extra_html}\n"
            "  </main>\n"
            "  <?php wp_footer(); ?>\n"
            "</body>\n"
            "</html>\n"
        ),
        encoding="utf-8",
    )


def _require_local_runtime() -> tuple[Path, str]:
    rt_dir = runtime_dir_from_env()
    manifest = check_runtime(rt_dir)
    assert manifest["wordpress_version"] == "7.1.5"
    assert manifest["php_version"] == "8.3.37"
    browser = find_browser_binary()
    assert browser, "Local Chromium/Edge browser binary missing for AZOM_WP_RUNTIME_E2E=1"
    return rt_dir, browser


def test_local_wordpress_vertical_theme_render_base_work_desktop_mobile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rt_dir, browser = _require_local_runtime()
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("WP_APP_PASSWORD", "must-never-enter-sandbox")

    theme_mock = tmp_path / "data" / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "azom-synth"
    _write_synthetic_theme(theme_mock, marker="AZOM-BASE-REV", bg_hex="#f0f4ff")

    plans = ReviewPlans(data_dir=tmp_path / "data", registry_path=REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-synth",
        actor="oscar",
        verified_theme=VerifiedTheme("azom-synth", "azom-synth-parent"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    root = workspace_root(tmp_path / "data", "mock-staging", plan_id)

    _write_synthetic_theme(root / "work", marker="AZOM-WORK-REV", bg_hex="#e6ffe6")
    refreshed = plans.refresh("mock-staging", plan_id, actor="oscar")
    assert refreshed.ok is True
    plan = plans.show("mock-staging", plan_id, actor="oscar").data["plan"]

    result = run_vertical_theme_render(
        site=_site(),
        site_id="mock-staging",
        plan_id=plan_id,
        slug="azom-synth",
        base_dir=root / "base",
        work_dir=root / "work",
        artifact_dir=root / "artifacts",
        risk=plan["risk"],
        runtime_dir=rt_dir,
        browser_binary=browser,
    )

    # SandboxGate reported real running loopback process, then stopped cleanly, never publishing a port.
    for rev in ("base", "work"):
        rec = result["sandbox_records"][rev]
        assert rec["state"] == "running"
        assert rec["engine"] == "local_php_wp"
        assert rec["network"] == "loopback_only"
        assert rec["port"] is None
        assert rec["published_port"] is False
        assert rec["docker_socket"] is False
        assert rec["broad_host_mount"] is False
        assert "WP_APP_PASSWORD" not in rec["env_names"]
        assert result["sandbox_records"][f"{rev}_stopped"]["state"] == "stopped"

    # Both base and work rendered real WordPress 7.1.5 pages at desktop (1280x800) and mobile (390x844).
    base_rev = result["revisions"]["base"]
    work_rev = result["revisions"]["work"]
    assert base_rev["ok"] is True and base_rev["php_errors"] == []
    assert work_rev["ok"] is True and work_rev["php_errors"] == []

    for vp_name, dims in (("desktop", [1280, 800]), ("mobile", [390, 844])):
        b_vp = base_rev["viewports"][vp_name]
        w_vp = work_rev["viewports"][vp_name]
        assert b_vp["http_status"] == 200 and w_vp["http_status"] == 200
        assert b_vp["screenshot_dimensions"] == dims and w_vp["screenshot_dimensions"] == dims
        assert Path(b_vp["screenshot_path"]).is_file() and b_vp["screenshot_bytes"] > 1000
        assert Path(w_vp["screenshot_path"]).is_file() and w_vp["screenshot_bytes"] > 1000
        assert b_vp["theme_marker"] == "AZOM-BASE-REV"
        assert w_vp["theme_marker"] == "AZOM-WORK-REV"
        assert "WordPress version: 7.1.5" in b_vp["body_text"]
        assert "WordPress version: 7.1.5" in w_vp["body_text"]
        assert b_vp["body_bg"] != w_vp["body_bg"]
        assert b_vp["html_sha256"] != w_vp["html_sha256"]
        assert b_vp["console_errors"] == [] and w_vp["console_errors"] == []
        assert b_vp["network_errors"] == [] and w_vp["network_errors"] == []

    # Bound evidence checks: php_lint, browser_smoke, and egress_probe really executed and passed;
    # wpcs_php honestly remains unavailable (not forged).
    by_job = {row["job"]: row for row in result["checks"]}
    assert by_job["php_lint"]["status"] == "passed" and by_job["php_lint"]["executed"] is True
    assert by_job["browser_smoke"]["status"] == "passed" and by_job["browser_smoke"]["executed"] is True
    assert by_job["egress_probe"]["status"] == "passed" and by_job["egress_probe"]["executed"] is True
    assert by_job["wpcs_php"]["status"] == "unavailable" and by_job["wpcs_php"]["executed"] is False
    assert result["status"] == "checks_incomplete"

    binding = result["binding"]
    expected_policy = requirements(
        "theme",
        [(p.relative_to(root / "work").as_posix(), p.read_text(encoding="utf-8")) for p in sorted((root / "work").rglob("*")) if p.is_file()],
        plan["risk"],
    )
    assert binding["site_id"] == "mock-staging"
    assert binding["plan_id"] == plan_id
    assert binding["base_sha256"] == plan["base_sha256"]
    assert binding["work_sha256"] == plan["work_sha256"]
    assert binding["policy"] == expected_policy
    assert binding["profile"] == PROFILE
    assert binding["profile_version"] == PROFILE_VERSION
    for row in result["checks"]:
        assert row["run_id"] == result["run_id"]
        assert row["site_id"] == "mock-staging"
        assert row["plan_id"] == plan_id
        assert row["base_sha256"] == plan["base_sha256"]
        assert row["work_sha256"] == plan["work_sha256"]
        assert row["profile"] == PROFILE
        assert row["profile_version"] == PROFILE_VERSION

    # JSON artifacts carry the exact run evidence binding.
    for job_name in ("php_lint", "browser_smoke", "egress_probe"):
        artifact = json.loads(Path(by_job[job_name]["artifact_path"]).read_text(encoding="utf-8"))
        assert artifact["evidence"]["run_id"] == result["run_id"]
        assert artifact["evidence"]["site_id"] == "mock-staging"
        assert artifact["evidence"]["plan_id"] == plan_id
        assert artifact["evidence"]["base_sha256"] == plan["base_sha256"]
        assert artifact["evidence"]["work_sha256"] == plan["work_sha256"]


def test_local_wordpress_negative_syntax_and_egress_fixtures_fail_honestly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rt_dir, browser = _require_local_runtime()
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    base_dir = tmp_path / "base"
    broken_dir = tmp_path / "work-broken"
    _write_synthetic_theme(base_dir, marker="AZOM-BASE-OK", bg_hex="#ffffff")
    _write_synthetic_theme(broken_dir, marker="AZOM-BROKEN", bg_hex="#ffffff")
    (broken_dir / "index.php").write_text("<?php function broken_fixture( {\n", encoding="utf-8")

    broken = run_vertical_theme_render(
        site=_site(),
        site_id="mock-staging",
        plan_id="wr-aaaaaaaaaaaa",
        slug="azom-broken",
        base_dir=base_dir,
        work_dir=broken_dir,
        artifact_dir=tmp_path / "artifacts-broken",
        runtime_dir=rt_dir,
        browser_binary=browser,
    )
    broken_jobs = {row["job"]: row for row in broken["checks"]}
    assert broken_jobs["php_lint"]["status"] == "failed"
    assert broken_jobs["php_lint"]["executed"] is True
    assert broken_jobs["php_lint"]["exit_code"] != 0
    assert broken_jobs["browser_smoke"]["status"] == "failed"
    assert broken_jobs["browser_smoke"]["executed"] is True
    assert broken["status"] == "tests_failed"

    egress_dir = tmp_path / "work-egress"
    _write_synthetic_theme(
        egress_dir,
        marker="AZOM-EGRESS",
        bg_hex="#ffffff",
        extra_php="add_action('template_redirect', function () { wp_remote_get('https://azom.no/never-contact'); });",
        extra_html='<script src="https://azom.no/never-load.js"></script>',
    )
    egress = run_vertical_theme_render(
        site=_site(),
        site_id="mock-staging",
        plan_id="wr-bbbbbbbbbbbb",
        slug="azom-egress",
        base_dir=base_dir,
        work_dir=egress_dir,
        artifact_dir=tmp_path / "artifacts-egress",
        runtime_dir=rt_dir,
        browser_binary=browser,
    )
    egress_jobs = {row["job"]: row for row in egress["checks"]}
    assert egress_jobs["php_lint"]["status"] == "passed"
    assert egress_jobs["egress_probe"]["status"] == "failed"
    assert egress_jobs["egress_probe"]["executed"] is True
    assert any(item["host"] == "azom.no" for item in egress["egress_attempts"])
    assert egress["status"] == "tests_failed"
