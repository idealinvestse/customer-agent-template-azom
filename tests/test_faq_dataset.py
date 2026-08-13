"""Tests for FAQ Q&A dataset export from cases.db."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ecom_ops.cases.store import CaseStore
from ecom_ops.faq.dataset import export_qa_pairs
from ecom_ops.faq.ingest_config import clear_faq_ingest_config_cache
from ecom_ops.faq.pii import redact_pii


@pytest.fixture
def data_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("AZOM_DATA_DIR", str(data))
    monkeypatch.delenv("AZOM_FAQ_INGEST_KILL", raising=False)
    clear_faq_ingest_config_cache()
    return data


@pytest.fixture
def store(tmp_path: Path) -> CaseStore:
    return CaseStore(path=tmp_path / "cases.db")


def _seed_pair(
    store: CaseStore,
    *,
    body_q: str,
    body_a: str,
    category: str = "shipping",
    market: str = "se",
    status: str = "replied",
    order_id: str | None = "12345",
    email: str = "kund@example.com",
) -> str:
    case = store.create_case(
        mailbox_id="se",
        subject="Fråga om leverans",
        from_addr=email,
        body=body_q,
        category=category,
        draft_reply="utkast ska inte exporteras",
        order_id=order_id,
        message_id=f"mid-{body_q[:8]}",
        market=market,
        language="sv",
        status="open",
    )
    store.mark_replied(
        case.id,
        outbound_body=body_a,
        to_addr=email,
        from_addr="support@azom.se",
        subject="Re: Fråga om leverans",
        message_id=f"out-{case.id[:8]}",
    )
    if status == "closed":
        store.close(case.id)
    return case.id


def test_redact_pii_strips_email_and_phone() -> None:
    text = "Kontakta mig på anna@example.com eller 070-123 45 67. Pn 19900101-1234."
    out = redact_pii(text)
    assert "anna@example.com" not in out
    assert "[EMAIL]" in out
    assert "[PERSONNUMMER]" in out
    assert "070-123 45 67" not in out
    assert "[TELEFON]" in out
    dated = redact_pii("Leverans 2024-08-13 till ombudet.")
    assert "2024-08-13" in dated
    assert "[TELEFON]" not in dated


def test_export_pairs_pairing_and_redaction(
    store: CaseStore, data_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    q1 = "Var är min order? Mejla mig på hemlig@exempel.se tack."
    a1 = (
        "Hej! Din order är skickad. Spårningslänk finns i bekräftelsen. "
        "Hör av dig om den inte syns inom två dagar."
    )
    q2 = "Hur lång är returtiden?"
    a2 = (
        "Du har 14 dagars ångerrätt enligt konsumentköplagen. "
        "Skicka ordernummer så hjälper vi dig vidare."
    )
    id1 = _seed_pair(store, body_q=q1, body_a=a1)
    id2 = _seed_pair(
        store,
        body_q=q2,
        body_a=a2,
        category="return",
        order_id=None,
        email="annan@exempel.se",
    )

    result = export_qa_pairs(
        market="se",
        actor="oscar",
        store=store,
        use_mock=True,
        redact=True,
    )
    assert result.ok, result.message
    assert result.pair_count == 2
    assert result.path
    rows = [
        json.loads(line)
        for line in Path(result.path).read_text(encoding="utf-8").splitlines()
    ]
    by_id = {r["case_id"]: r for r in rows}
    assert id1 in by_id and id2 in by_id
    assert "hemlig@exempel.se" not in by_id[id1]["question"]
    assert "[EMAIL]" in by_id[id1]["question"]
    assert by_id[id1]["order_id_present"] is True
    assert by_id[id2]["order_id_present"] is False
    assert "utkast ska inte exporteras" not in by_id[id1]["answer"]
    assert by_id[id1]["answer"].startswith("Hej!")
    assert result.path and result.path.endswith(".jsonl")
    assert not result.path.endswith(".raw.jsonl")


def test_raw_export_uses_separate_filename(
    store: CaseStore, data_env: Path
) -> None:
    _seed_pair(
        store,
        body_q="Var är min order? Mejla hemlig@exempel.se tack.",
        body_a="Hej! Din order är skickad och du får spårning i bekräftelsen snart.",
    )
    raw = export_qa_pairs(
        market="se",
        actor="oscar",
        store=store,
        use_mock=True,
        redact=False,
    )
    assert raw.ok, raw.message
    assert raw.path and raw.path.endswith(".raw.jsonl")
    denied = export_qa_pairs(
        market="se",
        actor="jonatan",
        store=store,
        use_mock=False,
        redact=False,
    )
    assert not denied.ok


def test_export_stale_flag(
    store: CaseStore, data_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    case_id = _seed_pair(
        store,
        body_q="Gammal fråga om fraktstatus för min leverans?",
        body_a=(
            "Svar: paketet är hos ombudet. Hämta inom 14 dagar annars går det i retur."
        ),
    )
    # Backdate both messages so ORDER BY created_at keeps inbound→outbound.
    old_in = (datetime.now(timezone.utc) - timedelta(days=401)).isoformat()
    old_out = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    import sqlite3

    with sqlite3.connect(str(store.path)) as conn:
        conn.execute(
            "UPDATE case_messages SET created_at = ? WHERE case_id = ? AND direction = 'inbound'",
            (old_in, case_id),
        )
        conn.execute(
            "UPDATE case_messages SET created_at = ? WHERE case_id = ? AND direction = 'outbound'",
            (old_out, case_id),
        )
        conn.commit()

    result = export_qa_pairs(
        actor="oscar",
        store=store,
        use_mock=True,
        include_stale=True,
    )
    assert result.ok
    assert result.stale_count >= 1
    rows = [
        json.loads(line)
        for line in Path(result.path).read_text(encoding="utf-8").splitlines()
    ]
    stale_row = next(r for r in rows if r["case_id"] == case_id)
    assert stale_row["stale"] is True

    result2 = export_qa_pairs(
        actor="oscar",
        store=store,
        use_mock=True,
        include_stale=False,
    )
    assert result2.ok
    assert result2.pair_count == 0


def test_export_excludes_abuse_and_requires_permission(
    store: CaseStore, data_env: Path
) -> None:
    _seed_pair(
        store,
        body_q="Hotfullt meddelande om något helt annat här.",
        body_a="Vi tar detta vidare internt och återkommer via rätt kanal snart.",
        category="abuse",
    )
    denied = export_qa_pairs(actor="jonatan", store=store, use_mock=False)
    assert not denied.ok

    result = export_qa_pairs(actor="oscar", store=store, use_mock=True)
    assert result.ok
    assert result.pair_count == 0


def test_short_outbound_is_not_paired(store: CaseStore, data_env: Path) -> None:
    _seed_pair(
        store,
        body_q="Var är min order just nu egentligen?",
        body_a="Ok.",
    )
    result = export_qa_pairs(actor="oscar", store=store, use_mock=True)
    assert result.ok
    assert result.pair_count == 0


def test_agent_mock_export_allowed(store: CaseStore, data_env: Path) -> None:
    _seed_pair(
        store,
        body_q="Hur lång är leveranstiden till ombud just nu?",
        body_a="Vanligtvis 1–3 arbetsdagar beroende på transportör och destination.",
    )
    result = export_qa_pairs(actor="agent", store=store, use_mock=True)
    assert result.ok, result.message
    assert result.pair_count == 1


def test_ingest_kill_switch(
    store: CaseStore, data_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_FAQ_INGEST_KILL", "1")
    clear_faq_ingest_config_cache()
    _seed_pair(
        store,
        body_q="Var är paketet just nu egentligen?",
        body_a="Paketet är skickat och du får spårning i bekräftelsemejlet snart.",
    )
    result = export_qa_pairs(actor="oscar", store=store, use_mock=True)
    assert not result.ok
    assert "kill-switch" in result.message.lower()
