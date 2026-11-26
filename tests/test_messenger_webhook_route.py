"""Flask /webhooks/messenger integration tests."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
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
    for name in ("azom_dashboard", "settings_store", "status", "secret_probes"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


@pytest.fixture
def messenger_client(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sites.yaml").write_text("customer: azom\ndomains:\n  - se\n", encoding="utf-8")
    (cfg / "limits.yaml").write_text("openrouter_cap: 100\n", encoding="utf-8")
    (cfg / "integrations.yaml").write_text("email:\n  enabled: true\n", encoding="utf-8")
    (cfg / "rbac.yaml").write_text(
        "roles:\n  jonatan: viewer\n  oscar: full_admin\n", encoding="utf-8"
    )
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(data))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-dashboard-secret")
    monkeypatch.setenv("MESSENGER_VERIFY_TOKEN", "verify-token")
    monkeypatch.setenv("MESSENGER_APP_SECRET", "app-secret")
    monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", "PSID1")
    monkeypatch.setenv("MESSENGER_ACTOR_MAP", "PSID1:jonatan")
    monkeypatch.chdir(DASH_DIR)
    app = _load_dashboard_app()
    app.config["TESTING"] = True
    return app.test_client()


def _sign(body: bytes) -> str:
    dig = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={dig}"


def test_messenger_webhook_verify_challenge(messenger_client):
    resp = messenger_client.get(
        "/webhooks/messenger"
        "?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=9999"
    )
    assert resp.status_code == 200
    assert resp.data == b"9999"


def test_messenger_webhook_rejects_bad_signature(messenger_client):
    body = b'{"object":"page","entry":[]}'
    resp = messenger_client.post(
        "/webhooks/messenger",
        data=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert resp.status_code == 401


def test_messenger_webhook_handles_message(messenger_client):
    payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "message": {"mid": "m1", "text": "/help"},
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    resp = messenger_client.post(
        "/webhooks/messenger",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body),
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["handled"] == 1
    assert data["errors"] == 0


def test_health_includes_messenger(monkeypatch, messenger_client):
    monkeypatch.delenv("MESSENGER_PAGE_ACCESS_TOKEN", raising=False)
    resp = messenger_client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "messenger" in data
    assert data["messenger"]["configured"] is True
    assert data["messenger"]["page_token_present"] is False
    assert data["messenger"]["send_enabled"] is False
    assert "fail_closed" in data["messenger"]
    assert "allowlist_set" in data["messenger"]


def test_messenger_webhook_skips_duplicate_mid(messenger_client, tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "message": {"mid": "dup-mid", "text": "/help"},
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": _sign(body),
    }
    first = messenger_client.post("/webhooks/messenger", data=body, headers=headers)
    second = messenger_client.post("/webhooks/messenger", data=body, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["handled"] == 1
    assert second.get_json()["skipped"] == 1


def test_metrics_requires_token(messenger_client, monkeypatch):
    monkeypatch.delenv("METRICS_SCRAPE_TOKEN", raising=False)
    assert messenger_client.get("/metrics").status_code == 403
    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "secret-metrics")
    denied = messenger_client.get(
        "/metrics",
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
    )
    assert denied.status_code == 403
    allowed = messenger_client.get(
        "/metrics",
        headers={"Authorization": "Bearer secret-metrics"},
        environ_base={"REMOTE_ADDR": "8.8.8.8"},
    )
    assert allowed.status_code == 200


def test_messenger_webhook_wires_proposal_store_quick_reply_and_inbound_images(
    messenger_client, tmp_path, monkeypatch
):
    import webhooks
    from ecom_ops.bot import handlers as handlers_mod
    from ecom_ops.bot import messenger_adapter
    from ecom_ops.bot.reply import BotReply

    data_dir = tmp_path / "data"
    monkeypatch.setenv("AZOM_DATA_DIR", str(data_dir))

    # Shared SQLite proposal_store is cached per resolved path
    ps1 = webhooks._get_messenger_proposal_store(data_dir)
    ps2 = webhooks._get_messenger_proposal_store(data_dir)
    assert ps1 is ps2
    assert (data_dir / "messenger_proposals.sqlite3").is_file()

    # Spy on BotHandler construction + handle() + handle_callback()
    seen_proposal_stores: list[object] = []
    seen_handles: list[tuple[str, str, list[str] | None]] = []
    seen_callbacks: list[tuple[str, str]] = []

    class _SpyHandler:
        def __init__(self, *, store, proposal_store=None, channel="messenger", **kwargs):
            self.store = store
            self.proposal_store = proposal_store
            self.channel = channel
            seen_proposal_stores.append(proposal_store)

        def handle(self, chat_id: str, text: str, *args, image_paths=None, **kwargs):
            seen_handles.append((chat_id, text, image_paths))
            return BotReply(text="ok")

        def handle_callback(self, chat_id: str, data: str):
            seen_callbacks.append((chat_id, data))
            return BotReply(text="cb-ok")

    monkeypatch.setattr(handlers_mod, "BotHandler", _SpyHandler)

    # 1. Quick Reply payload routes to handle_callback with durable proposal_store attached
    qr_payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "message": {
                            "mid": "qr-mid-1",
                            "text": "Alternativ 1",
                            "quick_reply": {"payload": "clarify:pick:0"},
                        },
                    }
                ]
            }
        ],
    }
    qr_body = json.dumps(qr_payload).encode("utf-8")
    qr_resp = messenger_client.post(
        "/webhooks/messenger",
        data=qr_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(qr_body)},
    )
    assert qr_resp.status_code == 200
    assert qr_resp.get_json()["handled"] == 1
    assert seen_callbacks == [("PSID1", "clarify:pick:0")]
    assert seen_proposal_stores and seen_proposal_stores[-1] is ps1

    # 2. Inbound image attachment downloads via download_messenger_image and forwards image_paths
    fake_img = data_dir / "messenger_inbox_media" / "PSID1" / "abc123.png"
    fake_img.parent.mkdir(parents=True, exist_ok=True)
    fake_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    downloaded_urls: list[str] = []

    def _fake_download(url: str, *, peer_id: str, data_dir: Path | None = None, **kwargs):
        downloaded_urls.append(url)
        return fake_img

    monkeypatch.setattr(messenger_adapter, "download_messenger_image", _fake_download)

    img_payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "message": {
                            "mid": "img-mid-1",
                            "text": "Ändra denna knapp",
                            "attachments": [
                                {
                                    "type": "image",
                                    "payload": {"url": "https://cdn.example.com/button.png"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    img_body = json.dumps(img_payload).encode("utf-8")
    img_resp = messenger_client.post(
        "/webhooks/messenger",
        data=img_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(img_body)},
    )
    assert img_resp.status_code == 200
    assert img_resp.get_json()["handled"] == 1
    assert downloaded_urls == ["https://cdn.example.com/button.png"]
    assert seen_handles == [("PSID1", "Ändra denna knapp", [str(fake_img)])]

    # 3. Pre-auth guard: unallowlisted or unmapped PSID NEVER triggers image download or disk write
    downloaded_urls.clear()
    seen_handles.clear()
    unauth_payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "999999_UNALLOWLISTED"},
                        "message": {
                            "mid": "unauth-img-1",
                            "text": "Hämta min bild",
                            "attachments": [
                                {
                                    "type": "image",
                                    "payload": {"url": "https://cdn.example.com/evil.png"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    unauth_body = json.dumps(unauth_payload).encode("utf-8")
    unauth_resp = messenger_client.post(
        "/webhooks/messenger",
        data=unauth_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(unauth_body)},
    )
    assert unauth_resp.status_code == 200
    assert downloaded_urls == []
    assert not (data_dir / "messenger_inbox_media" / "999999_UNALLOWLISTED").exists()

    # Unmapped PSID (allowlisted but missing from MESSENGER_ACTOR_MAP) also blocked from downloading
    monkeypatch.setenv("MESSENGER_ALLOWED_PSIDS", "PSID1,PSID_UNMAPPED")
    unmapped_payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID_UNMAPPED"},
                        "message": {
                            "mid": "unmapped-img-1",
                            "text": "Bild",
                            "attachments": [
                                {
                                    "type": "image",
                                    "payload": {"url": "https://cdn.example.com/unmapped.png"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    unmapped_body = json.dumps(unmapped_payload).encode("utf-8")
    assert (
        messenger_client.post(
            "/webhooks/messenger",
            data=unmapped_body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(unmapped_body)},
        ).status_code
        == 200
    )
    assert downloaded_urls == []

    # 4. Text-only turn queued ahead of image turn does NOT steal the image turn's URLs
    downloaded_urls.clear()
    seen_handles.clear()
    two_turn_payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "message": {"mid": "text-first-1", "text": "Hej utan bild"},
                    },
                    {
                        "sender": {"id": "PSID1"},
                        "message": {
                            "mid": "img-second-2",
                            "text": "Här är bilden",
                            "attachments": [
                                {
                                    "type": "image",
                                    "payload": {"url": "https://cdn.example.com/turn2.png"},
                                }
                            ],
                        },
                    },
                ]
            }
        ],
    }
    two_turn_body = json.dumps(two_turn_payload).encode("utf-8")
    two_turn_resp = messenger_client.post(
        "/webhooks/messenger",
        data=two_turn_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(two_turn_body)},
    )
    assert two_turn_resp.status_code == 200
    assert downloaded_urls == ["https://cdn.example.com/turn2.png"]
    assert seen_handles == [
        ("PSID1", "Hej utan bild", None),
        ("PSID1", "Här är bilden", [str(fake_img)]),
    ]
