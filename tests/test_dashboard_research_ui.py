"""Tests for Dashboard Research Hub UI, auto-selection, Oscar-only setup, detail & contextual panels, CSP/CSS."""

from __future__ import annotations

import base64
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"

_RESEARCH_ENV_KEYS = (
    "PARALLEL_API_KEY",
    "EXA_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "SEARXNG_API_KEY",
    "AZOM_SEARCH_ROUTE_ORDER",
    "AZOM_RESEARCH_DEFAULT_COUNTRY",
    "AZOM_RESEARCH_DEFAULT_LANG",
    "AZOM_WEB_SEARCH_ENABLED",
    "AZOM_WEB_FETCH_ENABLED",
    "AZOM_RESEARCH_ENABLED",
    "AZOM_WEB_SEARCH_KILL",
    "AZOM_RESEARCH_KILL",
    "AZOM_SEARCH_ALLOWED_DOMAINS",
    "SEARXNG_BASE_URL",
)


class _StrictCspHtmlAudit(HTMLParser):
    """Audit rendered HTML for inline executable scripts and on* event handlers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_events: list[tuple[str, str]] = []
        self.bad_scripts: list[str] = []
        self.hrefs: list[str] = []
        self._in_non_json_script = False
        self._script_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        for key in attr_map:
            if key.startswith("on"):
                self.inline_events.append((tag, key))
        t = tag.lower()
        if t == "a" and "href" in attr_map:
            self.hrefs.append(attr_map["href"])
        if t == "script":
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


@pytest.fixture(autouse=True)
def _no_live_sockets(monkeypatch):
    """Block all live TCP/network sockets across dashboard UI tests."""

    def _blocked_connect(*_args, **_kwargs):
        raise AssertionError("Live network socket connection attempted in offline test")

    monkeypatch.setattr(socket, "create_connection", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    for name in (
        "azom_dashboard_research_ui_test",
        "auth",
        "cases_routes",
        "core_routes",
        "health_routes",
        "marketing_routes",
        "oauth_routes",
        "oscar_routes",
        "research_routes",
        "secret_probes",
        "settings_store",
        "status",
        "support",
        "webhooks",
        "wp",
        "wp_routes",
    ):
        sys.modules.pop(name, None)
    for key in [k for k in list(sys.modules) if k.startswith(("wp.", "infrastructure.dashboard"))]:
        sys.modules.pop(key, None)

    spec = importlib.util.spec_from_file_location(
        "azom_dashboard_research_ui_test", DASH_DIR / "app.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_research_ui_test"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    mod.app.config["TESTING"] = True
    return mod.app


@pytest.fixture
def dash_env(tmp_path, monkeypatch):
    from ecom_ops.integrations.search.router import reset_search_router
    from ecom_ops.research.config import clear_research_config_cache
    from ecom_ops.runtime_env import reset_bootstrap_for_tests

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for item in (ROOT / "config").iterdir():
        if item.is_file():
            (cfg_dir / item.name).write_text(item.read_text(encoding="utf-8"), encoding="utf-8")

    for k in _RESEARCH_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg_dir))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(data_dir / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-research-ui-secret")
    monkeypatch.chdir(DASH_DIR)

    reset_bootstrap_for_tests()
    clear_research_config_cache()
    reset_search_router()

    app = _load_dashboard_app()
    client = app.test_client()
    try:
        yield {"app": app, "client": client, "data_dir": data_dir, "cfg_dir": cfg_dir}
    finally:
        for k in _RESEARCH_ENV_KEYS:
            os.environ.pop(k, None)
        reset_bootstrap_for_tests()
        clear_research_config_cache()
        reset_search_router()


def _basic(user: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{user}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _auth_csrf(client, user: str) -> dict[str, str]:
    hdrs = _basic(user)
    resp = client.get("/", headers=hdrs)
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        tok = sess.get("csrf_token")
    assert tok
    hdrs["X-CSRF-Token"] = str(tok)
    return hdrs


def _assert_strict_csp_and_html(resp, path: str) -> str:
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
    assert "'unsafe-eval'" not in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    html = resp.get_data(as_text=True)
    audit = _StrictCspHtmlAudit()
    audit.feed(html)
    assert not audit.bad_scripts, f"{path}: {audit.bad_scripts}"
    assert not audit.inline_events, f"{path}: {audit.inline_events}"
    return html


def test_research_hub_ui_rendering_oscar_vs_jonatan(dash_env):
    """Hub renders auto defaults, 8 presets, 12 profile cards, readiness badges, and Oscar-only setup."""
    from ecom_ops.research import list_profiles

    client = dash_env["client"]
    oscar = _auth_csrf(client, "oscar")
    jonatan = _auth_csrf(client, "jonatan")

    # 1. Oscar view: full Hub + writable #research-setup
    osc_resp = client.get("/research", headers=oscar)
    assert osc_resp.status_code == 200
    osc_html = osc_resp.get_data(as_text=True)

    # Default auto profile & depth selectors
    assert osc_html.count('<option value="auto" selected>Automatisk (rekommenderad)</option>') == 2
    assert '<option value="auto" selected>Automatisk (följer profil)</option>' in osc_html

    # All 8 quick_presets
    expected_presets = (
        "thankyou_ux",
        "product_spec_carplay",
        "support_postnord",
        "ads_ga4",
        "seo_gaps",
        "competitor_nordic",
        "gdpr_consumer_law",
        "triangulated_deep",
    )
    for preset_id in expected_presets:
        assert f'data-research-preset="{preset_id}"' in osc_html

    # Ambition filter buttons
    for amb in ("all", "quick", "standard", "deep"):
        assert f'data-profile-ambition-filter="{amb}"' in osc_html

    # All 13 profile cards + search/deep apply buttons
    profiles = list_profiles()
    assert len(profiles) == 13
    assert osc_html.count('data-profile-card="true"') == 13
    for prof in profiles:
        assert f'data-apply-profile-search="{prof.id}"' in osc_html
        assert f'data-apply-profile-deep="{prof.id}"' in osc_html

    # Provider readiness badges
    for label in ("Parallel", "Exa", "Brave Search", "Tavily", "SearXNG"):
        assert label in osc_html
    assert "Ej nyckel" in osc_html

    # Oscar-only #research-setup controls
    assert 'id="research-setup"' in osc_html
    assert 'action="/research/config/keys"' in osc_html
    assert 'action="/research/config/settings"' in osc_html
    assert 'action="/research/config/test"' in osc_html
    for env_key in (
        "PARALLEL_API_KEY",
        "EXA_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "SEARXNG_API_KEY",
    ):
        assert f'name="{env_key}"' in osc_html
        assert f'data-toggle-secret="research-key-{env_key}"' in osc_html
    for gate in (
        "AZOM_USE_MOCK: 1",
        "AZOM_WEB_SEARCH_ENABLED: 0",
        "AZOM_WEB_FETCH_ENABLED: 0",
        "AZOM_RESEARCH_ENABLED: 0",
        "AZOM_WEB_SEARCH_KILL: 0",
        "AZOM_RESEARCH_KILL: 0",
    ):
        assert gate in osc_html

    # 2. Jonatan view: read-only #research-setup without secret/settings/test forms
    jon_resp = client.get("/research", headers=jonatan)
    assert jon_resp.status_code == 200
    jon_html = jon_resp.get_data(as_text=True)
    assert 'id="research-setup"' in jon_html
    assert "Sökmotorstatus" in jon_html
    assert 'action="/research/config/keys"' not in jon_html
    assert 'action="/research/config/settings"' not in jon_html
    assert 'action="/research/config/test"' not in jon_html
    assert "data-toggle-secret" not in jon_html
    assert 'name="PARALLEL_API_KEY"' not in jon_html
    assert "Spara API-nycklar" not in jon_html


def test_auto_selection_recommend_search_and_jobs(dash_env, monkeypatch):
    """Auto-selection works across /research/recommend, /research/search, and /research/jobs."""
    import research_routes as rr_mod
    from ecom_ops.rbac import Actor
    from ecom_ops.research.store import ResearchStore

    client = dash_env["client"]
    data_dir = dash_env["data_dir"]
    jonatan = _auth_csrf(client, "jonatan")

    # 1. /research/recommend requires auth & permission
    assert client.get("/research/recommend?q=Konverteringsanalys+tacksida").status_code == 401

    with monkeypatch.context() as mp:
        mp.setattr(
            rr_mod,
            "_dashboard_actor",
            lambda _raw: Actor(name="restricted", role="none"),
        )
        denied = client.get("/research/recommend?q=Konverteringsanalys+tacksida", headers=jonatan)
        assert denied.status_code == 403
        assert denied.get_json() == {"ok": False, "error": "forbidden"}

    rec = client.get(
        "/research/recommend?q=Konverteringsanalys+tacksida&ambition=auto",
        headers=jonatan,
    )
    assert rec.status_code == 200
    rec_json = rec.get_json()
    assert rec_json["ok"] is True
    assert rec_json["profile"]["id"] == "conversion_teardown"
    assert rec_json["resolved_depth"] == "deep"
    assert isinstance(rec_json["configured_providers"], list)

    # 2. POST /research/search with profile_id=auto resolves profile & blocks PII
    search_ok = client.post(
        "/research/search",
        headers=jonatan,
        data={
            "_csrf": jonatan["X-CSRF-Token"],
            "query": "Konverteringsanalys tacksida checkout",
            "profile_id": "auto",
            "locale": "SE",
        },
    )
    assert search_ok.status_code == 200
    search_html = search_ok.get_data(as_text=True)
    assert 'data-quick-search-results="true"' in search_html
    assert "Auto: CRO &amp; Konverteringsanalys" in search_html

    search_pii = client.post(
        "/research/search",
        headers=jonatan,
        data={
            "_csrf": jonatan["X-CSRF-Token"],
            "query": "Order 104218 anna.andersson@example.com",
            "profile_id": "auto",
        },
    )
    assert search_pii.status_code == 200
    pii_html = search_pii.get_data(as_text=True)
    assert "Sökningen blockerades av säkerhetspolicy" in pii_html
    assert 'data-quick-search-results="true"' not in pii_html

    # 3. POST /research/jobs with profile_id=auto and depth=auto
    create_resp = client.post(
        "/research/jobs",
        headers=jonatan,
        data={
            "_csrf": jonatan["X-CSRF-Token"],
            "topic": "Konverteringsanalys av tacksida och varukorg",
            "profile_id": "auto",
            "depth": "auto",
            "market": "SE",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 302
    loc = create_resp.headers["Location"]
    m = re.search(r"/research/(res-[0-9a-f]{12})", loc)
    assert m, f"Expected redirect to /research/res-*, got {loc}"
    rid = m.group(1)

    store = ResearchStore(
        path=data_dir / "research_jobs.sqlite3",
        artifacts_dir=data_dir / "research_artifacts",
    )
    job = store.get_job(rid)
    assert job is not None
    assert job.spec is not None
    assert job.spec.profile_id == "conversion_teardown"
    assert job.spec.depth == "deep"


def test_oscar_config_endpoints_rbac_and_csrf(dash_env):
    """/research/config/* enforces 401 unauth, 400 CSRF, and 403 for Jonatan."""
    client = dash_env["client"]
    jonatan = _auth_csrf(client, "jonatan")
    oscar_basic = _basic("oscar")

    endpoints = (
        ("/research/config/keys", {"PARALLEL_API_KEY": "par-123"}),
        ("/research/config/settings", {"AZOM_RESEARCH_DEFAULT_COUNTRY": "SE"}),
        ("/research/config/test", {"route_id": "parallel"}),
    )
    for path, payload in endpoints:
        assert client.post(path, data=payload).status_code == 401
        assert client.post(path, headers=oscar_basic, data=payload).status_code == 400
        assert (
            client.post(
                path,
                headers={**oscar_basic, "X-CSRF-Token": "wrong"},
                data={**payload, "_csrf": "wrong"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                path,
                headers=jonatan,
                data={**payload, "_csrf": jonatan["X-CSRF-Token"]},
            ).status_code
            == 403
        )


def test_oscar_config_keys_saves_without_clobber_or_secret_leak(dash_env):
    """POST /research/config/keys writes secrets.env cleanly, keeps untouched keys, never leaks secrets."""
    client = dash_env["client"]
    data_dir = dash_env["data_dir"]
    oscar = _auth_csrf(client, "oscar")

    existing_brave = "bsa-existing-keep-secret-999"
    par_secret = "par-new-secret-token-111"
    exa_secret = "exa-new-secret-token-222"
    (data_dir / "secrets.env").write_text(
        f"BRAVE_SEARCH_API_KEY={existing_brave}\n",
        encoding="utf-8",
    )

    resp = client.post(
        "/research/config/keys",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "PARALLEL_API_KEY": par_secret,
            "EXA_API_KEY": exa_secret,
            "BRAVE_SEARCH_API_KEY": "",
            "TAVILY_API_KEY": "",
            "SEARXNG_API_KEY": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert "/research" in loc
    assert loc.endswith("#research-setup")
    for secret in (par_secret, exa_secret, existing_brave):
        assert secret not in loc
        assert secret not in resp.get_data(as_text=True)

    # secrets.env updated without clobbering BRAVE_SEARCH_API_KEY
    secrets_text = (data_dir / "secrets.env").read_text(encoding="utf-8")
    assert f"PARALLEL_API_KEY={par_secret}" in secrets_text
    assert f"EXA_API_KEY={exa_secret}" in secrets_text
    assert f"BRAVE_SEARCH_API_KEY={existing_brave}" in secrets_text

    # audit.jsonl records key names only, never values
    audit_text = (data_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "research_keys_updated" in audit_text
    assert "PARALLEL_API_KEY" in audit_text
    assert "EXA_API_KEY" in audit_text
    for secret in (par_secret, exa_secret, existing_brave):
        assert secret not in audit_text

    # GET /research marks configured providers and never echoes secret values
    hub_resp = client.get("/research", headers=oscar)
    assert hub_resp.status_code == 200
    hub_html = hub_resp.get_data(as_text=True)
    for secret in (par_secret, exa_secret, existing_brave):
        assert secret not in hub_html
    assert hub_html.count("Konfigurerad") >= 3

    # Control chars in API key are rejected safely
    bad_resp = client.post(
        "/research/config/keys",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "TAVILY_API_KEY": "bad\nline"},
        follow_redirects=False,
    )
    assert bad_resp.status_code == 302
    assert "Ogiltig" in bad_resp.headers["Location"]
    assert "TAVILY_API_KEY" not in (data_dir / "secrets.env").read_text(encoding="utf-8")


def test_oscar_config_settings_runtime_yaml_and_protected_keys(dash_env):
    """POST /research/config/settings writes runtime.env + research.yaml and ignores PROTECTED_OVERLAY_KEYS."""
    from ecom_ops.research.config import load_research_config
    from settings_store import load_research_settings_view

    client = dash_env["client"]
    data_dir = dash_env["data_dir"]
    cfg_dir = dash_env["cfg_dir"]
    oscar = _auth_csrf(client, "oscar")

    resp = client.post(
        "/research/config/settings",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "AZOM_SEARCH_ROUTE_ORDER": "exa, parallel, exa",
            "AZOM_RESEARCH_DEFAULT_COUNTRY": "no",
            "AZOM_RESEARCH_DEFAULT_LANG": "NB",
            "max_queries_per_day": "150",
            "max_fetches_per_day": "220",
            "monthly_external_usd_cap": "18.5",
            # Attempt injection of PROTECTED_OVERLAY_KEYS (must be ignored/refused)
            "AZOM_WEB_SEARCH_ENABLED": "1",
            "AZOM_WEB_SEARCH_KILL": "1",
            "SEARXNG_BASE_URL": "https://evil.example/searx",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("#research-setup")

    runtime_text = (data_dir / "runtime.env").read_text(encoding="utf-8")
    assert "AZOM_SEARCH_ROUTE_ORDER=exa,parallel" in runtime_text
    assert "AZOM_RESEARCH_DEFAULT_COUNTRY=NO" in runtime_text
    assert "AZOM_RESEARCH_DEFAULT_LANG=nb" in runtime_text
    assert "AZOM_WEB_SEARCH_ENABLED" not in runtime_text
    assert "AZOM_WEB_SEARCH_KILL" not in runtime_text
    assert "SEARXNG_BASE_URL" not in runtime_text
    assert "AZOM_WEB_SEARCH_ENABLED" not in os.environ
    assert "AZOM_WEB_SEARCH_KILL" not in os.environ
    assert "SEARXNG_BASE_URL" not in os.environ

    # Verify config/research.yaml was updated and config cache cleared
    yaml_text = (cfg_dir / "research.yaml").read_text(encoding="utf-8")
    assert "max_queries_per_day: 150" in yaml_text
    cfg = load_research_config()
    assert cfg.budgets.max_queries_per_day == 150
    assert cfg.budgets.max_fetches_per_day == 220
    assert cfg.budgets.monthly_external_usd_cap == 18.5
    assert cfg.cache.enabled is False

    # Clearing route_order resets it to automatic and cache_enabled=1 re-enables cache
    reset_resp = client.post(
        "/research/config/settings",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "AZOM_SEARCH_ROUTE_ORDER": "",
            "AZOM_RESEARCH_DEFAULT_COUNTRY": "SE",
            "AZOM_RESEARCH_DEFAULT_LANG": "sv",
            "max_queries_per_day": "200",
            "max_fetches_per_day": "300",
            "monthly_external_usd_cap": "25.0",
            "cache_enabled": "1",
        },
        follow_redirects=False,
    )
    assert reset_resp.status_code == 302
    view = load_research_settings_view()
    assert view["route_order"] == ""
    assert view["cache_enabled"] is True
    assert load_research_config().cache.enabled is True


def test_oscar_config_test_runs_safely_in_mock_mode(dash_env, monkeypatch):
    """POST /research/config/test probes single or all routes safely in mock mode without leaking keys."""
    from ecom_ops.integrations.search.router import reset_search_router

    client = dash_env["client"]
    oscar = _auth_csrf(client, "oscar")

    secret_val = "par-probe-secret-xyz"
    monkeypatch.setenv("PARALLEL_API_KEY", secret_val)
    reset_search_router()

    single = client.post(
        "/research/config/test",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "route_id": "parallel"},
        follow_redirects=False,
    )
    assert single.status_code == 302
    loc_single = single.headers["Location"]
    assert loc_single.endswith("#research-setup")
    assert "parallel" in loc_single
    assert secret_val not in loc_single

    hub_single = client.get("/research", headers=oscar).get_data(as_text=True)
    assert "Söktest: parallel: ok" in hub_single
    assert secret_val not in hub_single

    all_resp = client.post(
        "/research/config/test",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "route_id": "all"},
        follow_redirects=False,
    )
    assert all_resp.status_code == 302
    assert all_resp.headers["Location"].endswith("#research-setup")
    hub_all = client.get("/research", headers=oscar).get_data(as_text=True)
    assert "parallel: ok" in hub_all
    assert "exa: missing" in hub_all
    assert secret_val not in hub_all


def test_detail_and_contextual_panels_render_profile_selectors_and_csp(dash_env):
    """Detail view shows profile_meta + Hub link; contextual panels include auto profile_id, target_ref, _csrf."""
    from ecom_ops.cases.store import CaseStore
    from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme

    client = dash_env["client"]
    data_dir = dash_env["data_dir"]
    jonatan = _auth_csrf(client, "jonatan")
    oscar = _auth_csrf(client, "oscar")

    # Create job with auto -> conversion_teardown
    create_resp = client.post(
        "/research/jobs",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "topic": "Konverteringsanalys tacksida checkout",
            "profile_id": "auto",
            "depth": "auto",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 302
    m = re.search(r"/research/(res-[0-9a-f]{12})", create_resp.headers["Location"])
    assert m
    rid = m.group(1)

    detail_resp = client.get(f"/research/{rid}", headers=oscar)
    assert detail_resp.status_code == 200
    detail_html = _assert_strict_csp_and_html(detail_resp, f"/research/{rid}")
    assert "CRO &amp; Konverteringsanalys" in detail_html
    assert "(staged / parallel_extract_first)" in detail_html
    assert 'href="/research?profile=conversion_teardown"' in detail_html
    assert "Ny liknande i Hub" in detail_html

    # Case detail contextual panel
    case_store = CaseStore(path=data_dir / "cases.db")
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="Paket saknas",
        from_addr="kund@example.com",
        body="Fråga om PostNord paketombud.",
        category="shipping",
        draft_reply="Vi kollar.",
        order_id="12345",
        message_id="<ctx-ui-case@azom>",
        site="azom",
    )

    # WP review plan
    plans = ReviewPlans(data_dir=data_dir)
    opened = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert opened.ok
    plan_id = opened.data["plan_id"]

    contextual_cases = (
        (f"/cases/{case.id}", jonatan, f"case:{case.id}"),
        ("/marketing", jonatan, "marketing:general"),
        ("/wp/mock-staging", oscar, "wp_site:mock-staging"),
        (f"/wp/mock-staging/reviews/{plan_id}", oscar, f"wp:mock-staging:{plan_id}"),
    )
    for path, hdrs, expected_ref in contextual_cases:
        resp = client.get(path, headers=hdrs)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        html = _assert_strict_csp_and_html(resp, path)
        assert '<select' in html and 'name="profile_id"' in html, f"{path} missing profile_id select"
        assert '<option value="auto">Automatisk (rekommenderad)</option>' in html, (
            f"{path} missing auto option"
        )
        assert f'name="target_ref" value="{expected_ref}"' in html, (
            f"{path} missing target_ref={expected_ref}"
        )
        assert 'name="_csrf" value="' in html, f"{path} missing _csrf input"

    # Also strict CSP audit on /research Hub
    hub_resp = client.get("/research", headers=oscar)
    assert hub_resp.status_code == 200
    _assert_strict_csp_and_html(hub_resp, "/research")


def test_compiled_css_template_audit_and_dashboard_js_syntax():
    """All classes in edited research templates exist in compiled dashboard.css and dashboard.js passes node --check."""
    css = (DASH_DIR / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
    edited_templates = (
        "research/hub.html",
        "research/detail.html",
        "case_detail.html",
        "marketing.html",
        "wp_site.html",
        "wp_review.html",
    )
    class_attr_re = re.compile(r'(?<![:\w-])class="([^"]*)"')
    missing: list[tuple[str, str]] = []
    for rel in edited_templates:
        tpl_path = DASH_DIR / "templates" / rel
        raw = tpl_path.read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe\b", raw), f"forbidden '| safe' in {rel}"
        assert not re.search(r"\bon[a-z]+\s*=", raw, re.IGNORECASE), f"inline on* in {rel}"
        for m in re.finditer(r"<script\b([^>]*)>", raw, re.IGNORECASE):
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

    js_path = DASH_DIR / "static" / "js" / "dashboard.js"
    js_text = js_path.read_text(encoding="utf-8")
    assert "function initResearchHub()" in js_text
    assert "/research/recommend?q=" in js_text
    assert "innerHTML" not in js_text
    assert "new Function" not in js_text

    node_bin = shutil.which("node")
    if node_bin:
        proc = subprocess.run(
            [node_bin, "--check", str(js_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"node --check failed:\n{proc.stderr}"
