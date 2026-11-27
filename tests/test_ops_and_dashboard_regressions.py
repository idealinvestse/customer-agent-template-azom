"""Regression tests for cases, actions, integrations, CLI, and dashboard hardening (#108, #112, #123, #127)."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import socket
import sqlite3
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


@pytest.fixture(autouse=True)
def _forbid_live_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Live network socket connection attempted in offline test")

    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)


def _setup_case_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ecom_ops.cases.service import CaseService
    from ecom_ops.cases.store import CaseStore
    from ecom_ops.integrations.mail import (
        InMemoryMailTransport,
        MailClient,
        MailConfig,
        MailProvider,
    )
    from ecom_ops.telemetry import Telemetry

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(data_dir / "audit.jsonl"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    transport = InMemoryMailTransport()
    client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            from_addr="support@azom.se",
            username="support@azom.se",
        ),
        transport=transport,
    )
    store = CaseStore(path=data_dir / "cases.db")
    tel = Telemetry(path=data_dir / "telemetry.jsonl")
    svc = CaseService(store=store, mail_client=client, telemetry=tel)
    return svc, store, transport, data_dir


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    for name in (
        "azom_dashboard_regressions_test",
        "auth",
        "cases_routes",
        "core_routes",
        "health_routes",
        "marketing_routes",
        "oauth_routes",
        "oscar_routes",
        "research_routes",
        "secret_probes",
        "settings_store",
        "status",
        "support",
        "webhooks",
        "wp",
        "wp_routes",
    ):
        sys.modules.pop(name, None)
    for key in [k for k in list(sys.modules) if k.startswith(("wp.", "infrastructure.dashboard"))]:
        sys.modules.pop(key, None)

    spec = importlib.util.spec_from_file_location(
        "azom_dashboard_regressions_test", DASH_DIR / "app.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_regressions_test"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    mod.app.config["TESTING"] = True
    return mod


@pytest.fixture
def dash_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for item in (ROOT / "config").iterdir():
        if item.is_file():
            (cfg_dir / item.name).write_text(item.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(data_dir / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-ops-dashboard-secret")
    monkeypatch.chdir(DASH_DIR)

    mod = _load_dashboard_app()
    return {
        "mod": mod,
        "app": mod.app,
        "client": mod.app.test_client(),
        "data_dir": data_dir,
        "cfg_dir": cfg_dir,
    }


def _basic(user: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{user}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _auth_csrf(client, user: str) -> dict[str, str]:
    hdrs = _basic(user)
    resp = client.get("/", headers=hdrs)
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        tok = sess.get("csrf_token")
    assert tok
    hdrs["X-CSRF-Token"] = str(tok)
    return hdrs


def test_rfc5322_display_name_from_across_ingest_regen_gdpr_and_approve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Display-name ``From:`` headers work across ingest, regenerate, GDPR lookup, and approve."""
    from ecom_ops.cases.ingest import ingest_message
    from ecom_ops.cases.mailboxes import MailboxConfig
    from ecom_ops.cases.retention import gdpr_cases_for_email, gdpr_export
    from ecom_ops.integrations.mail import MailMessage
    from ecom_ops.rbac import resolve_actor

    svc, store, transport, _data_dir = _setup_case_env(tmp_path, monkeypatch)
    mb = MailboxConfig(
        id="support_default",
        label="Support",
        site="azom",
        market="SE",
        language="sv",
        provider="generic_imap",
        address="support@azom.se",
    )
    mail_msg = MailMessage(
        uid="u-1",
        from_addr="Anna Svensson <anna@example.com>",
        to_addrs=["support@azom.se"],
        subject="Var är order #1001?",
        body="Hej! Var är min order #1001?",
        message_id="<rfc5322-1@example.com>",
    )
    ingested = ingest_message(
        svc,
        mb,
        mail_msg,
        actor=resolve_actor("agent"),
        client=svc.mail.client,
        use_mock=True,
    )
    assert ingested is not None
    assert ingested.from_addr == "anna@example.com"
    assert ingested.order_id == "1001"
    assert ingested.category in {"order_status", "shipping"}

    # Legacy row storing un-normalized RFC 5322 display name directly in SQLite
    legacy = store.create_case(
        mailbox_id="support_default",
        subject="Order #1001 fråga",
        from_addr="Anna Svensson <anna@example.com>",
        body="Hej, när skickas order #1001?",
        category="order_status",
        draft_reply="Gammalt svar",
        order_id="1001",
        message_id="<rfc5322-legacy@example.com>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )

    regen = svc.regenerate_draft(legacy.id, actor="jonatan", use_mock=True)
    assert regen.ok is True, regen.message

    with sqlite3.connect(str(store.path)) as conn:
        matched_ids = gdpr_cases_for_email(conn, "anna@example.com")
    assert set(matched_ids) == {ingested.id, legacy.id}
    exported = gdpr_export(email="anna@example.com", db_path=store.path)
    assert exported["ok"] is True
    assert {c["id"] for c in exported["cases"]} == {ingested.id, legacy.id}

    sent = svc.approve_and_send(legacy.id, actor="jonatan")
    assert sent.ok is True, sent.message
    assert transport.outbox
    assert transport.outbox[-1].to_addrs == ["anna@example.com"]


def test_start_case_research_taint_inbound_only_and_safe_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-order IDs, addresses, inbound-only scan, faktura fallback, and no raw subject leak."""
    from ecom_ops.cases.service import build_case_taint_context
    from ecom_ops.research import get_research_service

    svc, store, _transport, _data_dir = _setup_case_env(tmp_path, monkeypatch)
    case = store.create_case(
        mailbox_id="support_default",
        subject="Leverans till Storgatan 14B, Göteborg - order 1001 och order 2002 - paket 003733253812345678",
        from_addr="faktura@kundbolag.se",
        body="Hej, se order 1001 och order 2002 till Storgatan 14B, Göteborg",
        category="billing",
        draft_reply="Utkast",
        order_id="1001",
        message_id="<taint-case-1@kundbolag.se>",
        site="azom",
        language="sv",
        market="SE",
        to_addr="support@azom.se",
    )
    with store._conn() as conn:
        conn.execute(
            """
            INSERT INTO case_messages (
                id, case_id, direction, from_addr, to_addr, subject, body,
                message_id, created_at, in_reply_to, references_header
            ) VALUES (?, ?, 'outbound', ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                "out-msg-1",
                case.id,
                "support@azom.se",
                "faktura@kundbolag.se",
                "Re: Leverans",
                "Hei,\nVennlig hilsen\nAzom Support",
                "<taint-out-1@azom.se>",
                datetime.now(UTC).isoformat(),
            ),
        )

    msgs = store.messages(case.id)
    taint = build_case_taint_context(case, msgs)
    assert "1001" in taint.order_ids
    assert "2002" in taint.order_ids
    assert any("Storgatan" in addr for addr in taint.addresses)
    assert any("Göteborg" in addr for addr in taint.addresses)
    assert "Azom Support" not in taint.customer_names

    started = svc.start_case_research(case.id, query=None, actor="jonatan")
    assert started["ok"] is True
    job = get_research_service().store.get_job(started["research_id"])
    assert job is not None and job.spec is not None
    topic = job.spec.topic
    for leaked in ("Storgatan", "Göteborg", "1001", "2002", "faktura", "003733253812345678"):
        assert leaked.lower() not in topic.lower()


def test_regenerate_draft_single_llm_call_strips_delimiters_and_keeps_sb5_ask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``regenerate_draft`` with ``framed_brief`` makes 1 LLM call, strips tags, and keeps ordernummer ask."""
    from ecom_ops import llm
    from ecom_ops.research.models import EvidenceStatus, ResearchBrief, ResearchSpec
    from ecom_ops.research.store import ResearchStore

    svc, store, _transport, data_dir = _setup_case_env(tmp_path, monkeypatch)
    rstore = ResearchStore(
        path=data_dir / "research_jobs.sqlite3",
        artifacts_dir=data_dir / "research_artifacts",
    )
    job = rstore.enqueue_job(
        ResearchSpec(topic="Nordisk kundsupport returvillkor", depth="quick"),
        actor="jonatan",
    )
    brief = ResearchBrief(
        research_id=job.research_id,
        topic="Nordisk kundsupport returvillkor",
        profile_id="support_fact_check",
        evidence_status=EvidenceStatus.GROUNDED,
        summary="Kunder behöver ange ordernummer för spårning.",
        recommendations=["Be kunden uppge sitt ordernummer."],
        gaps=[],
        citations=[],
        created_at=datetime.now(UTC).isoformat(),
    )
    rstore.complete_job(
        job.research_id,
        status="completed",
        evidence_status=EvidenceStatus.GROUNDED,
        brief=brief,
        sources=[],
    )

    case = store.create_case(
        mailbox_id="support_default",
        subject="Returfråga",
        from_addr="kund@example.se",
        body="Hej, jag vill skicka tillbaka min vara.",
        category="return",
        draft_reply="Gammalt utkast",
        order_id=None,
        message_id="<regen-brief-1@example.se>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )

    llm_calls: list[dict[str, Any]] = []

    def _fake_draft_with_llm(**kwargs: Any) -> str:
        llm_calls.append(kwargs)
        return (
            "Hej! <<<UNTRUSTED_RESEARCH_BRIEF_deadbeef>>>Vi hjälper dig gärna med returen."
            "<<<END_UNTRUSTED_RESEARCH_BRIEF_deadbeef>>>"
        )

    monkeypatch.setattr(llm, "draft_support_with_llm", _fake_draft_with_llm)

    res = svc.regenerate_draft(
        case.id,
        actor="jonatan",
        use_mock=True,
        research_brief_id=job.research_id,
    )
    assert res.ok is True, res.message
    assert len(llm_calls) == 1
    assert res.case is not None
    draft = res.case["draft_reply"]
    assert "<<<UNTRUSTED_RESEARCH_BRIEF" not in draft
    assert "<<<END_UNTRUSTED_RESEARCH_BRIEF" not in draft
    assert "ordernum" in draft.lower()


def test_retention_skips_creating_research_db_and_keeps_ok_on_unlink_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``retention.py`` must not create ``research_jobs.sqlite3`` when absent or fail on unlink error."""
    from ecom_ops.cases import retention

    svc, store, _transport, data_dir = _setup_case_env(tmp_path, monkeypatch)
    old_ts = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    c1 = store.create_case(
        mailbox_id="support_default",
        subject="Gammalt ärende",
        from_addr="old@example.se",
        body="Tack",
        category="other",
        draft_reply="Varsågod",
        order_id=None,
        message_id="<ret-1@example.se>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )
    c2 = store.create_case(
        mailbox_id="support_default",
        subject="GDPR ärende",
        from_addr="gdpr@example.se",
        body="Radera",
        category="other",
        draft_reply="Klart",
        order_id=None,
        message_id="<ret-2@example.se>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )
    with sqlite3.connect(str(store.path)) as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', updated_at = ? WHERE id = ?",
            (old_ts, c1.id),
        )
        conn.commit()

    assert not (data_dir / "research_jobs.sqlite3").exists()
    purged = retention.purge_closed_cases(store=store, retention_days=30)
    assert purged.ok is True
    assert purged.deleted == 1
    assert not (data_dir / "research_jobs.sqlite3").exists()

    deleted = retention.gdpr_delete(email="gdpr@example.se", db_path=store.path)
    assert deleted["ok"] is True
    assert deleted["deleted"] == 1
    assert not (data_dir / "research_jobs.sqlite3").exists()

    # Now simulate unlink failure post-commit on both purge_closed_cases and gdpr_delete
    c3 = store.create_case(
        mailbox_id="support_default",
        subject="Ärende 3",
        from_addr="warn@example.se",
        body="Hej",
        category="other",
        draft_reply="Svar",
        order_id=None,
        message_id="<ret-3@example.se>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )
    c4 = store.create_case(
        mailbox_id="support_default",
        subject="Ärende 4",
        from_addr="warn2@example.se",
        body="Hej",
        category="other",
        draft_reply="Svar",
        order_id=None,
        message_id="<ret-4@example.se>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )
    with sqlite3.connect(str(store.path)) as conn:
        conn.execute(
            "UPDATE cases SET status = 'closed', updated_at = ? WHERE id = ?",
            (old_ts, c3.id),
        )
        conn.commit()

    monkeypatch.setattr(
        retention,
        "_unlink_case_research_refs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    purged_warn = retention.purge_closed_cases(store=store, retention_days=30)
    assert purged_warn.ok is True
    assert "research unlink warning" in purged_warn.message

    gdpr_warn = retention.gdpr_delete(email="warn2@example.se", db_path=store.path)
    assert gdpr_warn["ok"] is True
    assert "warning" in gdpr_warn


def test_product_desc_marketing_woo_gads_and_cli_access_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify product_desc allow_llm/publish gates, marketing SearchRouteError, Woo traversal, GAQL BETWEEN, and CLI AccessDenied."""
    from ecom_ops import cli
    from ecom_ops.actions.marketing import MarketingService
    from ecom_ops.actions.product_desc import ProductDescService
    from ecom_ops.integrations.google_ads import LiveGoogleAdsTransport
    from ecom_ops.integrations.search.errors import SearchConfigError
    from ecom_ops.integrations.woocommerce import WooCommerceClient
    from ecom_ops.integrations.woocommerce import client_from_env as woo_from_env
    from ecom_ops.research.models import SearchPolicyError
    from ecom_ops.security import SecurityError

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(data_dir / "audit.jsonl"))

    # 1) product_desc skips research when allow_llm=False, still raises SearchPolicyError on run_research+publish
    pd_svc = ProductDescService(woo=woo_from_env(use_mock=True))
    search_calls: list[Any] = []
    monkeypatch.setattr(
        "ecom_ops.research.ResearchService.quick_search",
        lambda *a, **kw: search_calls.append((a, kw)) or {},
    )
    pd_res = pd_svc.generate(product_id="501", allow_llm=False, run_research=True, actor="agent")
    assert pd_res.ok is True
    assert search_calls == []
    tel_lines = (data_dir / "telemetry.jsonl").read_text(encoding="utf-8")
    assert '"research_applied": false' in tel_lines.lower()
    assert '"has_research": false' in tel_lines.lower()

    with pytest.raises(SearchPolicyError):
        pd_svc.generate(product_id="501", run_research=True, publish=True, actor="agent")

    # 2) marketing.research_topic(wait=True) catches SearchRouteError
    mkt = MarketingService()
    monkeypatch.setattr(
        "ecom_ops.research.engine.ResearchEngine.run_job",
        lambda *_a, **_kw: (_ for _ in ()).throw(SearchConfigError("missing provider key")),
    )
    mkt_res = mkt.research_topic("CarPlay marknadsanalys SE", wait=True, actor="jonatan")
    assert mkt_res["ok"] is False
    assert mkt_res["error_class"] == "SearchConfigError"

    # 3) WooCommerceClient.delete_shipment_tracking & delete_webhook reject path traversal
    woo_client = WooCommerceClient(
        base_url="https://mock.azom.example",
        consumer_key="ck",
        consumer_secret="cs",
    )
    with pytest.raises(SecurityError):
        woo_client.delete_shipment_tracking("1001", "../../products/501")
    with pytest.raises(SecurityError):
        woo_client.delete_webhook("../orders/1001")

    # 4) LiveGoogleAdsTransport.campaign_performance emits segments.date BETWEEN
    gads = LiveGoogleAdsTransport(access_token="tok", developer_token="dev")
    seen_queries: list[str] = []
    monkeypatch.setattr(
        gads,
        "_search",
        lambda _cid, query: seen_queries.append(query) or {"results": []},
    )
    gads.campaign_performance("1234567890", days=10)
    assert len(seen_queries) == 1
    assert "DURING LAST_" not in seen_queries[0]
    assert re.search(
        r"segments\.date BETWEEN '\d{4}-\d{2}-\d{2}' AND '\d{4}-\d{2}-\d{2}'",
        seen_queries[0],
    )

    # 5) cli.main() catches AccessDenied cleanly as JSON exit 1
    capsys.readouterr()
    rc = cli.main(
        ["--mock", "--actor", "unknown_actor", "cases", "close", "--id", "missing-case"]
    )
    out = capsys.readouterr().out
    assert rc == 1
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert parsed["error"] == "access_denied"


def test_dashboard_research_routes_taint_steer_cancel_next_url_hub_detail_health_auth_metrics(
    dash_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify dashboard research hardening, health/metrics/auth fixes, and template states."""
    from ecom_ops.cases.store import CaseStore
    from ecom_ops.integrations.search.errors import SearchConfigError
    from ecom_ops.research.models import ResearchSpec
    from ecom_ops.research.store import ResearchStore

    client = dash_env["client"]
    data_dir: Path = dash_env["data_dir"]
    mod = dash_env["mod"]
    hdrs = _auth_csrf(client, "jonatan")

    # 1) POST /research/jobs catches SearchRouteError cleanly (302 flash, not 500)
    monkeypatch.setattr(
        "ecom_ops.research.ResearchService.start_research",
        lambda *_a, **_kw: (_ for _ in ()).throw(SearchConfigError("provider missing")),
    )
    err_resp = client.post(
        "/research/jobs",
        data={"topic": "WooCommerce HPOS hooks", "profile_id": "technical_docs"},
        headers=hdrs,
    )
    assert err_resp.status_code == 302
    assert "err=" in err_resp.headers["Location"]

    # Restore real ResearchService.start_research for remaining checks
    monkeypatch.undo()
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(dash_env["cfg_dir"]))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(data_dir / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-ops-dashboard-secret")

    # 2) POST /research/jobs builds QueryTaintContext on case:* and blocks PII
    case_store = CaseStore(path=data_dir / "cases.db")
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="Leveransfråga",
        from_addr="Bengt Holmberg <bengt@example.se>",
        body="Hej, min order 987654 har fastnat.",
        category="shipping",
        draft_reply="Utkast",
        order_id="987654",
        message_id="<dash-taint-1@example.se>",
        site="azom",
        language="sv",
        to_addr="support@azom.se",
    )
    pii_resp = client.post(
        "/research/jobs",
        data={
            "topic": "PostNord leverans Bengt Holmberg 987654",
            "target_ref": f"case:{case.id}",
        },
        headers=hdrs,
    )
    assert pii_resp.status_code == 302
    assert "err=" in pii_resp.headers["Location"]

    # 3) _safe_next_url rejects C0 control chars & handles #fragment cleanly
    import research_routes

    assert research_routes._safe_next_url("/\x0b/evil.com") is None
    assert research_routes._safe_next_url("/a\x7fb") is None
    assert research_routes._safe_next_url("/cases/1#frag") == "/cases/1"

    frag_resp = client.post(
        "/research/jobs",
        data={
            "topic": "Nordisk e-handel leveranstider",
            "next_url": "/cases/1#frag",
        },
        headers=hdrs,
    )
    assert frag_resp.status_code == 302
    loc = frag_resp.headers["Location"]
    assert "#frag?msg=" not in loc
    assert urlparse(loc).path == "/cases/1"
    assert "msg" in parse_qs(urlparse(loc).query)

    # 4) Steer enforces OutboundQueryFirewall on active case:* job and rejects terminal jobs
    rstore = ResearchStore(
        path=data_dir / "research_jobs.sqlite3",
        artifacts_dir=data_dir / "research_artifacts",
    )
    active_job = rstore.enqueue_job(
        ResearchSpec(topic="Nordisk frakt", depth="quick"),
        actor="jonatan",
        target_ref=f"case:{case.id}",
        visibility="team",
    )
    steer_pii = client.post(
        f"/research/{active_job.research_id}/steer",
        data={"note": "Kontakta kund Bengt Holmberg order 987654"},
        headers=hdrs,
    )
    assert steer_pii.status_code == 302
    assert "err=" in steer_pii.headers["Location"]
    assert rstore.consume_steers(active_job.research_id) == []

    term_job = rstore.enqueue_job(
        ResearchSpec(topic="Färdigt jobb", depth="quick"),
        actor="jonatan",
        target_ref=f"case:{case.id}",
        visibility="team",
    )
    rstore.complete_job(
        term_job.research_id,
        status="failed",
        error_code="SearchUnavailableError",
        error_message=None,
    )

    steer_term = client.post(
        f"/research/{term_job.research_id}/steer",
        data={"note": "Bra notering"},
        headers=hdrs,
    )
    assert steer_term.status_code == 302
    assert "err=" in steer_term.headers["Location"]

    # 5) POST /research/<id>/cancel flashes already_terminal message
    cancel_term = client.post(f"/research/{term_job.research_id}/cancel", headers=hdrs)
    assert cancel_term.status_code == 302
    assert "redan" in cancel_term.headers["Location"].lower() or "info=" in cancel_term.headers["Location"]
    assert "Avbrytning" not in cancel_term.headers["Location"]

    # 6) detail.html shows error_code fallback in data-initial-error when error_message is None
    detail_resp = client.get(f"/research/{term_job.research_id}", headers=_basic("jonatan"))
    assert detail_resp.status_code == 200
    detail_html = detail_resp.get_data(as_text=True)
    assert 'data-initial-error="SearchUnavailableError"' in detail_html

    # 7) hub.html shows live active when web_search_readiness status == "ready"
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jonatan")
    monkeypatch.setenv("AZOM_WEB_SEARCH_ENABLED", "1")
    monkeypatch.setenv("PARALLEL_API_KEY", "test-parallel-key")
    hub_resp = client.get("/research", headers=_basic("jonatan"))
    assert hub_resp.status_code == 200
    hub_html = hub_resp.get_data(as_text=True)
    assert "Live webbsökning aktiv" in hub_html
    assert "Inaktiverad (fail-closed)" not in hub_html

    # 8) GET /health includes web_search_status
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    health_json = health_resp.get_json()
    assert "web_search_status" in health_json
    assert health_json["web_search_status"] == "ready"

    # 9) auth.py has _login_failures_lock and never derives fallback secret from passwords
    import auth as dash_auth

    assert isinstance(dash_auth._login_failures_lock, type(threading.Lock()))
    monkeypatch.delenv("DASHBOARD_SECRET_KEY", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "super-secret-viewer-pass")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "super-secret-oscar-pass")
    mod.app.secret_key = "azom-mock-dashboard-secret"
    dash_auth.configure_secret_key(mod.app)
    assert isinstance(mod.app.secret_key, str)
    assert mod.app.secret_key != "azom-mock-dashboard-secret"
    assert "super-secret" not in mod.app.secret_key

    # 10) /metrics closes SQLite connection cleanly
    closed_flags: list[bool] = []
    real_connect = sqlite3.connect

    class _ClosingSpy:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, *args: Any, **kwargs: Any):
            return self._inner.execute(*args, **kwargs)

        def close(self) -> None:
            closed_flags.append(True)
            self._inner.close()

    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "test-metrics-token")
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: _ClosingSpy(real_connect(*args, **kwargs)),
    )
    metrics_resp = client.get(
        "/metrics", headers={"Authorization": "Bearer test-metrics-token"}
    )
    assert metrics_resp.status_code == 200
    assert closed_flags == [True]


def test_dashboard_templates_have_zero_uncompiled_tailwind_classes() -> None:
    """Verify none of the uncompiled Tailwind utilities remain in templates vs static/css/dashboard.css."""
    css = (DASH_DIR / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
    tpl_dir = DASH_DIR / "templates"

    uncompiled_tokens = (
        "sm:grid-cols-4",
        "sm:items-end",
        "ring-steel-500/50",
        "border-steel-500/30",
        "border-steel-500/40",
        "border-ok/40",
        "bg-ok/10",
        "bg-ok/5",
        "text-ok",
    )
    class_attr_re = re.compile(r'\bclass\s*=\s*"([^"]*)"')

    offenders: list[tuple[str, str]] = []
    for tpl in sorted(tpl_dir.rglob("*.html")):
        raw = tpl.read_text(encoding="utf-8")
        # Strip Jinja expressions so we only inspect literal class tokens
        stripped = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", raw, flags=re.DOTALL)
        all_tokens: set[str] = set()
        for match in class_attr_re.finditer(stripped):
            all_tokens.update(match.group(1).split())
        for tok in uncompiled_tokens:
            if tok in all_tokens:
                escaped = tok.replace(":", "\\:").replace("/", "\\/")
                if escaped not in css and f".{tok}" not in css:
                    offenders.append((tpl.relative_to(tpl_dir).as_posix(), tok))
    assert offenders == [], f"Uncompiled CSS utility classes found in templates: {offenders}"

    # Also ensure replacement classes used across marketing/wp/runtime templates are compiled in dashboard.css
    expected_compiled = (
        "sm\\:grid-cols-2",
        "lg\\:grid-cols-4",
        "lg\\:items-end",
        "ring-steel-500\\/40",
        "border-steel-500\\/35",
        "border-steel-500\\/45",
        "border-ok\\/30",
        "bg-teal-500\\/15",
        "text-teal-400",
    )
    for selector in expected_compiled:
        assert selector in css, f"Expected compiled utility {selector!r} missing from dashboard.css"
