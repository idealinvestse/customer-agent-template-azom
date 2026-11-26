"""Cross-layer harmonization, packaging, cold-start import, and e2e safety tests."""

from __future__ import annotations

import ast
import collections
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from _helpers import MOCK_REGISTRY, REPO_ROOT, dashboard_auth_headers, load_dashboard_app

from ecom_ops import cli as ecom_cli
from ecom_ops.bot import chat_agent, wp_intents, wp_session, wp_turn
from ecom_ops.integrations import (
    wp_browser_smoke,
    wp_component_reads,
    wp_files_ssh,
    wp_live_preview,
    wp_local_runtime,
    wp_org_client,
    wp_review_process,
    wp_runtime_fetch,
)
from ecom_ops.integrations.wp.sandbox_html import build_sandbox_guard_script
from ecom_ops.integrations.wp.sftp_transport import SshFileTransport
from ecom_ops.wp_ops import (
    agent_tools,
    file_changes,
    file_paths,
    files,
    html_sanitize,
    page_review,
    page_review_intent,
    page_review_loop,
    preview_overlay,
    registry,
    review_checks,
    review_knowledge,
    review_plan,
    review_runner,
    review_workspace,
    service,
)
from ecom_ops.wp_ops.files import TransportError
from ecom_ops.wp_ops.tools import registry as tools_registry


def _run_cold_python(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AZOM_USE_MOCK"] = "1"
    env["AZOM_WP_FILES_SSH"] = "0"
    skills_dir = str(REPO_ROOT / "skills")
    dash_dir = str(REPO_ROOT / "infrastructure" / "dashboard")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (skills_dir, dash_dir, existing) if p
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cold_start_isolated_imports_and_fetch_script_cli() -> None:
    targets = (
        "ecom_ops.wp_ops",
        "ecom_ops.wp_ops.service",
        "ecom_ops.wp_ops.agent_tools",
        "ecom_ops.wp_ops.review_plan",
        "ecom_ops.wp_ops.review_workspace",
        "ecom_ops.integrations.wp_local_runtime",
        "ecom_ops.integrations.wp_runtime_fetch",
        "ecom_ops.integrations.wp_review_process",
        "ecom_ops.integrations.wp_live_preview",
        "ecom_ops.integrations.wp_browser_smoke",
        "ecom_ops.integrations.wp_files_ssh",
        "ecom_ops.integrations.wp.process_env",
        "ecom_ops.integrations.wp.php_lint",
        "ecom_ops.integrations.wp.local_runtime",
        "ecom_ops.integrations.wp.runtime_fetch",
        "ecom_ops.bot.chat_agent",
        "ecom_ops.bot.handlers",
        "ecom_ops.bot.wp_turn",
        "ecom_ops.bot.wp_handlers",
        "ecom_ops.bot.wp_session",
        "ecom_ops.bot.wp_intents",
    )
    script = (
        "import importlib, subprocess, sys\n"
        f"targets = {list(targets)!r}\n"
        "for mod in targets:\n"
        "    p = subprocess.run([sys.executable, '-c', f'import {mod}'], capture_output=True, text=True)\n"
        "    if p.returncode != 0:\n"
        "        raise SystemExit(f'{mod}: {p.stderr.strip()}')\n"
        "from ecom_ops.wp_ops import WpOpsService\n"
        "assert WpOpsService.__name__ == 'WpOpsService'\n"
    )
    proc = _run_cold_python(script)
    assert proc.returncode == 0, proc.stderr or proc.stdout

    # Minimal wp_ops.__init__ and integrations.wp.__init__ must stay lazy / import-free.
    minimal_check = _run_cold_python(
        "import sys; import ecom_ops.wp_ops; "
        "assert 'ecom_ops.wp_ops.service' not in sys.modules; "
        "assert 'ecom_ops.wp_ops.agent_tools' not in sys.modules; "
        "import ecom_ops.integrations.wp; "
        "assert 'paramiko' not in sys.modules; "
        "assert 'ecom_ops.integrations.wp.sftp_transport' not in sys.modules"
    )
    assert minimal_check.returncode == 0, minimal_check.stderr
    wp_init_tree = ast.parse(
        (REPO_ROOT / "skills" / "ecom_ops" / "integrations" / "wp" / "__init__.py").read_text(encoding="utf-8")
    )
    assert not [n for n in wp_init_tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]

    # bin/fetch-wp-runtime.py must launch cleanly from cold start.
    cli_proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "fetch-wp-runtime.py"), "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert cli_proc.returncode == 0, cli_proc.stderr


def test_wp_module_graph_acyclic_and_all_exports_resolve() -> None:
    def mod_for(path: Path) -> str:
        rel = path.relative_to(REPO_ROOT)
        parts = list(rel.parts[1:] if rel.parts[0] == "skills" else rel.parts[2:])
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][:-3]
        return ".".join(parts)

    py_files = sorted(
        list((REPO_ROOT / "skills" / "ecom_ops" / "wp_ops").rglob("*.py"))
        + list((REPO_ROOT / "skills" / "ecom_ops" / "integrations" / "wp").rglob("*.py"))
        + list((REPO_ROOT / "skills" / "ecom_ops" / "bot").glob("wp_*.py"))
        + list((REPO_ROOT / "infrastructure" / "dashboard" / "wp").rglob("*.py"))
    )
    mods = {mod_for(path): path for path in py_files}
    assert len(mods) >= 50

    graph: dict[str, set[str]] = collections.defaultdict(set)
    for mod_name, path in mods.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in mods:
                        graph[mod_name].add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    base = mod_name.split(".") if path.name == "__init__.py" else mod_name.split(".")[:-1]
                    prefix = base[: len(base) - (node.level - 1)]
                    target = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                if target in mods:
                    graph[mod_name].add(target)
                for alias in node.names:
                    cand = f"{target}.{alias.name}" if target else alias.name
                    if cand in mods:
                        graph[mod_name].add(cand)

    white, gray, black = 0, 1, 2
    color = {m: white for m in mods}
    cycles: list[list[str]] = []
    stack: list[str] = []

    def dfs(node_name: str) -> None:
        color[node_name] = gray
        stack.append(node_name)
        for dep in sorted(graph[node_name]):
            if color.get(dep) == gray:
                idx = stack.index(dep)
                cycles.append(stack[idx:] + [dep])
            elif color.get(dep) == white:
                dfs(dep)
        stack.pop()
        color[node_name] = black

    for mod_name in sorted(mods):
        if color[mod_name] == white:
            dfs(mod_name)
    assert cycles == []

    dash_dir = str(REPO_ROOT / "infrastructure" / "dashboard")
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    for mod_name in sorted(mods):
        module = importlib.import_module(mod_name)
        for exported in getattr(module, "__all__", ()):
            assert hasattr(module, exported), f"{mod_name}.__all__ missing {exported}"


def test_cross_surface_single_source_of_truth_and_packaging() -> None:
    expected_all = set(tools_registry.WP_TOOL_NAMES) | set(tools_registry.ALL_FILE_TOOLS)
    assert len(expected_all) == 60
    assert file_changes.FILE_TOOL_NAMES == file_paths.FILE_TOOL_NAMES
    assert set(file_changes.FILE_TOOL_NAMES) == set(tools_registry.FILE_TOOL_NAMES)
    assert agent_tools.WP_TOOL_SCHEMAS == tools_registry.WP_TOOL_SCHEMAS
    assert agent_tools.FILE_TOOL_SCHEMAS == tools_registry.FILE_TOOL_SCHEMAS

    assert set(chat_agent._WP_TOOL_LABELS) == expected_all
    caps_text = chat_agent.tool_capabilities()
    for tool_name in sorted(expected_all):
        assert tool_name in caps_text

    tpl_dir = REPO_ROOT / "skills" / "ecom_ops" / "integrations" / "wp" / "templates"
    found_templates = {p.relative_to(tpl_dir).as_posix() for p in tpl_dir.rglob("*") if p.is_file()}
    assert found_templates == {
        "db_dropin.php",
        "egress_guard.php",
        "router.php.tmpl",
        "synthetic_theme/functions.php",
        "synthetic_theme/index.php",
        "synthetic_theme/style.css",
        "wp_config.php.tmpl",
    }
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"ecom_ops.integrations.wp"' in pyproject
    assert '"templates/*.php"' in pyproject

    wp_ops_doc = (REPO_ROOT / "docs" / "WORDPRESS_OPS.md").read_text(encoding="utf-8")
    assert "tests/test_wp_" not in wp_ops_doc
    assert "tests/wp/test_wp_runtime_e2e.py" in wp_ops_doc


def test_compatibility_facades_and_safety_contracts(tmp_path: Path, wp_env) -> None:
    wp_env(mock="1", files_ssh="0", data_dir=tmp_path)

    facade_attrs = (
        (agent_tools, ("WpAgentTools", "WP_TOOL_MAX_SECONDS", "run_bounded_file_tool_loop", "run_bounded_wp_tool_loop")),
        (service, ("WpOpsService", "WpOpsResult", "_site_lock")),
        (files, ("FileEditor", "PathRejected", "TransportError")),
        (file_changes, ("FileChangeOps", "FILE_TOOL_NAMES", "rest_reads_allowed", "is_safe_pure_css_file")),
        (review_plan, ("ReviewPlans", "run_catalog", "_safe_preview_html_path")),
        (review_workspace, ("ReviewError", "VerifiedTheme", "VerifiedPlugin", "workspace_root")),
        (review_knowledge, ("template_candidates", "hierarchy_candidates")),
        (review_checks, ("JOBS", "requirements", "run_catalog")),
        (review_runner, ("SandboxGate", "public_sandbox_record")),
        (preview_overlay, ("apply_overlay", "compose_standalone_page", "sanitize_page_draft_html")),
        (page_review, ("ReviewContext", "prepare_context", "step_home")),
        (page_review_loop, ("PAGE_REVIEW_TOOLS", "execute_page_review_tool", "run_bounded_page_review_loop")),
        (page_review_intent, ("is_page_review", "extract_page_review_target")),
        (wp_live_preview, ("fetch_live_html", "capture_preview", "inspect_element", "BLOCKED_WP_PATHS")),
        (wp_local_runtime, ("LocalWordPressSandbox", "resolve_local_runtime", "build_php_child_env")),
        (wp_browser_smoke, ("run_browser_render", "run_vertical_theme_render", "run_vertical_plugin_smoke")),
        (wp_files_ssh, ("SshFileTransport", "build_file_transport", "host_allowed")),
        (wp_review_process, ("run_php_lint", "resolve_php_binary")),
        (wp_runtime_fetch, ("check_runtime", "install", "WORDPRESS_SHA1")),
        (wp_org_client, ("fetch_wporg_json",)),
        (wp_component_reads, ("wp_client_for_site", "load_theme_rows", "load_plugin_rows")),
    )
    for mod, names in facade_attrs:
        for name in names:
            assert hasattr(mod, name), f"{mod.__name__} missing {name}"

    dash_dir = str(REPO_ROOT / "infrastructure" / "dashboard")
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    wp_routes_mod = importlib.import_module("wp_routes")
    assert hasattr(wp_routes_mod, "register_wp_routes")

    svc = service.WpOpsService(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    restored = svc.restore("mock-staging", actor="oscar", change_ref="bk-1")
    assert restored.ok is False and restored.data["error_class"] == "RestoreNotInMvp"

    plans = review_plan.ReviewPlans(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    nl_denied = plans.decide_text("ja godkänn", actor="oscar")
    assert nl_denied.ok is False and nl_denied.data["error_class"] == "TextNotApproval"

    site_obj = registry.site_by_id(registry.load_sites(MOCK_REGISTRY, data_dir=tmp_path), "mock-staging")
    assert site_obj is not None
    sftp = SshFileTransport(site_obj, tmp_path)
    assert sftp.is_ssh is True
    try:
        sftp.create_bytes("/var/www/mock-staging/wp-content/themes/x/style.css", b"body{}")
    except TransportError as exc:
        assert exc.error_class == "SshWriteDisabled"
    else:
        raise AssertionError("SshFileTransport.create_bytes must raise TransportError('SshWriteDisabled')")

    wp_ops_dir = REPO_ROOT / "skills" / "ecom_ops" / "wp_ops"
    for leaf in ("config.py", "models.py", "locks.py", "pii.py", "credentials.py", "file_policy.py", "html_sanitize.py", "source_scan.py"):
        text = (wp_ops_dir / leaf).read_text(encoding="utf-8")
        assert "integrations." not in text
        for banned in ("urllib.request", "urlopen", "requests", "http.client", "subprocess"):
            assert banned not in text


def test_end_to_end_wp_ops_harmonized_workflow(
    tmp_path: Path,
    monkeypatch,
    capsys,
    wp_service_tools,
) -> None:
    svc, tools = wp_service_tools(
        mock="1",
        files_ssh="0",
        sandbox="0",
        live_preview="0",
        data_dir=tmp_path,
        with_child_theme=True,
    )

    # 1. Service health + mock backup + staging drill + prod restore refusal
    chk = svc.run_checks("mock-staging", actor="oscar")
    assert chk.ok is True
    bk = svc.create_backup("mock-staging", actor="oscar")
    assert bk.ok is True
    backup_id = bk.data["backup"]["id"]
    drill = svc.restore_drill(backup_id, target_site_id="mock-staging", actor="oscar")
    assert drill.ok is True
    prod_drill = svc.restore_drill(backup_id, target_site_id="mock-shop", actor="oscar")
    assert prod_drill.ok is False and prod_drill.data["error_class"] == "TargetNotStaging"

    # 2. Agent tools -> ReviewPlans workspace + page draft (PII + HTML sanitize) + checks
    listed = tools.execute("wp_list_sites", {}, actor="oscar")
    assert listed.data["ok"] is True
    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "azom-child", "summary": "Harmonisering"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]

    draft = tools.execute(
        "wp_workspace_page_draft",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "slug": "tack-test",
            "title": "Tack",
            "page_type": "thankyou",
            "body_html": "<section><h1>Order #AZ-TEST-1042</h1><p>test@example.invalid</p></section>",
            "page_css": ".ty { max-width: 960px; }",
            "synthetic_context": {"order_number": "AZ-TEST-1042", "customer_email": "test@example.invalid"},
        },
        actor="oscar",
    )
    assert draft.data["ok"] is True and draft.data["risk"] == "low"
    checked = tools.execute(
        "wp_workspace_run_checks",
        {"site_id": "mock-staging", "plan_id": plan_id},
        actor="oscar",
    )
    assert "plan_id" in checked.data

    # 3. Canonical ReviewPlans read-model + preview HTML hardening roundtrip
    plans = review_plan.ReviewPlans(registry_path=MOCK_REGISTRY, data_dir=tmp_path)
    site_view = plans.describe_site_plans("mock-staging", actor="oscar")
    assert site_view.ok is True and len(site_view.data["plans"]) == 1
    one_view = plans.describe_plan("mock-staging", plan_id, actor="oscar", include_diff=True)
    assert one_view.ok is True and one_view.data["plan"]["is_standalone"] is True
    ready, _reason = plans.staging_readiness(one_view.data["plan"], changed_count=1)
    assert ready is False  # tests_incomplete in mock mode

    guard_tag = build_sandbox_guard_script(nonce="n-test", focus_text="Tack")
    hardened = html_sanitize.harden_preview_html(
        f'<html><body><div data-azom-sandbox-toolbar="1"></div><script>evil()</script>{guard_tag}</body></html>',
        nonce="n-rotated",
        guard_script_builder=build_sandbox_guard_script,
    )
    assert "evil()" not in hardened
    assert 'nonce="n-rotated"' in hardened
    assert "connect-src 'none'" in html_sanitize.preview_csp_header("n-rotated")

    # 4. Stateless dashboard routes + Oscar production HITL gate
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "oscar")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jonatan")
    app = load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()
    headers = dashboard_auth_headers(client, "oscar")
    assert client.get("/wp", headers=headers).status_code == 200
    assert client.get("/wp/mock-staging", headers=headers).status_code == 200
    assert client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=headers).status_code == 200
    prod_resp = client.post(
        f"/wp/mock-staging/reviews/{plan_id}/production",
        headers=headers,
        data={
            "csrf_token": headers["X-CSRF-Token"],
            "base_sha256": one_view.data["plan"]["base_sha256"],
            "work_sha256": one_view.data["plan"]["work_sha256"],
        },
        follow_redirects=True,
    )
    assert prod_resp.status_code == 200
    assert "BackupNotVerified" in prod_resp.get_data(as_text=True)

    # 5. Bot layer: intents, 3-key session pruning, and turn capability formatting
    assert wp_intents.WP_CODE_INTENT_RE.search("ändra färgen på knappen")
    assert wp_intents.WP_CODE_FALLTHROUGH_RE.search("ändra färgen på knappen")
    sess = {
        "wp_ops_context": {"actor": "oscar", "channel": "telegram", "conversation_id": "c1", "site_id": "mock-staging"},
        "active_wp_review": {"actor": "oscar", "channel": "telegram", "conversation_id": "c1", "plan_id": plan_id},
        "wp_review_hashes": {plan_id: {"actor": "oscar", "site_id": "mock-staging", "base_sha256": "a", "work_sha256": "b"}},
    }
    _updates, popped = wp_session.prune_mismatched_wp_session(
        sess, actor="jonatan", channel="telegram", conversation_id="c1"
    )
    assert set(popped) == {"wp_ops_context", "active_wp_review", "wp_review_hashes"}
    assert "wp_open_review_workspace" in wp_turn.format_wp_capabilities_lines()

    # 6. CLI wp subcommands in mock mode
    assert ecom_cli.main(["--mock", "--actor", "oscar", "wp", "sites"]) == 0
    sites_out = json.loads(capsys.readouterr().out)
    assert sites_out["ok"] is True and sites_out["data"]["live_calls"] is False
    assert ecom_cli.main(["--mock", "--actor", "oscar", "wp", "status"]) == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["ok"] is True and status_out["data"]["live_calls"] is False
