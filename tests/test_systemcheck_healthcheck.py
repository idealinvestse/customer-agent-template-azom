"""Offline tests for AzomOps systemcheck, healthcheck, CLI, and shell helpers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ecom_ops import cli, healthcheck

ROOT = Path(__file__).resolve().parents[1]


def _seed_live_env(monkeypatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "a" * 32)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jonatan-live-pass")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "oscar-live-pass")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dashboard.azom.no")
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-live-token-xyz")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "12345")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "12345:jonatan")
    monkeypatch.setenv("WOO_BASE_URL", "https://azom.no")
    monkeypatch.setenv("WOO_CONSUMER_KEY", "ck_test_123")
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "cs_test_secret_456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-789")
    return data_dir


def test_systemcheck_mock_no_secret_leak_and_json_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret-value-999")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-secret-value-888")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "dash-secret-value-777")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw-secret-value-666")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "oscar-secret-value-555")
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "woo-secret-value-444")

    rc = cli.main(["--mock", "systemcheck", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    for secret in (
        "or-secret-value-999",
        "tg-secret-value-888",
        "dash-secret-value-777",
        "pw-secret-value-666",
        "oscar-secret-value-555",
        "woo-secret-value-444",
    ):
        assert secret not in out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["kind"] == "systemcheck"
    assert payload["mode"] == "mock"
    assert payload["blockers"] == []

    # Also accepts --mock after subcommand
    rc2 = cli.main(["systemcheck", "--mock", "--json"])
    assert rc2 == 0
    capsys.readouterr()


def test_systemcheck_exit_codes_strict_warn_and_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)

    # Without DASHBOARD_SECRET_KEY in mock mode, there is at least a warning
    rep = healthcheck.run_systemcheck(ROOT, mode="mock", check_ports=False)
    assert rep.warnings
    assert rep.blockers == []
    assert rep.exit_code(strict=False) == 0
    assert rep.exit_code(strict=True) == 2

    # Critical disk failure (< 500 MB) -> exit code 1 even without --strict
    monkeypatch.setattr(healthcheck, "_disk_usage_for", lambda _p: (100 * 1024 * 1024, 20 * 1024**3))
    rep_fail = healthcheck.run_systemcheck(ROOT, mode="mock", check_ports=False)
    assert rep_fail.exit_code(strict=False) == 1
    assert any("Diskutrymme" in b for b in rep_fail.blockers)


def test_systemcheck_linux_meminfo_binaries_env_mode_and_systemd(tmp_path, monkeypatch):
    fake_root = tmp_path / "opt_azom"
    cfg_dir = fake_root / "config"
    data_dir = tmp_path / "var_lib_azom"
    cfg_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    for name in ("sites.yaml", "prompts.yaml", "model_profiles.yaml", "limits.yaml"):
        (cfg_dir / name).write_text("ok: true\n", encoding="utf-8")
    env_file = fake_root / ".env"
    env_file.write_text("AZOM_USE_MOCK=0\n", encoding="utf-8")

    _seed_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setattr(healthcheck, "_is_linux", lambda: True)
    monkeypatch.setattr(healthcheck, "_read_os_release", lambda: {"ID": "ubuntu", "VERSION_ID": "26.04"})
    monkeypatch.setattr(healthcheck, "_read_meminfo", lambda: {"mem_total_kb": 1000000, "mem_available_kb": 400000})
    monkeypatch.setattr(healthcheck, "_env_posix_mode", lambda _p: 0o644)
    monkeypatch.setattr(
        healthcheck,
        "_find_binary",
        lambda name: None if name == "rsync" else f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        healthcheck,
        "_query_systemd_units",
        lambda units: {
            u: {
                "load_state": "loaded",
                "unit_file_state": "disabled" if u == "azom-backup.timer" else "enabled",
                "active_state": "active",
                "sub_state": "running",
            }
            for u in units
        },
    )

    rep = healthcheck.run_systemcheck(fake_root, mode="live", check_ports=False, apply_dotenv=False)
    by_name = {c.name: c for c in rep.checks}
    assert "ubuntu 26.04" in by_name["Operativsystem"].detail
    assert by_name["RAM-minne"].status == "warn"
    assert by_name["Linux-systembinärer"].status == "warn"
    assert "rsync" in by_name["Linux-systembinärer"].detail
    assert by_name[".env-fil & behörighet"].status == "fail"
    assert "0o644" in by_name[".env-fil & behörighet"].detail
    assert by_name["Systemd-enheter"].status == "warn"
    assert "azom-backup.timer" in by_name["Systemd-enheter"].detail


def test_systemcheck_sqlite_integrity_and_wal(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))

    # Valid WAL DB
    db_ok = data_dir / "cases.db"
    conn = sqlite3.connect(str(db_ok))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    rep_ok = healthcheck.run_systemcheck(ROOT, mode="mock", check_ports=False)
    by_name = {c.name: c for c in rep_ok.checks}
    assert by_name["SQLite-databaser"].status == "ok"

    # Corrupt DB triggers fail
    (data_dir / "seo_studio.sqlite3").write_text("not a sqlite file", encoding="utf-8")
    rep_bad = healthcheck.run_systemcheck(ROOT, mode="mock", check_ports=False)
    by_name_bad = {c.name: c for c in rep_bad.checks}
    assert by_name_bad["SQLite-databaser"].status == "fail"


def test_systemcheck_live_safety_and_opt_in_network(tmp_path, monkeypatch):
    _seed_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "0")
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)

    # Default check_network=False must NOT call DNS/TLS
    def _boom(*_a, **_kw):
        raise AssertionError("network probe must not run unless check_network=True")

    monkeypatch.setattr(healthcheck, "_resolve_dns_a", _boom)
    monkeypatch.setattr(healthcheck, "_probe_tls_cert", _boom)

    rep_offline = healthcheck.run_systemcheck(ROOT, mode="live", check_network=False, check_ports=False)
    by_name = {c.name: c for c in rep_offline.checks}
    assert by_name["Reverse proxy (TRUSTED_PROXY_COUNT)"].status == "warn"
    assert by_name["Telegram-bot"].status == "fail"

    # Mock mode with check_network=True must also NEVER hit network
    rep_mock_net = healthcheck.run_systemcheck(
        ROOT, mode="mock", domain="dashboard.azom.no", expected_ip="95.217.216.3", check_network=True
    )
    assert any(c.category == "network" and c.status == "skip" for c in rep_mock_net.checks)

    # Live opt-in network check with mocked DNS/TLS/port bind
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "12345:jonatan")
    monkeypatch.setattr(healthcheck, "_resolve_dns_a", lambda _d: ["95.217.216.3"])
    monkeypatch.setattr(
        healthcheck,
        "_probe_tls_cert",
        lambda _h, _p=443, timeout=4.0: {"ok": True, "days_remaining": 60, "subject_cn": "dashboard.azom.no"},
    )
    monkeypatch.setattr(
        healthcheck,
        "_inspect_port_bind",
        lambda port: {"listening": True, "loopback_bind": True, "public_bind": False, "detail": f"127.0.0.1:{port} lyssnar"},
    )
    rep_net = healthcheck.run_systemcheck(
        ROOT, mode="live", domain="dashboard.azom.no", expected_ip="95.217.216.3", check_network=True
    )
    by_net = {c.name: c for c in rep_net.checks}
    assert by_net["DNS A-post"].status == "ok"
    assert by_net["TLS-certifikat (:443)"].status == "ok"
    assert by_net["Port 8080 bind-posture"].status == "ok"

    # Mismatch IP & public bind
    monkeypatch.setattr(healthcheck, "_resolve_dns_a", lambda _d: ["1.2.3.4"])
    monkeypatch.setattr(
        healthcheck,
        "_inspect_port_bind",
        lambda port: {"listening": True, "loopback_bind": False, "public_bind": True, "detail": f"offentlig bind på :{port}"},
    )
    rep_net_bad = healthcheck.run_systemcheck(
        ROOT, mode="live", domain="dashboard.azom.no", expected_ip="95.217.216.3", check_network=True
    )
    by_net_bad = {c.name: c for c in rep_net_bad.checks}
    assert by_net_bad["DNS A-post"].status == "fail"
    assert by_net_bad["Port 8080 bind-posture"].status == "warn"


def test_healthcheck_mock_skip_and_live_http_public_and_snapshot(tmp_path, monkeypatch, capsys):
    data_dir = _seed_live_env(monkeypatch, tmp_path)

    # 1. Mock mode skips unreachable local HTTP cleanly
    monkeypatch.setattr(healthcheck, "_http_get", lambda _url, timeout=4.0: (0, None, "ConnectionRefused"))
    rc_cli = cli.main(["--mock", "healthcheck", "--json"])
    assert rc_cli == 0
    mock_out = json.loads(capsys.readouterr().out)
    assert mock_out["kind"] == "healthcheck"
    assert mock_out["ok"] is True

    # 2. Live mode with mocked local + public HTTPS + systemd + runtime snapshot
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret-never-print")

    def _fake_http(url: str, *, timeout: float = 4.0):
        del timeout
        if url.endswith("/live"):
            return 200, {"ok": True, "liveness": True}, None
        if url.endswith("/health"):
            return 200, {
                "ok": True,
                "liveness": True,
                "readiness": {"ok": True, "detail": "ok"},
                "messenger": {"configured": True, "fail_closed": False},
                "null_send": "off",
                "budget_ratio": 0.12,
                "budget_pacing_warn": False,
                "last_poll_errors": 0,
                "web_search_status": "ready",
            }, None
        if url.endswith("/ready"):
            return 200, {"ok": True, "readiness": {"ok": True}}, None
        if url.endswith("/metrics"):
            return 403, None, None
        return 404, None, None

    monkeypatch.setattr(healthcheck, "_http_get", _fake_http)
    monkeypatch.setattr(
        healthcheck,
        "_probe_tls_cert",
        lambda _h, _p=443, timeout=4.0: {"ok": True, "days_remaining": 75},
    )
    monkeypatch.setattr(healthcheck, "_is_linux", lambda: True)
    monkeypatch.setattr(healthcheck, "_find_binary", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(
        healthcheck,
        "_query_systemd_units",
        lambda units: {
            u: {
                "load_state": "loaded",
                "unit_file_state": "enabled",
                "active_state": "active",
                "sub_state": "running",
            }
            for u in units
        },
    )

    rep = healthcheck.run_healthcheck(ROOT, mode="live", check_public=True)
    assert rep.exit_code(strict=True) == 0
    by_name = {c.name: c for c in rep.checks}
    assert by_name["Dashboard /live"].status == "ok"
    assert by_name["Dashboard /health"].status == "ok"
    assert by_name["Dashboard /ready"].status == "ok"
    assert by_name["Publik HTTPS-TLS"].status == "ok"
    assert by_name["Publik /live & /health"].status == "ok"
    assert by_name["Publik /metrics låsning"].status == "ok"
    assert by_name["systemd azom-dashboard"].status == "ok"
    assert by_name["systemd timers"].status == "ok"
    assert "runtime_snapshot" in rep.details
    assert "or-secret-never-print" not in healthcheck.format_report(rep)

    # Exposed /metrics (HTTP 200) must fail
    def _bad_metrics(url: str, *, timeout: float = 4.0):
        if url.startswith("https://") and url.endswith("/metrics"):
            return 200, None, None
        return _fake_http(url, timeout=timeout)

    monkeypatch.setattr(healthcheck, "_http_get", _bad_metrics)
    rep_bad = healthcheck.run_healthcheck(ROOT, mode="live", check_public=True)
    assert rep_bad.exit_code() == 1
    assert any("/metrics" in b for b in rep_bad.blockers)


def test_shell_scripts_strict_lf_and_env_handling():
    for rel in ("bin/launch.sh", "bin/systemcheck.sh", "bin/healthcheck.sh"):
        raw = (ROOT / rel).read_bytes()
        assert b"\r" not in raw, f"{rel} contains CR/CRLF bytes"
        text = raw.decode("utf-8")
        assert "load_dotenv_defaults" in text
        assert "/var/lib/azom" in text
        assert "/opt/azom-agent" in text
