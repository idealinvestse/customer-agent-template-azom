"""Tests for Messenger Quick Replies, inbound/outbound images, and WP draft-push markup."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ecom_ops.bot.change_clarifier import ClarificationOption, ClarificationProposal
from ecom_ops.bot.messenger_adapter import (
    InboundEvent,
    actions_to_messenger_buttons,
    actions_to_messenger_quick_replies,
    download_messenger_image,
    parse_webhook_payload,
    process_inbound,
    reply_to_messenger_messages,
    send_bot_reply,
    send_messenger_message,
    send_sender_action,
)
from ecom_ops.bot.reply import (
    ActionButton,
    ActionMarkup,
    BotReply,
    bound_action_markup,
    clarification_actions,
    order_status_confirm_actions,
    safe_workspace_attachments,
    wp_push_draft_actions,
    wp_review_actions,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16


class _FakeResponse:
    def __init__(self, body: bytes, *, final_url: str = "", headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(body)
        self._final_url = final_url
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def test_clarify_six_options_become_messenger_quick_replies() -> None:
    prop = ClarificationProposal(
        understanding="Förtydliga knappändringen",
        question="Vilket spår gäller?",
        options=(
            ClarificationOption(id="css", label="CSS-justering", summary="Ändra CSS", recommended=True),
            ClarificationOption(id="tpl", label="Temamall", summary="Ändra mall"),
            ClarificationOption(id="blk", label="Blocklayout", summary="Ändra block"),
            ClarificationOption(id="insp", label="Inspektera först", summary="Läs först"),
        ),
    )
    cid = "cl-0123456789ab"
    actions = clarification_actions(cid, prop)

    qrs = actions_to_messenger_quick_replies(actions)
    assert len(qrs) == 6
    assert all(item["content_type"] == "text" for item in qrs)
    assert all(len(item["title"]) <= 20 for item in qrs)
    payloads = [item["payload"] for item in qrs]
    assert payloads == [
        f"clarify:pick:{cid}:css",
        f"clarify:pick:{cid}:tpl",
        f"clarify:pick:{cid}:blk",
        f"clarify:pick:{cid}:insp",
        f"clarify:other:{cid}",
        f"clarify:cancel:{cid}",
    ]

    msgs = reply_to_messenger_messages(BotReply(text="Välj alternativ:", actions=actions))
    assert len(msgs) == 1
    assert "quick_replies" in msgs[0]
    assert len(msgs[0]["quick_replies"]) == 6
    assert "attachment" not in msgs[0]

    # Legacy 1-3 button non-clarify actions continue to use button template
    simple = ActionMarkup(
        rows=[[ActionButton("Godkänn", payload="cases:approve:12345678"), ActionButton("Visa", payload="cases:show:12345678")]]
    )
    assert len(actions_to_messenger_buttons(simple)) == 2
    simple_msgs = reply_to_messenger_messages(BotReply(text="Ärende", actions=simple))
    assert "attachment" in simple_msgs[-1]


def test_quick_reply_limit_and_url_footer(monkeypatch) -> None:
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://ops.example.com")
    rows = [[ActionButton(f"Val {i}", payload=f"opt:{i}")] for i in range(1, 16)]
    rows.append([ActionButton("Öppna", url="https://ops.example.com/cases")])
    actions = ActionMarkup(rows=rows)
    qrs = actions_to_messenger_quick_replies(actions)
    assert len(qrs) == 13
    msgs = reply_to_messenger_messages(BotReply(text="Lista", actions=actions))
    assert "quick_replies" in msgs[-1]
    assert "https://ops.example.com/cases" in msgs[-1]["text"]


def test_parse_quick_reply_and_inbound_images_and_process_inbound() -> None:
    payload = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "PSID_A"},
                        "message": {
                            "mid": "m-qr-1",
                            "text": "CSS-justering",
                            "quick_reply": {"payload": "clarify:pick:cl-0123456789ab:css"},
                        },
                    },
                    {
                        "sender": {"id": "PSID_A"},
                        "message": {
                            "mid": "m-img-1",
                            "attachments": [
                                {"type": "image", "payload": {"url": "https://cdn.example.com/1.png"}},
                                {"type": "image", "payload": {"url": "http://insecure.example.com/2.png"}},
                                {"type": "file", "payload": {"url": "https://cdn.example.com/doc.pdf"}},
                            ],
                        },
                    },
                ]
            }
        ],
    }
    events = parse_webhook_payload(payload)
    assert len(events) == 2

    qr_ev = events[0]
    assert qr_ev.kind == "callback"
    assert qr_ev.payload == "clarify:pick:cl-0123456789ab:css"
    assert qr_ev.postback == "clarify:pick:cl-0123456789ab:css"

    img_ev = events[1]
    assert img_ev.kind == "message"
    assert img_ev.text is None
    assert img_ev.get("image_urls") == ("https://cdn.example.com/1.png",)
    assert img_ev["image_urls"] == ("https://cdn.example.com/1.png",)

    calls: list[tuple[str, Any]] = []

    class _FakeHandler:
        def handle_callback(self, peer_id: str, data: str) -> BotReply:
            calls.append(("callback", (peer_id, data)))
            return BotReply(text="cb ok")

        def handle(self, peer_id: str, text: str, *, image_paths: list[str] | None = None) -> BotReply:
            calls.append(("handle", (peer_id, text, image_paths)))
            return BotReply(text="msg ok")

    handler = _FakeHandler()
    assert process_inbound(qr_ev, handler).text == "cb ok"
    img_ev_with_path = InboundEvent(
        peer_id="PSID_A",
        text="Se bilden",
        image_paths=("/tmp/img.png",),
    )
    assert process_inbound(img_ev_with_path, handler).text == "msg ok"
    assert calls[0] == ("callback", ("PSID_A", "clarify:pick:cl-0123456789ab:css"))
    assert calls[1] == ("handle", ("PSID_A", "Se bilden", ["/tmp/img.png"]))


def test_download_messenger_image_antissrf_and_persistence(tmp_path: Path, monkeypatch) -> None:
    import ecom_ops.bot.messenger_adapter as ma

    called_urls: list[str] = []

    def _boom_open(*_args: object, **_kwargs: object):
        raise AssertionError("Network must not be called for blocked URLs")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_kw: type("_O", (), {"open": staticmethod(_boom_open)})())

    # Reject non-HTTPS, private/loopback/link-local IPs, userinfo, backslashes, non-443 ports
    for bad in (
        "http://cdn.example.com/a.png",
        "https://127.0.0.1/a.png",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/a.png",
        "https://localhost/a.png",
        "https://user:pass@cdn.example.com/a.png",
        "https://cdn.example.com:8443/a.png",
        "https://cdn.example.com/\\evil.png",
    ):
        assert download_messenger_image(bad, peer_id="PSID1", data_dir=tmp_path) is None

    # DNS resolving to loopback must be rejected
    monkeypatch.setattr(
        ma.socket,
        "getaddrinfo",
        lambda host, port, **_kw: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    assert download_messenger_image("https://rebind.example.com/a.png", peer_id="PSID1", data_dir=tmp_path) is None

    # Valid public DNS + non-image payload -> rejected
    monkeypatch.setattr(
        ma.socket,
        "getaddrinfo",
        lambda host, port, **_kw: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    class _FakeOpener:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def open(self, req: urllib.request.Request, timeout: float = 15) -> _FakeResponse:
            called_urls.append(req.full_url)
            return _FakeResponse(self._body, final_url=req.full_url, headers={"Content-Length": str(len(self._body))})

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_kw: _FakeOpener(b"<html>not png</html>"))
    assert download_messenger_image("https://lookaside.fbsbx.com/img1", peer_id="PSID1", data_dir=tmp_path) is None

    # Oversized payload -> rejected
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_kw: _FakeOpener(_PNG_BYTES + b"x" * 200))
    assert (
        download_messenger_image(
            "https://lookaside.fbsbx.com/img2",
            peer_id="PSID1",
            data_dir=tmp_path,
            max_bytes=50,
        )
        is None
    )

    # Valid PNG -> saved under messenger_inbox_media/<safe_peer>/<sha256[:32]>.png
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_a, **_kw: _FakeOpener(_PNG_BYTES))
    saved = download_messenger_image(
        "https://lookaside.fbsbx.com/img3",
        peer_id="../PSID:1/2",
        data_dir=tmp_path,
    )
    assert saved is not None
    expected_digest = hashlib.sha256(_PNG_BYTES).hexdigest()[:32]
    assert saved.name == f"{expected_digest}.png"
    assert saved.parent.name == "___PSID_1_2"
    assert ".." not in saved.parent.name
    assert saved.parent.parent.name == "messenger_inbox_media"
    assert saved.read_bytes() == _PNG_BYTES


def test_outbound_messenger_images_respect_gates_and_multipart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    art = tmp_path / "wp_workspaces" / "azom-no" / "wr-abcdef012345" / "artifacts"
    art.mkdir(parents=True)
    compare_png = art / "compare-desktop.png"
    compare_png.write_bytes(_PNG_BYTES)
    preview_png = art / "preview-desktop.png"
    preview_png.write_bytes(_PNG_BYTES)

    reply = BotReply(
        text="Se jämförelse:",
        attachments=(str(compare_png), str(preview_png)),
    )

    network_calls: list[tuple[str, str, bytes]] = []

    def _mock_urlopen(req: urllib.request.Request, timeout: float = 20):
        del timeout
        ctype = str(req.get_header("Content-type") or "")
        network_calls.append((req.full_url, ctype, req.data or b""))
        return _FakeResponse(json.dumps({"recipient_id": "PSID1", "message_id": "mid.1"}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _mock_urlopen)

    # 1. Mock mode -> suppresses sender_action, text message, and image upload
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_NULL_SEND", "0")
    monkeypatch.setenv("MESSENGER_SEND_ENABLED", "1")
    act_mock = send_sender_action("PSID1", "typing_on", page_token="tok", dry_run=False)
    msg_mock = send_messenger_message("PSID1", {"text": "Hej"}, page_token="tok", dry_run=False)
    res_mock = send_bot_reply("PSID1", reply, page_token="tok", dry_run=False)
    assert act_mock.get("dry_run") is True
    assert msg_mock.get("dry_run") is True
    assert all(r.get("dry_run") is True for r in res_mock)
    assert network_calls == []

    # 2. Null-send -> suppresses text and image upload
    network_calls.clear()
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_NULL_SEND", "1")
    res_null = send_bot_reply("PSID1", reply, page_token="tok", dry_run=False)
    assert all(r.get("dry_run") is True for r in res_null)
    assert network_calls == []

    # 3. MESSENGER_SEND_ENABLED=0 -> suppresses text and image upload
    monkeypatch.setenv("AZOM_NULL_SEND", "0")
    monkeypatch.setenv("MESSENGER_SEND_ENABLED", "0")
    res_dis = send_bot_reply("PSID1", reply, page_token="tok", dry_run=False)
    assert all(r.get("dry_run") is True for r in res_dis)
    assert network_calls == []

    # 4. dry_run=True preserves attachments and suppresses network
    monkeypatch.setenv("MESSENGER_SEND_ENABLED", "1")
    res_dry = send_bot_reply("PSID1", reply, page_token="tok", dry_run=True)
    assert len(res_dry) == 3  # 1 text + 2 dry-run images
    assert all(r.get("dry_run") is True for r in res_dry)
    assert network_calls == []

    # 5. Live + enabled -> multipart image uploads sent for validated attachments
    res_live = send_bot_reply("PSID1", reply, page_token="tok", dry_run=False)
    assert len(res_live) == 3
    assert len(network_calls) == 3
    img_calls = [c for c in network_calls if "multipart/form-data" in c[1]]
    assert len(img_calls) == 2
    assert b'"is_reusable": false' in img_calls[0][2]
    assert b'filename="compare-desktop.png"' in img_calls[0][2]
    assert b'filename="preview-desktop.png"' in img_calls[1][2]


def test_reply_compare_png_and_push_draft_actions(tmp_path: Path) -> None:
    art = tmp_path / "wp_workspaces" / "azom-no" / "wr-abcdef012345" / "artifacts"
    art.mkdir(parents=True)
    cmp_d = art / "compare-desktop.png"
    cmp_m = art / "compare-mobile.png"
    cmp_d.write_bytes(_PNG_BYTES)
    cmp_m.write_bytes(_PNG_BYTES)
    assert safe_workspace_attachments([str(cmp_d), str(cmp_m)], data_dir=tmp_path) == (
        str(cmp_d.resolve()),
        str(cmp_m.resolve()),
    )

    plan_id = "wr-abcdef012345"
    push_only = wp_push_draft_actions(plan_id)
    payloads = [btn.payload for row in push_only.rows for btn in row if btn.payload]
    assert payloads == [f"wpreview:push_draft:{plan_id}", f"wpreview:no:{plan_id}"]
    assert all(len(p.encode("utf-8")) <= 64 for p in payloads)

    full = wp_review_actions(
        plan_id,
        review_url="https://ops.example.com/wp/azom-no/reviews/wr-abcdef012345",
        include_stage=True,
        include_push_draft=True,
    )
    full_payloads = [btn.payload for row in full.rows for btn in row if btn.payload]
    assert full_payloads == [
        f"wpreview:stage:{plan_id}",
        f"wpreview:no:{plan_id}",
        f"wpreview:push_draft:{plan_id}",
    ]
    assert all(len(p.encode("utf-8")) <= 64 for p in full_payloads)

    # Bound action helper preserves existing order_status_confirm_actions signature when unused
    orig = order_status_confirm_actions("1001", "completed")
    assert bound_action_markup(orig) is orig
    bound = bound_action_markup(orig, bound_proposal_id="pr-112233445566")
    assert bound is not None
    assert bound.rows[0][0].payload == "proposal:yes:pr-112233445566"
    assert bound.rows[0][1].payload == "order:cancel"


def test_messenger_send_bot_reply_sends_images_before_quick_replies(
    tmp_path: Path, monkeypatch
) -> None:
    import ecom_ops.bot.messenger_adapter as ma
    from ecom_ops.bot.messenger_adapter import send_bot_reply
    from ecom_ops.bot.reply import ActionButton, ActionMarkup, BotReply

    art = tmp_path / "wp_workspaces" / "azom-no" / "wr-123456abcdef" / "artifacts"
    art.mkdir(parents=True)
    img = art / "preview-desktop.png"
    img.write_bytes(_PNG_BYTES)
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_NULL_SEND", "0")
    monkeypatch.setenv("MESSENGER_SEND_ENABLED", "1")
    monkeypatch.setattr(ma, "null_send_active", lambda: False)

    sent_payloads: list[dict[str, Any]] = []

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 20):
        del timeout
        ctype = str(req.get_header("Content-type") or "")
        body = req.data or b""
        if "multipart/form-data" in ctype:
            sent_payloads.append({"multipart": True})
        else:
            sent_payloads.append(json.loads(body.decode("utf-8")))
        return _FakeResponse(json.dumps({"recipient_id": "PSID1", "message_id": "mid.1"}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    reply = BotReply(
        text="Här är förhandsvisningen.",
        attachments=(str(img),),
        actions=ActionMarkup(
            rows=tuple((ActionButton(f"Val {i}", payload=f"opt:{i}"),) for i in range(1, 6))
        ),
    )
    res = send_bot_reply("PSID1", reply, page_token="tok", dry_run=False)
    assert len(res) >= 2
    # Quick replies must travel on the final message so image attachments don't dismiss them
    assert sent_payloads[-1].get("message", {}).get("quick_replies")
    assert any(p.get("multipart") for p in sent_payloads[:-1])
