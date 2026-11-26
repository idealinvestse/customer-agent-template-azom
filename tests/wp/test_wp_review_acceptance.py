"""Acceptance for the review chain: isolation, hashes, links, rollback, and honest checks."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from _helpers import (
    MOCK_REGISTRY as REGISTRY,
)
from _helpers import (
    REPO_ROOT as ROOT,
)
from _helpers import (
    clear_dashboard_password_env,
)
from _helpers import (
    dashboard_auth_headers as _auth,
)
from _helpers import (
    load_dashboard_app as _load_app,
)

from ecom_ops.bot.dashboard_links import review_plan_url
from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient
from ecom_ops.wp_ops.agent_tools import WpAgentTools, run_bounded_file_tool_loop
from ecom_ops.wp_ops.page_review_loop import run_bounded_page_review_loop
from ecom_ops.wp_ops.review_checks import run_job
from ecom_ops.wp_ops.review_plan import ReviewPlans, review_risk
from ecom_ops.wp_ops.review_runner import FORBIDDEN_ENV_NAMES, SandboxGate, synthetic_env
from ecom_ops.wp_ops.review_workspace import (
    VerifiedPlugin,
    VerifiedTheme,
    read_site_component,
    workspace_root,
)
from ecom_ops.wp_ops.service import WpOpsService

SYNTAX_MARKER = "AZOM_FAKE_PHP_SYNTAX_ERROR"
SHOP = "SHOP_ONLY_SECRET"
STAGING = "STAGING_ONLY_TEXT"


def _plans(tmp_path: Path, **extra) -> ReviewPlans:
    return ReviewPlans(data_dir=tmp_path / "data", registry_path=REGISTRY, **extra)


def _theme(tmp_path: Path, site: str, slug: str, text: str) -> None:
    folder = tmp_path / "data" / "wp_files_mock" / site / "wp-content" / "themes" / slug
    folder.mkdir(parents=True)
    (folder / "style.css").write_text(text, encoding="utf-8")


def _open_child(plans: ReviewPlans):
    return plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )


def _dashboard(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    clear_dashboard_password_env(monkeypatch)
    for name in (
        "WP_APP_PASSWORD",
        "WP_BASE_URL",
        "WOO_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    from ecom_ops.rbac import clear_rbac_cache

    clear_rbac_cache()
    app = _load_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_sites_and_plugins_stay_isolated(tmp_path: Path):
    _theme(tmp_path, "mock-shop", "storefront-child", f"/* {SHOP} */\n")
    _theme(tmp_path, "mock-shop", "storefront", f"/* parent {SHOP} */\n")
    _theme(tmp_path, "mock-staging", "storefront-child", f"/* {STAGING} */\n")
    plans = _plans(tmp_path)
    opened = plans.open(
        "mock-staging",
        "theme",
        "storefront-child",
        actor="oscar",
        verified_theme=VerifiedTheme("storefront-child", "storefront"),
    )
    assert opened.ok
    plan_id = opened.data["plan_id"]
    body = (workspace_root(tmp_path / "data", "mock-staging", plan_id) / "work" / "style.css").read_text(
        encoding="utf-8"
    )
    assert STAGING in body
    assert SHOP not in body
    listed = plans.list_site("mock-shop", actor="jonatan")
    assert listed.ok
    assert plan_id not in {row["plan_id"] for row in listed.data["plans"]}
    hidden = plans.show("mock-shop", plan_id, actor="jonatan")
    assert hidden.data["error_class"] == "UnknownPlan"
    assert "mock-staging" not in json.dumps(hidden.data)
    parent = plans.open(
        "mock-staging",
        "theme",
        "storefront",
        actor="oscar",
        verified_theme=VerifiedTheme("storefront-child", "storefront"),
    )
    assert parent.data["error_class"] == "ParentThemeBlocked"
    woo = plans.open("mock-staging", "plugin", "woocommerce", actor="agent")
    assert woo.data["error_class"] == "WooCoreRefused"
    third = plans.open("mock-staging", "plugin", "hello-dolly", actor="agent")
    assert third.data["status"] == "fork_not_approved"
    own = plans.open("mock-staging", "plugin", actor="agent")
    assert own.ok
    assert own.data["status"] == "validated"
    assert not (tmp_path / "data" / "wp_files_mock" / "mock-staging" / "wp-content" / "plugins").exists()


def test_child_skeleton_stays_in_the_workspace(tmp_path: Path):
    plans = _plans(tmp_path)
    opened = _open_child(plans)
    assert opened.data["status"] == "validated"
    assert opened.data["theme_relationship"] == "no_child"
    plan_id = opened.data["plan_id"]
    css = (workspace_root(tmp_path / "data", "mock-staging", plan_id) / "work" / "style.css").read_text(
        encoding="utf-8"
    )
    assert "Template: twentytwentyfive" in css
    assert not (
        tmp_path / "data" / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "twentytwentyfive-child"
    ).exists()
    unknown = plans.open("mock-staging", "theme", "twentytwentyfive", actor="oscar")
    assert unknown.data["status"] == "draft"
    assert unknown.data["theme_relationship"] == "unknown"


def test_hash_risk_and_free_text_do_not_write(tmp_path: Path, monkeypatch):
    plans = _plans(tmp_path)
    first = _open_child(plans)
    plan_id = first.data["plan_id"]
    shown = plans.show("mock-staging", plan_id, actor="oscar", include_diff=True)
    base = shown.data["plan"]["base_sha256"]
    work = shown.data["plan"]["work_sha256"]
    mismatch = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=base,
        work_sha256="0" * 64,
    )
    assert mismatch.data["error_class"] == "DigestMismatch"
    assert mismatch.data["executed"] is False
    second = _open_child(plans)
    stale = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=base,
        work_sha256=work,
    )
    assert stale.data["error_class"] == "StalePlan"
    assert second.data["plan_id"] != plan_id
    risky = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="lager",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert risky.data["risk"] == "high"
    assert review_risk("shipping/rates.php", "order") == "high"
    blocked = plans.approve_staging(
        "mock-staging",
        risky.data["plan_id"],
        actor="oscar",
        base_sha256=plans.show("mock-staging", risky.data["plan_id"], actor="oscar").data["plan"]["base_sha256"],
        work_sha256=plans.show("mock-staging", risky.data["plan_id"], actor="oscar").data["plan"]["work_sha256"],
    )
    assert blocked.data["error_class"] == "HighRiskBlocked"
    assert plans.approve_staging(
        "mock-staging", plan_id, actor="agent", base_sha256=base, work_sha256=work
    ).data["error_class"] == "AccessDenied"
    for phrase in ("ja", "action:yes", "godkänn"):
        answer = plans.decide_text(phrase, actor="oscar")
        assert answer.data["error_class"] == "TextNotApproval"
        assert answer.data["changed"] is False
    called = {"restore": 0}

    def _restore(self, *args, **kwargs):
        del self, args, kwargs
        called["restore"] += 1
        raise AssertionError("restore")

    monkeypatch.setattr(WpOpsService, "restore", _restore)
    fresh = _open_child(plans)
    fresh_id = fresh.data["plan_id"]
    placed = plans.place(
        "mock-staging",
        fresh_id,
        "functions.php",
        b"<?php\n// changed\n",
        actor="oscar",
    )
    assert placed.ok
    rolled = plans.rollback("mock-staging", fresh_id, actor="oscar")
    assert rolled.data["status"] == "rolled_back"
    assert rolled.data["restore"] == "not_called"
    assert called["restore"] == 0
    assert (
        workspace_root(tmp_path / "data", "mock-staging", fresh_id) / "work" / "functions.php"
    ).read_bytes() == b"<?php\n"
    tools = WpAgentTools()
    for name in ("wp_apply_staging", "wp_deploy_production", "wp_rollback_file"):
        arguments = {"plan_id": fresh_id}
        if name != "wp_apply_staging":
            arguments["site_id"] = "mock-staging"
        output = tools.execute(name, arguments, actor="oscar")
        assert output.data["ok"] is False
        assert output.data["error_class"] == "InvalidToolArguments"


def test_expiry_removes_work_and_keeps_the_row(tmp_path: Path):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    current = {"at": start}
    plans = _plans(tmp_path, clock=lambda: current["at"])
    opened = _open_child(plans)
    plan_id = opened.data["plan_id"]
    root = workspace_root(tmp_path / "data", "mock-staging", plan_id)
    assert (root / "work").is_dir()
    current["at"] = start + timedelta(hours=13)
    assert plans.expire_due(actor="agent") == 1
    assert not (root / "work").exists()
    assert (root / "base").is_dir()
    saved = json.loads((tmp_path / "data" / "wp_review_plans.json").read_text(encoding="utf-8"))
    assert saved["plans"][0]["plan_id"] == plan_id
    assert saved["plans"][0]["status"] == "expired"
    again = plans.show("mock-staging", plan_id, actor="jonatan")
    assert again.data["error_class"] == "Expired"


def test_review_link_requires_https_and_contains_only_site_and_plan(monkeypatch):
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.test/base/")
    url = review_plan_url("mock-staging", "wr-0123456789ab")
    assert url == "https://ops.example.test/base/wp/mock-staging/reviews/wr-0123456789ab"
    assert "token" not in url and "approve" not in url
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "http://ops.example.test")
    assert review_plan_url("mock-staging", "wr-0123456789ab") is None
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://user:pass@ops.example.test")
    assert review_plan_url("mock-staging", "wr-0123456789ab") is None


def test_sftp_workspace_copy_is_bounded_and_skips_sensitive_paths():
    root = "/var/www/mock-staging/wp-content/themes/twentytwentyfive"

    class ReadOnlyTree:
        def list_dir(self, path):
            return {
                root: ["style.css", "functions.php", "assets", "wp-config.php"],
                f"{root}/assets": ["main.js"],
            }.get(path, [])

        def read_bytes(self, path):
            return {
                f"{root}/style.css": b"body { color: black; }",
                f"{root}/functions.php": b"<?php\n",
                f"{root}/assets/main.js": b"window.azom = true;",
            }[path]

    files = read_site_component(ReadOnlyTree(), "/var/www/mock-staging", "theme", "twentytwentyfive")
    assert files == [
        ("assets/main.js", b"window.azom = true;"),
        ("functions.php", b"<?php\n"),
        ("style.css", b"body { color: black; }"),
    ]


def test_existing_plugin_workspace_requires_server_verified_slug(tmp_path: Path):
    root = "/var/www/mock-staging/wp-content/plugins/demo-plugin"

    class ReadOnlyTree:
        def list_dir(self, path):
            return ["demo-plugin.php"] if path == root else []

        def read_bytes(self, path):
            assert path == f"{root}/demo-plugin.php"
            return b"<?php\nadd_action('init', 'demo_init');\n"

    plans = ReviewPlans(
        data_dir=tmp_path / "data",
        registry_path=REGISTRY,
        transport_factory=lambda _site: ReadOnlyTree(),
    )
    opened = plans.open(
        "mock-staging",
        "plugin",
        "demo-plugin",
        actor="agent",
        verified_plugin=VerifiedPlugin("demo-plugin"),
    )
    assert opened.ok is True
    workspace = workspace_root(tmp_path / "data", "mock-staging", opened.data["plan_id"])
    assert (workspace / "base" / "demo-plugin.php").read_bytes().startswith(b"<?php")
    rejected = _plans(tmp_path).open("mock-staging", "plugin", "other-plugin", actor="agent")
    assert rejected.ok is False
    assert rejected.data["error_class"] == "ThirdPartyPluginRefused"


def test_static_jobs_and_skills_do_not_invent_a_pass(monkeypatch):
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    missing = run_job("woo_static", "plugin", [("azom.php", "<?php\nwc_get_order();\n")], "high")
    assert missing["status"] == "failed"
    assert missing["findings"] == ["wc_tested_up_to", "hpos_declaration"]
    assert missing["declaration_is_not_proof"] is True
    direct = run_job(
        "woo_static",
        "plugin",
        [("azom.php", "WC tested up to: 9\ndeclare_compatibility('custom_order_tables');\nshop_order")],
        "high",
    )
    assert direct["status"] == "failed"
    assert "shop_order" in direct["findings"]
    tests = [("tests/OrderTest.php", "<?php echo 'syntax is valid';")]
    assert run_job("phpunit", "plugin", tests, "low")["status"] == "unavailable"
    lint = run_job("php_lint", "plugin", tests, "low")
    assert lint["status"] in {"unavailable", "passed"}
    if lint["status"] == "unavailable":
        assert lint["executed"] is False and lint["simulated"] is False
    assert {"command", "started_at", "finished_at", "exit_code", "artifact_path", "executed", "simulated"} <= set(lint)
    env = synthetic_env()
    assert env.keys() == {"AZOM_REVIEW_SYNTHETIC"}
    assert FORBIDDEN_ENV_NAMES.isdisjoint(env)
    record = SandboxGate().start(type("S", (), {"php_version": "8.3", "wordpress_version": "6.7", "woocommerce": False})(), "wr-0123456789ab", high_risk=True)
    assert record["database"] == "not_created"
    assert record["state"] == "unavailable"
    assert record["payments_enabled"] is False
    skill = (ROOT / "skills" / "wordpress-review" / "SKILL.md").read_text(encoding="utf-8")
    knowledge = (ROOT / "skills" / "ecom_ops" / "wp_ops" / "review_knowledge.py").read_text(encoding="utf-8")
    assert "WP_OPS_" not in skill
    assert "WP_OPS_" not in knowledge
    assert "import os" not in knowledge


def test_backup_restored_plan_without_workspace_is_not_resumable(tmp_path: Path):
    plans = _plans(tmp_path)
    opened = _open_child(plans)
    plan_id = opened.data["plan_id"]
    root = workspace_root(tmp_path / "data", "mock-staging", plan_id)
    import shutil

    shutil.rmtree(root)
    shown = plans.show("mock-staging", plan_id, actor="jonatan")
    assert shown.ok
    assert shown.data["plan"]["status"] == "not_resumable"
    attempted = plans.run_checks("mock-staging", plan_id, actor="agent")
    assert attempted.ok is False
    assert attempted.data["error_class"] == "WorkspaceNotResumable"


def test_page_review_stops_on_call_and_time_budgets(tmp_path: Path):
    memory = InMemoryWpTransport()
    memory.settings["show_on_front"] = "page"
    memory.settings["page_on_front"] = 2
    memory.posts[2]["content"] = {"rendered": "<p>Hem</p>", "raw": "<!-- wp:paragraph --><p>Hem</p>"}
    memory.themes["twentytwentyfive"]["template"] = "twentytwentyfive"
    client = WordPressClient(base_url="https://mock.local", transport=memory)
    limited = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        registry_path=REGISTRY,
        data_dir=tmp_path / "data",
        clock=lambda: 0.0,
        max_calls=2,
    )
    assert [step["reason"] for step in limited.steps[2:]] == ["call_budget", "call_budget"]
    assert all(step["executed"] is False and step["status"] == "not_run" for step in limited.steps[2:])
    ticks = [0.0, 0.0, 46.0]

    def clock() -> float:
        return ticks.pop(0) if ticks else 46.0

    timed = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        registry_path=REGISTRY,
        data_dir=tmp_path / "data",
        clock=clock,
        max_calls=4,
    )
    assert timed.steps[0]["status"] == "read"
    assert [step["reason"] for step in timed.steps[1:]] == ["time_budget", "time_budget", "time_budget"]


def test_open_review_states_a_login_path_and_does_not_write(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.test")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))

    def complete(messages):
        del messages
        return "Arbetsytan är skapad.", 0.0

    result = run_bounded_file_tool_loop(
        (
            '<tool_call>{"name":"wp_open_review_workspace","arguments":'
            '{"site_id":"mock-staging","kind":"theme","slug":"twentytwentyfive","summary":"hem"}}'
            "</tool_call>"
        ),
        [],
        complete=complete,
        actor="agent",
        channel="telegram",
        conversation_id="telegram:1",
    )
    assert "Arbetsytan är skapad." in result.content
    assert "/wp/mock-staging/reviews/wr-" in result.content
    assert "risk " in result.content
    assert "https://ops.example.test/wp/mock-staging/reviews/" in result.content
    assert "kräver inloggning" in result.content
    assert "inte en behörighet" in result.content
    assert not (
        tmp_path / "data" / "wp_files_mock" / "mock-staging" / "wp-content" / "themes" / "twentytwentyfive-child"
    ).exists()


def test_dashboard_review_link_is_fail_closed(monkeypatch, tmp_path: Path):
    _dashboard(monkeypatch, tmp_path)
    plans = _plans(tmp_path)
    opened = _open_child(plans)
    plan_id = opened.data["plan_id"]
    plans.place("mock-staging", plan_id, "functions.php", f"<?php function {SYNTAX_MARKER}( {{\n".encode(), actor="oscar")
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.data["status"] == ("tests_failed" if shutil.which("php") else "checks_incomplete")
    shown = plans.show("mock-staging", plan_id, actor="oscar", include_diff=True)
    base = shown.data["plan"]["base_sha256"]
    work = shown.data["plan"]["work_sha256"]
    client = _load_app().test_client()
    client.application.config["TESTING"] = True
    path = f"/wp/mock-staging/reviews/{plan_id}"
    anonymous = client.get(path)
    assert anonymous.status_code == 401
    assert "Authentication required" in anonymous.get_data(as_text=True)
    assert SYNTAX_MARKER not in anonymous.get_data(as_text=True)
    assert client.post(f"{path}/production", data={"base_sha256": base, "work_sha256": work}).status_code == 401

    jonatan = _auth(client, "jonatan")
    viewer = client.get(path, headers=jonatan)
    assert viewer.status_code == 200
    viewer_body = viewer.get_data(as_text=True)
    assert plan_id in viewer_body
    assert "Spara synpunkt" not in viewer_body
    assert SYNTAX_MARKER not in viewer_body
    assert client.post(f"{path}/comment", headers=jonatan, data={"text": "hej", "_csrf": jonatan["X-CSRF-Token"]}).status_code == 403
    assert client.post(f"{path}/extend", headers=jonatan, data={"_csrf": jonatan["X-CSRF-Token"]}).status_code == 403
    assert client.post(f"{path}/checks", headers=jonatan, data={"_csrf": jonatan["X-CSRF-Token"]}).status_code == 403

    oscar = _auth(client, "oscar")
    page = client.get(path, headers=oscar)
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "php_lint" in body
    assert 'data-rendered-preview="false"' in body
    assert 'data-check-published="false"' in body
    assert "Ingen renderad förhandsvisning." in body
    assert 'data-review-action="checks"' in body
    assert "Spara synpunkt" in body
    assert "Förläng tolv timmar" in body
    bare = {key: value for key, value in oscar.items() if key != "X-CSRF-Token"}
    rejected = client.post(
        f"{path}/staging",
        headers=bare,
        data={"_csrf": "nope", "base_sha256": base, "work_sha256": work},
    )
    assert rejected.status_code == 400
    rejected_checks = client.post(f"{path}/checks", headers=bare, data={"_csrf": "nope"})
    assert rejected_checks.status_code == 400
    assert plans.show("mock-staging", plan_id, actor="oscar").data["plan"]["status"] == checked.data["status"]
    mismatched = client.post(
        f"{path}/staging",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "base_sha256": base, "work_sha256": "0" * 64},
    )
    assert mismatched.status_code == 200
    assert 'data-error="DigestMismatch"' in mismatched.get_data(as_text=True)
    other = client.get(f"/wp/mock-shop/reviews/{plan_id}", headers=oscar)
    assert other.status_code == 404
    assert "mock-staging" not in other.get_data(as_text=True)
    assert SYNTAX_MARKER not in other.get_data(as_text=True)
    preview = client.post(f"{path}/preview", headers=oscar, data={"_csrf": oscar["X-CSRF-Token"]})
    assert 'data-error="PreviewBlocked"' in preview.get_data(as_text=True)
    rerun = client.post(f"{path}/checks", headers=oscar, data={"_csrf": oscar["X-CSRF-Token"]})
    rerun_body = rerun.get_data(as_text=True)
    assert rerun.status_code == 200
    assert 'data-check-published="false"' in rerun_body
    assert 'data-status="tests_passed"' not in rerun_body
    assert "php_lint" in rerun_body
    produced = client.post(
        f"{path}/production",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "base_sha256": base, "work_sha256": work},
    )
    produced_body = produced.get_data(as_text=True)
    assert 'data-error="BackupNotVerified"' in produced_body
    assert 'data-executed="false"' in produced_body
    assert 'data-bytes-copied="false"' in produced_body
    assert 'data-restore="RestoreNotInMvp"' in produced_body
    assert SYNTAX_MARKER not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    before = datetime.fromisoformat(shown.data["plan"]["expires_at"])
    extended = client.post(f"{path}/extend", headers=oscar, data={"_csrf": oscar["X-CSRF-Token"]})
    assert extended.status_code == 200
    saved = json.loads((tmp_path / "data" / "wp_review_plans.json").read_text(encoding="utf-8"))
    row = next(item for item in saved["plans"] if item["plan_id"] == plan_id)
    assert row["extended"] is True
    assert datetime.fromisoformat(row["expires_at"]) - before == timedelta(hours=12)
    again = client.post(f"{path}/extend", headers=oscar, data={"_csrf": oscar["X-CSRF-Token"]})
    assert 'data-error="ExtendUsed"' in again.get_data(as_text=True)
    saved["plans"] = [
        {**item, "expires_at": "2000-01-01T00:00:00+00:00"} if item["plan_id"] == plan_id else item
        for item in saved["plans"]
    ]
    (tmp_path / "data" / "wp_review_plans.json").write_text(json.dumps(saved), encoding="utf-8")
    expired = client.get(path, headers=oscar)
    assert expired.status_code == 403
    assert "Planen har gått ut" in expired.get_data(as_text=True)
    assert SYNTAX_MARKER not in expired.get_data(as_text=True)
    assert not (workspace_root(tmp_path / "data", "mock-staging", plan_id) / "work").exists()
    kept = json.loads((tmp_path / "data" / "wp_review_plans.json").read_text(encoding="utf-8"))
    assert any(item["plan_id"] == plan_id and item["status"] == "expired" for item in kept["plans"])
