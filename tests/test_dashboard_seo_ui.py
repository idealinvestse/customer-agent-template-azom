"""Tests for dedicated /seo Dashboard Studio UI, RBAC, CSRF, CSP, compiled CSS, and CLI seo subcommands."""

from __future__ import annotations

import base64
import csv
import importlib.util
import io
import json
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


class _StrictCspHtmlAudit(HTMLParser):
    """Audit rendered HTML for inline executable scripts and on* event handlers."""

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


@pytest.fixture(autouse=True)
def _no_live_sockets(monkeypatch):
    """Block all live network sockets across SEO dashboard & CLI tests."""

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
        "azom_dashboard_seo_ui_test",
        "auth",
        "cases_routes",
        "core_routes",
        "health_routes",
        "marketing_routes",
        "oauth_routes",
        "oscar_routes",
        "research_routes",
        "seo_routes",
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

    spec = importlib.util.spec_from_file_location("azom_dashboard_seo_ui_test", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_seo_ui_test"] = mod
    spec.loader.exec_module(mod)
    app = mod.create_app()
    app.config["TESTING"] = True
    return app


def _basic_auth(user: str, pw: str) -> dict[str, str]:
    tok = base64.b64encode(f"{user}:{pw}".encode()).decode("ascii")
    return {"Authorization": f"Basic {tok}"}


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "Expected hidden _csrf input in rendered HTML"
    return m.group(1)


def _assert_strict_csp(resp, path: str) -> str:
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp, f"{path}: weak CSP: {csp!r}"
    assert "unsafe-eval" not in csp, f"{path}: unsafe-eval in CSP"
    html = resp.get_data(as_text=True)
    auditor = _StrictCspHtmlAudit()
    auditor.feed(html)
    assert auditor.inline_events == [], f"{path}: inline on* handlers found: {auditor.inline_events}"
    assert auditor.bad_scripts == [], f"{path}: inline scripts found: {auditor.bad_scripts}"
    return html


@pytest.fixture()
def seo_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jon-pw")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "osc-pw")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-seo-dashboard-secret-key")
    monkeypatch.setenv("AZOM_DISABLE_BACKGROUND_WORKERS", "1")
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    app = _load_dashboard_app()
    return (
        app.test_client(),
        _basic_auth("jonatan", "jon-pw"),
        _basic_auth("oscar", "osc-pw"),
    )


def test_compiled_css_csp_and_js_audit_on_all_seo_templates():
    """All classes in templates/seo/*.html and base.html exist in compiled dashboard.css and pass strict CSP/JS checks."""
    css = (DASH_DIR / "static" / "css" / "dashboard.css").read_text(encoding="utf-8")
    seo_templates = sorted((DASH_DIR / "templates" / "seo").glob("*.html"))
    assert len(seo_templates) == 6
    targets = seo_templates + [DASH_DIR / "templates" / "base.html"]

    class_attr_re = re.compile(r'(?<![:\w-])class="([^"]*)"')
    missing: list[tuple[str, str]] = []
    for tpl_path in targets:
        rel = tpl_path.relative_to(DASH_DIR / "templates").as_posix()
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
                esc = (
                    tok.replace(":", "\\:")
                    .replace("/", "\\/")
                    .replace(".", "\\.")
                    .replace("[", "\\[")
                    .replace("]", "\\]")
                )
                if esc not in css and f".{tok}" not in css:
                    missing.append((rel, tok))
    assert missing == [], f"Uncompiled CSS utility classes found: {missing}"

    js_path = DASH_DIR / "static" / "js" / "dashboard.js"
    js_text = js_path.read_text(encoding="utf-8")
    assert "function initSeoStudio()" in js_text
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


def test_seo_hub_nav_audit_and_fix_rbac_flow(seo_client):
    """Jonatan can view /seo, run audits, and propose fixes; ONLY Oscar can apply fixes; CSRF enforced."""
    client, jonatan, oscar = seo_client

    # Unauthenticated -> 401
    assert client.get("/seo").status_code == 401

    # Jonatan GET /seo
    hub = client.get("/seo", headers=jonatan)
    assert hub.status_code == 200
    html = _assert_strict_csp(hub, "/seo")
    assert "SEO Studio" in html
    assert 'href="/seo"' in html
    assert "Google Search Console" in html
    assert "/changes" in html
    csrf = _extract_csrf(html)

    # CSRF rejection on POST /seo/audit without _csrf
    bad_csrf = client.post("/seo/audit", headers=jonatan, data={"site_id": "mock-staging"})
    assert bad_csrf.status_code == 400

    # Jonatan runs read-only audit -> redirects cleanly without ?msg=/?err=
    audit_post = client.post(
        "/seo/audit",
        headers=jonatan,
        data={
            "_csrf": csrf,
            "site_id": "mock-staging",
            "market": "no",
            "scope": "site",
            "path": "/",
        },
        follow_redirects=False,
    )
    assert audit_post.status_code == 302
    loc = audit_post.headers["Location"]
    assert "/seo/audit/seoa-" in loc
    assert "?msg=" not in loc and "?err=" not in loc

    audit_id = loc.rsplit("/", 1)[-1]
    detail = client.get(loc, headers=jonatan)
    assert detail.status_code == 200
    detail_html = _assert_strict_csp(detail, loc)
    assert audit_id in detail_html
    assert "Teknisk SEO" in detail_html
    assert "Bild-" in detail_html
    assert "Kannibaliseringsmatris" in detail_html
    assert "Skapa granskningsbar SEO-fixplan" in detail_html

    # Invalid audit ID -> 404
    assert client.get("/seo/audit/not-an-id", headers=jonatan).status_code == 404

    # Jonatan proposes an SEO fix plan (seom-*)
    propose_resp = client.post(
        "/seo/fixes/propose",
        headers=jonatan,
        data={
            "_csrf": csrf,
            "site_id": "mock-staging",
            "fix_type": "seo_meta",
            "target_type": "product",
            "target_id": "501",
            "market": "no",
            "summary": "Förbättra meta-titel och beskrivning",
            "meta_title": "Trådlös CarPlay Volvo XC60 | Azom",
            "meta_description": "Snabb montering och OEM-passform för trådlös CarPlay i Volvo XC60.",
            "next_url": "/seo",
        },
        follow_redirects=False,
    )
    assert propose_resp.status_code == 302
    assert "?msg=" not in propose_resp.headers["Location"]

    # Verify plan appears on /seo
    hub_after = client.get("/seo", headers=oscar)
    assert hub_after.status_code == 200
    hub_after_html = _assert_strict_csp(hub_after, "/seo")
    m_plan = re.search(r"(seom-[a-f0-9]{12})", hub_after_html)
    assert m_plan, "Expected created seom-* plan on /seo"
    plan_id = m_plan.group(1)
    m_sha = re.search(r'name="expected_sha256"\s+value="([a-f0-9]{64})"', hub_after_html)
    assert m_sha, "Expected pinned expected_sha256 for Oscar apply form"
    plan_sha = m_sha.group(1)
    oscar_csrf = _extract_csrf(hub_after_html)

    # Jonatan CANNOT apply SEO fix plan -> 403
    jon_hub = client.get("/seo", headers=jonatan)
    jon_csrf = _extract_csrf(jon_hub.get_data(as_text=True))
    jon_apply = client.post(
        f"/seo/fixes/{plan_id}/apply",
        headers=jonatan,
        data={
            "_csrf": jon_csrf,
            "site_id": "mock-staging",
            "expected_sha256": plan_sha,
        },
    )
    assert jon_apply.status_code == 403

    # Oscar apply without CSRF -> 400
    assert (
        client.post(
            f"/seo/fixes/{plan_id}/apply",
            headers=oscar,
            data={"site_id": "mock-staging", "expected_sha256": plan_sha},
        ).status_code
        == 400
    )

    # Oscar applies fix plan with pinned SHA-256 -> succeeds and flashes Change Vault ID
    osc_apply = client.post(
        f"/seo/fixes/{plan_id}/apply",
        headers=oscar,
        data={
            "_csrf": oscar_csrf,
            "site_id": "mock-staging",
            "expected_sha256": plan_sha,
            "next_url": "/seo",
        },
        follow_redirects=True,
    )
    assert osc_apply.status_code == 200
    applied_html = osc_apply.get_data(as_text=True)
    assert "applicerad" in applied_html.lower()
    assert "applied" in applied_html.lower()


def test_seo_strategies_and_campaigns_ui(seo_client):
    """Jonatan can generate SEO strategies and campaigns and view structured clusters/checklists."""
    client, jonatan, _oscar = seo_client

    r_strat = client.get("/seo/strategies", headers=jonatan)
    assert r_strat.status_code == 200
    html = _assert_strict_csp(r_strat, "/seo/strategies")
    csrf = _extract_csrf(html)

    # Create strategy
    post_strat = client.post(
        "/seo/strategies",
        headers=jonatan,
        data={
            "_csrf": csrf,
            "site_id": "mock-staging",
            "market": "se",
            "title": "Nordisk CarPlay Tillväxtstrategi",
            "focus_topics": "CarPlay Volvo, Backkamera montering, Android Auto BMW",
            "goals": "Öka icke-varumärkestrafik 40%",
        },
        follow_redirects=True,
    )
    assert post_strat.status_code == 200
    strat_html = _assert_strict_csp(post_strat, "/seo/strategies")
    assert "Nordisk CarPlay Tillväxtstrategi" in strat_html
    m_sid = re.search(r"(seos-[a-f0-9]{12})", strat_html)
    assert m_sid
    strategy_id = m_sid.group(1)

    # Create campaign linked to strategy
    r_camp = client.get("/seo/campaigns", headers=jonatan)
    assert r_camp.status_code == 200
    camp_csrf = _extract_csrf(_assert_strict_csp(r_camp, "/seo/campaigns"))
    post_camp = client.post(
        "/seo/campaigns",
        headers=jonatan,
        data={
            "_csrf": camp_csrf,
            "site_id": "mock-staging",
            "market": "se",
            "name": "Volvo XC60 CarPlay Kampanj",
            "strategy_id": strategy_id,
            "campaign_type": "commercial",
            "target_keywords": "carplay volvo xc60, trådlös carplay adapter",
            "target_urls": "/produkt/carplay-volvo/, /kategori/carplay/",
            "objective": "Topp-3 placering på kommersiella sökord",
        },
        follow_redirects=True,
    )
    assert post_camp.status_code == 200
    camp_html = _assert_strict_csp(post_camp, "/seo/campaigns")
    assert "Volvo XC60 CarPlay Kampanj" in camp_html
    assert re.search(r"seoc-[a-f0-9]{12}", camp_html)


def test_seo_link_packages_guardrails_rbac_and_exports(seo_client):
    """Link packages enforce natural anchor guardrails, Oscar-only approval, and MD/JSON/CSV exports."""
    client, jonatan, oscar = seo_client

    lp_page = client.get("/seo/link-packages", headers=jonatan)
    assert lp_page.status_code == 200
    lp_html = _assert_strict_csp(lp_page, "/seo/link-packages")
    assert "Inga automatiska externa köp" in lp_html
    csrf = _extract_csrf(lp_html)

    # 1. Create a natural link package (auto balanced anchors)
    created = client.post(
        "/seo/link-packages",
        headers=jonatan,
        data={
            "_csrf": csrf,
            "site_id": "mock-staging",
            "market": "no",
            "name": "NO Bilteknik Länkpaket Q3",
            "budget_sek": "18000",
            "link_count": "6",
            "notes": "Endast norska redaktionella bilsajter.",
        },
        follow_redirects=False,
    )
    assert created.status_code == 302
    detail_url = created.headers["Location"]
    assert "/seo/link-packages/seol-" in detail_url
    assert "?msg=" not in detail_url and "?err=" not in detail_url
    pkg_id = detail_url.rsplit("/", 1)[-1]

    detail = client.get(detail_url, headers=jonatan)
    assert detail.status_code == 200
    detail_html = _assert_strict_csp(detail, detail_url)
    assert "NO Bilteknik Länkpaket Q3" in detail_html
    assert 'data-copy-target="#link-pack-brief-text"' in detail_html
    assert "inga externa köp" in detail_html.lower()

    # Jonatan moves draft -> ready_for_review (allowed via MARKETING_SUGGEST)
    r_ready = client.post(
        f"/seo/link-packages/{pkg_id}/status",
        headers=jonatan,
        data={"_csrf": csrf, "new_status": "ready_for_review"},
        follow_redirects=True,
    )
    assert r_ready.status_code == 200
    assert "ready_for_review" in r_ready.get_data(as_text=True)

    # Jonatan CANNOT approve_for_manual_order / ordered_external / delivered / cancelled -> 403
    for forbidden_status in (
        "approved_for_manual_order",
        "ordered_external",
        "delivered",
        "cancelled",
    ):
        resp_403 = client.post(
            f"/seo/link-packages/{pkg_id}/status",
            headers=jonatan,
            data={"_csrf": csrf, "new_status": forbidden_status},
        )
        assert resp_403.status_code == 403, f"Jonatan should be denied {forbidden_status}"

    # Oscar approves -> ordered_external -> delivered
    osc_detail = client.get(detail_url, headers=oscar)
    osc_csrf = _extract_csrf(_assert_strict_csp(osc_detail, detail_url))
    for step_status in ("approved_for_manual_order", "ordered_external", "delivered"):
        r_step = client.post(
            f"/seo/link-packages/{pkg_id}/status",
            headers=oscar,
            data={"_csrf": osc_csrf, "new_status": step_status},
            follow_redirects=True,
        )
        assert r_step.status_code == 200
        assert step_status in r_step.get_data(as_text=True)

    # Export Markdown, JSON, CSV
    exp_md = client.get(f"/seo/link-packages/{pkg_id}/export.md", headers=jonatan)
    assert exp_md.status_code == 200
    assert "attachment" in exp_md.headers.get("Content-Disposition", "")
    assert exp_md.headers.get("Cache-Control") == "no-store"
    assert "NO Bilteknik Länkpaket Q3" in exp_md.get_data(as_text=True)

    exp_json = client.get(f"/seo/link-packages/{pkg_id}/export.json", headers=jonatan)
    assert exp_json.status_code == 200
    parsed_json = json.loads(exp_json.get_data(as_text=True))
    assert parsed_json["package_id"] == pkg_id

    exp_csv = client.get(f"/seo/link-packages/{pkg_id}/export.csv", headers=jonatan)
    assert exp_csv.status_code == 200
    rows = list(csv.reader(io.StringIO(exp_csv.get_data(as_text=True))))
    assert len(rows) >= 2

    # Invalid export format -> 404
    assert client.get(f"/seo/link-packages/{pkg_id}/export.xml", headers=jonatan).status_code == 404

    # 2. Create an over-optimized exact-match link package -> guardrail blocks ready_for_review
    spam_targets = "\n".join(
        [
            "/produkt/carplay-volvo/ | carplay volvo | exact | carplay volvo",
            "/produkt/carplay-volvo/ | carplay volvo | exact | carplay volvo",
            "/produkt/carplay-volvo/ | carplay volvo | exact | carplay volvo",
            "/produkt/carplay-volvo/ | carplay volvo | exact | carplay volvo",
        ]
    )
    spam_created = client.post(
        "/seo/link-packages",
        headers=jonatan,
        data={
            "_csrf": csrf,
            "site_id": "mock-staging",
            "market": "no",
            "name": "Överoptimerat testpaket",
            "budget_sek": "12000",
            "link_count": "4",
            "targets_raw": spam_targets,
        },
        follow_redirects=False,
    )
    assert spam_created.status_code == 302
    spam_url = spam_created.headers["Location"]
    spam_id = spam_url.rsplit("/", 1)[-1]
    spam_detail = client.get(spam_url, headers=jonatan)
    assert spam_detail.status_code == 200
    spam_html = _assert_strict_csp(spam_detail, spam_url)
    assert "Blockerad" in spam_html

    # Submitting blocked package to ready_for_review must be blocked by service guardrail
    blocked_submit = client.post(
        f"/seo/link-packages/{spam_id}/status",
        headers=jonatan,
        data={"_csrf": csrf, "new_status": "ready_for_review"},
        follow_redirects=True,
    )
    assert blocked_submit.status_code == 200
    assert "approved_for_manual_order" not in blocked_submit.get_data(as_text=True) or "draft" in blocked_submit.get_data(as_text=True)


def test_cli_seo_subcommands_end_to_end(tmp_path, monkeypatch, capsys):
    """Test all CLI `seo` subcommands in mock mode including RBAC gates and link-pack export."""
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    from ecom_ops.cli import main

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "cli_data"))
    monkeypatch.delenv("AZOM_SEO_KILL", raising=False)
    monkeypatch.delenv("AZOM_WP_DEPLOY_KILL", raising=False)

    # 1. seo audit
    rc = main(["--mock", "--actor", "jonatan", "seo", "audit", "--site", "mock-staging", "--scope", "site", "--market", "no"])
    assert rc == 0
    audit_out = json.loads(capsys.readouterr().out)
    assert audit_out["ok"] is True
    assert audit_out["audit_id"].startswith("seoa-")

    # 2. seo fix-propose
    rc = main(
        [
            "--mock",
            "--actor",
            "jonatan",
            "seo",
            "fix-propose",
            "--site",
            "mock-staging",
            "--fix-type",
            "media_alt",
            "--target-type",
            "media",
            "--target-id",
            "101",
            "--json-fields",
            '{"alt_text": "Trådlös CarPlay-skärm monterad i Volvo XC60"}',
        ]
    )
    assert rc == 0
    prop_out = json.loads(capsys.readouterr().out)
    assert prop_out["ok"] is True
    plan = prop_out["plan"]
    assert plan["plan_id"].startswith("seom-")

    # 3. seo fix-apply: Jonatan denied, Oscar allowed
    rc_jon = main(
        [
            "--mock",
            "--actor",
            "jonatan",
            "seo",
            "fix-apply",
            "--site",
            "mock-staging",
            "--plan",
            plan["plan_id"],
            "--expected-sha256",
            plan["plan_sha256"],
        ]
    )
    assert rc_jon == 1
    jon_out = json.loads(capsys.readouterr().out)
    assert jon_out["ok"] is False

    rc_osc = main(
        [
            "--mock",
            "--actor",
            "oscar",
            "seo",
            "fix-apply",
            "--site",
            "mock-staging",
            "--plan",
            plan["plan_id"],
            "--expected-sha256",
            plan["plan_sha256"],
        ]
    )
    assert rc_osc == 0
    osc_out = json.loads(capsys.readouterr().out)
    assert osc_out["ok"] is True
    assert osc_out["executed"] is True

    # 4. seo strategy
    rc = main(
        [
            "--mock",
            "--actor",
            "jonatan",
            "seo",
            "strategy",
            "--site",
            "mock-staging",
            "--title",
            "CLI Strategi NO",
            "--market",
            "no",
            "--topics",
            "CarPlay,Backkamera",
            "--goals",
            "Öka organisk synlighet",
        ]
    )
    assert rc == 0
    strat_out = json.loads(capsys.readouterr().out)
    assert strat_out["ok"] is True
    assert strat_out["strategy"]["strategy_id"].startswith("seos-")

    # 5. seo campaign
    rc = main(
        [
            "--mock",
            "--actor",
            "jonatan",
            "seo",
            "campaign",
            "--site",
            "mock-staging",
            "--name",
            "CLI Kampanj NO",
            "--market",
            "no",
            "--keywords",
            "carplay volvo,carplay bmw",
            "--urls",
            "/produkt/carplay-volvo/,/produkt/carplay-bmw/",
        ]
    )
    assert rc == 0
    camp_out = json.loads(capsys.readouterr().out)
    assert camp_out["ok"] is True
    assert camp_out["campaign"]["campaign_id"].startswith("seoc-")

    # 6. seo link-pack with --export md
    rc = main(
        [
            "--mock",
            "--actor",
            "jonatan",
            "seo",
            "link-pack",
            "--site",
            "mock-staging",
            "--name",
            "CLI Länkpaket NO",
            "--market",
            "no",
            "--budget",
            "15000",
            "--links",
            "6",
            "--export",
            "md",
        ]
    )
    assert rc == 0
    lp_out = json.loads(capsys.readouterr().out)
    assert lp_out["ok"] is True
    pkg_id = lp_out["package"]["package_id"]
    assert pkg_id.startswith("seol-")
    assert lp_out["export"]["format"] == "md"
    assert "CLI Länkpaket NO" in lp_out["export"]["content"]

    # 7. seo link-pack-status: Jonatan -> ready_for_review (ok), Jonatan -> approved (denied), Oscar -> approved (ok)
    rc = main(["--mock", "--actor", "jonatan", "seo", "link-pack-status", "--id", pkg_id, "--status", "ready_for_review"])
    assert rc == 0
    st1 = json.loads(capsys.readouterr().out)
    assert st1["ok"] is True

    rc = main(["--mock", "--actor", "jonatan", "seo", "link-pack-status", "--id", pkg_id, "--status", "approved_for_manual_order"])
    assert rc == 1
    st2 = json.loads(capsys.readouterr().out)
    assert st2["ok"] is False

    rc = main(["--mock", "--actor", "oscar", "seo", "link-pack-status", "--id", pkg_id, "--status", "approved_for_manual_order"])
    assert rc == 0
    st3 = json.loads(capsys.readouterr().out)
    assert st3["ok"] is True


def test_seo_stage2_regressions_budget_audit_links_campaigns_targets_and_js(
    seo_client, tmp_path, monkeypatch, capsys
):
    """Regression suite for U1-02, U1-04, U1-05, U1-06, U1-07, U1-12, U1-13, U1-16, U1-17, U1-18."""
    client, jonatan, oscar = seo_client
    from ecom_ops.cli import main
    from ecom_ops.seo.service import SeoStudioService

    # U1-04, U1-13, U1-17: JS bfcache HITL fix, clipboard fallback/SVG safety, anchor-mix parity
    js_text = (DASH_DIR / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert "form.removeAttribute('data-az-confirmed');\n            HTMLFormElement.prototype.submit.call(form);" in js_text or (
        "form.removeAttribute('data-az-confirmed');" in js_text
        and "querySelectorAll('form[data-az-confirmed]')" in js_text
    )
    assert ".catch(fallbackCopy)" in js_text
    assert "btn.querySelector('svg')" in js_text
    assert "naturalPct < 60" in js_text
    assert "spamPbnRe" in js_text
    assert "inferAnchorType" in js_text

    # U1-13: link_packages.html threshold copy aligned to <= 10%
    lp_get = client.get("/seo/link-packages", headers=jonatan)
    assert lp_get.status_code == 200
    lp_html = lp_get.get_data(as_text=True)
    assert "exact-match &lt;= 10% (varning &gt; 10%, blockering &gt; 20%)" in lp_html
    assert "exact-match &lt;= 15%" not in lp_html
    csrf = _extract_csrf(lp_html)

    # U1-02: reject non-finite / out-of-bounds budget_sek & link_count in UI
    for bad_data in (
        {"name": "InfTest", "budget_sek": "inf", "link_count": "6"},
        {"name": "NanTest", "budget_sek": "nan", "link_count": "6"},
        {"name": "LowBudget", "budget_sek": "500", "link_count": "6"},
        {"name": "HighBudget", "budget_sek": "999999", "link_count": "6"},
        {"name": "ZeroLinks", "budget_sek": "15000", "link_count": "0"},
        {"name": "TooManyLinks", "budget_sek": "15000", "link_count": "51"},
    ):
        resp = client.post(
            "/seo/link-packages",
            headers=jonatan,
            data={"_csrf": csrf, "site_id": "mock-staging", "market": "no", **bad_data},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert bad_data["name"] not in resp.get_data(as_text=True)

    # U1-02 defensive template rendering: legacy non-finite float in SQLite never 500s list or detail
    svc = SeoStudioService(data_dir=tmp_path / "data")
    good_pkg = svc.create_link_package(
        site_id="mock-staging", actor="jonatan", name="DefensiveFloatPkg", market="no", budget_sek=15000.0, link_count=4
    )["package"]
    corrupted = dict(good_pkg)
    corrupted["budget_sek"] = float("inf")
    svc.store.save_link_package(corrupted)
    assert client.get("/seo/link-packages", headers=jonatan).status_code == 200
    assert client.get(f"/seo/link-packages/{good_pkg['package_id']}", headers=jonatan).status_code == 200

    # U1-06: draft must NOT show Oscar approve button; ready_for_review shows approve + return-to-draft
    pkg_id = good_pkg["package_id"]
    osc_draft_html = client.get(f"/seo/link-packages/{pkg_id}", headers=oscar).get_data(as_text=True)
    assert "Godkänn för manuell order (Oscar)" not in osc_draft_html
    assert "Skicka tillbaka till utkast" not in osc_draft_html
    osc_csrf = _extract_csrf(osc_draft_html)

    # Move draft -> ready_for_review, verify Oscar sees approve + return-to-draft, then return to draft
    client.post(
        f"/seo/link-packages/{pkg_id}/status",
        headers=jonatan,
        data={"_csrf": csrf, "new_status": "ready_for_review"},
        follow_redirects=True,
    )
    osc_review_html = client.get(f"/seo/link-packages/{pkg_id}", headers=oscar).get_data(as_text=True)
    assert "Godkänn för manuell order (Oscar)" in osc_review_html
    assert "Skicka tillbaka till utkast" in osc_review_html
    ret_resp = client.post(
        f"/seo/link-packages/{pkg_id}/status",
        headers=oscar,
        data={"_csrf": osc_csrf, "new_status": "draft"},
        follow_redirects=True,
    )
    assert ret_resp.status_code == 200
    assert svc.get_link_package(pkg_id)["status"] == "draft"

    # U1-06: guardrail_blocked disables submit-for-review button with inline reason
    blocked_pkg = svc.create_link_package(
        site_id="mock-staging",
        actor="jonatan",
        name="BlockedPkgUI",
        market="no",
        budget_sek=12000.0,
        link_count=4,
        targets=[
            {"target_url": "/produkt/x/", "keyword": "carplay volvo", "anchor_type": "exact_match", "anchor_text": "carplay volvo"}
            for _ in range(4)
        ],
    )["package"]
    blk_html = client.get(f"/seo/link-packages/{blocked_pkg['package_id']}", headers=jonatan).get_data(as_text=True)
    assert 'disabled aria-disabled="true"' in blk_html
    assert "Blockerad av anti-spam-guardrail" in blk_html

    # U1-05 & U1-12: audit_detail.html key alignments + category/site target_type + data-proposed-json
    synth_audit = {
        "audit_id": "seoa-112233445566",
        "site_id": "mock-staging",
        "market": "no",
        "scope": "site",
        "score": 52,
        "grade": "D",
        "created_at": "2026-11-27T10:00:00+00:00",
        "technical": {
            "seo_plugin": "yoast",
            "blog_public": True,
            "robots": {"blocks_entire_site": True, "blocks_products": False, "issues": ["Disallow: /"]},
            "sitemap": {"url_count": 12},
            "html": {
                "canonical": "https://staging.example.test/",
                "meta_robots": "index, follow",
                "hreflang": [
                    {"lang": "nb-NO", "href": "https://azom.no/"},
                    {"lang": "sv-SE", "href": "https://azom.se/"},
                    {"lang": "da-DK", "href": "https://azom.dk/"},
                ],
                "json_ld_types": ["Organization"],
            },
        },
        "on_page": {
            "entities_audited": 2,
            "thin_content_count": 1,
            "items": [
                {"object_type": "product", "object_id": 501, "title": "Prod A", "meta_title": "Title A", "meta_description": "", "focus_keyphrase": "", "word_count": 90},
                {"object_type": "page", "object_id": 10, "title": "Page B", "meta_title": "Title B", "meta_description": "Desc B", "focus_keyphrase": "carplay", "word_count": 400},
            ],
        },
        "media": {"total_media": 3, "missing_alt_count": 1, "items": []},
        "links": {"total_nodes": 7, "total_internal_links": 14, "orphan_pages": []},
        "cannibalization": [
            {"keyword": "carplay volvo", "competing_urls": ["/produkt/a/", "/guide/b/"], "severity": "high"}
        ],
        "issues": [
            {
                "code": "MISSING_META_DESC",
                "severity": "high",
                "category": "on_page",
                "title": "Saknar meta-beskrivning på kategori",
                "target_type": "category",
                "target_id": "42",
                "fix_type": "seo_meta",
                "proposed_fields": {"meta_description": "Föreslagen beskrivning för kategori."},
            }
        ],
    }
    svc.store.save_audit(synth_audit)
    aud_resp = client.get("/seo/audit/seoa-112233445566", headers=jonatan)
    assert aud_resp.status_code == 200
    aud_html = _assert_strict_csp(aud_resp, "/seo/audit/seoa-112233445566")
    assert "robots.txt Problem" in aud_html
    assert "robots.txt OK" not in aud_html
    assert "Hreflang-taggar: 3" in aud_html
    assert "Prod A" in aud_html
    assert "/produkt/a/" in aud_html and "/guide/b/" in aud_html
    assert "<dd class=\"mt-1 font-mono text-base font-semibold tabular-nums text-mist-100\">7</dd>" in aud_html
    assert '<option value="category">category</option>' in aud_html
    assert '<option value="site">site</option>' in aud_html
    assert "data-proposed-json=" in aud_html
    assert "Föreslagen beskrivning för kategori." in aud_html

    # U1-12: POST /seo/fixes/propose accepts category and site target_type
    prop_cat = client.post(
        "/seo/fixes/propose",
        headers=jonatan,
        data={
            "_csrf": csrf,
            "site_id": "mock-staging",
            "fix_type": "seo_meta",
            "target_type": "category",
            "target_id": "42",
            "meta_description": "Ny kategoribeskrivning för CarPlay.",
            "next_url": "/seo",
        },
        follow_redirects=True,
    )
    assert prop_cat.status_code == 200
    assert "Ogiltig target_type" not in prop_cat.get_data(as_text=True)

    # U1-07: strategy_campaign.html has all 8 canonical campaign types
    camp_html = client.get("/seo/campaigns", headers=jonatan).get_data(as_text=True)
    for ctype in (
        "commercial",
        "seasonal",
        "product_launch",
        "content_hub",
        "technical_sprint",
        "category",
        "informational",
        "technical",
    ):
        assert f'value="{ctype}"' in camp_html

    # U1-16: /seo?site=nonexistent does NOT emit broken /wp/nonexistent/changes link
    unreg_html = client.get("/seo?site=nonexistent", headers=jonatan).get_data(as_text=True)
    assert "/wp/nonexistent/changes" not in unreg_html
    reg_html = client.get("/seo?site=mock-staging", headers=jonatan).get_data(as_text=True)
    assert "/wp/mock-staging/changes" in reg_html

    # U1-02, U1-06, U1-07, U1-12, U1-18 CLI regressions
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    assert main(["--mock", "--actor", "jonatan", "seo", "link-pack", "--site", "mock-staging", "--name", "BadInf", "--budget", "inf"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert main(["--mock", "--actor", "jonatan", "seo", "link-pack", "--site", "mock-staging", "--name", "BadLinks", "--links", "0"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False

    # U1-18: --targets-json support on seo link-pack
    targets_payload = json.dumps(
        [{"target_url": "/", "keyword": "carplay", "anchor_type": "brand", "anchor_text": "Azom"}]
    )
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "link-pack",
                "--site",
                "mock-staging",
                "--name",
                "TargetsJsonCLI",
                "--targets-json",
                targets_payload,
            ]
        )
        == 0
    )
    cli_lp = json.loads(capsys.readouterr().out)
    assert cli_lp["ok"] is True
    cli_pkg_id = cli_lp["package"]["package_id"]

    # U1-06: CLI link-pack-status supports returning ready_for_review -> draft
    assert main(["--mock", "--actor", "jonatan", "seo", "link-pack-status", "--id", cli_pkg_id, "--status", "ready_for_review"]) == 0
    capsys.readouterr()
    assert main(["--mock", "--actor", "oscar", "seo", "link-pack-status", "--id", cli_pkg_id, "--status", "draft"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # U1-12: CLI seo fix-propose supports category target-type
    assert (
        main(
            [
                "--mock",
                "--actor",
                "jonatan",
                "seo",
                "fix-propose",
                "--site",
                "mock-staging",
                "--fix-type",
                "seo_meta",
                "--target-type",
                "category",
                "--target-id",
                "42",
                "--json-fields",
                '{"meta_title": "Kategori CarPlay | Azom"}',
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_seo_vendors_autopilot_and_vendor_selector_ui(seo_client, tmp_path):
    """Test /seo/vendors, /seo/autopilot, /seo/link-packages/<id>/vendor (draft vs approved), CSP/CSRF."""
    from ecom_ops.seo.service import SeoStudioService

    client, jonatan, oscar = seo_client
    svc = SeoStudioService(data_dir=tmp_path / "data")

    # 1. /seo hub shows Autopilot card & /seo/vendors nav pill
    hub = client.get("/seo?site=mock-staging", headers=jonatan)
    assert hub.status_code == 200
    hub_html = _assert_strict_csp(hub, "/seo")
    assert "Autonom SEO-pipeline" in hub_html
    assert 'href="/seo/vendors?site=mock-staging"' in hub_html
    csrf_j = _extract_csrf(hub_html)

    # 2. GET /seo/vendors (strict CSP, vetted shops, disclosure checklist, web citations)
    v_get = client.get("/seo/vendors?site=mock-staging&market=no&budget=15000&links=6", headers=jonatan)
    assert v_get.status_code == 200
    v_html = _assert_strict_csp(v_get, "/seo/vendors")
    assert "Kvalitetssäkrade SEO-butiker" in v_html
    assert "Bazoom" in v_html
    assert "WhitePress" in v_html
    assert "Annonse" in v_html
    assert "inga automatiska köp" in v_html.lower()
    assert 'data-seo-vendor-citations="true"' in v_html

    # POST /seo/vendors with low budget triggers white-hat floor budget warning (NO >= 1800 SEK/link)
    v_csrf = _extract_csrf(v_html)
    assert client.post("/seo/vendors", headers=jonatan, data={"market": "no"}).status_code == 400
    v_low = client.post(
        "/seo/vendors",
        headers=jonatan,
        data={
            "_csrf": v_csrf,
            "site_id": "mock-staging",
            "market": "no",
            "budget_sek": "5000",
            "link_count": "10",
            "goal": "organic_growth",
            "include_web_research_present": "1",
            "include_web_research": "1",
        },
    )
    assert v_low.status_code == 200
    v_low_html = _assert_strict_csp(v_low, "/seo/vendors")
    assert "Budgetvarning" in v_low_html

    # 3. POST /seo/autopilot enforces CSRF and strict budget/link bounds
    assert (
        client.post(
            "/seo/autopilot",
            headers=jonatan,
            data={"site_id": "mock-staging", "topic": "CarPlay Volvo"},
        ).status_code
        == 400
    )
    bad_auto = client.post(
        "/seo/autopilot",
        headers=jonatan,
        data={
            "_csrf": csrf_j,
            "site_id": "mock-staging",
            "topic": "Bad Budget",
            "market": "no",
            "budget_sek": "inf",
            "link_count": "6",
        },
        follow_redirects=False,
    )
    assert bad_auto.status_code == 302
    assert "/seo/link-packages/" not in bad_auto.headers["Location"]

    # Valid POST /seo/autopilot creates audit + strategy + campaign + link package + selected vendor
    auto_resp = client.post(
        "/seo/autopilot",
        headers=jonatan,
        data={
            "_csrf": csrf_j,
            "site_id": "mock-staging",
            "topic": "Trådlös CarPlay Volvo & BMW",
            "market": "no",
            "budget_sek": "18000",
            "link_count": "6",
            "include_web_research_present": "1",
            "include_web_research": "1",
            "include_batch_fixes": "1",
        },
        follow_redirects=False,
    )
    assert auto_resp.status_code == 302
    loc = auto_resp.headers["Location"]
    assert "/seo/link-packages/seol-" in loc
    assert "autopilot=1" in loc
    assert "?msg=" not in loc and "&msg=" not in loc
    pkg_id = loc.split("/seo/link-packages/", 1)[1].split("?", 1)[0]

    # Follow redirect -> detail page shows autopilot banner, flash summary, selected shop, top-3 table, draft selector
    det = client.get(loc, headers=jonatan)
    assert det.status_code == 200
    det_html = _assert_strict_csp(det, loc)
    assert "SEO Autopilot" in det_html
    assert "Skapad av Autonom SEO-pipeline" in det_html
    assert 'data-seo-vendor-selection="true"' in det_html
    assert "Spara vald SEO-butik" in det_html
    assert "Annonse" in det_html

    assert len(svc.list_strategies(site_id="mock-staging")) >= 1
    assert len(svc.list_campaigns(site_id="mock-staging")) >= 1
    pkg_before = svc.get_link_package(pkg_id)
    assert pkg_before is not None
    assert pkg_before["status"] == "draft"
    assert pkg_before.get("selected_vendor_id")

    # 4. POST /seo/link-packages/<id>/vendor: CSRF required; draft switch succeeds; unknown vendor refused
    assert (
        client.post(
            f"/seo/link-packages/{pkg_id}/vendor",
            headers=jonatan,
            data={"vendor_id": "whitepress"},
        ).status_code
        == 400
    )
    sw_ok = client.post(
        f"/seo/link-packages/{pkg_id}/vendor",
        headers=jonatan,
        data={"_csrf": csrf_j, "vendor_id": "whitepress"},
        follow_redirects=True,
    )
    assert sw_ok.status_code == 200
    sw_html = _assert_strict_csp(sw_ok, f"/seo/link-packages/{pkg_id}")
    assert "whitepress" in sw_html.lower()
    assert svc.get_link_package(pkg_id)["selected_vendor_id"] == "whitepress"

    # Unknown/unvetted vendor refused fail-closed
    sw_bad = client.post(
        f"/seo/link-packages/{pkg_id}/vendor",
        headers=jonatan,
        data={"_csrf": csrf_j, "vendor_id": "shady-pbn-shop"},
        follow_redirects=True,
    )
    assert sw_bad.status_code == 200
    assert svc.get_link_package(pkg_id)["selected_vendor_id"] == "whitepress"

    # Transition draft -> ready_for_review -> approved_for_manual_order (Oscar); vendor switch MUST be refused
    client.post(
        f"/seo/link-packages/{pkg_id}/status",
        headers=jonatan,
        data={"_csrf": csrf_j, "new_status": "ready_for_review"},
    )
    osc_det = client.get(f"/seo/link-packages/{pkg_id}", headers=oscar)
    csrf_o = _extract_csrf(_assert_strict_csp(osc_det, f"/seo/link-packages/{pkg_id}"))
    client.post(
        f"/seo/link-packages/{pkg_id}/status",
        headers=oscar,
        data={"_csrf": csrf_o, "new_status": "approved_for_manual_order"},
    )
    assert svc.get_link_package(pkg_id)["status"] == "approved_for_manual_order"

    sw_locked = client.post(
        f"/seo/link-packages/{pkg_id}/vendor",
        headers=oscar,
        data={"_csrf": csrf_o, "vendor_id": "bazoom"},
        follow_redirects=True,
    )
    assert sw_locked.status_code == 200
    assert svc.get_link_package(pkg_id)["selected_vendor_id"] == "whitepress"

    # 5. Direct POST /seo/link-packages with selected_vendor_id binds vendor and shows badge on list
    lp_create = client.post(
        "/seo/link-packages",
        headers=jonatan,
        data={
            "_csrf": csrf_j,
            "site_id": "mock-staging",
            "market": "se",
            "name": "SE Direktvald Butik Test",
            "budget_sek": "15000",
            "link_count": "5",
            "selected_vendor_id": "whitepress",
        },
        follow_redirects=False,
    )
    assert lp_create.status_code == 302
    new_pkg_id = lp_create.headers["Location"].rsplit("/", 1)[-1]
    assert svc.get_link_package(new_pkg_id)["selected_vendor_id"] == "whitepress"
    list_html = _assert_strict_csp(
        client.get("/seo/link-packages?site=mock-staging", headers=jonatan),
        "/seo/link-packages",
    )
    assert "Butik: whitepress" in list_html
