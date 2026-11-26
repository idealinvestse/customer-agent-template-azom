"""Wave 5 verification: Cases, Product Desc, Marketing, CLI, ops_gc, runtime_health, backup."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from ecom_ops.actions.mail import MailService
from ecom_ops.actions.marketing import MarketingService
from ecom_ops.actions.product_desc import ProductDescService
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.cli import main as cli_main
from ecom_ops.integrations.mail import (
    InMemoryMailTransport,
    MailClient,
    MailConfig,
    MailMessage,
    MailProvider,
)
from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient
from ecom_ops.marketing.suggest_store import MarketingSuggestStore
from ecom_ops.ops_gc import run_ops_gc
from ecom_ops.research import get_research_service
from ecom_ops.research.models import RESEARCH_ID_RE, SearchPolicyError
from ecom_ops.research.store import ResearchStore, SearchCache
from ecom_ops.runtime_health import collect_runtime_snapshot, format_ops_telegram_summary

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_live_sockets(monkeypatch: pytest.MonkeyPatch):
    """Fail immediately if any test attempts a real outbound network socket or HTTP call."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def _forbid(*_args, **_kwargs):
        raise AssertionError("Live network socket/HTTP call forbidden in Wave 5 tests")

    monkeypatch.setattr(socket, "create_connection", _forbid)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _forbid)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", _forbid)
    monkeypatch.setattr(urllib.request, "urlopen", _forbid)
    monkeypatch.setattr(requests.Session, "request", _forbid)


def _set_mtime(path: Path, ts: float) -> None:
    os.utime(path, (ts, ts))
    if path.is_dir():
        for child in path.rglob("*"):
            if not child.is_symlink():
                os.utime(child, (ts, ts))


# ── 1. Cases Poll Hard Isolation (0 web search/research calls) ──────────────


def test_cases_poll_and_ingest_make_zero_web_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WEB_SEARCH_ENABLED", "1")
    monkeypatch.setenv("AZOM_RESEARCH_ENABLED", "1")

    def _boom(*_args, **_kwargs):
        raise AssertionError("cases.ingest must NEVER invoke web search or research")

    monkeypatch.setattr("ecom_ops.research.get_research_service", _boom)
    monkeypatch.setattr("ecom_ops.integrations.search.router.SearchRouter.search", _boom)
    monkeypatch.setattr("ecom_ops.integrations.search.fetcher.SafePublicHttpClient.fetch", _boom)

    transport = InMemoryMailTransport()
    transport.inbox = [
        MailMessage(
            subject="Leverans och retur för order 1001",
            body="Hej, var är mitt paket från PostNord för order 1001? Vill kanske returnera.",
            from_addr="kund@example.com",
            message_id="<poll-iso-1@azom.se>",
            uid="uid-iso-1",
        ),
        MailMessage(
            subject="Faktura dubbel debitering",
            body="Min faktura verkar felaktig, kan ni kontrollera?",
            from_addr="billing-kund@example.com",
            message_id="<poll-iso-2@azom.se>",
            uid="uid-iso-2",
        ),
    ]
    client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            username="mock@azom.se",
            from_addr="support@azom.se",
        ),
        transport=transport,
    )
    monkeypatch.setattr("ecom_ops.cases.ingest.client_from_env", lambda **kw: client)

    store = CaseStore(path=tmp_path / "cases.db")
    svc = CaseService(store=store, mail=MailService(client=client))
    res = svc.poll(actor="agent", use_mock=True)
    assert res.ok is True
    assert res.created == 2
    assert not (tmp_path / "research_jobs.sqlite3").exists()
    assert not (tmp_path / "web_search_cache.sqlite3").exists()


# ── 2. Cases On-Demand Research, Draft Regenerate & GDPR Retention Unlink ───


def test_case_research_taint_refusal_and_safe_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    store = CaseStore(path=tmp_path / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Leveransförsening order 1001 - Anna Lindqvist <anna.lindqvist@example.com>",
        from_addr="Anna Lindqvist <anna.lindqvist@example.com>",
        body="Hej! Mitt telefonnummer är +46701234567 och order 1001 sitter fast hos PostNord.",
        category="shipping",
        draft_reply="Hej, vi kollar på detta.",
        order_id="1001",
        message_id="<case-res-1@azom.se>",
        site="azom",
        market="SE",
        language="sv",
    )
    svc = CaseService(store=store)

    # PII/taint in explicit query must fail closed with SearchPolicyError (OutboundQueryBlocked)
    for bad_query in (
        "PostNord status anna.lindqvist@example.com",
        "Anna Lindqvist leverans",
        "order 1001 spårning",
        f"case {case.id[:8]} status",
        "ring +46701234567",
    ):
        with pytest.raises(SearchPolicyError, match="OutboundQueryBlocked"):
            svc.start_case_research(case.id, query=bad_query, actor="jonatan")

    # Auto-built query scrubs PII and links target_ref="case:<uuid>" safely
    started = svc.start_case_research(case.id, query=None, wait=True, actor="jonatan")
    assert started["ok"] is True
    rid = started["research_id"]
    assert RESEARCH_ID_RE.fullmatch(rid)
    assert started["target_ref"] == f"case:{case.id}"

    brief_json = json.dumps(started.get("brief") or {}, ensure_ascii=False).lower()
    assert "anna.lindqvist@example.com" not in brief_json
    assert "lindqvist" not in brief_json
    assert "+46701234567" not in brief_json

    # Invariant #17.8: target_ref must NOT appear in disk artifacts
    art_dir = tmp_path / "research_artifacts" / rid
    for fname in ("meta.json", "brief.json", "brief.md", "sources.json"):
        raw = (art_dir / fname).read_text(encoding="utf-8")
        assert f"case:{case.id}" not in raw
        assert '"target_ref"' not in raw


def test_regenerate_draft_with_research_brief_safety_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    store = CaseStore(path=tmp_path / "cases.db")
    transport = InMemoryMailTransport()
    mail_client = MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            username="mock@azom.se",
            from_addr="support@azom.se",
        ),
        transport=transport,
    )
    svc = CaseService(store=store, mail_client=mail_client)

    order_case = store.create_case(
        mailbox_id="support_default",
        subject="Var är order 1001?",
        from_addr="kund@example.com",
        body="Hej, var är min order 1001?",
        category="order_status",
        draft_reply="Gammal draft",
        order_id="1001",
        message_id="<regen-res-order@azom.se>",
        site="azom",
        language="sv",
        classify_confidence=0.98,
        classify_method="llm",
    )
    return_case = store.create_case(
        mailbox_id="support_default",
        subject="Retur",
        from_addr="retur@example.com",
        body="Jag vill returnera min vara, fel storlek.",
        category="return",
        draft_reply="Gammal returdraft",
        order_id="1002",
        message_id="<regen-res-return@azom.se>",
        site="azom",
        language="sv",
        classify_confidence=0.95,
        classify_method="keyword",
    )
    abuse_case = store.create_case(
        mailbox_id="support_default",
        subject="Chargeback",
        from_addr="angry@example.com",
        body="Chargeback and legal threat",
        category="abuse",
        draft_reply="Gammal abuse",
        order_id="1003",
        message_id="<regen-res-abuse@azom.se>",
        site="azom",
        status="escalated",
        priority="high",
        escalation_id="esc-1",
    )

    started = svc.start_case_research(
        order_case.id,
        query="PostNord leveransstatus driftstörning",
        wait=True,
        actor="jonatan",
    )
    assert started["ok"] is True
    rid = started["research_id"]

    # Force support.handle to return suggest_approve=True so we prove research_brief_id forces it False
    monkeypatch.setattr(
        svc.support,
        "handle",
        lambda *_a, **_kw: SimpleNamespace(
            reply="Hej, din order är på väg.",
            category=SimpleNamespace(value="order_status"),
            order_id="1001",
            confidence=0.99,
            classify_method="llm",
            suggest_approve=True,
            escalated=False,
            ticket_id=None,
        ),
    )
    captured_contexts: list[str] = []

    def _fake_draft(**kwargs):
        captured_contexts.append(str(kwargs.get("order_context") or ""))
        return "Uppdaterat utkast med research-stöd."

    monkeypatch.setattr("ecom_ops.llm.draft_support_with_llm", _fake_draft)

    res = svc.regenerate_draft(
        order_case.id, actor="jonatan", use_mock=True, research_brief_id=rid
    )
    assert res.ok is True, res.message
    assert res.case is not None
    # NEVER sets suggest_approve=True when research_brief_id is used, NEVER sends mail
    assert res.case["suggest_approve"] is False
    assert res.case["status"] == "open"
    assert not [c for c in transport.calls if c[0] == "send"]
    assert captured_contexts
    ctx = captured_contexts[0]
    assert "<<<UNTRUSTED_RESEARCH_BRIEF nonce=" in ctx
    assert "<<<END_UNTRUSTED_RESEARCH_BRIEF nonce=" in ctx
    # Framed brief portion stays bounded <= 4 KiB
    framed_part = ctx[ctx.index("<<<UNTRUSTED_RESEARCH_BRIEF") :]
    assert len(framed_part.encode("utf-8")) <= 4096

    # Brief linked to order_case must be refused for a different case
    wrong_case_res = svc.regenerate_draft(
        return_case.id, actor="jonatan", use_mock=True, research_brief_id=rid
    )
    assert wrong_case_res.ok is False
    assert "different case" in wrong_case_res.message.lower()

    # Return (never_suggest_categories) & abuse stay suggest_approve=False even with team/unlinked brief
    ret_started = svc.start_case_research(
        return_case.id,
        query="konsumentköplagen ångerrätt 14 dagar",
        wait=True,
        actor="jonatan",
    )
    monkeypatch.setattr(
        svc.support,
        "handle",
        lambda *_a, **_kw: SimpleNamespace(
            reply="Returinfo",
            category=SimpleNamespace(value="return"),
            order_id="1002",
            confidence=0.99,
            classify_method="keyword",
            suggest_approve=True,
            escalated=False,
            ticket_id=None,
        ),
    )
    ret_regen = svc.regenerate_draft(
        return_case.id,
        actor="jonatan",
        use_mock=True,
        research_brief_id=ret_started["research_id"],
    )
    assert ret_regen.ok is True
    assert ret_regen.case["suggest_approve"] is False
    assert ret_regen.case["status"] == "open"

    # Abuse remains escalated and never invokes research LLM drafting
    ab_started = svc.start_case_research(
        abuse_case.id,
        query="konsumenttvist allmänna reklamationsnämnden",
        wait=True,
        actor="jonatan",
    )
    calls_before = len(captured_contexts)
    ab_regen = svc.regenerate_draft(
        abuse_case.id,
        actor="jonatan",
        use_mock=True,
        research_brief_id=ab_started["research_id"],
    )
    assert ab_regen.ok is True
    assert ab_regen.case["status"] == "escalated"
    assert ab_regen.case["suggest_approve"] is False
    assert len(captured_contexts) == calls_before


def test_cases_retention_gdpr_delete_and_purge_unlink_research_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ecom_ops.cases.retention import gdpr_delete, purge_closed_cases

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    store = CaseStore(path=tmp_path / "cases.db")
    svc = CaseService(store=store)

    c_gdpr = store.create_case(
        mailbox_id="support_default",
        subject="GDPR case",
        from_addr="gdpr-user@example.com",
        body="Fråga om produkt",
        category="other",
        draft_reply="Svar",
        order_id=None,
        message_id="<gdpr-unlink@azom.se>",
    )
    c_purge = store.create_case(
        mailbox_id="support_default",
        subject="Old closed case",
        from_addr="old-user@example.com",
        body="Gammal fråga",
        category="other",
        draft_reply="Svar",
        order_id=None,
        message_id="<purge-unlink@azom.se>",
    )
    store.close(c_purge.id)

    old_iso = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (old_iso, c_purge.id))
        conn.commit()

    r_gdpr = svc.start_case_research(
        c_gdpr.id, query="CarPlay USB adapter specifikation", wait=False, actor="jonatan"
    )
    r_purge = svc.start_case_research(
        c_purge.id, query="DAB+ antenn montering", wait=False, actor="jonatan"
    )

    rs = ResearchStore(
        path=tmp_path / "research_jobs.sqlite3",
        artifacts_dir=tmp_path / "research_artifacts",
    )
    assert rs.get_job(r_gdpr["research_id"], actor="oscar").target_ref == f"case:{c_gdpr.id}"
    assert rs.get_job(r_purge["research_id"], actor="oscar").target_ref == f"case:{c_purge.id}"

    del_res = gdpr_delete(email="gdpr-user@example.com", db_path=store.path)
    assert del_res["ok"] is True and del_res["deleted"] == 1

    purge_res = purge_closed_cases(store=store, retention_days=90, redact=False)
    assert purge_res.ok is True and purge_res.deleted == 1

    with sqlite3.connect(rs.path) as conn:
        for rid in (r_gdpr["research_id"], r_purge["research_id"]):
            row = conn.execute(
                "SELECT target_ref, spec_json FROM research_jobs WHERE id = ?", (rid,)
            ).fetchone()
            assert row is not None
            assert row[0] is None
            assert json.loads(row[1]).get("target_ref") is None


# ── 3. Product Descriptions: Hard Gate + SKU/Attrs + Safe Research Brief ────


def test_product_desc_refuses_research_and_publish_and_enriches_sku_attrs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    transport = InMemoryWooTransport()
    transport.products["501"] = {
        "id": 501,
        "name": "Azom Pro CarPlay Unit",
        "sku": "AZ-CP-501",
        "categories": [{"id": 12, "name": "CarPlay"}, {"id": 15, "name": "Bilmultimedia"}],
        "attributes": [
            {"name": "Skärmstorlek", "options": ["9 tum", "IPS"]},
            {"name": "Anslutning", "option": "Trådlös CarPlay & Android Auto"},
        ],
        "short_description": "",
        "description": "",
    }
    woo = WooCommerceClient(base_url="https://mock.local", transport=transport)
    svc = ProductDescService(woo=woo)

    # Hard gate: run_research=True + publish=True MUST refuse before any Woo update
    with pytest.raises(SearchPolicyError, match="auto-publish"):
        svc.generate(
            product_id="501",
            language="sv",
            publish=True,
            run_research=True,
            actor="agent",
        )
    assert not any(c[0] == "update_product_description" for c in transport.calls)

    # Enqueue & run a safe product research brief
    res_svc = get_research_service()
    job = res_svc.start_research(
        "wireless CarPlay head unit installation specs",
        actor="agent",
        profile_id="technical_docs",
        depth="quick",
        target_ref="product:501",
    )
    from ecom_ops.research.engine import ResearchEngine

    ResearchEngine(
        store=res_svc.store,
        search_router=res_svc.search_router,
        fetcher=res_svc.fetcher,
        telemetry=res_svc.telemetry,
    ).run_job(job.research_id)

    captured: list[dict] = []

    def _fake_prod_llm(**kwargs):
        captured.append(kwargs)
        return ("Kort text för AZ-CP-501.", "<p>Detaljerad specifikation för AZ-CP-501.</p>")

    monkeypatch.setattr("ecom_ops.llm.generate_product_desc_with_llm", _fake_prod_llm)

    draft = svc.generate(
        product_id="501",
        language="sv",
        publish=False,
        research_brief_id=job.research_id,
        actor="agent",
        allow_llm=True,
    )
    assert draft.ok is True
    assert draft.published is False
    assert captured
    passed_features = captured[0]["features"]
    passed_ctx = captured[0].get("research_context") or ""
    assert "SKU: AZ-CP-501" in passed_features
    assert "Categories: CarPlay, Bilmultimedia" in passed_features
    assert "Skärmstorlek: 9 tum, IPS" in passed_features
    assert "<<<UNTRUSTED_RESEARCH_BRIEF nonce=" in passed_ctx
    assert "<<<END_UNTRUSTED_RESEARCH_BRIEF nonce=" in passed_ctx
    assert len(passed_ctx.encode("utf-8")) <= 4096

    # run_research=True with publish=False is allowed as draft
    draft_live = svc.generate(
        product_id="501",
        language="sv",
        publish=False,
        run_research=True,
        actor="agent",
        allow_llm=True,
    )
    assert draft_live.ok is True
    assert draft_live.published is False
    assert "<<<UNTRUSTED_WEB_DATA nonce=" in (captured[1].get("research_context") or "")


# ── 4. Marketing: Waste Annotation, Research Topic & Truth Hierarchy ────────


def test_marketing_waste_annotation_research_topic_and_mutate_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("AZOM_ADS_MUTATE_KILL", raising=False)
    monkeypatch.delenv("AZOM_MP_KILL", raising=False)

    suggests = MarketingSuggestStore(path=tmp_path / "mkt_suggests.jsonl")
    svc = MarketingService(use_mock=True, suggests=suggests)

    dig_before = svc.digest(days=7, actor="jonatan").data
    built = svc.build_waste_suggests(actor="jonatan", annotate_research=True)
    assert built.ok is True
    items = (built.data or {}).get("suggests") or []
    negatives = [s for s in items if s["kind"] == "negative"]
    assert negatives
    ev = negatives[0]["evidence"]
    assert "research" in ev
    assert ev["research"]["source"] == "web_research"
    assert ev["research"]["provenance"] == "mock"
    assert ev["conversions"] == 0
    assert ev["cost_micros"] > 0

    # Ads/GA4/Woo digest numbers are strictly unchanged by web research
    dig_after = svc.digest(days=7, actor="jonatan").data
    assert dig_after["ads"] == dig_before["ads"]
    assert dig_after["ga4"] == dig_before["ga4"]

    # Ads mutate still blocked by default config + kill switches + RBAC
    blocked = svc.approve_and_mutate(negatives[0]["id"], actor="jonatan")
    assert blocked.ok is False
    assert "blocked" in blocked.message.lower() or "disabled" in blocked.message.lower()

    mp_q = svc.queue_mp_event({"name": "purchase", "params": {}}, actor="jonatan")
    assert mp_q.ok is True
    assert svc.approve_and_mutate(mp_q.data["id"], actor="oscar").ok is False

    # research_topic enforces MARKETING_READ + RESEARCH_RUN and preserves truth_note
    topic_res = svc.research_topic(
        "nordic car multimedia seasonal demand Q4",
        depth="quick",
        wait=True,
        actor="jonatan",
    )
    assert topic_res["ok"] is True
    assert RESEARCH_ID_RE.fullmatch(topic_res["research_id"])
    assert topic_res["target_ref"] == "marketing:general"
    assert "source=web_research" in topic_res["truth_note"]

    bad_ref = svc.research_topic(
        "test", target_ref="wp_site:azom_se", actor="jonatan"
    )
    assert bad_ref["ok"] is False
    assert bad_ref["error_class"] == "policy_blocked"


# ── 5. CLI End-to-End (Mock / Offline) ──────────────────────────────────────


def test_cli_search_fetch_research_cases_marketing_product_status_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    # search
    assert cli_main(["--mock", "search", "--query", "woocommerce checkout hooks", "--profile", "technical_docs"]) == 0
    s_out = json.loads(capsys.readouterr().out)
    assert s_out["ok"] is True and s_out["provenance"] == "mock" and s_out["hits_count"] >= 1

    # fetch-url (offline deterministic mock)
    assert cli_main(["--mock", "fetch-url", "--url", "https://developer.wordpress.org/block-editor/"]) == 0
    f_out = json.loads(capsys.readouterr().out)
    assert f_out["ok"] is True and f_out["provenance"] == "mock" and "developer.wordpress.org" in f_out["text"]

    # research start --wait, list, show, status, steer, cancel
    assert cli_main(["--mock", "--actor", "jonatan", "research", "start", "--topic", "CarPlay antenna specs", "--depth", "quick", "--market", "SE", "--wait"]) == 0
    r_wait = json.loads(capsys.readouterr().out)
    assert r_wait["ok"] is True
    rid_done = r_wait["research_id"]
    assert RESEARCH_ID_RE.fullmatch(rid_done)

    assert cli_main(["--mock", "--actor", "jonatan", "research", "list"]) == 0
    r_list = json.loads(capsys.readouterr().out)
    assert r_list["ok"] is True and any(j["research_id"] == rid_done for j in r_list["jobs"])

    for section in ("full", "summary", "sources"):
        assert cli_main(["--mock", "--actor", "jonatan", "research", "show", "--id", rid_done, "--section", section]) == 0
        r_show = json.loads(capsys.readouterr().out)
        assert r_show["ok"] is True

    assert cli_main(["--mock", "--actor", "jonatan", "research", "status", "--id", rid_done]) == 0
    r_stat = json.loads(capsys.readouterr().out)
    assert r_stat["ok"] is True and r_stat["status"] in {"completed", "partial"}

    # Async queued job -> steer -> cancel
    assert cli_main(["--mock", "--actor", "jonatan", "research", "start", "--topic", "Nordic ecom UX checkout", "--depth", "standard"]) == 0
    r_async = json.loads(capsys.readouterr().out)
    rid_q = r_async["research_id"]
    assert r_async["status"] == "queued"

    assert cli_main(["--mock", "--actor", "jonatan", "research", "steer", "--id", rid_q, "--note", "Fokusera på mobilcheckout"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli_main(["--mock", "--actor", "jonatan", "research", "cancel", "--id", rid_q]) == 0
    r_can = json.loads(capsys.readouterr().out)
    assert r_can["ok"] is True and r_can["result"] == "cancelled"

    # cases research (success + PII refusal)
    case = CaseStore(path=tmp_path / "cases.db").create_case(
        mailbox_id="support_default",
        subject="Fraktfråga",
        from_addr="pelle@example.com",
        body="Hur lång är leveranstiden med PostNord?",
        category="shipping",
        draft_reply="Hej",
        order_id="1009",
        message_id="<cli-case-res@azom.se>",
        market="SE",
    )
    assert cli_main(["--mock", "--actor", "jonatan", "cases", "research", "--id", case.id, "--depth", "quick", "--wait"]) == 0
    cr_ok = json.loads(capsys.readouterr().out)
    assert cr_ok["ok"] is True and cr_ok["target_ref"] == f"case:{case.id}"

    assert cli_main(["--mock", "--actor", "jonatan", "cases", "research", "--id", case.id, "--query", "status pelle@example.com"]) == 1
    cr_bad = json.loads(capsys.readouterr().out)
    assert cr_bad["ok"] is False and cr_bad["error_class"] == "SearchPolicyError"

    # marketing research
    assert cli_main(["--mock", "--actor", "jonatan", "marketing", "research", "--topic", "Google Ads Consent Mode v2 SE", "--depth", "quick", "--wait"]) == 0
    mr_out = json.loads(capsys.readouterr().out)
    assert mr_out["ok"] is True and mr_out["target_ref"] == "marketing:general"

    # product-desc --research & --research --publish refusal
    assert cli_main(["--mock", "--actor", "agent", "product-desc", "--product-id", "501", "--research"]) == 0
    pd_ok = json.loads(capsys.readouterr().out)
    assert pd_ok["ok"] is True and pd_ok["published"] is False

    # Private jonatan brief cannot be read by agent; agent's own brief (or oscar) succeeds
    assert cli_main(["--mock", "--actor", "agent", "product-desc", "--product-id", "501", "--research-brief", rid_done]) == 1
    pd_denied = json.loads(capsys.readouterr().out)
    assert pd_denied["ok"] is False and pd_denied["escalated"] is True

    assert cli_main(["--mock", "--actor", "agent", "research", "start", "--topic", "CarPlay 9 inch specs", "--depth", "quick", "--wait"]) == 0
    rid_agent = json.loads(capsys.readouterr().out)["research_id"]

    assert cli_main(["--mock", "--actor", "agent", "product-desc", "--product-id", "501", "--research-brief", rid_agent]) == 0
    pd_brief = json.loads(capsys.readouterr().out)
    assert pd_brief["ok"] is True

    assert cli_main(["--mock", "--actor", "agent", "product-desc", "--product-id", "501", "--research", "--publish"]) == 1
    pd_refuse = json.loads(capsys.readouterr().out)
    assert pd_refuse["ok"] is False and pd_refuse["error_class"] == "SearchPolicyError"

    # status + smoke
    assert cli_main(["--mock", "status"]) == 0
    st_out = json.loads(capsys.readouterr().out)
    assert st_out["web_search"]["status"] == "mock"
    assert st_out["web_search"]["ready"] is True

    assert cli_main(["--mock", "smoke", "--live"]) == 0
    sm_mock = json.loads(capsys.readouterr().out)
    checks_mock = {c["name"]: c for c in sm_mock["checks"]}
    assert checks_mock["web_search"]["ok"] is True
    assert checks_mock["web_search"]["status"] == "mock"

    # Live smoke with AZOM_WEB_SEARCH_ENABLED disabled must report honest skipped_disabled (zero sockets)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_WEB_SEARCH_ENABLED", raising=False)
    monkeypatch.setattr("ecom_ops.integrations.woocommerce.client_from_env", lambda **kw: WooCommerceClient(base_url="https://mock.local", transport=InMemoryWooTransport()))
    monkeypatch.setattr(
        "ecom_ops.integrations.mail.client_from_env",
        lambda **kw: MailClient(config=MailConfig(provider=MailProvider.GENERIC_IMAP, from_addr="s@azom.se"), transport=InMemoryMailTransport()),
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy-ignored-by-monkeypatch")
    monkeypatch.setattr(
        "requests.get",
        lambda *_a, **_kw: SimpleNamespace(status_code=200, json=lambda: {"ok": True}),
    )
    assert cli_main(["smoke", "--live"]) == 0
    sm_live_off = json.loads(capsys.readouterr().out)
    checks_off = {c["name"]: c for c in sm_live_off["checks"]}
    assert checks_off["web_search"]["ok"] is True
    assert checks_off["web_search"]["status"] == "skipped_disabled"


# ── 6. ops_gc, runtime_health & bin/backup-azom-data.sh ────────────────────


def test_ops_gc_search_cache_research_artifacts_dry_run_and_symlink_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    data_dir = tmp_path / "data"
    outside_dir = tmp_path / "outside"
    data_dir.mkdir()
    outside_dir.mkdir()
    outside_secret = outside_dir / "keep_research.txt"
    outside_secret.write_text("KEEP ME", encoding="utf-8")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))

    now_ts = time.time()
    old_ts = now_ts - 20 * 86400  # older than 14d TTL + 24h grace

    # 1. Populate SearchCache with 1 expired row + 1 active row
    cache = SearchCache(path=data_dir / "web_search_cache.sqlite3")
    with sqlite3.connect(cache.path) as conn:
        conn.execute(
            "INSERT INTO search_cache(cache_key, kind, payload_json, size_bytes, created_at, last_accessed_at, expires_at) VALUES (?, 'search', ?, 32, ?, ?, ?)",
            ("expired-k", '{"ok":true}', now_ts - 7200, now_ts - 7200, now_ts - 60),
        )
        conn.execute(
            "INSERT INTO search_cache(cache_key, kind, payload_json, size_bytes, created_at, last_accessed_at, expires_at) VALUES (?, 'search', ?, 32, ?, ?, ?)",
            ("active-k", '{"ok":true}', now_ts, now_ts, now_ts + 3600),
        )
        conn.commit()

    # 2. Populate ResearchStore with expired non-pinned, expired pinned, active, and symlinked-outside artifact
    rs = ResearchStore(
        path=data_dir / "research_jobs.sqlite3",
        artifacts_dir=data_dir / "research_artifacts",
    )
    (data_dir / "research_artifacts").mkdir(parents=True, exist_ok=True)

    exp_id = "res-000000000001"
    pin_id = "res-000000000002"
    act_id = "res-000000000003"
    sym_id = "res-000000000004"

    with sqlite3.connect(rs.path) as conn:
        for jid, status, pinned, ts_val in (
            (exp_id, "completed", 0, old_ts),
            (pin_id, "completed", 1, old_ts),
            (act_id, "completed", 0, now_ts - 3600),
            (sym_id, "completed", 0, old_ts),
        ):
            conn.execute(
                """
                INSERT INTO research_jobs (id, actor, status, profile_id, depth, visibility, spec_json, pinned, created_at, updated_at, completed_at)
                VALUES (?, 'jonatan', ?, 'deep_general', 'quick', 'private', '{"topic":"t"}', ?, ?, ?, ?)
                """,
                (jid, status, pinned, ts_val, ts_val, ts_val),
            )
        conn.commit()

    for jid in (exp_id, pin_id, act_id):
        d = data_dir / "research_artifacts" / jid
        d.mkdir(parents=True)
        (d / "brief.md").write_text("# Brief\n" + ("x" * 256), encoding="utf-8")
        _set_mtime(d, old_ts if jid != act_id else now_ts - 3600)

    sym_dir = data_dir / "research_artifacts" / sym_id
    symlink_supported = True
    try:
        os.symlink(outside_dir, sym_dir, target_is_directory=True)
    except OSError:
        symlink_supported = False
        # Create a regular dir with a symlink inside if OS blocks dir symlinks
        sym_dir.mkdir(parents=True)
        try:
            os.symlink(outside_secret, sym_dir / "escape.txt")
        except OSError:
            pass

    # Dry-run MUST be non-destructive
    dry = run_ops_gc(data_dir=data_dir, dry_run=True)
    assert dry["ok"] is True and dry["dry_run"] is True
    assert dry["purged_search_cache_rows"] == 1
    assert dry["removed_research_artifacts"] == 1
    assert dry["bytes_reclaimed"] > 0
    assert (data_dir / "research_artifacts" / exp_id / "brief.md").is_file()
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0] == 2

    # Execute GC
    live = run_ops_gc(data_dir=data_dir, dry_run=False)
    assert live["ok"] is True and live["dry_run"] is False
    assert live["purged_search_cache_rows"] == 1
    assert live["removed_research_artifacts"] == 1
    assert not (data_dir / "research_artifacts" / exp_id).exists()
    assert (data_dir / "research_artifacts" / pin_id / "brief.md").is_file()
    assert (data_dir / "research_artifacts" / act_id / "brief.md").is_file()
    assert outside_secret.read_text(encoding="utf-8") == "KEEP ME"
    if symlink_supported:
        assert sym_dir.is_symlink()

    with sqlite3.connect(cache.path) as conn:
        keys = [r[0] for r in conn.execute("SELECT cache_key FROM search_cache").fetchall()]
        assert keys == ["active-k"]
    with sqlite3.connect(rs.path) as conn:
        rem_ids = {r[0] for r in conn.execute("SELECT id FROM research_jobs").fetchall()}
        assert exp_id not in rem_ids
        assert pin_id in rem_ids and act_id in rem_ids


def test_runtime_health_snapshot_and_swedish_ops_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSA_SECRET_KEY_DO_NOT_LEAK_12345")

    now_ts = time.time()
    old_ts = now_ts - 20 * 86400

    rs = ResearchStore(
        path=tmp_path / "research_jobs.sqlite3",
        artifacts_dir=tmp_path / "research_artifacts",
    )
    with sqlite3.connect(rs.path) as conn:
        conn.execute(
            "INSERT INTO research_jobs (id, actor, status, profile_id, depth, visibility, spec_json, pinned, created_at, updated_at, completed_at) VALUES ('res-aaaaaaaaaaaa', 'jonatan', 'queued', 'deep_general', 'quick', 'private', '{}', 0, ?, ?, NULL)",
            (now_ts, now_ts),
        )
        conn.execute(
            "INSERT INTO research_jobs (id, actor, status, profile_id, depth, visibility, spec_json, pinned, created_at, updated_at, completed_at) VALUES ('res-bbbbbbbbbbbb', 'jonatan', 'completed', 'deep_general', 'quick', 'private', '{}', 0, ?, ?, ?)",
            (old_ts, old_ts, old_ts),
        )
        conn.commit()
    art = tmp_path / "research_artifacts" / "res-bbbbbbbbbbbb"
    art.mkdir(parents=True)
    (art / "brief.md").write_text("brief", encoding="utf-8")

    cache = SearchCache(path=tmp_path / "web_search_cache.sqlite3")
    cache.set("k1", {"ok": True}, ttl_seconds=3600)

    (tmp_path / "search_router_health.json").write_text(
        json.dumps(
            {
                "entries": {
                    "route:brave": {
                        "state": "open",
                        "cooldown_until": now_ts + 60,
                        "failure_count": 2,
                        "last_error_class": "SearchRateLimitError",
                        "last_error_code": "rate_limit",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    snap = collect_runtime_snapshot(data_dir=tmp_path)
    summary = format_ops_telegram_summary(snap)
    dumped = json.dumps(snap)

    assert "BSA_SECRET_KEY_DO_NOT_LEAK_12345" not in dumped
    assert "BSA_SECRET_KEY_DO_NOT_LEAK_12345" not in summary

    assert snap["queues"]["research"]["queued"] == 1
    assert snap["queues"]["research"]["completed"] == 1
    assert snap["search_router"]["mock_mode"] is True
    assert snap["search_router"]["open_breakers_count"] == 1
    assert snap["search_router"]["breakers"]["route:brave"]["state"] == "open"
    assert "research_artifacts" in snap["disk_usage"]["folders"]
    assert "web_search_cache" in snap["disk_usage"]["folders"]
    assert snap["disk_usage"]["expired_research_artifacts_count"] == 1

    assert "Research-kö:" in summary
    assert "Sökrouter (mock):" in summary
    assert "utgångna research-artefakter" in summary


def test_backup_script_research_inclusions_exclusions_and_syntax():
    script_path = ROOT / "bin" / "backup-azom-data.sh"
    text = script_path.read_text(encoding="utf-8")

    assert "research_jobs" in text
    assert '${DATA_DIR}/research_jobs.sqlite3' in text
    assert '--exclude="*/research_jobs.sqlite3"' in text
    assert '--exclude="*/research_jobs.sqlite3-wal"' in text
    assert '--exclude="*/research_jobs.sqlite3-shm"' in text
    assert '--exclude="*/web_search_cache.sqlite3*"' in text
    assert '--exclude="*/research_artifacts*"' in text
    assert '--exclude="*/search_router_health.lock"' in text

    bash_bin = shutil.which("bash")
    if bash_bin:
        proc = subprocess.run(
            [bash_bin, "-n"],
            input=text.replace("\r\n", "\n").encode("utf-8"),
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")

    shellcheck_bin = shutil.which("shellcheck")
    if shellcheck_bin:
        proc = subprocess.run(
            [shellcheck_bin, str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
