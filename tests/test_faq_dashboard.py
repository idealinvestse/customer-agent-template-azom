"""Dashboard FAQ browse + Oscar HITL routes."""

from __future__ import annotations

import base64
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    name = "azom_dashboard_faq"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret-faq")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def _auth(user: str = "jonatan", password: str | None = None) -> dict[str, str]:
    pw = password or user
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _csrf(html: str) -> str:
    m = re.search(r'name="_csrf"\s+value="([^"]+)"', html)
    assert m, "csrf token missing"
    return m.group(1)


def test_faq_browse_requires_auth(dash_client):
    assert dash_client.get("/faq").status_code == 401


def test_faq_browse_jonatan_readonly(dash_client):
    resp = dash_client.get("/faq?market=se", headers=_auth())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "read-only" in body
    assert "se-shipping-tracking" in body
    assert "Sync draft till WP" not in body


def test_faq_browse_search_hits(dash_client):
    resp = dash_client.get(
        "/faq?market=se&q=sp%C3%A5rning", headers=_auth()
    )
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Sökresultat" in body
    assert "se-shipping" in body or "Spår" in body or "spår" in body.lower()


def test_oscar_faq_forbidden_for_jonatan(dash_client):
    resp = dash_client.get("/oscar/faq", headers=_auth("jonatan", "jonatan"))
    assert resp.status_code == 403


def test_oscar_faq_renders_staging_and_hitl(dash_client):
    resp = dash_client.get("/oscar/faq?market=se", headers=_auth("oscar", "oscar"))
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Ingest / staging" in body
    assert "faq promote" in body
    assert "aldrig silent WP-publish" in body
    assert "Sync draft till WP" in body
    assert "Publish" in body


def test_oscar_faq_publish_requires_csrf(dash_client):
    resp = dash_client.post(
        "/oscar/faq/publish",
        headers=_auth("oscar", "oscar"),
        data={"market": "se", "status": "publish"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_oscar_faq_sync_draft_with_csrf(dash_client):
    page = dash_client.get("/oscar/faq", headers=_auth("oscar", "oscar"))
    assert page.status_code == 200
    html = page.data.decode("utf-8", errors="replace")
    csrf = _csrf(html)
    resp = dash_client.post(
        "/oscar/faq/sync-draft",
        headers=_auth("oscar", "oscar"),
        data={"_csrf": csrf, "market": "se"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers.get("Location", "")
    assert "oscar/faq" in loc
    assert "err=" not in loc


def test_oscar_faq_reload_with_csrf(dash_client):
    page = dash_client.get("/oscar/faq", headers=_auth("oscar", "oscar"))
    html = page.data.decode("utf-8", errors="replace")
    csrf = _csrf(html)
    resp = dash_client.post(
        "/oscar/faq/reload",
        headers=_auth("oscar", "oscar"),
        data={"_csrf": csrf, "market": "se"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers.get("Location", "")
    assert "Korpus" in loc or "oscar/faq" in loc


def test_faq_article_detail_jonatan(dash_client):
    resp = dash_client.get("/faq/se-shipping-tracking", headers=_auth())
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "Spåra" in body or "spår" in body.lower()
    assert "se-shipping-tracking" in body


def test_faq_browse_category_filter_and_score(dash_client):
    resp = dash_client.get(
        "/faq?market=se&category=shipping&q=sp%C3%A5rning", headers=_auth()
    )
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")
    assert "score" in body
    assert 'name="category"' in body


def test_oscar_faq_promote_dry_run_csrf(dash_client):
    import os

    import yaml

    from ecom_ops.faq.staging import articles_staging_dir

    staging = articles_staging_dir("se")
    article = {
        "id": "se-dash-promote",
        "market": "se",
        "language": "sv",
        "category": "product",
        "title": "Dash promote",
        "body": "En tillräckligt lång brödtext för att passera validering av FAQ-artikel.",
        "customer_safe": False,
        "needs_review": True,
    }
    (staging / "se-dash-promote.yaml").write_text(
        yaml.safe_dump([article], allow_unicode=True),
        encoding="utf-8",
    )
    assert os.environ.get("AZOM_DATA_DIR")
    page = dash_client.get("/oscar/faq", headers=_auth("oscar", "oscar"))
    assert page.status_code == 200
    html = page.data.decode("utf-8", errors="replace")
    assert "se-dash-promote" in html or "Staging-utkast" in html
    csrf = _csrf(html)
    resp = dash_client.post(
        "/oscar/faq/promote",
        headers=_auth("oscar", "oscar"),
        data={"_csrf": csrf, "market": "se", "article_id": "se-dash-promote"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    loc = resp.headers.get("Location", "")
    assert "err=" not in loc
    page = dash_client.get("/faq", headers=_auth("jonatan", "jonatan"))
    assert page.status_code == 200
    with dash_client.session_transaction() as sess:
        csrf = sess["csrf_token"]
    resp = dash_client.post(
        "/oscar/faq/publish",
        headers=_auth("jonatan", "jonatan"),
        data={"_csrf": csrf, "market": "se", "status": "publish"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
