"""Unit tests for allowlisted log reader / redaction."""

from __future__ import annotations

import json

import pytest

from ecom_ops.log_reader import (
    MAX_LINES,
    list_sources,
    read_log_source,
    redact_log_text,
)


def test_redact_log_text_json_password_field():
    line = json.dumps({"message": "x", "password": "hunter2hunter2"})
    out = redact_log_text(line)
    payload = json.loads(out)
    assert payload["password"] == "***REDACTED***"
    assert payload["message"] == "x"


def test_redact_log_text_env_value(monkeypatch):
    monkeypatch.setenv("MAIL_PASSWORD", "env-secret-value-99")
    out = redact_log_text("failed auth env-secret-value-99 end")
    assert "env-secret-value-99" not in out
    assert "***REDACTED***" in out


def test_redact_log_text_json_message_embeds_env_secret(monkeypatch):
    """Secrets in message/exception values must be scrubbed, not only keyed fields."""
    monkeypatch.setenv("MAIL_PASSWORD", "env-secret-value-99")
    line = json.dumps(
        {
            "level": "ERROR",
            "message": "smtp failed with env-secret-value-99",
            "exception": "trace env-secret-value-99",
        }
    )
    out = redact_log_text(line)
    assert "env-secret-value-99" not in out
    assert "***REDACTED***" in out


def test_read_log_source_unknown():
    with pytest.raises(ValueError):
        read_log_source("../etc/passwd")


def test_read_log_source_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_LOG_DIR", str(tmp_path / "empty-logs"))
    payload = read_log_source("dashboard", lines=10)
    assert payload["ok"] is True
    assert payload["exists"] is False
    assert payload["rows"] == []


def test_list_sources_includes_runtime_and_events(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    ids = {s["id"] for s in list_sources()}
    assert "dashboard" in ids
    assert "bot" in ids
    assert "telemetry" in ids
    assert "audit" in ids
    assert len(ids) <= MAX_LINES  # sanity; sources are few
