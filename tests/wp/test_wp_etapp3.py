"""Unit and integration tests for Etapp 3 WordPress sandbox, artifacts, Telegram photos, and page review."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import sqlite3
import struct
import subprocess
import sys
import urllib.request
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecom_ops.bot import __main__ as bot_main
from ecom_ops.bot.chat_agent import ChatResult
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.reply import BotReply, safe_workspace_attachments
from ecom_ops.bot.store import ConversationStore
from ecom_ops.integrations import wp_browser_smoke, wp_local_runtime
from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient
from ecom_ops.integrations.wp_browser_smoke import run_vertical_plugin_smoke
from ecom_ops.integrations.wp_local_runtime import (
    LocalWordPressSandbox,
    _init_sqlite_db,
    _php_serialize_list,
    resolve_local_runtime,
)
from ecom_ops.integrations.wp_runtime_fetch import RuntimeFetchError
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops import review_plan as review_plan_mod
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.files import LocalTree
from ecom_ops.wp_ops.page_review_intent import extract_page_review_target, is_page_review
from ecom_ops.wp_ops.page_review_loop import run_bounded_page_review_loop
from ecom_ops.wp_ops.review_checks import JOBS
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import (
    ReviewError,
    VerifiedPlugin,
    VerifiedTheme,
    workspace_root,
)
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[2]
DASH_DIR = ROOT / "infrastructure" / "dashboard"
REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"


def _make_png(width: int = 2, height: int = 2) -> bytes:
    """Build a minimal valid PNG payload."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + (b"\x10\x20\x30" * width) for _ in range(height))
    idat = zlib.compress(raw)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    for name in ("azom_dashboard", "wp_routes", "settings_store", "auth", "support"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _dash_auth(client, user: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{user}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    client.get("/wp", headers=headers)
    with client.session_transaction() as sess:
        csrf = sess.get("csrf_token")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


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


# --------------------------------------------------------------------------- #
# 1. _php_serialize_list and _init_sqlite_db
# --------------------------------------------------------------------------- #


def test_php_serialize_list_empty_and_utf8_bytes() -> None:
    assert _php_serialize_list([]) == "a:0:{}"
    serialized = _php_serialize_list(["foo/foo.php", "åäö/b.php"])
    assert serialized == 'a:2:{i:0;s:11:"foo/foo.php";i:1;s:12:"åäö/b.php";}'


def test_init_sqlite_db_stores_valid_active_plugins_and_rejects_bad_format(tmp_path: Path) -> None:
    db_path = tmp_path / "wp.sqlite3"
    plugins = ["my-plugin/my-plugin.php", "helper_2/init-1.php"]
    _init_sqlite_db(
        db_path,
        base_url="http://127.0.0.1:8080",
        theme_slug="azom-plugin-host",
        active_plugins=plugins,
    )
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT option_value FROM wp_options WHERE option_name = 'active_plugins'"
        ).fetchone()
    assert row is not None
    assert row[0] == _php_serialize_list(plugins)

    db_empty = tmp_path / "wp_empty.sqlite3"
    _init_sqlite_db(
        db_empty,
        base_url="http://127.0.0.1:8080",
        theme_slug="azom-plugin-host",
        active_plugins=None,
    )
    with sqlite3.connect(str(db_empty)) as conn:
        empty_row = conn.execute(
            "SELECT option_value FROM wp_options WHERE option_name = 'active_plugins'"
        ).fetchone()
    assert empty_row == ("a:0:{}",)

    for bad in ("noslash.php", "../evil/main.php", "BadUpper/main.php", "a/b/c.php", "slug/notphp.txt"):
        with pytest.raises(ReviewError) as raised:
            _init_sqlite_db(
                tmp_path / "bad.sqlite3",
                base_url="http://127.0.0.1:8080",
                theme_slug="azom-plugin-host",
                active_plugins=[bad],
            )
        assert raised.value.error_class == "PathRejected"


# --------------------------------------------------------------------------- #
# 2. LocalWordPressSandbox & resolve_local_runtime
# --------------------------------------------------------------------------- #


def test_local_sandbox_validation_and_plugin_only_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_dir = tmp_path / "my_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "my_plugin.php").write_text("<?php\n/* Plugin Name: Test */\n", encoding="utf-8")

    # Missing both theme and plugin
    with pytest.raises(ReviewError) as exc_missing:
        LocalWordPressSandbox(plan_id="wr-0123456789ab", sandbox_dir=tmp_path / "sb1")
    assert exc_missing.value.error_class == "WorkspaceMissing"

    # Invalid plugin_slug or missing plugin_slug with plugin_dir
    for bad_slug in ("../bad", "BadSlug", "", None):
        with pytest.raises(ReviewError) as exc_slug:
            LocalWordPressSandbox(
                plan_id="wr-0123456789ab",
                sandbox_dir=tmp_path / "sb_bad_slug",
                plugin_slug=bad_slug,
                plugin_dir=plugin_dir,
            )
        assert exc_slug.value.error_class == "SlugRejected"

    # Invalid plugin_main_file
    for bad_main in ("../evil.php", "sub/main.php", "readme.txt"):
        with pytest.raises(ReviewError) as exc_path:
            LocalWordPressSandbox(
                plan_id="wr-0123456789ab",
                sandbox_dir=tmp_path / "sb_bad_main",
                plugin_slug="my_plugin",
                plugin_dir=plugin_dir,
                plugin_main_file=bad_main,
            )
        assert exc_path.value.error_class == "PathRejected"

    # Valid plugin-only setup fails closed when AZOM_WP_SANDBOX=0
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    sb = LocalWordPressSandbox(
        plan_id="wr-0123456789ab",
        sandbox_dir=tmp_path / "sb_plugin",
        theme_dir=None,
        plugin_slug="my_plugin",
        plugin_dir=plugin_dir,
        runtime_dir=tmp_path / "rt",
    )
    assert sb.theme_dir is None
    assert sb.theme_slug == "azom-plugin-host"
    assert sb.plugin_slug == "my_plugin"
    with pytest.raises(ReviewError) as exc_disabled:
        sb.start()
    assert exc_disabled.value.error_class == "SandboxDisabled"

    # With AZOM_WP_SANDBOX=1 and mocked runtime/Popen, plugin-only start materializes synthetic host + plugin
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")
    fake_php = tmp_path / "rt" / "php"
    fake_wp = tmp_path / "rt" / "wordpress"
    fake_php.parent.mkdir(parents=True, exist_ok=True)
    fake_php.write_bytes(b"")
    fake_wp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        wp_local_runtime,
        "resolve_local_runtime",
        lambda _rt: {
            "php_path": str(fake_php),
            "wordpress_path": str(fake_wp),
            "wordpress_version": "7.1.5",
            "php_version": "8.3.14",
            "system_php": True,
        },
    )
    fake_proc = SimpleNamespace(pid=4321, poll=lambda: None, terminate=lambda: None, wait=lambda timeout=5.0: 0)
    monkeypatch.setattr(wp_local_runtime.subprocess, "Popen", lambda *a, **kw: fake_proc)
    monkeypatch.setattr(LocalWordPressSandbox, "_wait_ready", lambda self: None)

    record = sb.start()
    try:
        assert record["engine"] == "local_php_wp"
        assert (sb.sandbox_dir / "wp-content" / "themes" / "azom-plugin-host" / "style.css").is_file()
        assert (sb.sandbox_dir / "wp-content" / "plugins" / "my_plugin" / "my_plugin.php").is_file()
        with sqlite3.connect(str(sb.sandbox_dir / "wordpress.sqlite3")) as conn:
            val = conn.execute(
                "SELECT option_value FROM wp_options WHERE option_name = 'active_plugins'"
            ).fetchone()
        assert val == (_php_serialize_list(["my_plugin/my_plugin.php"]),)
    finally:
        sb.stop()


def test_resolve_local_runtime_posix_system_php_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = tmp_path / "runtime"
    ver_file = rt / "wordpress" / "wp-includes" / "version.php"
    ver_file.parent.mkdir(parents=True)
    ver_file.write_text("<?php\n$wp_version = '7.1.5';\n", encoding="utf-8")

    class _PosixOsProxy:
        name = "posix"

        def __getattr__(self, attr: str) -> Any:
            return getattr(os, attr)

    monkeypatch.setattr(wp_local_runtime, "check_runtime", lambda _d: (_ for _ in ()).throw(RuntimeFetchError("missing")))
    monkeypatch.setattr(wp_local_runtime, "os", _PosixOsProxy())
    monkeypatch.setattr(wp_local_runtime.shutil, "which", lambda name: "/usr/bin/php" if name == "php" else None)

    def _run_ok(cmd, **_kwargs):
        if "-v" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="PHP 8.3.14 (cli) (built: Jan 1 2026)\n", stderr="")
        if "-m" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="[PHP Modules]\nCore\npdo_sqlite\nsqlite3\n", stderr="")
        raise AssertionError(f"Unexpected cmd: {cmd}")

    monkeypatch.setattr(wp_local_runtime.subprocess, "run", _run_ok)
    manifest = resolve_local_runtime(rt)
    assert manifest["php_path"] == "/usr/bin/php"
    assert manifest["php_version"] == "8.3.14"
    assert manifest["wordpress_version"] == "7.1.5"
    assert manifest["system_php"] is True

    # Reject unsupported PHP version (e.g., 8.1.29)
    def _run_old_php(cmd, **_kwargs):
        if "-v" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="PHP 8.1.29 (cli)\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="pdo_sqlite\n", stderr="")

    monkeypatch.setattr(wp_local_runtime.subprocess, "run", _run_old_php)
    with pytest.raises(ReviewError) as exc_ver:
        resolve_local_runtime(rt)
    assert exc_ver.value.error_class == "SandboxRuntimeUnavailable"

    # Reject missing pdo_sqlite module
    def _run_no_pdo(cmd, **_kwargs):
        if "-v" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="PHP 8.3.14 (cli)\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="Core\njson\n", stderr="")

    monkeypatch.setattr(wp_local_runtime.subprocess, "run", _run_no_pdo)
    with pytest.raises(ReviewError) as exc_mod:
        resolve_local_runtime(rt)
    assert exc_mod.value.error_class == "SandboxRuntimeUnavailable"


# --------------------------------------------------------------------------- #
# 3. run_vertical_plugin_smoke
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# 4. ReviewPlans.run_checks() & WpAgentTools wp_workspace_run_checks
# --------------------------------------------------------------------------- #


def test_review_plans_run_checks_sandbox_off_vs_on_and_agent_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    _setup_mock_staging_theme(tmp_path)
    _setup_mock_staging_plugin(tmp_path)

    plans = ReviewPlans(
        data_dir=tmp_path,
        registry_path=REGISTRY,
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

    png_bytes = _make_png(4, 4)

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
        registry_path=REGISTRY,
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


# --------------------------------------------------------------------------- #
# 5. Dashboard artifact route & wp_review.html
# --------------------------------------------------------------------------- #


def test_dashboard_review_artifact_and_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", "secret-shop")
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "secret-staging")
    for name in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(name, raising=False)
    clear_rbac_cache()

    plans = ReviewPlans(data_dir=tmp_path, registry_path=REGISTRY)
    opened = plans.open("mock-staging", "plugin", "", actor="oscar")
    assert opened.ok
    plan_id = opened.data["plan_id"]
    adir = workspace_root(tmp_path, "mock-staging", plan_id) / "artifacts"
    adir.mkdir(parents=True, exist_ok=True)
    png_bytes = _make_png(4, 4)

    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()
    oscar = _dash_auth(client, "oscar")
    jonatan = _dash_auth(client, "jonatan")

    # Without browser_smoke pass and without PNG -> data-rendered-preview="false", no Syntetiska skärmdumpar
    page_no_preview = client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=oscar)
    assert page_no_preview.status_code == 200
    html_no_preview = page_no_preview.get_data(as_text=True)
    assert 'data-rendered-preview="false"' in html_no_preview
    assert "Syntetiska skärmdumpar" not in html_no_preview

    # Write valid PNG and invalid PNG
    (adir / "work-desktop.png").write_bytes(png_bytes)
    (adir / "base-mobile.png").write_bytes(b"not-a-valid-png")

    # Even with PNG on disk, without passing executed browser_smoke check -> data-rendered-preview="false"
    page_unverified = client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=oscar)
    assert page_unverified.status_code == 200
    assert 'data-rendered-preview="false"' in page_unverified.get_data(as_text=True)

    # Mark an executed passing browser_smoke check in wp_review_plans.json
    stored = json.loads((tmp_path / "wp_review_plans.json").read_text(encoding="utf-8"))
    stored["plans"][0]["checks"] = [
        {
            "job": "browser_smoke",
            "status": "passed",
            "summary": "browser_smoke passed",
            "command": ["chrome"],
            "started_at": "2026-11-24T00:00:00+00:00",
            "finished_at": "2026-11-24T00:00:01+00:00",
            "exit_code": 0,
            "executed": True,
            "simulated": False,
        }
    ]
    (tmp_path / "wp_review_plans.json").write_text(json.dumps(stored), encoding="utf-8")

    page_with_preview = client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=oscar)
    assert page_with_preview.status_code == 200
    html_with_preview = page_with_preview.get_data(as_text=True)
    assert 'data-rendered-preview="true"' in html_with_preview
    assert "Syntetiska skärmdumpar" in html_with_preview

    # Artifact endpoint RBAC & validation
    ok_resp = client.get(f"/wp/mock-staging/reviews/{plan_id}/artifact/work-desktop.png", headers=oscar)
    assert ok_resp.status_code == 200
    assert ok_resp.mimetype == "image/png"
    assert ok_resp.headers.get("Cache-Control") == "no-store"
    assert ok_resp.data == png_bytes

    denied_resp = client.get(f"/wp/mock-staging/reviews/{plan_id}/artifact/work-desktop.png", headers=jonatan)
    assert denied_resp.status_code == 403

    for bad_name in ("evil.php", "..", "other.png", "base-mobile.png", "work-mobile.png"):
        bad_resp = client.get(f"/wp/mock-staging/reviews/{plan_id}/artifact/{bad_name}", headers=oscar)
        assert bad_resp.status_code == 404


# --------------------------------------------------------------------------- #
# 6. Telegram attachments & sendPhoto
# --------------------------------------------------------------------------- #


def test_telegram_attachments_send_photo_and_bot_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()

    png_bytes = _make_png(4, 4)
    art_dir = tmp_path / "wp_workspaces" / "mock-staging" / "wr-0123456789ab" / "artifacts"
    art_dir.mkdir(parents=True)
    valid_png = art_dir / "work-desktop.png"
    valid_png.write_bytes(png_bytes)

    outside = tmp_path / "outside.png"
    outside.write_bytes(png_bytes)
    wrong_name = art_dir / "evil.png"
    wrong_name.write_bytes(png_bytes)
    bad_magic = art_dir / "work-mobile.png"
    bad_magic.write_bytes(b"not-a-png-header")
    traversal = art_dir / ".." / ".." / ".." / "outside.png"

    validated = safe_workspace_attachments(
        [str(valid_png), str(outside), str(traversal), str(wrong_name), str(bad_magic)],
        data_dir=tmp_path,
    )
    assert validated == (str(valid_png.resolve()),)

    # _send_photo constructs multipart/form-data POST to /sendPhoto
    captured: dict[str, Any] = {}

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._buf = io.BytesIO(body)

        def read(self) -> bytes:
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 15.0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _FakeResp(b'{"ok":true,"result":{"message_id":77}}')

    monkeypatch.setattr(bot_main.urllib.request, "urlopen", _fake_urlopen)
    photo_res = bot_main._send_photo("tok123", 555, valid_png, caption="Sandlåda: work-desktop.png")
    assert photo_res["ok"] is True
    assert captured["url"] == "https://api.telegram.org/bottok123/sendPhoto"
    assert captured["method"] == "POST"
    ctype = next(v for k, v in captured["headers"].items() if k.lower() == "content-type")
    assert ctype.startswith("multipart/form-data; boundary=")
    body = captured["data"]
    assert b'name="chat_id"\r\n\r\n555\r\n' in body
    assert b'name="caption"\r\n\r\nSandl' in body
    assert b'name="photo"; filename="work-desktop.png"\r\nContent-Type: image/png\r\n\r\n' + png_bytes in body

    # _result_for_reply / _reply_from_result round-trips attachments
    orig = BotReply(text="Hej", attachments=(str(valid_png),))
    payload = bot_main._result_for_reply(orig)
    assert payload["attachments"] == [str(valid_png)]
    restored = bot_main._reply_from_result(payload)
    assert restored.attachments == (str(valid_png),)

    # BotHandler forwards safe_workspace_attachments from ChatResult.attachments -> BotReply.attachments
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")
    monkeypatch.setattr(
        "ecom_ops.bot.handlers.run_chat",
        lambda *a, **kw: ChatResult(
            text="Kontroll klar",
            attachments=(str(valid_png), str(outside), str(bad_magic)),
        ),
    )
    handler = BotHandler(store=ConversationStore(path=tmp_path / "conv.json"))
    reply = handler.handle(42, "status")
    assert reply.attachments == (str(valid_png.resolve()),)


# --------------------------------------------------------------------------- #
# 7. Extended page_review
# --------------------------------------------------------------------------- #


def _block_theme_tree(tmp_path: Path, slug: str = "twentytwentyfive") -> LocalTree:
    local = tmp_path / "tree"
    theme = local / "wp-content" / "themes" / slug
    (theme / "templates").mkdir(parents=True)
    (theme / "theme.json").write_text('{"version": 3}\n', encoding="utf-8")
    (theme / "style.css").write_text("/* block theme */\n", encoding="utf-8")
    (theme / "templates" / "front-page.html").write_text("<!-- wp:paragraph /-->\n", encoding="utf-8")
    (theme / "templates" / "page.html").write_text("<!-- wp:post-content /-->\n", encoding="utf-8")
    return LocalTree("/var/www/mock-staging", local)


def test_page_review_target_intent_loop_and_site_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    assert is_page_review("granska startsidans källkod") is True
    assert extract_page_review_target("granska startsidans källkod") is None
    assert is_page_review("granska sidan om-oss källkod") is True
    assert extract_page_review_target("granska sidan om-oss källkod") == "om-oss"
    assert is_page_review("granska sidan 2 källkod") is True
    assert extract_page_review_target("granska sidan 2 källkod") == "2"
    assert is_page_review("visa sidan om-oss") is False
    assert extract_page_review_target("visa sidan om-oss") is None

    memory = InMemoryWpTransport()
    memory.settings["show_on_front"] = "page"
    memory.settings["page_on_front"] = 2
    memory.posts[2]["slug"] = "om-oss"
    memory.posts[2]["name"] = "om-oss"
    memory.posts[2]["content"] = {"rendered": "<p>Azom AB</p>", "raw": "<!-- wp:paragraph --><p>Om oss</p>"}
    memory.themes["twentytwentyfive"]["template"] = "twentytwentyfive"
    client = WordPressClient(base_url="https://mock.local", transport=memory)
    transport = _block_theme_tree(tmp_path)

    # Empty templates -> site_editor == "unavailable"
    by_slug = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=transport,
        registry_path=REGISTRY,
        data_dir=tmp_path,
        target_page="om-oss",
    )
    home_slug = by_slug.steps[0]
    assert home_slug["status"] == "read"
    assert home_slug["page_id"] == 2
    assert home_slug["front_page"]["slug"] == "om-oss"
    assert home_slug["theme_kind"] == "block"
    assert home_slug["site_editor"] == "unavailable"
    assert home_slug["site_editor_templates"] == []

    # Populated templates -> site_editor == "read" + site_editor_templates, and numeric target_page="2"
    memory.templates = [
        {"id": "twentytwentyfive//front-page", "slug": "front-page"},
        {"id": "twentytwentyfive//page", "slug": "page"},
    ]
    by_id = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=transport,
        registry_path=REGISTRY,
        data_dir=tmp_path,
        target_page="2",
    )
    home_id = by_id.steps[0]
    assert home_id["status"] == "read"
    assert home_id["page_id"] == 2
    assert home_id["site_editor"] == "read"
    assert home_id["site_editor_templates"] == ["front-page", "page"]

    # Unknown target slug -> target_page_not_found
    unknown = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=transport,
        registry_path=REGISTRY,
        data_dir=tmp_path,
        target_page="finns-inte",
    )
    assert unknown.steps[0]["status"] == "not_run"
    assert unknown.steps[0]["reason"] == "target_page_not_found"
