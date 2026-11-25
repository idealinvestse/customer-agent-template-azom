"""Delegated xAI route backed by the Grok Build CLI.

Grok Build owns and refreshes its own OAuth session. Azom does not read,
copy, or rotate that credential file, and it does not reuse another
application's OAuth client identity.

TODO: direct app-owned xAI OAuth refresh is blocked until xAI publishes a
verified client registration, scope list, token endpoint, and inference
contract that Azom can implement without guessing. Do not fake that flow.
"""

from __future__ import annotations

import codecs
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ecom_ops.model_router.base import ModelRequest, ModelResult
from ecom_ops.model_router.base import emit_phase as _emit_phase
from ecom_ops.model_router.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelUnavailableError,
)
from ecom_ops.telemetry import Telemetry, default_telemetry

DEFAULT_XAI_MODEL = "grok-4"
_FAST_TOOL_MODEL = "grok-4.3"
_FAST_TOOL_FALLBACK = "grok-4.7-build-fast"
_POLL_SECONDS = 0.25
_DEFAULT_HARD_TIMEOUT = 180
_MAX_HARD_TIMEOUT = 600
_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})
_UNKNOWN_MODEL_MARKERS = (
    "unknown model",
    "model not found",
    "invalid model",
    "not a valid model",
    "unrecognized model",
    "no such model",
    "unsupported model",
)
# Phases that mean this turn is still producing text. waiting_for_model does not.
_ACTIVE_PHASES = frozenset({"streaming_reasoning", "streaming_text"})
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


def _stderr_says_unknown_model(stderr: str) -> bool:
    """True when the CLI rejected the model id. The text is not returned."""
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in _UNKNOWN_MODEL_MARKERS)


def _accepted_effort(request: ModelRequest) -> str | None:
    effort = (request.reasoning_effort or "").strip().lower()
    if effort in _EFFORTS:
        return effort
    return None


def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _hard_limit(idle: int, override: int | None = None) -> int:
    """Upper bound while reasoning or text is still streaming.

    The idle timeout resets on new active phases. This cap still stops a
    stream that never ends. A positive override replaces the env cap for
    that call. Values above 600 seconds are clamped.
    """
    if override is not None and int(override) > 0:
        return max(idle, min(int(override), _MAX_HARD_TIMEOUT))
    raw = (os.environ.get("AZOM_XAI_GROK_HARD_TIMEOUT") or "").strip()
    chosen = int(raw) if raw.isdigit() and int(raw) > 0 else _DEFAULT_HARD_TIMEOUT
    return max(idle, min(chosen, _MAX_HARD_TIMEOUT))


def _sessions_dir() -> Path:
    return Path.home() / ".grok" / "sessions"


def _event_files(work: Path) -> list[Path]:
    """Grok session event logs for this temp cwd. No prompt or chat files."""
    base = _sessions_dir()
    if not base.is_dir():
        return []
    name = work.name
    found: list[Path] = []
    try:
        children = list(base.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or name not in child.name:
            continue
        candidates = [child / "events.jsonl"]
        try:
            candidates.extend(sub / "events.jsonl" for sub in child.iterdir() if sub.is_dir())
        except OSError:
            continue
        for path in candidates:
            if path.is_file():
                found.append(path)
    return found


def _read_active_phases(path: Path, state: dict[str, tuple[int, str]]) -> list[str]:
    """Return new active phase names from ``events.jsonl``.

    Only the ``phase`` string from ``phase_changed`` events is inspected.
    Prompt, text, and reasoning content are never read or returned.
    """
    key = str(path)
    offset, partial = state.get(key, (0, ""))
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size < offset:
        offset, partial = 0, ""
    if size == offset:
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            chunk = handle.read()
            offset = handle.tell()
    except OSError:
        return []
    text = partial + chunk
    lines = text.split("\n")
    state[key] = (offset, lines[-1][-4000:])
    phases: list[str] = []
    for line in lines[:-1]:
        row_text = line.strip()
        if not row_text:
            continue
        try:
            row = json.loads(row_text)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and row.get("type") == "phase_changed"
            and row.get("phase") in _ACTIVE_PHASES
        ):
            phases.append(str(row["phase"]))
    return phases


def _count_active(path: Path, state: dict[str, tuple[int, str]]) -> int:
    """Count new streaming_reasoning or streaming_text phases.

    Only the phase name is inspected. Prompt text is not kept.
    """
    return len(_read_active_phases(path, state))


def spawn_grok(
    command: list[str],
    cwd: str,
    on_text_delta=None,
) -> subprocess.Popen[str]:
    """Start grok and drain pipes so a long answer cannot block the child."""
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    holder: dict[str, str] = {"out": "", "err": ""}

    def _read(pipe: Any, key: str) -> None:
        if pipe is None:
            return
        raw_pipe = getattr(pipe, "buffer", None)
        read_chunk = getattr(raw_pipe, "read1", None)
        if key != "out" or not on_text_delta or not callable(read_chunk):
            holder[key] = pipe.read()
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        chunks: list[str] = []
        while True:
            raw = read_chunk(1024)
            if not raw:
                break
            text = decoder.decode(raw)
            if text:
                chunks.append(text)
                try:
                    on_text_delta(text)
                except Exception:
                    pass
        tail = decoder.decode(b"", final=True)
        if tail:
            chunks.append(tail)
            try:
                on_text_delta(tail)
            except Exception:
                pass
        holder[key] = "".join(chunks)

    threads = [
        threading.Thread(target=_read, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=_read, args=(proc.stderr, "err"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    proc.azom_output = holder  # type: ignore[attr-defined]
    proc.azom_threads = threads  # type: ignore[attr-defined]
    return proc


def _read_output(proc: Any) -> tuple[str, str]:
    threads = getattr(proc, "azom_threads", None)
    holder = getattr(proc, "azom_output", None)
    if threads is not None and holder is not None:
        for thread in threads:
            thread.join(timeout=5)
        return str(holder.get("out") or ""), str(holder.get("err") or "")
    return str(getattr(proc, "stdout_data", "") or ""), str(getattr(proc, "stderr_data", "") or "")


def _stop_process(proc: Any) -> None:
    if isinstance(proc, subprocess.Popen):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    proc.kill()
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return


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

    def _grok_command(
        self,
        binary: str,
        model: str,
        prompt_path: Path,
        effort: str | None,
    ) -> list[str]:
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
        if effort:
            command.extend(["--reasoning-effort", effort])
        return command

    def _run_cli(
        self,
        command: list[str],
        work: str,
        request: ModelRequest,
        *,
        model: str | None = None,
    ) -> str:
        idle = request.timeout if request.timeout and request.timeout > 0 else 30
        hard = _hard_limit(idle, request.hard_timeout)
        chosen_model = model or (
            command[command.index("--model") + 1]
            if "--model" in command and command.index("--model") + 1 < len(command)
            else (request.model or self._model())
        )
        started = _now()
        deadline = started + idle
        hard_at = started + hard
        phase_state: dict[str, tuple[int, str]] = {}
        phase_lock = threading.Lock()
        current_phase = "waiting"

        def _emit_if_changed(new_phase: str, payload: dict[str, Any]) -> None:
            nonlocal current_phase
            with phase_lock:
                if current_phase == new_phase:
                    return
                current_phase = new_phase
            _emit_phase(request, payload)

        _emit_phase(
            request,
            {
                "phase": "waiting",
                "route_id": self.route_id,
                "model": chosen_model,
                "hard_timeout": hard,
            },
        )
        on_delta = request.on_text_delta
        if request.on_phase_change is not None:
            orig_delta = request.on_text_delta

            def _wrapped_delta(chunk: str) -> None:
                _emit_if_changed(
                    "streaming",
                    {
                        "phase": "streaming",
                        "route_id": self.route_id,
                        "model": chosen_model,
                    },
                )
                if orig_delta is not None:
                    orig_delta(chunk)

            on_delta = _wrapped_delta

        try:
            process = spawn_grok(command, work, on_delta)
        except FileNotFoundError as exc:
            raise ModelConfigError("Grok Build CLI binary was not found") from exc
        while True:
            code = process.poll()
            active = 0
            for events in _event_files(Path(work)):
                phases = _read_active_phases(events, phase_state)
                active += len(phases)
                for phase_name in phases:
                    if phase_name == "streaming_reasoning":
                        _emit_if_changed(
                            "reasoning",
                            {
                                "phase": "reasoning",
                                "route_id": self.route_id,
                                "model": chosen_model,
                                "hard_timeout": hard,
                            },
                        )
                    elif phase_name == "streaming_text":
                        _emit_if_changed(
                            "streaming",
                            {
                                "phase": "streaming",
                                "route_id": self.route_id,
                                "model": chosen_model,
                            },
                        )
            if active:
                # Still reasoning or writing the answer: grant another idle window.
                deadline = min(_now() + idle, hard_at)
            if code is not None:
                break
            now = _now()
            if now >= hard_at or now >= deadline:
                _stop_process(process)
                _read_output(process)
                raise ModelUnavailableError("Grok Build CLI timed out")
            _sleep(_POLL_SECONDS)

        stdout, stderr = _read_output(process)
        if process.returncode != 0:
            exit_code = f"exit_{process.returncode}"
            if _stderr_says_unauthenticated(stderr):
                raise ModelAuthError(
                    "Grok Build is not authenticated. Run grok login --device-auth",
                    code=exit_code,
                )
            failed = ModelUnavailableError("Grok Build CLI failed", code=exit_code)
            if _stderr_says_unknown_model(stderr):
                failed.unknown_model = True  # type: ignore[attr-defined]
            raise failed

        content = stdout.strip()
        if not content:
            raise ModelUnavailableError("Grok Build CLI returned no content")
        return content

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
            effort = _accepted_effort(request)
            # grok --model grok-4.3 models does not reject the id. A real
            # call may. Retry that one rejection with the fast build model.
            candidates = [model]
            if model == _FAST_TOOL_MODEL:
                candidates.append(_FAST_TOOL_FALLBACK)
            content = ""
            served = model
            for index, chosen in enumerate(candidates):
                command = self._grok_command(binary, chosen, prompt_path, effort)
                try:
                    content = self._run_cli(command, work, request, model=chosen)
                    served = chosen
                    break
                except ModelUnavailableError as exc:
                    unknown = bool(getattr(exc, "unknown_model", False))
                    if unknown and index == 0 and len(candidates) > 1:
                        _emit_phase(
                            request,
                            {
                                "phase": "model_fallback",
                                "from_model": chosen,
                                "to_model": candidates[index + 1],
                            },
                        )
                        continue
                    raise

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
                    "model": served,
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
                model=served,
                usage_known=False,
                cost_usd=0.0,
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
