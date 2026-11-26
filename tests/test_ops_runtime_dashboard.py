"""Tests for Oscar-only /ops/runtime and /ops/runtime/gc dashboard console (Feature C4)."""

from __future__ import annotations

import base64
import importlib.util
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


class _StrictCspHtmlAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts = 0
        self.event_handlers: list[tuple[str, str]] = []
        self.inline_styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "script" and "src" not in attr_map:
            self.inline_scripts += 1
        for k in attr_map:
            if k.startswith("on"):
                self.event_handlers.append((tag, k))
            if k == "style":
                self.inline_styles.append(tag)


def _load_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    for name in ("azom_dashboard", "oscar_routes", "support", "auth", "settings_store"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _setup_client(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-ops-runtime-secret")
    monkeypatch.chdir(DASH_DIR)
    app = _load_app()
    app.config["TESTING"] = True
    return app.test_client(), data_dir


def _auth(client, user: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{user}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    r = client.get("/", headers=headers)
    assert r.status_code == 200
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', r.get_data(as_text=True))
    if not m:
        r2 = client.get("/settings", headers=headers)
        m = re.search(r'name="_csrf"\s+value="([^"]+)"', r2.get_data(as_text=True))
    if m:
        headers["X-CSRF-Token"] = m.group(1)
    return headers


def test_ops_runtime_rbac_nav_cards_and_csp(tmp_path, monkeypatch):
    client, data_dir = _setup_client(tmp_path, monkeypatch)

    # Unauthenticated denied
    assert client.get("/ops/runtime").status_code == 401

    jonatan = _auth(client, "jonatan")
    oscar = _auth(client, "oscar")

    # Viewer denied on GET /ops/runtime and POST /ops/runtime/gc, and nav link hidden
    assert client.get("/ops/runtime", headers=jonatan).status_code == 403
    assert (
        client.post(
            "/ops/runtime/gc",
            headers=jonatan,
            data={"_csrf": jonatan.get("X-CSRF-Token", ""), "mode": "dry_run"},
        ).status_code
        == 403
    )
    jonatan_home = client.get("/", headers=jonatan).get_data(as_text=True)
    assert 'href="/ops/runtime"' not in jonatan_home

    # Inject a deterministic snapshot module if backend is present or monkeypatched
    fake_snapshot = {
        "generated_at": "2026-11-26T10:00:00+00:00",
        "poller": {
            "owner": "poller-host:1234",
            "expires_at": "2026-11-26T10:02:00+00:00",
            "ttl_seconds": 115,
            "healthy": True,
        },
        "queues": {
            "telegram": {"pending": 1, "running": 0, "done": 14, "failed": 0},
            "messenger": {"pending": 0, "running": 1, "done": 8, "failed": 0},
        },
        "model_router": {
            "circuit_breakers": {
                "route:openrouter": {"state": "closed", "failures": 0},
                "route:openai_chatgpt": {"state": "open", "failures": 3, "open_until": "2026-11-26T10:05:00+00:00"},
            },
            "chatgpt_oauth": {
                "connected": True,
                "expires_at": "2026-11-26T12:00:00+00:00",
                "ttl_seconds": 7200,
                "needs_reauth": False,
            },
        },
        "disk_usage": {
            "total_bytes": 12_582_912,
            "total_mb": 12.0,
            "folders": {
                "wp_workspaces": 8_388_608,
                "telegram_inbox_media": 1_048_576,
                "messenger_inbox_media": 524_288,
                "telemetry_jsonl": 2_097_152,
                "audit_jsonl": 262_144,
                "sqlite_dbs": 262_144,
            },
            "expired_plans_count": 3,
            "untracked_pv_count": 2,
        },
        "recent_errors": [
            {
                "ts": "2026-11-26T09:58:00+00:00",
                "source": "telegram_poller",
                "error_class": "TimeoutError",
                "status_code": 504,
                "detail": "getUpdates timed out",
            }
        ],
        "waitress": {"configured_threads": 12},
    }

    import types

    fake_rh = types.ModuleType("ecom_ops.runtime_health")
    fake_rh.collect_runtime_snapshot = lambda data_dir=None, recent_error_limit=15: fake_snapshot
    monkeypatch.setitem(sys.modules, "ecom_ops.runtime_health", fake_rh)

    # Oscar sees nav link and all 5 cards cleanly under strict CSP
    resp = client.get("/ops/runtime", headers=oscar)
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy") or ""
    assert "script-src 'self'" in csp
    html = resp.get_data(as_text=True)
    assert 'href="/ops/runtime"' in html
    assert 'data-ops-runtime="true"' in html
    for card in ("poller", "queues", "model-router", "disk-gc", "recent-errors"):
        assert f'data-runtime-card="{card}"' in html
    assert "poller-host:1234" in html
    assert "route:openai_chatgpt" in html
    assert "7200 s" in html
    assert "12.0 MB" in html
    assert "telegram_poller" in html
    assert "getUpdates timed out" in html
    assert 'data-gc-action="dry_run"' in html
    assert 'data-gc-action="execute"' in html

    audit = _StrictCspHtmlAudit()
    audit.feed(html)
    assert audit.inline_scripts == 0
    assert audit.event_handlers == []
    assert audit.inline_styles == []


def test_ops_runtime_gc_csrf_dry_run_and_execute(tmp_path, monkeypatch):
    client, _data_dir = _setup_client(tmp_path, monkeypatch)
    oscar = _auth(client, "oscar")

    import types

    calls: list[bool] = []

    def _fake_gc(*, data_dir=None, dry_run: bool = False, **kwargs):
        calls.append(dry_run)
        return {
            "dry_run": dry_run,
            "ran_at": "2026-11-26T10:01:00+00:00",
            "expired_plans": 2,
            "removed_workspace_dirs": 3,
            "removed_pv_dirs": 1,
            "removed_media_files": 4,
            "purged_proposals": 5,
            "rotated_logs": ["telemetry.jsonl"],
            "bytes_reclaimed": 3 * 1024 * 1024,
            "errors": [],
        }

    fake_gc = types.ModuleType("ecom_ops.ops_gc")
    fake_gc.run_ops_gc = _fake_gc
    monkeypatch.setitem(sys.modules, "ecom_ops.ops_gc", fake_gc)

    fake_rh = types.ModuleType("ecom_ops.runtime_health")
    fake_rh.collect_runtime_snapshot = lambda data_dir=None, recent_error_limit=15: {
        "poller": {"healthy": False},
        "queues": {},
        "model_router": {},
        "disk_usage": {"total_mb": 0, "folders": {}},
        "recent_errors": [],
        "waitress": {"configured_threads": 12},
    }
    monkeypatch.setitem(sys.modules, "ecom_ops.runtime_health", fake_rh)

    # Missing CSRF rejected
    no_csrf = client.post(
        "/ops/runtime/gc",
        headers={"Authorization": oscar["Authorization"]},
        data={"mode": "dry_run"},
    )
    assert no_csrf.status_code in {400, 403}
    assert calls == []

    # Default / dry_run executes with dry_run=True and renders GC summary card
    dry_resp = client.post(
        "/ops/runtime/gc",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "mode": "dry_run"},
        follow_redirects=True,
    )
    assert dry_resp.status_code == 200
    assert calls == [True]
    dry_html = dry_resp.get_data(as_text=True)
    assert 'data-gc-result="true"' in dry_html
    assert 'data-gc-dry-run="true"' in dry_html
    assert "3.00 MB" in dry_html

    # Execute runs with dry_run=False and renders executed card
    exec_resp = client.post(
        "/ops/runtime/gc",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "mode": "execute"},
        follow_redirects=True,
    )
    assert exec_resp.status_code == 200
    assert calls == [True, False]
    exec_html = exec_resp.get_data(as_text=True)
    assert 'data-gc-result="true"' in exec_html
    assert 'data-gc-dry-run="false"' in exec_html
    assert "3.00 MB" in exec_html


def test_ops_runtime_works_with_real_backend_modules(tmp_path, monkeypatch):
    """Integration check against real ecom_ops.runtime_health and ecom_ops.ops_gc once available."""
    client, _data_dir = _setup_client(tmp_path, monkeypatch)
    oscar = _auth(client, "oscar")

    try:
        import ecom_ops.ops_gc  # noqa: F401
        import ecom_ops.runtime_health  # noqa: F401
    except ImportError:
        import pytest

        pytest.skip("Backend runtime_health/ops_gc modules not present yet")

    resp = client.get("/ops/runtime", headers=oscar)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-ops-runtime="true"' in html
    assert "Delvis driftdata saknas" not in html

    gc_resp = client.post(
        "/ops/runtime/gc",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "mode": "dry_run"},
        follow_redirects=True,
    )
    assert gc_resp.status_code == 200
    assert 'data-gc-result="true"' in gc_resp.get_data(as_text=True)
