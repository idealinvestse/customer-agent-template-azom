"""Tests for Ubuntu 26.04 / Hetzner deployment artifacts, systemd units, proxy configs, and ProxyFix."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ALL_SHELL_SCRIPTS = sorted(
    p.relative_to(ROOT).as_posix() for p in (ROOT / "bin").glob("*.sh")
)

SYSTEMD_UNITS = [
    "azom-backup.service",
    "azom-backup.timer",
    "azom-bot.service",
    "azom-cases-poll.service",
    "azom-cases-poll.timer",
    "azom-daily-brief.service",
    "azom-daily-brief.timer",
    "azom-dashboard.service",
    "azom-retention-purge.service",
    "azom-retention-purge.timer",
    "azom-shop-intel.service",
    "azom-shop-intel.timer",
]


@pytest.mark.parametrize("rel_path", ALL_SHELL_SCRIPTS)
def test_all_shell_scripts_strict_lf_and_bash_syntax(rel_path: str) -> None:
    path = ROOT / rel_path
    assert path.is_file(), f"Missing script {rel_path}"
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"CRLF detected in {rel_path}"
    bash_bin = shutil.which("bash")
    if bash_bin:
        proc = subprocess.run(
            [bash_bin, "-n"],
            input=raw,
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")


@pytest.mark.parametrize("unit_name", SYSTEMD_UNITS)
def test_systemd_units_exist_strict_lf_and_never_default_null_send(unit_name: str) -> None:
    path = ROOT / "infrastructure" / "systemd" / unit_name
    assert path.is_file(), f"Missing systemd unit {unit_name}"
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"CRLF detected in {unit_name}"
    text = raw.decode("utf-8")
    assert "AZOM_NULL_SEND=1" not in text
    assert ".venv/bin/bash" not in text


def test_systemd_backup_and_retention_and_dashboard_hardening() -> None:
    sdir = ROOT / "infrastructure" / "systemd"

    backup = (sdir / "azom-backup.service").read_text(encoding="utf-8")
    assert "ExecStart=/bin/bash /opt/azom-agent/bin/backup-azom-data.sh" in backup
    assert "EnvironmentFile=-/opt/azom-agent/.env" in backup
    assert "ProtectSystem=strict" in backup
    assert "ReadWritePaths=" in backup and "/var/lib/azom" in backup

    purge = (sdir / "azom-retention-purge.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=-/opt/azom-agent/.env" in purge
    assert "Environment=PYTHONPATH=/opt/azom-agent/skills" in purge
    assert "Environment=AZOM_USE_MOCK=0" in purge
    assert "--actor oscar" in purge
    assert "cases retention-purge" in purge
    assert "ProtectSystem=strict" in purge
    assert purge.index("Environment=AZOM_USE_MOCK=0") < purge.index("EnvironmentFile=-/opt/azom-agent/.env")

    dashboard = (sdir / "azom-dashboard.service").read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in dashboard
    assert "/opt/azom-agent/config" in dashboard
    assert dashboard.index("Environment=AZOM_USE_MOCK=0") < dashboard.index("EnvironmentFile=-/opt/azom-agent/.env")

    daily = (sdir / "azom-daily-brief.service").read_text(encoding="utf-8")
    for directive in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ReadWritePaths=",
    ):
        assert directive in daily
    assert daily.index("Environment=AZOM_USE_MOCK=0") < daily.index("EnvironmentFile=-/opt/azom-agent/.env")

    shop_intel = (sdir / "azom-shop-intel.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/azom-agent/bin/shop-intel.sh" in shop_intel
    assert "EnvironmentFile=-/opt/azom-agent/.env" in shop_intel
    assert shop_intel.index("Environment=AZOM_USE_MOCK=0") < shop_intel.index("EnvironmentFile=-/opt/azom-agent/.env")


def test_caddy_and_nginx_configs() -> None:
    caddy_path = ROOT / "infrastructure" / "caddy" / "Caddyfile"
    nginx_path = ROOT / "infrastructure" / "nginx" / "dashboard.azom.no.conf"
    assert caddy_path.is_file()
    assert nginx_path.is_file()

    caddy_raw = caddy_path.read_bytes()
    nginx_raw = nginx_path.read_bytes()
    assert b"\r\n" not in caddy_raw
    assert b"\r\n" not in nginx_raw

    caddy = caddy_raw.decode("utf-8")
    assert "dashboard.azom.no" in caddy
    assert "127.0.0.1:8080" in caddy
    assert "/metrics" in caddy
    assert "403" in caddy
    assert "reverse_proxy" in caddy

    nginx = nginx_raw.decode("utf-8")
    assert "server_name dashboard.azom.no;" in nginx
    assert "http://127.0.0.1:8080" in nginx
    assert "/metrics" in nginx
    assert "403" in nginx
    assert "X-Forwarded-Proto" in nginx
    assert "X-Forwarded-For" in nginx
    assert "X-Forwarded-Host" in nginx
    assert "X-Hub-Signature-256" in nginx


def test_install_ubuntu26_features() -> None:
    text = (ROOT / "bin" / "install-ubuntu26.sh").read_text(encoding="utf-8")
    for token in (
        "--domain",
        "--proxy",
        "--mock",
        "azom-shop-intel.service",
        "azom-shop-intel.timer",
        "DASHBOARD_SECRET_KEY",
        "sqlite3",
        "libffi-dev",
        "libssl-dev",
        "dnsutils",
        "bind9-dnsutils",
        "AZOM_DASHBOARD_PUBLIC_URL",
        "TRUSTED_PROXY_COUNT",
        "ufw allow 80/tcp",
        "ufw allow 443/tcp",
        "systemcheck.sh",
        "healthcheck.sh",
        'ENVIRON["AZOM_ENV_VAL"]',
        "Environment=AZOM_USE_MOCK=${USE_MOCK}",
        "/webhooks/messenger",
        "/webhooks/woo",
        "/oauth/gmail/callback",
        "/oauth/google/callback",
        "/oauth/openai/callback",
    ):
        assert token in text, f"Expected {token!r} in bin/install-ubuntu26.sh"
    assert "ufw allow 8080" not in text


def test_backup_script_includes_seo_and_shop_intel_with_sqlite3_and_python_fallback() -> None:
    text = (ROOT / "bin" / "backup-azom-data.sh").read_text(encoding="utf-8")
    for token in (
        "seo_studio.sqlite3",
        "shop_intel.sqlite3",
        '--exclude="*/seo_studio.sqlite3"',
        '--exclude="*/seo_studio.sqlite3-wal"',
        '--exclude="*/seo_studio.sqlite3-shm"',
        '--exclude="*/shop_intel.sqlite3"',
        '--exclude="*/shop_intel.sqlite3-wal"',
        '--exclude="*/shop_intel.sqlite3-shm"',
        "seo_studio_db_bytes",
        "shop_intel_db_bytes",
        "command -v sqlite3",
        "source.backup(target",
    ):
        assert token in text, f"Expected {token!r} in bin/backup-azom-data.sh"


def test_requirements_env_example_and_gitattributes() -> None:
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "paramiko>=3.2,<5" in pyproject
    assert "paramiko>=3.2,<5" in reqs

    env_ex = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "TRUSTED_PROXY_COUNT=1" in env_ex
    assert "DASHBOARD_THREADS=12" in env_ex
    assert "dashboard.azom.no" in env_ex

    gitattr = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for rule in ("*.sh text eol=lf", "*.service text eol=lf", "*.timer text eol=lf", "Caddyfile text eol=lf", "*.conf text eol=lf"):
        assert rule in gitattr


def test_dashboard_proxyfix_handles_forwarded_host_proto_and_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dash_dir = str(ROOT / "infrastructure" / "dashboard")
    skills_dir = str(ROOT / "skills")
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    if skills_dir not in sys.path:
        sys.path.insert(0, skills_dir)

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")

    import app as dash_app

    flask_app = dash_app.create_app()
    captured: dict[str, str | None] = {}

    @flask_app.route("/_test_proxy_echo")
    def _proxy_echo():
        from flask import request

        captured["scheme"] = request.scheme
        captured["host"] = request.host
        captured["remote_addr"] = request.remote_addr
        return "ok", 200

    client = flask_app.test_client()
    resp = client.get(
        "/_test_proxy_echo",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "dashboard.azom.no",
            "X-Forwarded-For": "203.0.113.42",
        },
    )
    assert resp.status_code == 200
    assert captured["scheme"] == "https"
    assert captured["host"] == "dashboard.azom.no"
    assert captured["remote_addr"] == "203.0.113.42"
    assert "Strict-Transport-Security" in resp.headers

    # With TRUSTED_PROXY_COUNT=0, forwarded headers must NOT rewrite remote_addr/host
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "0")
    untrusted_app = dash_app.create_app()
    untrusted_captured: dict[str, str | None] = {}

    @untrusted_app.route("/_test_proxy_echo_untrusted")
    def _proxy_echo_untrusted():
        from flask import request

        untrusted_captured["scheme"] = request.scheme
        untrusted_captured["host"] = request.host
        untrusted_captured["remote_addr"] = request.remote_addr
        return "ok", 200

    resp2 = untrusted_app.test_client().get(
        "/_test_proxy_echo_untrusted",
        headers={
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-For": "198.51.100.77",
        },
    )
    assert resp2.status_code == 200
    assert untrusted_captured["host"] != "evil.example"
    assert untrusted_captured["remote_addr"] != "198.51.100.77"
    assert os.environ.get("TRUSTED_PROXY_COUNT") == "0"
