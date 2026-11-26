"""Focused tests for PR 2.4 extracted integrations/wp/ modules, templates, and facades."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ecom_ops.integrations import (
    wp_browser_smoke,
    wp_component_reads,
    wp_files_ssh,
    wp_local_runtime,
    wp_review_process,
)
from ecom_ops.integrations.wp import (
    component_reads,
    local_runtime,
    php_lint,
    sandbox_smoke,
    sandbox_sqlite,
    sftp_transport,
)
from ecom_ops.wp_ops.files import TransportError
from ecom_ops.wp_ops.models import Site


def test_external_templates_load_and_render(tmp_path: Path) -> None:
    db_dropin = local_runtime._render_db_dropin()
    assert "class Azom_SQLite_WPDB extends wpdb" in db_dropin
    assert "$wpdb = new Azom_SQLite_WPDB" in db_dropin

    egress_guard = local_runtime._render_egress_guard()
    assert "azom_egress_blocked" in egress_guard
    assert "pre_http_request" in egress_guard

    wp_core = tmp_path / "wp-core"
    wp_content = tmp_path / "wp-content"
    wp_config_path = tmp_path / "wp-config.php"
    sqlite_path = tmp_path / "wordpress.sqlite3"
    debug_log = tmp_path / "wp-debug.log"
    egress_log = tmp_path / "egress.jsonl"

    wp_config = local_runtime._render_wp_config(
        wp_core=wp_core,
        wp_content=wp_content,
        base_url="http://127.0.0.1:43210/",
        sqlite_path=sqlite_path,
        debug_log=debug_log,
        egress_log=egress_log,
    )
    assert "__URL__" not in wp_config
    assert "__WP_CORE__" not in wp_config
    assert "define( 'WP_HOME', 'http://127.0.0.1:43210' );" in wp_config
    assert "define( 'WP_HTTP_BLOCK_EXTERNAL', true );" in wp_config

    router = local_runtime._render_router(
        wp_core=wp_core,
        wp_content=wp_content,
        wp_config=wp_config_path,
    )
    assert "__WP_CORE__" not in router
    assert "__WP_CONTENT__" not in router
    assert "__WP_CONFIG__" not in router
    assert "require_once $azom_wp_config;" in router

    synth_dir = tmp_path / "synth-theme"
    local_runtime._write_minimal_synthetic_theme(synth_dir)
    assert (synth_dir / "style.css").is_file()
    assert "Azom Synthetic Plugin Host" in (synth_dir / "style.css").read_text(encoding="utf-8")
    assert "wp_enqueue_scripts" in (synth_dir / "functions.php").read_text(encoding="utf-8")
    assert 'data-azom-theme-marker="azom-plugin-host"' in (synth_dir / "index.php").read_text(encoding="utf-8")


def test_sandbox_sqlite_init_and_serialize(tmp_path: Path) -> None:
    assert sandbox_sqlite.php_serialize_list([]) == "a:0:{}"
    assert sandbox_sqlite.php_serialize_list(["demo/demo.php"]) == 'a:1:{i:0;s:13:"demo/demo.php";}'

    db_path = tmp_path / "wordpress.sqlite3"
    sandbox_sqlite.init_sqlite_db(
        db_path,
        base_url="http://127.0.0.1:9999",
        theme_slug="child-theme",
        parent_slug="parent-theme",
        active_plugins=["demo/demo.php"],
    )
    conn = sqlite3.connect(str(db_path))
    try:
        opts = dict(conn.execute("SELECT option_name, option_value FROM wp_options").fetchall())
    finally:
        conn.close()
    assert opts["stylesheet"] == "child-theme"
    assert opts["template"] == "parent-theme"
    assert opts["active_plugins"] == 'a:1:{i:0;s:13:"demo/demo.php";}'


def test_build_php_command_system_vs_pinned(tmp_path: Path) -> None:
    php_bin = tmp_path / "php"
    ext_dir = tmp_path / "ext"
    err_log = tmp_path / "php-errors.log"
    wp_core = tmp_path / "wp"
    router = tmp_path / "router.php"

    pinned = local_runtime._build_php_command(
        php_bin,
        ext_dir=ext_dir,
        system_php=False,
        php_error_log=err_log,
        port=12345,
        wp_core=wp_core,
        router=router,
    )
    assert "-n" in pinned
    assert "extension=pdo_sqlite" in pinned
    assert "127.0.0.1:12345" in pinned

    system = local_runtime._build_php_command(
        php_bin,
        ext_dir=ext_dir,
        system_php=True,
        php_error_log=err_log,
        port=12345,
        wp_core=wp_core,
        router=router,
    )
    assert "-n" not in system
    assert "127.0.0.1:12345" in system


def _make_site() -> Site:
    return Site(
        id="site-a",
        display_name="Site A",
        canonical_url="https://staging.example.com/",
        environment="staging",
        business_owner="oscar",
        hosting_provider="hetzner",
        wordpress_version="6.7",
        php_version="8.3",
        woocommerce=True,
        critical_urls=(),
        maintenance_window="03:00-04:00",
        secret_ref="WP_SECRET_SITE_A",
        capabilities=(),
        alert_confirmations=2,
        check_interval_seconds=300,
        timeout_seconds=10.0,
        dedupe_window_seconds=900,
        retention_days=30,
        alert_owner="oscar",
        files_root="/var/www/site-a",
        ssh_host="staging.example.com",
        ssh_user="sitea-wp-reader",
        ssh_port=22,
        host_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey",
    )


def test_ssh_transport_protocol_attrs_and_write_refusals(tmp_path: Path) -> None:
    transport = sftp_transport.SshFileTransport(_make_site(), tmp_path)
    assert transport.is_ssh is True
    assert wp_files_ssh.SshFileTransport is sftp_transport.SshFileTransport

    with pytest.raises(TransportError, match="SshWriteDisabled"):
        transport.write_bytes("/var/www/site-a/wp-content/themes/t/style.css", b"x")
    with pytest.raises(TransportError, match="SshWriteDisabled"):
        transport.create_bytes("/var/www/site-a/wp-content/themes/t/new.css", b"x")

    with pytest.raises(TransportError, match="FileMissing"):
        with transport._sftp_errors():
            raise FileNotFoundError("missing")

    with pytest.raises(TransportError, match="SshFailed"):
        with transport._sftp_errors():
            raise TimeoutError("timed out")


def test_facades_expose_canonical_implementations() -> None:
    assert wp_local_runtime.LocalWordPressSandbox is local_runtime.LocalWordPressSandbox
    assert wp_browser_smoke.run_browser_render is sandbox_smoke.run_browser_render
    assert wp_browser_smoke.run_vertical_theme_render is sandbox_smoke.run_vertical_theme_render
    assert wp_browser_smoke.run_vertical_plugin_smoke is sandbox_smoke.run_vertical_plugin_smoke
    assert wp_files_ssh.build_file_transport is sftp_transport.build_file_transport
    assert wp_review_process.run_php_lint is php_lint.run_php_lint
    assert wp_component_reads.wp_client_for_site is component_reads.wp_client_for_site
