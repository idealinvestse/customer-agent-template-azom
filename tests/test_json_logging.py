"""Tests for structured JSON logging (stderr + file)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ecom_ops.json_logging import JsonFormatter, configure_json_logging, resolve_log_dir


def test_json_formatter_includes_extra_and_message():
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="azom.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.actor = "jonatan"
    payload = json.loads(fmt.format(record))
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "azom.test"
    assert payload["actor"] == "jonatan"
    assert "ts" in payload


def test_configure_json_logging_writes_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AZOM_JSON_LOGGING", raising=False)
    monkeypatch.setenv("AZOM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AZOM_LOG_NAME", "unit")
    monkeypatch.setenv("AZOM_LOG_LEVEL", "INFO")
    root = logging.getLogger()
    prev = list(root.handlers)
    try:
        configure_json_logging(force=True, log_name="unit")
        log = logging.getLogger("azom.unit_test_file")
        log.info("file line", extra={"case_id": "abc"})
        for h in root.handlers:
            h.flush()
        path = tmp_path / "logs" / "unit.log"
        assert path.is_file()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert lines
        row = json.loads(lines[-1])
        assert row["message"] == "file line"
        assert row["case_id"] == "abc"
    finally:
        root.handlers.clear()
        root.handlers.extend(prev)


def test_configure_json_logging_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_JSON_LOGGING", "0")
    monkeypatch.setenv("AZOM_LOG_DIR", str(tmp_path / "logs"))
    root = logging.getLogger()
    before = list(root.handlers)
    configure_json_logging(force=True, log_name="disabled")
    assert root.handlers == before
    assert not (tmp_path / "logs" / "disabled.log").exists()


def test_resolve_log_dir_create_false_no_mkdir(tmp_path, monkeypatch):
    target = tmp_path / "never-created"
    monkeypatch.setenv("AZOM_LOG_DIR", str(target))
    path = resolve_log_dir(create=False)
    assert path == target
    assert not target.exists()
