"""SIWC defaults, OpenRouter catalog search, and Grok device login."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

from ecom_ops.model_router import catalog
from ecom_ops.model_router.openai_chatgpt import (
    DYNAMIC_CLIENT_ID,
    OpenAIChatGPTOAuthRoute,
)
from ecom_ops.model_router.xai_device_login import device_login_status, start_device_login


def test_siwc_defaults_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    route = OpenAIChatGPTOAuthRoute()
    assert route.is_configured() is True
    url, _state = route.start_authorization()
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://auth.openai.com/api/accounts/authorize?")
    assert query["client_id"][0] == DYNAMIC_CLIENT_ID
    assert query["redirect_uri"][0] == "http://127.0.0.1:8080/oauth/openai/callback"
    host_id = query["ext_agent_host_id"][0]
    again, _state_again = route.start_authorization()
    assert parse_qs(urlparse(again).query)["ext_agent_host_id"][0] == host_id


def test_openrouter_search_filters_without_a_completion(monkeypatch):
    catalog._cache.clear()

    class _Response:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {
                        "id": "deepseek/deepseek-v4.1-flash-1017",
                        "name": "DeepSeek V4.1 Flash",
                        "endpoints": ["/api/v1/chat/completions"],
                    },
                    {
                        "id": "deepseek/deepseek-v4.1-flash-1017:batch",
                        "name": "DeepSeek batch",
                        "endpoints": ["/api/v1/batches"],
                    },
                    {
                        "id": "openai/gpt-4o-mini",
                        "name": "GPT-4o mini",
                        "endpoints": ["/api/v1/chat/completions"],
                    },
                ]
            }

    monkeypatch.setattr(catalog.requests, "get", lambda *args, **kwargs: _Response())
    found = catalog.search_models("openrouter", "flash")
    assert found["error"] == ""
    assert [row["id"] for row in found["models"]] == ["deepseek/deepseek-v4.1-flash-1017"]
    catalog._cache.clear()


def test_device_login_keeps_code_and_drops_token_text(tmp_path, monkeypatch):
    import ecom_ops.model_router.xai_device_login as login

    script = tmp_path / "fake_grok.py"
    script.write_text(
        "import time\n"
        "print('access_token=super-secret', flush=True)\n"
        "print('https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH', flush=True)\n"
        "print('ABCD-EFGH', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(login, "_command", lambda: [sys.executable, str(script)])
    login._proc = None
    try:
        status = start_device_login(wait_s=3)
        assert status["user_code"] == "ABCD-EFGH"
        assert status["verification_uri"].startswith("https://accounts.x.ai/")
        raw = (tmp_path / "data" / "xai_device_login.json").read_text(encoding="utf-8")
        assert "super-secret" not in raw
        assert "access_token" not in raw
    finally:
        proc = login._proc
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        login._proc = None


def test_device_login_success_enables_grok_build(tmp_path, monkeypatch):
    import ecom_ops.model_router.xai_device_login as login

    script = tmp_path / "fake_grok.py"
    script.write_text(
        "print('https://auth.x.ai/oauth2/device?user_code=WXYZ-1234', flush=True)\n"
        "print('WXYZ-1234', flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(login, "_command", lambda: [sys.executable, str(script)])
    login._proc = None
    start_device_login(wait_s=3)
    deadline = time.time() + 3
    while time.time() < deadline and device_login_status()["status"] != "finished":
        time.sleep(0.05)
    assert device_login_status()["status"] == "finished"
    assert device_login_status()["user_code"] == ""
    runtime = (tmp_path / "data" / "runtime.env").read_text(encoding="utf-8")
    assert "AZOM_XAI_GROK_BUILD=1" in runtime
    stored = json.loads((tmp_path / "data" / "xai_device_login.json").read_text(encoding="utf-8"))
    assert "refresh_token" not in stored


def test_device_login_does_not_inherit_stdin(tmp_path, monkeypatch):
    import ecom_ops.model_router.xai_device_login as login

    seen: dict = {}

    class _Proc:
        stdout = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def _popen(command, **kwargs):
        seen["command"] = command
        seen["stdin"] = kwargs.get("stdin")
        proc = _Proc()
        proc.stdout = iter(())
        return proc

    monkeypatch.setattr(login.subprocess, "Popen", _popen)
    monkeypatch.setattr(login, "_command", lambda: ["grok", "login", "--device-auth"])
    login._proc = None
    try:
        start_device_login(wait_s=0.1)
        assert seen["stdin"] is subprocess.DEVNULL
        assert seen["command"] == ["grok", "login", "--device-auth"]
    finally:
        reader = login._reader
        if reader is not None:
            reader.join(timeout=2)
        login._proc = None
        login._reader = None


def test_openai_chatgpt_catalog_search_lists_connected_models(tmp_path, monkeypatch):
    import responses as rsps_mod

    from ecom_ops.model_router.openai_chatgpt import MODELS_URL, OpenAIChatGPTOAuthRoute
    from ecom_ops.model_router.store import ModelSession

    catalog._cache.clear()
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))

    # Disconnected -> friendly error
    disconnected = catalog.search_models("openai", "gpt")
    assert disconnected["models"] == []
    assert "ansluten" in disconnected["error"].lower()

    route = OpenAIChatGPTOAuthRoute()
    route.store.save(
        ModelSession(
            provider="openai",
            account_id="default",
            client_id="oaiapp_123",
            access_token="plus-tok",
            refresh_token="plus-ref",
            expires_at=time.time() + 3600,
            generation=1,
            status="connected",
            scopes=["openid", "offline_access", "chatgpt.tokens.use.direct"],
        )
    )
    with rsps_mod.RequestsMock() as rsps:
        rsps.add(
            rsps_mod.GET,
            MODELS_URL,
            json={
                "data": [
                    {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini", "visibility": "list"},
                    {"id": "gpt-6.2-sol", "name": "GPT-6.2 Sol", "visibility": "list"},
                    {"id": "internal-eval", "name": "Hidden", "visibility": "hidden"},
                ]
            },
            status=200,
        )
        found = catalog.search_models("openai", "gpt")
    assert found["error"] == ""
    assert [m["id"] for m in found["models"]] == ["gpt-5.4-mini", "gpt-6.2-sol"]
