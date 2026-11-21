"""Delegated xAI route backed by the Grok Build CLI.

Grok Build owns and refreshes its own OAuth session. Azom does not read,
copy, or rotate that credential file, and it does not reuse another
application's OAuth client identity.

TODO: direct app-owned xAI OAuth refresh is blocked until xAI publishes a
verified client registration, scope list, token endpoint, and inference
contract that Azom can implement without guessing. Do not fake that flow.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ecom_ops.model_router.base import ModelRequest, ModelResult
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelUnavailableError,
)
from ecom_ops.telemetry import Telemetry, default_telemetry

DEFAULT_XAI_MODEL = "grok-4"
_AUTH_MARKERS = (
    "not logged in",
    "not authenticated",
    "login required",
    "authentication required",
    "please login",
    "please log in",
    "sign in to",
    "device-auth",
    "unauthorized",
)


def _stderr_says_unauthenticated(stderr: str) -> bool:
    """True when the CLI text is an auth failure. The text is not returned."""
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


def grok_build_enabled() -> bool:
    """True only when an operator explicitly opts in.

    A ``grok`` binary on PATH is not enough. Otherwise every LLM call on a
    developer machine would spawn the CLI.
    """
    flag = (os.environ.get("AZOM_XAI_GROK_BUILD") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    return bool((os.environ.get("XAI_GROK_BIN") or "").strip())


class XaiGrokBuildRoute:
    provider = "xai"
    route_id = "xai_grok_build"

    def _binary(self) -> str:
        return (os.environ.get("XAI_GROK_BIN") or "grok").strip() or "grok"

    def _model(self) -> str:
        return (os.environ.get("XAI_MODEL") or DEFAULT_XAI_MODEL).strip()

    def is_configured(self) -> bool:
        return grok_build_enabled()

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "route_id": self.route_id,
            "configured": self.is_configured(),
            "connected": False,
            "auth_checked": False,
            "refresh_owner": "grok_build",
            "app_owned_refresh": False,
            "model": self._model(),
            "binary": self._binary() if self.is_configured() else "",
            "cost_kind": "subscription",
            "delegated_refresh": True,
        }

    def complete(
        self,
        request: ModelRequest,
        *,
        telemetry: Telemetry | None = None,
    ) -> ModelResult:
        tel = telemetry or default_telemetry
        if not self.is_configured():
            raise ModelConfigError("Grok Build route is not enabled")

        binary = self._binary()
        model = request.model or self._model()
        prompt = "\n\n".join(
            f"{(message.get('role') or 'user').upper()}:\n{message.get('content') or ''}"
            for message in request.messages
        )
        # --prompt-file is the headless one-shot input. --single cannot be
        # combined with it, and --permission-mode plan waits instead of
        # printing. The prompt stays out of argv. An empty --tools list
        # allows no built-in tools. cwd is an empty temp dir.
        work = tempfile.mkdtemp(prefix="azom-grok-")
        prompt_path = Path(work) / "prompt.txt"
        try:
            prompt_path.write_text(prompt, encoding="utf-8")
            try:
                os.chmod(prompt_path, 0o600)
            except OSError:
                pass
            command = [
                binary,
                "--model",
                model,
                "--prompt-file",
                str(prompt_path),
                "--max-turns",
                "1",
                "--no-subagents",
                "--disable-web-search",
                "--no-memory",
                "--verbatim",
                "--tools",
                "",
            ]
            try:
                process = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=request.timeout,
                    check=False,
                    cwd=work,
                )
            except FileNotFoundError as exc:
                raise ModelConfigError("Grok Build CLI binary was not found") from exc
            except subprocess.TimeoutExpired as exc:
                raise ModelUnavailableError("Grok Build CLI timed out") from exc

            if process.returncode != 0:
                code = f"exit_{process.returncode}"
                if _stderr_says_unauthenticated(process.stderr or ""):
                    raise ModelAuthError(
                        "Grok Build is not authenticated. Run grok login --device-auth",
                        code=code,
                    )
                raise ModelUnavailableError("Grok Build CLI failed", code=code)

            content = (process.stdout or "").strip()
            if not content:
                raise ModelUnavailableError("Grok Build CLI returned no content")

            tel.record(
                action="model_xai_grok_build",
                site=request.site,
                units=1,
                unit_type="api_calls",
                cost_usd=0.0,
                case_id=request.case_id,
                actor=request.actor,
                meta={
                    "provider": self.provider,
                    "route_id": self.route_id,
                    "model": model,
                    "usage_known": False,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_kind": "subscription",
                    "refresh_owner": "grok_build",
                    "app_owned_refresh": False,
                },
            )
            return ModelResult(
                content=content,
                provider=self.provider,
                route_id=self.route_id,
                model=model,
                usage_known=False,
                cost_usd=0.0,
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
