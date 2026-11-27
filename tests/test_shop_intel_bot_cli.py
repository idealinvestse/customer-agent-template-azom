"""Tests for Wave 2 Shop Intelligence: dashboard links, Bot commands, CLI, and ops_gc."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ecom_ops.bot.dashboard_links import shop_intel_url
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.store import ConversationStore
from ecom_ops.cli import main as cli_main
from ecom_ops.ops_gc import prune_shop_intel_runs, run_ops_gc
from ecom_ops.rbac import ROLE_PERMISSIONS, Actor, Permission
from ecom_ops.shop_intel import (
    IntelDiff,
    IntelFact,
    IntelRun,
    SecurityFinding,
    clear_shop_intel_config_cache,
    get_shop_intel_service,
    reset_shop_intel_service,
)


@pytest.fixture(autouse=True)
def _reset_shop_intel_state():
    reset_shop_intel_service()
    clear_shop_intel_config_cache()
    yield
    reset_shop_intel_service()
    clear_shop_intel_config_cache()


# --------------------------------------------------------------------------- #
# 1. Dashboard deep links
# --------------------------------------------------------------------------- #


def test_shop_intel_url_requires_strict_https_and_valid_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZOM_DASHBOARD_PUBLIC_URL", raising=False)
    assert shop_intel_url("mock-shop") is None

    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "http://ops.azom.se")
    assert shop_intel_url("mock-shop") is None

    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.azom.se/")
    assert shop_intel_url() == "https://ops.azom.se/shop-intel"
    assert shop_intel_url("mock-shop") == "https://ops.azom.se/shop-intel?site_id=mock-shop"
    assert (
        shop_intel_url("mock-shop", run_id="si-abcdef012345")
        == "https://ops.azom.se/shop-intel/runs/si-abcdef012345?site_id=mock-shop"
    )
    assert (
        shop_intel_url(run_id="si-abcdef012345")
        == "https://ops.azom.se/shop-intel/runs/si-abcdef012345"
    )

    assert shop_intel_url("../evil") is None
    assert shop_intel_url("mock_shop") is None
    assert shop_intel_url("mock-shop", run_id="si-nothex") is None
    assert shop_intel_url("mock-shop", run_id="wr-abcdef012345") is None


# --------------------------------------------------------------------------- #
# 2. Bot /shop-intel & /store-learn (Telegram + Messenger)
# --------------------------------------------------------------------------- #


def _make_telegram_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BotHandler:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.azom.se")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "101,102,103")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "101:jonatan,102:oscar,103:agent")
    return BotHandler(store=ConversationStore(path=tmp_path / "tg_conv.json"), channel="telegram")


def _make_messenger_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BotHandler:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.azom.se")
    monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", "psid-j,psid-o")
    monkeypatch.setenv("MESSENGER_ACTOR_MAP", "psid-j:jonatan,psid-o:oscar")
    return BotHandler(store=ConversationStore(path=tmp_path / "ms_conv.json"), channel="messenger")


def test_bot_shop_intel_telegram_commands_and_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _make_telegram_handler(tmp_path, monkeypatch)

    # /help & /commands mention /shop-intel
    help_oc = handler.handle(101, "/help")
    assert "/shop-intel" in help_oc.text
    cmds_oc = handler.handle(101, "/commands")
    assert "/shop-intel" in cmds_oc.text

    # Bare /shop-intel and /store-learn help
    bare = handler.handle(101, "/shop-intel")
    assert "Webshop Intelligence" in bare.text
    assert "/shop-intel run" in bare.text
    assert "https://ops.azom.se/shop-intel" in bare.text
    assert bare.actions is not None
    assert all(btn.url and btn.payload is None for row in bare.actions.rows for btn in row)

    help_alias = handler.handle(101, "/store-learn help")
    assert "/shop-intel status" in help_alias.text

    # /shop-intel run with progress events + URL-only buttons + mock provenance
    events: list[str] = []
    run_reply = handler.handle(101, "/shop-intel run mock-shop quick", on_progress=events.append)
    assert "Shop Intel-körning si-" in run_reply.text
    assert "läge=quick" in run_reply.text
    assert "källa=mock" in run_reply.text
    assert "read-only · zero-PII · inga sajtändringar gjorda" in run_reply.text
    assert "https://ops.azom.se/shop-intel/runs/si-" in run_reply.text
    assert run_reply.actions is not None
    assert all(btn.url and btn.payload is None for row in run_reply.actions.rows for btn in row)
    assert len(events) == 2
    assert "Shop Intel" in events[0]
    assert "klar" in events[1]

    # /store-learn alias runs full mode and generates diff/baseline
    run2 = handler.handle(103, "/store-learn run mock-shop full")
    assert "läge=full" in run2.text
    assert "källa=mock" in run2.text

    # /shop-intel status (all sites + specific site)
    st_all = handler.handle(101, "/shop-intel status")
    assert "Shop Intel status (alla sajter · läge=mock" in st_all.text
    assert "mock-shop:" in st_all.text

    st_site = handler.handle(101, "/shop-intel status mock-shop")
    assert "Shop Intel status (mock-shop · läge=mock" in st_site.text
    assert "Senaste körning: si-" in st_site.text

    # /shop-intel query (domain, keyword, and multi-word keyword without false site error)
    q_dom = handler.handle(101, "/shop-intel query mock-shop platform_rules")
    assert "Shop Intel kunskap (mock-shop" in q_dom.text
    assert "källa=mock" in q_dom.text
    assert "[platform_rules]" in q_dom.text

    q_kw = handler.handle(101, "/shop-intel query fri frakt")
    assert "Shop Intel kunskap (mock-shop" in q_kw.text
    assert "sökord=fri frakt" in q_kw.text

    # /shop-intel security & diff
    sec_rep = handler.handle(101, "/shop-intel security mock-shop")
    assert "Shop Intel säkerhet (mock-shop" in sec_rep.text
    assert "Säkerhetspoäng:" in sec_rep.text
    assert "källa=mock" in sec_rep.text

    diff_rep = handler.handle(101, "/shop-intel diff mock-shop")
    assert "Shop Intel drift (mock-shop · källa=mock)" in diff_rep.text

    # Unknown explicit site fails closed
    bad_site = handler.handle(101, "/shop-intel run unknown-site")
    assert "Okänd sajt" in bad_site.text


def test_bot_shop_intel_messenger_parity_and_rbac_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ms_handler = _make_messenger_handler(tmp_path, monkeypatch)

    # Unmapped/disallowed Messenger PSID is denied fail-closed
    denied_peer = ms_handler.handle("psid-evil", "/shop-intel status")
    assert "inte behörig" in denied_peer.text or "Åtkomst" in denied_peer.text

    # Allowed Messenger peer can run & query with URL-only ActionMarkup
    ms_run = ms_handler.handle("psid-j", "/store-learn run mock-shop full")
    assert "Shop Intel-körning si-" in ms_run.text
    assert ms_run.actions is not None
    assert all(btn.url and btn.payload is None for row in ms_run.actions.rows for btn in row)

    ms_sec = ms_handler.handle("psid-j", "/shop-intel security mock-shop")
    assert "Shop Intel säkerhet (mock-shop" in ms_sec.text

    # Role with only READ (lacking RESEARCH_RUN / WP_OPS_OPERATE) is denied on run, allowed on status
    monkeypatch.setitem(ROLE_PERMISSIONS, "readonly_test", frozenset({Permission.READ}))
    monkeypatch.setattr(
        "ecom_ops.bot.shop_intel_handlers.resolve_actor",
        lambda _name: Actor(name="readonly-guest", role="readonly_test"),
    )
    monkeypatch.setattr(
        "ecom_ops.shop_intel.service.resolve_actor",
        lambda _name: Actor(name="readonly-guest", role="readonly_test"),
    )
    ro_run = ms_handler.handle("psid-j", "/shop-intel run mock-shop")
    assert "Åtkomst nekad" in ro_run.text
    ro_status = ms_handler.handle("psid-j", "/shop-intel status mock-shop")
    assert "Shop Intel status" in ro_status.text


def test_bot_shop_intel_live_gate_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _make_telegram_handler(tmp_path, monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("AZOM_SHOP_INTEL_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("AZOM_SHOP_INTEL_KILL", raising=False)

    events: list[str] = []
    rep = handler.handle(102, "/shop-intel run azom-no", on_progress=events.append)
    assert "AZOM_SHOP_INTEL_LIVE_ENABLED=1" in rep.text
    assert len(events) == 2
    assert "fel" in events[1] or "misslyck" in events[1] or "⚠️" in events[1]

    monkeypatch.setenv("AZOM_SHOP_INTEL_LIVE_ENABLED", "1")
    monkeypatch.setenv("AZOM_SHOP_INTEL_KILL", "1")
    kill_rep = handler.handle(102, "/shop-intel run azom-no")
    assert "AZOM_SHOP_INTEL_KILL" in kill_rep.text


# --------------------------------------------------------------------------- #
# 3. CLI shop-intel subcommands
# --------------------------------------------------------------------------- #


def test_cli_shop_intel_all_subcommands_and_rbac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    # 1. run
    assert (
        cli_main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "shop-intel",
                "run",
                "--site",
                "mock-shop",
                "--mode",
                "full",
                "--domains",
                "platform_rules,policies_legal,security_posture",
            ]
        )
        == 0
    )
    run_out = json.loads(capsys.readouterr().out)
    assert run_out["ok"] is True
    assert run_out["run"]["site_id"] == "mock-shop"
    assert run_out["run"]["provenance"] == "mock"
    assert run_out["run"]["run_id"].startswith("si-")

    # 2. status (with and without --site, plus global --site)
    assert cli_main(["--mock", "--actor", "jonatan", "shop-intel", "status"]) == 0
    st_all = json.loads(capsys.readouterr().out)
    assert st_all["ok"] is True and st_all["gates"]["mock_mode"] is True

    assert (
        cli_main(["--mock", "--actor", "jonatan", "--site", "mock-shop", "shop-intel", "status"])
        == 0
    )
    st_one = json.loads(capsys.readouterr().out)
    assert st_one["ok"] is True and st_one["site_id"] == "mock-shop"

    # 3. query
    assert (
        cli_main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "shop-intel",
                "query",
                "--site",
                "mock-shop",
                "--domain",
                "platform_rules",
                "--purpose",
                "support_draft",
            ]
        )
        == 0
    )
    q_out = json.loads(capsys.readouterr().out)
    assert q_out["ok"] is True
    assert q_out["facts"]
    assert "SHOP_INTEL_CONTEXT" in (q_out["context"] or "")

    # 4. security & diff
    assert cli_main(["--mock", "--actor", "jonatan", "shop-intel", "security", "--site", "mock-shop"]) == 0
    sec_out = json.loads(capsys.readouterr().out)
    assert sec_out["ok"] is True and "security_score" in sec_out

    assert cli_main(["--mock", "--actor", "jonatan", "shop-intel", "diff", "--site", "mock-shop", "--limit", "5"]) == 0
    diff_out = json.loads(capsys.readouterr().out)
    assert diff_out["ok"] is True and isinstance(diff_out["diffs"], list)

    # 5. context
    assert (
        cli_main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "shop-intel",
                "context",
                "--site",
                "mock-shop",
                "--purpose",
                "support_draft",
                "--max-chars",
                "900",
            ]
        )
        == 0
    )
    ctx_out = json.loads(capsys.readouterr().out)
    assert ctx_out["ok"] is True and ctx_out["present"] is True
    assert "SHOP_INTEL_CONTEXT" in ctx_out["context"]

    # 6. export (json & md)
    assert cli_main(["--mock", "--actor", "jonatan", "shop-intel", "export", "--site", "mock-shop", "--format", "json"]) == 0
    exp_json = json.loads(capsys.readouterr().out)
    assert exp_json["ok"] is True and exp_json["format"] == "json"

    assert cli_main(["--mock", "--actor", "jonatan", "shop-intel", "export", "--site", "mock-shop", "--format", "md"]) == 0
    exp_md = json.loads(capsys.readouterr().out)
    assert exp_md["ok"] is True and exp_md["format"] == "md" and "mock-shop" in exp_md["content"]

    # 7. schedule-set RBAC: jonatan denied, oscar allowed; then schedule-run
    assert (
        cli_main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "shop-intel",
                "schedule-set",
                "--site",
                "mock-shop",
                "--interval-hours",
                "12",
                "--mode",
                "security",
                "--enabled",
            ]
        )
        == 1
    )
    denied_sched = json.loads(capsys.readouterr().out)
    assert denied_sched["ok"] is False and denied_sched["error"] == "access_denied"

    assert (
        cli_main(
            [
                "--mock",
                "--actor",
                "oscar",
                "shop-intel",
                "schedule-set",
                "--site",
                "mock-shop",
                "--interval-hours",
                "12",
                "--mode",
                "security",
                "--enabled",
            ]
        )
        == 0
    )
    set_ok = json.loads(capsys.readouterr().out)
    assert set_ok["ok"] is True and set_ok["schedule"]["enabled"] is True

    # Force schedule due in DB so schedule-run executes it
    svc = get_shop_intel_service(tmp_path)
    with sqlite3.connect(svc.store.db_path) as conn:
        conn.execute(
            "UPDATE intel_schedules SET next_run_at = ? WHERE site_id = ?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), "mock-shop"),
        )
    assert cli_main(["--mock", "--actor", "agent", "shop-intel", "schedule-run", "--site", "mock-shop"]) == 0
    srun_out = json.loads(capsys.readouterr().out)
    assert srun_out["ok"] is True and srun_out["executed_count"] == 1

    # Disable schedule
    assert (
        cli_main(
            [
                "--mock",
                "--actor",
                "oscar",
                "shop-intel",
                "schedule-set",
                "--site",
                "mock-shop",
                "--disabled",
            ]
        )
        == 0
    )
    dis_out = json.loads(capsys.readouterr().out)
    assert dis_out["ok"] is True and dis_out["schedule"]["enabled"] is False


# --------------------------------------------------------------------------- #
# 4. ops_gc Shop Intel pruning (dry-run, execute, baseline preservation)
# --------------------------------------------------------------------------- #


def _seed_run(
    svc,
    *,
    run_id: str,
    site_id: str,
    created_at: datetime,
    prev_run_id: str | None = None,
) -> None:
    ts = created_at.astimezone(UTC).isoformat()
    run = IntelRun(
        run_id=run_id,
        site_id=site_id,
        mode="full",
        provenance="mock",
        actor="agent",
        domains_collected=["platform_rules"],
        status="completed",
        security_score=95,
        findings_count=1,
        diffs_count=1 if prev_run_id else 0,
        created_at=ts,
        dossier={"platform_rules": {"currency": "SEK"}},
    )
    facts = [
        IntelFact(
            domain="platform_rules",
            key="currency",
            value="SEK",
            source="mock",
            observed_at=ts,
        )
    ]
    findings = [
        SecurityFinding(
            finding_id=f"sif-{run_id[-12:]}",
            run_id=run_id,
            site_id=site_id,
            severity="low",
            category="posture",
            code="test_code",
            title="Test finding",
            detail="Test detail",
            created_at=ts,
        )
    ]
    diffs = (
        [
            IntelDiff(
                diff_id=f"sid-{run_id[-12:]}",
                site_id=site_id,
                prev_run_id=prev_run_id,
                curr_run_id=run_id,
                severity="low",
                domain="platform_rules",
                category="change",
                summary="Test diff",
                created_at=ts,
            )
        ]
        if prev_run_id
        else []
    )
    svc.store.save_run(run, facts=facts, diffs=diffs, findings=findings)


def test_ops_gc_prunes_shop_intel_runs_dry_run_and_keeps_latest_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")

    # Missing DB does not fail or create DB
    assert prune_shop_intel_runs(tmp_path, dry_run=True) == (0, 0)
    assert not (tmp_path / "shop_intel.sqlite3").exists()

    svc = get_shop_intel_service(tmp_path)
    now = datetime.now(UTC)

    # site-a: 1 fresh latest run + 2 old expired runs (>90d)
    _seed_run(svc, run_id="si-aaaaaaaa0001", site_id="site-a", created_at=now - timedelta(days=120))
    _seed_run(
        svc,
        run_id="si-aaaaaaaa0002",
        site_id="site-a",
        created_at=now - timedelta(days=100),
        prev_run_id="si-aaaaaaaa0001",
    )
    _seed_run(
        svc,
        run_id="si-aaaaaaaa0003",
        site_id="site-a",
        created_at=now - timedelta(hours=2),
        prev_run_id="si-aaaaaaaa0002",
    )

    # site-b: ONLY 1 run, 150 days old -> MUST be preserved as latest baseline!
    _seed_run(svc, run_id="si-bbbbbbbb0001", site_id="site-b", created_at=now - timedelta(days=150))

    # Dry-run via run_ops_gc reports exact count & bytes without mutating DB
    dry = run_ops_gc(data_dir=tmp_path, dry_run=True)
    assert dry["ok"] is True
    assert dry["dry_run"] is True
    assert dry["purged_shop_intel_runs"] == 2
    assert dry["bytes_reclaimed"] > 0
    assert len(svc.store.list_runs("site-a", limit=50)) == 3
    assert len(svc.store.list_runs("site-b", limit=50)) == 1

    # Live execute removes only the 2 old non-latest runs from site-a and keeps both baselines
    live = run_ops_gc(data_dir=tmp_path, dry_run=False)
    assert live["ok"] is True
    assert live["dry_run"] is False
    assert live["purged_shop_intel_runs"] == 2
    assert live["bytes_reclaimed"] > 0
    assert [r["run_id"] for r in svc.store.list_runs("site-a", limit=50)] == ["si-aaaaaaaa0003"]
    assert [r["run_id"] for r in svc.store.list_runs("site-b", limit=50)] == ["si-bbbbbbbb0001"]

    # Related facts/findings/diffs for deleted runs are cleaned too
    assert svc.store.list_facts(run_id="si-aaaaaaaa0001") == []
    assert svc.store.list_facts(run_id="si-aaaaaaaa0003") != []
    assert svc.store.list_diffs("site-a", run_id="si-aaaaaaaa0002") == []


def test_ops_gc_prune_shop_intel_respects_max_runs_and_refuses_symlink(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    data_dir.mkdir()
    outside.mkdir()

    # Symlink protection
    target_db = outside / "shop_intel.sqlite3"
    target_db.write_bytes(b"not-a-db")
    link_db = data_dir / "shop_intel.sqlite3"
    try:
        os.symlink(target_db, link_db)
    except OSError:
        pass
    else:
        assert prune_shop_intel_runs(data_dir, dry_run=False) == (0, 0)
        assert target_db.read_bytes() == b"not-a-db"
        link_db.unlink()

    svc = get_shop_intel_service(data_dir)
    now = datetime.now(UTC)
    for i in range(1, 5):
        _seed_run(
            svc,
            run_id=f"si-cccccccc000{i}",
            site_id="site-c",
            created_at=now - timedelta(hours=5 - i),
        )
    purged, reclaimed = prune_shop_intel_runs(data_dir, dry_run=False, max_runs=2, retention_days=90)
    assert purged == 2
    assert reclaimed > 0
    assert [r["run_id"] for r in svc.store.list_runs("site-c", limit=20)] == [
        "si-cccccccc0004",
        "si-cccccccc0003",
    ]
