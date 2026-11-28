"""Tests for Oscar-managed Telegram User ID access control (/oscar/telegram)."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from ecom_ops import healthcheck
from ecom_ops.audit import read_audit_log
from ecom_ops.bot.actors import (
    TelegramActorDenied,
    channel_peer_allowed,
    channel_posture,
    effective_telegram_actor_map,
    effective_telegram_allowlist,
    is_chat_allowed,
    load_telegram_access_store,
    parse_actor_map,
    resolve_actor,
    resolve_channel_actor,
    resolve_telegram_actor,
    save_telegram_access_store,
    telegram_access_path,
)

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _load_dashboard_app():
    if str(ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    for mod_name in ("azom_dashboard", "settings_store", "status", "secret_probes", "oscar_routes"):
        sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def _auth(user: str = "jonatan", password: str = "jonatan", *, client=None) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if client is not None:
        client.get("/", headers=headers)
        with client.session_transaction() as sess:
            csrf = sess.get("csrf_token")
        if csrf:
            headers["X-CSRF-Token"] = csrf
    return headers


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sites.yaml").write_text("customer: azom\ndomains:\n  - se\n", encoding="utf-8")
    (cfg / "limits.yaml").write_text("openrouter_cap: 100\n", encoding="utf-8")
    (cfg / "integrations.yaml").write_text("email:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_store_save_load_clear_and_corrupt_handling(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    assert load_telegram_access_store() == {}

    saved = save_telegram_access_store(
        {"jonatan": "111222333, 444555666", "oscar": "-100999888777; 777888999"},
        updated_by="oscar",
    )
    assert saved == {
        "jonatan": ["111222333", "444555666"],
        "oscar": ["-100999888777", "777888999"],
    }
    path = telegram_access_path()
    assert path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert load_telegram_access_store() == saved

    # Clear both roles
    cleared = save_telegram_access_store({"jonatan": "", "oscar": []}, updated_by="oscar")
    assert cleared == {}
    assert load_telegram_access_store() == {}

    # Corrupt JSON returns empty dict cleanly
    path.write_text("{not-valid-json", encoding="utf-8")
    assert load_telegram_access_store() == {}

    # Wrong top-level type returns empty dict
    path.write_text(json.dumps(["12345"]), encoding="utf-8")
    assert load_telegram_access_store() == {}


def test_store_rejects_invalid_and_duplicate_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))

    for bad in ("abc", "@jonatan", "12", "1" * 21, "123:jonatan", "12.34"):
        with pytest.raises(ValueError):
            save_telegram_access_store({"jonatan": bad})

    with pytest.raises(ValueError, match="flera roller"):
        save_telegram_access_store({"jonatan": "123456789", "oscar": "123456789"})

    with pytest.raises(ValueError, match="roll"):
        save_telegram_access_store({"unknown_role": "123456789"})


def test_effective_merge_env_precedence_and_fail_closed_live(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    monkeypatch.delenv("TELEGRAM_FAIL_CLOSED", raising=False)
    monkeypatch.delenv("MESSENGER_ALLOWED_PSIDS", raising=False)
    monkeypatch.delenv("MESSENGER_ACTOR_MAP", raising=False)

    # Strictly fail-closed when both env and store are empty in live mode
    assert effective_telegram_allowlist() == set()
    assert effective_telegram_actor_map() == {}
    assert channel_peer_allowed("telegram", "111222333") is False
    assert is_chat_allowed("telegram", "111222333") is False
    with pytest.raises(TelegramActorDenied):
        resolve_telegram_actor("111222333")

    # Save Jonatan + Oscar in panel store
    save_telegram_access_store({"jonatan": "111222333", "oscar": "999888777"})

    assert effective_telegram_allowlist() == {"111222333", "999888777"}
    assert effective_telegram_actor_map() == {
        "111222333": "jonatan",
        "999888777": "oscar",
    }
    assert parse_actor_map("telegram") == {
        "111222333": "jonatan",
        "999888777": "oscar",
    }
    assert channel_peer_allowed("telegram", 111222333) is True
    assert is_chat_allowed("telegram", 999888777) is True
    assert channel_peer_allowed("telegram", 555444333) is False
    assert resolve_telegram_actor(111222333) == "jonatan"
    assert resolve_actor("telegram", 999888777) == "oscar"
    with pytest.raises(TelegramActorDenied):
        resolve_channel_actor("telegram", 555444333)

    # Messenger must remain completely unaffected and fail-closed
    assert channel_peer_allowed("messenger", "111222333") is False
    assert channel_posture("messenger")["allowlist_set"] is False

    # Env merges and TELEGRAM_ACTOR_MAP wins on conflict
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "333444555")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "111222333:oscar,333444555:agent")
    assert effective_telegram_allowlist() == {"111222333", "999888777", "333444555"}
    assert effective_telegram_actor_map()["111222333"] == "oscar"
    assert effective_telegram_actor_map()["999888777"] == "oscar"
    assert effective_telegram_actor_map()["333444555"] == "agent"
    assert resolve_telegram_actor("111222333") == "oscar"
    assert resolve_telegram_actor("333444555") == "agent"


def test_dashboard_rbac_save_validation_audit_and_csp(dash_client, tmp_path, monkeypatch):
    # Jonatan cannot view or post
    j_headers = _auth("jonatan", "jonatan", client=dash_client)
    assert dash_client.get("/oscar/telegram", headers=j_headers).status_code == 403
    assert (
        dash_client.post(
            "/oscar/telegram/actors",
            headers=j_headers,
            data={"jonatan_chat_ids": "111222", "oscar_chat_ids": "333444"},
        ).status_code
        == 403
    )

    # Oscar gets page with required strings + UI elements + zero inline JS
    o_headers = _auth("oscar", "oscar", client=dash_client)
    page = dash_client.get("/oscar/telegram", headers=o_headers)
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Allowlist i processen" in html
    assert "processmiljön" in html
    assert "@userinfobot" in html
    assert "/start" in html
    assert 'action="/oscar/telegram/actors"' in html
    assert 'name="jonatan_chat_ids"' in html
    assert 'name="oscar_chat_ids"' in html
    script_tags = re.findall(r"<script\b([^>]*)>", html, re.IGNORECASE)
    assert all("src=" in attrs.lower() for attrs in script_tags)
    tmpl_src = (ROOT / "infrastructure" / "dashboard" / "templates" / "oscar_telegram.html").read_text(
        encoding="utf-8"
    )
    assert "<script" not in tmpl_src.lower()
    assert "| safe" not in tmpl_src.lower()
    assert not re.search(r"\bon[a-z]+\s*=", tmpl_src, re.IGNORECASE)

    # Invalid ID shows error flash and does not save
    bad_resp = dash_client.post(
        "/oscar/telegram/actors",
        headers=o_headers,
        data={"jonatan_chat_ids": "not-a-number", "oscar_chat_ids": "987654321"},
        follow_redirects=True,
    )
    assert bad_resp.status_code == 200
    assert "Ogiltigt Telegram User ID" in bad_resp.get_data(as_text=True)
    assert load_telegram_access_store() == {}

    # Duplicate ID between Jonatan and Oscar is rejected
    dup_resp = dash_client.post(
        "/oscar/telegram/actors",
        headers=o_headers,
        data={"jonatan_chat_ids": "123456789", "oscar_chat_ids": "123456789"},
        follow_redirects=True,
    )
    assert dup_resp.status_code == 200
    assert "flera roller" in dup_resp.get_data(as_text=True)
    assert load_telegram_access_store() == {}

    # Valid save persists JSON, updates page, writes audit, leaves secrets.env untouched
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-test-token")
    ok_resp = dash_client.post(
        "/oscar/telegram/actors",
        headers=o_headers,
        data={"jonatan_chat_ids": "123456789", "oscar_chat_ids": "987654321"},
        follow_redirects=True,
    )
    assert ok_resp.status_code == 200
    ok_html = ok_resp.get_data(as_text=True)
    assert "123456789" in ok_html
    assert "987654321" in ok_html
    assert "Bot redo" in ok_html
    assert load_telegram_access_store() == {
        "jonatan": ["123456789"],
        "oscar": ["987654321"],
    }
    secrets_file = Path(os.environ["AZOM_DATA_DIR"]) / "secrets.env"
    if secrets_file.is_file():
        secrets_txt = secrets_file.read_text(encoding="utf-8")
        assert "TELEGRAM_ALLOWED_CHAT_IDS" not in secrets_txt
        assert "TELEGRAM_ACTOR_MAP" not in secrets_txt

    events = read_audit_log(limit=10)
    tg_events = [e for e in events if e.get("action") == "telegram_access_save"]
    assert tg_events
    assert tg_events[0]["actor"] == "oscar"
    assert tg_events[0]["details"]["jonatan_count"] == 1
    assert tg_events[0]["details"]["oscar_count"] == 1

    # Clearing works cleanly
    clear_resp = dash_client.post(
        "/oscar/telegram/actors",
        headers=o_headers,
        data={"jonatan_chat_ids": "", "oscar_chat_ids": ""},
        follow_redirects=True,
    )
    assert clear_resp.status_code == 200
    assert "rensade" in clear_resp.get_data(as_text=True).lower()
    assert load_telegram_access_store() == {}


def test_healthcheck_live_telegram_turns_green_with_saved_store(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "a" * 32)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "jonatan-live-pass")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "oscar-live-pass")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dashboard.azom.no")
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-live-token-xyz")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    monkeypatch.setenv("WOO_BASE_URL", "https://azom.no")
    monkeypatch.setenv("WOO_CONSUMER_KEY", "ck_test_123")
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "cs_test_secret_456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-789")

    rep_empty = healthcheck.run_systemcheck(
        ROOT, mode="live", check_network=False, check_ports=False, apply_dotenv=False
    )
    by_empty = {c.name: c for c in rep_empty.checks}
    assert by_empty["Telegram-bot"].status == "fail"

    save_telegram_access_store({"jonatan": "123456789", "oscar": "987654321"})

    rep_ready = healthcheck.run_systemcheck(
        ROOT, mode="live", check_network=False, check_ports=False, apply_dotenv=False
    )
    by_ready = {c.name: c for c in rep_ready.checks}
    assert by_ready["Telegram-bot"].status == "ok"
    assert "allowlist=satt" in by_ready["Telegram-bot"].detail
    assert "actor_map=satt" in by_ready["Telegram-bot"].detail


def test_probe_telegram_live_uses_saved_store(tmp_path, monkeypatch):
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    import secret_probes

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok":true,"result":{"username":"azom_bot"}}'

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:FAKE_TOKEN")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_ACTOR_MAP", raising=False)
    monkeypatch.setattr(secret_probes, "urlopen", lambda *_a, **_kw: _FakeResp())

    r_empty = secret_probes.probe_telegram()
    assert r_empty.status == "error"

    save_telegram_access_store({"jonatan": "123456789"})
    r_saved = secret_probes.probe_telegram()
    assert r_saved.status == "ok"
    assert r_saved.message == "getMe OK"
