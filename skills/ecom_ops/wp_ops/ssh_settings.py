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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ecom_ops.wp_ops.config import data_dir_path, is_mock_mode, ssh_files_enabled
from ecom_ops.wp_ops.locks import advisory_file_lock
from ecom_ops.wp_ops.models import (
    SITE_ID_RE,
    Site,
    files_root_narrow,
    validate_files_root,
    validate_ssh_host,
)
from ecom_ops.wp_ops.store import atomic_write_text

_SITE_ID = SITE_ID_RE
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


@dataclass(frozen=True)
class SshSettingsInput:
    """Validated-on-save input bundle for per-site WordPress SFTP settings."""

    files_root: str
    ssh_host: str
    ssh_user: str
    ssh_port: str
    ssh_identity_file: str
    private_key: str
    clear_key: bool
    global_user: str
    global_port: str
    global_identity_file: str
    host_public_key: str = ""


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
        "mock": is_mock_mode(),
        "ssh_enabled": ssh_files_enabled(),
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
    settings = SshSettingsInput(
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
    with _PROCESS_LOCK:
        try:
            lock_cm = advisory_file_lock(base / "wp_ssh.lock", mode=0o600, raise_on_timeout=True)
            with lock_cm:
                return _save_locked(base, site, settings)
        except SettingsRejected:
            raise
        except OSError as exc:
            raise SettingsRejected(_SAVE) from exc


def _key_identity_digest(key_data: bytes | str) -> bytes:
    """Hash parsed public key bytes when valid, falling back to raw key text SHA-256."""
    raw = key_data.encode("utf-8") if isinstance(key_data, str) else key_data
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        import io

        import paramiko

        pkey = paramiko.PKey.from_private_key(io.StringIO(text))
        return hashlib.sha256(pkey.asbytes()).digest()
    except Exception:
        return hashlib.sha256(text.encode("utf-8")).digest()


def _enforce_user_rotation_key_policy(
    key_path: Path,
    *,
    old_user: str,
    new_user: str,
    key_text: str | None,
) -> None:
    try:
        saved_key_stat = key_path.lstat()
        has_saved_key = True
    except FileNotFoundError:
        saved_key_stat = None
        has_saved_key = False
    except OSError as exc:
        raise SettingsRejected(_KEY_WRITE) from exc
    if old_user.casefold() == new_user.casefold() or not has_saved_key:
        return
    if key_text is None:
        raise SettingsRejected(
            "När WordPress SSH-användaren byts måste du skicka en ny, unik nyckel för läsarkontot."
        )
    if saved_key_stat is not None and stat.S_ISREG(saved_key_stat.st_mode):
        try:
            old_key_digest = _key_identity_digest(key_path.read_bytes())
        except OSError as exc:
            raise SettingsRejected(_KEY_WRITE) from exc
        new_key_digest = _key_identity_digest(key_text)
        if hmac.compare_digest(old_key_digest, new_key_digest):
            raise SettingsRejected(
                "Använd en annan nyckel än den som var kopplad till det tidigare SSH-kontot."
            )


def _save_locked(base: Path, site: Site, settings: SshSettingsInput) -> dict[str, Any]:
    overlay = load_overlay(base)
    previous = dict(overlay.get(site.id, {}))
    new_row = _validated_row(site.id, settings)
    if new_row.get("files_root") == site.files_root:
        new_row.pop("files_root", None)
    if new_row.get("ssh_host") == site.ssh_host:
        new_row.pop("ssh_host", None)
    key_text = _validated_key(settings.private_key, clear_key=settings.clear_key)
    old_user = str(previous.get("ssh_user", site.ssh_user) or "").strip()
    new_user = str(new_row.get("ssh_user", site.ssh_user) or "").strip()
    key_path = stored_key_path(base, site.id)
    _enforce_user_rotation_key_policy(key_path, old_user=old_user, new_user=new_user, key_text=key_text)
    deploy_key = stored_deploy_key_file(base, site.id)
    if key_text is not None and (deploy_key.exists() or deploy_key.is_symlink()):
        try:
            deploy_digest = _key_identity_digest(deploy_key.read_bytes())
        except OSError as exc:
            raise SettingsRejected(_KEY_WRITE) from exc
        if hmac.compare_digest(deploy_digest, _key_identity_digest(key_text)):
            raise SettingsRejected(
                "Läsarnyckeln får inte vara samma nyckel som används av *-wp-deployer."
            )
    if any(
        str(value or "").strip()
        for value in (settings.global_user, settings.global_port, settings.global_identity_file)
    ):
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
        elif settings.clear_key and key_path.is_file():
            _clear_private(key_path)
            changed.append("ssh_key")
    except SettingsRejected:
        if tmp_key is not None:
            _unlink_quiet(tmp_key)
        raise
    return {
        "changed": changed,
        "identity_present": key_path.is_file(),
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


def _validate_root_field(raw: Any, *, site_id: str, strict: bool) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        root = validate_files_root(text, site_id=site_id)
    except ValueError as exc:
        if strict:
            raise SettingsRejected(_ROOT) from exc
        raise ValueError(_ROW) from exc
    if not root:
        raise SettingsRejected(_ROOT) if strict else ValueError(_ROW)
    if not files_root_narrow(root):
        raise SettingsRejected(_ROOT_BROAD) if strict else ValueError(_ROW)
    return root


def _validate_host_field(raw: Any, *, site_id: str, strict: bool) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        host = validate_ssh_host(text, site_id=site_id)
    except ValueError as exc:
        if strict:
            raise SettingsRejected(_HOST) from exc
        raise ValueError(_ROW) from exc
    if not host:
        raise SettingsRejected(_HOST) if strict else ValueError(_ROW)
    return host


def _validate_user_field(raw: Any, *, strict: bool) -> str | None:
    if not strict and not isinstance(raw, str):
        raise ValueError(_ROW)
    user = str(raw or "").strip()
    if not user:
        return None
    if not user_ok(user) or not user.casefold().endswith("-wp-reader"):
        if strict:
            raise SettingsRejected("WordPress SFTP kräver en dedikerad *-wp-reader-användare.")
        raise ValueError(_ROW)
    return user


def _validate_port_field(raw: Any, *, strict: bool) -> int | None:
    if strict:
        text = str(raw or "").strip()
        if not text:
            return None
        if not text.isdigit() or not 1 <= int(text) <= 65535:
            raise SettingsRejected(_PORT)
        return int(text)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 65535:
        raise ValueError(_ROW)
    return raw


def _validate_host_key_field(raw: Any, *, strict: bool) -> str | None:
    if not strict and (not isinstance(raw, str) or _looks_like_key(raw)):
        raise ValueError(_ROW)
    public = str(raw or "").strip()
    if not public:
        return None
    if not host_key_ok(public):
        raise SettingsRejected(_HOST_KEY) if strict else ValueError(_ROW)
    return public


def _changes_from_row(site_id: str, row: dict[str, Any]) -> dict[str, Any]:
    if set(row) - set(_FIELDS):
        raise ValueError(_ROW)
    for key, value in row.items():
        if key != "ssh_port" and (not isinstance(value, str) or _looks_like_key(value)):
            raise ValueError(_ROW)
    validators = (
        ("files_root", lambda v: _validate_root_field(v, site_id=site_id, strict=False)),
        ("ssh_host", lambda v: _validate_host_field(v, site_id=site_id, strict=False)),
        ("ssh_user", lambda v: _validate_user_field(v, strict=False)),
        ("ssh_port", lambda v: _validate_port_field(v, strict=False)),
        ("host_public_key", lambda v: _validate_host_key_field(v, strict=False)),
    )
    changes: dict[str, Any] = {}
    for field_name, validator in validators:
        if field_name not in row:
            continue
        cleaned = validator(row[field_name])
        if cleaned is not None:
            changes[field_name] = cleaned
    return changes


def _validated_row(site_id: str, settings: SshSettingsInput) -> dict[str, Any]:
    row: dict[str, Any] = {}
    root = _validate_root_field(settings.files_root, site_id=site_id, strict=True)
    if root:
        row["files_root"] = root
    host = _validate_host_field(settings.ssh_host, site_id=site_id, strict=True)
    if host:
        row["ssh_host"] = host
    user = _validate_user_field(settings.ssh_user, strict=True)
    if user:
        row["ssh_user"] = user
    port = _validate_port_field(settings.ssh_port, strict=True)
    if port is not None:
        row["ssh_port"] = port
    if str(settings.ssh_identity_file or "").strip():
        raise SettingsRejected(_IDENTITY_MSG)
    public = _validate_host_key_field(settings.host_public_key, strict=True)
    if public:
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
    try:
        atomic_write_text(path, text)
    except OSError as exc:
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


# --------------------------------------------------------------------------- #
# Separate *-wp-deployer SFTP overlay (AZOM_DATA_DIR/wp_deploy_ssh.json)
# --------------------------------------------------------------------------- #


def deploy_overlay_file(data_dir: Path | None) -> Path:
    return data_dir_path(data_dir) / "wp_deploy_ssh.json"


def stored_deploy_key_file(data_dir: Path | None, site_id: str) -> Path:
    if not _SITE_ID.fullmatch(site_id):
        raise SettingsRejected(_KEY_WRITE)
    root = data_dir_path(data_dir).resolve() / "ssh-deploy-keys"
    path = root / site_id
    if path.parent != root:
        raise SettingsRejected(_KEY_WRITE)
    return path


_DEPLOY_FIELDS = (
    "deploy_ssh_user",
    "files_root",
    "ssh_host",
    "ssh_port",
    "host_public_key",
)


def _validate_deploy_user_field(raw: Any, *, strict: bool) -> str | None:
    if not strict and not isinstance(raw, str):
        raise ValueError(_ROW)
    user = str(raw or "").strip()
    if not user:
        return None
    if not user_ok(user) or not user.casefold().endswith("-wp-deployer"):
        if strict:
            raise SettingsRejected("Deploy SFTP kräver en dedikerad användare som slutar på -wp-deployer.")
        raise ValueError(_ROW)
    return user


def _deploy_changes_from_row(site_id: str, row: dict[str, Any]) -> dict[str, Any]:
    if set(row) - set(_DEPLOY_FIELDS):
        raise ValueError(_ROW)
    for key, value in row.items():
        if key != "ssh_port" and (not isinstance(value, str) or _looks_like_key(value)):
            raise ValueError(_ROW)
    validators = (
        ("deploy_ssh_user", lambda v: _validate_deploy_user_field(v, strict=False)),
        ("files_root", lambda v: _validate_root_field(v, site_id=site_id, strict=False)),
        ("ssh_host", lambda v: _validate_host_field(v, site_id=site_id, strict=False)),
        ("ssh_port", lambda v: _validate_port_field(v, strict=False)),
        ("host_public_key", lambda v: _validate_host_key_field(v, strict=False)),
    )
    cleaned: dict[str, Any] = {}
    for field_name, validator in validators:
        if field_name not in row:
            continue
        val = validator(row[field_name])
        if val is not None:
            cleaned[field_name] = val
    return cleaned


def load_deploy_overlay(data_dir: Path | None) -> dict[str, dict[str, Any]]:
    path = deploy_overlay_file(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(raw, dict) or set(raw) - {"sites"}:
        return {}
    sites = raw.get("sites", {})
    if not isinstance(sites, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for site_id, row in sites.items():
        if not isinstance(site_id, str) or _SITE_ID.fullmatch(site_id) is None:
            continue
        if not isinstance(row, dict):
            continue
        try:
            parsed_row = _deploy_changes_from_row(site_id, row)
        except ValueError:
            continue
        if parsed_row:
            cleaned[site_id] = parsed_row
    return cleaned


def describe_deploy_ssh(
    site: Site,
    data_dir: Path | None,
    *,
    include_paths: bool,
) -> dict[str, Any]:
    from ecom_ops.integrations.wp.sftp_deploy_transport import deploy_ssh_enabled

    base = data_dir_path(data_dir)
    row = load_deploy_overlay(base).get(site.id, {})
    stored = stored_deploy_key_file(base, site.id).is_file()
    host_key = str(row.get("host_public_key") or site.host_public_key or "").strip()
    return {
        "site_id": site.id,
        "files_root": str(row.get("files_root") or site.files_root or ""),
        "ssh_host": str(row.get("ssh_host") or site.ssh_host or ""),
        "deploy_ssh_user": str(row.get("deploy_ssh_user") or ""),
        "ssh_port": int(row.get("ssh_port") or site.ssh_port or 22),
        "deploy_key_stored": stored,
        "host_key_present": bool(host_key),
        "host_key_fingerprint": host_key_fingerprint(host_key) if include_paths else "",
        "deploy_ssh_enabled": deploy_ssh_enabled(),
    }


def save_deploy_ssh(
    data_dir: Path | None,
    site: Site,
    *,
    deploy_ssh_user: str,
    files_root: str = "",
    ssh_host: str = "",
    ssh_port: str = "",
    host_public_key: str = "",
    private_key: str = "",
    clear_key: bool = False,
    clear_host_key: bool = False,
) -> dict[str, Any]:
    """Validate and save dedicated ``*-wp-deployer`` SFTP settings separately from ``wp_ssh.json``."""
    base = data_dir_path(data_dir)
    user = _validate_deploy_user_field(deploy_ssh_user, strict=True)
    row: dict[str, Any] = {}
    if user:
        row["deploy_ssh_user"] = user
    root = _validate_root_field(files_root, site_id=site.id, strict=True)
    if root and root != site.files_root:
        row["files_root"] = root
    host = _validate_host_field(ssh_host, site_id=site.id, strict=True)
    if host and host != site.ssh_host:
        row["ssh_host"] = host
    port = _validate_port_field(ssh_port, strict=True)
    if port is not None:
        row["ssh_port"] = port
    public = _validate_host_key_field(host_public_key, strict=True)
    if public:
        row["host_public_key"] = public

    key_text = _validated_key(private_key, clear_key=clear_key)
    key_path = stored_deploy_key_file(base, site.id)
    reader_key = stored_key_path(base, site.id)

    with _PROCESS_LOCK:
        try:
            lock_cm = advisory_file_lock(base / "wp_ssh.lock", mode=0o600, raise_on_timeout=True)
            with lock_cm:
                if key_text is not None and (reader_key.exists() or reader_key.is_symlink()):
                    try:
                        reader_digest = _key_identity_digest(reader_key.read_bytes())
                    except OSError as exc:
                        raise SettingsRejected(_KEY_WRITE) from exc
                    if hmac.compare_digest(reader_digest, _key_identity_digest(key_text)):
                        raise SettingsRejected(
                            "Deploy-nyckeln får inte vara samma nyckel som används av *-wp-reader."
                        )
                overlay = load_deploy_overlay(base)
                prev_row = dict(overlay.get(site.id, {}))
                if (
                    "host_public_key" not in row
                    and not clear_host_key
                    and prev_row.get("host_public_key")
                ):
                    row["host_public_key"] = prev_row["host_public_key"]
                if row:
                    overlay[site.id] = row
                else:
                    overlay.pop(site.id, None)
                tmp_key: Path | None = None
                if key_text is not None:
                    tmp_key = _write_private_temp(key_path, key_text)
                text = json.dumps({"sites": overlay}, indent=2, sort_keys=True) + "\n"
                if _looks_like_key(text):
                    if tmp_key is not None:
                        _unlink_quiet(tmp_key)
                    raise SettingsRejected(_KEY)
                try:
                    atomic_write_text(deploy_overlay_file(base), text)
                except Exception:
                    if tmp_key is not None:
                        _unlink_quiet(tmp_key)
                    raise
                if tmp_key is not None:
                    _install_private(tmp_key, key_path)
                elif clear_key and key_path.is_file():
                    _clear_private(key_path)
        except SettingsRejected:
            raise
        except OSError as exc:
            raise SettingsRejected(_SAVE) from exc

    return {"saved": True, "deploy_key_stored": key_path.is_file()}
