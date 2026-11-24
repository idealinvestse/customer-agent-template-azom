"""Per-site SSH settings stored beside the registry.

The registry file stays in git and keeps its defaults. Overrides live in
``AZOM_DATA_DIR/wp_ssh.json``. A pasted private key is written to
``AZOM_DATA_DIR/ssh-keys/<site_id>`` and is not stored in the JSON file, in
``runtime.env``, or in the returned view. Saving these settings does not
open a connection and does not change ``AZOM_WP_FILES_SSH``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.models import Site, _files_root, _ssh_host, files_root_narrow

_SITE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")
_HOST_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "sk-ssh-ed25519@openssh.com",
    }
)
_HOST_KEY_B64 = re.compile(r"^[A-Za-z0-9+/]+=*$")
_FIELDS = (
    "files_root",
    "ssh_host",
    "ssh_user",
    "ssh_port",
    "host_public_key",
)
_UNREADABLE = "wp_ssh.json kan inte läsas."
_ROW = "SSH-överlagringen för sajten hoppades över."
_ROOT = "Rotmappen måste vara en absolut sökväg."
_ROOT_BROAD = "Rotmappen är för bred."
_HOST = "Värden måste vara ett värdnamn."
_PORT = "Porten måste vara 1 till 65535."
_IDENTITY_MSG = "WordPress SFTP accepterar bara den lagrade nyckeln per sajt."
_KEY = "Den privata nyckeln godtogs inte."
_KEY_CHOICE = "Skicka antingen en ny nyckel eller rensa den sparade."
_KEY_WRITE = "Den privata nyckeln kunde inte sparas."
_KEY_CLEAR = "Den sparade nyckeln kunde inte tas bort."
_HOST_KEY = "Värdens publika nyckel godtogs inte."
_SAVE = "SSH-inställningarna kunde inte sparas."
_MAX_KEY_BYTES = 16384
_MAX_HOST_KEY = 2000
_PROCESS_LOCK = threading.Lock()


class SettingsRejected(ValueError):
    """The SSH settings were refused. The message is a fixed sentence."""


def data_dir_path(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    return Path(os.environ.get("AZOM_DATA_DIR", ".azom-data"))


def overlay_file(data_dir: Path | None) -> Path:
    return data_dir_path(data_dir) / "wp_ssh.json"


def stored_key_path(data_dir: Path | None, site_id: str) -> Path:
    if not _SITE_ID.fullmatch(site_id):
        raise SettingsRejected(_KEY_WRITE)
    root = data_dir_path(data_dir).resolve() / "ssh-keys"
    path = root / site_id
    if path.parent != root:
        raise SettingsRejected(_KEY_WRITE)
    return path


def known_hosts_path(data_dir: Path | None, site_id: str) -> Path | None:
    if not _SITE_ID.fullmatch(site_id):
        return None
    root = (data_dir_path(data_dir) / "ssh-known-hosts").resolve()
    path = (root / site_id).resolve()
    if path.parent != root:
        return None
    return path


def user_ok(user: str) -> bool:
    return _USER.fullmatch(user) is not None and user.casefold() != "root"


def host_key_ok(value: str) -> bool:
    """True for one OpenSSH public-key line. Private keys are refused."""
    if not isinstance(value, str) or any(ch in value for ch in "\r\n\x00'\"`"):
        return False
    text = value.strip()
    if not text or len(text) > _MAX_HOST_KEY:
        return False
    if "PRIVATE KEY" in text or "-----BEGIN" in text:
        return False
    parts = text.split(" ", 2)
    if len(parts) < 2:
        return False
    kind, blob = parts[0], parts[1]
    if kind not in _HOST_KEY_TYPES or _HOST_KEY_B64.fullmatch(blob) is None:
        return False
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, TypeError):
        return False
    return len(raw) > 0


def host_key_fingerprint(value: str) -> str:
    if not host_key_ok(value):
        return ""
    blob = value.strip().split(" ", 2)[1]
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def load_overlay(data_dir: Path | None) -> dict[str, dict[str, Any]]:
    cleaned, _errors = _parse_overlay(data_dir)
    return cleaned


def apply_ssh_overlay(sites: tuple[Site, ...], data_dir: Path | None = None) -> tuple[Site, ...]:
    overlay = load_overlay(data_dir)
    updated: list[Site] = []
    for site in sites:
        row = overlay.get(site.id)
        if not row:
            updated.append(site)
            continue
        updated.append(replace(site, **row))
    return tuple(updated)


def effective_identity(site: Site, data_dir: Path | None = None) -> str | None:
    """Return the stored per-site key only; never resolve external identities."""
    path = stored_key_path(data_dir, site.id)
    if path.is_file() and not path.is_symlink():
        return str(path)
    return None


def describe_ssh(
    site: Site,
    registry_site: Site,
    data_dir: Path | None,
    *,
    include_paths: bool,
) -> dict[str, Any]:
    base = data_dir_path(data_dir)
    cleaned, errors = _parse_overlay(base)
    row = cleaned.get(site.id, {})
    stored = stored_key_path(base, site.id).is_file()
    host_key = site.host_public_key.strip()
    view: dict[str, Any] = {
        "site_id": site.id,
        "files_root": site.files_root,
        "registry_files_root": registry_site.files_root,
        "files_root_source": "overlay" if "files_root" in row else "registry",
        "ssh_host": site.ssh_host,
        "registry_ssh_host": registry_site.ssh_host,
        "ssh_host_source": "overlay" if "ssh_host" in row else "registry",
        "ssh_user": site.ssh_user,
        "ssh_port": site.ssh_port,
        "ssh_key_stored": stored,
        "identity_present": stored,
        "host_key_present": bool(host_key),
        "overlay_error": errors.get(site.id, ""),
        "mock": env_flag("AZOM_USE_MOCK"),
        "ssh_enabled": env_flag("AZOM_WP_FILES_SSH") and not env_flag("AZOM_USE_MOCK"),
        "live_calls": False,
    }
    if include_paths:
        view["host_public_key"] = host_key
        view["host_key_fingerprint"] = host_key_fingerprint(host_key)
    else:
        view["host_public_key"] = ""
        view["host_key_fingerprint"] = ""
    return view


def save_ssh(
    data_dir: Path | None,
    site: Site,
    *,
    files_root: str,
    ssh_host: str,
    ssh_user: str,
    ssh_port: str,
    ssh_identity_file: str,
    private_key: str,
    clear_key: bool,
    global_user: str,
    global_port: str,
    global_identity_file: str,
    host_public_key: str = "",
) -> dict[str, Any]:
    """Validate every field, then write. No connection is opened."""
    base = data_dir_path(data_dir)
    with _PROCESS_LOCK:
        fd = _acquire_file_lock(base)
        try:
            return _save_locked(
                base,
                site,
                files_root=files_root,
                ssh_host=ssh_host,
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                ssh_identity_file=ssh_identity_file,
                private_key=private_key,
                clear_key=clear_key,
                global_user=global_user,
                global_port=global_port,
                global_identity_file=global_identity_file,
                host_public_key=host_public_key,
            )
        finally:
            _release_file_lock(fd)


def _save_locked(
    base: Path,
    site: Site,
    *,
    files_root: str,
    ssh_host: str,
    ssh_user: str,
    ssh_port: str,
    ssh_identity_file: str,
    private_key: str,
    clear_key: bool,
    global_user: str,
    global_port: str,
    global_identity_file: str,
    host_public_key: str,
) -> dict[str, Any]:
    overlay = load_overlay(base)
    previous = dict(overlay.get(site.id, {}))
    new_row = _validated_row(
        site.id,
        files_root=files_root,
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        ssh_identity_file=ssh_identity_file,
        host_public_key=host_public_key,
    )
    if new_row.get("files_root") == site.files_root:
        new_row.pop("files_root", None)
    if new_row.get("ssh_host") == site.ssh_host:
        new_row.pop("ssh_host", None)
    key_text = _validated_key(private_key, clear_key=clear_key)
    old_user = str(previous.get("ssh_user", site.ssh_user) or "").strip()
    new_user = str(new_row.get("ssh_user", site.ssh_user) or "").strip()
    key_path = stored_key_path(base, site.id)
    try:
        saved_key_stat = key_path.lstat()
        has_saved_key = True
    except FileNotFoundError:
        saved_key_stat = None
        has_saved_key = False
    except OSError as exc:
        raise SettingsRejected(_KEY_WRITE) from exc
    if old_user.casefold() != new_user.casefold() and has_saved_key:
        if key_text is None:
            raise SettingsRejected(
                "När WordPress SSH-användaren byts måste du skicka en ny, unik nyckel för läsarkontot."
            )
        if saved_key_stat is not None and stat.S_ISREG(saved_key_stat.st_mode):
            try:
                old_key_digest = hashlib.sha256(key_path.read_bytes().strip()).digest()
            except OSError as exc:
                raise SettingsRejected(_KEY_WRITE) from exc
            new_key_digest = hashlib.sha256(key_text.strip().encode("utf-8")).digest()
            if hmac.compare_digest(old_key_digest, new_key_digest):
                raise SettingsRejected(
                    "Använd en annan nyckel än den som var kopplad till det tidigare SSH-kontot."
                )
    if any(str(value or "").strip() for value in (global_user, global_port, global_identity_file)):
        raise SettingsRejected("WordPress SFTP cannot change global SSH settings.")
    changed = [name for name in _FIELDS if previous.get(name) != new_row.get(name)]
    staged = dict(overlay)
    if new_row:
        staged[site.id] = new_row
    else:
        staged.pop(site.id, None)
    tmp_key: Path | None = None
    try:
        if key_text is not None:
            tmp_key = _write_private_temp(key_path, key_text)
        _write_overlay(base, staged)
        if tmp_key is not None:
            _install_private(tmp_key, key_path)
            tmp_key = None
            changed.append("ssh_key")
        elif clear_key and key_path.is_file():
            _clear_private(key_path)
            changed.append("ssh_key")
        notice = None
    except SettingsRejected:
        if tmp_key is not None:
            _unlink_quiet(tmp_key)
        raise
    stored = key_path.is_file()
    return {
        "changed": changed,
        "identity_present": stored,
        "global_notice": notice,
    }


def _parse_overlay(data_dir: Path | None) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    path = overlay_file(data_dir)
    if not path.is_file():
        return {}, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(_UNREADABLE) from exc
    if not isinstance(raw, dict) or set(raw) - {"sites"}:
        raise ValueError(_UNREADABLE)
    sites = raw.get("sites", {})
    if not isinstance(sites, dict):
        raise ValueError(_UNREADABLE)
    cleaned: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for site_id, row in sites.items():
        if not isinstance(site_id, str) or _SITE_ID.fullmatch(site_id) is None:
            continue
        if not isinstance(row, dict):
            errors[site_id] = _ROW
            continue
        try:
            cleaned[site_id] = _changes_from_row(site_id, row)
        except ValueError:
            errors[site_id] = _ROW
    return cleaned, errors


def _changes_from_row(site_id: str, row: dict[str, Any]) -> dict[str, Any]:
    if set(row) - set(_FIELDS):
        raise ValueError(_ROW)
    changes: dict[str, Any] = {}
    if row.get("files_root"):
        root = _files_root(row["files_root"], site_id=site_id)
        if not root or not files_root_narrow(root):
            raise ValueError(_ROW)
        changes["files_root"] = root
    if row.get("ssh_host"):
        host = _ssh_host(row["ssh_host"], site_id=site_id)
        if not host:
            raise ValueError(_ROW)
        changes["ssh_host"] = host
    if "ssh_user" in row:
        if not isinstance(row["ssh_user"], str):
            raise ValueError(_ROW)
        user = row["ssh_user"].strip()
        if user and (not user_ok(user) or not user.casefold().endswith("-wp-reader")):
            raise ValueError(_ROW)
        if user:
            changes["ssh_user"] = user
    if "ssh_port" in row:
        port = row["ssh_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(_ROW)
        changes["ssh_port"] = port
    if "host_public_key" in row:
        raw_key = row["host_public_key"]
        if not isinstance(raw_key, str) or _looks_like_key(raw_key):
            raise ValueError(_ROW)
        public = raw_key.strip()
        if public and not host_key_ok(public):
            raise ValueError(_ROW)
        if public:
            changes["host_public_key"] = public
    for key, value in row.items():
        if key == "ssh_port":
            continue
        if not isinstance(value, str) or _looks_like_key(value):
            raise ValueError(_ROW)
    return changes


def _validated_row(
    site_id: str,
    *,
    files_root: str,
    ssh_host: str,
    ssh_user: str,
    ssh_port: str,
    ssh_identity_file: str,
    host_public_key: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    root_text = str(files_root or "").strip()
    if root_text:
        try:
            root = _files_root(root_text, site_id=site_id)
        except ValueError as exc:
            raise SettingsRejected(_ROOT) from exc
        if not root:
            raise SettingsRejected(_ROOT)
        if not files_root_narrow(root):
            raise SettingsRejected(_ROOT_BROAD)
        row["files_root"] = root
    host_text = str(ssh_host or "").strip()
    if host_text:
        try:
            host = _ssh_host(host_text, site_id=site_id)
        except ValueError as exc:
            raise SettingsRejected(_HOST) from exc
        if not host:
            raise SettingsRejected(_HOST)
        row["ssh_host"] = host
    user = str(ssh_user or "").strip()
    if user:
        if not user_ok(user) or not user.casefold().endswith("-wp-reader"):
            raise SettingsRejected("WordPress SFTP kräver en dedikerad *-wp-reader-användare.")
        row["ssh_user"] = user
    port_text = str(ssh_port or "").strip()
    if port_text:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise SettingsRejected(_PORT)
        row["ssh_port"] = int(port_text)
    identity = str(ssh_identity_file or "").strip()
    if identity:
        raise SettingsRejected(_IDENTITY_MSG)
    public = str(host_public_key or "").strip()
    if public:
        if not host_key_ok(public):
            raise SettingsRejected(_HOST_KEY)
        row["host_public_key"] = public
    return row


def _validated_key(private_key: str, *, clear_key: bool) -> str | None:
    text = str(private_key or "").strip()
    if text and clear_key:
        raise SettingsRejected(_KEY_CHOICE)
    if not text:
        return None
    if "\x00" in text or len(text.encode("utf-8")) > _MAX_KEY_BYTES or "PRIVATE KEY" not in text:
        raise SettingsRejected(_KEY)
    if _key_is_encrypted(text):
        raise SettingsRejected(_KEY)
    return text


def _key_is_encrypted(text: str) -> bool:
    """True only when the text positively says the key is encrypted."""
    if "ENCRYPTED PRIVATE KEY" in text:
        return True
    if "Proc-Type:" in text and "ENCRYPTED" in text:
        return True
    lines: list[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("-----") or ":" in raw:
            continue
        lines.append(raw)
    try:
        blob = base64.b64decode("".join(lines), validate=True)
    except (ValueError, TypeError):
        return False
    magic = b"openssh-key-v1\x00"
    if not blob.startswith(magic):
        return False
    try:
        cipher, _rest = _read_ssh_string(blob[len(magic) :])
    except ValueError:
        return False
    return cipher not in {b"", b"none"}


def _read_ssh_string(blob: bytes) -> tuple[bytes, bytes]:
    if len(blob) < 4:
        raise ValueError("short")
    size = int.from_bytes(blob[:4], "big")
    if size > len(blob) - 4:
        raise ValueError("size")
    return blob[4 : 4 + size], blob[4 + size :]


def _looks_like_key(value: str) -> bool:
    return "\x00" in value or "PRIVATE KEY" in value or "-----BEGIN" in value


def _write_overlay(data_dir: Path, sites: dict[str, dict[str, Any]]) -> None:
    payload = {"sites": sites}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if _looks_like_key(text):
        raise SettingsRejected(_KEY)
    path = overlay_file(data_dir)
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        _unlink_quiet(tmp)
        raise SettingsRejected(_SAVE) from exc


def _write_private_temp(path: Path, text: str) -> Path:
    data = text.encode("utf-8")
    if not data.endswith(b"\n"):
        data += b"\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_stat = path.parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
            raise SettingsRejected(_KEY_WRITE)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
            parent_stat = path.parent.lstat()
            if stat.S_IMODE(parent_stat.st_mode) & 0o077:
                raise SettingsRejected(_KEY_WRITE)
        try:
            tmp_stat = tmp.lstat()
        except FileNotFoundError:
            tmp_stat = None
        if tmp_stat is not None:
            if not stat.S_ISREG(tmp_stat.st_mode) or stat.S_ISLNK(tmp_stat.st_mode):
                raise SettingsRejected(_KEY_WRITE)
            tmp.unlink()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
    except SettingsRejected:
        raise
    except OSError as exc:
        raise SettingsRejected(_KEY_WRITE) from exc
    try:
        _write_all(fd, data)
    except OSError as exc:
        os.close(fd)
        _unlink_quiet(tmp)
        raise SettingsRejected(_KEY_WRITE) from exc
    os.close(fd)
    _chmod_mode(tmp, 0o600)
    return tmp


def _install_private(tmp: Path, path: Path) -> None:
    try:
        tmp.replace(path)
    except OSError as exc:
        _unlink_quiet(tmp)
        raise SettingsRejected(_KEY_WRITE) from exc
    _chmod_mode(path, 0o600)
    _chmod_mode(path.parent, 0o700)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise SettingsRejected(_KEY_WRITE)
        view = view[written:]


def _clear_private(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".tmp").unlink(missing_ok=True)
    except OSError as exc:
        raise SettingsRejected(_KEY_CLEAR) from exc


def _chmod_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _acquire_file_lock(data_dir: Path) -> int:
    path = data_dir / "wp_ssh.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SettingsRejected(_SAVE) from exc
    _chmod_mode(path, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(fd)
        raise SettingsRejected(_SAVE) from exc
    return fd


def _release_file_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)
