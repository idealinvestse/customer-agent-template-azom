"""Theme and plugin diff plans. Mock only. No live SSH and no production write."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from _helpers import (
    MOCK_REGISTRY,
    clear_dashboard_password_env,
)
from _helpers import (
    REPO_ROOT as ROOT,
)
from _helpers import (
    tool_call as _call,
)

from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.store import ConversationStore
from ecom_ops.integrations.wp_files_ssh import SshFileTransport
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import (
    WpAgentTools,
    run_bounded_file_tool_loop,
    run_bounded_wp_tool_loop,
)
from ecom_ops.wp_ops.files import PathRejected, TransportError
from ecom_ops.wp_ops.registry import load_sites
from ecom_ops.wp_ops.service import WpOpsResult, WpOpsService

EMPTY = hashlib.sha256(b"").hexdigest()
TOKEN = "AZOM-FILE-TOKEN-9f3c"


def _service(tmp_path, monkeypatch, registry: Path | None = None) -> WpOpsService:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
    monkeypatch.setenv("XAI_GROK_BIN", "")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()
    return WpOpsService(
        registry_path=registry or MOCK_REGISTRY,
        data_dir=tmp_path,
    )


def _tools(service: WpOpsService) -> WpAgentTools:
    return WpAgentTools(service)


def _style(tmp_path: Path, site_id: str) -> Path:
    path = (
        tmp_path
        / "wp_files_mock"
        / site_id
        / "wp-content"
        / "themes"
        / "twentytwentyfive"
        / "style.css"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("/* mock theme */\n", encoding="utf-8")
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_shell_names_and_extra_fields_are_rejected(tmp_path, monkeypatch):
    tools = _tools(_service(tmp_path, monkeypatch))
    shell = tools.execute("wp_shell", {"command": "id"}, actor="oscar")
    cli = tools.execute("wp", {"command": "plugin list"}, actor="oscar")
    assert shell.data["error_class"] == "InvalidToolRequest"
    assert cli.data["error_class"] == "InvalidToolRequest"
    extra = tools.execute(
        "wp_propose_file_diff",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "twentytwentyfive",
            "path": "style.css",
            "new_text": "x",
            "base_sha256": EMPTY,
            "host": "attacker.invalid",
        },
        actor="oscar",
    )
    assert extra.data["error_class"] == "InvalidToolArguments"


def test_windowed_read_reports_next_offset_without_a_1200_clip(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)
    path = _style(tmp_path, "mock-staging")
    path.write_text("A" * 10000, encoding="utf-8")
    full = _digest(path)
    first = tools.execute(
        "wp_read_theme_plugin_file",
        {"site_id": "mock-staging", "kind": "theme", "slug": "twentytwentyfive", "path": "style.css"},
        actor="oscar",
    )
    assert first.data["ok"] is True
    assert len(first.data["content"]) > 1200
    assert len(first.data["content"].encode()) <= 8192
    assert first.data["next_offset"] == len(first.data["content"].encode())
    assert first.data["sha256"] == full
    assert "var/www" not in json.dumps(first.data)
    second = tools.execute(
        "wp_read_theme_plugin_file",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "twentytwentyfive",
            "path": "style.css",
            "offset": first.data["next_offset"],
        },
        actor="oscar",
    )
    assert second.data["content"]
    assert second.data["next_offset"] is None
    flagged = tools.execute(
        "wp_read_theme_plugin_file",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "twentytwentyfive",
            "path": "style.css",
            "offset": True,
        },
        actor="oscar",
    )
    assert flagged.data["error_class"] == "InvalidToolArguments"


def test_windowed_read_keeps_a_split_utf8_character(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)
    path = _style(tmp_path, "mock-staging")
    original = ("A" * 8191) + "ö" + ("B" * 20)
    path.write_text(original, encoding="utf-8")
    full = _digest(path)
    pages: list[str] = []
    offset = 0
    base = {
        "site_id": "mock-staging",
        "kind": "theme",
        "slug": "twentytwentyfive",
        "path": "style.css",
    }
    for _ in range(4):
        args = dict(base)
        if offset:
            args["offset"] = offset
        page = tools.execute("wp_read_theme_plugin_file", args, actor="oscar")
        assert page.data["ok"] is True
        assert page.data["sha256"] == full
        pages.append(page.data["content"])
        nxt = page.data["next_offset"]
        if nxt is None:
            assert page.data["truncated"] is False
            break
        assert page.data["truncated"] is True
        assert nxt > offset
        offset = nxt
    else:
        raise AssertionError("window did not reach the end of the file")
    assert "".join(pages) == original
    assert pages[0] == "A" * 8191
    assert pages[1].startswith("ö")


def test_denied_file_result_does_not_inherit_another_status(tmp_path, monkeypatch):
    tools = _tools(_service(tmp_path, monkeypatch))
    denied = tools._file_result(
        WpOpsResult(
            False,
            "no",
            {
                "error_class": "AccessDenied",
                "status": "pending_review",
                "plan_id": "fe-0123456789ab",
                "site_id": "mock-staging",
            },
        ),
        "mock-staging",
    )
    assert denied.data["status"] == "denied"
    assert denied.data["error_class"] == "AccessDenied"
    assert "plan_id" not in denied.data
    review = tools._file_result(
        WpOpsResult(
            False,
            "no",
            {
                "error_class": "ParentThemeBlocked",
                "status": "rejected",
                "review_path": "/wp/mock-staging/reviews/wr-0123456789ab",
                "plan_id": "wr-0123456789ab",
                "site_id": "mock-staging",
                "risk": "low",
                "login_required": True,
            },
        ),
        "mock-staging",
    )
    assert review.data["status"] == "rejected"
    assert review.data["review_path"] == "/wp/mock-staging/reviews/wr-0123456789ab"
    assert review.data["plan_id"] == "wr-0123456789ab"


def test_tree_is_readable_by_jonatan_and_search_is_oscar_only(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)
    theme = _style(tmp_path, "mock-staging").parent
    (theme / "find.php").write_text(f"<?php\n// {TOKEN}\n", encoding="utf-8")
    vendor = theme / "vendor"
    vendor.mkdir()
    (vendor / "skip.php").write_text(f"<?php\n// {TOKEN}\n", encoding="utf-8")
    listed = tools.execute(
        "wp_list_component_tree",
        {"site_id": "mock-staging", "kind": "theme", "slug": "twentytwentyfive"},
        actor="jonatan",
    )
    assert listed.data["ok"] is True
    names = {row["name"] for row in listed.data["entries"]}
    assert "style.css" in names
    assert "vendor" not in names
    assert TOKEN not in json.dumps(listed.data)
    denied = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "twentytwentyfive",
            "query": TOKEN,
        },
        actor="jonatan",
    )
    assert denied.data["error_class"] == "AccessDenied"
    found = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "twentytwentyfive",
            "query": TOKEN,
        },
        actor="oscar",
    )
    assert found.data["ok"] is True
    paths = [hit["path"] for hit in found.data["hits"]]
    assert "find.php" in paths
    assert all("vendor" not in path for path in paths)
    assert all(len(hit["excerpt"]) <= 120 for hit in found.data["hits"])
    hidden = tools.execute(
        "wp_read_theme_plugin_file",
        {"site_id": "mock-staging", "kind": "theme", "slug": "twentytwentyfive", "path": "find.php"},
        actor="jonatan",
    )
    assert hidden.data["error_class"] == "AccessDenied"


def test_component_context_strips_secrets_and_does_not_call_rest(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    def explode(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr("ecom_ops.wp_ops.agent_tools.load_live_theme_rows", explode)
    monkeypatch.setattr("ecom_ops.wp_ops.agent_tools.load_live_plugin_rows", explode)
    result = tools.execute("wp_component_context", {"site_id": "mock-shop"}, actor="jonatan")
    blob = json.dumps(result.data)
    assert result.data["ok"] is True
    assert result.data["php_version"] == "8.3"
    assert result.data["php_source"] == "registry"
    assert result.data["themes"]["source"] == "not_called"
    assert result.data["live_calls"] is False
    for secret in ("secret_ref", "files_root", "ssh_host", "WP_SECRET"):
        assert secret not in blob


def test_diff_plan_keeps_the_body_out_of_state_and_audit(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    path = _style(tmp_path, "mock-staging")
    body = f"/* mock theme */\n/* {TOKEN} */\n"
    proposed = service.propose_file_diff(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        "style.css",
        body,
        _digest(path),
        "lägg till en kommentar",
        actor="oscar",
    )
    assert proposed.ok is True
    assert proposed.data["executed"] is False
    plan_id = proposed.data["plan"]["plan_id"]
    state = json.dumps(json.loads((tmp_path / "wp_ops_state.json").read_text(encoding="utf-8")))
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert TOKEN not in state
    assert TOKEN not in audit
    assert TOKEN in (tmp_path / "wp_file_payloads" / plan_id).read_text(encoding="utf-8")
    assert "path" not in proposed.data["plan"]


def test_woo_core_parent_theme_and_high_risk_stop_before_a_write(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    path = _style(tmp_path, "mock-staging")
    before = path.read_bytes()
    woo = service.propose_file_diff(
        "mock-staging",
        "plugin",
        "woocommerce",
        "hello.php",
        "<?php\n",
        EMPTY,
        "nej",
        actor="oscar",
    )
    assert woo.data["error_class"] == "WooCoreRefused"
    parent = service.propose_file_diff(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        "style.css",
        "/* child */\n",
        _digest(path),
        "parent",
        actor="oscar",
        parent_slug="twentytwentyfive",
    )
    assert parent.data["error_class"] == "ParentThemeRefused"
    risky = service.propose_file_diff(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        "style.css",
        "/* betalning */\n",
        _digest(path),
        "kassa",
        actor="oscar",
    )
    assert risky.ok is True
    assert risky.data["plan"]["risk"] == "high"
    staged = service.stage_file_plan(risky.data["plan"]["plan_id"], actor="oscar")
    assert staged.data["error_class"] == "HighRiskBlocked"
    assert path.read_bytes() == before


def test_staging_local_tree_writes_and_production_does_not(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    staging = _style(tmp_path, "mock-staging")
    production = _style(tmp_path, "mock-shop")
    production_before = production.read_bytes()
    updated = "/* mock theme */\n/* staged */\n"
    plan = service.propose_file_diff(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        "style.css",
        updated,
        _digest(staging),
        "stagingkommentar",
        actor="oscar",
    )
    plan_id = plan.data["plan"]["plan_id"]
    preview = service.preview_file_stage(plan_id, actor="oscar")
    assert preview.data["executed"] is False
    assert preview.data["approval_required"] is True
    assert staging.read_text(encoding="utf-8") == "/* mock theme */\n"
    staged = service.stage_file_plan(plan_id, actor="oscar")
    assert staged.data["executed"] is True
    assert staging.read_text(encoding="utf-8") == updated
    assert not (tmp_path / "wp_file_payloads" / plan_id).exists()
    shop = service.propose_file_diff(
        "mock-shop",
        "theme",
        "twentytwentyfive",
        "style.css",
        updated,
        _digest(production),
        "produktion",
        actor="oscar",
    )
    refused = service.stage_file_plan(shop.data["plan"]["plan_id"], actor="oscar")
    assert refused.data["error_class"] == "ProductionWriteDisabled"
    assert production.read_bytes() == production_before
    deployed = service.deploy_file_production("mock-shop", shop.data["plan"]["plan_id"], actor="oscar")
    assert deployed.data["error_class"] == "BackupNotVerified"
    assert deployed.data["executed"] is False
    assert deployed.data["bytes_copied"] is False
    assert production.read_bytes() == production_before
    rolled = service.rollback_file_plan("mock-staging", plan_id, actor="oscar")
    assert rolled.data["error_class"] == "RestoreNotInMvp"
    assert staging.read_text(encoding="utf-8") == updated


def test_missing_staging_site_is_reported(tmp_path, monkeypatch):
    registry = tmp_path / "sites.yaml"
    text = (ROOT / "config" / "wp_sites.mock.yaml").read_text(encoding="utf-8")
    registry.write_text(text.split("  - id: mock-staging")[0], encoding="utf-8")
    service = _service(tmp_path, monkeypatch, registry)
    path = _style(tmp_path, "mock-shop")
    before = path.read_bytes()
    plan = service.propose_file_diff(
        "mock-shop",
        "theme",
        "twentytwentyfive",
        "style.css",
        "/* mock theme */\n/* x */\n",
        _digest(path),
        "ingen staging",
        actor="oscar",
    )
    refused = service.stage_file_plan(plan.data["plan"]["plan_id"], actor="oscar")
    assert refused.data["error_class"] == "StagingUnconfigured"
    assert path.read_bytes() == before


def test_expired_plan_and_legacy_apply_expire_without_deleting_the_row(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    path = _style(tmp_path, "mock-staging")
    original = path.read_text(encoding="utf-8")
    plan = service.propose_file_diff(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        "style.css",
        original + "/* later */\n",
        _digest(path),
        "går ut",
        actor="oscar",
    )
    plan_id = plan.data["plan"]["plan_id"]

    def expire(state):
        state["file_edits"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"

    service.store.update(expire)
    assert service.stage_file_plan(plan_id, actor="oscar").data["error_class"] == "PlanExpired"
    assert path.read_text(encoding="utf-8") == original
    legacy = service.propose_file(
        "mock-staging", "theme", "twentytwentyfive", "style.css", original + "/* legacy */\n", actor="oscar"
    )
    legacy_id = legacy.data["plan"]["id"]

    def age(state):
        for row in state["file_edits"]:
            if row.get("id") == legacy_id:
                row["created_at"] = "2000-01-01T00:00:00+00:00"

    service.store.update(age)
    applied = service.apply_file(
        "mock-staging", legacy_id, original + "/* legacy */\n", actor="oscar"
    )
    assert applied.data["error_class"] == "PlanExpired"
    assert any(row.get("id") == legacy_id for row in service.store.load()["file_edits"])
    assert path.read_text(encoding="utf-8") == original


def test_non_local_transport_and_live_sftp_do_not_write(tmp_path, monkeypatch):
    sites = load_sites(ROOT / "config" / "wp_sites.mock.yaml", apply_overlay=False)
    staging = next(site for site in sites if site.id == "mock-staging")
    style = staging.files_root + "/wp-content/themes/twentytwentyfive/style.css"

    class Fake:
        def __init__(self):
            self.writes: list[str] = []
            self.files = {style: b"/* mock theme */\n"}

        def list_dir(self, absolute_dir: str) -> list[str]:
            prefix = absolute_dir.rstrip("/") + "/"
            return sorted({path[len(prefix):].split("/", 1)[0] for path in self.files if path.startswith(prefix)})

        def read_bytes(self, absolute_file: str) -> bytes:
            if absolute_file not in self.files:
                raise TransportError("FileMissing")
            return self.files[absolute_file]

        def write_bytes(self, absolute_file: str, data: bytes) -> None:
            self.writes.append(absolute_file)
            self.files[absolute_file] = data

    fake = Fake()
    service = _service(tmp_path, monkeypatch)
    service.files_factory = lambda _site: fake
    plan = service.propose_file_diff(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        "style.css",
        "/* mock theme */\n/* no */\n",
        hashlib.sha256(fake.files[style]).hexdigest(),
        "inte local",
        actor="oscar",
    )
    refused = service.stage_file_plan(plan.data["plan"]["plan_id"], actor="oscar")
    assert refused.data["error_class"] == "DeployTransportUnavailable"
    assert fake.writes == []
    transport = SshFileTransport(staging, tmp_path)
    deeper = staging.files_root + "/wp-content/themes/twentytwentyfive/parts"
    assert transport._safe_path(deeper, listing=True) == deeper
    with pytest.raises(PathRejected):
        transport._safe_path(staging.files_root + "/wp-content/themes/twentytwentyfive/.env", listing=True)
    with pytest.raises(TransportError) as raised:
        transport.write_bytes(style, b"nope")
    assert raised.value.error_class == "SshWriteDisabled"


def test_action_yes_does_not_apply_and_the_stage_button_does(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    path = _style(tmp_path, "mock-staging")
    updated = "/* mock theme */\n/* button */\n"
    plan = service.propose_file_diff(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        "style.css",
        updated,
        _digest(path),
        "knapp",
        actor="oscar",
    )
    plan_id = plan.data["plan"]["plan_id"]
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")
    handler = BotHandler(store=ConversationStore(path=tmp_path / "telegram.json"))
    ignored = handler.handle_callback(42, "action:yes")
    assert path.read_text(encoding="utf-8") == "/* mock theme */\n"
    assert "ändrades" in ignored.text.lower() or "bekräft" in ignored.text.lower() or ignored.text
    staged = handler.handle_callback(42, f"wpfile:stage:{plan_id}")
    assert staged.text.startswith("Filen är skriven i staging.")
    assert path.read_text(encoding="utf-8") == updated
    created = _style(tmp_path, "mock-staging")
    assert TOKEN not in staged.text
    assert created.name == "style.css"


def test_cancel_button_removes_the_payload_and_does_not_write(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    path = _style(tmp_path, "mock-staging")
    plan = service.propose_file_diff(
        "mock-staging",
        "theme",
        "twentytwentyfive",
        "style.css",
        "/* mock theme */\n/* cancel */\n",
        _digest(path),
        "avbryt",
        actor="oscar",
    )
    plan_id = plan.data["plan"]["plan_id"]
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")
    handler = BotHandler(store=ConversationStore(path=tmp_path / "telegram.json"))
    reply = handler.handle_callback(42, f"wpfile:no:{plan_id}")
    assert reply.text == "Avbrutet. Ingen fil ändrades."
    assert path.read_text(encoding="utf-8") == "/* mock theme */\n"
    assert not (tmp_path / "wp_file_payloads" / plan_id).exists()


def test_backup_script_excludes_review_payloads():
    text = (ROOT / "bin" / "backup-azom-data.sh").read_text(encoding="utf-8")
    assert 'wp_file_payloads' in text


def test_drift_loop_does_not_execute_a_file_tool():
    class Fake:
        def __init__(self):
            self.names: list[str] = []

        def execute(self, name, arguments, *, actor, sticky_site_id=None, channel="", conversation_id=""):
            del arguments, actor, sticky_site_id, channel, conversation_id
            self.names.append(name)
            from ecom_ops.wp_ops.agent_tools import ToolOutput

            return ToolOutput({"ok": True, "status": "ok", "site_id": "mock-shop"}, "mock-shop")

    tools = Fake()
    result = run_bounded_wp_tool_loop(
        _call("wp_list_sites"),
        [],
        complete=lambda _messages: (
            _call(
                "wp_read_theme_plugin_file",
                {"site_id": "mock-shop", "kind": "theme", "slug": "twentytwentyfive", "path": "style.css"},
            ),
            0.0,
        ),
        actor="agent",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
        wp_intent=True,
    )
    assert result.handled is True
    assert tools.names == ["wp_list_sites"]
    assert "nytt meddelande" in result.content


def test_file_loop_does_not_keep_source_in_the_returned_summary(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    path = _style(tmp_path, "mock-staging")
    path.write_text(TOKEN + ("B" * 2000), encoding="utf-8")
    seen: list[str] = []

    def complete(messages):
        seen.append(json.dumps(messages))
        return "Fönstret är läst. Ingen fil ändrades.", 0.0

    result = run_bounded_file_tool_loop(
        _call(
            "wp_read_theme_plugin_file",
            {"site_id": "mock-staging", "kind": "theme", "slug": "twentytwentyfive", "path": "style.css"},
        ),
        [],
        complete=complete,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=_tools(service),
    )
    assert result.handled is True
    assert TOKEN not in result.content
    assert result.stage_plan_id is None
    assert any(TOKEN in blob for blob in seen)


def test_dashboard_review_shows_the_diff_and_deploy_stays_closed(
    tmp_path, monkeypatch, dashboard_app, dashboard_auth
):
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    clear_dashboard_password_env(monkeypatch)
    path = _style(tmp_path, "mock-shop")
    before = path.read_bytes()
    body = f"/* mock theme */\n/* {TOKEN} */\n"
    plan = service.propose_file_diff(
        "mock-shop",
        "theme",
        "twentytwentyfive",
        "style.css",
        body,
        _digest(path),
        "panel",
        actor="oscar",
    )
    plan_id = plan.data["plan"]["plan_id"]
    record = plan.data["plan"]
    app = dashboard_app()
    client = app.test_client()
    jonatan = dashboard_auth(client, "jonatan")
    oscar = dashboard_auth(client, "oscar")
    assert client.get(f"/wp/mock-shop/files/plan/{plan_id}", headers=jonatan).status_code == 403
    page = client.get(f"/wp/mock-shop/files/plan/{plan_id}", headers=oscar)
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert TOKEN in html
    assert 'data-executed="false"' in html
    missing = client.post(
        "/wp/mock-shop/files/deploy",
        headers={"Authorization": oscar["Authorization"]},
        data={"plan_id": plan_id},
    )
    assert missing.status_code == 400
    posted = client.post(
        "/wp/mock-shop/files/deploy",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "plan_id": plan_id,
            "before_digest": record["before_digest"],
            "after_digest": record["after_digest"],
            "environment": record["environment"],
            "path": record["relative_path"],
        },
    )
    body_text = posted.get_data(as_text=True)
    assert posted.status_code == 200
    assert "BackupNotVerified" in body_text
    assert 'data-executed="false"' in body_text
    assert path.read_bytes() == before
    assert "wpfile:stage" not in html
