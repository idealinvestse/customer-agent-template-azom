"""Tests for Slice 3: WP plugin CLI, Dashboard /wp/<site_id>/plugins, Bot commands/intents, and review_routes health fix."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from _helpers import (
    REPO_ROOT,
    clear_dashboard_password_env,
    dashboard_auth_headers,
    load_dashboard_app,
    passing_catalog,
    seed_mock_child_theme,
)

from ecom_ops import cli as ecom_cli
from ecom_ops.bot import site_content, wp_intents
from ecom_ops.bot.reply import BotReply
from ecom_ops.bot.store import ConversationStore
from ecom_ops.bot.wp_handlers import handle_wp_context_command
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme

DASH_DIR = REPO_ROOT / "infrastructure" / "dashboard"


class _StubBotHandler:
    channel = "telegram"

    def __init__(self, store: ConversationStore, actor: str = "oscar") -> None:
        self.store = store
        self._actor = actor

    def _resolve_actor(self, _chat_id: str | int) -> str:
        return self._actor

    def _deny_actor(self) -> BotReply:
        return BotReply(text="Åtkomst nekad.")

    def _update_session_atomic(
        self,
        chat_id: str | int,
        *,
        session_updates: dict[str, Any] | None = None,
        pop_session_keys: tuple[str, ...] = (),
    ) -> None:
        prev = self.store.get(chat_id) or {}
        session = dict(prev.get("session") or {})
        for k in pop_session_keys:
            session.pop(k, None)
        if session_updates:
            session.update(session_updates)
        self.store.set(chat_id, {"session": session})


def _setup_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed_fn=None):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "mock-staging-secret-1234567890")
    clear_dashboard_password_env(monkeypatch)
    clear_rbac_cache()
    if seed_fn is not None:
        seed_fn(tmp_path)
    app = load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def _extract_plan_id_and_sha(payload: dict[str, Any]) -> tuple[str, str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else data
    pid = str(plan.get("plan_id") or data.get("plan_id") or "")
    sha = str(plan.get("plan_sha256") or data.get("plan_sha256") or "")
    return pid, sha


def _pick_non_critical_slug(plugins: list[dict[str, Any]], *, prefer_update: bool = False) -> str:
    candidates = [
        p
        for p in plugins
        if isinstance(p, dict)
        and not p.get("is_bridge")
        and not p.get("is_critical")
        and str(p.get("slug") or "")
    ]
    if prefer_update:
        with_upd = [p for p in candidates if p.get("update_available")]
        if with_upd:
            return str(with_upd[0]["slug"])
    if candidates:
        return str(candidates[0]["slug"])
    return "akismet"


def test_review_routes_post_deploy_health_uses_correct_pinned_https_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix live bug: pinned_https_request(url, method='GET', ...) returns 4-tuple (status, headers, body, final_url)."""
    meta: dict[str, str] = {}
    captured: dict[str, Any] = {}
    monkeypatch.setattr("ecom_ops.wp_ops.review_plan.run_catalog", passing_catalog)

    def _seed(data_dir: Path) -> None:
        seed_mock_child_theme(data_dir, site_id="mock-staging", slug="azom-child")
        plans = ReviewPlans(data_dir=data_dir, registry_path=REPO_ROOT / "config" / "wp_sites.mock.yaml")
        opened = plans.open(
            "mock-staging",
            "theme",
            "azom-child",
            actor="oscar",
            summary="Post-deploy health check test",
            verified_theme=VerifiedTheme("azom-child", "parent-base"),
        )
        assert opened.ok is True
        pid = opened.data["plan_id"]
        plans.patch_file(
            "mock-staging",
            pid,
            "style.css",
            ".btn { background: #111; color: #fff; }",
            ".btn { background: #333; color: #fff; }",
            actor="oscar",
        )
        assert plans.run_checks("mock-staging", pid, actor="oscar").ok is True
        row = plans.get(pid, site_id="mock-staging")
        assert row is not None
        meta["plan_id"] = pid
        meta["base_sha"] = row["base_sha256"]
        meta["work_sha"] = row["work_sha256"]

    client = _setup_app(tmp_path, monkeypatch, seed_fn=_seed)
    oscar = dashboard_auth_headers(client, "oscar")

    from ecom_ops.wp_ops.deploy_orchestrator import DeployOrchestrator
    from ecom_ops.wp_ops.models import WpOpsResult

    def _fake_deploy_workspace(self, site_id, plan_id, *, health_check_fn=None, **_kwargs):
        captured["health_check_fn"] = health_check_fn
        return WpOpsResult(ok=True, message="ok", data={"executed": True})

    monkeypatch.setattr(DeployOrchestrator, "deploy_workspace", _fake_deploy_workspace)
    resp = client.post(
        f"/wp/mock-staging/reviews/{meta['plan_id']}/deploy-live",
        data={"base_sha256": meta["base_sha"], "work_sha256": meta["work_sha"]},
        headers=oscar,
    )
    assert resp.status_code == 200
    health_fn = captured["health_check_fn"]
    assert callable(health_fn)

    # In mock mode -> True without HTTP call
    assert health_fn() is True

    # In live mode -> pinned_https_request(url, method="GET", ...) must be unpacked as 4-tuple
    monkeypatch.setattr("ecom_ops.wp_ops.config.is_mock_mode", lambda: False)
    http_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_pinned_ok(url: str, *, method: str = "GET", **kwargs):
        http_calls.append(((url,), {"method": method, **kwargs}))
        return 200, {"content-type": "text/html"}, b"<html>ok</html>", url

    monkeypatch.setattr("ecom_ops.integrations._http_guard.pinned_https_request", _fake_pinned_ok)
    assert health_fn() is True
    assert len(http_calls) == 1
    assert http_calls[0][0][0].startswith("https://")
    assert http_calls[0][1]["method"] == "GET"

    def _fake_pinned_500(url: str, *, method: str = "GET", **kwargs):
        return 500, {}, b"fatal", url

    monkeypatch.setattr("ecom_ops.integrations._http_guard.pinned_https_request", _fake_pinned_500)
    assert health_fn() is False


@pytest.mark.parametrize(
    "phrase",
    [
        "lista alla mu-plugins på sajten",
        "kolla mu-plugin laddningsordning",
        "inspektera mu_plugin",
        "vilka drop-ins finns på sajten",
        "visa object-cache.php och advanced-cache.php",
        "kontrollera autouppdatering av plugins",
        "visa auto-update policy",
        "planera pluginuppdateringar",
        "sätt trasigt plugin i karantän",
        "run quarantine for wsod",
    ],
)
def test_wp_intents_expand_for_mu_plugins_dropins_autoupdate_and_recovery(phrase: str) -> None:
    assert wp_intents.WP_OPS_INTENT_RE.search(phrase), f"WP_OPS_INTENT_RE missed: {phrase}"
    assert wp_intents.WP_CODE_INTENT_RE.search(phrase), f"WP_CODE_INTENT_RE missed: {phrase}"
    assert wp_intents.WP_CODE_FALLTHROUGH_RE.search(phrase), f"WP_CODE_FALLTHROUGH_RE missed: {phrase}"


def test_wp_intents_plugin_update_fallthrough_and_install_delete_refusals() -> None:
    # With active site context, "uppdatera plugin" falls through to wp_ops (planning only)
    turn_ctx = site_content.site_content_turn(
        "uppdatera plugin akismet", allow_wp_ops_fallthrough=True
    )
    assert turn_ctx.consumed is False

    # Without active site context, legacy PACKAGE_REFUSE_RE still refuses
    turn_no_ctx = site_content.site_content_turn(
        "uppdatera plugin akismet", allow_wp_ops_fallthrough=False
    )
    assert turn_no_ctx.consumed is True

    # Silent install / uninstall / delete ALWAYS remain refused even with site context active
    for forbidden in (
        "installera plugin yoast",
        "avinstallera plugin yoast",
        "radera plugin yoast",
        "ta bort plugin yoast",
        "delete plugin yoast",
    ):
        assert wp_intents.PACKAGE_FALLTHROUGH_REFUSE_RE.search(forbidden), forbidden
        refused = site_content.site_content_turn(forbidden, allow_wp_ops_fallthrough=True)
        assert refused.consumed is True, forbidden


def test_bot_wp_plugins_and_mu_plugins_commands(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    wp_env(mock="1", files_ssh="0")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.com")
    store = ConversationStore(tmp_path / "conv.sqlite3")
    handler = _StubBotHandler(store, actor="jonatan")

    help_rep = handle_wp_context_command(handler, 7, "/wp help")
    assert help_rep is not None
    assert "/wp plugins" in help_rep.text
    assert "/wp mu-plugins" in help_rep.text

    # No active site yet -> polite prompt
    no_site = handle_wp_context_command(handler, 7, "/wp plugins")
    assert no_site is not None and "/wp use" in no_site.text

    handler._update_session_atomic(
        7, session_updates={"wp_ops_context": {"site_id": "mock-staging"}}
    )

    rep_plugins = handle_wp_context_command(handler, 7, "/wp plugins")
    assert rep_plugins is not None
    assert "Plugins — mock-staging" in rep_plugins.text
    assert "https://ops.example.com/wp/mock-staging/plugins" in rep_plugins.text
    assert "aldrig direkt från textchatt" in rep_plugins.text

    rep_mu = handle_wp_context_command(handler, 7, "/wp mu-plugins")
    assert rep_mu is not None
    assert "MU-plugins & Drop-ins — mock-staging" in rep_mu.text
    assert "https://ops.example.com/wp/mock-staging/plugins" in rep_mu.text


def test_cli_wp_plugins_subcommands_end_to_end(
    tmp_path: Path, wp_env, capsys: pytest.CaptureFixture[str]
) -> None:
    wp_env(mock="1", files_ssh="0")

    # 1. Inventory (viewer Jonatan allowed)
    rc = ecom_cli.main(
        ["--mock", "--actor", "jonatan", "wp", "plugins-inventory", "--id", "mock-staging"]
    )
    assert rc == 0
    inv = json.loads(capsys.readouterr().out)
    assert inv["ok"] is True
    inv_data = inv["data"].get("inventory") if isinstance(inv["data"].get("inventory"), dict) else inv["data"]
    plugins = inv_data.get("plugins") or []
    assert isinstance(plugins, list) and len(plugins) >= 1
    target_slug = _pick_non_critical_slug(plugins, prefer_update=True)

    # 2. MU-plugins & drop-ins
    rc = ecom_cli.main(["--mock", "--actor", "jonatan", "wp", "plugins-mu", "--id", "mock-staging"])
    assert rc == 0
    mu = json.loads(capsys.readouterr().out)
    assert mu["ok"] is True
    assert "mu_plugins" in mu["data"] and "dropins" in mu["data"]

    # 3. Plan & execute plugin update (Oscar)
    rc = ecom_cli.main(
        [
            "--mock",
            "--actor",
            "oscar",
            "wp",
            "plugins-plan-update",
            "--id",
            "mock-staging",
            "--mode",
            "policy",
            "--policy",
            "non_critical_only",
        ]
    )
    assert rc == 0
    plan_out = json.loads(capsys.readouterr().out)
    assert plan_out["ok"] is True
    plu_id, plu_sha = _extract_plan_id_and_sha(plan_out)
    assert re.fullmatch(r"plu-[a-f0-9]{12}", plu_id)
    assert len(plu_sha) == 64

    # Jonatan denied execute
    rc_j = ecom_cli.main(
        [
            "--mock",
            "--actor",
            "jonatan",
            "wp",
            "plugins-update",
            "--id",
            "mock-staging",
            "--plan",
            plu_id,
            "--expected-sha256",
            plu_sha,
        ]
    )
    assert rc_j != 0
    capsys.readouterr()

    # Oscar executes update plan
    rc_o = ecom_cli.main(
        [
            "--mock",
            "--actor",
            "oscar",
            "wp",
            "plugins-update",
            "--id",
            "mock-staging",
            "--plan",
            plu_id,
            "--expected-sha256",
            plu_sha,
        ]
    )
    assert rc_o == 0
    upd_out = json.loads(capsys.readouterr().out)
    assert upd_out["ok"] is True

    # 4. Auto-update show, drift, plan, execute
    assert (
        ecom_cli.main(
            ["--mock", "--actor", "jonatan", "wp", "plugins-autoupdate", "--id", "mock-staging", "--show"]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        ecom_cli.main(
            ["--mock", "--actor", "jonatan", "wp", "plugins-autoupdate", "--id", "mock-staging", "--drift"]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        ecom_cli.main(
            [
                "--mock",
                "--actor",
                "oscar",
                "wp",
                "plugins-autoupdate",
                "--id",
                "mock-staging",
                "--plan",
                "--preset",
                "non_critical_only",
            ]
        )
        == 0
    )
    pau_out = json.loads(capsys.readouterr().out)
    assert pau_out["ok"] is True
    pau_id, pau_sha = _extract_plan_id_and_sha(pau_out)
    assert re.fullmatch(r"pau-[a-f0-9]{12}", pau_id)
    assert len(pau_sha) == 64

    assert (
        ecom_cli.main(
            [
                "--mock",
                "--actor",
                "oscar",
                "wp",
                "plugins-autoupdate",
                "--id",
                "mock-staging",
                "--execute",
                "--plan",
                pau_id,
                "--expected-sha256",
                pau_sha,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # 5. Diagnostics
    assert (
        ecom_cli.main(
            [
                "--mock",
                "--actor",
                "oscar",
                "wp",
                "plugins-diagnostics",
                "--id",
                "mock-staging",
                "--hooks",
                "init,plugins_loaded",
                "--log-lines",
                "40",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # 6. Recovery plan & execute (deactivate non-critical plugin)
    assert (
        ecom_cli.main(
            [
                "--mock",
                "--actor",
                "oscar",
                "wp",
                "plugins-recovery",
                "--id",
                "mock-staging",
                "--slug",
                target_slug,
                "--action",
                "deactivate",
            ]
        )
        == 0
    )
    plr_out = json.loads(capsys.readouterr().out)
    assert plr_out["ok"] is True
    plr_id, plr_sha = _extract_plan_id_and_sha(plr_out)
    assert re.fullmatch(r"plr-[a-f0-9]{12}", plr_id)
    assert len(plr_sha) == 64

    assert (
        ecom_cli.main(
            [
                "--mock",
                "--actor",
                "oscar",
                "wp",
                "plugins-recovery",
                "--id",
                "mock-staging",
                "--slug",
                target_slug,
                "--action",
                "deactivate",
                "--execute",
                "--plan",
                plr_id,
                "--expected-sha256",
                plr_sha,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # 7. Quarantine & unquarantine + azom-ops-bridge self-protection
    mock_plugin_dir = (
        tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "plugins" / target_slug
    )
    mock_plugin_dir.mkdir(parents=True, exist_ok=True)
    (mock_plugin_dir / f"{target_slug}.php").write_text("<?php\n// mock\n", encoding="utf-8")

    rc_bridge = ecom_cli.main(
        [
            "--mock",
            "--actor",
            "oscar",
            "wp",
            "plugins-quarantine",
            "--id",
            "mock-staging",
            "--slug",
            "azom-ops-bridge",
        ]
    )
    assert rc_bridge != 0
    capsys.readouterr()

    rc_q = ecom_cli.main(
        [
            "--mock",
            "--actor",
            "oscar",
            "wp",
            "plugins-quarantine",
            "--id",
            "mock-staging",
            "--slug",
            target_slug,
            "--kind",
            "plugin",
        ]
    )
    assert rc_q == 0
    q_out = json.loads(capsys.readouterr().out)
    assert q_out["ok"] is True
    q_name = str((q_out.get("data") or {}).get("quarantine_name") or "")
    assert not mock_plugin_dir.exists()

    rc_uq = ecom_cli.main(
        [
            "--mock",
            "--actor",
            "oscar",
            "wp",
            "plugins-unquarantine",
            "--id",
            "mock-staging",
            "--slug",
            target_slug,
            "--quarantine-name",
            q_name,
            "--kind",
            "plugin",
        ]
    )
    assert rc_uq == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert mock_plugin_dir.is_dir()


def test_dashboard_wp_plugins_rbac_csrf_flash_and_flows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _setup_app(tmp_path, monkeypatch)
    jonatan = dashboard_auth_headers(client, "jonatan")
    oscar = dashboard_auth_headers(client, "oscar")

    # Nav link present on wp_site and wp_changes
    site_html = client.get("/wp/mock-staging", headers=jonatan).get_data(as_text=True)
    assert "/wp/mock-staging/plugins" in site_html
    changes_html = client.get("/wp/mock-staging/changes", headers=jonatan).get_data(as_text=True)
    assert "/wp/mock-staging/plugins" in changes_html

    # Viewer (Jonatan) read-only access: sees inventory/MU/drop-ins, ZERO POST forms, strict CSP
    j_resp = client.get("/wp/mock-staging/plugins", headers=jonatan)
    assert j_resp.status_code == 200
    j_html = j_resp.get_data(as_text=True)
    assert 'data-wp-plugins-inventory="true"' in j_html
    assert 'data-wp-mu-plugins-section="true"' in j_html
    assert 'data-wp-dropins-section="true"' in j_html
    assert 'method="post"' not in j_html.lower()
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", j_html, re.I) == []
    assert not re.search(r"\bon[a-z]+\s*=", j_html, re.I)

    # All POST routes return 403 for Jonatan
    for path in (
        "/wp/mock-staging/plugins/plan-update",
        "/wp/mock-staging/plugins/execute-update",
        "/wp/mock-staging/plugins/autoupdate/plan",
        "/wp/mock-staging/plugins/autoupdate/execute",
        "/wp/mock-staging/plugins/diagnostics",
        "/wp/mock-staging/plugins/recovery/plan",
        "/wp/mock-staging/plugins/recovery/execute",
        "/wp/mock-staging/plugins/quarantine",
        "/wp/mock-staging/plugins/unquarantine",
    ):
        assert client.post(path, data={}, headers=jonatan).status_code == 403, path

    # Missing CSRF rejected for Oscar (400 Bad Request)
    no_csrf = {k: v for k, v in oscar.items() if k.lower() != "x-csrf-token"}
    assert client.post("/wp/mock-staging/plugins/diagnostics", data={}, headers=no_csrf).status_code == 400

    # Oscar GET renders controls with _csrf
    o_resp = client.get("/wp/mock-staging/plugins", headers=oscar)
    assert o_resp.status_code == 200
    o_html = o_resp.get_data(as_text=True)
    assert 'data-oscar-plugin-updates="true"' in o_html
    assert 'data-oscar-plugin-recovery="true"' in o_html
    assert 'name="_csrf"' in o_html

    # Plan update -> redirect with Flask flash (NO ?msg= or ?err= query params!)
    plan_resp = client.post(
        "/wp/mock-staging/plugins/plan-update",
        data={"selection_mode": "policy", "policy": "non_critical_only"},
        headers=oscar,
        follow_redirects=False,
    )
    assert plan_resp.status_code == 302
    loc = plan_resp.headers["Location"]
    assert loc.endswith("/wp/mock-staging/plugins")
    assert "?msg=" not in loc and "?err=" not in loc

    after_plan_html = client.get("/wp/mock-staging/plugins", headers=oscar).get_data(as_text=True)
    plu_match = re.search(r'name="plan_id"\s+value="(plu-[a-f0-9]{12})"', after_plan_html)
    sha_match = re.search(
        r'name="plan_id"\s+value="plu-[a-f0-9]{12}"[^>]*>\s*<input[^>]*name="expected_sha256"\s+value="([a-f0-9]{64})"',
        after_plan_html,
    )
    assert plu_match and sha_match

    exec_resp = client.post(
        "/wp/mock-staging/plugins/execute-update",
        data={"plan_id": plu_match.group(1), "expected_sha256": sha_match.group(1)},
        headers=oscar,
        follow_redirects=False,
    )
    assert exec_resp.status_code == 302
    assert "?msg=" not in exec_resp.headers["Location"] and "?err=" not in exec_resp.headers["Location"]

    # Auto-update plan + execute
    pau_resp = client.post(
        "/wp/mock-staging/plugins/autoupdate/plan",
        data={"preset": "non_critical_only"},
        headers=oscar,
        follow_redirects=False,
    )
    assert pau_resp.status_code == 302
    assert "?msg=" not in pau_resp.headers["Location"]
    pau_html = client.get("/wp/mock-staging/plugins", headers=oscar).get_data(as_text=True)
    pau_m = re.search(
        r'name="plan_id"\s+value="(pau-[a-f0-9]{12})"[^>]*>\s*<input[^>]*name="expected_sha256"\s+value="([a-f0-9]{64})"',
        pau_html,
    )
    assert pau_m
    pau_exec = client.post(
        "/wp/mock-staging/plugins/autoupdate/execute",
        data={"plan_id": pau_m.group(1), "expected_sha256": pau_m.group(2)},
        headers=oscar,
        follow_redirects=False,
    )
    assert pau_exec.status_code == 302

    # Diagnostics visible to Oscar only (not viewer Jonatan)
    diag_resp = client.post(
        "/wp/mock-staging/plugins/diagnostics",
        data={"hooks": "init", "log_lines": "40"},
        headers=oscar,
        follow_redirects=False,
    )
    assert diag_resp.status_code == 302
    assert 'data-plugin-diagnostics="true"' in client.get(
        "/wp/mock-staging/plugins", headers=oscar
    ).get_data(as_text=True)
    assert 'data-plugin-diagnostics="true"' not in client.get(
        "/wp/mock-staging/plugins", headers=jonatan
    ).get_data(as_text=True)

    # Quarantine azom-ops-bridge refused via flash
    q_bridge = client.post(
        "/wp/mock-staging/plugins/quarantine",
        data={"slug": "azom-ops-bridge", "kind": "plugin"},
        headers=oscar,
        follow_redirects=False,
    )
    assert q_bridge.status_code == 302
    assert "självskyddat" in client.get("/wp/mock-staging/plugins", headers=oscar).get_data(as_text=True)

    # Recovery plan + execute (plr-*) and SFTP quarantine/unquarantine via dashboard
    mock_broken = (
        tmp_path / "wp_files_mock" / "mock-staging" / "wp-content" / "plugins" / "broken-plugin"
    )
    mock_broken.mkdir(parents=True, exist_ok=True)
    (mock_broken / "broken-plugin.php").write_text("<?php\n// broken\n", encoding="utf-8")

    plr_resp = client.post(
        "/wp/mock-staging/plugins/recovery/plan",
        data={"slug": "broken-plugin", "action": "quarantine_sftp"},
        headers=oscar,
        follow_redirects=False,
    )
    assert plr_resp.status_code == 302
    assert "?msg=" not in plr_resp.headers["Location"]
    plr_html = client.get("/wp/mock-staging/plugins", headers=oscar).get_data(as_text=True)
    plr_m = re.search(
        r'name="plan_id"\s+value="(plr-[a-f0-9]{12})"[^>]*>\s*<input[^>]*name="expected_sha256"\s+value="([a-f0-9]{64})"',
        plr_html,
    )
    assert plr_m
    plr_exec = client.post(
        "/wp/mock-staging/plugins/recovery/execute",
        data={"plan_id": plr_m.group(1), "expected_sha256": plr_m.group(2)},
        headers=oscar,
        follow_redirects=False,
    )
    assert plr_exec.status_code == 302
    assert not mock_broken.exists()

    unq_resp = client.post(
        "/wp/mock-staging/plugins/unquarantine",
        data={"slug": "broken-plugin", "kind": "plugin"},
        headers=oscar,
        follow_redirects=False,
    )
    assert unq_resp.status_code == 302
    assert mock_broken.is_dir()


def test_wp_plugins_templates_compiled_css_and_strict_csp_audit() -> None:
    css = (DASH_DIR / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
    class_attr_re = re.compile(r'(?<![:\w-])class="([^"]*)"')
    missing: list[tuple[str, str]] = []
    for rel in ("wp_plugins.html", "wp_site.html", "wp_changes.html"):
        raw = (DASH_DIR / "templates" / rel).read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe\b", raw), f"forbidden '| safe' in {rel}"
        assert not re.search(r"\bon[a-z]+\s*=", raw, re.I), f"inline on* in {rel}"
        for m in re.finditer(r"<script\b([^>]*)>", raw, re.I):
            attrs = m.group(1)
            if 'type="application/json"' in attrs or "type='application/json'" in attrs:
                continue
            assert "src=" in attrs, f"inline executable script in {rel}"
        stripped = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", raw, flags=re.S)
        for m in class_attr_re.finditer(stripped):
            for tok in m.group(1).split():
                if not tok:
                    continue
                esc = tok.replace(":", "\\:").replace("/", "\\/").replace(".", "\\.")
                if esc not in css and f".{tok}" not in css:
                    missing.append((rel, tok))
    assert missing == [], f"Uncompiled CSS utility classes found: {missing}"


def test_stage2_wp_plugins_ui_routes_and_cli_regressions(
    tmp_path: Path, wp_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressions for U1-09, U1-10, U1-11, U1-16, and U1-18."""
    wp_env(mock="1", files_ssh="0")
    client = _setup_app(tmp_path, monkeypatch)
    oscar = dashboard_auth_headers(client, "oscar")

    # U1-09 & U1-16: wp_plugins.html has <select name="kind"> with mu_plugin on unquarantine + SEO Studio tab
    o_html = client.get("/wp/mock-staging/plugins", headers=oscar).get_data(as_text=True)
    assert '/seo?site=mock-staging' in o_html
    assert '<input type="hidden" name="kind" value="plugin"' not in o_html
    assert o_html.count('<option value="mu_plugin">mu_plugin</option>') >= 2

    # U1-10: changes_json non-list flashes error instead of silently creating a preset plan
    bad_json_resp = client.post(
        "/wp/mock-staging/plugins/autoupdate/plan",
        data={"preset": "non_critical_only", "changes_json": '{"plugin_file":"a/a.php","enabled":true}'},
        headers=oscar,
        follow_redirects=True,
    )
    assert bad_json_resp.status_code == 200
    assert "JSON-lista" in bad_json_resp.get_data(as_text=True)
    assert not (tmp_path / "wp_plugin_autoupdate_plans.json").is_file()

    # U1-10: invalid quarantine_name rejected cleanly instead of coerced to ""
    bad_q = client.post(
        "/wp/mock-staging/plugins/unquarantine",
        data={"slug": "akismet", "quarantine_name": "../bad/path", "kind": "plugin"},
        headers=oscar,
        follow_redirects=True,
    )
    assert bad_q.status_code == 200
    assert "Ogiltigt karantännamn" in bad_q.get_data(as_text=True)

    # U1-11: expired pau-* and plr-* plans show expired status without execute button
    (tmp_path / "wp_plugin_autoupdate_plans.json").write_text(
        json.dumps(
            {
                "plans": [
                    {
                        "plan_id": "pau-111111111111",
                        "site_id": "mock-staging",
                        "preset": "non_critical_only",
                        "status": "planned",
                        "plan_sha256": "a" * 64,
                        "created_at": "2020-01-01T00:00:00+00:00",
                        "expires_at": "2020-01-01T12:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "wp_plugin_recovery_plans.json").write_text(
        json.dumps(
            {
                "plans": [
                    {
                        "plan_id": "plr-222222222222",
                        "site_id": "mock-staging",
                        "slug": "broken-plugin",
                        "action": "deactivate",
                        "status": "planned",
                        "plan_sha256": "b" * 64,
                        "created_at": "2020-01-01T00:00:00+00:00",
                        "expires_at": "2020-01-01T12:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    exp_html = client.get("/wp/mock-staging/plugins", headers=oscar).get_data(as_text=True)
    assert "pau-111111111111" in exp_html
    assert "plr-222222222222" in exp_html
    assert "expired" in exp_html
    assert "Tillämpa policy" not in exp_html
    assert "Kör återställning" not in exp_html

    # U1-18: CLI fail-fast for plugins-autoupdate and wp check
    with pytest.raises(SystemExit) as exc1:
        ecom_cli.main(
            [
                "--mock",
                "--actor",
                "oscar",
                "wp",
                "plugins-autoupdate",
                "--id",
                "mock-staging",
                "--plan",
                "pau-aaaaaaaaaaaa",
            ]
        )
    assert exc1.value.code == 2

    with pytest.raises(SystemExit) as exc2:
        ecom_cli.main(
            [
                "--mock",
                "--actor",
                "oscar",
                "wp",
                "plugins-autoupdate",
                "--id",
                "mock-staging",
                "--preset",
                "disable_all",
            ]
        )
    assert exc2.value.code == 2

    with pytest.raises(SystemExit) as exc3:
        ecom_cli.main(["--mock", "--actor", "oscar", "wp", "check"])
    assert exc3.value.code == 2

    # U1-18: docs/CLI_REFERENCE.md documents all 12 wp subcommands
    cli_doc = (REPO_ROOT / "docs" / "CLI_REFERENCE.md").read_text(encoding="utf-8")
    for subcmd in (
        "bridge-maintenance",
        "bridge-translations",
        "deploy-live",
        "plugins-inventory",
        "plugins-mu",
        "plugins-plan-update",
        "plugins-update",
        "plugins-autoupdate",
        "plugins-diagnostics",
        "plugins-quarantine",
        "plugins-unquarantine",
        "plugins-recovery",
    ):
        assert f"wp {subcmd}" in cli_doc, f"Missing 'wp {subcmd}' in docs/CLI_REFERENCE.md"
