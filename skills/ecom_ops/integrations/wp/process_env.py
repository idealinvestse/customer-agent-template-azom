"""Shared subprocess environment, loopback port, timestamp, and process cleanup helpers."""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
from datetime import UTC, datetime
from typing import Any

from ecom_ops.wp_ops.review_runner import FORBIDDEN_ENV_NAMES
from ecom_ops.wp_ops.review_workspace import ReviewError

ALLOWED_OS_ENV: tuple[str, ...] = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
PROBE_ENV_KEYS: tuple[str, ...] = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "WP_SECRET_",
    "WOO_",
    "MAIL_",
    "SMTP_",
    "OPENROUTER_",
    "TELEGRAM_",
    "MESSENGER_",
)
WP_VERSION_RE = re.compile(r"\$wp_version\s*=\s*(['\"])([^'\"]+)\1\s*;")


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


def probe_subprocess_env() -> dict[str, str]:
    """Return a minimal safe OS environment for probing local binaries (e.g. php -v)."""
    allowed = {key.upper() for key in PROBE_ENV_KEYS}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def build_php_child_env() -> dict[str, str]:
    """Return a minimal synthetic env for the PHP child process. Never copies secrets."""
    env: dict[str, str] = {"AZOM_REVIEW_SYNTHETIC": "1"}
    for key in ALLOWED_OS_ENV:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if FORBIDDEN_ENV_NAMES.intersection(env):
        raise ReviewError("SandboxSecretRefused")
    if any(name.upper().startswith(FORBIDDEN_ENV_PREFIXES) for name in env):
        raise ReviewError("SandboxSecretRefused")
    return env


def pick_loopback_port() -> int:
    """Reserve and return an available ephemeral TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def terminate_process_tree(proc: subprocess.Popen[Any] | None, *, timeout_s: float = 5.0) -> None:
    """Terminate a child process and its descendants without raising on cleanup."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout_s)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout_s,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=timeout_s)
        except (OSError, subprocess.SubprocessError):
            pass
    except (OSError, subprocess.SubprocessError):
        pass
