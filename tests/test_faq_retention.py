"""FAQ dataset/staging retention hygiene."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ecom_ops.faq.retention import (
    purge_dataset_case_ids,
    purge_staging_older_than,
)
from ecom_ops.faq.staging import articles_staging_dir
from ecom_ops.rbac import Actor


def test_purge_dataset_case_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    ds = tmp_path / "faq_dataset"
    ds.mkdir()
    keep = {
        "case_id": "keep-1",
        "question": "Var är paketet?",
        "answer": "Det är skickat.",
    }
    drop = {
        "case_id": "drop-1",
        "question": "Hemlig fråga",
        "answer": "Svar",
    }
    path = ds / "qa_se_20260101.jsonl"
    path.write_text(
        json.dumps(keep) + "\n" + json.dumps(drop) + "\n",
        encoding="utf-8",
    )
    removed = purge_dataset_case_ids(["drop-1"])
    assert removed == 1
    remaining = path.read_text(encoding="utf-8")
    assert "keep-1" in remaining
    assert "drop-1" not in remaining


def test_staging_purge_dry_run_and_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    staging = articles_staging_dir("se")
    old = staging / "old.yaml"
    old.write_text("id: se-old\n", encoding="utf-8")
    old_time = datetime.now(timezone.utc) - timedelta(days=120)
    ts = old_time.timestamp()
    Path(old).touch()
    import os

    os.utime(old, (ts, ts))
    oscar = Actor(name="oscar", role="full_admin")
    dry = purge_staging_older_than(days=90, apply=False, actor=oscar)
    assert dry.ok and dry.dry_run
    assert old.is_file()
    applied = purge_staging_older_than(days=90, apply=True, actor=oscar)
    assert applied.ok and not applied.dry_run
    assert applied.files_removed >= 1
    assert not old.is_file()


def test_staging_purge_denies_jonatan(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    jon = Actor(name="jonatan", role="viewer")
    result = purge_staging_older_than(days=90, apply=False, actor=jon)
    assert not result.ok
