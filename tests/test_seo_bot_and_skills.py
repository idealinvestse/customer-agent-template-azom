"""Tests for SEO Studio bot commands, NL routing & misroute fixes, WP SEO tools, playbooks, and prompt budgets."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ecom_ops.bot.change_clarifier import detect_change_ambiguity
from ecom_ops.bot.chat_agent import (
    _build_system_prompt,
    gather_tool_results,
    tool_capabilities,
)
from ecom_ops.bot.dashboard_links import (
    changes_url,
    seo_dashboard_url,
    seo_digest_url,
    seo_parity_url,
    seo_plan_url,
    seo_vendors_url,
    wp_plugins_url,
)
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.openclaw_commands import (
    COMMANDS,
    TELEGRAM_MENU_COMMANDS,
    CommandContext,
    cmd_skill,
    cmd_tools,
)
from ecom_ops.bot.seo_handlers import EXPLICIT_ADS_RE, SEO_INTENT_RE, SEO_META_EDIT_RE
from ecom_ops.bot.store import ConversationStore
from ecom_ops.bot.wp_turn import append_wp_guidance_blocks, prepare_wp_turn
from ecom_ops.prompts import _BUILTIN, _PROMPT_NAMES, get_prompt
from ecom_ops.research.clarifier import build_research_spec
from ecom_ops.wp_ops.agent_tools import WP_TOOL_GUIDE, WpAgentTools
from ecom_ops.wp_ops.playbooks import PLAYBOOK_TOPICS, select_playbooks
from ecom_ops.wp_ops.service import WpOpsService

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_REGISTRY = REPO_ROOT / "config" / "wp_sites.mock.yaml"


def _make_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actor: str = "jonatan") -> BotHandler:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "101")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", f"101:{actor}")
    return BotHandler(store=ConversationStore(path=tmp_path / "bot_state.json"), channel="telegram")


def test_seo_slash_commands_registered_and_dashboard_links(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cmd_names = {c.name for c in COMMANDS}
    assert "seo" in cmd_names
    assert "seo" in {c["command"] for c in TELEGRAM_MENU_COMMANDS}

    ctx = CommandContext(chat_id=101, text="/tools", args="verbose", store=ConversationStore(path=tmp_path / "s.json"))
    tools_out = cmd_tools(ctx)
    assert "wp_seo_audit" in tools_out
    assert "wp_seo_autopilot" in tools_out
    assert "/seo scan" in tools_out
    assert "autopilot" in tools_out
    assert "vendors" in tools_out
    skill_out = cmd_skill(CommandContext(chat_id=101, text="/skill wordpress-seo", args="wordpress-seo", store=ctx.store))
    assert "wordpress-seo" in skill_out
    assert "wp_seo_autopilot" in skill_out
    assert "vendors|autopilot" in skill_out

    caps = tool_capabilities()
    for t in (
        "wp_seo_audit",
        "wp_seo_plan_meta",
        "wp_seo_batch_plan",
        "wp_seo_drift_digest",
        "wp_seo_strategy",
        "wp_seo_campaign",
        "wp_seo_link_package",
        "wp_seo_autopilot",
    ):
        assert t in caps

    # Strict HTTPS dashboard links
    monkeypatch.delenv("AZOM_DASHBOARD_PUBLIC_URL", raising=False)
    assert seo_dashboard_url() is None
    assert seo_plan_url("seom-0123456789ab") is None
    assert seo_parity_url("mock-shop") is None
    assert seo_digest_url("mock-shop") is None
    assert seo_vendors_url("no") is None
    assert wp_plugins_url("mock-shop") is None
    assert changes_url("mock-shop", category="seo_meta") is None

    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "http://insecure.example")
    assert seo_dashboard_url() is None
    assert seo_plan_url("seom-0123456789ab") is None
    assert seo_parity_url("mock-shop") is None
    assert seo_digest_url("mock-shop") is None
    assert seo_vendors_url("no") is None

    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.azom.se")
    assert seo_dashboard_url() == "https://ops.azom.se/seo"
    assert seo_dashboard_url("mock-shop") == "https://ops.azom.se/seo?site_id=mock-shop"
    assert seo_dashboard_url(section="link-packages") == "https://ops.azom.se/seo/link-packages"
    assert seo_dashboard_url(section="parity") == "https://ops.azom.se/seo/parity"
    assert seo_dashboard_url(section="digest") == "https://ops.azom.se/seo/digest"
    assert seo_dashboard_url(section="vendors") == "https://ops.azom.se/seo/vendors"
    assert seo_vendors_url() == "https://ops.azom.se/seo/vendors"
    assert seo_vendors_url("no") == "https://ops.azom.se/seo/vendors?market=no"
    assert seo_vendors_url("SE") == "https://ops.azom.se/seo/vendors?market=se"
    assert seo_vendors_url("invalid") is None
    assert seo_parity_url("mock-shop") == "https://ops.azom.se/seo/parity?site_id=mock-shop"
    assert seo_digest_url("mock-shop") == "https://ops.azom.se/seo/digest?site_id=mock-shop"
    assert wp_plugins_url("mock-shop") == "https://ops.azom.se/wp/mock-shop/plugins"
    assert (
        changes_url("mock-shop", category="seo_meta")
        == "https://ops.azom.se/wp/mock-shop/changes?category=seo_meta"
    )
    assert (
        changes_url("mock-shop", batch_id="seob-0123456789ab")
        == "https://ops.azom.se/wp/mock-shop/changes?batch_id=seob-0123456789ab"
    )
    assert seo_dashboard_url("bad site!") is None
    assert seo_plan_url("seoa-0123456789ab") == "https://ops.azom.se/seo/audit/seoa-0123456789ab"
    assert seo_plan_url("seol-0123456789ab") == "https://ops.azom.se/seo/link-packages/seol-0123456789ab"
    assert seo_plan_url("seos-0123456789ab") == "https://ops.azom.se/seo/strategies?id=seos-0123456789ab"
    assert seo_plan_url("seoc-0123456789ab") == "https://ops.azom.se/seo/campaigns?id=seoc-0123456789ab"
    assert seo_plan_url("seom-0123456789ab") == "https://ops.azom.se/seo?plan_id=seom-0123456789ab"
    assert seo_plan_url("seob-0123456789ab") == "https://ops.azom.se/seo?batch_id=seob-0123456789ab"
    assert seo_plan_url("invalid-123") is None


def test_misroute_fixes_research_chat_agent_clarifier_and_wp_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    # 1. research/clarifier.py routes SEO queries to seo_content_radar BEFORE marketing_intel
    spec_seo = build_research_spec("SEO sökordsanalys för Volvo CarPlay", [], [])
    assert spec_seo.profile_id == "seo_content_radar"

    spec_schema = build_research_spec("Strukturerad data och hreflang för bilstereo", [], [])
    assert spec_schema.profile_id == "seo_content_radar"

    spec_ads = build_research_spec("Google Ads ROAS och kampanjoptimering", [], [])
    assert spec_ads.profile_id == "marketing_intel"

    # 2. chat_agent.py does NOT hijack SEO queries with Ads-only marketing_snapshot
    assert SEO_INTENT_RE.search("Skapa en SEO-kampanj för Volvo CarPlay")
    assert not EXPLICIT_ADS_RE.search("Skapa en SEO-kampanj för Volvo CarPlay")
    assert SEO_META_EDIT_RE.search("Ändra SEO-titel på produkt 501 till CarPlay Volvo | Azom")

    pref_seo_campaign = gather_tool_results("Skapa en SEO-kampanj för Volvo CarPlay")
    assert not any(name == "marketing_snapshot" for name, _ in pref_seo_campaign.results)

    pref_organic = gather_tool_results("Hur förbättrar vi organisk trafik med SEO?")
    assert not any(name == "marketing_snapshot" for name, _ in pref_organic.results)

    # Explicit Google Ads + SEO still fetches marketing snapshot
    pref_both = gather_tool_results("Jämför SEO och Google Ads ROAS senaste veckan")
    assert any(name == "marketing_snapshot" for name, _ in pref_both.results)

    # SEO meta edits bypass catalog_write / site_content hijack
    pref_meta = gather_tool_results("Ändra SEO-titel på produkt 501 till CarPlay Volvo | Azom")
    assert pref_meta.pending_action is None
    assert not any(name in {"catalog_write", "site_content"} for name, _ in pref_meta.results)

    # 3. change_clarifier.py exempts bild-SEO / alt-text from screenshot false positives
    media_assess = detect_change_ambiguity("Uppdatera bild-SEO och alt-text på produktbilder")
    assert media_assess.mentions_screenshot_or_annotation is False

    shot_assess = detect_change_ambiguity("Ändra det jag ringat in på bilden")
    assert shot_assess.mentions_screenshot_or_annotation is True

    # 4. wp_turn.py routes SEO_INTENT_RE to chat_wp_file and injects SEO playbook hints
    turn = prepare_wp_turn(
        user_message="Gör en SEO-audit och kolla länkpaket för azom.no",
        session={},
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:101",
        image_paths=None,
        clarification_context="",
        extract_order_id=lambda _t: None,
        cases_intent_re=SEO_INTENT_RE,  # unused when no prior active review
        marketing_intent_re=EXPLICIT_ADS_RE,
    )
    assert turn.prompt_mode == "chat_wp_file"
    assert turn.default_profile_id == "wp_tool_router"

    guidance = append_wp_guidance_blocks(
        "",
        turn,
        user_message="Gör en SEO-audit och bygg ett länkpaket med naturlig ankartextmix",
        clarification_context="",
    )
    assert "seo_link_packages" in guidance or "seo_technical_and_onpage" in guidance


def test_prompt_sync_and_budget_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("AZOM_WEB_SEARCH_KILL", raising=False)

    prompts_yaml = yaml.safe_load((REPO_ROOT / "config" / "prompts.yaml").read_text(encoding="utf-8"))
    assert len(prompts_yaml) == 7
    assert set(prompts_yaml) == set(_BUILTIN) == set(_PROMPT_NAMES)
    assert prompts_yaml["chat"]["version"] == "1.6"
    assert prompts_yaml["wp_coder"]["version"] == "1.5"
    for key in _PROMPT_NAMES:
        assert str(prompts_yaml[key]["system"]).strip() == str(_BUILTIN[key]["system"]).strip()
        sys_txt, ver = get_prompt(key)
        assert sys_txt.strip() == str(prompts_yaml[key]["system"]).strip()
        assert ver == str(prompts_yaml[key]["version"])

    assert len(WP_TOOL_GUIDE) <= 7000
    full_wp_prompt, ver = _build_system_prompt(mode="chat_wp_file", include_web_research=True)
    assert ver == "1.5"
    assert len(full_wp_prompt) < 12986
    assert "wp_seo_audit" in full_wp_prompt
    assert "wp_seo_batch_plan" in full_wp_prompt
    assert "wp_seo_drift_digest" in full_wp_prompt
    assert "wp_seo_link_package" in full_wp_prompt
    assert "wp_seo_autopilot" in full_wp_prompt
    assert "vendors" in full_wp_prompt
    assert len(PLAYBOOK_TOPICS) == 20
    assert select_playbooks("länkpaket ankartext")[0]["topic"] == "seo_link_packages"


def test_bot_seo_slash_commands_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.azom.se")
    handler = _make_handler(tmp_path, monkeypatch, actor="jonatan")

    help_reply = handler.handle(101, "/seo")
    assert "SEO Studio" in help_reply.text
    assert "/seo scan" in help_reply.text

    scan_reply = handler.handle(101, "/seo scan mock-shop")
    assert "seoa-" in scan_reply.text
    assert "Poäng:" in scan_reply.text
    assert "https://ops.azom.se/seo/audit/seoa-" in scan_reply.text

    prod_reply = handler.handle(101, "/seo product 501")
    assert "scope=product" in prod_reply.text

    page_reply = handler.handle(101, "/seo page 2")
    assert "scope=page" in page_reply.text

    media_reply = handler.handle(101, "/seo media mock-shop")
    assert "scope=media" in media_reply.text

    strat_reply = handler.handle(101, "/seo strategy Nordisk CarPlay 2026")
    assert "seos-" in strat_reply.text

    camp_reply = handler.handle(101, "/seo campaign CarPlay Volvo Q2")
    assert "seoc-" in camp_reply.text

    links_reply = handler.handle(101, "/seo links Volvo CarPlay 18000kr 6st")
    assert "seol-" in links_reply.text
    assert "Inga externa köp görs automatiskt" in links_reply.text

    # Extract seol id and inspect with /seo plan <id>
    seol_id = next(tok for tok in links_reply.text.split() if tok.startswith("seol-"))
    plan_detail = handler.handle(101, f"/seo plan {seol_id}")
    assert seol_id in plan_detail.text
    assert "Guardrails:" in plan_detail.text

    plans_reply = handler.handle(101, "/seo plans mock-shop")
    assert "SEO Studio-översikt" in plans_reply.text
    assert seol_id in plans_reply.text

    # Operator (agent) lacks MARKETING_SUGGEST so /seo links is denied
    agent_handler = _make_handler(tmp_path, monkeypatch, actor="agent")
    denied_links = agent_handler.handle(101, "/seo links Test 10000 4")
    assert "Åtkomst nekad" in denied_links.text


def test_wp_seo_agent_tools_rbac_and_preview_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    service = WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    tools = WpAgentTools(service=service)

    # 1. wp_seo_audit allowed for jonatan, agent, oscar
    for actor in ("jonatan", "agent", "oscar"):
        res = tools.execute("wp_seo_audit", {"site_id": "mock-staging", "scope": "site"}, actor=actor)
        assert res.data["ok"] is True, (actor, res.data)
        assert str(res.data["audit_id"]).startswith("seoa-")
        assert res.data["dashboard_path"].startswith("/seo/audit/seoa-")

    # 2. wp_seo_plan_meta RBAC: viewer denied; agent allowed staging, denied prod; oscar allowed both, NEVER executes
    v_plan = tools.execute(
        "wp_seo_plan_meta",
        {
            "site_id": "mock-staging",
            "fix_type": "seo_meta",
            "target_type": "product",
            "target_id": 501,
            "proposed": {"meta_title": "CarPlay Volvo | Azom"},
        },
        actor="jonatan",
    )
    assert v_plan.data["ok"] is False and v_plan.data["error_class"] == "AccessDenied"

    op_staging = tools.execute(
        "wp_seo_plan_meta",
        {
            "site_id": "mock-staging",
            "fix_type": "media_alt",
            "target_type": "media",
            "target_id": 101,
            "proposed": {"alt_text": "Azom CarPlay trådlös adapter för Volvo"},
            "summary": "Skriv beskrivande alt-text",
        },
        actor="agent",
    )
    assert op_staging.data["ok"] is True, op_staging.data
    assert str(op_staging.data["plan_id"]).startswith("seom-")
    assert op_staging.data["executed"] is False
    assert op_staging.data["approval_required"] is True

    op_prod = tools.execute(
        "wp_seo_plan_meta",
        {
            "site_id": "mock-shop",
            "fix_type": "seo_meta",
            "target_type": "product",
            "target_id": 501,
            "proposed": {"meta_title": "CarPlay Volvo | Azom"},
        },
        actor="agent",
    )
    assert op_prod.data["ok"] is False and op_prod.data["error_class"] == "AccessDenied"

    osc_prod = tools.execute(
        "wp_seo_plan_meta",
        {
            "site_id": "mock-shop",
            "fix_type": "seo_meta",
            "target_type": "product",
            "target_id": 501,
            "proposed": {"meta_title": "CarPlay Volvo | Azom"},
        },
        actor="oscar",
    )
    assert osc_prod.data["ok"] is True and osc_prod.data["executed"] is False
    assert osc_prod.data["approval_required"] is True

    # 3. wp_seo_strategy & wp_seo_campaign
    strat = tools.execute(
        "wp_seo_strategy",
        {"site_id": "mock-shop", "title": "Nordisk CarPlay 2026", "market": "no", "focus_topics": ["CarPlay", "DAB+"]},
        actor="jonatan",
    )
    assert strat.data["ok"] is True and str(strat.data["strategy_id"]).startswith("seos-")

    camp = tools.execute(
        "wp_seo_campaign",
        {
            "site_id": "mock-shop",
            "name": "Volvo CarPlay Q2",
            "strategy_id": strat.data["strategy_id"],
            "target_keywords": ["carplay volvo", "trådlös carplay"],
        },
        actor="jonatan",
    )
    assert camp.data["ok"] is True and str(camp.data["campaign_id"]).startswith("seoc-")

    # 4. wp_seo_link_package: jonatan/oscar allowed (never orders); agent denied (lacks MARKETING_SUGGEST)
    pkg_denied = tools.execute(
        "wp_seo_link_package",
        {"site_id": "mock-shop", "name": "NO Q2 Länkpaket", "budget_sek": 15000, "link_count": 6},
        actor="agent",
    )
    assert pkg_denied.data["ok"] is False and pkg_denied.data["error_class"] == "AccessDenied"

    pkg_ok = tools.execute(
        "wp_seo_link_package",
        {
            "site_id": "mock-shop",
            "name": "NO Q2 Länkpaket",
            "campaign_id": camp.data["campaign_id"],
            "budget_sek": 15000,
            "link_count": 6,
        },
        actor="jonatan",
    )
    assert pkg_ok.data["ok"] is True, pkg_ok.data
    assert str(pkg_ok.data["package_id"]).startswith("seol-")
    assert pkg_ok.data["executed"] is False
    assert pkg_ok.data["approval_required"] is True
    assert pkg_ok.data["external_order_placed"] is False


def test_seo_stage2_regressions_t1_01_04_08_09_10_11_12_13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecom_ops.bot.change_clarifier import build_fallback_clarification
    from ecom_ops.bot.handlers import _slash_command_meta
    from ecom_ops.bot.seo_handlers import _parse_links_args
    from ecom_ops.seo.service import SeoStudioService
    from ecom_ops.wp_ops.tools.seo import _scrub_obj

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    # T1-01 + T1-04: wp_seo_* fail closed on unregistered sites and service errors (no TypeError)
    service = WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    tools = WpAgentTools(service=service)
    for tool_name, args, actor in (
        ("wp_seo_audit", {"site_id": "no-such-site", "scope": "site"}, "oscar"),
        (
            "wp_seo_plan_meta",
            {
                "site_id": "prod-unregistered",
                "fix_type": "seo_meta",
                "target_type": "product",
                "target_id": 501,
                "proposed": {"meta_title": "Test"},
            },
            "agent",
        ),
        ("wp_seo_strategy", {"site_id": "no-such-site", "title": "Test"}, "jonatan"),
        ("wp_seo_campaign", {"site_id": "no-such-site", "name": "Test"}, "jonatan"),
        ("wp_seo_link_package", {"site_id": "no-such-site", "name": "Test"}, "jonatan"),
    ):
        out = tools.execute(tool_name, args, actor=actor)
        assert out.data["ok"] is False, (tool_name, out.data)
        assert out.data["error_class"] == "UnknownSiteOrDenied", (tool_name, out.data)

    # Service-level error dict with bad error_class is sanitized cleanly
    monkeypatch.setattr(
        SeoStudioService,
        "run_audit",
        lambda *_a, **_kw: {"ok": False, "error_class": "bad class with spaces!"},
    )
    err_out = tools.execute("wp_seo_audit", {"site_id": "mock-staging"}, actor="oscar")
    assert err_out.data["ok"] is False
    assert err_out.data["error_class"] == "SeoAuditFailed"
    monkeypatch.undo()
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    # T1-08: bare Swedish "schema" no longer triggers SEO_META_EDIT_RE drop zone
    assert not SEO_META_EDIT_RE.search("Ändra sidan 2 text till Nytt schema för support")
    assert SEO_META_EDIT_RE.search("Uppdatera schema.org på produkt 501")
    assert SEO_META_EDIT_RE.search("Lägg till schema-markup och strukturerad data")
    pref_page = gather_tool_results("Ändra sidan 2 text till Nytt schema för support")
    assert pref_page.pending_action is not None
    assert pref_page.pending_action.kind == "page_update"

    # T1-09: _parse_links_args preserves years & model numbers and parses space-formatted SEK
    assert _parse_links_args("CarPlay Q2 2026 15000kr 8") == ("CarPlay Q2 2026", 15000.0, 8)
    assert _parse_links_args("CarPlay 15 000 kr 6 länkar") == ("CarPlay", 15000.0, 6)
    assert _parse_links_args("iOS 18 CarPlay guide") == ("iOS 18 CarPlay guide", 15000.0, 6)
    assert _parse_links_args("BMW 5 serie CarPlay 20000kr") == ("BMW 5 serie CarPlay", 20000.0, 6)
    assert _parse_links_args("CarPlay 2026") == ("CarPlay 2026", 15000.0, 6)
    assert _parse_links_args("CarPlay budget=25000 count=10") == ("CarPlay", 25000.0, 10)
    assert _parse_links_args("Test 10000 4") == ("Test", 10000.0, 4)

    # T1-10: SEO drafts & meta/alt-text requests are not misclassified as store/page mutations
    for seo_phrase in (
        "Skapa en SEO-strategi för Norge",
        "Skapa en SEO-kampanj för CarPlay i Norge",
        "Skapa ett länkpaket för CarPlay på azom.no",
        "Förbättra SEO-titel och metabeskrivning för produkt 501",
        "Uppdatera alt-text på produktbilder på mock-shop",
    ):
        assess = detect_change_ambiguity(seo_phrase)
        assert assess.is_ambiguous is False, (seo_phrase, assess)
        assert assess.category == "seo", (seo_phrase, assess)
    vague_seo = detect_change_ambiguity("Ändra SEO")
    fb = build_fallback_clarification("Ändra SEO", vague_seo)
    assert fb.category == "seo"
    assert all("Pris eller lagerstatus" not in opt.label for opt in fb.options)

    # T1-11: /seo rejects unknown explicit site and checks res["ok"]; dead /seo meta branch removed
    assert _slash_command_meta("/seo scan") is None
    handler = _make_handler(tmp_path, monkeypatch, actor="jonatan")
    bad_site = handler.handle(101, "/seo scan typo-site")
    assert "Okänd sajt: typo-site" in bad_site.text

    from ecom_ops.bot.progress_events import mark_event_consumer

    progress_phases: list[str] = []
    monkeypatch.setattr(
        SeoStudioService,
        "run_audit",
        lambda *_a, **_kw: {"ok": False, "error": "Bridge otillgänglig"},
    )
    fail_rep = handler.handle(
        101,
        "/seo scan mock-shop",
        on_progress=mark_event_consumer(lambda ev: progress_phases.append(ev.phase)),
    )
    assert "0/100" not in fail_rep.text
    assert "Bridge otillgänglig" in fail_rep.text
    assert "failed" in progress_phases

    # T1-12: _scrub_obj redacts secrets even at depth >= 4 and caps deep nesting
    secret_tok = "sk_live_" + "DeepSeoSecret998877"
    deep_obj = {"a": {"b": {"c": {"d": f"api_key={secret_tok}", "e": {"f": {"g": secret_tok}}}}}}
    scrubbed = _scrub_obj(tools, None, deep_obj)
    assert secret_tok not in str(scrubbed)

    # T1-13: Exact-match threshold wording aligned to <= 10% (warn > 10%, block > 20%)
    skill_md = (REPO_ROOT / "skills" / "wordpress-seo" / "SKILL.md").read_text(encoding="utf-8")
    doc_md = (REPO_ROOT / "docs" / "SEO_STUDIO.md").read_text(encoding="utf-8")
    assert "<= 15%" not in skill_md and "<= 10%" in skill_md
    assert "<= 15%" not in doc_md and "<= 10%" in doc_md


def test_p16_seo_agent_tools_and_bot_subcommands_rbac_and_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.azom.se")

    service = WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    tools = WpAgentTools(service=service)

    # 1. Extended wp_seo_audit scopes (site + diff, gsc, parity, deep_crawl)
    a1 = tools.execute("wp_seo_audit", {"site_id": "mock-staging", "scope": "site"}, actor="jonatan")
    assert a1.data["ok"] is True, a1.data
    audit_id = str(a1.data["audit_id"])
    assert audit_id.startswith("seoa-")

    a2 = tools.execute(
        "wp_seo_audit",
        {"site_id": "mock-staging", "scope": "site", "include_diff": True},
        actor="jonatan",
    )
    assert a2.data["ok"] is True and isinstance(a2.data.get("diff"), dict), a2.data
    assert a2.data["diff"]["has_previous"] is True

    gsc = tools.execute("wp_seo_audit", {"site_id": "mock-shop", "scope": "gsc"}, actor="jonatan")
    assert gsc.data["ok"] is True and gsc.data["scope"] == "gsc", gsc.data
    assert "strike_distance" in gsc.data and "gsc_status" in gsc.data

    par = tools.execute("wp_seo_audit", {"site_id": "mock-shop", "scope": "parity"}, actor="jonatan")
    assert par.data["ok"] is True and par.data["scope"] == "parity", par.data
    assert par.data["dashboard_path"] == "/seo/parity?site_id=mock-shop"

    dc = tools.execute(
        "wp_seo_audit",
        {"site_id": "mock-shop", "scope": "deep_crawl", "max_pages": 5},
        actor="jonatan",
    )
    assert dc.data["ok"] is True and dc.data["scope"] == "deep_crawl", dc.data

    # 2. wp_seo_batch_plan RBAC & fail-closed preview-only
    b_unreg = tools.execute(
        "wp_seo_batch_plan",
        {"site_id": "no-such-site", "audit_id": audit_id},
        actor="oscar",
    )
    assert b_unreg.data["ok"] is False and b_unreg.data["error_class"] == "UnknownSiteOrDenied"

    b_viewer = tools.execute(
        "wp_seo_batch_plan",
        {"site_id": "mock-staging", "audit_id": audit_id},
        actor="jonatan",
    )
    assert b_viewer.data["ok"] is False and b_viewer.data["error_class"] == "AccessDenied"

    b_agent_prod = tools.execute(
        "wp_seo_batch_plan",
        {"site_id": "mock-shop", "audit_id": audit_id},
        actor="agent",
    )
    assert b_agent_prod.data["ok"] is False and b_agent_prod.data["error_class"] == "AccessDenied"

    b_agent_stg = tools.execute(
        "wp_seo_batch_plan",
        {"site_id": "mock-staging", "audit_id": audit_id, "scope": "all"},
        actor="agent",
    )
    assert b_agent_stg.data["ok"] is True, b_agent_stg.data
    assert str(b_agent_stg.data["batch_id"]).startswith("seob-")
    assert b_agent_stg.data["executed"] is False
    assert b_agent_stg.data["approval_required"] is True

    # 3. wp_seo_drift_digest RBAC & fail-closed
    d_unreg = tools.execute("wp_seo_drift_digest", {"site_id": "no-such-site"}, actor="jonatan")
    assert d_unreg.data["ok"] is False and d_unreg.data["error_class"] == "UnknownSiteOrDenied"

    d_ok = tools.execute("wp_seo_drift_digest", {"site_id": "mock-shop", "days": 7}, actor="jonatan")
    assert d_ok.data["ok"] is True, d_ok.data
    assert d_ok.data["dashboard_path"] == "/seo/digest?site_id=mock-shop"
    assert "plugin_summary" in d_ok.data and "seo_trend" in d_ok.data

    # 4. wp_seo_link_package verify & cumulative modes (read-only)
    pkg = tools.execute(
        "wp_seo_link_package",
        {"site_id": "mock-shop", "name": "Verify Pack", "budget_sek": 12000, "link_count": 4},
        actor="jonatan",
    )
    assert pkg.data["ok"] is True
    pkg_id = str(pkg.data["package_id"])

    cum = tools.execute(
        "wp_seo_link_package",
        {"site_id": "mock-shop", "mode": "cumulative", "package_id": pkg_id},
        actor="agent",
    )
    assert cum.data["ok"] is True and cum.data["mode"] == "cumulative", cum.data
    assert cum.data["external_order_placed"] is False

    ver = tools.execute(
        "wp_seo_link_package",
        {"site_id": "mock-shop", "mode": "verify", "package_id": pkg_id},
        actor="jonatan",
    )
    assert ver.data["ok"] is True and ver.data["mode"] == "verify", ver.data
    assert ver.data["package_id"] == pkg_id

    # 5. Bot /seo subcommands + channel-neutral read-only navigation cards
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "101,102,103")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "101:jonatan,102:oscar,103:agent")
    bot_handler = BotHandler(store=ConversationStore(path=tmp_path / "bot_state.json"), channel="telegram")

    scan_rep = bot_handler.handle(102, "/seo scan mock-shop")
    shop_audit_id = next(tok for tok in scan_rep.text.split() if tok.startswith("seoa-"))
    assert "Öppna i SEO Studio" in scan_rep.text
    assert "Granska i Change Vault" in scan_rep.text
    assert scan_rep.actions is not None
    # Strictly URL-only navigation buttons — never mutating postback payloads
    for row in scan_rep.actions.rows:
        for btn in row:
            assert btn.url and btn.payload is None

    # /seo batch RBAC: jonatan (101) denied, agent (103) denied on prod, oscar (102) succeeds preview-only
    assert "Åtkomst nekad" in bot_handler.handle(101, f"/seo batch mock-shop {shop_audit_id}").text
    assert "Åtkomst nekad" in bot_handler.handle(103, f"/seo batch mock-shop {shop_audit_id}").text
    batch_rep = bot_handler.handle(102, f"/seo batch mock-shop {shop_audit_id} media_alt")
    assert "seob-" in batch_rep.text
    assert "executed=False" in batch_rep.text
    assert "Öppna i SEO Studio" in batch_rep.text
    assert "Granska i Change Vault" in batch_rep.text
    seob_id = next(tok for tok in batch_rep.text.split() if tok.startswith("seob-"))
    assert seob_id in bot_handler.handle(102, f"/seo plan {seob_id}").text

    # /seo gsc, parity, diff, digest, verify
    gsc_rep = bot_handler.handle(101, "/seo gsc mock-shop")
    assert "GSC quick-wins" in gsc_rep.text and "Öppna i SEO Studio" in gsc_rep.text

    par_rep = bot_handler.handle(101, "/seo parity mock-shop")
    assert "Nordisk SEO-paritet" in par_rep.text and "/seo/parity" in par_rep.text

    diff_rep = bot_handler.handle(101, "/seo diff mock-shop")
    assert "SEO-audit diff" in diff_rep.text

    dig_rep = bot_handler.handle(101, "/seo digest mock-shop")
    assert "Veckodigest" in dig_rep.text
    assert "Öppna i SEO Studio" in dig_rep.text
    assert "Granska i Change Vault" in dig_rep.text
    assert "Öppna /wp/mock-shop/plugins" in dig_rep.text
    assert dig_rep.actions is not None
    assert len(dig_rep.actions.rows) == 3

    ver_rep = bot_handler.handle(101, f"/seo verify {pkg_id}")
    assert "Länkverifiering" in ver_rep.text and pkg_id in ver_rep.text


def test_seo_autopilot_and_vendors_tools_bot_and_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.azom.se")

    # 1. NL SEO_INTENT_RE matches new vendor/autopilot phrases without customer-support false positives
    for positive in (
        "Vilka länkleverantörer rekommenderar du för Norge?",
        "Jämför nordiska länkshoppar för 15000 kr",
        "Hitta bästa SEO-butiker i Sverige",
        "Kör seo-autopilot för Trådlös CarPlay Volvo",
        "Starta autonom SEO för azom.no",
    ):
        assert SEO_INTENT_RE.search(positive), f"Expected SEO_INTENT_RE match for: {positive}"

    for non_seo in (
        "Var är min order 1001?",
        "Kunden vill returnera en vara till butiken",
        "Vilken leverantör fraktar paketet med PostNord?",
        "Kan du skicka returfraktsedel till kunden?",
        "Uppdatera lagret för produkt 501",
    ):
        assert not SEO_INTENT_RE.search(non_seo), f"Unexpected SEO_INTENT_RE false positive for: {non_seo}"

    # 2. WP agent tools: wp_seo_link_package(mode="vendors") & wp_seo_autopilot
    service = WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    tools = WpAgentTools(service=service)

    # Unregistered site fails closed
    v_unreg = tools.execute(
        "wp_seo_link_package",
        {"site_id": "no-such-site", "mode": "vendors", "market": "no"},
        actor="jonatan",
    )
    assert v_unreg.data["ok"] is False and v_unreg.data["error_class"] == "UnknownSiteOrDenied"

    a_unreg = tools.execute(
        "wp_seo_autopilot",
        {"site_id": "no-such-site", "topic": "CarPlay Volvo"},
        actor="jonatan",
    )
    assert a_unreg.data["ok"] is False and a_unreg.data["error_class"] == "UnknownSiteOrDenied"

    # wp_seo_link_package(mode="vendors") allowed for jonatan, agent, oscar (read-only)
    for actor in ("jonatan", "agent", "oscar"):
        vres = tools.execute(
            "wp_seo_link_package",
            {"site_id": "mock-shop", "mode": "vendors", "market": "no", "budget_sek": 15000, "link_count": 6},
            actor=actor,
        )
        assert vres.data["ok"] is True, (actor, vres.data)
        assert vres.data["mode"] == "vendors"
        assert vres.data["executed"] is False
        assert vres.data["external_order_placed"] is False
        assert vres.data["dashboard_path"] == "/seo/vendors?market=no"
        assert 2 <= len(vres.data["candidates"]) <= 3
        assert vres.data["selected_vendor_id"]

    # wp_seo_autopilot RBAC: agent denied (no MARKETING_SUGGEST/MARKETING_MUTATE); jonatan denied if include_batch_fixes=True
    a_agent = tools.execute(
        "wp_seo_autopilot",
        {"site_id": "mock-shop", "topic": "Trådlös CarPlay Volvo", "budget_sek": 15000, "link_count": 6},
        actor="agent",
    )
    assert a_agent.data["ok"] is False and a_agent.data["error_class"] == "AccessDenied"

    a_viewer_batch = tools.execute(
        "wp_seo_autopilot",
        {
            "site_id": "mock-shop",
            "topic": "Trådlös CarPlay Volvo",
            "include_batch_fixes": True,
        },
        actor="jonatan",
    )
    assert a_viewer_batch.data["ok"] is False and a_viewer_batch.data["error_class"] == "AccessDenied"

    # jonatan & oscar succeed on wp_seo_autopilot with strict HITL guarantees
    a_ok = tools.execute(
        "wp_seo_autopilot",
        {
            "site_id": "mock-shop",
            "topic": "Trådlös CarPlay Volvo & BMW",
            "market": "no",
            "budget_sek": 18000,
            "link_count": 6,
        },
        actor="jonatan",
    )
    assert a_ok.data["ok"] is True, a_ok.data
    assert a_ok.data["executed"] is False
    assert a_ok.data["external_order_placed"] is False
    assert a_ok.data["approval_required"] is True
    assert str(a_ok.data["strategy_id"]).startswith("seos-")
    assert str(a_ok.data["campaign_id"]).startswith("seoc-")
    assert str(a_ok.data["package_id"]).startswith("seol-")
    assert a_ok.data["selected_vendor_id"]
    assert a_ok.data["dashboard_path"] == f"/seo/link-packages/{a_ok.data['package_id']}"
    assert a_ok.data["vendors_path"] == "/seo/vendors?market=no"

    # 3. Bot /seo vendors & /seo autopilot commands + URL-only navigation cards
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "101,102,103")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "101:jonatan,102:oscar,103:agent")
    bot_handler = BotHandler(store=ConversationStore(path=tmp_path / "bot_state.json"), channel="telegram")

    help_rep = bot_handler.handle(101, "/seo")
    assert "/seo vendors" in help_rep.text
    assert "/seo autopilot" in help_rep.text

    vend_rep = bot_handler.handle(101, "/seo vendors no 15000 6")
    assert "Kvalitetssäkrade nordiska SEO-butiker" in vend_rep.text
    assert "https://ops.azom.se/seo/vendors?market=no" in vend_rep.text
    assert vend_rep.actions is not None
    for row in vend_rep.actions.rows:
        for btn in row:
            assert btn.url and btn.payload is None

    # Agent denied on /seo autopilot
    ap_denied = bot_handler.handle(103, "/seo autopilot mock-shop CarPlay Volvo 15000 6")
    assert "Åtkomst nekad" in ap_denied.text

    ap_rep = bot_handler.handle(101, "/seo autopilot mock-shop Trådlös CarPlay Volvo 18000kr 6st")
    assert "SEO-autopilot klar" in ap_rep.text
    assert "seos-" in ap_rep.text
    assert "seoc-" in ap_rep.text
    assert "seol-" in ap_rep.text
    assert "Vald SEO-butik:" in ap_rep.text
    assert "executed=False" in ap_rep.text
    assert "external_order_placed=False" in ap_rep.text
    assert "https://ops.azom.se/seo/link-packages/seol-" in ap_rep.text
    assert "https://ops.azom.se/seo/vendors?market=no" in ap_rep.text
    assert ap_rep.actions is not None
    for row in ap_rep.actions.rows:
        for btn in row:
            assert btn.url and btn.payload is None

    # 4. Zero banned network/shell tokens anywhere under skills/ecom_ops/wp_ops/
    banned_tokens = (
        "requests",
        "urlopen",
        "http.client",
        "urllib.request",
        "subprocess",
        "os.system",
        "integrations.wordpress",
    )
    for py_file in sorted((REPO_ROOT / "skills" / "ecom_ops" / "wp_ops").rglob("*.py")):
        src_text = py_file.read_text(encoding="utf-8")
        for tok in banned_tokens:
            assert tok not in src_text, f"Banned token {tok!r} found in {py_file}"
