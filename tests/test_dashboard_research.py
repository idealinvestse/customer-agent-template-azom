"""Verification tests for Wave 6 Dashboard Research Hub, contextual cards, CSP, CSRF, RBAC, XSS, and export."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
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
    """Enforce zero live TCP/network sockets across all dashboard research tests."""

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
        "azom_dashboard_research_test",
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
        "azom_dashboard_research_test", DASH_DIR / "app.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_research_test"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    mod.app.config["TESTING"] = True
    return mod.app


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
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-research-dashboard-secret")
    monkeypatch.chdir(DASH_DIR)

    app = _load_dashboard_app()
    client = app.test_client()
    return {"app": app, "client": client, "data_dir": data_dir, "cfg_dir": cfg_dir}


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


def _extract_csrf_from_html(html: str) -> str:
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "hidden _csrf input missing from HTML form"
    return m.group(1)


def _create_stored_job_with_brief(
    data_dir: Path,
    *,
    actor: str,
    topic: str = "Test research topic",
    target_ref: str | None = None,
    visibility: str = "private",
    summary: str = "Sammanfattning utan farlig kod.",
    sources_payload: list[dict] | None = None,
):
    from ecom_ops.research.models import (
        Citation,
        EvaluatedSource,
        EvidenceStatus,
        ResearchBrief,
        ResearchSpec,
    )
    from ecom_ops.research.store import ResearchStore

    store = ResearchStore(
        path=data_dir / "research_jobs.sqlite3",
        artifacts_dir=data_dir / "research_artifacts",
    )
    spec = ResearchSpec(
        topic=topic,
        profile_id="deep_general",
        depth="standard",
        target_ref=target_ref,
        locale="sv",
        country="SE",
    )
    job = store.enqueue_job(spec, actor=actor, target_ref=target_ref, visibility=visibility)
    raw_sources = sources_payload or [
        {
            "id": "S1",
            "title": "Official Docs",
            "url": "https://developer.wordpress.org/block-editor/",
            "domain": "developer.wordpress.org",
            "snippet": "Clean snippet text.",
            "score": 0.91,
            "provenance": "mock_fixture",
            "source_type": "official_docs",
        }
    ]
    eval_sources = [EvaluatedSource.from_dict(s) for s in raw_sources]
    citations = [
        Citation(
            id=s.source_id or f"S{i}",
            title=s.title,
            url=s.url,
            domain=s.domain,
            source_type=s.source_type,
        )
        for i, s in enumerate(eval_sources, start=1)
    ]
    brief = ResearchBrief(
        research_id=job.research_id,
        topic=topic,
        profile_id="deep_general",
        evidence_status=EvidenceStatus.MOCK,
        summary=summary,
        findings=[{"claim": "Verifierat fynd", "claim_kind": "fact", "citations": ["S1"]}],
        recommendations=["Följ officiella riktlinjer"],
        gaps=[],
        conflicts=[],
        citations=citations,
        markdown=f"# {topic}\n\n{summary}\n",
        provenance="mock_fixture",
    )
    store.write_artifacts(job, brief=brief, sources=eval_sources)
    return store, job


def test_create_app_cold_import_leaks_zero_research_threads(tmp_path, monkeypatch):
    """#17.7: Cold import of infrastructure.dashboard.app must not fail or spawn worker threads."""
    env = os.environ.copy()
    env["AZOM_USE_MOCK"] = "1"
    env["AZOM_DATA_DIR"] = str(tmp_path / "cold-data")
    env["AZOM_CONFIG_DIR"] = str(ROOT / "config")
    env["DASHBOARD_SECRET_KEY"] = "cold-import-test-secret"
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "skills"), str(DASH_DIR)])

    script = (
        "import threading\n"
        "before = {t.name for t in threading.enumerate()}\n"
        "import infrastructure.dashboard.app as dash_mod\n"
        "app2 = dash_mod.create_app()\n"
        "after = {t.name for t in threading.enumerate()}\n"
        "new_threads = sorted(after - before)\n"
        "assert not any('research' in n.lower() for n in new_threads), new_threads\n"
        "assert app2 is not None\n"
        "print('COLD_OK', len(new_threads))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "COLD_OK 0" in proc.stdout


def test_post_research_jobs_never_starts_worker_in_testing_or_mock(dash_env, monkeypatch):
    """Even when AZOM_USE_MOCK=0, TESTING=True or AZOM_DISABLE_BACKGROUND_WORKERS=1 prevents thread spawn."""
    import ecom_ops.research.worker as worker_mod

    called: list[object] = []
    monkeypatch.setattr(
        worker_mod,
        "ensure_research_worker_started",
        lambda **kw: called.append(kw),
    )

    client = dash_env["client"]
    hdrs = _auth_csrf(client, "jonatan")
    resp = client.post(
        "/research/jobs",
        headers=hdrs,
        data={"_csrf": hdrs["X-CSRF-Token"], "topic": "PostNord driftstörning"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert called == []

    # Flip TESTING=False + AZOM_USE_MOCK=0 + AZOM_DISABLE_BACKGROUND_WORKERS=1 -> still no worker
    dash_env["app"].config["TESTING"] = False
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jonatan")
    monkeypatch.setenv("AZOM_DISABLE_BACKGROUND_WORKERS", "1")
    resp2 = client.post(
        "/research/jobs",
        headers=hdrs,
        data={"_csrf": hdrs["X-CSRF-Token"], "topic": "PostNord driftstörning 2"},
        follow_redirects=False,
    )
    assert resp2.status_code == 302
    assert called == []


def test_strict_csp_and_static_template_security_scan(dash_env):
    """Strict CSP header on all research/contextual routes and zero inline scripts/on*/|safe in templates."""
    templates_to_scan = [
        DASH_DIR / "templates" / "research" / "hub.html",
        DASH_DIR / "templates" / "research" / "detail.html",
        DASH_DIR / "templates" / "base.html",
        DASH_DIR / "templates" / "case_detail.html",
        DASH_DIR / "templates" / "marketing.html",
        DASH_DIR / "templates" / "wp_site.html",
        DASH_DIR / "templates" / "wp_review.html",
        DASH_DIR / "templates" / "oscar_runtime.html",
    ]
    for tpl in templates_to_scan:
        assert tpl.is_file(), f"missing template {tpl}"
        text = tpl.read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe\b", text), f"forbidden '| safe' found in {tpl.name}"
        assert not re.search(r"\bon[a-z]+\s*=", text, re.IGNORECASE), (
            f"inline on* handler found in {tpl.name}"
        )
        # No <script> tags without src= unless explicitly type="application/json"
        for m in re.finditer(r"<script\b([^>]*)>", text, re.IGNORECASE):
            attrs = m.group(1)
            if 'type="application/json"' in attrs or "type='application/json'" in attrs:
                continue
            assert "src=" in attrs, f"inline executable <script> tag found in {tpl.name}: {m.group(0)}"

    js_text = (DASH_DIR / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert "Alpine.data('researchPolling'" in js_text
    assert "innerHTML" not in js_text
    assert "new Function" not in js_text

    # Runtime CSP + HTML parse audit across rendered pages
    client = dash_env["client"]
    oscar = _auth_csrf(client, "oscar")
    _, job = _create_stored_job_with_brief(dash_env["data_dir"], actor="oscar")
    for path in ("/research", f"/research/{job.research_id}", "/ops/runtime", "/oscar/runtime"):
        resp = client.get(path, headers=oscar)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";", 1)[0]
        assert "'unsafe-eval'" not in csp
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        audit = _StrictCspHtmlAudit()
        audit.feed(resp.get_data(as_text=True))
        assert not audit.bad_scripts, f"{path}: {audit.bad_scripts}"
        assert not audit.inline_events, f"{path}: {audit.inline_events}"


def test_csrf_and_auth_enforced_on_all_research_endpoints(dash_env):
    """Unauthenticated gets 401; POST endpoints without valid CSRF token return 400."""
    client = dash_env["client"]
    _, job = _create_stored_job_with_brief(dash_env["data_dir"], actor="oscar")
    rid = job.research_id

    for path in ("/research", f"/research/{rid}", f"/research/{rid}/status", f"/research/{rid}/export"):
        assert client.get(path).status_code == 401

    oscar_basic = _basic("oscar")
    for post_path, payload in (
        ("/research/search", {"query": "PostNord"}),
        ("/research/jobs", {"topic": "PostNord"}),
        (f"/research/{rid}/steer", {"note": "Fokusera på SE"}),
        (f"/research/{rid}/cancel", {}),
        ("/oscar/runtime/search-breaker-reset", {}),
    ):
        assert client.post(post_path, data=payload).status_code == 401
        missing_csrf = client.post(post_path, headers=oscar_basic, data=payload)
        assert missing_csrf.status_code == 400, f"{post_path} accepted missing CSRF!"
        bad_csrf = client.post(
            post_path,
            headers={**oscar_basic, "X-CSRF-Token": "wrong-csrf-token"},
            data={**payload, "_csrf": "wrong-csrf-token"},
        )
        assert bad_csrf.status_code == 400, f"{post_path} accepted bad CSRF!"


def test_rbac_jonatan_vs_oscar_and_wp_ceiling(dash_env):
    """#17.1: Jonatan has non-WP research access, zero WP research access, zero Oscar breaker reset."""
    client = dash_env["client"]
    data_dir = dash_env["data_dir"]
    jonatan = _auth_csrf(client, "jonatan")
    oscar = _auth_csrf(client, "oscar")

    # Jonatan can open /research and run mock quick search
    hub = client.get("/research", headers=jonatan)
    assert hub.status_code == 200
    hub_html = hub.get_data(as_text=True)
    assert 'data-research-hub="true"' in hub_html
    assert 'href="/research"' in hub_html

    search_resp = client.post(
        "/research/search",
        headers=jonatan,
        data={"_csrf": _extract_csrf_from_html(hub_html), "query": "PostNord driftstörning", "profile_id": "support_fact_check"},
    )
    assert search_resp.status_code == 200
    assert 'data-quick-search-results="true"' in search_resp.get_data(as_text=True)

    # Jonatan can start, view, status-poll, steer, export, and cancel his own non-WP job
    create_resp = client.post(
        "/research/jobs",
        headers=jonatan,
        data={
            "_csrf": jonatan["X-CSRF-Token"],
            "topic": "Konsumentköplagen garanti bilelektronik",
            "profile_id": "support_fact_check",
            "depth": "quick",
            "market": "SE",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 302
    loc = create_resp.headers["Location"]
    m = re.search(r"/research/(res-[0-9a-f]{12})", loc)
    assert m, f"expected res-* redirect, got {loc}"
    jon_rid = m.group(1)

    assert client.get(f"/research/{jon_rid}", headers=jonatan).status_code == 200
    st = client.get(f"/research/{jon_rid}/status", headers=jonatan)
    assert st.status_code == 200
    st_json = st.get_json()
    assert st_json["ok"] is True
    assert st_json["id"] == jon_rid
    assert "rounds_completed" in st_json
    assert "events" in st_json

    steer_resp = client.post(
        f"/research/{jon_rid}/steer",
        headers=jonatan,
        data={"_csrf": jonatan["X-CSRF-Token"], "note": "Fokusera på Konsumentverket"},
        follow_redirects=False,
    )
    assert steer_resp.status_code == 302
    detail_after_steer = client.get(f"/research/{jon_rid}", headers=jonatan).get_data(as_text=True)
    assert "Fokusera på Konsumentverket" in detail_after_steer

    assert client.get(f"/research/{jon_rid}/export?format=md", headers=jonatan).status_code == 200
    cancel_resp = client.post(
        f"/research/{jon_rid}/cancel",
        headers=jonatan,
        data={"_csrf": jonatan["X-CSRF-Token"]},
        follow_redirects=False,
    )
    assert cancel_resp.status_code == 302
    assert client.get(f"/research/{jon_rid}/status", headers=jonatan).get_json()["status"] == "cancelled"

    # Jonatan CANNOT access Oscar breaker reset or create/view/list/export any wp:* / wp_site:* job
    assert (
        client.post(
            "/oscar/runtime/search-breaker-reset",
            headers=jonatan,
            data={"_csrf": jonatan["X-CSRF-Token"]},
        ).status_code
        == 403
    )
    for wp_ref in ("wp_site:mock-staging", "wp:mock-staging:wr-0123456789ab"):
        deny_create = client.post(
            "/research/jobs",
            headers=jonatan,
            data={
                "_csrf": jonatan["X-CSRF-Token"],
                "topic": "WP thank-you layout",
                "target_ref": wp_ref,
            },
        )
        assert deny_create.status_code == 403

    # Create a WP research job as Oscar and verify Jonatan is completely blocked & cannot see it
    _, wp_job = _create_stored_job_with_brief(
        data_dir,
        actor="oscar",
        topic="Secret WP Checkout Research",
        target_ref="wp_site:mock-staging",
        visibility="private",
    )
    wp_rid = wp_job.research_id
    jon_hub = client.get("/research", headers=jonatan).get_data(as_text=True)
    assert wp_rid not in jon_hub
    assert "Secret WP Checkout Research" not in jon_hub

    assert client.get(f"/research/{wp_rid}", headers=jonatan).status_code in (403, 404)
    assert client.get(f"/research/{wp_rid}/status", headers=jonatan).status_code in (403, 404)
    assert client.get(f"/research/{wp_rid}/export?format=json", headers=jonatan).status_code in (403, 404)
    assert (
        client.post(
            f"/research/{wp_rid}/steer",
            headers=jonatan,
            data={"_csrf": jonatan["X-CSRF-Token"], "note": "hack"},
        ).status_code
        in (403, 404)
    )
    assert (
        client.post(
            f"/research/{wp_rid}/cancel",
            headers=jonatan,
            data={"_csrf": jonatan["X-CSRF-Token"]},
        ).status_code
        in (403, 404)
    )

    # Oscar has full access to WP research job & breaker reset
    oscar_hub = client.get("/research", headers=oscar).get_data(as_text=True)
    assert wp_rid in oscar_hub
    assert client.get(f"/research/{wp_rid}", headers=oscar).status_code == 200
    assert client.get(f"/research/{wp_rid}/export?format=json", headers=oscar).status_code == 200


def test_xss_escaping_and_safe_public_https_url_enforcement(dash_env, monkeypatch):
    """#17.3: Malicious scripts are HTML-escaped and unsafe URLs never render as <a href=...>."""
    from ecom_ops.research import ResearchService

    client = dash_env["client"]
    data_dir = dash_env["data_dir"]
    oscar = _auth_csrf(client, "oscar")

    xss_title = '<script>alert(1)</script><img src=x onerror=alert(1)>'
    xss_snippet = 'Snippet <script>alert(1)</script> end'
    unsafe_urls = [
        "http://example.com/insecure",
        "https://127.0.0.1:8080/internal",
        "https://169.254.169.254/latest/meta-data",
        "javascript:alert(1)",
    ]
    safe_url = "https://developer.wordpress.org/themes/basics/"

    # 1. Quick search XSS + URL safety
    def _fake_quick_search(self, query, **_kwargs):
        return {
            "query": query,
            "profile_id": "deep_general",
            "evidence_status": "mock",
            "provenance": "mock_fixture",
            "provider": "mock",
            "cached": False,
            "conflicts": [],
            "sources": [
                {
                    "id": f"S{i}",
                    "title": xss_title,
                    "url": u,
                    "domain": "bad.test",
                    "snippet": xss_snippet,
                    "score": 0.5,
                    "source_type": "unknown",
                    "provenance": "mock_fixture",
                }
                for i, u in enumerate([*unsafe_urls, safe_url], start=1)
            ],
        }

    monkeypatch.setattr(ResearchService, "quick_search", _fake_quick_search)
    resp = client.post(
        "/research/search",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"], "query": "xss url test"},
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=alert(1)>" not in html

    audit = _StrictCspHtmlAudit()
    audit.feed(html)
    assert not audit.bad_scripts
    assert not audit.inline_events
    for bad in unsafe_urls:
        assert not any(bad in h for h in audit.hrefs), f"unsafe href leaked: {bad}"
    assert safe_url in audit.hrefs

    # 2. Stored brief & sources XSS + URL safety on /research/<id>
    stored_sources = [
        {
            "id": f"S{i}",
            "title": xss_title,
            "url": u,
            "domain": "bad.test",
            "snippet": xss_snippet,
            "score": 0.8,
            "source_type": "unknown",
            "provenance": "mock_fixture",
        }
        for i, u in enumerate([*unsafe_urls, safe_url], start=1)
    ]
    _, job = _create_stored_job_with_brief(
        data_dir,
        actor="oscar",
        topic="XSS <script>alert(1)</script> Topic",
        summary="Summary <script>alert(1)</script>",
        sources_payload=stored_sources,
    )
    detail_resp = client.get(f"/research/{job.research_id}", headers=oscar)
    assert detail_resp.status_code == 200
    detail_html = detail_resp.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in detail_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in detail_html
    assert "<img src=x onerror=alert(1)>" not in detail_html

    detail_audit = _StrictCspHtmlAudit()
    detail_audit.feed(detail_html)
    assert not detail_audit.bad_scripts
    assert not detail_audit.inline_events
    for bad in unsafe_urls:
        assert not any(bad in h for h in detail_audit.hrefs), f"unsafe href leaked in detail: {bad}"
    assert safe_url in detail_audit.hrefs


def test_export_contract_md_json_and_400_on_html(dash_env):
    """#17.3: export supports md and json with attachment + nosniff, and rejects html with 400."""
    client = dash_env["client"]
    oscar = _auth_csrf(client, "oscar")
    _, job = _create_stored_job_with_brief(dash_env["data_dir"], actor="oscar", topic="Export check")
    rid = job.research_id

    md_resp = client.get(f"/research/{rid}/export?format=md", headers=oscar)
    assert md_resp.status_code == 200
    assert md_resp.headers.get("Content-Type", "").startswith("text/markdown")
    assert f'attachment; filename="research-{rid}.md"' in md_resp.headers.get("Content-Disposition", "")
    assert md_resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert b"Export check" in md_resp.data

    json_resp = client.get(f"/research/{rid}/export?format=json", headers=oscar)
    assert json_resp.status_code == 200
    assert json_resp.headers.get("Content-Type", "").startswith("application/json")
    assert f'attachment; filename="research-{rid}.json"' in json_resp.headers.get(
        "Content-Disposition", ""
    )
    assert json_resp.headers.get("X-Content-Type-Options") == "nosniff"
    parsed = json.loads(json_resp.get_data(as_text=True))
    assert parsed["research_id"] == rid
    assert parsed["brief"]["topic"] == "Export check"

    html_resp = client.get(f"/research/{rid}/export?format=html", headers=oscar)
    assert html_resp.status_code == 400
    assert html_resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert not html_resp.headers.get("Content-Type", "").startswith("text/html")


def test_contextual_cards_and_oscar_breaker_reset(dash_env):
    """Contextual cards render cleanly on cases, marketing, wp_site, wp_review, and oscar_runtime."""
    from ecom_ops.cases.store import CaseStore
    from ecom_ops.integrations.search.circuit_breaker import (
        current_search_epoch,
        record_failure,
        status_snapshot,
    )
    from ecom_ops.integrations.search.errors import SearchUnavailableError
    from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme

    client = dash_env["client"]
    data_dir = dash_env["data_dir"]
    jonatan = _auth_csrf(client, "jonatan")
    oscar = _auth_csrf(client, "oscar")

    # 1. Case detail contextual card (zero PII in prefilled query)
    case_store = CaseStore(path=data_dir / "cases.db")
    case = case_store.create_case(
        mailbox_id="support_default",
        subject="Paket saknas för order 98765",
        from_addr="hemlig.kund@example.com",
        body="Hej, jag heter Anna Andersson och min order 98765 har fastnat hos PostNord.",
        category="shipping",
        draft_reply="Vi kollar på det.",
        order_id="98765",
        message_id="<ctx-research-case@azom>",
        site="azom",
    )
    _, case_job = _create_stored_job_with_brief(
        data_dir,
        actor="jonatan",
        topic="PostNord driftstörning paket",
        target_ref=f"case:{case.id}",
        visibility="team",
    )
    case_resp = client.get(f"/cases/{case.id}", headers=jonatan)
    assert case_resp.status_code == 200
    case_html = case_resp.get_data(as_text=True)
    assert 'data-case-research="true"' in case_html
    assert "Faktakoll / Research" in case_html
    assert case_job.research_id in case_html
    # Extract prefilled input value and ensure zero PII leaked into the research query field
    q_match = re.search(r'name="topic"[^>]*value="([^"]*)"', case_html)
    assert q_match is not None
    prefill = q_match.group(1)
    assert "hemlig.kund@example.com" not in prefill
    assert "Anna" not in prefill
    assert "98765" not in prefill

    # 2. Marketing contextual card
    _, mkt_job = _create_stored_job_with_brief(
        data_dir,
        actor="jonatan",
        topic="Google Ads Consent Mode v2",
        target_ref="marketing:general",
        visibility="team",
    )
    mkt_resp = client.get("/marketing", headers=jonatan)
    assert mkt_resp.status_code == 200
    mkt_html = mkt_resp.get_data(as_text=True)
    assert 'data-marketing-research="true"' in mkt_html
    assert mkt_job.research_id in mkt_html

    # 3. WP site & WP review contextual cards (Oscar visible, Jonatan hidden)
    plans = ReviewPlans(data_dir=data_dir)
    opened = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert opened.ok
    plan_id = opened.data["plan_id"]
    _, wp_site_job = _create_stored_job_with_brief(
        data_dir,
        actor="oscar",
        topic="WP staging UX patterns",
        target_ref="wp_site:mock-staging",
    )
    _, wp_plan_job = _create_stored_job_with_brief(
        data_dir,
        actor="oscar",
        topic="Thank you page hierarchy",
        target_ref=f"wp:mock-staging:{plan_id}",
    )

    jon_wp = client.get("/wp/mock-staging", headers=jonatan)
    assert jon_wp.status_code == 200
    jon_wp_html = jon_wp.get_data(as_text=True)
    assert 'data-wp-research="true"' not in jon_wp_html
    assert wp_site_job.research_id not in jon_wp_html

    osc_wp = client.get("/wp/mock-staging", headers=oscar)
    assert osc_wp.status_code == 200
    osc_wp_html = osc_wp.get_data(as_text=True)
    assert 'data-wp-research="true"' in osc_wp_html
    assert wp_site_job.research_id in osc_wp_html

    osc_rev = client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=oscar)
    assert osc_rev.status_code == 200
    osc_rev_html = osc_rev.get_data(as_text=True)
    assert 'data-wp-research="true"' in osc_rev_html
    assert wp_plan_job.research_id in osc_rev_html

    # 4. Oscar runtime search-router card & breaker reset
    for _ in range(3):
        record_failure("brave", SearchUnavailableError("upstream 503"))
    assert status_snapshot().get("route:brave", {}).get("state") == "open"
    epoch_before = current_search_epoch()

    rt_resp = client.get("/oscar/runtime", headers=oscar)
    assert rt_resp.status_code == 200
    rt_html = rt_resp.get_data(as_text=True)
    assert 'data-runtime-card="search-router"' in rt_html
    assert "Sök-router &amp; kretsbrytare" in rt_html
    assert "route:brave" in rt_html

    reset_resp = client.post(
        "/oscar/runtime/search-breaker-reset",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"]},
        follow_redirects=False,
    )
    assert reset_resp.status_code == 302
    loc = reset_resp.headers["Location"]
    assert "/ops/runtime" in loc or "/oscar/runtime" in loc
    assert current_search_epoch() > epoch_before
    assert status_snapshot() == {}
