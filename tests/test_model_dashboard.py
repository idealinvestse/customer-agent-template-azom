"""Oscar model panel: CSRF, no fallback, no token echo."""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    spec = importlib.util.spec_from_file_location("azom_dashboard_models", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard_models"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _auth(user="oscar", password="oscar", *, client=None):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if client is not None:
        client.get("/oscar", headers=headers)
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token")
        if csrf:
            headers["X-CSRF-Token"] = csrf
    return headers


def test_oscar_model_panel_and_isolated_test(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()

    denied = client.get("/oscar", headers=_auth("jonatan", "jonatan"))
    assert denied.status_code == 403

    page = client.get("/oscar", headers=_auth())
    assert page.status_code == 200
    assert b"Modellrouter" in page.data
    assert b"grok_build" in page.data
    assert b"access_token" not in page.data

    failed = client.post(
        "/oscar/models/test",
        headers=_auth(client=client),
        data={"route_id": "chatgpt_oauth"},
    )
    assert failed.status_code == 302
    again = client.get("/oscar", headers=_auth())
    assert b"misslyckades" in again.data
    body = again.get_data(as_text=True)
    assert "refresh-secret" not in body
    assert "authorization code" not in body.lower()


def test_oscar_saves_model_ids_without_provider_call(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("XAI_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("AZOM_XAI_GROK_BUILD", raising=False)
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()

    denied = client.post(
        "/oscar/models/settings",
        headers=_auth("jonatan", "jonatan", client=client),
        data={"OPENAI_MODEL": "gpt-test"},
    )
    assert denied.status_code == 403

    saved = client.post(
        "/oscar/models/settings",
        headers=_auth(client=client),
        data={
            "OPENAI_MODEL": "gpt-test",
            "XAI_MODEL": "grok-4",
            "OPENROUTER_MODEL": "deepseek/deepseek-v4-flash",
            "AZOM_XAI_GROK_BUILD": "1",
        },
    )
    assert saved.status_code == 302
    page = client.get("/oscar", headers=_auth())
    text = page.get_data(as_text=True)
    assert "gpt-test" in text
    assert "deepseek/deepseek-v4-flash" in text
    assert "Logga in med xAI" in text
    assert "auth.json" in text
    runtime = (tmp_path / "data" / "runtime.env").read_text(encoding="utf-8")
    assert "OPENAI_MODEL=gpt-test" in runtime
    assert "AZOM_XAI_GROK_BUILD=1" in runtime
    assert "sk-" not in runtime

    from ecom_ops.model_router import catalog as model_catalog

    monkeypatch.setattr(
        model_catalog,
        "search_models",
        lambda provider, query, limit=25: {
            "provider": provider,
            "models": [
                {
                    "id": "deepseek/deepseek-v4.1-flash-1017",
                    "name": "DeepSeek Flash",
                }
            ],
            "error": "",
        },
    )
    catalog = client.get("/oscar?model_q=flash&catalog=openrouter", headers=_auth())
    assert catalog.status_code == 200
    assert b"deepseek/deepseek-v4.1-flash-1017" in catalog.data

    start = client.get("/oauth/openai/start", headers=_auth(), follow_redirects=False)
    assert start.status_code == 302
    assert start.headers["Location"].startswith("https://auth.openai.com/api/accounts/authorize?")


def test_xai_login_status_updates_without_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.chdir(DASH_DIR)
    data = tmp_path / "data"
    data.mkdir()
    (data / "xai_device_login.json").write_text(
        '{"status":"pending","verification_uri":"https://accounts.x.ai/oauth2/device",'
        '"user_code":"ABCD-EFGH","detail":"väntar på kod"}',
        encoding="utf-8",
    )
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()

    denied = client.get(
        "/oscar/models/xai/login/status",
        headers=_auth("jonatan", "jonatan"),
    )
    assert denied.status_code == 403

    pending = client.get("/oscar/models/xai/login/status", headers=_auth())
    assert pending.status_code == 200
    assert pending.is_json
    body = pending.get_json()
    assert body["status"] == "pending"
    assert body["user_code"] == "ABCD-EFGH"
    assert body["verification_uri"].startswith("https://accounts.x.ai/")
    assert "access_token" not in body
    assert "refresh_token" not in body

    page = client.get("/oscar", headers=_auth())
    html = page.get_data(as_text=True)
    assert 'data-status="pending"' in html
    assert "/oscar/models/xai/login/status" in html
    assert "ABCD-EFGH" in html

    (data / "xai_device_login.json").write_text(
        '{"status":"finished","verification_uri":"https://accounts.x.ai/oauth2/device",'
        '"user_code":"","detail":"Grok Build avslutade inloggningen"}',
        encoding="utf-8",
    )
    done = client.get("/oscar/models/xai/login/status", headers=_auth())
    assert done.get_json()["status"] == "finished"
    assert done.get_json()["user_code"] == ""
    finished = client.get("/oscar", headers=_auth())
    assert "Inloggningen är klar" in finished.get_data(as_text=True)

    bad = client.post(
        "/oscar/models/settings",
        headers=_auth(client=client),
        data={"OPENAI_MODEL": "not a model"},
        follow_redirects=True,
    )
    assert bad.status_code == 200
    assert "Ogiltigt" in bad.get_data(as_text=True)


def test_oscar_openrouter_model_test_records_telemetry(tmp_path, monkeypatch):
    import json

    from ecom_ops.model_router.base import ModelResult
    from ecom_ops.model_router.router import ModelRouter

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()

    def _fake_complete_route(self, route_id, request, *, telemetry=None):
        return ModelResult(
            content="PONG",
            provider="openrouter",
            route_id=route_id,
            model="openai/gpt-4o-mini",
            cost_usd=0.0012,
            usage_known=True,
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
        )

    monkeypatch.setattr(ModelRouter, "complete_route", _fake_complete_route)

    resp = client.post(
        "/oscar/models/test",
        headers=_auth(client=client),
        data={"route_id": "openrouter"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Modelltest ok utan fallback" in resp.get_data(as_text=True)

    tel_file = tmp_path / "data" / "telemetry.jsonl"
    assert tel_file.is_file()
    rows = [json.loads(line) for line in tel_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    test_rows = [r for r in rows if r["action"] == "llm_model_test"]
    assert test_rows
    assert test_rows[-1]["site"] == "azom"
    assert test_rows[-1]["cost_usd"] == 0.0012


def test_oscar_profile_mounts_save_test_reset_and_breaker_reset(tmp_path, monkeypatch):
    import json

    from ecom_ops.model_router.base import ModelResult
    from ecom_ops.model_router.circuit_breaker import record_failure
    from ecom_ops.model_router.errors import ModelUnavailableError
    from ecom_ops.model_router.router import ModelRouter

    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # Jonatan cannot mutate profiles or reset breaker
    j_hdrs = _auth("jonatan", "jonatan")
    with client.session_transaction() as sess:
        sess["csrf_token"] = "j-csrf"
    j_hdrs["X-CSRF-Token"] = "j-csrf"
    for path in (
        "/oscar/models/profiles/save",
        "/oscar/models/profiles/test",
        "/oscar/models/profiles/reset",
        "/oscar/models/breaker/reset",
    ):
        assert client.post(path, headers=j_hdrs, data={"profile_id": "cases_draft"}).status_code == 403

    # Oscar saves a profile override
    save_resp = client.post(
        "/oscar/models/profiles/save",
        headers=_auth(client=client),
        data={
            "profile_id": "cases_draft",
            "prompt_ref": "draft",
            "max_output_tokens": "900",
            "temperature": "0.25",
            "reasoning_effort": "medium",
            "idle_s": "20",
            "hard_s": "50",
            "slot_id": "primary",
            "route_id": "openrouter",
            "model_id": "openai/gpt-4o-mini",
            "enabled_0": "on",
        },
        follow_redirects=True,
    )
    assert save_resp.status_code == 200
    assert "Profil cases_draft sparad" in save_resp.get_data(as_text=True)
    assert (tmp_path / "data" / "model_profiles.override.yaml").is_file()

    # Isolated slot test
    seen = {}

    def _fake_complete_slot(self, profile_id, slot_id, request, *, telemetry=None):
        seen["profile_id"] = profile_id
        seen["slot_id"] = slot_id
        return ModelResult(
            content="PONG",
            provider="openrouter",
            route_id="openrouter",
            model="openai/gpt-4o-mini",
            cost_usd=0.0005,
        )

    monkeypatch.setattr(ModelRouter, "complete_slot", _fake_complete_slot)
    slot_resp = client.post(
        "/oscar/models/profiles/test",
        headers=_auth(client=client),
        data={"profile_id": "cases_draft", "slot_id": "primary"},
        follow_redirects=True,
    )
    assert slot_resp.status_code == 200
    assert seen == {"profile_id": "cases_draft", "slot_id": "primary"}
    assert "Slot cases_draft/primary ok utan fallback" in slot_resp.get_data(as_text=True)

    # Circuit breaker visible and resettable (2 unavailable errors trip threshold=2)
    record_failure("openrouter", "openai/gpt-4o-mini", ModelUnavailableError("down"))
    record_failure("openrouter", "openai/gpt-4o-mini", ModelUnavailableError("down"))
    before_reset = client.get("/oscar", headers=_auth())
    assert "open (" in before_reset.get_data(as_text=True)

    brk_resp = client.post(
        "/oscar/models/breaker/reset",
        headers=_auth(client=client),
        data={"breaker_key": "route:openrouter"},
        follow_redirects=True,
    )
    assert brk_resp.status_code == 200
    assert "Circuit breaker nollställd" in brk_resp.get_data(as_text=True)
    after_reset = client.get("/oscar", headers=_auth())
    assert "open (" not in after_reset.get_data(as_text=True)

    # Profile reset
    rst_resp = client.post(
        "/oscar/models/profiles/reset",
        headers=_auth(client=client),
        data={"profile_id": "cases_draft"},
        follow_redirects=True,
    )
    assert rst_resp.status_code == 200
    assert "Profilstandard återställd" in rst_resp.get_data(as_text=True)

    audit_lines = [
        json.loads(line)
        for line in (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    actions = {row["action"] for row in audit_lines}
    assert {"model_profile_save", "model_breaker_reset", "model_profile_reset"} <= actions
