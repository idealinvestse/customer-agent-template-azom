"""Dashboard UI tests for Shop Intelligence (/shop-intel)."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_module():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    for name in (
        "azom_dashboard_shop_intel",
        "auth",
        "cases_routes",
        "core_routes",
        "health_routes",
        "marketing_routes",
        "oauth_routes",
        "oscar_routes",
        "research_routes",
        "secret_probes",
        "seo_routes",
        "settings_store",
        "shop_intel_routes",
        "status",
        "support",
        "webhooks",
        "wp",
        "wp_routes",
    ):
        sys.modules.pop(name, None)
    for key in [k for k in list(sys.modules) if k.startswith(("wp.", "infrastructure.dashboard.wp"))]:
        sys.modules.pop(key, None)

    spec = importlib.util.spec_from_file_location("azom_dashboard_shop_intel", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_shop_intel"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    mod.app.config["TESTING"] = True
    return mod


def _basic_headers(user: str = "oscar", password: str | None = None) -> dict[str, str]:
    pwd = user if password is None else password
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _auth_with_csrf(client, user: str = "oscar") -> dict[str, str]:
    headers = _basic_headers(user)
    resp = client.get("/shop-intel", headers=headers)
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        csrf = sess.get("csrf_token")
    assert csrf
    headers["X-CSRF-Token"] = str(csrf)
    return headers


class _StrictCspHtmlAudit(HTMLParser):
    """Collect inline script violations and on* inline event handlers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_events: list[tuple[str, str]] = []
        self.bad_scripts: list[str] = []
        self._in_non_json_script = False
        self._script_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        for key in attr_map:
            if key.startswith("on"):
                self.inline_events.append((tag, key))
        if tag.lower() == "script":
            stype = attr_map.get("type", "").strip().lower()
            if stype != "application/json":
                self._in_non_json_script = True
                self._script_buf = []
                if not attr_map.get("src", "").strip():
                    self.bad_scripts.append(f"<script {attr_map}> missing src")

    def handle_data(self, data: str) -> None:
        if self._in_non_json_script:
            self._script_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_non_json_script:
            body = "".join(self._script_buf).strip()
            if body:
                self.bad_scripts.append(f"non-empty inline script: {body[:60]!r}")
            self._in_non_json_script = False
            self._script_buf = []


def _assert_strict_csp(resp) -> None:
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "'unsafe-eval'" not in csp

    html = resp.get_data(as_text=True)
    audit = _StrictCspHtmlAudit()
    audit.feed(html)
    assert not audit.bad_scripts, f"inline scripts found: {audit.bad_scripts}"
    assert not audit.inline_events, f"inline on* handlers found: {audit.inline_events}"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)


@pytest.fixture
def dash_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for item in (ROOT / "config").iterdir():
        if item.is_file():
            (cfg_dir / item.name).write_text(item.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(data_dir / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-shop-intel-dashboard-secret")
    monkeypatch.delenv("AZOM_SHOP_INTEL_KILL", raising=False)
    monkeypatch.delenv("AZOM_SHOP_INTEL_INJECT", raising=False)
    for var in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(DASH_DIR)

    from ecom_ops.rbac import clear_rbac_cache
    from ecom_ops.shop_intel import reset_shop_intel_service

    clear_rbac_cache()
    reset_shop_intel_service()
    mod = _load_dashboard_module()
    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "cfg_dir": cfg_dir,
        "mod": mod,
        "app": mod.app,
        "client": mod.app.test_client(),
    }


def test_unauthenticated_requests_require_auth(dash_env):
    client = dash_env["client"]
    assert client.get("/shop-intel").status_code == 401
    assert client.post("/shop-intel/run", data={"site_id": "mock-shop"}).status_code == 401
    assert client.get("/shop-intel/runs/si-000000000000").status_code == 401
    assert client.get("/shop-intel/export?site_id=mock-shop&format=json").status_code == 401
    assert client.post("/shop-intel/schedule", data={"site_id": "mock-shop"}).status_code == 401


def test_nav_link_present_under_drift_and_webbplatser(dash_env):
    client = dash_env["client"]
    hdrs = _basic_headers("jonatan")
    resp = client.get("/", headers=hdrs)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-nav-group="Drift &amp; Webbplatser"' in html
    wp_idx = html.index('href="/wp"')
    si_idx = html.index('href="/shop-intel"')
    settings_idx = html.index('href="/settings"')
    assert wp_idx < si_idx < settings_idx
    assert "Butiksanalys" in html


def test_csrf_enforced_on_post_endpoints(dash_env):
    client = dash_env["client"]
    jonatan_hdrs = _basic_headers("jonatan")
    oscar_hdrs = _basic_headers("oscar")

    run_resp = client.post(
        "/shop-intel/run",
        headers=jonatan_hdrs,
        data={"site_id": "mock-shop", "mode": "full"},
    )
    assert run_resp.status_code == 400
    assert "csrf" in run_resp.get_data(as_text=True).lower()

    sched_resp = client.post(
        "/shop-intel/schedule",
        headers=oscar_hdrs,
        data={"site_id": "mock-shop", "enabled": "1", "interval_hours": "24", "mode": "full"},
    )
    assert sched_resp.status_code == 400
    assert "csrf" in sched_resp.get_data(as_text=True).lower()


def test_jonatan_can_view_run_detail_export_and_is_403_on_schedule(dash_env):
    client = dash_env["client"]
    hdrs = _auth_with_csrf(client, "jonatan")

    # Initial page load (no runs yet)
    idx_empty = client.get("/shop-intel?site_id=mock-shop", headers=hdrs)
    assert idx_empty.status_code == 200
    _assert_strict_csp(idx_empty)
    empty_html = idx_empty.get_data(as_text=True)
    assert "Butiksanalys" in empty_html
    assert "mock" in empty_html.lower()
    assert 'action="/shop-intel/schedule"' not in empty_html

    # Jonatan cannot configure recurring schedule
    sched_forbidden = client.post(
        "/shop-intel/schedule",
        headers=hdrs,
        data={"site_id": "mock-shop", "enabled": "1", "interval_hours": "12", "mode": "quick"},
    )
    assert sched_forbidden.status_code == 403

    # Jonatan can run manual learning
    run_resp = client.post(
        "/shop-intel/run",
        headers=hdrs,
        data={"site_id": "mock-shop", "mode": "full"},
        follow_redirects=True,
    )
    assert run_resp.status_code == 200
    _assert_strict_csp(run_resp)
    run_html = run_resp.get_data(as_text=True)
    assert "100%" in run_html
    assert "Butiksanalys si-" in run_html
    assert "Plattform &amp; Regler" in run_html

    m = re.search(r"/shop-intel/runs/(si-[a-f0-9]{12})", run_html)
    assert m is not None
    run_id = m.group(1)

    # Filter by domain & query works via GET without JS
    filt_resp = client.get(
        "/shop-intel?site_id=mock-shop&domain=platform_rules&q=SEK",
        headers=hdrs,
    )
    assert filt_resp.status_code == 200
    _assert_strict_csp(filt_resp)
    assert "SEK" in filt_resp.get_data(as_text=True)

    # Run detail view renders facts, findings, diffs, and tailored context previews
    detail_resp = client.get(f"/shop-intel/runs/{run_id}", headers=hdrs)
    assert detail_resp.status_code == 200
    _assert_strict_csp(detail_resp)
    detail_html = detail_resp.get_data(as_text=True)
    assert run_id in detail_html
    assert "SHOP_INTEL_CONTEXT" in detail_html
    for purpose in ("support_draft", "product_copy", "wp_coder", "security_audit"):
        assert f'data-context-purpose="{purpose}"' in detail_html

    # Export JSON
    exp_json = client.get("/shop-intel/export?site_id=mock-shop&format=json", headers=hdrs)
    assert exp_json.status_code == 200
    assert "attachment;" in exp_json.headers.get("Content-Disposition", "")
    assert "shop-intel-mock-shop.json" in exp_json.headers.get("Content-Disposition", "")
    parsed = json.loads(exp_json.get_data(as_text=True))
    assert parsed["run_id"] == run_id
    assert parsed["site_id"] == "mock-shop"

    # Export Markdown
    exp_md = client.get("/shop-intel/export?site_id=mock-shop&format=md", headers=hdrs)
    assert exp_md.status_code == 200
    assert "attachment;" in exp_md.headers.get("Content-Disposition", "")
    assert "shop-intel-mock-shop.md" in exp_md.headers.get("Content-Disposition", "")
    md_text = exp_md.get_data(as_text=True)
    assert "# Shop Intelligence Dossier" in md_text
    assert run_id in md_text


def test_oscar_can_configure_schedule_and_invalid_inputs_handled(dash_env):
    client = dash_env["client"]
    hdrs = _auth_with_csrf(client, "oscar")

    # Oscar sees the schedule form
    page = client.get("/shop-intel?site_id=mock-shop", headers=hdrs)
    assert page.status_code == 200
    _assert_strict_csp(page)
    assert 'action="/shop-intel/schedule"' in page.get_data(as_text=True)

    # Oscar enables recurring schedule
    res = client.post(
        "/shop-intel/schedule",
        headers=hdrs,
        data={
            "site_id": "mock-shop",
            "enabled": "1",
            "interval_hours": "12",
            "mode": "security",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    _assert_strict_csp(res)
    html = res.get_data(as_text=True)
    assert "Aktivt (12h)" in html
    assert "var 12:e timme, läge security" in html

    # Invalid run_id / missing snapshot export / invalid format
    assert client.get("/shop-intel/runs/not-valid-id", headers=hdrs).status_code == 404
    assert client.get("/shop-intel/runs/si-deadbeef1234", headers=hdrs).status_code == 404
    assert (
        client.get("/shop-intel/export?site_id=mock-shop&format=xml", headers=hdrs).status_code
        == 400
    )
    assert (
        client.get("/shop-intel/export?site_id=mock-shop&format=json", headers=hdrs).status_code
        == 404
    )
