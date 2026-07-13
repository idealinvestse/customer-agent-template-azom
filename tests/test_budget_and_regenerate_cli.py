"""FU4/FU5: budget warn + status budget field; CLI regenerate wiring smoke."""

from __future__ import annotations

import json

from ecom_ops.budget import budget_status
from ecom_ops.cli import main
from ecom_ops.telemetry import Telemetry


def test_budget_status_near_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel = Telemetry(path=tmp_path / "t.jsonl")
    tel.record(action="llm", site="azom", cost_usd=85.0)
    st = budget_status(telemetry=tel, cap=100.0, warn_ratio=0.8)
    assert st["near_cap"] is True
    assert st["at_cap"] is False
    assert st["used_usd"] >= 85.0
    assert "near" in st["message"].lower() or "cap" in st["message"].lower()


def test_budget_status_ok_when_low(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    tel = Telemetry(path=tmp_path / "t.jsonl")
    tel.record(action="llm", site="azom", cost_usd=1.0)
    st = budget_status(telemetry=tel, cap=100.0, warn_ratio=0.8)
    assert st["near_cap"] is False
    assert st["at_cap"] is False


def test_status_includes_budget(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    code = main(["status"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out.get("ok") is True
    assert "budget" in out
    assert "used_usd" in out["budget"]
    assert "near_cap" in out["budget"]


def test_cli_regenerate_unknown_case(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    code = main(
        [
            "--mock",
            "--actor",
            "jonatan",
            "cases",
            "regenerate",
            "--id",
            "00000000-0000-0000-0000-000000000099",
        ]
    )
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out.get("ok") is False
