"""Tests for Telegram inbound annotated-screenshot intake and multimodal vision routing."""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import responses as rsps_mod

from ecom_ops.bot import __main__ as bot_main
from ecom_ops.bot.chat_agent import WP_CODE_INTENT_RE, run_chat
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.store import ConversationStore
from ecom_ops.bot.telegram_transport import (
    TelegramApiError,
    download_file_bytes,
    get_file_path,
)
from ecom_ops.model_router.base import (
    ModelRequest,
    ModelResult,
    RouteCapabilities,
    detect_image_format,
    format_chat_completions_messages,
)
from ecom_ops.model_router.errors import ModelConfigError
from ecom_ops.model_router.openai_chatgpt import build_responses_payload
from ecom_ops.model_router.openrouter import OPENROUTER_URL, OpenRouterRoute
from ecom_ops.model_router.router import ModelRouter
from ecom_ops.model_router.xai_api_key import XAI_CHAT_URL, XaiApiKeyRoute
from ecom_ops.telemetry import Telemetry

# Minimal valid magic-byte headers (>= 12 bytes) for PNG, JPEG, and WEBP.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
_TINY_WEBP = b"RIFF\x14\x00\x00\x00WEBPVP8 \x08\x00\x00\x00\x00\x00\x00\x00"


class _FakeHttpResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _write_png(path: Path, data: bytes = _TINY_PNG) -> Path:
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# 1. Telegram transport: get_file_path & download_file_bytes security checks
# ---------------------------------------------------------------------------


def test_get_file_path_and_download_valid_images(monkeypatch):
    def fake_post(token: str, method: str, **params: object) -> dict[str, Any]:
        assert token == "12345:SECRET_BOT_TOKEN"
        assert method == "getFile"
        assert params["file_id"] == "AgACAgIAAxkBA"
        return {"ok": True, "result": {"file_path": "photos/file_42.png", "file_size": len(_TINY_PNG)}}

    monkeypatch.setattr("ecom_ops.bot.telegram_transport.telegram_api_post", fake_post)
    path = get_file_path("12345:SECRET_BOT_TOKEN", "AgACAgIAAxkBA")
    assert path == "photos/file_42.png"

    for sample, expected_mime in (
        (_TINY_PNG, "image/png"),
        (_TINY_JPEG, "image/jpeg"),
        (_TINY_WEBP, "image/webp"),
    ):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=20.0, _s=sample: _FakeHttpResponse(_s, {"Content-Length": str(len(_s))}),
        )
        raw = download_file_bytes("12345:SECRET_BOT_TOKEN", path)
        assert raw == sample
        detected = detect_image_format(raw)
        assert detected is not None
        assert detected[0] == expected_mime


def test_download_rejects_traversal_invalid_magic_oversize_and_never_leaks_token(monkeypatch):
    called = False

    def boom(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be called for invalid file_path")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    for bad_path in (
        "../etc/passwd",
        "photos/../../secret.txt",
        "/etc/passwd",
        "photos/%2e%2e/secret.png",
        "photos/%252e%252e/secret.png",
        "photos\\..\\evil.png",
        "https://evil.example/x.png",
        "photos/file.png?foo=bar",
    ):
        with pytest.raises(ValueError):
            download_file_bytes("99999:TOPSECRET_TOKEN", bad_path)
    assert called is False

    # Invalid magic bytes rejected
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=20.0: _FakeHttpResponse(b"<html>not an image</html>"),
    )
    with pytest.raises(ValueError, match="Unsupported image format"):
        download_file_bytes("99999:TOPSECRET_TOKEN", "photos/fake.png")

    # Oversize rejected (both Content-Length and streaming cap)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=20.0: _FakeHttpResponse(_TINY_PNG + (b"\x00" * 500)),
    )
    with pytest.raises(ValueError, match="max_bytes"):
        download_file_bytes("99999:TOPSECRET_TOKEN", "photos/big.png", max_bytes=64)

    # HTTP error never leaks bot token in exception or __cause__
    def fake_http_err(_req, timeout=20.0):
        del timeout
        raise urllib.error.HTTPError(
            url="https://api.telegram.org/file/bot99999:TOPSECRET_TOKEN/photos/missing.png",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b'{"ok":false,"error_code":404,"description":"Not Found"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_http_err)
    with pytest.raises(TelegramApiError) as exc_info:
        download_file_bytes("99999:TOPSECRET_TOKEN", "photos/missing.png")
    assert "TOPSECRET_TOKEN" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


# ---------------------------------------------------------------------------
# 2. Inbound Telegram photo/document extraction, saving, cleanup & job payload
# ---------------------------------------------------------------------------


def test_extract_photo_picks_highest_res_within_limit_and_document_mime():
    msg_photo = {
        "photo": [
            {"file_id": "small", "width": 90, "height": 90, "file_size": 1_000},
            {"file_id": "best_ok", "width": 1280, "height": 720, "file_size": 400_000},
            {"file_id": "too_big", "width": 4000, "height": 3000, "file_size": 6_000_000},
        ],
        "caption": "Ändra den inringade rubriken",
    }
    fid, caption, err = bot_main.extract_inbound_image_candidate(msg_photo)
    assert fid == "best_ok"
    assert caption == "Ändra den inringade rubriken"
    assert err is None

    # All photos too big -> error_reason == "too_large"
    msg_huge = {
        "photo": [{"file_id": "huge", "width": 4000, "height": 3000, "file_size": 7_000_000}]
    }
    assert bot_main.extract_inbound_image_candidate(msg_huge) == (None, "", "too_large")

    # Document with allowed image MIME accepted; non-image MIME ignored
    msg_doc = {
        "document": {"file_id": "doc_webp", "mime_type": "image/webp", "file_size": 2048},
        "caption": "Fixa detta",
    }
    assert bot_main.extract_inbound_image_candidate(msg_doc) == ("doc_webp", "Fixa detta", None)
    msg_pdf = {
        "document": {"file_id": "doc_pdf", "mime_type": "application/pdf", "file_size": 2048},
    }
    assert bot_main.extract_inbound_image_candidate(msg_pdf) == (None, "", None)


def test_save_inbound_image_and_cleanup_old_files(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    old_dir = tmp_path / "telegram_inbox_media" / "42"
    old_dir.mkdir(parents=True)
    stale = old_dir / "stale.png"
    stale.write_bytes(_TINY_PNG)
    old_ts = time.time() - (25 * 3600)
    os.utime(stale, (old_ts, old_ts))

    saved = bot_main.save_inbound_telegram_image("42", _TINY_PNG, data_dir=tmp_path)
    assert saved.is_file()
    assert saved.suffix == ".png"
    assert saved.parent == old_dir
    assert not stale.exists()


def test_job_store_and_process_job_pass_image_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    img_path = _write_png(tmp_path / "shot.png")

    job_store = TelegramJobStore(tmp_path / "jobs.sqlite3")
    update = {
        "update_id": 101,
        "message": {
            "message_id": 77,
            "chat": {"id": 42},
            "photo": [{"file_id": "f1", "width": 800, "height": 600, "file_size": 123}],
            "caption": "Ändra det inringade på produktsidan",
        },
    }
    accepted = job_store.accept_update(update, image_paths=[str(img_path)])
    assert accepted["disposition"] == "queued"
    job = job_store.claim_next()
    assert job is not None
    assert job["payload"]["text"] == "Ändra det inringade på produktsidan"
    assert job["payload"]["image_paths"] == [str(img_path)]

    captured: dict[str, Any] = {}

    class _FakeHandler:
        def handle(self, chat_id, text, *, image_paths=None, **_kwargs):
            captured["chat_id"] = chat_id
            captured["text"] = text
            captured["image_paths"] = image_paths
            return bot_main.BotReply(text="Klart i sandlådan")

    monkeypatch.setattr(bot_main, "_api", lambda _t, _m, **_p: {"ok": True, "result": {"message_id": 99}})
    bot_main._process_job("tok", _FakeHandler(), job_store, job)  # type: ignore[arg-type]
    assert captured["chat_id"] == "42"
    assert captured["text"] == "Ändra det inringade på produktsidan"
    assert captured["image_paths"] == [str(img_path)]


# ---------------------------------------------------------------------------
# 3. Two-message Telegram flow (photo without caption -> next text message)
#    AND single-message photo + caption flow, with NO base64 in store/logs
# ---------------------------------------------------------------------------


def test_two_message_photo_then_text_handoff_and_single_message_caption(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    img_path = _write_png(tmp_path / "circle.png")

    conv_store = ConversationStore(path=tmp_path / "telegram_state.json")
    handler = BotHandler(store=conv_store)

    seen_calls: list[dict[str, Any]] = []

    def fake_run_chat(message: str, **kwargs: Any):
        seen_calls.append({"message": message, "image_paths": kwargs.get("image_paths")})
        from ecom_ops.bot.chat_agent import ChatResult

        return ChatResult(
            text="Fixat i sandlådan",
            messages=[
                {"role": "user", "content": f"[Bifogad skärmdump: {img_path.name}] {message}"},
                {"role": "assistant", "content": "Fixat i sandlådan"},
            ],
        )

    monkeypatch.setattr("ecom_ops.bot.handlers.run_chat", fake_run_chat)

    # Turn 1: Photo WITHOUT caption -> stored in pending_image_paths, brief Swedish ack
    r1 = handler.handle(42, "", image_paths=[str(img_path)])
    assert "Bild mottagen!" in r1.text
    assert "inringat" in r1.text
    assert seen_calls == []
    state_after_photo = conv_store.get(42) or {}
    assert state_after_photo["session"]["pending_image_paths"] == [str(img_path.resolve())]

    # Turn 2: Next text message automatically attaches pending image and clears it!
    r2 = handler.handle(42, "Ändra den inringade knappen till Köp nu")
    assert r2.text == "Fixat i sandlådan"
    assert len(seen_calls) == 1
    assert seen_calls[0]["image_paths"] == [str(img_path.resolve())]
    state_after_text = conv_store.get(42) or {}
    assert "pending_image_paths" not in (state_after_text.get("session") or {})

    # Turn 3: Subsequent text message does NOT re-attach consumed image
    handler.handle(42, "Tack!")
    assert len(seen_calls) == 2
    assert seen_calls[1]["image_paths"] is None

    # Turn 4: Photo + caption in one message goes straight through
    r4 = handler.handle(42, "Byt rubriken här", image_paths=[str(img_path)])
    assert r4.text == "Fixat i sandlådan"
    assert seen_calls[2]["image_paths"] == [str(img_path.resolve())]


# ---------------------------------------------------------------------------
# 4. Multimodal LLM payload formatting: Responses API, OpenRouter, xAI, Router
# ---------------------------------------------------------------------------


def test_openai_responses_payload_formats_input_image(tmp_path: Path):
    img = _write_png(tmp_path / "annotated.png")
    req = ModelRequest(
        messages=[
            {"role": "system", "content": "Du är AzomOps."},
            {"role": "user", "content": "Ändra det inringade", "images": [str(img)]},
        ],
        max_output_tokens=512,
    )
    payload = build_responses_payload(req, "gpt-5.2")
    assert payload["instructions"] == "Du är AzomOps."
    assert len(payload["input"]) == 1
    user_item = payload["input"][0]
    assert user_item["role"] == "user"
    assert isinstance(user_item["content"], list)
    assert user_item["content"][0] == {"type": "input_text", "text": "Ändra det inringade"}
    assert user_item["content"][1]["type"] == "input_image"
    expected_b64 = base64.b64encode(_TINY_PNG).decode("ascii")
    assert user_item["content"][1]["image_url"] == f"data:image/png;base64,{expected_b64}"


def test_openrouter_and_xai_format_image_url_parts(tmp_path: Path, monkeypatch):
    img = _write_png(tmp_path / "prod.jpg", _TINY_JPEG)
    req = ModelRequest(
        messages=[
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Se bilden", "images": [str(img)]},
        ],
        max_output_tokens=200,
    )
    formatted = format_chat_completions_messages(req.messages)
    assert formatted[0] == {"role": "system", "content": "System"}
    assert "images" not in formatted[1]
    assert formatted[1]["content"][0] == {"type": "text", "text": "Se bilden"}
    expected_b64 = base64.b64encode(_TINY_JPEG).decode("ascii")
    assert formatted[1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{expected_b64}"},
    }

    # Verify OpenRouter HTTP payload
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    tel = Telemetry(path=tmp_path / "tel.jsonl")

    with rsps_mod.RequestsMock() as rsps:
        rsps.add(
            rsps_mod.POST,
            OPENROUTER_URL,
            json={"choices": [{"message": {"content": "OR ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )
        res_or = OpenRouterRoute().complete(req, telemetry=tel)
        assert res_or.content == "OR ok"
        sent_or = json.loads(rsps.calls[0].request.body)
        assert sent_or["messages"][1]["content"][1]["type"] == "image_url"
        assert sent_or["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    with rsps_mod.RequestsMock() as rsps:
        rsps.add(
            rsps_mod.POST,
            XAI_CHAT_URL,
            json={"choices": [{"message": {"content": "XAI ok"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )
        res_xai = XaiApiKeyRoute().complete(req, telemetry=tel)
        assert res_xai.content == "XAI ok"
        sent_xai = json.loads(rsps.calls[0].request.body)
        assert sent_xai["messages"][1]["content"][1]["type"] == "image_url"
        assert sent_xai["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    # Telemetry must NEVER contain base64 image dumps
    tel_text = (tmp_path / "tel.jsonl").read_text(encoding="utf-8")
    assert expected_b64 not in tel_text
    assert "data:image/" not in tel_text


def test_router_skips_non_vision_grok_build_when_images_present(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_MODEL_ROUTE_ORDER", "xai_grok_build,chatgpt_oauth")
    img = _write_png(tmp_path / "v.png")
    req = ModelRequest(messages=[{"role": "user", "content": "Kolla", "images": [str(img)]}])

    class _NoVision:
        provider = "xai"
        route_id = "xai_grok_build"
        capabilities = RouteCapabilities(supports_vision=False)
        calls = 0

        def is_configured(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"route_id": self.route_id}

        def complete(self, request, *, telemetry=None):
            self.calls += 1
            raise AssertionError("Non-vision route must be skipped for image requests")

    class _VisionOk:
        provider = "openai"
        route_id = "chatgpt_oauth"
        capabilities = RouteCapabilities(supports_vision=True)
        calls = 0

        def is_configured(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"route_id": self.route_id}

        def complete(self, request, *, telemetry=None):
            self.calls += 1
            return ModelResult(content="Såg cirkeln", provider="openai", route_id="chatgpt_oauth", model="gpt-5.2")

    router = ModelRouter()
    router.grok_build = _NoVision()  # type: ignore[assignment]
    router.chatgpt = _VisionOk()  # type: ignore[assignment]

    res = router.complete(req, telemetry=Telemetry(path=tmp_path / "tel.jsonl"))
    assert res.content == "Såg cirkeln"
    assert router.grok_build.calls == 0
    assert router.chatgpt.calls == 1

    # Explicitly forcing a non-vision route fails honestly with vision_unsupported
    with pytest.raises(ModelConfigError) as exc_info:
        router.complete_route("xai_grok_build", req)
    assert exc_info.value.code == "vision_unsupported"


# ---------------------------------------------------------------------------
# 5. Vision + WP/product routing in chat_agent.py & zero base64 in store/logs
# ---------------------------------------------------------------------------


def test_wp_code_intent_matches_swedish_product_and_annotation_terms():
    for phrase in (
        "Ändra produktsidan för denna vara",
        "Redigera produkt i sandlådan",
        "Byt text på köpknappen",
        "Ändra det som är inringat i bifogad bild",
        "Kolla det inringade och uppdatera produktsida",
    ):
        assert WP_CODE_INTENT_RE.search(phrase), f"Expected WP_CODE_INTENT_RE match for: {phrase}"


def test_vision_routes_directly_into_wp_file_loop_and_keeps_store_text_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    img = _write_png(tmp_path / "circled_product.png")
    b64_sample = base64.b64encode(_TINY_PNG).decode("ascii")

    class _RouterStub:
        def any_configured(self) -> bool:
            return True

        def has_route_outside_openrouter_budget(self) -> bool:
            return True

        def preferred_route_id(self) -> str:
            return "chatgpt_oauth"

    monkeypatch.setattr("ecom_ops.bot.chat_agent.get_model_router", lambda: _RouterStub())
    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.catalog_read_turn",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("catalog_read_turn must be bypassed")),
    )
    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.catalog_write_turn",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("catalog_write_turn must be bypassed")),
    )

    llm_calls: list[dict[str, Any]] = []

    def fake_chat_completion(messages, **kwargs):
        llm_calls.append({"messages": list(messages), "profile_id": kwargs.get("profile_id")})
        return '<tool_call>{"name":"wp_list_sites","arguments":{}}</tool_call>', 0.0

    monkeypatch.setattr("ecom_ops.bot.chat_agent.chat_completion", fake_chat_completion)

    file_loop_called: dict[str, Any] = {}

    def fake_file_loop(initial_content, messages, **kwargs):
        file_loop_called["initial_content"] = initial_content
        file_loop_called["allow_wp_first"] = kwargs.get("allow_wp_first")
        file_loop_called["messages"] = messages
        return type(
            "_Loop",
            (),
            {
                "handled": True,
                "content": "Ändrat det inringade i sandlådan wr-112233445566.",
                "followup_cost_usd": 0.0,
                "calls": 2,
                "tool_seconds": 0.2,
                "site_id": "azom-no",
                "operation_ids": (),
                "model_error": False,
                "stage_plan_id": None,
                "review_stage_plan_id": "wr-112233445566",
                "attachments": (),
            },
        )()

    monkeypatch.setattr("ecom_ops.bot.chat_agent.run_bounded_file_tool_loop", fake_file_loop)

    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    result = run_chat(
        "Ändra det inringade till Testprodukt 123 i sandbox",
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:42",
        telemetry=tel,
        image_paths=[str(img)],
    )

    # Routed into wp_tool_router + chat_wp_file + run_bounded_file_tool_loop
    assert len(llm_calls) == 1
    assert llm_calls[0]["profile_id"] == "wp_tool_router"
    sent_messages = llm_calls[0]["messages"]
    assert any("vision-läge" in str(m.get("content") or "") for m in sent_messages if m.get("role") == "system")
    user_msgs = [m for m in sent_messages if m.get("role") == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["images"] == [str(img.resolve())]
    assert file_loop_called["allow_wp_first"] is True
    assert "wr-112233445566" in result.text

    # Stored conversation history is text-only with brief marker, NO images key and NO base64!
    stored_json = json.dumps(result.messages, ensure_ascii=False)
    assert "[Bifogad skärmdump: circled_product.png]" in stored_json
    assert b64_sample not in stored_json
    assert "data:image/" not in stored_json
    assert all("images" not in m for m in result.messages)

    # Telemetry has safe booleans/counts only, NO base64 or file paths
    tel_text = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")
    assert b64_sample not in tel_text
    assert str(img) not in tel_text
    assert '"has_images": true' in tel_text.lower()
