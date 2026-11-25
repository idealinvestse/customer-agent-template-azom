"""Tests for WP toolchain SFTP error classification, recovery hints, focus params, and smart loop budget."""

from __future__ import annotations

import errno
import json
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecom_ops.integrations.wp_files_ssh import SshFileTransport
from ecom_ops.rbac import ROLE_PERMISSIONS, Permission, clear_rbac_cache
from ecom_ops.wp_ops.agent_tools import (
    FILE_LOOP_MAX_CALLS,
    FILE_LOOP_MAX_RECOVERY_RETRIES,
    FILE_TOOL_SCHEMAS,
    WP_TOOL_GUIDE,
    ToolOutput,
    WpAgentTools,
    _safe_tool_detail,
    run_bounded_file_tool_loop,
)
from ecom_ops.wp_ops.files import TransportError, transport_message
from ecom_ops.wp_ops.models import parse_site
from ecom_ops.wp_ops.service import WpOpsService

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "wp_sites.mock.yaml"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_SANDBOX", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "0")
    monkeypatch.setenv("AZOM_XAI_GROK_BUILD", "0")
    monkeypatch.setenv("XAI_GROK_BIN", "")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    clear_rbac_cache()


def _service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry: Path | None = None,
) -> WpOpsService:
    _setup_env(tmp_path, monkeypatch)
    return WpOpsService(registry_path=registry or REGISTRY, data_dir=tmp_path)


def _write_theme(tmp_path: Path, site_id: str, slug: str, rel: str, text: str) -> Path:
    dest = tmp_path / "wp_files_mock" / site_id / "wp-content" / "themes" / slug / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(text.encode("utf-8"))
    return dest


def _call(name: str, arguments: dict[str, Any] | None = None) -> str:
    return f'<tool_call>{json.dumps({"name": name, "arguments": arguments or {}})}</tool_call>'


def _make_ssh_site() -> Any:
    return parse_site(
        {
            "id": "azom-no",
            "display_name": "Azom NO",
            "canonical_url": "https://azom.no/",
            "environment": "production",
            "business_owner": "customer",
            "hosting_provider": "unconfigured",
            "wordpress_version": "6.7",
            "php_version": "8.3",
            "woocommerce": True,
            "critical_urls": ["https://azom.no/"],
            "maintenance_window": "not agreed",
            "secret_ref": "WP_SECRET_AZOM_NO",
            "alert_owner": "unassigned",
            "alert_confirmations": 2,
            "check_interval_seconds": 60,
            "timeout_seconds": 10,
            "dedupe_window_seconds": 30,
            "capabilities": {"uptime": "available"},
            "files_root": "/var/www/azom",
            "ssh_host": "shop.internal",
            "ssh_user": "azom-wp-reader",
        }
    )


def test_sftp_enoent_classifies_as_file_missing_and_write_stays_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _make_ssh_site()
    transport = SshFileTransport(site, tmp_path)

    class _FakeSftp:
        def __init__(self, mode: str) -> None:
            self.mode = mode

        def lstat(self, path: str) -> SimpleNamespace:
            if path.endswith("footer.php"):
                if self.mode == "fnf":
                    raise FileNotFoundError(errno.ENOENT, "No such file", path)
                if self.mode == "enoent":
                    raise OSError(errno.ENOENT, "No such file")
                if self.mode == "perm":
                    raise PermissionError(errno.EACCES, "Permission denied", path)
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=12, st_ino=7)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=0, st_ino=1)

        def open(self, path: str, _mode: str) -> Any:
            if self.mode == "open_enoent":
                raise OSError(errno.ENOENT, "No such file")
            raise OSError(errno.EIO, "IO error")

    target = "/var/www/azom/wp-content/themes/parent-theme/footer.php"

    for mode in ("fnf", "enoent", "open_enoent"):
        fake = _FakeSftp(mode)

        @contextmanager
        def _sess(client: _FakeSftp = fake):
            yield client

        monkeypatch.setattr(transport, "_active_session", _sess)
        with pytest.raises(TransportError) as raised:
            transport.read_bytes(target)
        assert raised.value.error_class == "FileMissing", mode

    @contextmanager
    def _perm_sess():
        yield _FakeSftp("perm")

    monkeypatch.setattr(transport, "_active_session", _perm_sess)
    with pytest.raises(TransportError) as perm_err:
        transport.read_bytes(target)
    assert perm_err.value.error_class == "SshFailed"

    with pytest.raises(TransportError) as write_err:
        transport.write_bytes(target, b"blocked")
    assert write_err.value.error_class == "SshWriteDisabled"
    assert "not found" in transport_message("FileMissing").lower()


def test_site_required_includes_available_sites_for_authorized_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    single_reg = tmp_path / "single.yaml"
    single_reg.write_text(
        (ROOT / "config" / "wp_sites.mock.yaml")
        .read_text(encoding="utf-8")
        .split("  - id: mock-staging")[0],
        encoding="utf-8",
    )
    svc = _service(tmp_path, monkeypatch, registry=single_reg)
    tools = WpAgentTools(svc)

    res = tools.execute("wp_fetch_page_html", {}, actor="oscar")
    assert res.data["ok"] is False
    assert res.data["error_class"] == "SiteRequired"
    assert res.data["available_sites"] == ["mock-shop"]
    assert "mock-shop" in res.data["hint"]

    res_open = tools.execute("wp_open_review_workspace", {"kind": "theme"}, actor="oscar")
    assert res_open.data["error_class"] == "SiteRequired"
    assert res_open.data["available_sites"] == ["mock-shop"]

    res_status = tools.execute("wp_site_status", {}, actor="oscar")
    assert res_status.data["error_class"] == "SiteRequired"
    assert res_status.data["available_sites"] == ["mock-shop"]

    # Unauthorized actor must NOT see available_sites
    monkeypatch.setitem(ROLE_PERMISSIONS, "viewer", frozenset({Permission.READ}))
    clear_rbac_cache()
    denied_sites = tools.execute("wp_fetch_page_html", {}, actor="jonatan")
    assert denied_sites.data["error_class"] == "SiteRequired"
    assert "available_sites" not in denied_sites.data


def test_workspace_recovery_hints_open_copy_read_patch_and_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(tmp_path, monkeypatch)
    tools = WpAgentTools(svc)

    _write_theme(
        tmp_path,
        "mock-staging",
        "parent-theme",
        "style.css",
        "/*\nTheme Name: Parent Theme\n*/\n",
    )
    _write_theme(
        tmp_path,
        "mock-staging",
        "parent-theme",
        "page-index.php",
        "<?php\n// Footer address: Gamla gatan 1, Oslo\n",
    )
    _write_theme(
        tmp_path,
        "mock-staging",
        "child-theme",
        "style.css",
        "/*\nTheme Name: Child Theme\nTemplate: parent-theme\n*/\n",
    )
    _write_theme(
        tmp_path,
        "mock-staging",
        "child-theme",
        "functions.php",
        "<?php\n// child scaffold\n",
    )

    opened = tools.execute(
        "wp_open_review_workspace",
        {"site_id": "mock-staging", "kind": "theme", "slug": "child-theme", "summary": "Footer tweak"},
        actor="oscar",
    )
    assert opened.data["ok"] is True
    plan_id = opened.data["plan_id"]
    assert set(opened.data["workspace_files"]) == {"style.css", "functions.php"}
    assert opened.data["parent_theme_slug"] == "parent-theme"
    assert "wp_workspace_copy_from_parent" in opened.data["hint"]
    assert "parent-theme" in opened.data["hint"]

    # Search combining plan_id + kind/slug -> InvalidSearch with clear hint
    bad_search = tools.execute(
        "wp_search_component",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "kind": "theme",
            "slug": "parent-theme",
            "query": "Gamla gatan",
        },
        actor="oscar",
    )
    assert bad_search.data["ok"] is False
    assert bad_search.data["error_class"] == "InvalidSearch"
    assert "plan_id" in bad_search.data["hint"]
    assert "kind" in bad_search.data["hint"]

    # Workspace search with 0 hits points to parent theme
    zero_hits = tools.execute(
        "wp_search_component",
        {"site_id": "mock-staging", "plan_id": plan_id, "query": "Gamla gatan"},
        actor="oscar",
    )
    assert zero_hits.data["ok"] is True
    assert zero_hits.data["hits"] == []
    assert zero_hits.data["parent_theme_slug"] == "parent-theme"
    assert "parent-theme" in zero_hits.data["hint"]

    # Reading missing file directly from parent theme returns FileMissing + search hint for oscar
    missing_read = tools.execute(
        "wp_read_theme_plugin_file",
        {"site_id": "mock-staging", "kind": "theme", "slug": "parent-theme", "path": "footer.php"},
        actor="oscar",
    )
    assert missing_read.data["ok"] is False
    assert missing_read.data["error_class"] == "FileMissing"
    assert missing_read.data["recovery_tool"] == "wp_search_component"
    assert "wp_list_component_tree" in missing_read.data["hint"]

    # Non-admin still gets AccessDenied (no file existence leak)
    non_admin_missing = tools.execute(
        "wp_read_theme_plugin_file",
        {"site_id": "mock-staging", "kind": "theme", "slug": "parent-theme", "path": "footer.php"},
        actor="jonatan",
    )
    assert non_admin_missing.data["error_class"] == "AccessDenied"
    assert "hint" not in non_admin_missing.data

    # Workspace read / patch before copy_from_parent -> FileMissing + workspace_files + copy hint
    for tool_name, args in (
        (
            "wp_workspace_read_file",
            {"site_id": "mock-staging", "plan_id": plan_id, "path": "page-index.php"},
        ),
        (
            "wp_workspace_patch_file",
            {
                "site_id": "mock-staging",
                "plan_id": plan_id,
                "path": "page-index.php",
                "old_text": "Gamla gatan 1, Oslo",
                "new_text": "Testsidan123, falun",
            },
        ),
    ):
        miss = tools.execute(tool_name, args, actor="oscar")
        assert miss.data["ok"] is False
        assert miss.data["error_class"] == "FileMissing"
        assert set(miss.data["workspace_files"]) == {"style.css", "functions.php"}
        assert miss.data["parent_theme_slug"] == "parent-theme"
        assert miss.data["recovery_tool"] == "wp_workspace_copy_from_parent"
        assert "wp_workspace_copy_from_parent" in miss.data["hint"]

    # Copy from parent -> includes workspace_files and ready-to-patch hint
    copied = tools.execute(
        "wp_workspace_copy_from_parent",
        {"site_id": "mock-staging", "plan_id": plan_id, "path": "page-index.php"},
        actor="oscar",
    )
    assert copied.data["ok"] is True
    assert "page-index.php" in copied.data["workspace_files"]
    assert "wp_workspace_patch_file" in copied.data["hint"]

    # Patch now succeeds directly
    patched = tools.execute(
        "wp_workspace_patch_file",
        {
            "site_id": "mock-staging",
            "plan_id": plan_id,
            "path": "page-index.php",
            "old_text": "Gamla gatan 1, Oslo",
            "new_text": "Testsidan123, falun",
        },
        actor="oscar",
    )
    assert patched.data["ok"] is True


def test_preview_page_focus_schema_validation_and_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(tmp_path, monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")
    tools = WpAgentTools(svc)

    expected_props = {
        "site_id",
        "path",
        "plan_id",
        "focus_section",
        "focus_selector",
        "focus_text",
        "full_page",
        "reuse_live_baseline",
    }
    assert set(FILE_TOOL_SCHEMAS["wp_preview_page"]["properties"]) == expected_props
    assert _safe_tool_detail(
        "wp_preview_page",
        {"site_id": "mock-staging", "focus_section": "footer"},
        None,
    ) == "mock-staging · footer"

    captured_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "ecom_ops.integrations.wp_live_preview.fetch_live_html",
        lambda site, path="/": {
            "status": 200,
            "final_url": f"{site.canonical_url}{path}",
            "html": "<html><body><footer>Testsidan123, falun</footer></body></html>",
            "bytes": 64,
        },
    )

    def fake_capture(
        site,
        *,
        path="/",
        artifact_dir: Path,
        revisions,
        focus_section=None,
        focus_selector=None,
        focus_text=None,
        full_page=False,
        **_kwargs,
    ):
        captured_calls.append(
            {
                "focus_section": focus_section,
                "focus_selector": focus_selector,
                "focus_text": focus_text,
                "full_page": full_page,
            }
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "live-desktop.png").write_bytes(_PNG_BYTES)
        return {
            "blocked_counts": {},
            "screenshots": ["live-desktop.png"],
            "focus_matched": True,
            "focus_target": "footer",
            "revisions": {"live": {"desktop": {}, "mobile": {}}},
        }

    monkeypatch.setattr("ecom_ops.integrations.wp_live_preview.capture_preview", fake_capture)

    ok_res = tools.execute(
        "wp_preview_page",
        {
            "site_id": "mock-staging",
            "path": "/",
            "focus_section": "footer",
            "focus_text": "Testsidan123",
            "full_page": False,
        },
        actor="oscar",
    )
    assert ok_res.data["ok"] is True
    assert ok_res.data["focus_section"] == "footer"
    assert ok_res.data["focus_text"] == "Testsidan123"
    assert ok_res.data["focus_matched"] is True
    assert ok_res.data["focus_target"] == "footer"
    assert captured_calls[-1] == {
        "focus_section": "footer",
        "focus_selector": None,
        "focus_text": "Testsidan123",
        "full_page": False,
    }

    bad_section = tools.execute(
        "wp_preview_page",
        {"site_id": "mock-staging", "focus_section": "sidebar"},
        actor="oscar",
    )
    assert bad_section.data["ok"] is False
    assert bad_section.data["error_class"] == "InvalidFocusTarget"

    bad_selector = tools.execute(
        "wp_preview_page",
        {"site_id": "mock-staging", "focus_selector": "footer; javascript:alert(1)"},
        actor="oscar",
    )
    assert bad_selector.data["ok"] is False
    assert bad_selector.data["error_class"] == "InvalidFocusTarget"

    bad_full = tools.execute(
        "wp_preview_page",
        {"site_id": "mock-staging", "full_page": "yes"},
        actor="oscar",
    )
    assert bad_full.data["ok"] is False
    assert bad_full.data["error_class"] == "InvalidToolArguments"


def test_file_loop_allows_bounded_recovery_retries_without_burning_main_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service(tmp_path, monkeypatch)
    tools = WpAgentTools(svc)

    # 4 recoverable errors (SiteRequired x2, InvalidToolArguments x2) + 16 successful calls
    seq: list[str] = [
        _call("wp_fetch_page_html", {}),  # SiteRequired (retry 1)
        _call("wp_fetch_page_html", {}),  # SiteRequired (retry 2)
        _call("wp_playbook", {"topic": "child_theme_edit", "bogus": 1}),  # InvalidToolArguments (retry 3)
        _call("wp_playbook", {"topic": "child_theme_edit", "bogus": 2}),  # InvalidToolArguments (retry 4)
    ]
    seq.extend(_call("wp_playbook", {"topic": "child_theme_edit"}) for _ in range(FILE_LOOP_MAX_CALLS))
    seq.append("Alla 16 lyckade anrop hann köras trots 4 återhämtningar.")
    responses = iter(seq[1:])

    res = run_bounded_file_tool_loop(
        seq[0],
        [],
        complete=lambda _m: (next(responses), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:99",
        tools=tools,
    )
    assert res.handled is True
    assert res.calls == FILE_LOOP_MAX_CALLS
    assert len(res.results) == FILE_LOOP_MAX_CALLS + FILE_LOOP_MAX_RECOVERY_RETRIES
    assert "lyckade anrop" in res.content

    # Exceeding recovery retries (5th+ error) DOES burn main budget -> strictly bounded!
    fail_count = {"n": 0}

    def always_bad(_msgs: list[dict[str, str]]) -> tuple[str, float]:
        fail_count["n"] += 1
        if fail_count["n"] <= FILE_LOOP_MAX_CALLS + FILE_LOOP_MAX_RECOVERY_RETRIES + 5:
            return _call("wp_fetch_page_html", {}), 0.0
        return "Stoppad.", 0.0

    bounded = run_bounded_file_tool_loop(
        _call("wp_fetch_page_html", {}),
        [],
        complete=always_bad,
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:99",
        tools=tools,
    )
    assert bounded.handled is True
    assert bounded.calls == FILE_LOOP_MAX_CALLS
    assert len(bounded.results) == FILE_LOOP_MAX_CALLS + FILE_LOOP_MAX_RECOVERY_RETRIES


def test_file_loop_reserves_or_auto_captures_preview_after_workspace_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_LIVE_PREVIEW", "1")

    png = tmp_path / "wp_workspaces" / "mock-staging" / "wr-1234567890ab" / "artifacts" / "change-desktop.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(_PNG_BYTES)

    class _FakeTools:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, Any], str]] = []

        def execute(
            self,
            name: str,
            arguments: dict[str, Any],
            *,
            actor: str,
            sticky_site_id: str | None = None,
            channel: str = "",
            conversation_id: str = "",
        ) -> ToolOutput:
            del sticky_site_id, channel, conversation_id
            self.executed.append((name, dict(arguments), actor))
            if name == "wp_workspace_patch_file":
                return ToolOutput(
                    {
                        "ok": True,
                        "status": "validated",
                        "site_id": "mock-staging",
                        "plan_id": "wr-1234567890ab",
                    },
                    site_id="mock-staging",
                )
            if name == "wp_preview_page":
                return ToolOutput(
                    {
                        "ok": True,
                        "status": "preview",
                        "site_id": "mock-staging",
                        "plan_id": "wr-1234567890ab",
                        "screenshots": ["change-desktop.png"],
                    },
                    site_id="mock-staging",
                    attachments=(str(png),),
                )
            return ToolOutput({"ok": True, "status": "ok", "site_id": "mock-staging"}, site_id="mock-staging")

    # Case A: model explicitly calls wp_preview_page right after hitting 16/16 on a patch
    fake_a = _FakeTools()
    seq_a = [_call("wp_playbook", {"topic": "child_theme_edit"}) for _ in range(FILE_LOOP_MAX_CALLS - 1)]
    seq_a.append(
        _call(
            "wp_workspace_patch_file",
            {
                "site_id": "mock-staging",
                "plan_id": "wr-1234567890ab",
                "path": "page-index.php",
                "old_text": "old",
                "new_text": "new",
            },
        )
    )
    seq_a.append(
        _call(
            "wp_preview_page",
            {"site_id": "mock-staging", "plan_id": "wr-1234567890ab", "focus_section": "footer"},
        )
    )
    seq_a.append("Ändringen är gjord och skärmdumpen bifogad.")
    iter_a = iter(seq_a[1:])
    res_a = run_bounded_file_tool_loop(
        seq_a[0],
        [],
        complete=lambda _m: (next(iter_a), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=fake_a,  # type: ignore[arg-type]
    )
    assert res_a.handled is True
    assert res_a.attachments == (str(png),)
    assert fake_a.executed[-1][0] == "wp_preview_page"
    assert fake_a.executed[-1][2] == "oscar"

    # Case B: model hits 16/16 trying another tool after patching -> auto-preview runs under same actor
    fake_b = _FakeTools()
    seq_b = [_call("wp_playbook", {"topic": "child_theme_edit"}) for _ in range(FILE_LOOP_MAX_CALLS - 1)]
    seq_b.append(
        _call(
            "wp_workspace_patch_file",
            {
                "site_id": "mock-staging",
                "plan_id": "wr-1234567890ab",
                "path": "page-index.php",
                "old_text": "old",
                "new_text": "new",
            },
        )
    )
    seq_b.append(_call("wp_workspace_status", {"site_id": "mock-staging", "plan_id": "wr-1234567890ab"}))
    seq_b.append("Sammanfattning med automatisk slut-förhandsvisning.")
    iter_b = iter(seq_b[1:])
    res_b = run_bounded_file_tool_loop(
        seq_b[0],
        [],
        complete=lambda _m: (next(iter_b), 0.0),
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        tools=fake_b,  # type: ignore[arg-type]
    )
    assert res_b.handled is True
    assert res_b.attachments == (str(png),)
    assert fake_b.executed[-1][0] == "wp_preview_page"
    assert fake_b.executed[-1][1]["plan_id"] == "wr-1234567890ab"
    assert fake_b.executed[-1][2] == "oscar"


def test_wp_tool_guide_documents_recovery_focus_and_stays_within_budget() -> None:
    assert len(WP_TOOL_GUIDE) <= 7000
    assert "focus_section" in WP_TOOL_GUIDE
    assert "focus_text" in WP_TOOL_GUIDE
    assert "focus_selector" in WP_TOOL_GUIDE
    assert "hint/recovery_tool" in WP_TOOL_GUIDE
    assert "browser_smoke" in WP_TOOL_GUIDE
