"""Common model-route contract."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ecom_ops.telemetry import Telemetry

MAX_MULTIMODAL_IMAGE_BYTES = 5_242_880


def detect_image_format(data: bytes) -> tuple[str, str] | None:
    """Detect PNG, JPEG, or WEBP strictly from file magic bytes.

    Returns ``(mime_type, file_extension)`` or ``None`` if unsupported.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def encode_image_data_url(
    image_path: str | Path,
    *,
    max_bytes: int = MAX_MULTIMODAL_IMAGE_BYTES,
) -> str:
    """Read and validate a local image file and return a ``data:<mime>;base64,...`` URI.

    Never includes file bytes or base64 content in raised errors.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    raw_path = str(image_path or "").strip()
    if not raw_path or "\x00" in raw_path:
        raise ValueError("Invalid image path")
    path = Path(raw_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Image file does not exist or is not a regular file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("Unable to stat image file") from exc
    if size <= 0 or size > max_bytes:
        raise ValueError("Image file is empty or exceeds size limit")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError("Unable to read image file") from exc
    if len(data) <= 0 or len(data) > max_bytes:
        raise ValueError("Image file is empty or exceeds size limit")
    fmt = detect_image_format(data)
    if fmt is None:
        raise ValueError("Unsupported image format; only PNG, JPEG, and WEBP are allowed")
    mime, _ext = fmt
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def request_has_images(messages: list[dict[str, Any]] | None) -> bool:
    """Return True if any message carries local image paths or multimodal image parts."""
    for msg in messages or ():
        if not isinstance(msg, dict):
            continue
        images = msg.get("images")
        if isinstance(images, (list, tuple)) and any(str(p or "").strip() for p in images):
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
                    return True
    return False


def format_chat_completions_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize messages for OpenAI Chat Completions-compatible providers.

    Plain text messages stay ``{"role": ..., "content": "..."}``. Messages with
    ``images`` (validated local file paths) or multimodal ``content`` lists are
    converted to ``[{"type": "text", ...}, {"type": "image_url", "image_url": {"url": ...}}]``.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        raw_images = msg.get("images")
        image_list = (
            [str(p).strip() for p in raw_images if isinstance(p, (str, Path)) and str(p).strip()]
            if isinstance(raw_images, (list, tuple))
            else []
        )
        if not image_list and not isinstance(content, list):
            out.append({"role": role, "content": str(content or "")})
            continue

        parts: list[dict[str, Any]] = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = str(part.get("type") or "")
                if ptype in {"text", "input_text"}:
                    parts.append({"type": "text", "text": str(part.get("text") or "")})
                elif ptype == "image_url":
                    img = part.get("image_url")
                    if isinstance(img, dict) and img.get("url"):
                        parts.append({"type": "image_url", "image_url": {"url": str(img["url"])}})
                    elif isinstance(img, str) and img:
                        parts.append({"type": "image_url", "image_url": {"url": img}})
                elif ptype == "input_image" and part.get("image_url"):
                    parts.append(
                        {"type": "image_url", "image_url": {"url": str(part["image_url"])}}
                    )
        else:
            text_val = str(content or "")
            if text_val or not image_list:
                parts.append({"type": "text", "text": text_val})

        for img_path in image_list:
            data_url = encode_image_data_url(img_path)
            parts.append({"type": "image_url", "image_url": {"url": data_url}})

        out.append({"role": role, "content": parts})
    return out


@dataclass(frozen=True)
class RouteCapabilities:
    supports_temperature: bool = True
    supports_reasoning_effort: bool = False
    supports_json_schema: bool = False
    supports_streaming: bool = True
    supports_vision: bool = False
    cost_kind: str = "estimated"
    subject_to_openrouter_cap: bool = False


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, Any]]
    model: str | None = None
    max_output_tokens: int = 400
    temperature: float = 0.3
    timeout: int = 45
    site: str = "azom"
    case_id: str | None = None
    actor: str | None = None
    on_text_delta: Callable[[str], None] | None = None
    on_text_reset: Callable[[], None] | None = None
    on_phase_change: Callable[[dict[str, Any]], None] | None = None
    reasoning_effort: str | None = None
    hard_timeout: int | None = None
    profile_id: str | None = None
    slot_id: str | None = None
    response_format: dict[str, Any] | None = None
    provider_params: dict[str, Any] | None = None


def emit_phase(request: ModelRequest, info: dict[str, Any]) -> None:
    """Safely notify ``request.on_phase_change`` without ever breaking model execution."""
    callback = request.on_phase_change
    if callback is None:
        return
    try:
        callback(dict(info))
    except Exception:
        pass


@dataclass(frozen=True)
class ModelResult:
    content: str
    provider: str
    route_id: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    usage_known: bool = False
    raw_usage: dict[str, Any] | None = None
    profile_id: str | None = None
    slot_id: str | None = None
    fallback_used: bool = False
    primary_route: str = ""
    primary_error_class: str = ""
    primary_error_code: str = ""
    attempts_count: int = 1
    latency_ms: int = 0
    ttft_ms: int | None = None
    estimated_external_cost_usd: float = 0.0


class ModelRoute(Protocol):
    provider: str
    route_id: str
    capabilities: RouteCapabilities

    def is_configured(self) -> bool: ...

    def status(self) -> dict[str, Any]: ...

    def complete(
        self,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult: ...
