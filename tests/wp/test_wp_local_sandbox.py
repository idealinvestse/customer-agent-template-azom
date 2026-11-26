"""Tests for local WordPress SQLite sandbox serialization, validation, and runtime resolution."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecom_ops.integrations import wp_local_runtime
from ecom_ops.integrations.wp_local_runtime import (
    LocalWordPressSandbox,
    _init_sqlite_db,
    _php_serialize_list,
    resolve_local_runtime,
)
from ecom_ops.integrations.wp_runtime_fetch import RuntimeFetchError
from ecom_ops.wp_ops.review_workspace import ReviewError


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
