"""Read-only soak pre-flight: deterministic mock pass/fail."""

from __future__ import annotations

import json

from ecom_ops.cli import main
from ecom_ops.soak_preflight import run_soak_preflight


def test_preflight_ok_in_mock(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("MESSENGER_ALLOWED_PSIDS", raising=False)
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    result = run_soak_preflight()
    assert result["ok"] is True
    assert result["soak_complete"] is False
    assert result["mock"] is True
    keys = {c["key"] for c in result["checks"]}
    assert "auto_send_off" in keys
    assert all(c["ok"] for c in result["checks"] if c["key"] == "auto_send_off")


def test_preflight_fails_live_empty_allowlists(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    monkeypatch.delenv("MESSENGER_ALLOWED_PSIDS", raising=False)
    monkeypatch.delenv("MESSENGER_ACTOR_MAP", raising=False)
    monkeypatch.delenv("MESSENGER_PAGE_ACCESS_TOKEN", raising=False)
    result = run_soak_preflight()
    assert result["ok"] is False
    assert "telegram_allowlist" in result["failed"]
    assert result["soak_complete"] is False


def test_cli_soak_preflight(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    code = main(["soak-preflight"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["soak_complete"] is False
