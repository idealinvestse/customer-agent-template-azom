"""SQLite WAL posture + claim/approve race coverage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ecom_ops.cases.store import CaseStore


def _case(store: CaseStore, mid: str = "<conc@azom>") -> str:
    c = store.create_case(
        mailbox_id="m",
        subject="Order 1001",
        from_addr="a@b.co",
        body="var är order 1001",
        category="order_status",
        draft_reply="utkast",
        order_id="1001",
        message_id=mid,
    )
    return c.id


def test_sqlite_wal_and_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    posture = store.sqlite_posture()
    assert posture["journal_mode"] == "wal"
    assert posture["busy_timeout_ms"] >= 5000


def test_double_claim_only_one_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    cid = _case(store)

    def claim() -> bool:
        return store.claim_for_send(cid) is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        wins = list(pool.map(lambda _: claim(), range(8)))
    assert sum(1 for w in wins if w) == 1
    assert store.get(cid).status == "sending"


def test_regenerate_refuses_sending_status(tmp_path, monkeypatch):
    from ecom_ops.cases.service import CaseService

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    store = CaseStore(path=tmp_path / "cases.db")
    cid = _case(store, mid="<regen-send@azom>")
    assert store.claim_for_send(cid) is not None
    svc = CaseService(store=store)
    result = svc.regenerate_draft(cid, actor="jonatan", use_mock=True)
    assert result.ok is False
    assert "sending" in (result.message or "")


def test_second_approve_loses_claim(tmp_path, monkeypatch):
    from ecom_ops.actions.mail import MailService
    from ecom_ops.cases.service import CaseService
    from ecom_ops.integrations.mail import client_from_env

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    store = CaseStore(path=tmp_path / "cases.db")
    cid = _case(store, mid="<double-approve@azom>")
    svc = CaseService(store=store, mail=MailService(client=client_from_env(use_mock=True)))
    first = svc.approve_and_send(cid, actor="jonatan")
    assert first.ok is True
    second = svc.approve_and_send(cid, actor="jonatan")
    assert second.ok is False
