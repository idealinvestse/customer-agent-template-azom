"""Dashboard auth harden: salted hashes, CSRF, no mock passwords in prod."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_mod():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    # Fresh module each time so env changes apply
    name = "azom_dashboard_auth"
    sys.modules.pop(name, None)
    sys.modules.pop("settings_store", None)
    sys.modules.pop("secret_probes", None)
    sys.modules.pop("status", None)
    spec = importlib.util.spec_from_file_location(name, DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def dash_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret-key-for-csrf")
    monkeypatch.chdir(DASH_DIR)
    return tmp_path


def test_mock_passwords_rejected_when_not_mock(dash_env, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    mod = _load_mod()
    assert mod._authenticate("jonatan", "jonatan") is None
    assert mod._authenticate("oscar", "oscar") is None


def test_werkzeug_password_hash_accepted(dash_env, monkeypatch):
    from werkzeug.security import generate_password_hash

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    hashed = generate_password_hash("s3cret!")
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", hashed)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    mod = _load_mod()
    actor = mod._authenticate("jonatan", "s3cret!")
    assert actor is not None
    assert actor["name"] == "jonatan"
    assert mod._authenticate("jonatan", "wrong") is None


def test_legacy_sha256_hash_still_works(dash_env, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    legacy = hashlib.sha256(b"legacy-pass").hexdigest()
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", legacy)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    mod = _load_mod()
    assert mod._authenticate("jonatan", "legacy-pass") is not None


def test_post_without_csrf_rejected(dash_env, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    mod = _load_mod()
    mod.app.config["TESTING"] = True
    client = mod.app.test_client()
    resp = client.post(
        "/settings",
        headers=_basic("jonatan", "jonatan"),
        data={"customer": "azom", "domains": "se", "budget_cap_llm": "80", "openrouter_cap": "100"},
    )
    assert resp.status_code == 400
    assert b"CSRF" in resp.data or b"csrf" in resp.data.lower()


def test_post_with_csrf_accepted(dash_env, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    # Need writable config dir for settings save
    cfg = dash_env / "cfg"
    cfg.mkdir()
    for name in ("sites.yaml", "limits.yaml", "integrations.yaml", "rbac.yaml"):
        src = ROOT / "config" / name
        (cfg / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg))
    mod = _load_mod()
    mod.app.config["TESTING"] = True
    client = mod.app.test_client()
    hdrs = _basic("jonatan", "jonatan")
    client.get("/", headers=hdrs)
    with client.session_transaction() as sess:
        token = sess.get("csrf_token")
    assert token
    resp = client.post(
        "/settings",
        headers={**hdrs, "X-CSRF-Token": token},
        data={
            "customer": "azom",
            "domains": "se,no",
            "budget_cap_llm": "80",
            "openrouter_cap": "100",
            "jonatan_role": "read_only",
        },
    )
    assert resp.status_code in (200, 302)
