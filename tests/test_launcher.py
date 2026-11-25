"""Tests for AzomOps preflight checker and system launcher."""

from __future__ import annotations

import json
from pathlib import Path

from ecom_ops import launcher
from ecom_ops.model_router import circuit_breaker
from ecom_ops.model_router.errors import ModelAuthError
from ecom_ops.model_router.store import ModelSession, ModelSessionStore

ROOT = Path(__file__).resolve().parents[1]


def test_preflight_mock_succeeds_without_secrets_and_no_secret_leak(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret-value-999")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-secret-value-888")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "dash-secret-value-777")

    rc = launcher.main(["--check", "--mode", "mock", "--no-port-check", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "or-secret-value-999" not in out
    assert "tg-secret-value-888" not in out
    assert "dash-secret-value-777" not in out
    data = json.loads(out)
    assert data["ready"] is True
    assert data["mode"] == "mock"
    assert data["blockers"] == []


def test_preflight_live_blocks_on_missing_fail_closed_allowlists(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-live-tok")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)

    report = launcher.run_preflight(ROOT, mode="live", check_ports=False)
    assert len(report.blockers) == 2
    joined = " ".join(report.blockers)
    assert "Dashboard" in joined
    assert "Telegram-bot" in joined
    assert launcher.main(["--target", "all", "--mode", "live", "--no-port-check"]) == 1


def test_preflight_reports_chatgpt_oauth_and_open_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    ModelSessionStore("openai").save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="oaiapp_1",
            access_token="acc",
            refresh_token="ref",
            expires_at=9999999999.0,
            generation=2,
            status="connected",
            scopes=["openid", "offline_access", "chatgpt.tokens.use.direct"],
        )
    )
    circuit_breaker.record_failure("xai_grok_build", "grok-4", ModelAuthError("expired"))

    report = launcher.run_preflight(ROOT, mode="mock", check_ports=False)
    text = launcher.format_report(report)
    assert "ansluten=ja, plan=ja" in text
    assert "route:xai_grok_build" in text


def test_init_env_and_dry_run_launch(tmp_path, monkeypatch, capsys):
    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    (fake_root / ".env.example").write_text("AZOM_USE_MOCK=1\n", encoding="utf-8")

    created = launcher.init_environment(fake_root)
    assert any(".env" in msg for msg in created)
    env_file = fake_root / ".env"
    assert env_file.is_file()

    # Does not overwrite existing .env
    env_file.write_text("CUSTOM_KEEP=yes\n", encoding="utf-8")
    second = launcher.init_environment(fake_root)
    assert any("rördes inte" in msg for msg in second)
    assert "CUSTOM_KEEP=yes" in env_file.read_text(encoding="utf-8")

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-dry")
    cmds = launcher.build_launch_commands(ROOT, target="all")
    assert [name for name, _ in cmds] == ["dashboard", "telegram-bot"]

    rc = launcher.main(["--check", "--mode", "mock", "--no-port-check"])
    assert rc == 0
    dry_rc = launcher.main(["--target", "all", "--mode", "mock", "--dry-run", "--no-port-check"])
    assert dry_rc == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN] dashboard:" in out
    assert "[DRY-RUN] telegram-bot:" in out
