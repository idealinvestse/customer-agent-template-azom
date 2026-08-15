"""Structural hardening: overlays, case state machine, retention, CI contract."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ecom_ops import __version__
from ecom_ops.cases.retention import (
    gdpr_delete,
    gdpr_export,
    purge_closed_cases,
)
from ecom_ops.cases.store import CaseStore
from ecom_ops.integrations.webhooks import verify_webhook_signature
from ecom_ops.profile import load_profile
from ecom_ops.runtime_env import (
    apply_overlays,
    env_flag,
    overlay_key_allowed,
    reset_bootstrap_for_tests,
)
from ecom_ops.security import mask_email

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def test_env_flag_accepts_on():
    import os

    os.environ["AZOM_TEST_FLAG"] = "on"
    assert env_flag("AZOM_TEST_FLAG") is True
    os.environ.pop("AZOM_TEST_FLAG", None)
    assert env_flag("AZOM_TEST_FLAG", default=False) is False


def test_overlay_skips_protected_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    (tmp_path / "runtime.env").write_text(
        "AZOM_USE_MOCK=1\nAZOM_AUTO_SEND_KILL=1\nMAIL_PROVIDER=gmail\n",
        encoding="utf-8",
    )
    (tmp_path / "secrets.env").write_text(
        "MAIL_PASSWORD=secret\nMESSENGER_ALLOWED_PSIDS=999\n",
        encoding="utf-8",
    )
    reset_bootstrap_for_tests()
    applied = apply_overlays(data_dir=tmp_path)
    assert "MAIL_PROVIDER" in applied
    assert "MAIL_PASSWORD" in applied
    assert "AZOM_USE_MOCK" not in applied
    assert "MESSENGER_ALLOWED_PSIDS" not in applied
    assert overlay_key_allowed("AZOM_USE_MOCK") is False
    import os

    assert os.environ.get("AZOM_USE_MOCK") == "0"
    assert os.environ.get("MAIL_PROVIDER") == "gmail"


def test_close_refuses_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="mb",
        subject="S",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<h1@x>",
    )
    assert store.claim_for_send(case.id) is not None
    assert store.close(case.id) is None
    assert store.get(case.id).status == "sending"


def test_mark_replied_requires_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="mb",
        subject="S",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<h2@x>",
    )
    assert (
        store.mark_replied(
            case.id,
            outbound_body="Hej",
            to_addr="a@b.co",
            from_addr="",
            subject="Re: S",
        )
        is None
    )
    store.claim_for_send(case.id)
    updated = store.mark_replied(
        case.id,
        outbound_body="Hej",
        to_addr="a@b.co",
        from_addr="",
        subject="Re: S",
    )
    assert updated and updated.status == "replied"


def test_stale_send_claim_sweeper(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="mb",
        subject="S",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<h3@x>",
    )
    store.claim_for_send(case.id)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with store._conn() as conn:
        conn.execute(
            "UPDATE cases SET updated_at = ? WHERE id = ?",
            (old, case.id),
        )
    n = store.release_stale_send_claims(max_age_seconds=600)
    assert n == 1
    assert store.get(case.id).status == "open"


def test_create_case_integrity_returns_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    a = store.create_case(
        mailbox_id="mb",
        subject="S",
        from_addr="a@b.co",
        body="x",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<dup@x>",
    )
    b = store.create_case(
        mailbox_id="mb",
        subject="S2",
        from_addr="a@b.co",
        body="y",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<dup@x>",
    )
    assert a.id == b.id


def test_retention_includes_replied(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="mb",
        subject="Old",
        from_addr="cust@azom.se",
        body="hej",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<old@x>",
    )
    store.claim_for_send(case.id)
    store.mark_replied(
        case.id,
        outbound_body="Hej",
        to_addr="cust@azom.se",
        from_addr="",
        subject="Re: Old",
    )
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (old, case.id))
    result = purge_closed_cases(store=store, retention_days=90)
    assert result.ok
    assert result.deleted == 1
    assert store.get(case.id) is None


def test_retention_redact_replied(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="mb",
        subject="Old",
        from_addr="cust@azom.se",
        body="hej",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<old2@x>",
    )
    store.claim_for_send(case.id)
    store.mark_replied(
        case.id,
        outbound_body="Hej",
        to_addr="cust@azom.se",
        from_addr="",
        subject="Re: Old",
    )
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (old, case.id))
    result = purge_closed_cases(store=store, retention_days=90, redact=True)
    assert result.ok
    assert result.redacted == 1
    fresh = store.get(case.id)
    assert fresh is not None
    assert fresh.from_addr == "[redacted]"


def test_gdpr_export_validates_and_matches_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    store = CaseStore(path=tmp_path / "cases.db")
    store.create_case(
        mailbox_id="mb",
        subject="S",
        from_addr="Cust@Azom.se",
        body="x",
        category="other",
        draft_reply="Hej",
        order_id=None,
        message_id="<g1@x>",
    )
    bad = gdpr_export(email="not-an-email", db_path=tmp_path / "cases.db")
    assert bad["ok"] is False
    exported = gdpr_export(email="cust@azom.se", db_path=tmp_path / "cases.db")
    assert exported["ok"] is True
    assert len(exported["cases"]) == 1
    deleted = gdpr_delete(email="CUST@azom.se", db_path=tmp_path / "cases.db")
    assert deleted["ok"] is True
    assert deleted["deleted"] == 1


def test_woo_signature_accepts_base64():
    body = b'{"id": 1}'
    digest = hmac.new(b"sec", body, hashlib.sha256).digest()
    b64 = base64.b64encode(digest).decode()
    assert verify_webhook_signature(body, b64, "sec") is True
    assert verify_webhook_signature(body, digest.hex(), "sec") is True


def test_mask_email():
    assert mask_email("jonatan@azom.se") == "j***@azom.se"


def test_profile_order_status_url():
    prof = load_profile()
    url = prof.order_status_url("1001")
    assert "1001" in url
    assert "{order_id}" not in url


def test_version_sync():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject
    skill = (ROOT / "skills" / "ecom-ops" / "SKILL.md").read_text(encoding="utf-8")
    assert f'version: "{__version__}"' in skill
    compose = (ROOT / "infrastructure" / "docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )
    major_minor = ".".join(__version__.split(".")[:2])
    assert f"azom-agent:{major_minor}" in compose


def test_azom_order_url_uses_profile_template(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "profile.yaml").write_text(
        "customer: acme\nbrand: Acme\n"
        "order_status_url_template: https://shop.acme.test/order/{order_id}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg))
    from ecom_ops.profile import load_profile as _lp

    assert "shop.acme.test" in _lp().order_status_url("42")
    assert "azom.se" not in _lp().order_status_url("42")


def _load_dash_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    for name in ("azom_dashboard_hardening", "settings_store", "auth"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "azom_dashboard_hardening", DASH_DIR / "app.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_hardening"] = mod
    spec.loader.exec_module(mod)
    return mod.app


@pytest.fixture
def harden_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-hardening-secret")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dash_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_ready_503_when_poll_stale(harden_client, tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_POLL_STALE_SEC", "60")
    from ecom_ops.ops_status import write_last_case_poll

    write_last_case_poll(
        ok=True,
        errors=0,
        created=0,
        polled_at=(datetime.now(UTC) - timedelta(hours=3)).isoformat(),
    )
    resp = harden_client.get("/ready")
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False


def test_woo_webhook_http(harden_client, monkeypatch):
    monkeypatch.setenv("WOO_WEBHOOK_SECRET", "woo-secret")
    body = json.dumps({"id": 9}).encode()
    sig = hmac.new(b"woo-secret", body, hashlib.sha256).hexdigest()
    missing = harden_client.post("/webhooks/woo", data=body)
    assert missing.status_code in {401, 503}
    bad = harden_client.post(
        "/webhooks/woo",
        data=body,
        headers={"X-WC-Webhook-Signature": "nope", "X-WC-Webhook-Topic": "order.updated"},
    )
    assert bad.status_code == 401
    ok = harden_client.post(
        "/webhooks/woo",
        data=body,
        headers={"X-WC-Webhook-Signature": sig, "X-WC-Webhook-Topic": "order.updated"},
    )
    assert ok.status_code == 200
    assert ok.get_json()["ok"] is True


def test_conversation_store_atomic(tmp_path):
    from ecom_ops.bot.store import ConversationStore

    path = tmp_path / "telegram_state.json"
    store = ConversationStore(path=path)
    store.set(1, {"messages": [{"role": "user", "content": "hej"}]})
    assert path.is_file()
    assert not path.with_name("telegram_state.json.tmp").exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "1" in raw
