"""Tests for Wave 2 Shop Intelligence WP tools, playbooks, prompts, and context adaptation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ecom_ops.actions.mail import MailService
from ecom_ops.actions.product_desc import ProductDescService
from ecom_ops.actions.support import SupportService
from ecom_ops.bot import chat_agent
from ecom_ops.bot.seo_handlers import EXPLICIT_ADS_RE, SEO_INTENT_RE
from ecom_ops.bot.wp_turn import (
    append_wp_context_blocks,
    append_wp_guidance_blocks,
    format_wp_capabilities_lines,
    prepare_wp_turn,
)
from ecom_ops.cases.drafting import regenerate_draft
from ecom_ops.cases.service import CaseService
from ecom_ops.cases.store import CaseStore
from ecom_ops.escalation import EscalationService
from ecom_ops.integrations.mail import InMemoryMailTransport, MailClient, MailConfig, MailProvider
from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient
from ecom_ops.prompts import _BUILTIN, _PROMPT_NAMES, get_prompt
from ecom_ops.rbac import AccessDenied
from ecom_ops.research import get_research_service, reset_research_service
from ecom_ops.shop_intel import (
    clear_shop_intel_config_cache,
    get_shop_intel_service,
    reset_shop_intel_service,
)
from ecom_ops.telemetry import Telemetry
from ecom_ops.wp_ops.agent_tools import WpAgentTools
from ecom_ops.wp_ops.playbooks import PLAYBOOK_TOPICS, get_playbook, select_playbooks
from ecom_ops.wp_ops.service import WpOpsService
from ecom_ops.wp_ops.tools.registry import (
    ALL_FILE_TOOLS,
    TOOL_CAPABILITIES,
    TOOL_REGISTRY,
    UNTRUSTED_OUTPUT_TOOLS,
    WP_TOOL_NAMES,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_REGISTRY = REPO_ROOT / "config" / "wp_sites.mock.yaml"


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AZOM_SHOP_INTEL_INJECT", raising=False)
    monkeypatch.delenv("AZOM_SHOP_INTEL_KILL", raising=False)
    monkeypatch.delenv("AZOM_SHOP_INTEL_LIVE_ENABLED", raising=False)
    reset_shop_intel_service()
    clear_shop_intel_config_cache()
    reset_research_service()
    yield
    reset_shop_intel_service()
    clear_shop_intel_config_cache()
    reset_research_service()


def _make_tools(tmp_path: Path) -> WpAgentTools:
    svc = WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    return WpAgentTools(service=svc)


# --------------------------------------------------------------------------- #
# 1. Registry, Playbooks & Prompt Sync
# --------------------------------------------------------------------------- #


def test_registry_playbooks_and_prompts_include_shop_intel() -> None:
    assert len(TOOL_REGISTRY) == 81
    expected_tools = {
        "wp_shop_intel_run",
        "wp_shop_intel_status",
        "wp_shop_intel_query",
        "wp_shop_intel_security_audit",
    }
    assert expected_tools.issubset(TOOL_REGISTRY)
    assert set(TOOL_CAPABILITIES["shop_intel"]) == expected_tools
    assert expected_tools.issubset(ALL_FILE_TOOLS)
    assert {
        "wp_shop_intel_status",
        "wp_shop_intel_query",
        "wp_shop_intel_security_audit",
    }.issubset(WP_TOOL_NAMES)
    assert "wp_shop_intel_run" not in WP_TOOL_NAMES
    assert expected_tools.issubset(UNTRUSTED_OUTPUT_TOOLS)

    caps_lines = format_wp_capabilities_lines()
    for tool_name in expected_tools:
        assert tool_name in caps_lines

    assert len(PLAYBOOK_TOPICS) == 22
    assert "shop_intel_learning" in PLAYBOOK_TOPICS
    assert "shop_intel_security_drift" in PLAYBOOK_TOPICS
    pb_learn = get_playbook("shop_intel_learning")
    pb_sec = get_playbook("shop_intel_security_drift")
    assert pb_learn["ok"] is True
    assert pb_sec["ok"] is True
    assert select_playbooks("Kör butiksanalys för fraktregler och betalmetoder")[0]["topic"] == "shop_intel_learning"
    assert select_playbooks("Visa säkerhetspoäng och driftvarning för plugins")[0]["topic"] == "shop_intel_security_drift"

    prompts_yaml = yaml.safe_load((REPO_ROOT / "config" / "prompts.yaml").read_text(encoding="utf-8"))
    assert prompts_yaml["chat"]["version"] == "1.7"
    assert prompts_yaml["wp_coder"]["version"] == "1.6"
    for key in _PROMPT_NAMES:
        assert str(prompts_yaml[key]["system"]).strip() == str(_BUILTIN[key]["system"]).strip()
    chat_sys, chat_ver = get_prompt("chat")
    coder_sys, coder_ver = get_prompt("wp_coder")
    assert chat_ver == "1.7"
    assert coder_ver == "1.6"
    assert "wp_shop_intel_" in chat_sys and "SHOP_INTEL_CONTEXT" in chat_sys
    assert "wp_shop_intel_" in coder_sys and "SHOP_INTEL_CONTEXT" in coder_sys


# --------------------------------------------------------------------------- #
# 2. WP Agent Tools + RBAC
# --------------------------------------------------------------------------- #


def test_wp_shop_intel_tools_end_to_end_and_rbac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _make_tools(tmp_path)

    # Before any snapshot exists, query/security report NoSnapshot cleanly
    no_snap = tools.execute(
        "wp_shop_intel_query",
        {"site_id": "mock-shop", "domain": "platform_rules"},
        actor="jonatan",
    )
    assert no_snap.data["ok"] is False
    assert no_snap.data["error_class"] == "NoSnapshot"

    st_empty = tools.execute("wp_shop_intel_status", {"site_id": "mock-shop"}, actor="jonatan")
    assert st_empty.data["ok"] is True
    assert st_empty.data["status"] == "no_snapshot"
    assert st_empty.data["latest_run"] is None

    # Jonatan (viewer with WP_OPS_READ + RESEARCH_RUN) can run & read
    run_res = tools.execute(
        "wp_shop_intel_run",
        {"site_id": "mock-shop", "mode": "full"},
        actor="jonatan",
    )
    assert run_res.data["ok"] is True
    assert run_res.data["run_id"].startswith("si-")
    assert run_res.data["provenance"] == "mock"
    assert run_res.data["facts_count"] > 0
    assert run_res.data["dashboard_path"].startswith("/shop-intel/runs/si-")

    # Status after run
    st_res = tools.execute("wp_shop_intel_status", {"site_id": "mock-shop"}, actor="jonatan")
    assert st_res.data["ok"] is True
    assert st_res.data["status"] == "ok"
    assert st_res.data["latest_run"]["run_id"] == run_res.data["run_id"]

    # Query with domain + purpose
    q_res = tools.execute(
        "wp_shop_intel_query",
        {
            "site_id": "mock-shop",
            "domain": "platform_rules",
            "purpose": "support_draft",
        },
        actor="jonatan",
    )
    assert q_res.data["ok"] is True
    assert q_res.data["facts"]
    assert "purpose=support_draft" in (q_res.data.get("context") or "")
    assert "site_id=mock-shop" in (q_res.data.get("context") or "")

    # Second run triggers security_audit with diffs/findings
    run2 = tools.execute(
        "wp_shop_intel_run",
        {"site_id": "mock-shop", "mode": "security"},
        actor="oscar",
    )
    assert run2.data["ok"] is True

    sec_res = tools.execute(
        "wp_shop_intel_security_audit",
        {"site_id": "mock-shop"},
        actor="jonatan",
    )
    assert sec_res.data["ok"] is True
    assert isinstance(sec_res.data["security_score"], int)
    assert isinstance(sec_res.data["findings"], list)
    assert isinstance(sec_res.data["diffs"], list)

    # Validation errors
    bad_arg = tools.execute(
        "wp_shop_intel_query",
        {"site_id": "mock-shop", "domain": "invalid_domain"},
        actor="jonatan",
    )
    assert bad_arg.data["ok"] is False
    assert bad_arg.data["error_class"] == "InvalidToolArguments"

    # Unknown / denied actor fails closed
    monkeypatch.setattr(
        "ecom_ops.wp_ops.tools.shop_intel.resolve_actor",
        lambda _a: (_ for _ in ()).throw(AccessDenied("denied")),
    )
    denied_run = tools.execute("wp_shop_intel_run", {"site_id": "mock-shop"}, actor="intruder")
    assert denied_run.data["ok"] is False
    assert denied_run.data["error_class"] == "AccessDenied"


# --------------------------------------------------------------------------- #
# 3. wp_turn & chat_agent context injection and intent routing
# --------------------------------------------------------------------------- #


def test_wp_turn_and_chat_agent_routing_and_context_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1) No snapshot -> append_wp_context_blocks is a pure no-op
    turn = prepare_wp_turn(
        user_message="Kör butiksanalys och visa fraktregler för mock-shop",
        session={},
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:101",
        image_paths=None,
        clarification_context="",
        extract_order_id=lambda _t: None,
        cases_intent_re=chat_agent.CASES_INTENT_RE,
        marketing_intent_re=EXPLICIT_ADS_RE,
    )
    assert turn.has_shop_intel_intent is True
    assert turn.has_wp_drift_intent is True
    assert turn.prompt_mode == "chat_wp_drift"
    assert turn.site_id == "mock-shop"
    assert "SHOP_INTEL_CONTEXT" not in append_wp_context_blocks("base", turn)

    guidance = append_wp_guidance_blocks(
        "",
        turn,
        user_message="Visa säkerhetspoäng och driftvarningar för mock-shop",
        clarification_context="",
    )
    assert "shop_intel_" in guidance

    # Must NOT hijack order/case/SEO/ads intents
    for non_si_msg in (
        "Status på order 1001",
        "Visa öppna ärenden i kön",
        "Kör SEO-audit för mock-shop",
        "Hur går Google Ads och ROAS?",
    ):
        t_other = prepare_wp_turn(
            user_message=non_si_msg,
            session={},
            actor="jonatan",
            channel="telegram",
            conversation_id="telegram:101",
            image_paths=None,
            clarification_context="",
            extract_order_id=chat_agent._extract_order_id,
            cases_intent_re=chat_agent.CASES_INTENT_RE,
            marketing_intent_re=EXPLICIT_ADS_RE,
        )
        assert t_other.has_shop_intel_intent is False

    # _use_read_tool_loop does not hijack shop intel queries into Woo order loop
    assert (
        chat_agent._use_read_tool_loop(
            "Vilka fraktregler och betalmetoder har vår butik?",
            chat_agent.ToolPrefetch(),
        )
        is False
    )

    # 2) Create a snapshot -> append_wp_context_blocks & run_chat inject SHOP_INTEL_CONTEXT
    si_svc = get_shop_intel_service(data_dir=tmp_path)
    si_svc.run_learning("mock-shop", actor="jonatan", mode="full")

    injected_drift = append_wp_context_blocks("base", turn)
    assert "SHOP_INTEL_CONTEXT" in injected_drift
    assert "purpose=chat_general" in injected_drift

    file_turn = prepare_wp_turn(
        user_message="Uppdatera functions.php i mock-shop",
        session={},
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:101",
        image_paths=None,
        clarification_context="",
        extract_order_id=lambda _t: None,
        cases_intent_re=chat_agent.CASES_INTENT_RE,
        marketing_intent_re=EXPLICIT_ADS_RE,
    )
    injected_file = append_wp_context_blocks("base", file_turn)
    assert "SHOP_INTEL_CONTEXT" in injected_file
    assert "purpose=wp_coder" in injected_file

    # Respect AZOM_SHOP_INTEL_INJECT=0
    monkeypatch.setenv("AZOM_SHOP_INTEL_INJECT", "0")
    assert "SHOP_INTEL_CONTEXT" not in append_wp_context_blocks("base", turn)
    monkeypatch.delenv("AZOM_SHOP_INTEL_INJECT", raising=False)

    # Full run_chat test: verifies default-site injection when no site_id was in message
    captured_messages: list[list[dict[str, Any]]] = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        chat_agent,
        "chat_completion",
        lambda msgs, **_kw: (captured_messages.append(msgs) or "Klar.", 0.0),
    )
    monkeypatch.setattr(chat_agent, "SEO_INTENT_RE", SEO_INTENT_RE)

    res = chat_agent.run_chat(
        "Vilka betalmetoder och returregler har vi i vår butik?",
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:101",
        telemetry=Telemetry(path=tmp_path / "tel.jsonl"),
    )
    assert res.text == "Klar."
    assert captured_messages
    sys_contents = "\n".join(
        str(m.get("content") or "") for m in captured_messages[0] if m.get("role") == "system"
    )
    assert "SHOP_INTEL_CONTEXT" in sys_contents


# --------------------------------------------------------------------------- #
# 4. Support & Cases Drafting context injection
# --------------------------------------------------------------------------- #


def test_support_and_cases_drafting_context_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tel = Telemetry(path=tmp_path / "tel.jsonl")
    esc = EscalationService(store_path=tmp_path / "esc.jsonl")
    seen_order_ctxs: list[str | None] = []

    monkeypatch.setattr(
        "ecom_ops.actions.support.classify_with_llm",
        lambda **_kw: None,
    )
    monkeypatch.setattr(
        "ecom_ops.actions.support.draft_support_with_llm",
        lambda **kw: (seen_order_ctxs.append(kw.get("order_context")) or "Hej! Svar från LLM."),
    )

    svc = SupportService(telemetry=tel, escalation=esc)

    # 1) Without snapshot: order_context passed to LLM has NO SHOP_INTEL_CONTEXT
    res_no_snap = svc.handle(
        "Hej, vad gäller för retur på order 1001?",
        language="sv",
        site="azom",
        domain="se",
        actor="agent",
        use_mock=True,
        allow_llm=True,
    )
    assert res_no_snap.ok is True
    assert len(seen_order_ctxs) == 1
    assert "SHOP_INTEL_CONTEXT" not in (seen_order_ctxs[0] or "")
    assert "1001" in (seen_order_ctxs[0] or "")

    # 2) With snapshot: order_context includes real order facts + SHOP_INTEL_CONTEXT
    get_shop_intel_service(data_dir=tmp_path).run_learning("mock-shop", actor="jonatan", mode="full")

    res_with_snap = svc.handle(
        "Hej, vad gäller för retur på order 1001?",
        language="sv",
        site="azom",
        domain="se",
        actor="agent",
        use_mock=True,
        allow_llm=True,
    )
    assert res_with_snap.ok is True
    assert len(seen_order_ctxs) == 2
    assert "1001" in (seen_order_ctxs[1] or "")
    assert "SHOP_INTEL_CONTEXT" in (seen_order_ctxs[1] or "")
    assert "purpose=support_draft" in (seen_order_ctxs[1] or "")

    # Template fallback (allow_llm=False) is NOT modified by Shop Intel
    res_tmpl = svc.handle(
        "Hej, vad gäller för retur på order 1001?",
        language="sv",
        site="azom",
        domain="se",
        actor="agent",
        use_mock=True,
        allow_llm=False,
    )
    assert res_tmpl.ok is True
    assert "SHOP_INTEL_CONTEXT" not in (res_tmpl.reply or "")

    # Cases regenerate_draft with research brief also appends SHOP_INTEL_CONTEXT
    case_store = CaseStore(path=tmp_path / "cases.db")
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="Returfråga",
        from_addr="kund@example.com",
        body="Hej, jag vill returnera min order 1001.",
        category="return",
        draft_reply="Gammalt utkast",
        order_id="1001",
        message_id="<retur-shop-intel@x>",
    )
    from ecom_ops.research.engine import ResearchEngine

    res_svc = get_research_service()
    brief_job = res_svc.start_research(
        "konsumentköp ångerrätt 14 dagar",
        actor="agent",
        depth="quick",
        target_ref=f"case:{case.id}",
        start_worker=False,
    )
    rid = brief_job.research_id
    ResearchEngine(store=res_svc.store, telemetry=res_svc.telemetry).run_job(rid)

    drafting_ctxs: list[str | None] = []
    monkeypatch.setattr(
        "ecom_ops.cases.drafting.llm.draft_support_with_llm",
        lambda **kw: (drafting_ctxs.append(kw.get("order_context")) or "Hej! Uppdaterat utkast."),
    )
    mail = MailService(
        client=MailClient(
            config=MailConfig(
                provider=MailProvider.GENERIC_IMAP,
                username="mock@azom.se",
                from_addr="support@azom.se",
            ),
            transport=InMemoryMailTransport(),
        )
    )
    case_svc = CaseService(store=case_store, mail=mail, telemetry=tel, escalation=esc)
    regen = regenerate_draft(
        case_svc,
        case.id,
        actor="agent",
        use_mock=True,
        research_brief_id=rid,
    )
    assert regen.ok is True
    assert drafting_ctxs
    assert "RESEARCH_BRIEF" in (drafting_ctxs[0] or "")
    assert "SHOP_INTEL_CONTEXT" in (drafting_ctxs[0] or "")


# --------------------------------------------------------------------------- #
# 5. Product Description context injection
# --------------------------------------------------------------------------- #


def test_product_desc_context_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    woo = WooCommerceClient(base_url="https://mock.local", transport=InMemoryWooTransport())
    tel = Telemetry(path=tmp_path / "tel.jsonl")
    esc = EscalationService(store_path=tmp_path / "esc.jsonl")
    seen_features: list[str] = []

    monkeypatch.setattr(
        "ecom_ops.llm.generate_product_desc_with_llm",
        lambda **kw: (
            seen_features.append(str(kw.get("features") or ""))
            or ("Kort beskrivning", "<p>Lång beskrivning</p>")
        ),
    )

    svc = ProductDescService(woo=woo, telemetry=tel, escalation=esc)

    # 1) Without snapshot -> no SHOP_INTEL_CONTEXT
    res_no_snap = svc.generate(
        name="Azom CarPlay Box",
        features="Trådlös CarPlay",
        language="sv",
        site="azom",
        domain="se",
        actor="agent",
        allow_llm=True,
    )
    assert res_no_snap.ok is True
    assert len(seen_features) == 1
    assert "SHOP_INTEL_CONTEXT" not in seen_features[0]

    # 2) With snapshot + allow_llm=True -> SHOP_INTEL_CONTEXT appended with no-spec-invention guard
    get_shop_intel_service(data_dir=tmp_path).run_learning("mock-shop", actor="jonatan", mode="full")

    res_with_snap = svc.generate(
        name="Azom CarPlay Box",
        features="Trådlös CarPlay",
        language="sv",
        site="azom",
        domain="se",
        actor="agent",
        allow_llm=True,
    )
    assert res_with_snap.ok is True
    assert len(seen_features) == 2
    assert "Trådlös CarPlay" in seen_features[1]
    assert "SHOP_INTEL_CONTEXT" in seen_features[1]
    assert "purpose=product_copy" in seen_features[1]
    assert "never invent specs" in seen_features[1]

    # 3) allow_llm=False does not call LLM or alter template output
    res_tmpl = svc.generate(
        name="Azom CarPlay Box",
        features="Trådlös CarPlay",
        language="sv",
        site="azom",
        domain="se",
        actor="agent",
        allow_llm=False,
    )
    assert res_tmpl.ok is True
    assert "SHOP_INTEL_CONTEXT" not in (res_tmpl.description or "")
    assert len(seen_features) == 2
