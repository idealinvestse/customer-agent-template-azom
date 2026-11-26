"""Tests for Feature C3 (ops_gc) and Feature C4 (runtime_health, CLI, /ops, bot wiring)."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.messenger_adapter import InboundEvent
from ecom_ops.bot.messenger_queue import MessengerJobQueue
from ecom_ops.bot.openclaw_commands import TELEGRAM_MENU_COMMANDS, dispatch_openclaw_command
from ecom_ops.bot.store import ConversationStore
from ecom_ops.bot.telegram_transport import TelegramApiError
from ecom_ops.cli import main as cli_main
from ecom_ops.ops_gc import run_ops_gc
from ecom_ops.runtime_health import (
    collect_runtime_snapshot,
    format_ops_telegram_summary,
    record_runtime_error,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _make_plan(
    plan_id: str,
    *,
    site_id: str = "demo-site",
    status: str = "validated",
    expires_at: datetime,
) -> dict:
    return {
        "plan_id": plan_id,
        "site_id": site_id,
        "environment": "staging",
        "kind": "theme",
        "slug": "storefront",
        "work_slug": "storefront",
        "risk": "low",
        "status": status,
        "theme_relationship": "child",
        "base_sha256": "a" * 64,
        "work_sha256": "b" * 64,
        "created_at": _iso(expires_at - timedelta(hours=12)),
        "expires_at": _iso(expires_at),
        "extended": False,
        "checks": [],
        "check_run": {},
        "check_generation": 0,
        "sandbox": {"engine": "unavailable", "state": "stopped"},
        "comments": [],
        "label": "mock",
        "error_class": "",
        "superseded_by": "",
        "review_path": f"/wp/{site_id}/reviews/{plan_id}",
        "summary": "test plan",
    }


def _populate_wr_workspace(root: Path, site_id: str, plan_id: str, payload: bytes = b"x" * 2048) -> Path:
    ws = root / "wp_workspaces" / site_id / plan_id
    for sub in ("artifacts", "base", "origin", "work"):
        sub_dir = ws / sub
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "file.bin").write_bytes(payload)
    (ws / "manifest.json").write_text(json.dumps({"plan_id": plan_id}), encoding="utf-8")
    return ws


def _set_mtime(path: Path, ts: float) -> None:
    os.utime(path, (ts, ts))
    if path.is_dir():
        for child in path.rglob("*"):
            if not child.is_symlink():
                os.utime(child, (ts, ts))


# ── Feature C3: ops_gc tests ────────────────────────────────────────────────


def test_ops_gc_dry_run_vs_execute_full_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    now = datetime.now(UTC)
    now_ts = now.timestamp()
    old_ts = now_ts - 48 * 3600
    fresh_ts = now_ts - 3600

    # 1. WP review plans: active, recently expired (inside 24h grace), old expired (past 24h grace)
    plans = [
        _make_plan("wr-active000001", status="validated", expires_at=now + timedelta(hours=6)),
        _make_plan("wr-grace000002", status="validated", expires_at=now - timedelta(hours=2)),
        _make_plan("wr-oldexp00003", status="expired", expires_at=now - timedelta(hours=36)),
        _make_plan("wr-dueold00004", status="validated", expires_at=now - timedelta(hours=30)),
    ]
    (tmp_path / "wp_review_plans.json").write_text(
        json.dumps({"plans": plans}, indent=2), encoding="utf-8"
    )
    ws_active = _populate_wr_workspace(tmp_path, "demo-site", "wr-active000001")
    ws_grace = _populate_wr_workspace(tmp_path, "demo-site", "wr-grace000002")
    ws_oldexp = _populate_wr_workspace(tmp_path, "demo-site", "wr-oldexp00003")
    ws_dueold = _populate_wr_workspace(tmp_path, "demo-site", "wr-dueold00004")

    # 2. Untracked pv-* preview dirs: one old, one fresh
    pv_old = tmp_path / "wp_workspaces" / "demo-site" / "pv-old0001"
    pv_old.mkdir(parents=True)
    (pv_old / "preview.png").write_bytes(b"p" * 4096)
    _set_mtime(pv_old, old_ts)

    pv_fresh = tmp_path / "wp_workspaces" / "demo-site" / "pv-fresh02"
    pv_fresh.mkdir(parents=True)
    (pv_fresh / "preview.png").write_bytes(b"p" * 4096)
    _set_mtime(pv_fresh, fresh_ts)

    # 3. Inbound media in both Telegram and Messenger dirs (old + fresh)
    tg_old = tmp_path / "telegram_inbox_media" / "1001" / "old.png"
    tg_old.parent.mkdir(parents=True)
    tg_old.write_bytes(b"m" * 1024)
    _set_mtime(tg_old, old_ts)
    tg_fresh = tmp_path / "telegram_inbox_media" / "1001" / "fresh.png"
    tg_fresh.write_bytes(b"m" * 1024)
    _set_mtime(tg_fresh, fresh_ts)

    ms_old = tmp_path / "messenger_inbox_media" / "psid1" / "old.jpg"
    ms_old.parent.mkdir(parents=True)
    ms_old.write_bytes(b"j" * 1024)
    _set_mtime(ms_old, old_ts)
    ms_fresh = tmp_path / "messenger_inbox_media" / "psid1" / "fresh.jpg"
    ms_fresh.write_bytes(b"j" * 1024)
    _set_mtime(ms_fresh, fresh_ts)

    # 4. SQLite proposals & wp_review_pins in both telegram_jobs.sqlite3 & messenger_proposals.sqlite3
    tg_store = TelegramJobStore(tmp_path / "telegram_jobs.sqlite3")
    ms_prop_store = TelegramJobStore(tmp_path / "messenger_proposals.sqlite3")
    with sqlite3.connect(tg_store.path) as conn:
        # Expired via expires_at
        conn.execute(
            """
            INSERT INTO bot_proposals (proposal_id, chat_id, kind, payload_json, state, created_at, expires_at)
            VALUES ('pr-111111111111', '100', 'order', '{}', 'open', ?, ?)
            """,
            (now_ts - 7200, now_ts - 60),
        )
        # Expired via NULL expires_at + old created_at
        conn.execute(
            """
            INSERT INTO bot_proposals (proposal_id, chat_id, kind, payload_json, state, created_at, expires_at)
            VALUES ('pr-222222222222', '100', 'order', '{}', 'open', ?, NULL)
            """,
            (old_ts,),
        )
        # Active open proposal — MUST be preserved
        conn.execute(
            """
            INSERT INTO bot_proposals (proposal_id, chat_id, kind, payload_json, state, created_at, expires_at)
            VALUES ('pr-333333333333', '100', 'order', '{}', 'open', ?, ?)
            """,
            (now_ts, now_ts + 3600),
        )
        # Expired + active wp_review_pins
        conn.execute(
            """
            INSERT INTO wp_review_pins (chat_id, plan_id, actor, site_id, base_sha256, work_sha256, expires_at, created_at)
            VALUES ('100', 'wr-oldexp00003', 'oscar', 'demo-site', 'a', 'b', ?, ?)
            """,
            (now_ts - 10, now_ts - 3600),
        )
        conn.execute(
            """
            INSERT INTO wp_review_pins (chat_id, plan_id, actor, site_id, base_sha256, work_sha256, expires_at, created_at)
            VALUES ('100', 'wr-active000001', 'oscar', 'demo-site', 'a', 'b', ?, ?)
            """,
            (now_ts + 3600, now_ts),
        )
    with sqlite3.connect(ms_prop_store.path) as conn:
        conn.execute(
            """
            INSERT INTO bot_proposals (proposal_id, chat_id, kind, payload_json, state, created_at, expires_at)
            VALUES ('pr-444444444444', 'psid1', 'cases', '{}', 'open', ?, ?)
            """,
            (now_ts - 7200, now_ts - 30),
        )

    # 5. Oversized telemetry.jsonl & audit.jsonl (use small threshold 0.001 MB = ~1 KB)
    big_line = json.dumps({"action": "test", "msg": "x" * 200}) + "\n"
    (tmp_path / "telemetry.jsonl").write_text(big_line * 20, encoding="utf-8")
    (tmp_path / "audit.jsonl").write_text(big_line * 20, encoding="utf-8")

    # DRY RUN: counts everything, mutates NOTHING
    dry = run_ops_gc(data_dir=tmp_path, dry_run=True, rotate_max_mb=0.001)
    assert dry["ok"] is True
    assert dry["dry_run"] is True
    assert dry["expired_plans"] == 2  # wr-grace000002 + wr-dueold00004
    assert dry["removed_workspace_dirs"] == 2  # wr-oldexp00003 + wr-dueold00004
    assert dry["removed_pv_dirs"] == 1
    assert dry["removed_media_files"] == 2
    assert dry["purged_proposals"] == 4  # 2 tg proposals + 1 tg pin + 1 ms proposal
    assert set(dry["rotated_logs"]) == {"telemetry.jsonl", "audit.jsonl"}
    assert dry["bytes_reclaimed"] > 0
    assert dry["errors"] == []

    # Verify nothing was mutated during dry_run
    assert (ws_oldexp / "artifacts" / "file.bin").is_file()
    assert pv_old.is_dir()
    assert tg_old.is_file()
    assert ms_old.is_file()
    assert not (tmp_path / "telemetry.jsonl.1.gz").exists()
    assert (tmp_path / "telemetry.jsonl").stat().st_size > 2000

    # LIVE EXECUTE
    res = run_ops_gc(data_dir=tmp_path, dry_run=False, rotate_max_mb=0.001)
    assert res["ok"] is True
    assert res["dry_run"] is False
    assert res["expired_plans"] == 2
    assert res["removed_workspace_dirs"] == 2
    assert res["removed_pv_dirs"] == 1
    assert res["removed_media_files"] == 2
    assert res["purged_proposals"] == 4
    assert set(res["rotated_logs"]) == {"telemetry.jsonl", "audit.jsonl"}
    assert res["bytes_reclaimed"] > 0
    assert res["errors"] == []

    # Active & inside-grace workspaces MUST remain untouched!
    assert (ws_active / "work" / "file.bin").is_file()
    assert (ws_active / "artifacts" / "file.bin").is_file()
    assert (ws_grace / "work" / "file.bin").is_file()
    assert (ws_grace / "artifacts" / "file.bin").is_file()

    # Old expired workspaces had artifacts/base/origin/work removed (manifest preserved)
    for ws_cleaned in (ws_oldexp, ws_dueold):
        for sub in ("artifacts", "base", "origin", "work"):
            assert not (ws_cleaned / sub).exists()
        assert (ws_cleaned / "manifest.json").is_file()

    # Plans JSON updated wr-grace000002 & wr-dueold00004 to expired, left wr-active000001 active
    saved_plans = {
        p["plan_id"]: p["status"]
        for p in json.loads((tmp_path / "wp_review_plans.json").read_text(encoding="utf-8"))["plans"]
    }
    assert saved_plans["wr-active000001"] == "validated"
    assert saved_plans["wr-grace000002"] == "expired"
    assert saved_plans["wr-dueold00004"] == "expired"

    # pv-* old removed, fresh preserved
    assert not pv_old.exists()
    assert (pv_fresh / "preview.png").is_file()

    # Media old removed, fresh preserved
    assert not tg_old.exists()
    assert tg_fresh.is_file()
    assert not ms_old.exists()
    assert ms_fresh.is_file()

    # SQLite: active proposal & pin preserved, expired ones removed
    with sqlite3.connect(tg_store.path) as conn:
        rem_props = [r[0] for r in conn.execute("SELECT proposal_id FROM bot_proposals").fetchall()]
        rem_pins = [r[0] for r in conn.execute("SELECT plan_id FROM wp_review_pins").fetchall()]
    assert rem_props == ["pr-333333333333"]
    assert rem_pins == ["wr-active000001"]
    with sqlite3.connect(ms_prop_store.path) as conn:
        ms_props = [r[0] for r in conn.execute("SELECT proposal_id FROM bot_proposals").fetchall()]
    assert ms_props == []

    # Logs rotated + valid gzip + active files truncated
    for name in ("telemetry.jsonl", "audit.jsonl"):
        gz = tmp_path / f"{name}.1.gz"
        assert gz.is_file()
        assert (tmp_path / name).stat().st_size == 0
        with gzip.open(gz, "rt", encoding="utf-8") as fh:
            assert '"action": "test"' in fh.read()


def test_ops_gc_log_rotation_caps_at_three_archives(tmp_path: Path):
    log_file = tmp_path / "telemetry.jsonl"
    for idx in range(1, 6):
        log_file.write_text((f'{{"batch": {idx}, "pad": "' + "a" * 300 + '"}\n') * 10, encoding="utf-8")
        res = run_ops_gc(data_dir=tmp_path, dry_run=False, rotate_max_mb=0.001)
        assert res["rotated_logs"] == ["telemetry.jsonl"]

    archives = sorted(p.name for p in tmp_path.glob("telemetry.jsonl.*.gz"))
    assert archives == ["telemetry.jsonl.1.gz", "telemetry.jsonl.2.gz", "telemetry.jsonl.3.gz"]
    with gzip.open(tmp_path / "telemetry.jsonl.1.gz", "rt", encoding="utf-8") as fh:
        assert '"batch": 5' in fh.read()
    with gzip.open(tmp_path / "telemetry.jsonl.3.gz", "rt", encoding="utf-8") as fh:
        assert '"batch": 3' in fh.read()


def test_ops_gc_refuses_symlinks_pointing_outside_data_dir(tmp_path: Path):
    data_dir = tmp_path / "data"
    outside_dir = tmp_path / "outside"
    data_dir.mkdir()
    outside_dir.mkdir()
    secret_file = outside_dir / "keep_me.txt"
    secret_file.write_text("DO NOT DELETE", encoding="utf-8")

    # Symlink pv-* pointing outside data_dir, plus symlinked inbox media file
    site_dir = data_dir / "wp_workspaces" / "demo-site"
    site_dir.mkdir(parents=True)
    media_dir = data_dir / "telegram_inbox_media" / "1001"
    media_dir.mkdir(parents=True)

    pv_link = site_dir / "pv-escape"
    media_link = media_dir / "escape.png"
    try:
        os.symlink(outside_dir, pv_link, target_is_directory=True)
        os.symlink(secret_file, media_link)
    except OSError:
        pytest.skip("OS does not permit symlink creation without elevated privileges")

    res = run_ops_gc(
        data_dir=data_dir,
        dry_run=False,
        pv_max_age_hours=0,
        inbox_max_age_hours=0,
    )
    assert res["ok"] is True
    assert res["removed_pv_dirs"] == 0
    assert res["removed_media_files"] == 0
    assert secret_file.is_file()
    assert secret_file.read_text(encoding="utf-8") == "DO NOT DELETE"


# ── Feature C4: runtime_health tests ────────────────────────────────────────


def test_record_runtime_error_ring_cap_and_secret_scrubbing(tmp_path: Path):
    # Write 205 entries — cap must stay at 200
    for idx in range(205):
        record_runtime_error(
            "telegram_poller",
            "TimeoutError",
            detail=(
                f"seq={idx} url=https://api.telegram.org/bot123456:ABCdefGH_secretToken99/getUpdates "
                "Authorization: Bearer super_secret_bearer_token_12345 sk-proj-999999999999 "
                "xai-superSecretKey999999"
            ),
            status_code=504,
            data_dir=tmp_path,
        )

    err_path = tmp_path / "runtime_errors.jsonl"
    raw_text = err_path.read_text(encoding="utf-8")
    lines = [line for line in raw_text.splitlines() if line.strip()]
    assert len(lines) == 200

    # Zero secrets or raw URLs may appear anywhere in runtime_errors.jsonl
    for forbidden in (
        "123456:ABCdefGH_secretToken99",
        "https://api.telegram.org",
        "super_secret_bearer_token_12345",
        "sk-proj-999999999999",
        "xai-superSecretKey999999",
    ):
        assert forbidden not in raw_text

    # Oldest 5 entries dropped; newest entry is seq=204
    first = json.loads(lines[0])
    last = json.loads(lines[-1])
    assert "seq=5 " in first["detail"]
    assert "seq=204 " in last["detail"]
    assert last["status_code"] == 504


def test_collect_runtime_snapshot_and_summary_never_leak_oauth_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_THREADS", "16")
    now = datetime.now(UTC)
    now_ts = now.timestamp()

    # 1. Telegram DB with healthy poller lease + jobs + open proposal
    tg_store = TelegramJobStore(tmp_path / "telegram_jobs.sqlite3")
    assert tg_store.acquire_poller_lease(ttl_seconds=90) is True
    with sqlite3.connect(tg_store.path) as conn:
        conn.execute(
            """
            INSERT INTO bot_jobs (job_id, conversation_id, chat_id, kind, payload_json, state, sequence, created_at, updated_at)
            VALUES ('j1', '100', '100', 'read_turn', '{}', 'queued', 1, ?, ?)
            """,
            (now_ts, now_ts),
        )
        conn.execute(
            """
            INSERT INTO bot_jobs (job_id, conversation_id, chat_id, kind, payload_json, state, sequence, created_at, updated_at)
            VALUES ('j2', '100', '100', 'read_turn', '{}', 'failed', 2, ?, ?)
            """,
            (now_ts, now_ts),
        )
        conn.execute(
            """
            INSERT INTO bot_proposals (proposal_id, chat_id, kind, payload_json, state, created_at, expires_at)
            VALUES ('pr-aaaaaaaaaaaa', '100', 'order', '{}', 'open', ?, ?)
            """,
            (now_ts, now_ts + 1800),
        )

    # 2. Messenger DBs
    mq = MessengerJobQueue(tmp_path / "messenger_jobs.sqlite3")
    mq.enqueue(InboundEvent(peer_id="p1", mid="m1", text="hej"))
    ms_prop_store = TelegramJobStore(tmp_path / "messenger_proposals.sqlite3")
    with sqlite3.connect(ms_prop_store.path) as conn:
        conn.execute(
            """
            INSERT INTO bot_proposals (proposal_id, chat_id, kind, payload_json, state, created_at, expires_at)
            VALUES ('pr-bbbbbbbbbbbb', 'p1', 'order', '{}', 'open', ?, ?)
            """,
            (now_ts, now_ts + 1800),
        )

    # 3. Model router health (one open breaker)
    (tmp_path / "model_router_health.json").write_text(
        json.dumps(
            {
                "entries": {
                    "route:openrouter": {
                        "state": "open",
                        "cooldown_until": now_ts + 45,
                        "failure_count": 3,
                        "last_error_class": "ModelRateLimitError",
                        "last_error_code": "rate_limit",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    # 4. ChatGPT OAuth session file with REAL-LOOKING secret tokens (MUST NEVER LEAK!)
    oauth_dir = tmp_path / "model_oauth"
    oauth_dir.mkdir(parents=True)
    secret_access = "eyJhbGciOiJSUzI1NiIs.ACCESS_SECRET_DO_NOT_LEAK_XYZ"
    secret_refresh = "rt_REFRESH_SECRET_DO_NOT_LEAK_ABC"
    secret_id = "id_IDTOKEN_SECRET_DO_NOT_LEAK_123"
    (oauth_dir / "openai_default.json").write_text(
        json.dumps(
            {
                "provider": "openai",
                "account_id": "default",
                "status": "connected",
                "needs_reauth": False,
                "generation": 7,
                "expires_at": now_ts + 3600,
                "access_token": secret_access,
                "refresh_token": secret_refresh,
                "id_token": secret_id,
                "code_verifier": "VERIFIER_SECRET_999",
            }
        ),
        encoding="utf-8",
    )

    # 5. Disk artifacts: 1 expired plan, 1 untracked pv-* dir
    (tmp_path / "wp_review_plans.json").write_text(
        json.dumps({"plans": [_make_plan("wr-exp001", status="expired", expires_at=now - timedelta(hours=5))]}),
        encoding="utf-8",
    )
    pv_dir = tmp_path / "wp_workspaces" / "demo-site" / "pv-snap01"
    pv_dir.mkdir(parents=True)
    (pv_dir / "shot.png").write_bytes(b"z" * 4096)

    # 6. Recent runtime error
    record_runtime_error("telegram_poller", "TelegramApiError", detail="method=getUpdates", status_code=502, data_dir=tmp_path)

    snap = collect_runtime_snapshot(data_dir=tmp_path, recent_error_limit=10)
    dumped = json.dumps(snap)
    summary = format_ops_telegram_summary(snap)

    # Zero token leakage in both snapshot JSON and formatted Telegram summary
    for secret in (secret_access, secret_refresh, secret_id, "VERIFIER_SECRET_999"):
        assert secret not in dumped
        assert secret not in summary

    assert snap["poller"]["present"] is True
    assert snap["poller"]["healthy"] is True
    assert snap["poller"]["status"] == "healthy"
    assert snap["poller"]["ttl_seconds"] > 0

    assert snap["queues"]["telegram"]["queued"] == 1
    assert snap["queues"]["telegram"]["failed"] == 1
    assert snap["queues"]["telegram"]["open_proposals"] == 1
    assert snap["queues"]["messenger"]["pending"] == 1
    assert snap["queues"]["messenger"]["open_proposals"] == 1

    assert snap["model_router"]["open_breakers_count"] == 1
    assert snap["model_router"]["breakers"]["route:openrouter"]["state"] == "open"
    assert snap["model_router"]["chatgpt_oauth"]["connected"] is True
    assert snap["model_router"]["chatgpt_oauth"]["generation"] == 7
    assert snap["model_router"]["chatgpt_oauth"]["ttl_seconds"] > 3000

    assert snap["disk_usage"]["expired_plans_count"] == 1
    assert snap["disk_usage"]["untracked_pv_count"] == 1
    assert snap["disk_usage"]["total_bytes"] > 0
    assert snap["waitress"]["configured_threads"] == 16
    assert snap["recent_errors_total"] == 1
    assert snap["recent_errors"][0]["error_class"] == "TelegramApiError"

    # Swedish summary checks
    assert "Azom Runtime /ops" in summary
    assert "Poller: OK" in summary
    assert "ChatGPT OAuth: ansluten" in summary
    assert "Dashboard Waitress: 16 trådar" in summary


def test_collect_runtime_snapshot_reports_expired_poller_lease(tmp_path: Path):
    tg_store = TelegramJobStore(tmp_path / "telegram_jobs.sqlite3")
    with sqlite3.connect(tg_store.path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_poller_lease (lease_name, owner, expires_at) VALUES ('poller', 'old-host:1', ?)",
            (time.time() - 120,),
        )
    snap = collect_runtime_snapshot(data_dir=tmp_path)
    assert snap["poller"]["present"] is True
    assert snap["poller"]["healthy"] is False
    assert snap["poller"]["status"] == "expired"
    assert snap["poller"]["ttl_seconds"] == 0


# ── CLI ops gc / ops status & RBAC tests ────────────────────────────────────


def test_cli_ops_status_and_gc_rbac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    # ops status --json works read-only
    rc = cli_main(["ops", "status", "--json"])
    assert rc == 0
    snap = json.loads(capsys.readouterr().out)
    assert "poller" in snap and "queues" in snap and "disk_usage" in snap

    # ops gc --dry-run --json allowed read-only for jonatan/agent
    rc = cli_main(["--actor", "jonatan", "ops", "gc", "--dry-run", "--json"])
    assert rc == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["ok"] is True and dry["dry_run"] is True

    # Live ops gc DENIED for jonatan (viewer) and agent (operator)
    for denied_actor in ("jonatan", "agent"):
        rc = cli_main(["--actor", denied_actor, "ops", "gc", "--json"])
        assert rc == 1
        err_payload = json.loads(capsys.readouterr().out)
        assert err_payload["ok"] is False
        assert err_payload["error"] == "access_denied"

    # Live ops gc ALLOWED for oscar (full_admin)
    rc = cli_main(["--actor", "oscar", "ops", "gc", "--json"])
    assert rc == 0
    live = json.loads(capsys.readouterr().out)
    assert live["ok"] is True and live["dry_run"] is False


# ── Telegram /ops command RBAC & menu registration ──────────────────────────


def test_telegram_ops_command_oscar_only_and_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "111:oscar,222:jonatan,333:agent")

    store = ConversationStore(tmp_path / "conv.json")

    # /ops is registered in TELEGRAM_MENU_COMMANDS (consumed by bot._register_commands)
    assert any(cmd["command"] == "ops" for cmd in TELEGRAM_MENU_COMMANDS)

    # Jonatan (viewer) -> denied
    resp_j = dispatch_openclaw_command("222", "/ops", store, channel="telegram")
    assert isinstance(resp_j, str)
    assert "Åtkomst nekad" in resp_j

    # Agent (operator) -> denied
    resp_a = dispatch_openclaw_command("333", "/ops", store, channel="telegram")
    assert isinstance(resp_a, str)
    assert "Åtkomst nekad" in resp_a

    # Unmapped chat -> denied
    resp_u = dispatch_openclaw_command("999", "/ops", store, channel="telegram")
    assert isinstance(resp_u, str)
    assert "Åtkomst nekad" in resp_u

    # Oscar (admin) -> allowed & returns Swedish runtime summary
    resp_o = dispatch_openclaw_command("111", "/ops", store, channel="telegram")
    assert isinstance(resp_o, str)
    assert "Azom Runtime /ops" in resp_o
    assert "Poller:" in resp_o


# ── Bot wiring: runtime error recording & 30m periodic GC ───────────────────


def test_bot_records_poller_errors_and_triggers_periodic_gc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ecom_ops.bot import __main__ as bot_main

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    # Poller error recording captures safe metadata without leaking URLs/tokens
    exc = TelegramApiError(
        status_code=502,
        method="getUpdates",
        description="https://api.telegram.org/bot999:SUPERSECRET/getUpdates bad gateway",
        retry_after=5.0,
    )
    bot_main._record_poller_error("telegram_poller", exc, label="poll_loop")
    snap = collect_runtime_snapshot(data_dir=tmp_path)
    assert snap["recent_errors_total"] == 1
    entry = snap["recent_errors"][0]
    assert entry["source"] == "telegram_poller"
    assert entry["error_class"] == "TelegramApiError"
    assert entry["status_code"] == 502
    assert "SUPERSECRET" not in entry["detail"]
    assert "method=getUpdates" in entry["detail"]

    # Periodic GC skips before 30 min, executes once 30 min elapsed
    calls: list[bool] = []
    monkeypatch.setattr(
        "ecom_ops.ops_gc.run_ops_gc",
        lambda *, dry_run=False, **kwargs: (calls.append(dry_run) or {"ok": True, "errors": []}),
    )
    t0 = 1000.0
    assert bot_main._maybe_run_periodic_gc(t0, now=t0 + 600.0) == t0
    assert calls == []
    assert bot_main._maybe_run_periodic_gc(t0, now=t0 + 1805.0) == t0 + 1805.0
    assert calls == [False]

    # C1 cross-surface parity: wpreview:push_draft:wr-... matches Telegram action callback pattern
    assert bot_main._ACTION_CALLBACK.fullmatch("wpreview:push_draft:wr-abcdef012345") is not None
