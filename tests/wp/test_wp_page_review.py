"""Homepage source review stays off the order loop and does not fake checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecom_ops.audit import read_audit_log
from ecom_ops.bot.chat_agent import run_chat
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.reply import BotReply
from ecom_ops.integrations.wordpress import InMemoryWpTransport, WordPressClient
from ecom_ops.wp_ops.agent_tools import context_for_storage
from ecom_ops.wp_ops.file_changes import FILE_TOOL_NAMES
from ecom_ops.wp_ops.files import LocalTree
from ecom_ops.wp_ops.page_review_intent import is_page_review
from ecom_ops.wp_ops.page_review_loop import (
    ORDER_TOOL_NAMES,
    PAGE_REVIEW_TOOLS,
    execute_page_review_tool,
    run_bounded_page_review_loop,
)
from ecom_ops.wp_ops.review_checks import run_catalog, run_job, suite_status
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_runner import SandboxGate, synthetic_env
from ecom_ops.wp_ops.review_workspace import VerifiedTheme

ROOT = Path(__file__).resolve().parents[2]
SYNTAX_MARKER = "AZOM_FAKE_PHP_SYNTAX_ERROR"
REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"
RAW = "<!-- wp:paragraph --><p>Hem</p>"


class BoomClient:
    def get_settings(self):
        raise AssertionError("rest")

    def list_themes(self):
        raise AssertionError("rest")

    def get_page(self, page_id, context=None):
        raise AssertionError("rest")


class GuardTree(LocalTree):
    def __init__(self, remote_root: str, local_root: Path) -> None:
        super().__init__(remote_root, local_root)
        self.writes = 0

    def write_bytes(self, absolute_file: str, data: bytes) -> None:
        self.writes += 1
        raise AssertionError("production write")


def _client() -> tuple[WordPressClient, InMemoryWpTransport]:
    memory = InMemoryWpTransport()
    memory.settings["show_on_front"] = "page"
    memory.settings["page_on_front"] = 2
    memory.posts[2]["slug"] = "hem"
    memory.posts[2]["content"] = {"rendered": "<p>Azom AB</p>", "raw": RAW}
    memory.themes["twentytwentyfive"]["template"] = "twentytwentyfive"
    client = WordPressClient(base_url="https://mock.local", transport=memory)
    return client, memory


def _tree(tmp_path: Path) -> GuardTree:
    remote = "/var/www/mock-staging"
    local = tmp_path / "tree"
    theme = local / "wp-content" / "themes" / "twentytwentyfive"
    theme.mkdir(parents=True)
    (theme / "front-page.php").write_text(
        "<?php\nadd_filter('template_include', 'azom_front');\nwp_enqueue_style('azom', 'x');\n",
        encoding="utf-8",
    )
    woo = theme / "woocommerce"
    woo.mkdir()
    (woo / "checkout.php").write_text("<?php\n// checkout override\n", encoding="utf-8")
    (theme / "style.css").write_text("/* theme styles */\n", encoding="utf-8")
    (theme / "theme.json").write_text("{\"version\": 3}\n", encoding="utf-8")
    plugin = local / "wp-content" / "plugins" / "woocommerce"
    plugin.mkdir(parents=True)
    (plugin / "woocommerce.php").write_text(
        "<?php\nadd_filter('template_include', 'wc_front');\n", encoding="utf-8"
    )
    templates = theme / "templates"
    templates.mkdir()
    (templates / "front-page.html").write_text("<!-- wp:template-part {\"slug\":\"header\"} /-->\n", encoding="utf-8")
    return GuardTree(remote, local)


def _review(tmp_path: Path, actor: str, **extra):
    client, memory = _client()
    transport = _tree(tmp_path)
    outcome = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor=actor,
        client=client,
        transport=transport,
        registry_path=REGISTRY,
        data_dir=tmp_path / "data",
        **extra,
    )
    return outcome, memory, transport


def test_page_review_intent_does_not_match_orders():
    assert is_page_review("Granska startsidans källkod")
    assert is_page_review("granska startsidan")
    assert not is_page_review("visa order 1001")
    assert not is_page_review("visa sidan om oss")


def test_homepage_review_separates_layers_and_does_not_write(tmp_path: Path):
    outcome, memory, transport = _review(tmp_path, "oscar")
    names = [row["tool"] for row in outcome.steps]
    assert names == list(PAGE_REVIEW_TOOLS)
    assert set(PAGE_REVIEW_TOOLS).isdisjoint(FILE_TOOL_NAMES)
    assert set(PAGE_REVIEW_TOOLS).isdisjoint(ORDER_TOOL_NAMES)
    assert outcome.order_tools_called == ()
    assert outcome.wrote is False
    assert transport.writes == 0
    assert all(call[0] == "GET" for call in memory.calls)
    assert sum(1 for _method, url, _body in memory.calls if url.endswith("/pages/2")) == 1
    content = next(row for row in outcome.steps if row["tool"] == "wp_page_review_content")
    assert content["database"]["editor_raw_present"] is True
    assert content["database"]["stored_rendered_is_dom"] is False
    home = next(row for row in outcome.steps if row["tool"] == "wp_page_review_home")
    assert home["homepage_mode"] == "static_page"
    assert home["page_id"] == 2
    assert home["front_page"]["slug"] == "hem"
    assert home["front_page"]["title"] == "Om oss"
    assert home["front_page"]["template_origin"] == "theme"
    assert home["selected_template_path"] == "themes/twentytwentyfive/templates/front-page.html"
    sources = next(row for row in outcome.steps if row["tool"] == "wp_page_review_sources")
    inventory = {item["path"] for item in sources["source_inventory"]}
    assert "themes/twentytwentyfive/style.css" in inventory
    assert "themes/twentytwentyfive/theme.json" in inventory
    assert "plugins/woocommerce/woocommerce.php" in inventory
    assert sources["active_plugins"] == ["woocommerce"]
    preview = next(row for row in outcome.steps if row["tool"] == "wp_page_review_preview")
    assert preview["status"] == "not_run"
    assert preview["availability"] == "unavailable"
    assert preview["rendered_dom"]["desktop_screenshot"] is None
    assert preview["rendered_dom"]["mobile_screenshot"] is None
    assert preview["rendered_dom"]["console_errors"] is None
    assert preview["rendered_dom"]["network_errors"] is None
    assert "Orderverktyg kördes inte." in outcome.text
    assert "är inte DOM" in outcome.text
    assert "not_run/unavailable" in outcome.text
    assert RAW[:20] in outcome.text
    assert any(
        isinstance(finding.get("path"), str)
        and finding["path"].endswith("checkout.php")
        and finding.get("risk") == "high"
        for row in outcome.steps
        for finding in row.get("findings") or []
    )
    cited = [
        finding
        for row in outcome.steps
        for finding in row.get("findings") or []
        if finding.get("line") == 2
        and finding.get("verification") == "read"
        and str(finding.get("path") or "").endswith("front-page.php")
    ]
    assert cited
    assert all(finding.get("excerpt") == "add_filter('template_include', 'azom_front');" for finding in cited)
    checkout = [
        finding
        for row in outcome.steps
        for finding in row.get("findings") or []
        if str(finding.get("path") or "").endswith("checkout.php")
    ]
    assert checkout
    assert all(finding.get("risk") == "high" and finding.get("excerpt") == "<?php" for finding in checkout)


def test_homepage_review_resolves_posts_index_without_inventing_static_page(tmp_path: Path):
    client, memory = _client()
    memory.settings["show_on_front"] = "posts"
    memory.settings["page_on_front"] = 0
    memory.settings["page_for_posts"] = 2
    outcome = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=_tree(tmp_path),
        registry_path=REGISTRY,
        data_dir=tmp_path / "data",
    )
    home = next(row for row in outcome.steps if row["tool"] == "wp_page_review_home")
    content = next(row for row in outcome.steps if row["tool"] == "wp_page_review_content")
    assert home["homepage_mode"] == "latest_posts"
    assert home["page_id"] is None and home["page_for_posts"] == 2
    assert home["front_page"] is None
    assert content["database"]["kind"] == "posts_index"


def test_parent_theme_text_is_not_reused_for_the_child(tmp_path: Path):
    from ecom_ops.wp_ops.page_review import ReviewContext, _read

    parent = tmp_path / "parent"
    child = tmp_path / "child"
    parent.mkdir()
    child.mkdir()
    (parent / "front-page.php").write_bytes(b"UNIQUE_PARENT\n")
    (child / "front-page.php").write_bytes(b"UNIQUE_CHILD\n")

    class Memory:
        def read_bytes(self, absolute: str) -> bytes:
            return Path(absolute).read_bytes()

    ctx = ReviewContext(site_id="mock-staging", actor_name="oscar", transport=Memory())
    assert _read(ctx, str(parent / "front-page.php"), "front-page.php") == "UNIQUE_PARENT\n"
    assert _read(ctx, str(child / "front-page.php"), "front-page.php") == "UNIQUE_CHILD\n"


def test_viewer_omits_source_and_editor_excerpt(tmp_path: Path):
    outcome, _memory, transport = _review(tmp_path, "jonatan")
    assert transport.writes == 0
    assert RAW not in outcome.text
    assert "template_include" not in outcome.text
    assert "not_run/unavailable" in outcome.text
    assert all(
        finding.get("excerpt") is None and finding.get("line") is None
        for row in outcome.steps
        for finding in row.get("findings") or []
        if finding.get("layer") != "rendered_dom"
    )


def test_missing_front_setting_does_not_guess_a_page(tmp_path: Path):
    memory = InMemoryWpTransport()
    client = WordPressClient(base_url="https://mock.local", transport=memory)
    outcome = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=_tree(tmp_path),
        registry_path=REGISTRY,
        data_dir=tmp_path / "data",
    )
    home = outcome.steps[0]
    assert home["status"] == "not_run"
    assert home["reason"] == "settings_incomplete"
    assert all(not url.endswith("/pages/2") for _method, url, _body in memory.calls)


def test_unbound_rest_does_not_dial(monkeypatch, tmp_path: Path):
    def boom(*_args, **_kwargs):
        raise AssertionError("dialed")

    monkeypatch.setattr("ecom_ops.integrations.wordpress.wp_client_from_env", boom)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("WP_BASE_URL", raising=False)
    monkeypatch.delenv("WOO_BASE_URL", raising=False)
    outcome = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        registry_path=REGISTRY,
        data_dir=tmp_path / "data",
    )
    assert outcome.steps[0]["status"] == "not_run"
    assert outcome.steps[0]["availability"] == "not_bound"


def test_foreign_tools_and_urls_are_refused(tmp_path: Path):
    client, memory = _client()
    outcome = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=_tree(tmp_path),
        registry_path=REGISTRY,
        data_dir=tmp_path / "data",
        attempted=[
            ("lookup_order", {"order_id": "1001"}),
            ("wp_page_review_home", {"site_id": "mock-staging", "url": "https://evil.example/wp-admin"}),
            ("shell", {"command": "wp eval 'x'"}),
        ],
    )
    assert [row["tool"] for row in outcome.refused] == ["lookup_order", "wp_page_review_home", "shell"]
    assert all(row["executed"] is False for row in outcome.refused)
    assert outcome.order_tools_called == ()
    assert "evil.example" not in outcome.text
    assert "1001" not in outcome.text
    assert all("wc/" not in url for _method, url, _body in memory.calls)
    refused = execute_page_review_tool("lookup_order", {"order_id": "1001"}, None)  # type: ignore[arg-type]
    assert refused["executed"] is False
    assert refused["status"] == "not_run"


def test_chat_page_review_skips_order_prefetch(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("order prefetch")

    monkeypatch.setattr("ecom_ops.bot.chat_agent.gather_tool_results", boom)
    monkeypatch.setattr("ecom_ops.bot.chat_agent.tool_lookup_order", boom)
    context = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="",
        site_id="mock-staging",
        operation_ids=(),
    )
    result = run_chat(
        "Granska startsidans källkod https://azom.no/order/1001",
        session={"last_order_id": "1001", "tool_digest": "lookup_order: 1001", "wp_ops_context": context},
        actor="oscar",
        channel="telegram",
        conversation_id="",
        prior_digest="lookup_order: 1001",
    )
    assert result.tool_digest == ""
    assert "1001" not in result.text
    assert "azom.no" not in result.text
    assert "lookup_order" not in result.text
    assert "wp_page_review_home" in result.text
    assert "Orderverktyg kördes inte." in result.text


def test_telegram_job_is_page_review_and_not_chat(monkeypatch, tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    accepted = store.accept_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 5},
                "text": "granska startsidans källkod",
            },
        }
    )
    job = store.claim_next()
    assert accepted["disposition"] == "queued"
    assert job is not None
    assert job["kind"] == "page_review"
    order = store.accept_update(
        {
            "update_id": 2,
            "message": {"message_id": 11, "chat": {"id": 9}, "text": "visa order 1001"},
        }
    )
    other = store.get_job(order["job_id"])
    assert other is not None
    assert other["kind"] == "read_turn"
    steered = store.accept_update(
        {
            "update_id": 3,
            "message": {"message_id": 12, "chat": {"id": 5}, "text": "kolla order 1001"},
        }
    )
    assert steered["disposition"] == "steer"

    class Handler:
        channel = "telegram"

        def __init__(self) -> None:
            self.handled = False
            context = context_for_storage(
                actor="oscar",
                channel="telegram",
                conversation_id="telegram:5",
                site_id="mock-staging",
                operation_ids=(),
            )
            self.store = type("Box", (), {"get": lambda _self, _chat: {"session": {"wp_ops_context": context}}})()

        def _peer_allowed(self, _chat_id: str) -> bool:
            return True

        def _resolve_actor(self, _chat_id: str) -> str:
            return "oscar"

        def handle(self, *_args, **_kwargs):
            self.handled = True
            raise AssertionError("chat")

        def handle_callback(self, *_args, **_kwargs):
            self.handled = True
            raise AssertionError("callback")

    monkeypatch.setattr(
        "ecom_ops.bot.__main__._api",
        lambda *_args, **_kwargs: {"ok": True, "result": {"message_id": 9}},
    )
    from ecom_ops.bot.__main__ import _process_job

    handler = Handler()
    _process_job("token", handler, store, job)
    assert handler.handled is False
    finished = store.get_job(job["job_id"])
    assert finished is not None
    assert "Orderverktyg kördes inte." in finished["result"]["text"]
    assert isinstance(BotReply(text="ok"), BotReply)


def test_page_review_steer_is_not_promoted_to_read_turn(tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    store.accept_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 5},
                "text": "granska startsidans källkod",
            },
        }
    )
    job = store.claim_next()
    assert job is not None and job["kind"] == "page_review"
    steered = store.accept_update(
        {
            "update_id": 2,
            "message": {"message_id": 11, "chat": {"id": 5}, "text": "ja"},
        }
    )
    assert steered["disposition"] == "steer"
    assert store.complete_job(job["job_id"], {"text": "klar"}) == []
    assert store.claim_next() is None


def test_read_turn_steer_still_queues(tmp_path: Path):
    store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    store.accept_update(
        {
            "update_id": 1,
            "message": {"message_id": 10, "chat": {"id": 5}, "text": "hej"},
        }
    )
    job = store.claim_next()
    assert job is not None and job["kind"] == "read_turn"
    steered = store.accept_update(
        {
            "update_id": 2,
            "message": {"message_id": 11, "chat": {"id": 5}, "text": "ja"},
        }
    )
    assert steered["disposition"] == "steer"
    queued = store.complete_job(job["job_id"], {"text": "svar"})
    assert len(queued) == 1
    nxt = store.claim_next()
    assert nxt is not None
    assert nxt["kind"] == "read_turn"
    assert nxt["payload"]["text"] == "ja"


def test_page_review_chat_result_clears_digest_flag(monkeypatch):
    monkeypatch.setattr(
        "ecom_ops.wp_ops.page_review_loop.run_bounded_page_review_loop",
        lambda **_kwargs: type("Outcome", (), {"text": "Rapport"})(),
    )
    result = run_chat(
        "granska startsidans källkod",
        actor="oscar",
        prior_digest="order 1001",
        channel="telegram",
        conversation_id="telegram:5",
    )
    assert result.clear_tool_digest is True
    assert result.tool_digest == ""
    from ecom_ops.bot.handlers import _stored_tool_digest

    assert _stored_tool_digest(result, "order 1001") == ""


def test_page_review_job_clears_saved_order_digest():
    from ecom_ops.bot.handlers import BotHandler
    from ecom_ops.bot.page_review_job import run_page_review_job

    class Store:
        def __init__(self) -> None:
            self.data = {
                "5": {
                    "tool_digest": "order 1001",
                    "session": {"kept": True},
                    "flow": "content_confirm",
                    "messages": [{"role": "user", "content": "hej"}],
                }
            }

        def get(self, chat_id):
            return self.data.get(str(chat_id))

        def set(self, chat_id, state) -> None:
            self.data[str(chat_id)] = state

        def clear(self, chat_id) -> None:
            self.data.pop(str(chat_id), None)

    class Handler:
        channel = "telegram"

        def __init__(self) -> None:
            self.store = Store()

        _merge_state = BotHandler._merge_state

        def _peer_allowed(self, _chat_id: str) -> bool:
            return True

        def _resolve_actor(self, _chat_id: str) -> str:
            return "oscar"

    handler = Handler()
    reply = run_page_review_job(
        handler,
        {"chat_id": "5"},
        progress=lambda _message: None,
    )
    state = handler.store.get("5")
    assert state is not None
    assert state["tool_digest"] == ""
    assert state["session"] == {"kept": True}
    assert state["flow"] == "content_confirm"
    assert state["messages"]
    assert "Välj en WordPress-sajt" in reply.text


def test_stub_checks_are_not_passed_and_do_not_open_preview(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("WP_APP_PASSWORD", "should-not-leak")
    files = [("functions.php", "<?php\n")]
    results = run_catalog("theme", files, "low")
    assert suite_status("theme", files, "low", results) == "checks_incomplete"
    assert all(row["status"] != "passed" for row in results if row["job"] != "woo_static")
    browser = run_job("browser_smoke", "theme", [("style.css", "body{}")], "low")
    assert browser["status"] == "unavailable"
    assert browser["availability"] == "unavailable"
    egress = run_job("egress_probe", "theme", files, "low")
    assert egress["status"] == "unavailable"
    marked = [("functions.php", "<?php function (\n")]
    failed = run_catalog("theme", marked, "low")
    lint = next(row for row in failed if row["job"] == "php_lint")
    if lint["status"] == "failed":
        assert lint["executed"] is True and lint["exit_code"] != 0
        assert suite_status("theme", marked, "low", failed) == "tests_failed"
    else:
        assert lint["status"] == "unavailable"
        assert suite_status("theme", marked, "low", failed) == "checks_incomplete"
    woo = run_job(
        "woo_static",
        "plugin",
        [("azom.php", "<?php\n * WC tested up to: 9.0\nFeaturesUtil::declare_compatibility('custom_order_tables');\n")],
        "high",
    )
    assert woo["status"] == "passed"
    assert woo["declaration_is_not_proof"] is True

    gate = SandboxGate()
    record = gate.start(
        type("Site", (), {"php_version": "8.3", "wordpress_version": "6.7", "woocommerce": False})(),
        "wr-abc",
        high_risk=False,
    )
    assert record["state"] == "unavailable"
    assert record["container"] == "unavailable"
    assert record["env_names"] == ["AZOM_REVIEW_SYNTHETIC"]
    assert "WP_APP_PASSWORD" not in record["env_names"]
    assert synthetic_env() == {"AZOM_REVIEW_SYNTHETIC": "1"}
    monkeypatch.setenv("AZOM_WP_SANDBOX", "1")
    with pytest.raises(Exception) as raised:
        gate.start(
            type("Site", (), {"php_version": "8.3", "wordpress_version": "6.7", "woocommerce": False})(),
            "wr-abc",
            high_risk=False,
        )
    assert type(raised.value).__name__ == "ReviewError"
    assert raised.value.error_class == "SandboxRuntimeUnavailable"

    plans = ReviewPlans(data_dir=tmp_path / "data", registry_path=REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.data["status"] == "checks_incomplete"
    preview = plans.preview("mock-staging", plan_id, actor="oscar")
    assert preview.ok is False
    assert preview.data["error_class"] == "PreviewBlocked"
    plans.place(
        "mock-staging",
        plan_id,
        "functions.php",
        f"<?php function {SYNTAX_MARKER}( {{\n".encode(),
        actor="oscar",
    )
    failed_plan = plans.run_checks("mock-staging", plan_id, actor="oscar")
    expected = "tests_failed" if any(row.get("status") == "failed" for row in failed_plan.data["checks"]) else "checks_incomplete"
    assert failed_plan.data["status"] == expected
    blocked = plans.preview("mock-staging", plan_id, actor="oscar")
    assert blocked.data["error_class"] == "PreviewBlocked"
    audit = "\n".join(json.dumps(row, ensure_ascii=False) for row in read_audit_log(limit=50))
    assert SYNTAX_MARKER not in audit
    shown = plans.show("mock-staging", plan_id, actor="oscar")
    staged = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=shown.data["plan"]["base_sha256"],
        work_sha256=shown.data["plan"]["work_sha256"],
    )
    assert staged.ok is False
    assert staged.data["error_class"] == "TestsNotPassed"

    saved = json.loads((tmp_path / "data" / "wp_review_plans.json").read_text(encoding="utf-8"))
    saved["plans"][0]["sandbox"] = {"state": "created", "container": "running", "plan_id": plan_id}
    (tmp_path / "data" / "wp_review_plans.json").write_text(json.dumps(saved), encoding="utf-8")
    assert plans.note_process_restart(actor="oscar") == 1
    restarted = plans.show("mock-staging", plan_id, actor="oscar")
    assert restarted.data["plan"]["status"] == "sandbox_expired"
    assert restarted.data["plan"]["sandbox"]["container"] == "stopped"


def test_suite_requires_real_process_metadata_for_every_required_pass():
    files = [("index.php", "<?php\n")]
    required = ["php_lint", "wpcs_php", "browser_smoke", "egress_probe"]
    rows = [
        {
            "job": job,
            "status": "passed",
            "command": ["fixed-tool"],
            "started_at": "2026-11-24T00:00:00+00:00",
            "finished_at": "2026-11-24T00:00:01+00:00",
            "exit_code": 0,
            "executed": True,
            "simulated": False,
        }
        for job in required
    ]
    assert suite_status("theme", files, "low", rows) == "tests_passed"
    rows[0]["simulated"] = True
    assert suite_status("theme", files, "low", rows) == "checks_incomplete"
    rows[0]["simulated"] = False
    rows[0].pop("exit_code")
    assert suite_status("theme", files, "low", rows) == "checks_incomplete"


def test_review_modules_do_not_import_a_process_runner():
    for relative in (
        "skills/ecom_ops/wp_ops/review_checks.py",
        "skills/ecom_ops/wp_ops/review_runner.py",
        "skills/ecom_ops/wp_ops/review_plan.py",
        "skills/ecom_ops/wp_ops/page_review.py",
        "skills/ecom_ops/wp_ops/page_review_loop.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "import subprocess" not in source
        assert "import docker" not in source


def test_resolve_site_id_from_text_matches_id_or_host_and_ignores_unknown():
    from ecom_ops.wp_ops.registry import resolve_site_id_from_text

    assert resolve_site_id_from_text("Granska startsidans källkod på mock-staging", path=REGISTRY) == "mock-staging"
    assert resolve_site_id_from_text("Kolla https://mock-shop.example.test/index.php", path=REGISTRY) == "mock-shop"
    assert resolve_site_id_from_text("Kolla https://evil.example/wp-admin", path=REGISTRY) == ""
    assert resolve_site_id_from_text("Jämför mock-shop och mock-staging", path=REGISTRY) == ""
    assert resolve_site_id_from_text("", path=REGISTRY) == ""


def test_page_review_job_auto_resolves_explicit_site_id(monkeypatch):
    from ecom_ops.bot.handlers import BotHandler
    from ecom_ops.bot.page_review_job import run_page_review_job

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    called: list[str] = []

    class Store:
        def __init__(self) -> None:
            self.data = {"5": {"session": {"last_market": "no"}, "messages": []}}

        def get(self, chat_id):
            return self.data.get(str(chat_id))

        def set(self, chat_id, state) -> None:
            self.data[str(chat_id)] = state

    class Handler:
        channel = "telegram"

        def __init__(self) -> None:
            self.store = Store()

        _merge_state = BotHandler._merge_state
        _update_session_atomic = BotHandler._update_session_atomic

        def _peer_allowed(self, _chat_id: str) -> bool:
            return True

        def _resolve_actor(self, _chat_id: str) -> str:
            return "oscar"

    def fake_loop(**kwargs):
        called.append(str(kwargs.get("site_id") or ""))
        return type("Outcome", (), {"text": "Auto OK"})()

    monkeypatch.setattr("ecom_ops.bot.page_review_job.run_bounded_page_review_loop", fake_loop)
    handler = Handler()
    reply = run_page_review_job(
        handler,
        {"chat_id": "5", "payload": {"text": "Granska startsidans källkod på mock-staging"}},
        progress=lambda _msg: None,
    )
    assert reply.text == "Auto OK"
    assert called == ["mock-staging"]
    ctx = handler.store.get("5")["session"]["wp_ops_context"]
    assert ctx["site_id"] == "mock-staging"
    assert ctx["actor_id"] == "oscar"


def _block_theme_tree(tmp_path: Path, slug: str = "twentytwentyfive") -> LocalTree:
    local = tmp_path / "tree"
    theme = local / "wp-content" / "themes" / slug
    (theme / "templates").mkdir(parents=True)
    (theme / "theme.json").write_text('{"version": 3}\n', encoding="utf-8")
    (theme / "style.css").write_text("/* block theme */\n", encoding="utf-8")
    (theme / "templates" / "front-page.html").write_text("<!-- wp:paragraph /-->\n", encoding="utf-8")
    (theme / "templates" / "page.html").write_text("<!-- wp:post-content /-->\n", encoding="utf-8")
    return LocalTree("/var/www/mock-staging", local)


def test_page_review_target_intent_loop_and_site_editor(
    tmp_path: Path, monkeypatch
) -> None:
    from ecom_ops.wp_ops.page_review_intent import extract_page_review_target, is_page_review

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    assert is_page_review("granska startsidans källkod") is True
    assert extract_page_review_target("granska startsidans källkod") is None
    assert is_page_review("granska sidan om-oss källkod") is True
    assert extract_page_review_target("granska sidan om-oss källkod") == "om-oss"
    assert is_page_review("granska sidan 2 källkod") is True
    assert extract_page_review_target("granska sidan 2 källkod") == "2"
    assert is_page_review("visa sidan om-oss") is False
    assert extract_page_review_target("visa sidan om-oss") is None

    memory = InMemoryWpTransport()
    memory.settings["show_on_front"] = "page"
    memory.settings["page_on_front"] = 2
    memory.posts[2]["slug"] = "om-oss"
    memory.posts[2]["name"] = "om-oss"
    memory.posts[2]["content"] = {"rendered": "<p>Azom AB</p>", "raw": "<!-- wp:paragraph --><p>Om oss</p>"}
    memory.themes["twentytwentyfive"]["template"] = "twentytwentyfive"
    client = WordPressClient(base_url="https://mock.local", transport=memory)
    transport = _block_theme_tree(tmp_path)

    # Empty templates -> site_editor == "unavailable"
    by_slug = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=transport,
        registry_path=REGISTRY,
        data_dir=tmp_path,
        target_page="om-oss",
    )
    home_slug = by_slug.steps[0]
    assert home_slug["status"] == "read"
    assert home_slug["page_id"] == 2
    assert home_slug["front_page"]["slug"] == "om-oss"
    assert home_slug["theme_kind"] == "block"
    assert home_slug["site_editor"] == "unavailable"
    assert home_slug["site_editor_templates"] == []

    # Populated templates -> site_editor == "read" + site_editor_templates, and numeric target_page="2"
    memory.templates = [
        {"id": "twentytwentyfive//front-page", "slug": "front-page"},
        {"id": "twentytwentyfive//page", "slug": "page"},
    ]
    by_id = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=transport,
        registry_path=REGISTRY,
        data_dir=tmp_path,
        target_page="2",
    )
    home_id = by_id.steps[0]
    assert home_id["status"] == "read"
    assert home_id["page_id"] == 2
    assert home_id["site_editor"] == "read"
    assert home_id["site_editor_templates"] == ["front-page", "page"]

    # Unknown target slug -> target_page_not_found
    unknown = run_bounded_page_review_loop(
        site_id="mock-staging",
        actor="oscar",
        client=client,
        transport=transport,
        registry_path=REGISTRY,
        data_dir=tmp_path,
        target_page="finns-inte",
    )
    assert unknown.steps[0]["status"] == "not_run"
    assert unknown.steps[0]["reason"] == "target_page_not_found"
