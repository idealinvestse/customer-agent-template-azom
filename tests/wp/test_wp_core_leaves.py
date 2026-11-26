"""Unit tests for PR 1.2 core leaves: models, locks, atomic_write_text, source_scan, and theme_json."""

from __future__ import annotations

import ast
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ecom_ops.wp_ops import locks, models, service, source_scan, store, theme_json

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_models_result_validators_and_utc_helpers():
    assert service.WpOpsResult is models.WpOpsResult

    ok = models.ok_result("done", count=2)
    assert ok.ok is True
    assert ok.to_dict() == {"ok": True, "message": "done", "data": {"count": 2}}

    fail = models.fail_result("InvalidTarget", "bad", site_id="shop-a")
    assert fail.ok is False
    assert fail.data == {"error_class": "InvalidTarget", "site_id": "shop-a"}

    assert models.SITE_ID_RE.fullmatch("azom-no") is not None
    assert models.SITE_ID_RE.fullmatch("Bad_Site") is None
    assert models.validate_files_root("/var/www/shop/", site_id="shop") == "/var/www/shop"
    with pytest.raises(ValueError):
        models.validate_files_root("/var/www/../etc", site_id="shop")
    assert models.validate_ssh_host("Shop.Internal.", site_id="shop") == "shop.internal"
    with pytest.raises(ValueError):
        models.validate_ssh_host("bad host!", site_id="shop")

    naive = datetime(2026, 11, 26, 12, 0, 0)
    aware = models.ensure_utc(naive)
    assert aware.tzinfo is UTC
    assert models.parse_iso_utc("2026-11-26T12:00:00") == aware
    assert models.parse_iso_utc("not-a-date") is None
    assert models.parse_iso_utc("") is None


def test_locks_site_lock_hold_site_and_advisory_file_lock(tmp_path):
    assert service._site_lock is locks.site_lock
    lock_a = locks.site_lock("pr12-site-a")
    assert lock_a is locks.site_lock("pr12-site-a")

    acquired_inside = False
    with locks.hold_site("pr12-site-a"):
        acquired_inside = lock_a.acquire(blocking=False)
    assert acquired_inside is False
    assert lock_a.acquire(blocking=False) is True
    lock_a.release()

    lock_file = tmp_path / "nested" / "test.lock"
    order: list[str] = []
    with locks.advisory_file_lock(lock_file, mode=0o600):
        order.append("held")
        assert lock_file.exists()
    assert order == ["held"]


def test_store_atomic_write_text_retries_permission_error_and_cleans_tmp(monkeypatch, tmp_path):
    target = tmp_path / "sub" / "state.json"
    real_replace = os.replace
    calls = {"replace": 0, "sleep": 0}

    def _flaky_replace(src, dst):
        calls["replace"] += 1
        if calls["replace"] < 3:
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr("ecom_ops.wp_ops.store.os.replace", _flaky_replace)
    monkeypatch.setattr(
        "ecom_ops.wp_ops.store.time.sleep",
        lambda _s: calls.__setitem__("sleep", calls["sleep"] + 1),
    )

    store.atomic_write_text(target, '{"ok": true}\n', mode=0o600)
    assert target.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert calls["replace"] == 3
    assert calls["sleep"] == 2
    assert not list(target.parent.glob("*.tmp"))

    # Exhausted retries clean up temporary file and re-raise
    monkeypatch.setattr(
        "ecom_ops.wp_ops.store.os.replace",
        lambda _s, _d: (_ for _ in ()).throw(PermissionError("still locked")),
    )
    with pytest.raises(PermissionError):
        store.atomic_write_text(target, '{"ok": false}\n')
    assert not list(target.parent.glob("*.tmp"))


def test_source_scan_helpers_and_no_private_code_index_imports():
    files = source_scan.iter_source_files({"./b.php": "B", "a.php": "A", "": "skip"})
    assert files == [("a.php", "A"), ("b.php", "B")]

    php = "<?php // comment\nadd_action('init', function() { return 1; }); /* blk */\n"
    stripped, in_str, _depths = source_scan.strip_comments_and_find_strings(php, allow_hash=True)
    assert "comment" not in stripped and "blk" not in stripped
    paren_idx = stripped.index("(")
    args = source_scan.read_call_args(stripped, paren_idx)
    assert source_scan.unquote_literal(args[0]) == "init"
    assert source_scan.summarize_callback(args[1]) == "closure"
    assert source_scan.line_number(stripped, paren_idx) == 2
    assert in_str[stripped.index("'init'")] is True

    assert source_scan.parse_semver_key("v9.4.0-rc1") == ((9, 4, 0), 0, "rc1")
    assert source_scan.parse_semver_key("9.4.0") == ((9, 4, 0), 1, "")
    assert source_scan.parse_version_tuple("9.4.0-beta1") == (9, 4, 0)
    assert source_scan.parse_semver_key("not-semver") is None

    for rel in ("static_checks.py", "woo_templates.py", "wporg_intel.py"):
        src = (REPO_ROOT / "skills" / "ecom_ops" / "wp_ops" / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ecom_ops.wp_ops.code_index":
                raise AssertionError(f"{rel} still imports privately from code_index")


def test_theme_json_decomposed_apply_handles_dict_and_list_ops():
    base = json.dumps(
        {
            "version": 3,
            "settings": {"color": {"palette": [{"slug": "primary", "color": "#000"}]}},
            "styles": {},
        }
    )
    res = theme_json.apply_theme_json_operations(
        base,
        [
            {"op": "set", "path": "/settings/color/palette/-", "value": {"slug": "accent", "color": "#0f0"}},
            {"op": "set", "path": "/settings/color/palette/0/color", "value": "#111"},
            {"op": "set", "path": "/styles/color/background", "value": "#fff"},
            {"op": "remove", "path": "/settings/color/palette/1"},
        ],
    )
    parsed = json.loads(res["updated_json"])
    assert parsed["settings"]["color"]["palette"] == [{"slug": "primary", "color": "#111"}]
    assert parsed["styles"]["color"]["background"] == "#fff"

    with pytest.raises(ValueError, match="out of bounds"):
        theme_json.apply_theme_json_operations(
            base, [{"op": "remove", "path": "/settings/color/palette/9"}]
        )
    with pytest.raises(ValueError, match="cannot be used with remove"):
        theme_json.apply_theme_json_operations(
            base, [{"op": "remove", "path": "/settings/color/palette/-"}]
        )
    assert threading.current_thread().is_alive()
