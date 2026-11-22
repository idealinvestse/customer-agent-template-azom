"""Start Grok Build's own device login from the dashboard.

The grok process owns the session. This module stores the verification URL
and the short user code so Oscar can finish sign-in. It does not read
auth.json and it does not keep access or refresh tokens.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

_URL_RE = re.compile(r"https://(?:accounts|auth)\.x\.ai/[^\s\"']+")
_CODE_RE = re.compile(r"\b([A-Z0-9]{4,8}-[A-Z0-9]{4,8})\b")
_lock = threading.Lock()
_proc: subprocess.Popen[str] | None = None
_reader: threading.Thread | None = None


def _status_path() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data")) / "xai_device_login.json"


def _binary() -> str:
    explicit = (os.environ.get("XAI_GROK_BIN") or "").strip()
    if explicit:
        return explicit
    found = shutil.which("grok")
    return found or ""


def _command() -> list[str]:
    return [_binary(), "login", "--device-auth"]


def _blank() -> dict[str, str]:
    return {
        "status": "idle",
        "verification_uri": "",
        "user_code": "",
        "detail": "",
    }


def _read_status() -> dict[str, str]:
    path = _status_path()
    if not path.is_file():
        return _blank()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank()
    if not isinstance(raw, dict):
        return _blank()
    status = _blank()
    for key in status:
        value = raw.get(key)
        if isinstance(value, str):
            status[key] = value[:300]
    uri = status["verification_uri"]
    if not uri.startswith(("https://accounts.x.ai/", "https://auth.x.ai/")):
        status["verification_uri"] = ""
    if not _CODE_RE.fullmatch(status["user_code"]):
        status["user_code"] = ""
    return status


def _write_status(status: dict[str, str]) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = _blank()
    for key in safe:
        safe[key] = str(status.get(key) or "")[:300]
    path.write_text(json.dumps(safe), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def device_login_status() -> dict[str, str]:
    with _lock:
        return _read_status()


def _enable_grok_build() -> None:
    os.environ["AZOM_XAI_GROK_BUILD"] = "1"
    path = Path(os.environ.get("AZOM_DATA_DIR", ".azom-data")) / "runtime.env"
    lines: list[str] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("AZOM_XAI_GROK_BUILD="):
                continue
            lines.append(line)
    lines.append("AZOM_XAI_GROK_BUILD=1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    from ecom_ops.runtime_env import apply_overlays

    apply_overlays()


def _absorb(line: str, status: dict[str, str]) -> None:
    match = _URL_RE.search(line or "")
    if match:
        status["verification_uri"] = match.group(0).rstrip(").,")
        status["status"] = "pending"
    code = _CODE_RE.search(line or "")
    if code:
        status["user_code"] = code.group(1)
        status["status"] = "pending"


def _read_output(proc: subprocess.Popen[str]) -> None:
    status = _read_status()
    stream = proc.stdout
    if stream is not None:
        for line in stream:
            with _lock:
                _absorb(line, status)
                _write_status(status)
    code = proc.wait()
    with _lock:
        current = _read_status()
        if code == 0:
            current["status"] = "finished"
            current["user_code"] = ""
            current["detail"] = "Grok Build avslutade inloggningen"
            _write_status(current)
            _enable_grok_build()
        else:
            current["status"] = "failed"
            current["user_code"] = ""
            current["detail"] = f"exit_{code}"
            _write_status(current)


def start_device_login(*, wait_s: float = 8.0) -> dict[str, str]:
    """Start ``grok login --device-auth`` unless one is already running."""
    global _proc, _reader
    command = _command()
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return _read_status()
        if not command[0]:
            status = _blank()
            status["status"] = "failed"
            status["detail"] = "grok saknas"
            _write_status(status)
            return status
        status = _blank()
        status["status"] = "pending"
        status["detail"] = "väntar på kod"
        _write_status(status)
        try:
            _proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError:
            status["status"] = "failed"
            status["detail"] = "grok startade inte"
            _write_status(status)
            _proc = None
            return status
        _reader = threading.Thread(target=_read_output, args=(_proc,), daemon=True)
        _reader.start()
    deadline = time.time() + max(0.0, wait_s)
    while time.time() < deadline:
        snap = device_login_status()
        if snap["user_code"] or snap["status"] in {"failed", "finished"}:
            return snap
        time.sleep(0.05)
    return device_login_status()
