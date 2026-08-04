"""CLI shadow-report: latest-per-case trail summary."""

from __future__ import annotations

import json

from ecom_ops.cases.shadow_report import build_shadow_report
from ecom_ops.cases.store import CaseStore
from ecom_ops.cli import main


def test_empty_shadow_report(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    store = CaseStore(path=tmp_path / "cases.db")
    report = build_shadow_report(days=7, store=store)
    assert report["ok"] is True
    assert report["observed_cases"] == 0
    assert "Ingen skuggobservation" in report["message"]
    assert report["null_send"] == "off"
    assert report["warnings"]


def test_mixed_latest_per_case_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    store = CaseStore(path=tmp_path / "cases.db")
    a = store.create_case(
        mailbox_id="m",
        subject="A",
        from_addr="a@b.co",
        body="x",
        category="order_status",
        draft_reply="d",
        order_id="1",
        message_id="<shadow-a@azom>",
    )
    b = store.create_case(
        mailbox_id="m",
        subject="B",
        from_addr="b@b.co",
        body="y",
        category="order_status",
        draft_reply="d",
        order_id=None,
        message_id="<shadow-b@azom>",
    )
    store.set_shadow_decision(a.id, eligible=True, deny_reason=None)
    store.set_shadow_decision(b.id, eligible=False, deny_reason="missing_order_id")
    # Recompute same case — still one latest row
    store.set_shadow_decision(a.id, eligible=True, deny_reason=None)
    report = build_shadow_report(days=7, store=store)
    assert report["observed_cases"] == 2
    assert report["eligible"] == 1
    assert report["denied"] == 1
    assert report["deny_reasons"]["missing_order_id"] == 1


def test_cli_shadow_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    CaseStore(path=tmp_path / "cases.db")
    code = main(["--actor", "oscar", "cases", "shadow-report", "--days", "7"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["null_send"] == "on"


def test_cli_shadow_report_requires_admin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    CaseStore(path=tmp_path / "cases.db")
    code = main(["--actor", "agent", "cases", "shadow-report", "--days", "7"])
    assert code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error"] == "access_denied"


def test_cli_retention_purge_requires_admin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    CaseStore(path=tmp_path / "cases.db")
    code = main(["--actor", "jonatan", "cases", "retention-purge", "--dry-run"])
    assert code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error"] == "access_denied"


def test_cli_retention_purge_oscar_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    CaseStore(path=tmp_path / "cases.db")
    code = main(["--actor", "oscar", "cases", "retention-purge", "--dry-run"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["dry_run"] is True


def test_systemd_retention_purge_uses_actor_oscar():
    """Prod timer must pass ADMIN actor after RBAC gate on retention-purge."""
    from pathlib import Path

    unit = (
        Path(__file__).resolve().parents[1]
        / "infrastructure"
        / "systemd"
        / "azom-retention-purge.service"
    )
    text = unit.read_text(encoding="utf-8")
    assert "--actor oscar" in text
    assert "cases retention-purge" in text
