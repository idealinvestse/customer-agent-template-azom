"""Process-safe OAuth session storage for model providers.

Sessions live under ``AZOM_DATA_DIR/model_oauth/``, separate from Gmail and
Google Marketing OAuth. Refresh tokens never enter telemetry, logs, SQLite,
or the public status view.

The lock is ``fcntl.flock`` on POSIX (Ubuntu production). On Windows the same
API uses ``msvcrt.locking`` so local tests can serialize threads.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]


def data_dir() -> Path:
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def model_oauth_dir() -> Path:
    path = data_dir() / "model_oauth"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


@dataclass
class ModelSession:
    provider: str
    account_id: str
    client_id: str = ""
    ext_agent_host_id: str = ""
    redirect_uri: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    code_verifier: str = ""
    oauth_state: str = ""
    nonce: str = ""
    expires_at: float = 0.0
    earliest_refresh_at: float = 0.0
    generation: int = 0
    status: str = "connected"
    needs_reauth: bool = False
    scopes: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def public_view(self) -> dict[str, Any]:
        """Dashboard-safe view. Whitelist only — never add secret fields here."""
        return {
            "provider": self.provider,
            "account_id": self.account_id,
            "ext_agent_host_id": self.ext_agent_host_id,
            "status": self.status,
            "needs_reauth": self.needs_reauth,
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "expires_at": self.expires_at,
            "earliest_refresh_at": self.earliest_refresh_at,
            "generation": self.generation,
            "scopes": list(self.scopes),
            "updated_at": self.updated_at,
        }


def _acquire_lock(fh: Any, *, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    if fcntl is not None:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring model session lock: {label}")
                time.sleep(0.05)
        return
    import msvcrt

    fh.seek(0, os.SEEK_END)
    if fh.tell() == 0:
        fh.write(b"\0")
        fh.flush()
    fh.seek(0)
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out acquiring model session lock: {label}")
            time.sleep(0.05)


def _release_lock(fh: Any) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt

    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        return


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` atomically via temp file + fsync + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass
        os.replace(tmp_name, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


@contextmanager
def file_lock(lock_path: Path, *, timeout: float = 15.0, label: str = "file") -> Iterator[None]:
    """Acquire an advisory cross-process lock on ``lock_path``."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+b")
    try:
        _acquire_lock(fh, timeout=timeout, label=label)
        try:
            yield
        finally:
            _release_lock(fh)
    finally:
        fh.close()


class ModelSessionStore:
    """Atomic JSON session persistence with an advisory cross-process lock."""

    def __init__(self, provider: str, account_id: str = "default") -> None:
        self.provider = provider
        self.account_id = account_id
        base = model_oauth_dir()
        self.session_path = base / f"{provider}_{account_id}.json"
        self.lock_path = base / f"{provider}_{account_id}.lock"

    @contextmanager
    def lock(self, *, timeout: float = 15.0) -> Iterator[None]:
        """Serialize refresh for one provider and account across processes."""
        with file_lock(self.lock_path, timeout=timeout, label=self.provider):
            yield

    def load(self) -> ModelSession | None:
        if not self.session_path.is_file():
            return None
        try:
            raw = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        scopes = raw.get("scopes") or []
        if not isinstance(scopes, list):
            scopes = []
        return ModelSession(
            provider=str(raw.get("provider") or self.provider),
            account_id=str(raw.get("account_id") or self.account_id),
            client_id=str(raw.get("client_id") or ""),
            ext_agent_host_id=str(raw.get("ext_agent_host_id") or ""),
            redirect_uri=str(raw.get("redirect_uri") or ""),
            access_token=str(raw.get("access_token") or ""),
            refresh_token=str(raw.get("refresh_token") or ""),
            id_token=str(raw.get("id_token") or ""),
            code_verifier=str(raw.get("code_verifier") or ""),
            oauth_state=str(raw.get("oauth_state") or ""),
            nonce=str(raw.get("nonce") or ""),
            expires_at=float(raw.get("expires_at") or 0.0),
            earliest_refresh_at=float(raw.get("earliest_refresh_at") or 0.0),
            generation=int(raw.get("generation") or 0),
            status=str(raw.get("status") or "connected"),
            needs_reauth=bool(raw.get("needs_reauth") or False),
            scopes=[str(item) for item in scopes],
            updated_at=float(raw.get("updated_at") or time.time()),
        )

    def save(self, session: ModelSession) -> None:
        session.updated_at = time.time()
        payload = {
            "provider": session.provider,
            "account_id": session.account_id,
            "client_id": session.client_id,
            "ext_agent_host_id": session.ext_agent_host_id,
            "redirect_uri": session.redirect_uri,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "id_token": session.id_token,
            "code_verifier": session.code_verifier,
            "oauth_state": session.oauth_state,
            "nonce": session.nonce,
            "expires_at": session.expires_at,
            "earliest_refresh_at": session.earliest_refresh_at,
            "generation": session.generation,
            "status": session.status,
            "needs_reauth": session.needs_reauth,
            "scopes": session.scopes,
            "updated_at": session.updated_at,
        }
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.session_path.parent),
            prefix=f".{self.session_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.session_path)
            os.chmod(self.session_path, 0o600)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def clear(self) -> None:
        if self.session_path.is_file():
            self.session_path.unlink()

    def is_access_token_fresh(self, session: ModelSession, skew: int = 120) -> bool:
        return bool(session.access_token) and time.time() < (session.expires_at - skew)

    def can_refresh_now(self, session: ModelSession) -> bool:
        return (
            bool(session.refresh_token)
            and not session.needs_reauth
            and time.time() >= session.earliest_refresh_at
        )
