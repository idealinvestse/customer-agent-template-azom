"""Regression tests for the modern dashboard rebuild (CSP, P0/P1 hardening, Oscar, WP ops)."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_module():
    """Load a fresh dashboard app module so env/tmp_path changes apply cleanly."""
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    for name in (
        "azom_dashboard_rebuild",
        "auth",
        "cases_routes",
        "core_routes",
        "health_routes",
        "marketing_routes",
        "oauth_routes",
        "oscar_routes",
        "secret_probes",
        "settings_store",
        "status",
        "support",
        "webhooks",
        "wp",
        "wp_routes",
    ):
        sys.modules.pop(name, None)
    for key in [
        k for k in list(sys.modules) if k.startswith(("wp.", "infrastructure.dashboard.wp"))
    ]:
        sys.modules.pop(key, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard_rebuild", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_rebuild"] = mod
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
    resp = client.get("/", headers=headers)
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        csrf = sess.get("csrf_token")
    assert csrf
    headers["X-CSRF-Token"] = str(csrf)
    return headers


@pytest.fixture
def rebuild_env(tmp_path, monkeypatch):
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
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-rebuild-dashboard-secret")
    for var in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(DASH_DIR)

    from ecom_ops.rbac import clear_rbac_cache

    clear_rbac_cache()
    mod = _load_dashboard_module()
    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "cfg_dir": cfg_dir,
        "mod": mod,
        "app": mod.app,
        "client": mod.app.test_client(),
    }


class _HtmlSecurityAudit(HTMLParser):
    """Collect inline script violations, inline on* attributes, and form inputs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_events: list[tuple[str, str]] = []
        self.bad_scripts: list[str] = []
        self.inputs: list[dict[str, str]] = []
        self.textareas: list[dict[str, str]] = []
        self.selects: list[dict[str, str]] = []
        self._in_non_json_script = False
        self._script_has_src = False
        self._script_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        for key in attr_map:
            if key.startswith("on"):
                self.inline_events.append((tag, key))
        t = tag.lower()
        if t == "script":
            stype = attr_map.get("type", "").strip().lower()
            if stype != "application/json":
                self._in_non_json_script = True
                self._script_has_src = bool(attr_map.get("src", "").strip())
                self._script_buf = []
                if not self._script_has_src:
                    self.bad_scripts.append(f"<script {attr_map}> missing src")
        elif t == "input":
            self.inputs.append(attr_map)
        elif t == "textarea":
            self.textareas.append(attr_map)
        elif t == "select":
            self.selects.append(attr_map)

    def handle_data(self, data: str) -> None:
        if self._in_non_json_script:
            self._script_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_non_json_script:
            body = "".join(self._script_buf).strip()
            if body:
                self.bad_scripts.append(f"non-empty inline script: {body[:60]!r}")
            self._in_non_json_script = False
            self._script_has_src = False
            self._script_buf = []


def test_csp_header_and_static_assets_and_favicon(rebuild_env):
    client = rebuild_env["client"]
    hdrs = _basic_headers("oscar")

    home = client.get("/", headers=hdrs)
    assert home.status_code == 200
    csp = home.headers.get("Content-Security-Policy", "")
    directives = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, val = part.partition(" ")
        directives[name.lower()] = val.strip()

    assert directives.get("script-src") == "'self'"
    assert "'unsafe-eval'" not in directives.get("script-src", "")
    assert "'unsafe-inline'" not in directives.get("script-src", "")
    assert "http:" not in directives.get("script-src", "")
    assert "https:" not in directives.get("script-src", "")
    assert directives.get("font-src") == "'self'"
    assert directives.get("base-uri") == "'self'"
    assert directives.get("form-action") == "'self'"
    assert directives.get("object-src") == "'none'"

    fav = client.get("/favicon.ico")
    assert fav.status_code == 200
    assert fav.mimetype == "image/svg+xml"

    for asset in (
        "/static/css/dashboard.css",
        "/static/js/alpine-csp.min.js",
        "/static/js/dashboard.js",
        "/static/fonts/sora-latin-400-normal.woff2",
        "/static/fonts/source-sans-3-latin-400-normal.woff2",
        "/static/fonts/jetbrains-mono-latin-400-normal.woff2",
    ):
        resp = client.get(asset)
        assert resp.status_code == 200, f"expected 200 for {asset}, got {resp.status_code}"
        assert len(resp.data) > 0


def test_zero_inline_scripts_or_event_handlers_across_pages(rebuild_env):
    client = rebuild_env["client"]
    hdrs = _basic_headers("oscar")
    pages = (
        "/",
        "/onboarding",
        "/cases",
        "/marketing",
        "/interact",
        "/settings",
        "/secrets",
        "/oscar",
        "/oscar/secrets",
        "/wp",
        "/wp/mock-staging",
    )
    for path in pages:
        resp = client.get(path, headers=hdrs)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        html = resp.get_data(as_text=True)
        audit = _HtmlSecurityAudit()
        audit.feed(html)
        assert not audit.bad_scripts, f"{path} had inline/non-CSP scripts: {audit.bad_scripts}"
        assert not audit.inline_events, f"{path} had inline on* handlers: {audit.inline_events}"


def test_cases_post_fail_closed_missing_unknown_or_empty_body(rebuild_env, monkeypatch):
    from ecom_ops.cases.service import CaseService
    from ecom_ops.cases.store import CaseStore

    data_dir = rebuild_env["data_dir"]
    store = CaseStore(path=data_dir / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="kund@example.com",
        body="Var är min order?",
        category="order_status",
        draft_reply="GAMMALT UTKAST SOM INTE SKA SKICKAS",
        order_id="1001",
        message_id="<fail-closed-case@azom>",
        site="azom",
    )

    send_calls: list[tuple] = []

    def _spy_approve(self, *args, **kwargs):
        send_calls.append((args, kwargs))
        raise AssertionError("approve_and_send should not be called")

    monkeypatch.setattr(CaseService, "approve_and_send", _spy_approve)

    client = rebuild_env["client"]
    hdrs = _auth_with_csrf(client, "jonatan")

    # Missing action
    r_missing = client.post(
        f"/cases/{case.id}",
        headers=hdrs,
        data={"_csrf": hdrs["X-CSRF-Token"], "body": "Hej"},
        follow_redirects=False,
    )
    assert r_missing.status_code == 302
    assert "err=" in r_missing.headers.get("Location", "")

    # Unknown action
    r_unknown = client.post(
        f"/cases/{case.id}",
        headers=hdrs,
        data={"_csrf": hdrs["X-CSRF-Token"], "action": "send_silently", "body": "Hej"},
        follow_redirects=False,
    )
    assert r_unknown.status_code == 302
    assert "err=" in r_unknown.headers.get("Location", "")

    # Reply with empty body must NOT fall back to old draft_reply
    r_empty = client.post(
        f"/cases/{case.id}",
        headers=hdrs,
        data={"_csrf": hdrs["X-CSRF-Token"], "action": "reply", "body": "   "},
        follow_redirects=False,
    )
    assert r_empty.status_code == 302
    assert "err=" in r_empty.headers.get("Location", "")
    assert not send_calls
    reloaded = store.get(case.id)
    assert reloaded is not None
    assert reloaded.status == "open"


def test_secret_probes_scrub_bot_tokens_before_return_and_persist(rebuild_env, monkeypatch):
    import secret_probes

    data_dir = rebuild_env["data_dir"]
    client = rebuild_env["client"]
    hdrs = _auth_with_csrf(client, "oscar")

    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "oscar")
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET_TOKEN")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "42:oscar")

    def _boom_urlopen(*_args, **_kwargs):
        raise RuntimeError(
            "GET https://api.telegram.org/bot123456:SECRET/getMe failed: bot123456:SECRET_TOKEN"
        )

    monkeypatch.setattr(secret_probes, "urlopen", _boom_urlopen)

    probe_res = secret_probes.probe_telegram()
    assert probe_res.status == "error"
    assert "SECRET_TOKEN" not in probe_res.message
    assert "bot123456:SECRET" not in probe_res.message
    assert "api.telegram.org/bot" not in probe_res.message

    # Trigger run_all_probes() + /oscar/secrets/test (writes probe_history.jsonl and probe_last.json)
    resp = client.post(
        "/oscar/secrets/test",
        headers={**hdrs, "Accept": "application/json"},
        data={"_csrf": hdrs["X-CSRF-Token"], "probe": "all"},
    )
    assert resp.status_code == 200
    body_text = resp.get_data(as_text=True)
    assert "SECRET_TOKEN" not in body_text
    assert "bot123456:SECRET" not in body_text

    # Also test single probe endpoint which updates probe_last.json
    single = client.post(
        "/oscar/secrets/test",
        headers={**hdrs, "Accept": "application/json"},
        data={"_csrf": hdrs["X-CSRF-Token"], "probe": "telegram"},
    )
    assert single.status_code == 200
    assert "SECRET_TOKEN" not in single.get_data(as_text=True)
    assert "bot123456:SECRET" not in single.get_data(as_text=True)

    hist_path = data_dir / "probe_history.jsonl"
    assert hist_path.is_file()
    hist_text = hist_path.read_text(encoding="utf-8")
    assert "SECRET_TOKEN" not in hist_text
    assert "bot123456:SECRET" not in hist_text

    last_path = data_dir / "probe_last.json"
    assert last_path.is_file()
    last_text = last_path.read_text(encoding="utf-8")
    assert "SECRET_TOKEN" not in last_text
    assert "bot123456:SECRET" not in last_text


def test_save_secrets_rejects_newline_injection(rebuild_env):
    import settings_store

    data_dir = rebuild_env["data_dir"]
    secrets_path = data_dir / "secrets.env"

    with pytest.raises(ValueError):
        settings_store.save_secrets({"MAIL_FROM": "support@azom.se\nINJECTED_KEY=evil"})
    assert not secrets_path.exists()

    with pytest.raises(ValueError):
        settings_store.save_secrets({"MAIL_FROM": "support@azom.se\rEVIL=1"})
    assert not secrets_path.exists()

    client = rebuild_env["client"]
    hdrs = _auth_with_csrf(client, "oscar")
    resp = client.post(
        "/oscar/secrets",
        headers=hdrs,
        data={"_csrf": hdrs["X-CSRF-Token"], "MAIL_FROM": "a@b.se\r\nINJECTED_HTTP=1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Ogiltiga kontrolltecken" in html
    if secrets_path.is_file():
        assert "INJECTED_HTTP" not in secrets_path.read_text(encoding="utf-8")


def test_telemetry_and_logs_redact_secret_meta_keys(rebuild_env):
    data_dir = rebuild_env["data_dir"]
    tel_row = {
        "ts": "2026-11-25T08:00:00+00:00",
        "action": "llm_complete",
        "site": "azom",
        "cost_usd": 0.001,
        "meta": {
            "route_id": "openrouter",
            "prompt_tokens": 15,
            "access_token": "secret-access-token-xyz",
            "refresh_token": "secret-refresh-token-xyz",
            "api_key": "secret-api-key-xyz",
            "authorization": "Bearer super-secret",
            "client_secret": "shhh",
        },
    }
    (data_dir / "telemetry.jsonl").write_text(
        json.dumps(tel_row) + "\n", encoding="utf-8"
    )

    client = rebuild_env["client"]
    hdrs = _basic_headers("jonatan")

    tel_resp = client.get("/telemetry", headers=hdrs)
    assert tel_resp.status_code == 200
    tel_text = tel_resp.get_data(as_text=True)
    for forbidden in (
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        "client_secret",
        "secret-access-token-xyz",
        "secret-refresh-token-xyz",
        "secret-api-key-xyz",
        "Bearer super-secret",
        "shhh",
    ):
        assert forbidden not in tel_text
    tel_json = tel_resp.get_json()
    assert tel_json[0]["meta"]["route_id"] == "openrouter"
    assert tel_json[0]["meta"]["prompt_tokens"] == 15

    logs_resp = client.get("/logs", headers=hdrs)
    assert logs_resp.status_code == 200
    logs_text = logs_resp.get_data(as_text=True)
    assert "access_token" not in logs_text
    assert "secret-access-token-xyz" not in logs_text
    assert "Bearer super-secret" not in logs_text


def test_oscar_escalations_secrets_profiles_and_behavior(rebuild_env):
    import settings_store

    data_dir = rebuild_env["data_dir"]
    client = rebuild_env["client"]
    hdrs = _auth_with_csrf(client, "oscar")

    # 1. POST /oscar/escalations resolve redirects cleanly with single '?'
    esc_path = data_dir / "escalations.jsonl"
    esc_path.write_text(
        json.dumps(
            {
                "id": "esc-1234",
                "status": "open",
                "severity": "critical",
                "reason": "critical",
                "summary": "Behöver kontroll",
                "created_at": "2026-11-25T07:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    resolved = client.post(
        "/oscar/escalations?show=open",
        headers=hdrs,
        data={"_csrf": hdrs["X-CSRF-Token"], "ticket_id": "esc-1234"},
        follow_redirects=False,
    )
    assert resolved.status_code == 302
    loc = resolved.headers.get("Location", "")
    assert "??" not in loc
    assert loc.count("?") == 1
    assert "show=open" in loc

    # 2. GET /oscar/secrets renders all 49 EDITABLE_SECRET_KEYS with non-password inputs for URL/host/email
    assert len(settings_store.EDITABLE_SECRET_KEYS) == 49
    sec_page = client.get("/oscar/secrets", headers=hdrs)
    assert sec_page.status_code == 200
    sec_html = sec_page.get_data(as_text=True)
    parser = _HtmlSecurityAudit()
    parser.feed(sec_html)

    input_by_name = {inp["name"]: inp for inp in parser.inputs if "name" in inp}
    select_names = {sel["name"] for sel in parser.selects if "name" in sel}
    rendered_names = set(input_by_name) | select_names
    for key in settings_store.EDITABLE_SECRET_KEYS:
        assert key in rendered_names, f"Missing editable secret field {key} in /oscar/secrets"

    for non_secret_key in (
        "WOO_BASE_URL",
        "SMTP_HOST",
        "IMAP_HOST",
        "SSH_HOST",
        "MAIL_FROM",
        "GRAPH_USER",
        "AZOM_DASHBOARD_PUBLIC_URL",
        "MAIL_OAUTH_REDIRECT_URI",
        "GOOGLE_OAUTH_REDIRECT_URI",
    ):
        assert input_by_name[non_secret_key].get("type") != "password"

    # 3. POST /oscar/models/profiles/save accepts wp_code_deep temperature=0.12 & reasoning_effort=xhigh
    prof_resp = client.post(
        "/oscar/models/profiles/save",
        headers=hdrs,
        data={
            "_csrf": hdrs["X-CSRF-Token"],
            "profile_id": "wp_code_deep",
            "prompt_ref": "wp_coder",
            "max_output_tokens": "4096",
            "temperature": "0.12",
            "reasoning_effort": "xhigh",
            "idle_s": "60",
            "hard_s": "180",
        },
        follow_redirects=True,
    )
    assert prof_resp.status_code == 200
    assert "Profil wp_code_deep sparad" in prof_resp.get_data(as_text=True)
    override_yaml = yaml.safe_load(
        (data_dir / "model_profiles.override.yaml").read_text(encoding="utf-8")
    )
    saved_gen = override_yaml["profiles"]["wp_code_deep"]["generation"]
    assert saved_gen["temperature"] == 0.12
    assert saved_gen["reasoning_effort"] == "xhigh"

    # 4. POST /oscar/behavior preserves submitted text on validation error
    custom_draft = "BEVARAD_KLASSIFICERING_VID_FEL_MARKER"
    beh_resp = client.post(
        "/oscar/behavior",
        headers=hdrs,
        data={
            "_csrf": hdrs["X-CSRF-Token"],
            "chat": "",  # invalid: empty raises ValueError
            "classify": custom_draft,
            "draft": "Ett giltigt utkast.",
            "product_desc": "Giltig produkttext.",
            "wp_coder": "Giltig wp-kodare.",
        },
        follow_redirects=False,
    )
    assert beh_resp.status_code == 200
    beh_html = beh_resp.get_data(as_text=True)
    assert custom_draft in beh_html


def test_wp_review_operator_and_site_orders_mutated_and_css_classes(rebuild_env, monkeypatch):
    from ecom_ops.wp_ops import WpOpsService
    from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme

    data_dir = rebuild_env["data_dir"]
    mod = rebuild_env["mod"]
    client = rebuild_env["client"]

    # Expose a drill with orders_mutated=True via WpOpsService._drills_for so wp_site renders it honestly
    monkeypatch.setattr(
        WpOpsService,
        "_drills_for",
        lambda self, state, site_id: [
            {
                "id": "drill-mutated-1",
                "target_site_id": "mock-staging",
                "recovery_seconds": 1.5,
                "orders_mutated": True,
                "order_reconciliation": "diff: 1 order changed",
            }
        ],
    )

    # Create a backup as oscar so wp_site renders the drill <select class="az-select"> too
    oscar_hdrs = _auth_with_csrf(client, "oscar")
    assert (
        client.post(
            "/wp/mock-shop/backup",
            headers=oscar_hdrs,
            data={"_csrf": oscar_hdrs["X-CSRF-Token"]},
        ).status_code
        == 302
    )

    site_resp = client.get("/wp/mock-shop", headers=oscar_hdrs)
    assert site_resp.status_code == 200
    site_html = site_resp.get_data(as_text=True)
    assert 'data-orders-mutated="true"' in site_html
    assert "ordrar ändrade" in site_html
    site_audit = _HtmlSecurityAudit()
    site_audit.feed(site_html)
    assert any("az-select" in s.get("class", "") for s in site_audit.selects)

    # Open a wp_review plan and check wp_files + wp_review classes & operator (agent) RBAC
    plans = ReviewPlans(data_dir=data_dir)
    opened = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert opened.ok
    plan_id = opened.data["plan_id"]

    files_resp = client.get(
        "/wp/mock-staging/files?kind=theme&slug=twentytwentyfive&path=style.css",
        headers=oscar_hdrs,
    )
    assert files_resp.status_code == 200
    files_audit = _HtmlSecurityAudit()
    files_audit.feed(files_resp.get_data(as_text=True))
    assert any("az-textarea" in ta.get("class", "") for ta in files_audit.textareas)

    oscar_rev = client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=oscar_hdrs)
    assert oscar_rev.status_code == 200
    rev_audit = _HtmlSecurityAudit()
    rev_audit.feed(oscar_rev.get_data(as_text=True))
    assert any("az-textarea" in ta.get("class", "") for ta in rev_audit.textareas)

    # Operator ('agent', role='operator') sees checks/preview controls (can_drill) without review/staging
    import auth as auth_mod

    orig_auth = auth_mod._authenticate

    def _fake_auth(username: str, password: str):
        if username == "agent" and password == "agent":
            return {"name": "agent", "role": "operator", "is_oscar": False}
        return orig_auth(username, password)

    monkeypatch.setattr(auth_mod, "_authenticate", _fake_auth)

    agent_hdrs = _auth_with_csrf(client, "agent")
    agent_rev = client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=agent_hdrs)
    assert agent_rev.status_code == 200
    agent_html = agent_rev.get_data(as_text=True)
    assert 'data-review-action="checks"' in agent_html
    assert 'data-review-action="preview"' in agent_html
    assert 'data-review-action="staging"' not in agent_html
    assert "Spara synpunkt" not in agent_html

    # Operator can run checks/preview (200) while review comment remains forbidden (403)
    assert (
        client.post(
            f"/wp/mock-staging/reviews/{plan_id}/checks",
            headers=agent_hdrs,
            data={"_csrf": agent_hdrs["X-CSRF-Token"]},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/wp/mock-staging/reviews/{plan_id}/comment",
            headers=agent_hdrs,
            data={"_csrf": agent_hdrs["X-CSRF-Token"], "text": "hej"},
        ).status_code
        == 403
    )
    assert mod.app is client.application


def test_wp_review_live_overlay_and_site_workspaces_csp_clean(rebuild_env):
    import struct
    import zlib

    from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme
    from ecom_ops.wp_ops.review_workspace import workspace_root

    data_dir = rebuild_env["data_dir"]
    client = rebuild_env["client"]
    oscar_hdrs = _auth_with_csrf(client, "oscar")

    plans = ReviewPlans(data_dir=data_dir)
    p1 = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="Variant A",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    p2 = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="Variant B",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert p1.ok and p2.ok
    pid1, pid2 = p1.data["plan_id"], p2.data["plan_id"]

    store_path = data_dir / "wp_review_plans.json"
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    for row in stored["plans"]:
        if row["plan_id"] == pid1:
            row["variant_group"] = "cta-group"
            row["superseded_by"] = pid2
        elif row["plan_id"] == pid2:
            row["variant_group"] = "cta-group"
            row["forked_from"] = pid1
            row["superseded_by"] = ""
    store_path.write_text(json.dumps(stored), encoding="utf-8")

    # Write minimal valid PNG + preview.html for pid1
    raw = b"\x00" + b"\x11\x22\x33"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")
    adir = workspace_root(data_dir, "mock-staging", pid1) / "artifacts"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "focus-desktop.png").write_bytes(png)
    (adir / "live-desktop.png").write_bytes(png)
    (adir / "preview-desktop.png").write_bytes(png)
    (adir / "preview.html").write_text("<!doctype html><html><body>ok</body></html>", encoding="utf-8")

    # Add a standalone page draft on pid2 to verify CSP cleanliness on standalone gallery + meta card
    root2 = workspace_root(data_dir, "mock-staging", pid2)
    (root2 / "work" / "page_draft.json").write_text(
        json.dumps(
            {
                "slug": "tack-sida",
                "title": "Tack för din order",
                "page_type": "thankyou",
                "shell_path": "/",
                "template_file": "woocommerce/checkout/thankyou.php",
                "synthetic_data": True,
            }
        ),
        encoding="utf-8",
    )
    adir2 = root2 / "artifacts"
    adir2.mkdir(parents=True, exist_ok=True)
    (adir2 / "preview-desktop.png").write_bytes(png)
    (adir2 / "preview-mobile.png").write_bytes(png)
    (adir2 / "preview.json").write_text(
        json.dumps({"preview_kind": "standalone_page", "path": "/tack-sida/", "synthetic_data": True}),
        encoding="utf-8",
    )
    (adir2 / "preview.html").write_text("<!doctype html><html><body>standalone</body></html>", encoding="utf-8")

    for url in ("/wp/mock-staging", f"/wp/mock-staging/reviews/{pid1}", f"/wp/mock-staging/reviews/{pid2}"):
        resp = client.get(url, headers=oscar_hdrs)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        audit = _HtmlSecurityAudit()
        audit.feed(html)
        assert not audit.bad_scripts, f"{url} had inline scripts: {audit.bad_scripts}"
        assert not audit.inline_events, f"{url} had inline on* handlers: {audit.inline_events}"
