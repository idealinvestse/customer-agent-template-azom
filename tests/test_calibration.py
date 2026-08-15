"""Redacted calibration export/report + fixture helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecom_ops.cases.calibration import (
    FORBIDDEN_EXPORT_KEYS,
    build_calibration_report,
    export_calibration_samples,
    reviewed_sample_to_fixture,
    write_fixture_from_reviewed,
)
from ecom_ops.cases.store import CaseStore
from ecom_ops.cli import main
from ecom_ops.kpis import compare_kpis_to_baseline, load_baseline


def _seed(store: CaseStore) -> None:
    a = store.create_case(
        mailbox_id="support_default",
        subject="Var är order 1001?",
        from_addr="kund@example.com",
        body="Hej, var är paketet för order 1001?",
        category="order_status",
        draft_reply="Hej, order 1001 är skickad.",
        order_id="1001",
        message_id="<cal-a@azom>",
        classify_confidence=0.91,
        classify_method="hybrid",
        suggest_approve=True,
    )
    store.set_status(a.id, "replied")
    store.create_case(
        mailbox_id="support_default",
        subject="Retur",
        from_addr="retur@example.com",
        body="Jag vill returnera",
        category="return",
        draft_reply="Vi hjälper till med retur.",
        order_id=None,
        message_id="<cal-b@azom>",
        classify_confidence=0.7,
        classify_method="keyword",
        suggest_approve=False,
        status="escalated",
    )


def test_export_has_no_pii(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    _seed(store)
    payload = export_calibration_samples(days=30, store=store)
    assert payload["n"] == 2
    for sample in payload["samples"]:
        assert FORBIDDEN_EXPORT_KEYS.isdisjoint(sample)
        assert "subject" not in sample
        assert "body" not in sample
        assert "from_addr" not in sample
        assert "draft_reply" not in sample
        assert "text" not in sample
        leaked = json.dumps(sample)
        assert "kund@example.com" not in leaked
        assert "Var är order" not in leaked
        assert "paketet" not in leaked


def test_calibration_report_crosstab(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    _seed(store)
    report = build_calibration_report(days=30, store=store)
    assert report["ok"] is True
    assert report["calibration_complete"] is False
    assert report["suggest_n"] == 1
    assert report["suggest_approved_sent"] == 1
    assert report["never_suggest_starred"] == 0


def test_reviewed_fixture_helper(tmp_path):
    with pytest.raises(ValueError):
        reviewed_sample_to_fixture({"category": "order_status"})
    fixture = reviewed_sample_to_fixture(
        {
            "id": "rev_order_sv",
            "text": "Var är order 1001?",
            "expected_category": "order_status",
            "order_id_in_text": "1001",
            "suggest_with_llm_confidence": 0.91,
            "expect_suggest_approve": True,
        }
    )
    path = write_fixture_from_reviewed(fixture, directory=tmp_path)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["expected_category"] == "order_status"
    assert data["text"]


def test_cli_calibration_requires_admin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    CaseStore(path=tmp_path / "cases.db")
    code = main(["--actor", "jonatan", "cases", "calibration-export"])
    assert code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "access_denied"


def test_cli_calibration_oscar(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    _seed(store)
    code = main(["--actor", "oscar", "cases", "calibration-report", "--days", "30"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["calibration_complete"] is False


def test_kpis_baseline_compare(tmp_path):
    path = tmp_path / "baseline.yaml"
    path.write_text(
        "start_date: 2026-08-01\n"
        "hours_per_week_or_proxy: 10\n"
        "source: jonatan notes\n"
        "notes: test\n"
        "median_time_to_approve_sec: 600\n",
        encoding="utf-8",
    )
    baseline = load_baseline(path)
    cmp = compare_kpis_to_baseline(
        {"median_time_to_approve_sec": 300, "n_case_approved": 4},
        baseline,
    )
    assert cmp["deltas"]["median_time_to_approve_sec"] == -300.0
    assert cmp["baseline_complete"] is False
    assert not cmp["missing_human_fields"]
