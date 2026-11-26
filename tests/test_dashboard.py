"""Dashboard routes: Basic Auth, onboarding, Gmail OAuth."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    """Load Flask app from infrastructure/dashboard/app.py."""
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def _auth_headers(user="jonatan", password="jonatan"):
    import base64

    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_health_unauthenticated(dash_client):
    resp = dash_client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_live_session_cookie_secure_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "live-oscar-secret")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "live-jonatan-secret")
    monkeypatch.delenv("DASHBOARD_COOKIE_SECURE", raising=False)
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    assert app.config["SESSION_COOKIE_SECURE"] is True
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "0")
    app = _load_dashboard_app()
    assert app.config["SESSION_COOKIE_SECURE"] is False


def test_index_requires_auth(dash_client):
    assert dash_client.get("/").status_code == 401


def test_index_with_auth(dash_client):
    resp = dash_client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    assert b"Azom Agent Dashboard" in resp.data


def test_onboarding_page(dash_client):
    resp = dash_client.get("/onboarding", headers=_auth_headers())
    assert resp.status_code == 200
    assert b"Onboarding wizard" in resp.data
    assert b"Secrets checklist" in resp.data


def test_onboarding_status_json(dash_client):
    resp = dash_client.get("/onboarding/status", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert "runtime" in data
    assert "secrets" in data
    assert "health" in data


def test_gmail_oauth_mock_connect(dash_client, tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    resp = dash_client.get("/oauth/gmail/start", headers=_auth_headers())
    assert resp.status_code == 302
    assert "onboarding" in resp.headers.get("Location", "")

    from ecom_ops.oauth.gmail import GmailOAuthStore

    store = GmailOAuthStore()
    assert store.has_tokens()

    status = dash_client.get("/oauth/gmail/status", headers=_auth_headers())
    assert status.status_code == 200
    assert status.get_json()["connected"] is True


def test_index_partial_readiness_shows_runbook(dash_client):
    from ecom_ops.ops_status import write_last_case_poll

    write_last_case_poll(ok=True, errors=1, created=0, extra={"partial": True})
    resp = dash_client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "PARTIAL" in body
    assert "mail-poll-stuck" in body


def test_index_presence_not_live_probe_storm(dash_client):
    """Home uses presence/runtime chrome; does not require live probe labels."""
    resp = dash_client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Integrationer" in body or "integration" in body.lower()
    # Presence summary / counts chrome (not Oscar live probe table)
    assert "Öppna" in body or "open_cases" in body or "Ärenden" in body


def test_gmail_status_authenticated(dash_client):
    resp = dash_client.get("/oauth/gmail/status", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert "connected" in data


def test_onboarding_refresh_markup(dash_client):
    resp = dash_client.get("/onboarding", headers=_auth_headers())
    assert resp.status_code == 200
    assert b"/onboarding/status" in resp.data
    assert b"Uppdatera status" in resp.data
    assert b"x-for=\"s in secrets\"" in resp.data or b"x-for='s in secrets'" in resp.data
    assert b"x-for=\"c in checks\"" in resp.data or b"x-for='c in checks'" in resp.data


def test_case_detail_shows_draft_diff(dash_client, tmp_path):
    """After regenerate, case_detail shows Föregående/Nytt when draft_before_regen set."""
    import os
    from pathlib import Path

    from ecom_ops.cases.store import CaseStore

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    store = CaseStore(path=data_dir / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order 1001?",
        category="order_status",
        draft_reply="NYTT UTKAST TEXT",
        order_id="1001",
        message_id="<dash-draft-diff@azom>",
        site="azom",
    )
    with store._conn() as conn:
        conn.execute(
            """
            UPDATE cases SET draft_before_regen = ?, draft_regenerated_at = ?
            WHERE id = ?
            """,
            ("GAMMALT UTKAST TEXT", "2026-07-29T12:00:00Z", case.id),
        )
    resp = dash_client.get(f"/cases/{case.id}", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Föregående utkast" in body
    assert "Nytt utkast" in body
    assert "GAMMALT UTKAST TEXT" in body
    assert "NYTT UTKAST TEXT" in body


def test_marketing_page_renders_digest(dash_client):
    resp = dash_client.get("/marketing", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Marketing ledger" in body
    assert "Ads kostnad" in body or "digest" in body.lower()


def test_google_marketing_oauth_start_oscar_only(dash_client):
    assert (
        dash_client.get(
            "/oauth/google/start", headers=_auth_headers("jonatan", "jonatan")
        ).status_code
        == 403
    )
    resp = dash_client.get(
        "/oauth/google/start", headers=_auth_headers("oscar", "oscar")
    )
    assert resp.status_code == 302
    assert "marketing" in resp.headers.get("Location", "")


def test_case_detail_shadow_deny_badge(dash_client):
    import os
    from pathlib import Path

    from ecom_ops.cases.store import CaseStore

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    store = CaseStore(path=data_dir / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Saknar order",
        from_addr="a@b.co",
        body="hej",
        category="order_status",
        draft_reply="Skicka ordernummer",
        order_id=None,
        message_id="<dash-shadow-deny@azom>",
        site="azom",
    )
    store.set_shadow_decision(
        case.id, eligible=False, deny_reason="missing_order_id"
    )
    resp = dash_client.get(f"/cases/{case.id}", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Skugga: nej (saknar order_id)" in body
    assert "Skicka nu?" not in body or "Godkänn" in body  # approve UI still separate


def test_case_detail_shadow_eligible_badge(dash_client):
    import os
    from pathlib import Path

    from ecom_ops.cases.store import CaseStore

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    store = CaseStore(path=data_dir / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order 1001",
        category="order_status",
        draft_reply="Skickad",
        order_id="1001",
        message_id="<dash-shadow-ok@azom>",
        site="azom",
    )
    store.set_shadow_decision(case.id, eligible=True, deny_reason=None)
    resp = dash_client.get(f"/cases/{case.id}", headers=_auth_headers())
    body = resp.data.decode("utf-8", errors="replace")
    assert "Skugga: skulle skickats" in body


def test_case_detail_no_shadow_badge_when_unevaluated(dash_client):
    import os
    from pathlib import Path

    from ecom_ops.cases.store import CaseStore

    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    store = CaseStore(path=data_dir / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Ingen skugga",
        from_addr="a@b.co",
        body="hej",
        category="other",
        draft_reply="ok",
        order_id=None,
        message_id="<dash-shadow-none@azom>",
        site="azom",
    )
    resp = dash_client.get(f"/cases/{case.id}", headers=_auth_headers())
    body = resp.data.decode("utf-8", errors="replace")
    assert "Skugga:" not in body


def _plain_oauth_error(dash_client, path: str) -> None:
    resp = dash_client.get(
        path,
        query_string={"error": "<script>azom-oauth-reflect</script>"},
    )
    assert resp.status_code == 400
    assert resp.mimetype == "text/plain"
    assert b"azom-oauth-reflect" not in resp.data
    assert b"<script>" not in resp.data


def test_oauth_callbacks_do_not_reflect_errors(dash_client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("DASHBOARD_OSCAR_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_OSCAR_PASSWORD_HASH", raising=False)
    _plain_oauth_error(dash_client, "/oauth/google/callback")
    _plain_oauth_error(dash_client, "/oauth/gmail/callback")

    marker = "token-secret-leak-marker"

    def boom(*_args, **_kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr("ecom_ops.oauth.google_marketing.exchange_code", boom)
    monkeypatch.setattr("ecom_ops.oauth.gmail.GmailOAuthStore.exchange_code", boom)
    from ecom_ops.oauth.gmail import GmailOAuthStore
    from ecom_ops.oauth.google_marketing import GoogleMarketingOAuthStore

    google_state = GoogleMarketingOAuthStore().create_state()
    gmail_state = GmailOAuthStore().create_state()
    for path, state in (
        ("/oauth/google/callback", google_state),
        ("/oauth/gmail/callback", gmail_state),
    ):
        resp = dash_client.get(path, query_string={"code": "abc", "state": state})
        assert resp.status_code == 502
        assert resp.mimetype == "text/plain"
        assert marker.encode() not in resp.data
        assert b"Token exchange failed" in resp.data


def test_approve_under_null_send_does_not_flash_skickat(dash_client, monkeypatch):
    import os
    from pathlib import Path

    from ecom_ops.cases.store import CaseStore

    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    data_dir = Path(os.environ["AZOM_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    store = CaseStore(path=data_dir / "cases.db")
    case = store.create_case(
        mailbox_id="support_default",
        subject="Order 1001",
        from_addr="a@b.co",
        body="order",
        category="order_status",
        draft_reply="Hej kund",
        order_id="1001",
        message_id="<dash-null-approve@azom>",
        site="azom",
    )
    # CSRF: grab token from detail page
    detail = dash_client.get(f"/cases/{case.id}", headers=_auth_headers())
    assert detail.status_code == 200
    html = detail.data.decode("utf-8", errors="replace")
    import re

    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "csrf token missing"
    resp = dash_client.post(
        f"/cases/{case.id}",
        headers=_auth_headers(),
        data={"_csrf": m.group(1), "action": "reply", "body": "Hej kund"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers.get("Location", "")
    assert "Skickat" not in loc
    assert "Null-send" in loc or "err=" in loc


def test_null_send_banner_when_active(dash_client, monkeypatch):
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    resp = dash_client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Null-send aktiv" in body
    assert "null-send" in body


def test_null_send_banner_absent_by_default(dash_client, monkeypatch):
    monkeypatch.delenv("AZOM_NULL_SEND", raising=False)
    resp = dash_client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Null-send aktiv" not in body


def test_marketing_suggests_build_and_deny(dash_client):
    import re

    page = dash_client.get("/marketing", headers=_auth_headers())
    assert page.status_code == 200
    html = page.data.decode("utf-8", errors="replace")
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "csrf token missing"
    csrf = m.group(1)

    build = dash_client.post(
        "/marketing/suggests/build",
        headers=_auth_headers(),
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert build.status_code in (302, 303)

    after = dash_client.get("/marketing", headers=_auth_headers())
    assert after.status_code == 200
    body = after.data.decode("utf-8", errors="replace")
    deny = re.search(
        r'action="/marketing/suggests/([^"]+)/deny"', body
    )
    if deny is None:
        # Mock may yield empty waste → no open suggests; still proved POST accepted.
        return
    csrf2 = re.search(r'name="_csrf"\s+value="([^"]+)"', body)
    assert csrf2
    resp = dash_client.post(
        f"/marketing/suggests/{deny.group(1)}/deny",
        headers=_auth_headers(),
        data={"_csrf": csrf2.group(1)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def test_metrics_probe_last_ts_is_valid_prometheus_numeric(tmp_path, monkeypatch):
    import json
    import re

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "scrape-tok-123")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()

    (data_dir / "probe_last.json").write_text(
        json.dumps({"checked_at": "2026-11-25T20:00:00Z", "ok": True}),
        encoding="utf-8",
    )
    resp = client.get("/metrics", headers={"Authorization": "Bearer scrape-tok-123"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "# TYPE azom_probe_last_ts gauge" in body
    m = re.search(r"^azom_probe_last_ts\s+(.+)$", body, re.MULTILINE)
    assert m is not None
    raw_val = m.group(1).strip()
    assert "'" not in raw_val and '"' not in raw_val
    parsed = float(raw_val)
    assert parsed > 1_700_000_000

    # Invalid/missing timestamp falls back cleanly to numeric 0
    (data_dir / "probe_last.json").write_text(
        json.dumps({"checked_at": "not-a-timestamp"}),
        encoding="utf-8",
    )
    resp_bad = client.get("/metrics", headers={"Authorization": "Bearer scrape-tok-123"})
    assert resp_bad.status_code == 200
    assert "azom_probe_last_ts 0\n" in resp_bad.get_data(as_text=True)


def test_tail_jsonl_chunked_reverse_seek_and_utf8_safety(tmp_path):
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    import json

    from support import _tail_jsonl

    log = tmp_path / "telemetry.jsonl"
    rows = [
        json.dumps({"i": i, "text": f"ÅÄÖ-rad-{i}-🚀"}, ensure_ascii=False)
        for i in range(40)
    ]
    rows.append("not-json-line-åäö")
    rows.append("")
    rows.append(json.dumps({"i": 999, "text": "sista-raden-ö"}, ensure_ascii=False))
    log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    # Tiny chunk_size forces multi-byte UTF-8 characters and JSON lines to split across chunk boundaries
    tailed = _tail_jsonl(log, limit=5, chunk_size=17)
    assert len(tailed) == 5
    assert tailed[0] == {"i": 999, "text": "sista-raden-ö"}
    assert tailed[1] == {"raw": "not-json-line-åäö"}
    assert tailed[2] == {"i": 39, "text": "ÅÄÖ-rad-39-🚀"}
    assert tailed[3] == {"i": 38, "text": "ÅÄÖ-rad-38-🚀"}
    assert tailed[4] == {"i": 37, "text": "ÅÄÖ-rad-37-🚀"}
    assert _tail_jsonl(log, limit=0) == []

    # max_bytes cap bounds total bytes scanned on huge files
    capped = _tail_jsonl(log, limit=100, chunk_size=32, max_bytes=128)
    assert 1 <= len(capped) < 10
    assert capped[0] == {"i": 999, "text": "sista-raden-ö"}


def test_resolve_dashboard_threads_clamping_and_default(monkeypatch):
    _load_dashboard_app()
    import azom_dashboard

    monkeypatch.delenv("DASHBOARD_THREADS", raising=False)
    assert azom_dashboard._resolve_dashboard_threads() == 12
    assert azom_dashboard._resolve_dashboard_threads("") == 12
    assert azom_dashboard._resolve_dashboard_threads("not-a-number") == 12
    assert azom_dashboard._resolve_dashboard_threads("1") == 4
    assert azom_dashboard._resolve_dashboard_threads("4") == 4
    assert azom_dashboard._resolve_dashboard_threads("16") == 16
    assert azom_dashboard._resolve_dashboard_threads("999") == 32


def test_messenger_webhook_reuses_shared_conversation_store(tmp_path):
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    import webhooks

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    s1 = webhooks._get_messenger_store(dir_a)
    s2 = webhooks._get_messenger_store(dir_a)
    s3 = webhooks._get_messenger_store(dir_b)
    assert s1 is s2
    assert s1 is not s3


def test_index_reuses_single_telemetry_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()

    import ecom_ops.budget as budget_mod
    import ecom_ops.kpis as kpis_mod

    seen: list[object] = []
    orig_budget = budget_mod.budget_status
    orig_kpis = kpis_mod.support_kpis_last_days

    def _spy_budget(*args, **kwargs):
        seen.append(kwargs.get("telemetry"))
        return orig_budget(*args, **kwargs)

    def _spy_kpis(*args, **kwargs):
        seen.append(kwargs.get("telemetry"))
        return orig_kpis(*args, **kwargs)

    monkeypatch.setattr(budget_mod, "budget_status", _spy_budget)
    monkeypatch.setattr(kpis_mod, "support_kpis_last_days", _spy_kpis)

    resp = client.get("/", headers=_auth_headers())
    assert resp.status_code == 200
    assert len(seen) == 2
    assert seen[0] is not None
    assert seen[0] is seen[1]
