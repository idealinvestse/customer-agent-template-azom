"""Model catalog search. Lists provider model ids. Does not call a completion."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from ecom_ops.model_router.errors import ModelConfigError, ModelRouteError

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
XAI_MODELS_URL = "https://api.x.ai/v1/models"
_CACHE_SEC = 600
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def _rows_from_payload(payload: Any) -> list[dict[str, str]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    rows: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        endpoints = item.get("endpoints")
        if isinstance(endpoints, list) and endpoints:
            if "/api/v1/chat/completions" not in endpoints and "chat/completions" not in endpoints:
                continue
        rows.append({"id": model_id, "name": str(item.get("name") or model_id)})
    return rows


def _load(provider: str) -> list[dict[str, str]]:
    cached = _cache.get(provider)
    now = time.time()
    if cached and now - cached[0] < _CACHE_SEC:
        return cached[1]
    if provider == "openai":
        from ecom_ops.model_router.openai_chatgpt import OpenAIChatGPTOAuthRoute

        route = OpenAIChatGPTOAuthRoute()
        if not route.is_callable():
            raise ModelConfigError("ChatGPT OAuth är inte ansluten")
        try:
            rows = route.list_models()
        except ModelRouteError as exc:
            raise ModelConfigError("Kunde inte hämta ChatGPT-modeller") from exc
        _cache[provider] = (now, rows)
        return rows
    headers = {"Accept": "application/json"}
    if provider == "openrouter":
        url = OPENROUTER_MODELS_URL
        key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif provider == "xai":
        url = XAI_MODELS_URL
        key = (os.environ.get("XAI_API_KEY") or "").strip()
        if not key:
            raise ModelConfigError("XAI_API_KEY saknas")
        headers["Authorization"] = f"Bearer {key}"
    else:
        raise ModelConfigError("Okänd modellkatalog")
    try:
        response = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise ModelConfigError("Modellkatalogen svarade inte") from exc
    if response.status_code >= 400:
        raise ModelConfigError(f"Modellkatalogen svarade HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelConfigError("Modellkatalogen gick inte att läsa") from exc
    rows = _rows_from_payload(payload)
    _cache[provider] = (now, rows)
    return rows


def search_models(provider: str, query: str, *, limit: int = 25) -> dict[str, Any]:
    """Return matching model ids. The query matches id or display name."""
    name = (provider or "openrouter").strip().lower()
    needle = (query or "").strip().casefold()[:80]
    if len(needle) < 2:
        return {"provider": name, "models": [], "error": "Skriv minst två tecken"}
    try:
        rows = _load(name)
    except ModelConfigError as exc:
        return {"provider": name, "models": [], "error": str(exc)}
    ranked: list[tuple[int, dict[str, str]]] = []
    for row in rows:
        model_id = row["id"].casefold()
        label = row["name"].casefold()
        if needle in model_id:
            rank = 0 if model_id.startswith(needle) else 1
        elif needle in label:
            rank = 2
        else:
            continue
        ranked.append((rank, row))
    ranked.sort(key=lambda item: (item[0], item[1]["id"]))
    return {
        "provider": name,
        "models": [row for _, row in ranked[: max(1, limit)]],
        "error": "",
    }
