"""Regression tests for WP review workspace, staging, search redaction, and tool-loop budgets."""

from __future__ import annotations

import json
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _helpers import (
    MOCK_REGISTRY,
    REPO_ROOT,
    mock_wp_content,
    passing_catalog,
    tool_call,
    write_plugin_file,
    write_theme_file,
)

from ecom_ops.bot import (
    chat_agent,
    site_content,
)
from ecom_ops.bot.chat_agent import (
    WP_CODE_INTENT_RE,
    _use_file_tool_loop,
    _use_read_tool_loop,
    gather_tool_results,
    run_chat,
)
from ecom_ops.bot.site_content import site_content_turn
from ecom_ops.integrations.wp_files_ssh import SshFileTransport
from ecom_ops.model_router import reset_model_router
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.wp_ops import review_plan as review_plan_mod
from ecom_ops.wp_ops.agent_tools import (
    ALL_FILE_TOOLS,
    FILE_LOOP_MAX_CALLS,
    FILE_LOOP_MAX_READS,
    FILE_LOOP_MAX_SEARCHES,
    FILE_LOOP_MAX_SECONDS,
    WpAgentTools,
    context_for_storage,
    run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.file_changes import search_texts
from ecom_ops.wp_ops.files import LocalTree
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.review_plan import ReviewPlans
from ecom_ops.wp_ops.review_workspace import (
    VerifiedTheme,
    workspace_root,
)
from ecom_ops.wp_ops.service import WpOpsService


def _setup_mock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
    monkeypatch.setenv("XAI_GROK_BIN", "")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()
    reset_model_router()


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WpOpsService:
    _setup_mock_env(tmp_path, monkeypatch)
    return WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)


def _tools(service: WpOpsService) -> WpAgentTools:
    return WpAgentTools(service)


def test_bug1_open_review_workspace_mock_local_tree_fallback_and_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    write_theme_file(
        tmp_path, "mock-staging", "parent-base", "style.css", "/*\nTheme Name: Parent Base\n*/\n"
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "functions.php",
        "<?php\n// populated from LocalTree\n",
    )
    write_theme_file(
        tmp_path, "mock-staging", "solo-theme", "style.css", "/*\nTheme Name: Solo Theme\n*/\n"
    )
    write_plugin_file(tmp_path, "mock-staging", "third-party", "third-party.php", "<?php\n")
    write_plugin_file(tmp_path, "mock-staging", "woocommerce", "woocommerce.php", "<?php\n")

    child = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child"},
        actor="oscar",
    )
    assert child.data["ok"] is True
    assert child.data["status"] == "validated"
    assert child.data["theme_relationship"] == "child"
    ws_base = workspace_root(tmp_path, "mock-staging", child.data["plan_id"]) / "base"
    assert "populated from LocalTree" in (ws_base / "functions.php").read_text(encoding="utf-8")

    solo = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "solo-theme"},
        actor="oscar",
    )
    assert solo.data["ok"] is True
    assert solo.data["status"] == "validated"
    assert solo.data["theme_relationship"] == "no_child"

    parent = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "parent-base"},
        actor="oscar",
    )
    assert parent.data["ok"] is False
    assert parent.data["error_class"] == "ParentThemeBlocked"

    third = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin", "slug": "third-party"},
        actor="oscar",
    )
    assert third.data["ok"] is False
    assert third.data["error_class"] == "ThirdPartyPluginRefused"

    woo = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "plugin", "slug": "woocommerce"},
        actor="oscar",
    )
    assert woo.data["ok"] is False
    assert woo.data["error_class"] == "WooCoreRefused"


def test_bug2_approve_staging_succeeds_on_created_and_parent_copied_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    write_theme_file(
        tmp_path, "mock-staging", "parent-base", "archive.php", "<?php\n// parent archive\n"
    )
    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n",
    )

    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)
    opened = plans.open(
        "mock-staging",
        "theme",
        "azom-child",
        actor="oscar",
        verified_theme=VerifiedTheme(stylesheet="azom-child", template="parent-base"),
    )
    assert opened.ok is True
    plan_id = opened.data["plan_id"]

    assert (
        plans.create_file(
            "mock-staging", plan_id, "includes/badge.php", "<?php\n// new badge\n", actor="oscar"
        ).ok
        is True
    )
    assert (
        plans.copy_from_parent(
            "mock-staging", plan_id, "archive.php", actor="oscar", parent_slug="parent-base"
        ).ok
        is True
    )

    monkeypatch.setattr(review_plan_mod, "run_catalog", passing_catalog)
    checked = plans.run_checks("mock-staging", plan_id, actor="oscar")
    assert checked.ok is True
    assert checked.data["status"] == "tests_passed"

    row = plans.get(plan_id, site_id="mock-staging")
    assert row is not None
    staged = plans.approve_staging(
        "mock-staging",
        plan_id,
        actor="oscar",
        base_sha256=row["base_sha256"],
        work_sha256=row["work_sha256"],
    )
    assert staged.ok is True
    assert staged.data["status"] == "staged"
    assert staged.data["executed"] is True
    theme_dir = mock_wp_content(tmp_path, "mock-staging") / "themes" / "azom-child"
    assert (theme_dir / "includes" / "badge.php").read_text(encoding="utf-8") == (
        "<?php\n// new badge\n"
    )
    assert (theme_dir / "archive.php").read_text(encoding="utf-8") == "<?php\n// parent archive\n"


def test_bug3_workspace_run_checks_summary_findings_no_artifact_path_and_stage_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)

    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "style.css",
        "/*\nTheme Name: Azom Child\nTemplate: parent\n*/\n",
    )
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child"},
        actor="oscar",
    )
    plan_id = opened.data["plan_id"]
    assert (
        tools.execute(
            "wp_workspace_create_file",
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "path": "includes/woo-badge.php",
                "content": "<?php\n// shipping helper without HPOS declaration\n",
            },
            actor="oscar",
        ).data["ok"]
        is True
    )

    incomplete = tools.execute(
        "wp_workspace_run_checks",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="oscar",
    )
    assert incomplete.data["ok"] is True
    assert incomplete.review_stage_plan_id is None
    assert incomplete.data["staging_ready"] is False
    assert incomplete.data["summary"]
    assert "wc_tested_up_to" in incomplete.data["findings"]
    assert "artifact_path" not in json.dumps(incomplete.data)

    opened2 = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Color tweak"},
        actor="oscar",
    )
    plan2 = opened2.data["plan_id"]
    assert (
        tools.execute(
            "wp_workspace_create_file",
            {
                "site_id": "mock-staging",
                "plan_id": plan2,
                "path": "includes/badge.php",
                "content": "<?php\n// badge\n",
            },
            actor="oscar",
        ).data["ok"]
        is True
    )

    monkeypatch.setattr(review_plan_mod, "run_catalog", passing_catalog)
    passed = tools.execute(
        "wp_workspace_run_checks",
        {"site_id": "mock-staging", "plan_id": plan2},
        actor="oscar",
    )
    assert passed.data["ok"] is True
    assert passed.data["status"] == "tests_passed"
    assert passed.data["staging_ready"] is True
    assert passed.review_stage_plan_id == plan2
    assert "artifact_path" not in json.dumps(passed.data)
    assert passed.data["summary"].startswith("tests_passed:")


def test_bug4_list_entries_distinguishes_files_and_dotted_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    site_root = mock_wp_content(tmp_path, "mock-staging") / "themes" / "azom-child"
    (site_root / "inc").mkdir(parents=True)
    (site_root / "release.v2").mkdir(parents=True)
    for fname in ("Makefile", "style.scss", "README.md", "sv_SE.po", "theme.pot", "style.css"):
        (site_root / fname).write_text("ok\n", encoding="utf-8")

    local_tree = LocalTree("/var/www/mock-staging", tmp_path / "wp_files_mock" / "mock-staging")
    entries = dict(local_tree.list_entries("wp-content/themes/azom-child"))
    assert entries == {
        "Makefile": False,
        "README.md": False,
        "inc": True,
        "release.v2": True,
        "style.css": False,
        "style.scss": False,
        "sv_SE.po": False,
        "theme.pot": False,
    }

    listed = service.list_component_tree("mock-staging", "theme", "azom-child", "", actor="jonatan")
    assert listed.ok is True
    kinds = {row["name"]: row["kind"] for row in listed.data["entries"]}
    assert kinds["Makefile"] == "file"
    assert kinds["style.scss"] == "file"
    assert kinds["README.md"] == "file"
    assert kinds["sv_SE.po"] == "file"
    assert kinds["theme.pot"] == "file"
    assert kinds["inc"] == "directory"
    assert kinds["release.v2"] == "directory"

    site = parse_site(
        {
            "id": "stage",
            "display_name": "Stage",
            "canonical_url": "https://stage.example.test/",
            "environment": "staging",
            "business_owner": "customer",
            "hosting_provider": "unconfigured",
            "wordpress_version": "6.7",
            "php_version": "8.3",
            "woocommerce": False,
            "critical_urls": ["https://stage.example.test/"],
            "maintenance_window": "not agreed",
            "secret_ref": "WP_SECRET_STAGE",
            "alert_owner": "unassigned",
            "alert_confirmations": 2,
            "check_interval_seconds": 60,
            "timeout_seconds": 10,
            "dedupe_window_seconds": 30,
            "capabilities": {"uptime": "available"},
            "files_root": "/var/www/stage",
            "ssh_host": "stage.internal",
            "ssh_user": "azom-wp-reader",
        }
    )
    ssh_transport = SshFileTransport(site, tmp_path)

    class _FakeSftp:
        def listdir_attr(self, _path: str):
            return [
                SimpleNamespace(filename="Makefile", st_mode=stat.S_IFREG | 0o644),
                SimpleNamespace(filename="release.v2", st_mode=stat.S_IFDIR | 0o755),
                SimpleNamespace(filename="style.scss", st_mode=stat.S_IFREG | 0o644),
                SimpleNamespace(filename="sym", st_mode=stat.S_IFLNK | 0o777),
                SimpleNamespace(filename=".env", st_mode=stat.S_IFREG | 0o600),
            ]

    @contextmanager
    def _fake_session():
        yield _FakeSftp()

    monkeypatch.setattr(ssh_transport, "_active_session", _fake_session)
    monkeypatch.setattr(SshFileTransport, "_check_remote_path", staticmethod(lambda *_a, **_k: None))
    ssh_entries = ssh_transport.list_entries("wp-content/themes/azom-child")
    assert ssh_entries == [
        ("Makefile", False),
        ("release.v2", True),
        ("style.scss", False),
    ]


def test_bug5_search_redacts_secrets_in_excerpts_and_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "bound_site_cred_999")

    s1 = "sk_live_" + "SearchSecret8877"
    s2 = "pass1234secret"
    s3 = "Bearer bearerTok889900"
    res = search_texts(
        {
            "cfg.php": (
                f"$marker_uri = 'https://user:{s2}@api.example.test';\n"
                f"// azom_needle line with {s1} and {s3}\n"
                "define('DB_PASSWORD', 'bound_site_cred_999');\n"
            )
        },
        "azom_needle",
        context_lines=1,
        extra_secrets=["bound_site_cred_999"],
    )
    assert len(res["hits"]) == 1
    dumped = json.dumps(res)
    for secret in (s1, s2, s3, "bound_site_cred_999"):
        assert secret not in dumped

    write_theme_file(
        tmp_path,
        "mock-staging",
        "azom-child",
        "inc/creds.php",
        f"<?php\n// azom_search_hit\n$api_key = '{s1}';\n$auth = '{s3}';\n",
    )
    found = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "query": "azom_search_hit",
            "context_lines": 2,
        },
        actor="oscar",
    )
    assert found.data["ok"] is True
    assert len(found.data["hits"]) == 1
    found_json = json.dumps(found.data)
    assert s1 not in found_json
    assert s3 not in found_json
    assert "[REDACTED]" in found_json


def test_bug6_file_tool_loop_caps_budget_complete_drift_reads_and_16000_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert FILE_LOOP_MAX_CALLS == 16
    assert FILE_LOOP_MAX_READS == 10
    assert FILE_LOOP_MAX_SEARCHES == 5
    assert FILE_LOOP_MAX_SECONDS == 60.0

    service = _service(tmp_path, monkeypatch)
    tools = _tools(service)
    write_theme_file(
        tmp_path, "mock-staging", "azom-child", "style.css", "/*\nTheme Name: Azom Child\n*/\n"
    )
    write_theme_file(
        tmp_path, "mock-staging", "azom-child", "functions.php", "<?php\n// azom_func\n"
    )

    long_prefix = "A" * 5000
    initial = long_prefix + tool_call(
        "wp_theme_overview", {"site_id": "mock-staging", "slug": "azom-child"}
    )
    captured_msgs: list[list[dict[str, str]]] = []
    followups = iter(
        [
            tool_call("wp_site_status", {"site_id": "mock-staging"}),
            "Tema och sajtstatus hämtades.",
        ]
    )

    def complete_drift(msgs: list[dict[str, str]]) -> tuple[str, float]:
        captured_msgs.append([dict(m) for m in msgs])
        return next(followups), 0.001

    drift_res = run_bounded_file_tool_loop(
        initial,
        [{"role": "user", "content": "kolla temat och sajtens hälsa"}],
        complete=complete_drift,
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert drift_res.handled is True
    assert drift_res.calls == 2
    assert drift_res.results[0]["status"] == "overview"
    assert drift_res.results[1]["site_id"] == "mock-staging"
    assistant_msgs = [m["content"] for m in captured_msgs[0] if m["role"] == "assistant"]
    assert len(assistant_msgs[0]) > 4500

    huge = "B" * 17000 + tool_call("wp_playbook", {"topic": "child_theme_edit"})
    huge_seen: list[list[dict[str, str]]] = []

    def complete_huge(msgs: list[dict[str, str]]) -> tuple[str, float]:
        huge_seen.append([dict(m) for m in msgs])
        return "Klart.", 0.0

    run_bounded_file_tool_loop(
        huge,
        [],
        complete=complete_huge,
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert len([m["content"] for m in huge_seen[0] if m["role"] == "assistant"][0]) == 16000

    budget_calls = {"n": 0}

    def complete_calls(msgs: list[dict[str, str]]) -> tuple[str, float]:
        budget_calls["n"] += 1
        if budget_calls["n"] <= FILE_LOOP_MAX_CALLS:
            return tool_call("wp_playbook", {"topic": "child_theme_edit"}), 0.0
        assert any(
            "Verktygsbudgeten är nådd" in m["content"] for m in msgs if m["role"] == "system"
        )
        return "Sammanfattning av alla 16 anrop.", 0.0

    cap_res = run_bounded_file_tool_loop(
        tool_call("wp_playbook", {"topic": "child_theme_edit"}),
        [],
        complete=complete_calls,
        actor="jonatan",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert cap_res.handled is True
    assert cap_res.calls == FILE_LOOP_MAX_CALLS
    assert len(cap_res.results) == FILE_LOOP_MAX_CALLS
    assert budget_calls["n"] == FILE_LOOP_MAX_CALLS + 1
    assert "Sammanfattning av alla 16 anrop." in cap_res.content

    read_calls = {"n": 0}
    read_call = tool_call(
        "wp_read_theme_plugin_file",
        {
            "site_id": "mock-staging",
            "kind": "theme",
            "slug": "azom-child",
            "path": "functions.php",
        },
    )

    def complete_reads(_msgs: list[dict[str, str]]) -> tuple[str, float]:
        read_calls["n"] += 1
        if read_calls["n"] <= FILE_LOOP_MAX_READS:
            return read_call, 0.0
        return "Sammanfattning efter läsgräns.", 0.0

    read_res = run_bounded_file_tool_loop(
        read_call,
        [],
        complete=complete_reads,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert read_res.calls == FILE_LOOP_MAX_READS
    assert read_calls["n"] == FILE_LOOP_MAX_READS + 1
    assert "läsgräns" in read_res.content

    search_calls = {"n": 0}
    search_call = tool_call(
        "wp_search_component",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "query": "azom_func"},
    )

    def complete_searches(_msgs: list[dict[str, str]]) -> tuple[str, float]:
        search_calls["n"] += 1
        if search_calls["n"] <= FILE_LOOP_MAX_SEARCHES:
            return search_call, 0.0
        return "Sammanfattning efter sökgräns.", 0.0

    search_res = run_bounded_file_tool_loop(
        search_call,
        [],
        complete=complete_searches,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=tools,
    )
    assert search_res.calls == FILE_LOOP_MAX_SEARCHES
    assert search_calls["n"] == FILE_LOOP_MAX_SEARCHES + 1
    assert "sökgräns" in search_res.content


def test_bug7_and_bug8_chat_agent_and_site_content_wp_code_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    monkeypatch.delenv("AZOM_TELEGRAM_MODEL_ROUTE", raising=False)
    reset_model_router()

    assert site_content_turn("ta bort hook i temat", allow_wp_ops_fallthrough=True).consumed is False
    assert (
        site_content_turn("jämför header.php och footer.php", allow_wp_ops_fallthrough=True).consumed
        is False
    )
    refused_switch = site_content_turn("byt tema till storefront", allow_wp_ops_fallthrough=True)
    assert refused_switch.consumed is True
    assert "Temabyte" in refused_switch.results[0][1]

    for prompt in (
        "ta bort hook i temat",
        "jämför header.php och footer.php",
        "ändra hook på produktsidan i single-product.php",
    ):
        assert WP_CODE_INTENT_RE.search(prompt)
        pref = gather_tool_results(prompt, has_wp_site_context=True)
        assert pref.results == []
        assert pref.pending_action is None
        assert _use_read_tool_loop(prompt, pref, has_wp_site_context=True) is False

    for name in ALL_FILE_TOOLS:
        assert _use_file_tool_loop(tool_call(name, {})) is True

    assert chat_agent.WP_CODE_INTENT_RE is WP_CODE_INTENT_RE
    assert hasattr(site_content, "_WP_CODE_FALLTHROUGH_RE")

    ctx = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        site_id="mock-staging",
        operation_ids=(),
    )
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            tool_call("wp_playbook", {"topic": "child_theme_edit"}),
            "Vi ändrar detta säkert via barntemat i en wr-arbetsyta.",
        ]
    )

    def fake_chat(messages, **kwargs):
        calls.append({"messages": list(messages), **kwargs})
        return next(responses), 0.001

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)

    res = run_chat(
        "ta bort hook i temat",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        session={"wp_ops_context": ctx},
    )
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 4096
    assert calls[1]["max_tokens"] == 4096
    assert "barntemat" in res.text

    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.chat_completion",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("LLM must not run on theme switch")
        ),
    )
    refused_chat = run_chat(
        "byt tema till storefront",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        session={"wp_ops_context": ctx},
    )
    assert "Temabyte" in refused_chat.text


def test_bug9_rejected_review_open_leaves_no_dangling_workspace_or_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    plans = ReviewPlans(data_dir=tmp_path, registry_path=MOCK_REGISTRY)

    for res in (
        plans.open("mock-staging", "plugin", "woocommerce", actor="oscar"),
        plans.open("mock-staging", "plugin", "hello-dolly", actor="oscar"),
        plans.open(
            "mock-staging",
            "theme",
            "twentytwentyfive",
            actor="oscar",
            verified_theme=VerifiedTheme(stylesheet="azom-child", template="twentytwentyfive"),
        ),
    ):
        assert res.ok is False

    assert not (tmp_path / "wp_workspaces").exists()
    assert not (tmp_path / "wp_review_plans.json").exists()


def test_bug10_drift_followup_completion_passes_wp_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_mock_env(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-key")
    monkeypatch.delenv("AZOM_TELEGRAM_MODEL_ROUTE", raising=False)
    reset_model_router()

    ctx = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        site_id="mock-staging",
        operation_ids=(),
    )
    max_tokens_seen: list[int | None] = []
    responses = iter(
        [
            tool_call("wp_site_status", {"site_id": "mock-staging"}),
            "mock-staging har status ok.",
        ]
    )

    def fake_chat(_messages, **kwargs):
        max_tokens_seen.append(kwargs.get("max_tokens"))
        return next(responses), 0.001

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat)

    res = run_chat(
        "vad är sajtens status?",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        session={"wp_ops_context": ctx},
    )
    assert "mock-staging" in res.text
    assert max_tokens_seen == [2048, 2048]
