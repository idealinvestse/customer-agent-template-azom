"""Dashboard status for the mock WordPress registry."""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
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
    assert hidden.status_code == 200
    assert SECRET not in hidden_body
    assert "***" in hidden_body
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
    assert "Lösenord skickas inte." in form_body
    assert "lösenfras" in form_body
    assert "ärver registret" in form_body
    posted = client.post(
        "/wp/mock-staging/ssh",
        headers=oscar,
        data={
            "_csrf": oscar["X-CSRF-Token"],
            "files_root": "/var/www/from-dashboard",
            "ssh_host": "mock-staging.example.test",
            "ssh_user": "deploy",
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
