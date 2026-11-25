"""ChatGPT OAuth route. Azom owns and rotates the refresh token.

Uses OpenAI Sign in with ChatGPT (authorization code + PKCE) and the
Responses API. Endpoint URLs come from the environment. Do not reuse Gmail,
Google Marketing, or Codex CLI client credentials.

Direct id_token signature checks are not done here. The token is stored and
never logged. Reconnect if refresh fails with invalid_grant.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode

import requests

from ecom_ops.model_router.base import ModelRequest, ModelResult, RouteCapabilities
from ecom_ops.model_router.base import emit_phase as _emit_phase
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelRateLimitError,
    ModelUnavailableError,
)
from ecom_ops.model_router.store import ModelSession, ModelSessionStore, atomic_write_text
from ecom_ops.telemetry import Telemetry, default_telemetry

RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_MODEL = "gpt-5.2"
_EFFORTS = frozenset({"minimal", "low", "medium", "high"})
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
DEFAULT_SCOPE = (
    "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
)
DEFAULT_RESOURCE = "https://api.openai.com/v1"
DEFAULT_AGENT_NAME = "Azom"
DEFAULT_AUTHORIZATION_URL = "https://auth.openai.com/api/accounts/authorize"
DEFAULT_TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
_REQUIRED_ENV = (
    "OPENAI_SIWC_CLIENT_ID",
    "OPENAI_SIWC_REDIRECT_URI",
    "OPENAI_SIWC_AUTHORIZATION_URL",
    "OPENAI_SIWC_TOKEN_URL",
    "OPENAI_SIWC_HOST_ID",
)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _explicit_siwc_set() -> bool:
    """True when the operator set any SIWC endpoint. Partial config does not use defaults."""
    return any(_env(name) for name in _REQUIRED_ENV)


def _default_host_id(*, create: bool = False) -> str:
    path = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data")) / "openai_siwc_host_id"
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing and len(existing) <= 128 and existing.replace("-", "").replace("_", "").isalnum():
                return existing
    except OSError:
        pass
    if not create:
        return ""
    generated = secrets.token_hex(16)
    try:
        atomic_write_text(path, generated + "\n", mode=0o600)
    except OSError:
        return generated
    return generated


def _siwc(name: str, *, create_host_id: bool = False) -> str:
    """SIWC setting. Env wins. Documented defaults apply only when none of the five are set."""
    explicit = _env(name)
    if explicit:
        return explicit
    if _explicit_siwc_set():
        return ""
    if name == "OPENAI_SIWC_CLIENT_ID":
        return DYNAMIC_CLIENT_ID
    if name == "OPENAI_SIWC_AUTHORIZATION_URL":
        return DEFAULT_AUTHORIZATION_URL
    if name == "OPENAI_SIWC_TOKEN_URL":
        return DEFAULT_TOKEN_URL
    if name == "OPENAI_SIWC_REDIRECT_URI":
        port = _env("DASHBOARD_PORT") or "8080"
        if not port.isdigit():
            port = "8080"
        return f"http://127.0.0.1:{port}/oauth/openai/callback"
    if name == "OPENAI_SIWC_HOST_ID":
        return _default_host_id(create=create_host_id)
    return ""


def _scope() -> str:
    return _env("OPENAI_SIWC_SCOPE") or DEFAULT_SCOPE


def _resource() -> str:
    return _env("OPENAI_SIWC_RESOURCE") or DEFAULT_RESOURCE


def _agent_name() -> str:
    return _env("OPENAI_SIWC_AGENT_NAME") or DEFAULT_AGENT_NAME


def _usage_ints(usage: dict[str, Any]) -> tuple[int, int, int]:
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    return prompt_tokens, completion_tokens, total_tokens


def _completed_output_text(response_obj: dict[str, Any]) -> str:
    """Text from a completed Responses object when deltas were not sent."""
    direct = response_obj.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    output = response_obj.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    return "".join(chunks).strip()


def _prefer_output_text(delta_text: str, done_text: str, completed_text: str) -> str:
    """Use the streamed deltas, or a later full-text event when deltas are absent."""
    delta = (delta_text or "").strip()
    for candidate in (done_text, completed_text):
        full = (candidate or "").strip()
        if not full:
            continue
        if not delta or full.startswith(delta):
            return full
    return delta


def build_responses_payload(request: ModelRequest, model: str) -> dict[str, Any]:
    """Responses API body. No temperature and no max_tokens.

    Only the leading contiguous ``system`` block is hoisted into top-level
    ``instructions`` so prompt-cache prefixes remain static across multi-turn
    tool loops. Any mid-conversation ``system`` messages stay ordered inside
    ``input`` as ``developer`` items.
    """
    leading_system: list[str] = []
    idx = 0
    while idx < len(request.messages):
        msg = request.messages[idx]
        if msg.get("role") != "system":
            break
        content = msg.get("content")
        if content:
            leading_system.append(str(content))
        idx += 1

    developer_prefix: list[str] = []
    conversation: list[dict[str, str]] = []
    for msg in request.messages[idx:]:
        role = msg.get("role") or "user"
        content = msg.get("content") or ""
        if role == "developer" and not conversation:
            if content:
                developer_prefix.append(str(content))
            continue
        if role == "system":
            conversation.append({"role": "developer", "content": str(content)})
        else:
            conversation.append({"role": str(role), "content": str(content)})

    input_items: list[dict[str, str]] = []
    if developer_prefix:
        input_items.append({"role": "developer", "content": "\n\n".join(developer_prefix)})
    input_items.extend(conversation)

    payload: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "stream": True,
        "store": False,
        "max_output_tokens": request.max_output_tokens,
    }
    if leading_system:
        payload["instructions"] = "\n\n".join(leading_system)
    effort = (request.reasoning_effort or "").strip().lower()
    if effort in _EFFORTS:
        payload["reasoning"] = {"effort": effort}
    if isinstance(request.response_format, dict) and request.response_format:
        fmt = dict(request.response_format)
        # Responses API uses text.format with flat name/schema if json_schema wrapper passed.
        if fmt.get("type") == "json_schema" and isinstance(fmt.get("json_schema"), dict):
            js = fmt["json_schema"]
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": js.get("name") or "response",
                    "schema": js.get("schema") or {},
                    "strict": bool(js.get("strict", True)),
                }
            }
        else:
            payload["text"] = {"format": fmt}
    return payload


def extract_authorization_code(callback_url: str) -> str:
    """Pull an OAuth code from a callback URL. Do not log the URL."""
    match = re.search(r"[?&]code=([^&#]+)", callback_url or "")
    if not match:
        raise ModelAuthError("OAuth callback did not contain an authorization code")
    return unquote(match.group(1))


class OpenAIChatGPTOAuthRoute:
    provider = "openai"
    route_id = "chatgpt_oauth"
    capabilities = RouteCapabilities(
        supports_temperature=False,
        supports_reasoning_effort=True,
        supports_json_schema=True,
        supports_streaming=True,
        cost_kind="subscription",
        subject_to_openrouter_cap=False,
    )

    def __init__(self, account_id: str = "default") -> None:
        self.account_id = account_id
        self.store = ModelSessionStore(self.provider, account_id)

    def _state_store(self) -> ModelSessionStore:
        return ModelSessionStore(f"{self.provider}_state", self.account_id)

    def siwc_endpoints_ready(self) -> bool:
        """True when Oscar can start the SIWC connect flow (explicit 5 vars or empty defaults)."""
        if _explicit_siwc_set():
            return all(bool(_env(name)) for name in _REQUIRED_ENV)
        return True

    def is_configured(self) -> bool:
        return self.siwc_endpoints_ready()

    def is_callable(self) -> bool:
        """True when SIWC endpoints are ready AND an active session exists."""
        if not self.siwc_endpoints_ready():
            return False
        session = self.store.load()
        return bool(
            session is not None
            and not session.needs_reauth
            and (session.access_token or session.refresh_token)
        )

    def status(self) -> dict[str, Any]:
        session = self.store.load()
        host_id = _siwc("OPENAI_SIWC_HOST_ID", create_host_id=False)
        base = {
            "provider": self.provider,
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "siwc_ready": self.siwc_endpoints_ready(),
            "connected": False,
            "refresh_owner": "azom",
            "app_owned_refresh": True,
            "needs_reauth": False,
            "generation": 0,
            "expires_at": 0.0,
            "model": _env("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
            "cost_kind": "subscription",
            "plan_usage": False,
            "id_token_verified": False,
            "ext_agent_host_id": host_id,
        }
        if not session:
            return base
        view = session.public_view()
        base.update(view)
        base["configured"] = self.is_configured()
        base["siwc_ready"] = self.siwc_endpoints_ready()
        base["connected"] = (
            not session.needs_reauth and bool(session.access_token) and session.status == "connected"
        )
        base["refresh_owner"] = "azom"
        base["app_owned_refresh"] = True
        base["route_id"] = self.route_id
        base["model"] = _env("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        base["cost_kind"] = "subscription"
        base["plan_usage"] = "chatgpt.tokens.use.direct" in session.scopes
        base["id_token_verified"] = False
        return base

    def _authorize_client_id(self) -> tuple[str, bool]:
        """Return the authorize client id and whether this is first registration.

        ``dynamic_agent_client`` is only the registration entrypoint. A saved
        issued client id is reused and must not be replaced by the sentinel.
        """
        existing = self.store.load()
        saved = (existing.client_id if existing else "").strip()
        if saved and saved != DYNAMIC_CLIENT_ID:
            return saved, False
        configured = _siwc("OPENAI_SIWC_CLIENT_ID")
        return configured, configured == DYNAMIC_CLIENT_ID

    def start_authorization(self) -> tuple[str, str]:
        """Return ``(authorize_url, state)``. State is stored until exchange."""
        if not self.siwc_endpoints_ready():
            raise ModelConfigError("ChatGPT OAuth configuration is incomplete")

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        nonce = secrets.token_urlsafe(24)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        client_id, initial_registration = self._authorize_client_id()
        host_id = _siwc("OPENAI_SIWC_HOST_ID", create_host_id=True)
        scope = _scope()
        self._state_store().save(
            ModelSession(
                provider=f"{self.provider}_state",
                account_id=self.account_id,
                client_id=client_id,
                ext_agent_host_id=host_id,
                code_verifier=verifier,
                oauth_state=state,
                nonce=nonce,
                expires_at=time.time() + 600,
                status="pending_authorization",
                scopes=scope.split(),
            )
        )
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _siwc("OPENAI_SIWC_REDIRECT_URI"),
            "scope": scope,
            "resource": _resource(),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "ext_agent_host_id": host_id,
        }
        if initial_registration:
            params["agent_name_hint"] = _agent_name()
        return f"{_siwc('OPENAI_SIWC_AUTHORIZATION_URL')}?{urlencode(params)}", state

    def exchange_authorization_code(
        self,
        *,
        code: str,
        state: str,
        issued_client_id: str | None = None,
    ) -> ModelSession:
        state_store = self._state_store()
        pending = state_store.load()
        if not pending or pending.status != "pending_authorization":
            raise ModelAuthError("Invalid or expired ChatGPT OAuth state")
        if not state or not pending.oauth_state:
            raise ModelAuthError("Invalid or expired ChatGPT OAuth state")
        if not hmac.compare_digest(pending.oauth_state, state):
            raise ModelAuthError("Invalid or expired ChatGPT OAuth state")
        if not pending.code_verifier or time.time() >= pending.expires_at:
            state_store.clear()
            raise ModelAuthError("ChatGPT OAuth state expired")

        callback_client = (issued_client_id or "").strip()
        requested = pending.client_id.strip()
        if requested == DYNAMIC_CLIENT_ID:
            if not callback_client or callback_client == DYNAMIC_CLIENT_ID:
                raise ModelAuthError(
                    "ChatGPT OAuth registration did not return an issued client id",
                    code="incomplete_registration",
                )
            client_id = callback_client
        elif callback_client and (
            len(callback_client) != len(requested)
            or not hmac.compare_digest(callback_client, requested)
        ):
            raise ModelAuthError(
                "ChatGPT OAuth callback client id did not match the pending registration",
                code="client_mismatch",
            )
        else:
            client_id = requested or _siwc("OPENAI_SIWC_CLIENT_ID")

        data = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": _siwc("OPENAI_SIWC_REDIRECT_URI"),
                "code_verifier": pending.code_verifier,
                "resource": _resource(),
            }
        )
        state_store.clear()
        session = self._session_from_token_response(
            data,
            generation=1,
            previous=None,
            client_id=client_id,
            host_id=pending.ext_agent_host_id or _siwc("OPENAI_SIWC_HOST_ID"),
        )
        if not session.access_token or not session.refresh_token:
            raise ModelAuthError("ChatGPT OAuth response missing required tokens")
        self.store.save(session)
        return session

    def disconnect(self) -> None:
        self.store.clear()
        self._state_store().clear()

    def _session_from_token_response(
        self,
        data: dict[str, Any],
        *,
        generation: int,
        previous: ModelSession | None,
        client_id: str = "",
        host_id: str = "",
    ) -> ModelSession:
        refresh = str(data.get("refresh_token") or "")
        if not refresh and previous is not None:
            refresh = previous.refresh_token
        scopes_raw = data.get("scope")
        if isinstance(scopes_raw, str) and scopes_raw.strip():
            scopes = scopes_raw.split()
        elif previous is not None:
            scopes = list(previous.scopes)
        else:
            scopes = []
        earliest = data.get("earliest_refresh_at")
        try:
            earliest_offset = float(earliest or 0)
        except (TypeError, ValueError):
            earliest_offset = 0.0
        try:
            expires_in = float(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        return ModelSession(
            provider=self.provider,
            account_id=self.account_id,
            client_id=(
                client_id
                or (previous.client_id if previous else "")
                or _siwc("OPENAI_SIWC_CLIENT_ID")
            ),
            ext_agent_host_id=(
                host_id
                or (previous.ext_agent_host_id if previous else "")
                or _siwc("OPENAI_SIWC_HOST_ID")
            ),
            access_token=str(data.get("access_token") or ""),
            refresh_token=refresh,
            id_token=str(data.get("id_token") or (previous.id_token if previous else "")),
            expires_at=time.time() + expires_in,
            earliest_refresh_at=time.time() + earliest_offset,
            generation=generation,
            status="connected",
            needs_reauth=False,
            scopes=scopes,
        )

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = requests.post(
                _siwc("OPENAI_SIWC_TOKEN_URL"),
                data=form,
                timeout=30,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            raise ModelUnavailableError(
                "ChatGPT OAuth token endpoint unreachable",
                code="token_endpoint_unavailable",
            ) from exc

        if response.status_code == 429:
            raise ModelRateLimitError("ChatGPT OAuth token endpoint rate limited")
        if response.status_code >= 500:
            raise ModelUnavailableError(
                f"ChatGPT OAuth token endpoint unavailable ({response.status_code})",
                code="token_endpoint_unavailable",
            )
        code = ""
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("error") == "invalid_grant":
                code = "invalid_grant"
            raise ModelAuthError(
                f"ChatGPT OAuth token request rejected ({response.status_code})",
                code=code,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ModelAuthError("ChatGPT OAuth token response was not JSON") from exc
        if not isinstance(data, dict):
            raise ModelAuthError("ChatGPT OAuth token response was invalid")
        return data

    def _mark_needs_reauth(self, current: ModelSession) -> None:
        current.needs_reauth = True
        current.status = "needs_reauth"
        try:
            self.store.save(current)
        except OSError as exc:
            raise ModelAuthError(
                "ChatGPT OAuth session could not be marked for reconnect",
                code="persist_failed",
            ) from exc

    def _refresh_session(self, *, rejected_generation: int | None = None) -> ModelSession:
        """Refresh under the process lock.

        When ``rejected_generation`` is set, a still-unexpired token from that
        generation was rejected (HTTP 401). Refresh it once. If another
        process already stored a newer generation, use that instead.
        """
        try:
            lock_ctx = self.store.lock()
        except Exception as exc:
            raise ModelUnavailableError(
                "ChatGPT OAuth session lock unavailable",
                code="lock_timeout",
            ) from exc
        try:
            with lock_ctx:
                current = self.store.load()
                if not current:
                    raise ModelAuthError("No ChatGPT OAuth session to refresh")
                if current.needs_reauth:
                    raise ModelAuthError("ChatGPT OAuth needs reconnect", code="needs_reauth")
                if rejected_generation is None:
                    if self.store.is_access_token_fresh(current):
                        return current
                elif (
                    current.generation > rejected_generation
                    and self.store.is_access_token_fresh(current)
                ):
                    return current
                if not current.refresh_token:
                    raise ModelAuthError("ChatGPT OAuth session has no refresh token")
                if not self.store.can_refresh_now(current):
                    raise ModelAuthError("ChatGPT OAuth refresh is not currently allowed")

                started_generation = current.generation
                try:
                    data = self._token_request(
                        {
                            "grant_type": "refresh_token",
                            "client_id": current.client_id or _siwc("OPENAI_SIWC_CLIENT_ID"),
                            "refresh_token": current.refresh_token,
                            "resource": _resource(),
                            "ext_agent_host_id": current.ext_agent_host_id or _siwc("OPENAI_SIWC_HOST_ID"),
                        }
                    )
                except ModelAuthError as exc:
                    if exc.code != "invalid_grant":
                        raise
                    latest = self.store.load()
                    if (
                        latest is not None
                        and latest.generation > started_generation
                        and self.store.is_access_token_fresh(latest)
                        and not latest.needs_reauth
                    ):
                        return latest
                    self._mark_needs_reauth(current)
                    raise

                refreshed = self._session_from_token_response(
                    data,
                    generation=started_generation + 1,
                    previous=current,
                )
                if not refreshed.access_token:
                    raise ModelAuthError("ChatGPT OAuth refresh response missing access token")
                try:
                    self.store.save(refreshed)
                except OSError as exc:
                    self._mark_needs_reauth(current)
                    raise ModelAuthError(
                        "ChatGPT OAuth refresh could not be persisted; reconnect required",
                        code="persist_failed",
                    ) from exc
                return refreshed
        except TimeoutError as exc:
            raise ModelUnavailableError(
                "ChatGPT OAuth session lock timed out",
                code="lock_timeout",
            ) from exc

    def _ensure_access_token(self) -> str:
        session = self.store.load()
        if not session:
            raise ModelAuthError("ChatGPT OAuth is not connected")
        if session.needs_reauth:
            raise ModelAuthError("ChatGPT OAuth needs reconnect", code="needs_reauth")
        if self.store.is_access_token_fresh(session):
            return session.access_token
        return self._refresh_session().access_token

    def _post_responses(
        self,
        access_token: str,
        payload: dict[str, Any],
        timeout: int,
    ) -> requests.Response:
        try:
            return requests.post(
                RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
                stream=True,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise ModelUnavailableError("ChatGPT Responses endpoint unreachable") from exc

    def _read_sse(
        self,
        response: requests.Response,
        on_text_delta=None,
        *,
        on_first_delta=None,
        hard_deadline: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        parts: list[str] = []
        done_text = ""
        completed_text = ""
        usage: dict[str, Any] = {}
        saw_delta = False
        try:
            for line in response.iter_lines(decode_unicode=True):
                if hard_deadline is not None and time.monotonic() > hard_deadline:
                    raise ModelUnavailableError("ChatGPT stream hard timeout", code="hard_timeout")
                if not line or not str(line).startswith("data:"):
                    continue
                raw = str(line)[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        if not saw_delta:
                            saw_delta = True
                            if on_first_delta:
                                try:
                                    on_first_delta()
                                except Exception:
                                    pass
                        parts.append(delta)
                        if on_text_delta:
                            try:
                                on_text_delta(delta)
                            except Exception:
                                pass
                elif event_type == "response.output_text.done":
                    text = event.get("text")
                    if isinstance(text, str):
                        done_text = text
                elif event_type == "response.completed":
                    response_obj = event.get("response") or {}
                    if isinstance(response_obj, dict):
                        if isinstance(response_obj.get("usage"), dict):
                            usage = response_obj["usage"]
                        completed_text = _completed_output_text(response_obj)
        except ModelUnavailableError:
            raise
        except requests.RequestException as exc:
            raise ModelUnavailableError("ChatGPT stream interrupted", code="stream_error") from exc
        finally:
            response.close()
        return _prefer_output_text("".join(parts), done_text, completed_text), usage

    def complete(
        self,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult:
        tel = telemetry or default_telemetry
        if not self.siwc_endpoints_ready():
            raise ModelConfigError("ChatGPT OAuth configuration is incomplete")
        model = request.model or _env("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        payload = build_responses_payload(request, model)
        _emit_phase(
            request,
            {"phase": "waiting", "route_id": self.route_id, "model": model},
        )
        hard_deadline = (
            time.monotonic() + float(request.hard_timeout)
            if request.hard_timeout and request.hard_timeout > 0
            else None
        )
        access_token = self._ensure_access_token()
        used = self.store.load()
        rejected_generation = used.generation if used else -1
        response = self._post_responses(access_token, payload, request.timeout)
        if response.status_code == 401:
            response.close()
            _emit_phase(
                request,
                {"phase": "oauth_refresh", "route_id": self.route_id},
            )
            refreshed = self._refresh_session(rejected_generation=rejected_generation)
            response = self._post_responses(refreshed.access_token, payload, request.timeout)
            if response.status_code == 401:
                response.close()
                raise ModelAuthError("ChatGPT Responses rejected the refreshed token")
        if response.status_code == 429:
            retry_raw = getattr(response, "headers", {}).get("Retry-After") if getattr(response, "headers", None) else None
            retry_after = None
            if retry_raw is not None:
                try:
                    retry_after = float(str(retry_raw).strip())
                except (TypeError, ValueError):
                    retry_after = None
            response.close()
            raise ModelRateLimitError("ChatGPT Responses rate limit reached", retry_after=retry_after)
        if response.status_code == 403:
            response.close()
            raise ModelAuthError("ChatGPT Responses authorization denied", code="forbidden")
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            code = "unknown_model" if status in {400, 404} else f"http_{status}"
            raise ModelUnavailableError(f"ChatGPT Responses request rejected ({status})", code=code)

        on_first_delta = (
            (
                lambda: _emit_phase(
                    request,
                    {"phase": "streaming", "route_id": self.route_id, "model": model},
                )
            )
            if request.on_phase_change is not None
            else None
        )
        content, usage = self._read_sse(
            response,
            request.on_text_delta,
            on_first_delta=on_first_delta,
            hard_deadline=hard_deadline,
        )
        if not content:
            raise ModelUnavailableError("ChatGPT Responses returned no content")
        prompt_tokens, completion_tokens, total_tokens = _usage_ints(usage)
        tel.record(
            action="model_chatgpt_oauth",
            site=request.site,
            units=total_tokens or 1,
            unit_type="tokens" if usage else "api_calls",
            cost_usd=0.0,
            case_id=request.case_id,
            actor=request.actor,
            meta={
                "provider": self.provider,
                "route_id": self.route_id,
                "model": model,
                "usage_known": bool(usage),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_kind": "subscription",
                "refresh_owner": "azom",
            },
        )
        return ModelResult(
            content=content,
            provider=self.provider,
            route_id=self.route_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=0.0,
            usage_known=bool(usage),
            raw_usage=dict(usage) if usage else None,
        )
