"""CLI coverage for faq list/search/coverage/validate/staging/ingest HITL."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ecom_ops.cli import main
from ecom_ops.faq.ingest_config import clear_faq_ingest_config_cache
from ecom_ops.faq.store import clear_faq_store_cache


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_cli_faq_list_and_search(capsys):
    clear_faq_store_cache()
    code = main(["--mock", "faq", "list", "--market", "se"])
    assert code == 0
    listed = _out(capsys)
    assert listed["ok"] is True
    assert listed["count"] >= 5
    ids = {a["id"] for a in listed["articles"]}
    assert "se-shipping-tracking" in ids

    code = main(
        ["--mock", "faq", "search", "--q", "spårning", "--market", "se"]
    )
    assert code == 0
    hits = _out(capsys)
    assert hits["ok"] is True
    assert hits["count"] >= 1


def test_cli_faq_coverage_validate_reload(capsys):
    clear_faq_store_cache()
    code = main(["--mock", "faq", "coverage"])
    assert code == 0
    cov = _out(capsys)
    assert cov["ok"] is True
    assert cov["coverage"]["total"] >= 5
    assert "se" in cov["coverage"]["markets"]
    assert "drift" in cov

    code = main(["--mock", "faq", "validate"])
    assert code == 0
    report = _out(capsys)
    assert report["ok"] is True
    assert report["article_count"] >= 5
    assert report["error_count"] == 0

    code = main(["--mock", "faq", "reload"])
    assert code == 0
    reloaded = _out(capsys)
    assert reloaded["ok"] is True
    assert reloaded["count"] >= 5


def test_cli_faq_staging_empty(capsys):
    code = main(["--mock", "faq", "staging"])
    assert code == 0
    out = _out(capsys)
    assert out["ok"] is True
    staging = out["staging"]
    assert staging["site_docs"] == 0
    assert staging["article_drafts"] == 0


def test_cli_faq_ingest_site_and_kill_switch(capsys, monkeypatch):
    monkeypatch.delenv("AZOM_FAQ_INGEST_KILL", raising=False)
    clear_faq_ingest_config_cache()
    code = main(
        ["--mock", "--actor", "oscar", "faq", "ingest", "site", "--market", "se"]
    )
    assert code == 0
    out = _out(capsys)
    assert out["ok"] is True
    assert out["written"] >= 1

    monkeypatch.setenv("AZOM_FAQ_INGEST_KILL", "1")
    clear_faq_ingest_config_cache()
    code = main(
        ["--mock", "--actor", "oscar", "faq", "ingest", "products", "--market", "se"]
    )
    assert code == 1
    killed = _out(capsys)
    assert killed["ok"] is False
    assert "kill-switch" in killed["message"].lower()


def test_cli_faq_promote_dry_run_default(capsys):
    from ecom_ops.faq.staging import articles_staging_dir

    staging = articles_staging_dir("se")
    article = {
        "id": "se-cli-promote-demo",
        "market": "se",
        "language": "sv",
        "category": "product",
        "title": "CLI promote demo",
        "body": "En tillräckligt lång brödtext för att passera validering av FAQ-artikel.",
        "customer_safe": False,
        "needs_review": True,
        "tags": ["test"],
    }
    (staging / "se-cli-promote-demo.yaml").write_text(
        yaml.safe_dump([article], allow_unicode=True),
        encoding="utf-8",
    )
    code = main(
        [
            "--mock",
            "--actor",
            "oscar",
            "faq",
            "promote",
            "--id",
            "se-cli-promote-demo",
        ]
    )
    assert code == 0
    out = _out(capsys)
    assert out["ok"] is True
    assert out["dry_run"] is True
    dest = Path(out["dest"] or "")
    assert not dest.exists()


def test_cli_faq_dataset_export(capsys):
    import os

    from ecom_ops.cases.store import CaseStore

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cs = CaseStore(path=data_dir / "cases.db")
    case = cs.create_case(
        mailbox_id="se",
        subject="Leverans",
        from_addr="kund@example.com",
        body="Var är min order just nu egentligen?",
        category="shipping",
        draft_reply="utkast",
        order_id="1001",
        message_id="cli-faq-ds",
        market="se",
        language="sv",
        status="open",
    )
    cs.mark_replied(
        case.id,
        outbound_body=(
            "Hej! Ordern är skickad och du får spårning i bekräftelsen inom kort."
        ),
        to_addr="kund@example.com",
        from_addr="support@azom.se",
        subject="Re: Leverans",
        message_id="cli-faq-ds-out",
    )
    code = main(
        ["--mock", "--actor", "oscar", "faq", "dataset", "export", "--market", "se"]
    )
    assert code == 0
    out = _out(capsys)
    assert out["ok"] is True
    assert out["pair_count"] >= 1
    assert out["path"] and str(out["path"]).endswith(".jsonl")
    assert not str(out["path"]).endswith(".raw.jsonl")


def test_cli_faq_jonatan_cannot_promote(capsys):
    code = main(
        [
            "--mock",
            "--actor",
            "jonatan",
            "faq",
            "promote",
            "--id",
            "se-does-not-exist",
            "--apply",
        ]
    )
    assert code == 1
    out = _out(capsys)
    assert out["ok"] is False
