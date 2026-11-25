"""Telegram /oauth panel, inline buttons, ephemeral callback URL, and xAI login."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import responses

from ecom_ops.bot import __main__ as tg_main
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.openclaw_commands import TELEGRAM_MENU_COMMANDS
from ecom_ops.bot.reply import ActionMarkup, actions_to_telegram_markup
from ecom_ops.bot.store import ConversationStore
from ecom_ops.bot.telegram_oauth import is_ephemeral_oauth_message, match_natural_oauth_intent
from ecom_ops.model_router import circuit_breaker
from ecom_ops.model_router.errors import ModelAuthError


def _setup_telegram(monkeypatch, tmp_path, *, suffix: str = "") -> BotHandler:
    root_cfg = Path(__file__).resolve().parents[1] / "config"
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(root_cfg))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111,222")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "111:oscar,222:jonatan")
    monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", "psid-oscar")
    monkeypatch.setenv("MESSENGER_ACTOR_MAP", "psid-oscar:oscar")
    monkeypatch.setenv("OPENAI_SIWC_CLIENT_ID", "dynamic_agent_client")
    monkeypatch.setenv("OPENAI_SIWC_REDIRECT_URI", "http://127.0.0.1:8080/oauth/openai/callback")
    monkeypatch.setenv("OPENAI_SIWC_AUTHORIZATION_URL", "https://auth.example/authorize")
    monkeypatch.setenv("OPENAI_SIWC_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv("OPENAI_SIWC_HOST_ID", "host-tg-1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    return BotHandler(store=ConversationStore(path=tmp_path / f"state{suffix}.json"))


def test_oauth_rbac_menu_and_natural_language(monkeypatch, tmp_path):
    handler = _setup_telegram(monkeypatch, tmp_path)
    assert any(c["command"] == "oauth" for c in TELEGRAM_MENU_COMMANDS)

    # Jonatan denied fail-closed
    j_reply = handler.handle("222", "/oauth")
    assert "Endast Oscar" in j_reply.text

    # Natural language denied for Jonatan, allowed for Oscar even with zero LLMs configured
    j_nat = handler.handle("222", "länka chatgpt oauth")
    assert "Endast Oscar" in j_nat.text

    msg_handler = BotHandler(store=ConversationStore(path=tmp_path / "msg_state.json"), channel="messenger")
    m_reply = msg_handler.handle("psid-oscar", "/oauth")
    assert "Telegram" in m_reply.text

    o_panel = handler.handle("111", "/oauth")
    assert "ChatGPT Plus" in o_panel.text
    assert "xAI Grok Build" in o_panel.text
    assert isinstance(o_panel.actions, ActionMarkup)
    markup = actions_to_telegram_markup(o_panel.actions)
    assert markup and markup["inline_keyboard"][0][0]["callback_data"] == "oauth:chatgpt:start"

    o_nat_both = handler.handle("111", "Kan vi länka chatgpt oauth och x.ai oauth?")
    assert "ChatGPT Plus" in o_nat_both.text and "xAI Grok Build" in o_nat_both.text

    o_mod = handler.handle("111", "/model oauth")
    assert "Modell-OAuth" in o_mod.text

    # Non-OAuth ordinary phrasing does NOT falsely hijack into OAuth menu
    assert match_natural_oauth_intent("Hur mår kundens order med chatgpt-fråga?") is None
    assert match_natural_oauth_intent("anslut x.ai oauth") == "xai"


@responses.activate
def test_telegram_chatgpt_start_and_ephemeral_callback_zero_sqlite_secrets(monkeypatch, tmp_path):
    handler = _setup_telegram(monkeypatch, tmp_path)

    # Trip open_auth breaker first to verify connect clears it
    circuit_breaker.record_failure(
        "chatgpt_oauth",
        "gpt-5.2",
        ModelAuthError("revoked", code="invalid_grant"),
    )
    assert circuit_breaker.allow_attempt("chatgpt_oauth", "gpt-5.2")[0] is False

    # Oscar clicks/asks to start ChatGPT OAuth
    start_reply = handler.handle("111", "länka chatgpt oauth")
    assert start_reply.actions is not None
    url_btn = start_reply.actions.rows[0][0]
    assert url_btn.url and url_btn.url.startswith("https://auth.example/authorize?")
    state = parse_qs(urlparse(url_btn.url).query)["state"][0]
    assert "code_verifier" not in start_reply.text

    callback_url = (
        f"http://127.0.0.1:8080/oauth/openai/callback"
        f"?code=SECRET_AUTH_CODE_777&state={state}&client_id=oaiapp_tg_1"
    )
    assert is_ephemeral_oauth_message(callback_url) is True
    assert is_ephemeral_oauth_message(f"/oauth callback {callback_url}") is True

    responses.add(
        responses.POST,
        "https://auth.example/token",
        json={
            "access_token": "SECRET_ACCESS_888",
            "refresh_token": "SECRET_REFRESH_999",
            "expires_in": 3600,
            "earliest_refresh_at": int(time.time()) + 30,
            "scope": "openid offline_access chatgpt.tokens.use.direct",
        },
        status=200,
    )

    store = TelegramJobStore()
    tg_calls: list[tuple[str, dict]] = []

    def fake_api(_token: str, method: str, **params):
        tg_calls.append((method, params))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(tg_main, "_api", fake_api)
    update = {
        "update_id": 501,
        "message": {
            "message_id": 91,
            "chat": {"id": 111},
            "text": callback_url,
        },
    }
    handled = tg_main._handle_ephemeral_oauth_update("tg-tok", handler, store, update)
    assert handled is True
    # Pasted message was deleted and reply was sent
    assert any(m == "deleteMessage" and p.get("message_id") == 91 for m, p in tg_calls)
    sent_texts = [p.get("text", "") for m, p in tg_calls if m == "sendMessage"]
    assert any("ChatGPT Plus OAuth ansluten" in t for t in sent_texts)
    assert store.update_offset() == 502

    # Defense-in-depth: even accept_update must scrub ephemeral callback URL
    store.accept_update(
        {
            "update_id": 502,
            "message": {
                "message_id": 92,
                "chat": {"id": 111},
                "text": "http://127.0.0.1:8080/oauth/openai/callback?code=NEVER_DB_SECRET&state=ST999",
            },
        }
    )

    db_bytes = store.path.read_bytes()
    for secret_needle in (b"SECRET_AUTH_CODE_777", b"NEVER_DB_SECRET", b"SECRET_ACCESS_888", b"SECRET_REFRESH_999", state.encode()):
        assert secret_needle not in db_bytes

    audit_text = (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8")
    assert "SECRET_AUTH_CODE_777" not in audit_text
    assert state not in audit_text
    assert handler.store.get("111") is None

    # Circuit breaker cleared immediately on connect
    assert circuit_breaker.allow_attempt("chatgpt_oauth", "gpt-5.2")[0] is True

    # Isolated route test works via callback button without fallback
    responses.add(
        responses.POST,
        "https://api.openai.com/v1/responses",
        body='data: {"type":"response.output_text.delta","delta":"PONG"}\n\ndata: [DONE]\n\n',
        status=200,
    )
    test_reply = handler.handle_callback("111", "oauth:test:chatgpt_oauth")
    assert "chatgpt_oauth svarade utan fallback" in test_reply.text
    status_file = json.loads((tmp_path / "data" / "model_router_status.json").read_text(encoding="utf-8"))
    assert status_file["ok"] is True
    assert status_file["route_id"] == "chatgpt_oauth"

    # Disconnect works
    disc = handler.handle_callback("111", "oauth:chatgpt:disconnect")
    assert "frånkopplat" in disc.text


def test_telegram_xai_device_login_buttons_and_completion_clears_breaker(monkeypatch, tmp_path):
    import ecom_ops.model_router.xai_device_login as login

    handler = _setup_telegram(monkeypatch, tmp_path)
    circuit_breaker.record_failure(
        "xai_grok_build",
        "grok-4",
        ModelAuthError("expired"),
    )
    assert circuit_breaker.allow_attempt("xai_grok_build", "grok-4")[0] is False

    script = tmp_path / "fake_grok.py"
    script.write_text(
        "import time\n"
        "print('Visit https://auth.x.ai/oauth2/device?user_code=ABCD-EFGH', flush=True)\n"
        "print('Code: ABCD-EFGH', flush=True)\n"
        "time.sleep(0.15)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(login, "_command", lambda: [sys.executable, str(script)])
    login._proc = None
    login._reader = None

    reply = handler.handle("111", "/oauth xai")
    assert "ABCD-EFGH" in reply.text
    assert reply.actions is not None
    assert any(
        btn.url and btn.url.startswith("https://auth.x.ai/oauth2/device")
        for row in reply.actions.rows
        for btn in row
    )

    deadline = time.time() + 4.0
    while time.time() < deadline and login.device_login_status()["status"] != "finished":
        time.sleep(0.05)
    assert login.device_login_status()["status"] == "finished"

    # Breaker cleared and AZOM_XAI_GROK_BUILD enabled
    assert circuit_breaker.allow_attempt("xai_grok_build", "grok-4")[0] is True
    st = handler.handle_callback("111", "oauth:xai:status")
    assert "inloggat och aktiverat" in st.text
    assert "auth.json" in st.text

    # Fake subprocess test cleanup
    if login._reader is not None:
        login._reader.join(timeout=2)
    login._proc = None
    login._reader = None


def test_telegram_xai_missing_binary_reports_cleanly(monkeypatch, tmp_path):
    import ecom_ops.model_router.xai_device_login as login

    handler = _setup_telegram(monkeypatch, tmp_path)
    monkeypatch.delenv("XAI_GROK_BIN", raising=False)
    monkeypatch.setattr(login, "_command", lambda: ["", "login", "--device-auth"])
    login._proc = None

    reply = handler.handle_callback("111", "oauth:xai:start")
    assert "grok saknas" in reply.text
    assert subprocess.DEVNULL is not None
