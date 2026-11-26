"""Dashboard status for the mock WordPress registry."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"
SECRET = "dashboard-shop-password-ccc"


def _load_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    for name in ("azom_dashboard", "wp_routes", "settings_store", "auth", "support"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _auth(client, user: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{user}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    client.get("/wp", headers=headers)
    with client.session_transaction() as sess:
        csrf = sess.get("csrf_token")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def test_dashboard_shows_unknown_backup_and_blocks_viewer_check(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", SECRET)
    for name in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(name, raising=False)
    from ecom_ops.rbac import clear_rbac_cache

    clear_rbac_cache()
    app = _load_app()
    app.config["TESTING"] = True
    client = app.test_client()

    jonatan = _auth(client, "jonatan")
    page = client.get("/wp", headers=jonatan)
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "mock-shop" in body
    assert 'data-healthy="false"' in body
    assert 'data-tone="attention"' in body
    assert "okänd" in body
    assert SECRET not in body
    assert "Webbplatser" in body

    detail = client.get("/wp/mock-shop", headers=jonatan)
    assert detail.status_code == 200
    assert "Kör läskontroll" not in detail.get_data(as_text=True)
    assert client.post("/wp/mock-shop/check", headers=jonatan).status_code == 403
    assert client.get("/wp/mock-shop/restore", headers=jonatan).status_code == 404

    oscar = _auth(client, "oscar")
    oscar_detail = client.get("/wp/mock-shop", headers=oscar)
    assert "Kör läskontroll" in oscar_detail.get_data(as_text=True)
    posted = client.post("/wp/mock-shop/check", headers=oscar, data={"_csrf": oscar["X-CSRF-Token"]})
    assert posted.status_code == 302
    followed = client.get("/wp/mock-shop", headers=oscar)
    followed_body = followed.get_data(as_text=True)
    assert "http_uptime" in followed_body
    assert 'data-tone="attention"' in followed_body
    assert SECRET not in followed_body
    assert 'data-live-calls="false"' in followed_body


def test_dashboard_mock_backup_is_not_green_and_has_no_production_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", SECRET)
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "dashboard-staging-password-ddd")
    for name in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(name, raising=False)
    from ecom_ops.rbac import clear_rbac_cache

    clear_rbac_cache()
    app = _load_app()
    app.config["TESTING"] = True
    client = app.test_client()
    jonatan = _auth(client, "jonatan")
    assert client.post("/wp/mock-shop/backup", headers=jonatan).status_code == 403
    assert client.post("/wp/mock-shop/restore-drill", headers=jonatan).status_code == 403
    staging = client.get("/wp/mock-staging", headers=jonatan)
    assert "Skapa mock-backup" not in staging.get_data(as_text=True)
    assert "data-drill-target" not in staging.get_data(as_text=True)

    oscar = _auth(client, "oscar")
    before = client.get("/wp/mock-shop", headers=oscar).get_data(as_text=True)
    assert "Skapa mock-backup" in before
    assert 'data-drill-target="mock-staging"' in before
    assert 'data-drill-target="mock-shop"' not in before
    assert "Återställ till produktion" not in before
    posted = client.post(
        "/wp/mock-shop/backup",
        headers=oscar,
        data={"_csrf": oscar["X-CSRF-Token"]},
    )
    assert posted.status_code == 302
    page = client.get("/wp/mock-shop", headers=oscar)
    body = page.get_data(as_text=True)
    assert "verifierad, inte off-site" in body
    assert 'data-backup="verified"' in body
    assert SECRET not in body
    assert "dashboard-staging-password-ddd" not in body
    overview = client.get("/wp", headers=oscar).get_data(as_text=True)
    assert "verifierad, inte off-site" in overview
    assert 'data-healthy="false"' in overview
    backup_id = body.split('data-backup-id="', 1)[1].split('"', 1)[0]
    drilled = client.post(
        "/wp/mock-shop/restore-drill",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "backup_id": backup_id,
            "target_site_id": "mock-staging",
        },
    )
    assert drilled.status_code == 302
    after = client.get("/wp/mock-shop", headers=oscar).get_data(as_text=True)
    assert 'data-drill="staging"' in after
    assert 'data-orders-mutated="false"' in after
    assert SECRET not in after
    assert client.get("/wp/mock-shop/restore", headers=oscar).status_code == 404


def test_dashboard_file_edit_is_typed_and_not_a_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", SECRET)
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "mock-staging-app-password-bbb")
    for name in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(name, raising=False)
    from ecom_ops.rbac import clear_rbac_cache

    clear_rbac_cache()
    app = _load_app()
    app.config["TESTING"] = True
    client = app.test_client()
    jonatan = _auth(client, "jonatan")
    site = client.get("/wp/mock-staging", headers=jonatan)
    assert site.status_code == 200
    assert "Tema- och pluginfiler" in site.get_data(as_text=True)
    assert not (tmp_path / "wp_files_mock").exists()
    secret_file = (
        tmp_path
        / "wp_files_mock"
        / "mock-shop"
        / "wp-content"
        / "themes"
        / "twentytwentyfive"
        / "style.css"
    )
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text(f"/* {SECRET} */\n", encoding="utf-8")
    hidden = client.get(
        "/wp/mock-shop/files?kind=theme&slug=twentytwentyfive&path=style.css",
        headers=jonatan,
    )
    hidden_body = hidden.get_data(as_text=True)
    assert hidden.status_code == 403
    assert SECRET not in hidden_body
    assert 'data-file-action="propose"' not in hidden_body
    assert 'name="command"' not in hidden_body
    assert client.post(
        "/wp/mock-staging/files/propose",
        headers=jonatan,
        data={"kind": "theme", "slug": "twentytwentyfive", "path": "style.css", "content": "x"},
    ).status_code == 403
    assert client.post(
        "/wp/mock-staging/files/apply",
        headers=jonatan,
        data={"plan_id": "", "content": "x"},
    ).status_code == 403

    oscar = _auth(client, "oscar")
    oscar_prod_file = client.get(
        "/wp/mock-shop/files?kind=theme&slug=twentytwentyfive&path=style.css",
        headers=oscar,
    )
    oscar_prod_body = oscar_prod_file.get_data(as_text=True)
    assert oscar_prod_file.status_code == 200
    assert SECRET not in oscar_prod_body
    assert "***" in oscar_prod_body
    opened = client.get(
        "/wp/mock-staging/files?kind=theme&slug=twentytwentyfive&path=style.css",
        headers=oscar,
    )
    opened_body = opened.get_data(as_text=True)
    assert 'data-file-action="propose"' in opened_body
    assert 'name="command"' not in opened_body
    assert 'data-ssh="false"' in opened_body
    assert 'data-live-calls="false"' in opened_body
    target = (
        tmp_path
        / "wp_files_mock"
        / "mock-staging"
        / "wp-content"
        / "themes"
        / "twentytwentyfive"
        / "style.css"
    )
    before = target.read_text(encoding="utf-8")
    new_text = before + "\nAZOM-FILE-MARKER\n"
    proposed = client.post(
        "/wp/mock-staging/files/propose",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "kind": "theme",
            "slug": "twentytwentyfive",
            "path": "style.css",
            "content": new_text,
        },
    )
    proposed_body = proposed.get_data(as_text=True)
    assert proposed.status_code == 200
    assert 'bg-ink-700 text-mist-200" data-edit="proposed"' in proposed_body
    assert target.read_text(encoding="utf-8") == before
    plan_id = proposed_body.split('name="plan_id" value="', 1)[1].split('"', 1)[0]
    applied = client.post(
        "/wp/mock-staging/files/apply",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "kind": "theme",
            "slug": "twentytwentyfive",
            "path": "style.css",
            "plan_id": plan_id,
            "content": new_text,
        },
    )
    assert applied.status_code == 302
    followed = client.get(applied.headers["Location"], headers=oscar)
    followed_body = followed.get_data(as_text=True)
    assert 'data-edit="applied"' in followed_body
    assert "AZOM-FILE-MARKER" in target.read_text(encoding="utf-8")
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "AZOM-FILE-MARKER" not in audit
    assert SECRET not in audit
    assert SECRET not in followed_body


def test_dashboard_ssh_settings_hide_the_key_and_block_viewers(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.delenv("SSH_IDENTITY_FILE", raising=False)
    for name in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(name, raising=False)
    from ecom_ops.rbac import clear_rbac_cache

    clear_rbac_cache()
    registry = (ROOT / "config" / "wp_sites.yaml").read_text(encoding="utf-8")
    app = _load_app()
    app.config["TESTING"] = True
    client = app.test_client()
    marker = "AZOM-DASH-KEY-MARKER"
    key = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{marker}\n-----END OPENSSH PRIVATE KEY-----\n"
    identity = "C:/keys/azom-dash-test"
    host_blob = base64.b64encode(b"azom-dash-host").decode("ascii")
    host_key = f"ssh-ed25519 {host_blob} dash"
    jonatan = _auth(client, "jonatan")
    viewer = client.get("/wp/mock-staging/ssh", headers=jonatan)
    viewer_body = viewer.get_data(as_text=True)
    assert viewer.status_code == 200
    assert "SSH-inställningar" in viewer_body
    assert "<form" not in viewer_body
    assert 'name="ssh_private_key"' not in viewer_body
    assert 'name="command"' not in viewer_body
    blocked = client.post(
        "/wp/mock-staging/ssh",
        headers=jonatan,
        data={"_csrf": jonatan["X-CSRF-Token"], "files_root": "/var/www/custom", "ssh_private_key": key},
    )
    assert blocked.status_code == 403
    oscar = _auth(client, "oscar")
    opened = client.get("/wp/mock-staging", headers=oscar)
    assert "SSH-inställningar" in opened.get_data(as_text=True)
    form = client.get("/wp/mock-staging/ssh", headers=oscar)
    form_body = form.get_data(as_text=True)
    assert 'name="files_root"' in form_body
    assert 'name="ssh_private_key"' in form_body
    assert 'name="host_public_key"' in form_body
    assert 'name="command"' not in form_body
    assert 'name="AZOM_WP_FILES_SSH"' not in form_body
    assert 'data-ssh="false"' in form_body
    assert 'data-live-calls="false"' in form_body
    assert "lösenordsinloggning" in form_body
    assert "lösenfras" in form_body
    assert "måste sluta på -wp-reader" in form_body
    posted = client.post(
        "/wp/mock-staging/ssh",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "files_root": "/var/www/from-dashboard",
            "ssh_host": "mock-staging.example.test",
            "ssh_user": "azom-wp-reader",
            "ssh_port": "2222",
            "ssh_identity_file": identity,
            "host_public_key": host_key,
            "ssh_private_key": key,
            "global_ssh_user": "",
            "global_ssh_port": "",
            "global_ssh_identity_file": "",
        },
    )
    assert posted.status_code == 302
    assert marker not in posted.headers["Location"]
    assert identity not in posted.headers["Location"]
    followed = client.get(posted.headers["Location"], headers=oscar)
    page = followed.get_data(as_text=True)
    assert followed.status_code == 200
    assert "/var/www/from-dashboard" in page
    assert "nyckel sparad" in page
    assert "SHA256:" in page
    assert host_blob in page
    assert marker not in page
    assert "PRIVATE KEY" not in page
    saved_key = tmp_path / "ssh-keys" / "mock-staging"
    assert marker in saved_key.read_text(encoding="utf-8")
    overlay = (tmp_path / "wp_ssh.json").read_text(encoding="utf-8")
    assert marker not in overlay
    assert "PRIVATE KEY" not in overlay
    assert '"ssh_host"' not in overlay
    assert host_blob in overlay
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert marker not in audit
    assert identity not in audit
    assert host_blob not in audit
    assert (ROOT / "config" / "wp_sites.yaml").read_text(encoding="utf-8") == registry
    hidden = client.get("/wp/mock-staging/ssh", headers=jonatan)
    hidden_body = hidden.get_data(as_text=True)
    assert "<form" not in hidden_body
    assert marker not in hidden_body
    assert identity not in hidden_body
    assert host_blob not in hidden_body
    assert "SHA256:" not in hidden_body
    assert "satt" in hidden_body
    assert "/var/www/from-dashboard" in hidden_body
    assert os.environ["AZOM_WP_FILES_SSH"] == "1"
    runtime = tmp_path / "runtime.env"
    if runtime.is_file():
        assert "AZOM_WP_FILES_SSH" not in runtime.read_text(encoding="utf-8")
        assert marker not in runtime.read_text(encoding="utf-8")


def _make_png(width: int = 4, height: int = 4) -> bytes:
    raw = b"".join(b"\x00" + b"\x22\x88\x44" * width for _ in range(height))

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _setup_dashboard_env(tmp_path: Path, monkeypatch) -> object:
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("WP_SECRET_MOCK_SHOP", SECRET)
    monkeypatch.setenv("WP_SECRET_MOCK_STAGING", "mock-staging-app-password-bbb")
    for name in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_PASSWORD_HASH",
        "DASHBOARD_OSCAR_PASSWORD",
        "DASHBOARD_OSCAR_PASSWORD_HASH",
    ):
        monkeypatch.delenv(name, raising=False)
    from ecom_ops.rbac import clear_rbac_cache

    clear_rbac_cache()
    app = _load_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_wp_review_live_overlay_gallery_renders_without_browser_smoke(tmp_path, monkeypatch):
    from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme
    from ecom_ops.wp_ops.review_workspace import workspace_root

    client = _setup_dashboard_env(tmp_path, monkeypatch)
    plans = ReviewPlans(data_dir=tmp_path)
    opened = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="Kompakt köpknapp med tydlig ikonmarginal på produktsidan.",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert opened.ok
    plan_id = opened.data["plan_id"]
    adir = workspace_root(tmp_path, "mock-staging", plan_id) / "artifacts"
    adir.mkdir(parents=True, exist_ok=True)
    png = _make_png()
    for name in (
        "focus-desktop.png",
        "focus-mobile.png",
        "live-desktop.png",
        "preview-desktop.png",
        "live-mobile.png",
        "preview-mobile.png",
    ):
        (adir / name).write_bytes(png)
    (adir / "preview.json").write_text(
        json.dumps(
            {
                "preview_kind": "live_overlay",
                "path": "/produkt/azom-dnx-8/",
                "focus_target": "selector:form.w-commerce-commerceaddtocartform",
                "focus_matched": True,
            }
        ),
        encoding="utf-8",
    )
    (adir / "preview.html").write_text("<!doctype html><html><body>Sandbox</body></html>", encoding="utf-8")

    oscar = _auth(client, "oscar")
    resp = client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=oscar)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Live-overlay gallery renders even when browser_smoke has not passed
    assert 'data-rendered-preview="true"' in body
    assert 'data-live-overlay-preview="true"' in body
    assert 'data-sandbox-smoke="false"' in body
    assert "Ingen renderad förhandsvisning." not in body
    assert "Syntetiska skärmdumpar" not in body
    assert 'data-live-overlay-gallery="true"' in body
    assert "Live vs. Förhandsvisning" in body
    assert "Fokus på ändringen" in body
    assert "Live (före)" in body
    assert "Förhandsvisning (efter)" in body
    assert "/produkt/azom-dnx-8/" in body
    assert "selector:form.w-commerce-commerceaddtocartform" in body
    for name in (
        "focus-desktop.png",
        "focus-mobile.png",
        "live-desktop.png",
        "preview-desktop.png",
        "live-mobile.png",
        "preview-mobile.png",
    ):
        assert f"/wp/mock-staging/reviews/{plan_id}/artifact/{name}" in body
    assert f"/wp/mock-staging/reviews/{plan_id}/preview" in body
    assert "Kompakt köpknapp med tydlig ikonmarginal på produktsidan." in body

    # Viewer does not see code summary or admin-only artifact images
    jonatan = _auth(client, "jonatan")
    viewer_resp = client.get(f"/wp/mock-staging/reviews/{plan_id}", headers=jonatan)
    assert viewer_resp.status_code == 200
    viewer_body = viewer_resp.get_data(as_text=True)
    assert "Kompakt köpknapp med tydlig ikonmarginal" not in viewer_body
    assert 'data-live-overlay-gallery="true"' not in viewer_body


def test_wp_review_variant_switcher_forked_from_superseded_and_site_list(tmp_path, monkeypatch):
    from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme
    from ecom_ops.wp_ops.review_workspace import workspace_root

    client = _setup_dashboard_env(tmp_path, monkeypatch)
    plans = ReviewPlans(data_dir=tmp_path)
    p1 = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="Förslag 1: kompakt grön knapp",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    p2 = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="Förslag 2: grön konturknapp",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    p3 = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="Äldre fristående plan",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert p1.ok and p2.ok and p3.ok
    pid1, pid2, pid3 = p1.data["plan_id"], p2.data["plan_id"], p3.data["plan_id"]

    # Stamp iterative metadata directly in wp_review_plans.json (works with both old & new schema)
    store_path = tmp_path / "wp_review_plans.json"
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    for row in stored["plans"]:
        if row["plan_id"] == pid1:
            row["variant_group"] = "btn-3379"
            row["superseded_by"] = pid2
        elif row["plan_id"] == pid2:
            row["variant_group"] = "btn-3379"
            row["forked_from"] = pid1
            row["superseded_by"] = ""
        elif row["plan_id"] == pid3:
            row.pop("variant_group", None)
            row.pop("forked_from", None)
            row["superseded_by"] = ""
    store_path.write_text(json.dumps(stored), encoding="utf-8")

    # Add interactive preview HTML to pid2 only
    adir2 = workspace_root(tmp_path, "mock-staging", pid2) / "artifacts"
    adir2.mkdir(parents=True, exist_ok=True)
    (adir2 / "preview.html").write_text("<!doctype html><html><body>V2</body></html>", encoding="utf-8")

    oscar = _auth(client, "oscar")

    # pid1 shows superseded banner pointing to pid2 + variant switcher for btn-3379
    r1 = client.get(f"/wp/mock-staging/reviews/{pid1}", headers=oscar)
    assert r1.status_code == 200
    h1 = r1.get_data(as_text=True)
    assert f'data-superseded-by="{pid2}"' in h1
    assert f'href="/wp/mock-staging/reviews/{pid2}"' in h1
    assert 'data-variant-switcher="true"' in h1
    assert 'data-variant-group="btn-3379"' in h1
    assert f'data-variant-plan-id="{pid1}"' in h1
    assert f'data-variant-plan-id="{pid2}"' in h1

    # pid2 shows forked_from link to pid1 + variant switcher (no superseded banner)
    r2 = client.get(f"/wp/mock-staging/reviews/{pid2}", headers=oscar)
    assert r2.status_code == 200
    h2 = r2.get_data(as_text=True)
    assert f'data-forked-from="{pid1}"' in h2
    assert 'data-superseded-by=' not in h2
    assert 'data-variant-switcher="true"' in h2

    # pid3 (no variant_group / forked_from) renders cleanly without variant switcher
    r3 = client.get(f"/wp/mock-staging/reviews/{pid3}", headers=oscar)
    assert r3.status_code == 200
    h3 = r3.get_data(as_text=True)
    assert 'data-variant-switcher="true"' not in h3
    assert 'data-forked-from=' not in h3

    # wp_site.html lists recent wr-* workspaces with links, badges, and summary
    site_resp = client.get("/wp/mock-staging", headers=oscar)
    assert site_resp.status_code == 200
    site_html = site_resp.get_data(as_text=True)
    assert 'data-wp-reviews-section="true"' in site_html
    assert "Granskningar / Sandlådor" in site_html
    for pid in (pid1, pid2, pid3):
        assert f'data-review-row="{pid}"' in site_html
        assert f'/wp/mock-staging/reviews/{pid}' in site_html
    assert "Förslag 1: kompakt grön knapp" in site_html
    assert "btn-3379" in site_html
    assert "ersatt" in site_html
    assert f"/wp/mock-staging/reviews/{pid2}/preview" in site_html
    assert f"/wp/mock-staging/reviews/{pid1}/preview" not in site_html

    # Viewer sees workspace list on wp_site.html but without admin summaries
    jonatan = _auth(client, "jonatan")
    j_site = client.get("/wp/mock-staging", headers=jonatan)
    assert j_site.status_code == 200
    j_html = j_site.get_data(as_text=True)
    assert 'data-wp-reviews-section="true"' in j_html
    assert f'data-review-row="{pid2}"' in j_html
    assert "Förslag 1: kompakt grön knapp" not in j_html


def test_wp_review_standalone_page_draft_gallery_metadata_and_site_badge(tmp_path, monkeypatch):
    from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme
    from ecom_ops.wp_ops.review_workspace import workspace_root

    client = _setup_dashboard_env(tmp_path, monkeypatch)
    plans = ReviewPlans(data_dir=tmp_path)
    standalone = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="Ny tacksida med syntetisk orderbekräftelse och produktrekommendationer.",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    overlay = plans.open(
        "mock-staging",
        "theme",
        actor="oscar",
        summary="Befintlig overlay-ändring av knappen.",
        verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
    )
    assert standalone.ok and overlay.ok
    pid_new = standalone.data["plan_id"]
    pid_ov = overlay.data["plan_id"]

    # Populate standalone page_draft.json + preview.json + standalone-only screenshots (no live-*.png)
    new_root = workspace_root(tmp_path, "mock-staging", pid_new)
    (new_root / "work" / "page_draft.json").write_text(
        json.dumps(
            {
                "slug": "tack-for-din-bestallning",
                "title": "Tack för din beställning mock-staging-app-password-bbb",
                "page_type": "thankyou",
                "shell_path": "/",
                "template_file": "woocommerce/checkout/thankyou.php",
                "synthetic_data": True,
                "body_html": "<section><h1>Tack</h1></section>",
            }
        ),
        encoding="utf-8",
    )
    adir_new = new_root / "artifacts"
    adir_new.mkdir(parents=True, exist_ok=True)
    png = _make_png()
    for name in ("focus-desktop.png", "preview-desktop.png", "preview-mobile.png"):
        (adir_new / name).write_bytes(png)
    (adir_new / "preview.json").write_text(
        json.dumps(
            {
                "preview_kind": "standalone_page",
                "path": "/tack-for-din-bestallning/",
                "focus_target": "selector:.azom-ty-hero",
                "focus_matched": True,
                "synthetic_data": True,
            }
        ),
        encoding="utf-8",
    )
    (adir_new / "preview.html").write_text("<!doctype html><html><body>New Page</body></html>", encoding="utf-8")

    # Populate normal overlay plan with live + preview pair
    adir_ov = workspace_root(tmp_path, "mock-staging", pid_ov) / "artifacts"
    adir_ov.mkdir(parents=True, exist_ok=True)
    (adir_ov / "live-desktop.png").write_bytes(png)
    (adir_ov / "preview-desktop.png").write_bytes(png)

    oscar = _auth(client, "oscar")
    resp_new = client.get(f"/wp/mock-staging/reviews/{pid_new}", headers=oscar)
    assert resp_new.status_code == 200
    body_new = resp_new.get_data(as_text=True)

    assert 'data-standalone-page="true"' in body_new
    assert 'data-standalone-gallery="true"' in body_new
    assert "Ny sida · Sandbox (syntetisk exempeldata)" in body_new
    assert 'data-page-draft-meta="true"' in body_new
    assert "tack-for-din-bestallning" in body_new
    assert "thankyou" in body_new
    assert "woocommerce/checkout/thankyou.php" in body_new
    assert "mock-staging-app-password-bbb" not in body_new
    assert "Desktop-förhandsvisning" in body_new
    assert "Mobil-förhandsvisning" in body_new
    assert "Live (före)" not in body_new
    assert "Live vs. Förhandsvisning" not in body_new
    assert "Ingen renderad förhandsvisning." not in body_new
    assert 'data-gallery-preview-link="true"' in body_new
    assert f"/wp/mock-staging/reviews/{pid_new}/preview" in body_new
    assert f"/wp/mock-staging/reviews/{pid_new}/artifact/preview-desktop.png" in body_new
    assert f"/wp/mock-staging/reviews/{pid_new}/artifact/preview-mobile.png" in body_new
    assert f"/wp/mock-staging/reviews/{pid_new}/artifact/focus-desktop.png" in body_new

    # Existing normal overlay gallery stays intact
    resp_ov = client.get(f"/wp/mock-staging/reviews/{pid_ov}", headers=oscar)
    assert resp_ov.status_code == 200
    body_ov = resp_ov.get_data(as_text=True)
    assert 'data-standalone-page="true"' not in body_ov
    assert "Live vs. Förhandsvisning" in body_ov
    assert "Live (före)" in body_ov

    # wp_site.html displays "Ny sida" badge on standalone row only
    site_resp = client.get("/wp/mock-staging", headers=oscar)
    assert site_resp.status_code == 200
    site_html = site_resp.get_data(as_text=True)
    row_new = site_html.split(f'data-review-row="{pid_new}"', 1)[1].split("</tr>", 1)[0]
    row_ov = site_html.split(f'data-review-row="{pid_ov}"', 1)[1].split("</tr>", 1)[0]
    assert 'data-standalone-badge="true"' in row_new
    assert "Ny sida" in row_new
    assert 'data-standalone-badge="true"' not in row_ov

    # Viewer RBAC: sees standalone badge/banner, does NOT see admin page_draft metadata or gallery
    jonatan = _auth(client, "jonatan")
    j_rev = client.get(f"/wp/mock-staging/reviews/{pid_new}", headers=jonatan)
    assert j_rev.status_code == 200
    j_rev_html = j_rev.get_data(as_text=True)
    assert 'data-standalone-page="true"' in j_rev_html
    assert 'data-page-draft-meta="true"' not in j_rev_html
    assert "woocommerce/checkout/thankyou.php" not in j_rev_html
    assert 'data-live-overlay-gallery="true"' not in j_rev_html

    j_site = client.get("/wp/mock-staging", headers=jonatan)
    assert j_site.status_code == 200
    j_site_html = j_site.get_data(as_text=True)
    assert 'data-standalone-badge="true"' in j_site_html
    assert "Ny tacksida med syntetisk orderbekräftelse" not in j_site_html


def test_wp_site_review_plans_avoids_n_plus_one_and_hides_preview_from_viewer(tmp_path, monkeypatch):
    from ecom_ops.wp_ops.review_plan import ReviewPlans, VerifiedTheme
    from ecom_ops.wp_ops.review_workspace import workspace_root

    client = _setup_dashboard_env(tmp_path, monkeypatch)
    plans = ReviewPlans(data_dir=tmp_path)
    pids: list[str] = []
    for _ in range(3):
        opened = plans.open(
            "mock-staging",
            "theme",
            actor="oscar",
            verified_theme=VerifiedTheme("twentytwentyfive", "twentytwentyfive"),
        )
        assert opened.ok
        pids.append(opened.data["plan_id"])

    art_dir = workspace_root(tmp_path, "mock-staging", pids[0]) / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "preview.html").write_text("<html><body>ok</body></html>", encoding="utf-8")

    # Ensure _site_review_plans never calls ReviewPlans.has_preview_html (N+1 plan-store reads)
    def _fail_has_preview(*args, **kwargs):
        raise AssertionError("N+1 ReviewPlans.has_preview_html should not be called")

    monkeypatch.setattr(ReviewPlans, "has_preview_html", _fail_has_preview)

    oscar = _auth(client, "oscar")
    oscar_resp = client.get("/wp/mock-staging", headers=oscar)
    assert oscar_resp.status_code == 200
    oscar_html = oscar_resp.get_data(as_text=True)
    assert f"/wp/mock-staging/reviews/{pids[0]}/preview" in oscar_html
    assert f"/wp/mock-staging/reviews/{pids[1]}/preview" not in oscar_html

    jonatan = _auth(client, "jonatan")
    viewer_resp = client.get("/wp/mock-staging", headers=jonatan)
    assert viewer_resp.status_code == 200
    assert f"/wp/mock-staging/reviews/{pids[0]}/preview" not in viewer_resp.get_data(as_text=True)
