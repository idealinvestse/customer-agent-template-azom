"""Helpers for OpenAI-compatible streamed chat-completion responses."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any


def _decode_line(line: str | bytes) -> str:
    if isinstance(line, bytes):
        return line.decode("utf-8", errors="replace")
    return str(line)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(part.get("text") or "")
            for part in value
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        )
    return ""


def read_chat_completion_sse(
    lines: Iterable[str | bytes],
    *,
    on_text_delta: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Read chat-completion SSE, returning full text and usage when present."""
    parts: list[str] = []
    plain_lines: list[str] = []
    usage: dict[str, Any] = {}
    for line_value in lines:
        line = _decode_line(line_value).strip()
        if not line:
            continue
        if not line.startswith("data:"):
            plain_lines.append(line)
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_usage = event.get("usage")
        if isinstance(event_usage, dict):
            usage = event_usage
        choices = event.get("choices") or []
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}
        text = _content_text(delta.get("content")) if isinstance(delta, dict) else ""
        if not text:
            message = choice.get("message") or {}
            text = _content_text(message.get("content")) if isinstance(message, dict) else ""
        if text:
            parts.append(text)
            if on_text_delta:
                try:
                    on_text_delta(text)
                except Exception:
                    # A transport callback must not fail model inference.
                    pass

    if parts:
        return "".join(parts).strip(), usage

    # Some compatible endpoints/proxies may ignore stream=true and return JSON.
    if plain_lines:
        try:
            data = json.loads("\n".join(plain_lines))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            event_usage = data.get("usage")
            if isinstance(event_usage, dict):
                usage = event_usage
            choices = data.get("choices") or []
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                text = _content_text(message.get("content")) if isinstance(message, dict) else ""
                if text and on_text_delta:
                    try:
                        on_text_delta(text)
                    except Exception:
                        pass
                return text.strip(), usage
    return "", usage
