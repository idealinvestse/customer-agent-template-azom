"""Dedicated, out-of-band SFTP deploy & rollback transport (``*-wp-deployer``).

Strictly separate from read-only ``SshFileTransport`` (``*-wp-reader``):
- Requires ``AZOM_WP_DEPLOY_SSH=1``, ``AZOM_USE_MOCK=0``, and ``AZOM_WP_DEPLOY_KILL`` unset.
- Requires dedicated account ending in ``-wp-deployer``.
- Requires separate per-site key under ``AZOM_DATA_DIR/ssh-deploy-keys/<site_id>``
  whose SHA-256 digest differs from ``AZOM_DATA_DIR/ssh-keys/<site_id>``.
- Performs atomic temp-file upload + readback SHA-256 verification + ``posix_rename``
  + optional post-deploy HTTP health check with automatic SFTP rollback on failure.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import posixpath
import re
import socket
import stat
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import paramiko
from paramiko.hostkeys import HostKeyEntry
from paramiko.sftp_client import SFTPClient
from paramiko.transport import Transport

from ecom_ops.integrations.wp.sftp_transport import _pinned_host_key_types, host_allowed
from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.config import is_mock_mode
from ecom_ops.wp_ops.credentials import redact_secret_text
from ecom_ops.wp_ops.file_paths import (
    SEGMENT_RE,
    SLUG_RE,
    PathRejected,
    TransportError,
    sensitive_relative,
)
from ecom_ops.wp_ops.models import SITE_ID_RE, Site, files_root_narrow
from ecom_ops.wp_ops.plugin_policy import is_bridge_plugin
from ecom_ops.wp_ops.ssh_settings import _key_identity_digest, host_key_ok, stored_key_path, user_ok

_CONNECT_TIMEOUT = 10
_CHANNEL_TIMEOUT = 15
_MAX_DEPLOY_BYTES = 65536
_MAX_DEBUG_LOG_BYTES = 16384
_QUARANTINE_MARKER = ".azom-quarantine-"
_QUARANTINE_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.azom-quarantine-[a-f0-9]{8}$"
)


def deploy_ssh_enabled() -> bool:
    if is_mock_mode():
        return False
    if env_flag("AZOM_WP_DEPLOY_KILL"):
        return False
    return env_flag("AZOM_WP_DEPLOY_SSH")


def stored_deploy_key_path(data_dir: Path, site_id: str) -> Path:
    if not SITE_ID_RE.fullmatch(site_id):
        raise TransportError("SshDeployKeyInvalid")
    root = Path(data_dir).resolve() / "ssh-deploy-keys"
    path = root / site_id
    if path.parent != root:
        raise TransportError("SshDeployKeyInvalid")
    return path


class SshDeployTransport:
    """Atomic, WSOD-proof SFTP deploy & rollback transport for ``*-wp-deployer``."""

    is_ssh: bool = True

    def __init__(
        self,
        site: Site,
        data_dir: Path,
        *,
        deploy_user: str | None = None,
        deploy_host: str | None = None,
        deploy_port: int | None = None,
        files_root: str | None = None,
        host_public_key: str | None = None,
    ) -> None:
        self.site = site
        self.data_dir = Path(data_dir)
        from ecom_ops.wp_ops.ssh_settings import load_deploy_overlay

        overlay = load_deploy_overlay(self.data_dir).get(site.id, {})
        self.deploy_user = str(
            deploy_user if deploy_user is not None else overlay.get("deploy_ssh_user", "")
        ).strip()
        self.deploy_host = str(
            deploy_host if deploy_host is not None else (overlay.get("ssh_host") or site.ssh_host)
        ).strip().lower().rstrip(".")
        self.deploy_port = int(
            deploy_port if deploy_port is not None else (overlay.get("ssh_port") or site.ssh_port or 22)
        )
        self.files_root = str(
            files_root if files_root is not None else (overlay.get("files_root") or site.files_root)
        ).strip().rstrip("/")
        self.host_public_key = str(
            host_public_key
            if host_public_key is not None
            else (overlay.get("host_public_key") or site.host_public_key)
        ).strip()

    def _validate_config(self) -> tuple[str, str, int]:
        if not deploy_ssh_enabled():
            raise TransportError("SshDeployDisabled")
        if not host_allowed(self.deploy_host):
            raise TransportError("SshHostRefused")
        if not host_key_ok(self.host_public_key):
            raise TransportError("SshHostKeyMissing")
        if not user_ok(self.deploy_user) or self.deploy_user.casefold() == "root":
            raise TransportError("SshUserRefused")
        if not self.deploy_user.casefold().endswith("-wp-deployer"):
            raise TransportError("SshDeployUserRequired")
        if not 1 <= self.deploy_port <= 65535:
            raise TransportError("SshFailed")
        if not files_root_narrow(self.files_root):
            raise TransportError("PathRejected")
        return self.deploy_host, self.deploy_user, self.deploy_port

    def _load_deploy_key(self) -> paramiko.PKey:
        key_path = stored_deploy_key_path(self.data_dir, self.site.id)
        try:
            kstat = key_path.lstat()
            dstat = key_path.parent.lstat()
        except OSError as exc:
            raise TransportError("SshDeployIdentityMissing") from exc
        if not stat.S_ISDIR(dstat.st_mode) or stat.S_ISLNK(dstat.st_mode):
            raise TransportError("SshIdentityRejected")
        if os.name != "nt" and stat.S_IMODE(dstat.st_mode) & 0o077:
            raise TransportError("SshIdentityPermissions")
        if not stat.S_ISREG(kstat.st_mode) or key_path.is_symlink():
            raise TransportError("SshIdentityRejected")
        if os.name != "nt" and stat.S_IMODE(kstat.st_mode) & 0o077:
            raise TransportError("SshIdentityPermissions")
        # Never allow reusing the read-only *-wp-reader private key (parsed key identity, fail-closed on OSError).
        reader_key = stored_key_path(self.data_dir, self.site.id)
        if reader_key.exists() or reader_key.is_symlink():
            try:
                r_dig = _key_identity_digest(reader_key.read_bytes())
                d_dig = _key_identity_digest(key_path.read_bytes())
            except OSError as exc:
                raise TransportError("SshDeployKeyCheckFailed") from exc
            if hmac.compare_digest(r_dig, d_dig):
                raise TransportError("SshDeployKeyReuseRefused")
        try:
            return paramiko.PKey.from_path(key_path)
        except (OSError, paramiko.SSHException, ValueError) as exc:
            raise TransportError("SshIdentityRejected") from exc

    def _parse_host_key(self, host: str) -> paramiko.PKey:
        try:
            entry = HostKeyEntry.from_line(f"{host} {self.host_public_key}", None)
        except (TypeError, ValueError, paramiko.SSHException) as exc:
            raise TransportError("SshHostKeyMissing") from exc
        if not entry or not entry.key:
            raise TransportError("SshHostKeyMissing")
        return entry.key

    @contextmanager
    def _session(self):
        host, user, port = self._validate_config()
        pkey = self._load_deploy_key()
        expected = self._parse_host_key(host)
        sock: socket.socket | None = None
        transport: Transport | None = None
        sftp: SFTPClient | None = None
        try:
            sock = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT)
            transport = Transport(sock)
            transport.get_security_options().key_types = _pinned_host_key_types(expected)
            transport.banner_timeout = _CONNECT_TIMEOUT
            transport.auth_timeout = _CONNECT_TIMEOUT
            transport.start_client(timeout=_CONNECT_TIMEOUT)
            actual = transport.get_remote_server_key()
            if actual.get_name() != expected.get_name() or actual.asbytes() != expected.asbytes():
                raise TransportError("SshHostKeyMismatch")
            transport.auth_publickey(user, pkey)
            if not transport.is_authenticated():
                raise TransportError("SshAuthenticationFailed")
            sftp = SFTPClient.from_transport(transport)
            if sftp is None:
                raise TransportError("SftpUnavailable")
            sftp.get_channel().settimeout(_CHANNEL_TIMEOUT)
            yield sftp
        except TransportError:
            raise
        except (OSError, EOFError, TimeoutError, paramiko.SSHException) as exc:
            raise TransportError("SshFailed") from exc
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
            elif sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _validate_target_path(self, absolute_file: str) -> str:
        from ecom_ops.integrations.wp.sftp_transport import SshFileTransport

        dummy = SshFileTransport(
            Site(
                id=self.site.id,
                display_name=self.site.display_name,
                canonical_url=self.site.canonical_url,
                environment=self.site.environment,
                business_owner=self.site.business_owner,
                hosting_provider=self.site.hosting_provider,
                wordpress_version=self.site.wordpress_version,
                php_version=self.site.php_version,
                woocommerce=self.site.woocommerce,
                critical_urls=self.site.critical_urls,
                maintenance_window=self.site.maintenance_window,
                secret_ref=self.site.secret_ref,
                capabilities=self.site.capabilities,
                alert_confirmations=self.site.alert_confirmations,
                check_interval_seconds=self.site.check_interval_seconds,
                timeout_seconds=self.site.timeout_seconds,
                dedupe_window_seconds=self.site.dedupe_window_seconds,
                retention_days=self.site.retention_days,
                alert_owner=self.site.alert_owner,
                files_root=self.files_root,
                ssh_host=self.deploy_host,
            ),
            self.data_dir,
        )
        return dummy._safe_path(absolute_file, listing=False)

    @staticmethod
    def _ensure_remote_dir(sftp: SFTPClient, remote_dir: str) -> None:
        parts = [p for p in remote_dir.split("/") if p]
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}"
            try:
                attrs = sftp.lstat(cur)
                if stat.S_ISLNK(getattr(attrs, "st_mode", 0)):
                    raise PathRejected("remote symlink")
            except FileNotFoundError:
                sftp.mkdir(cur, mode=0o755)
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.ENOENT:
                    sftp.mkdir(cur, mode=0o755)
                else:
                    raise

    @staticmethod
    def _atomic_put(
        sftp: SFTPClient,
        abs_path: str,
        data: bytes,
        *,
        mode: int = 0o644,
    ) -> None:
        if len(data) > _MAX_DEPLOY_BYTES:
            raise TransportError("FileTooLarge")
        parent = posixpath.dirname(abs_path)
        SshDeployTransport._ensure_remote_dir(sftp, parent)
        tmp_path = f"{abs_path}.azom-tmp-{uuid.uuid4().hex[:10]}"
        try:
            with sftp.open(tmp_path, "wb") as fh:
                fh.write(data)
            if hasattr(sftp, "chmod"):
                try:
                    sftp.chmod(tmp_path, mode)
                except OSError:
                    pass
            with sftp.open(tmp_path, "rb") as fh:
                readback = fh.read(_MAX_DEPLOY_BYTES + 1)
            if hashlib.sha256(readback).hexdigest() != hashlib.sha256(data).hexdigest():
                raise TransportError("DigestMismatch")
            try:
                sftp.posix_rename(tmp_path, abs_path)
            except NotImplementedError:
                sftp.rename(tmp_path, abs_path)
            except OSError as exc:
                if getattr(exc, "errno", None) in {
                    getattr(errno, "EOPNOTSUPP", -1),
                    getattr(errno, "ENOSYS", -1),
                }:
                    try:
                        sftp.remove(abs_path)
                    except OSError:
                        pass
                    sftp.rename(tmp_path, abs_path)
                else:
                    raise
        except Exception:
            try:
                sftp.remove(tmp_path)
            except OSError:
                pass
            raise

    def atomic_apply_bundle(
        self,
        changes: list[tuple[str, str, bytes | None]],
        *,
        health_check_fn: Callable[[], bool] | None = None,
        expected_before_hashes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Apply ``[(op, abs_path, bytes_or_none)]`` atomically over SFTP with rollback on failure."""
        validated: list[tuple[str, str, bytes | None]] = []
        for op, abs_path, payload in changes:
            safe = self._validate_target_path(abs_path)
            validated.append((op, safe, payload))

        expected_hashes = dict(expected_before_hashes or {})
        before_bytes: dict[str, bytes] = {}
        remote_modes: dict[str, int] = {}
        created_paths: list[str] = []
        with self._session() as sftp:
            for op, abs_path, _payload in validated:
                try:
                    if hasattr(sftp, "lstat"):
                        if op != "create":
                            self._check_chain_no_symlink(sftp, posixpath.dirname(abs_path))
                        attrs = sftp.lstat(abs_path)
                        if attrs is not None and stat.S_ISLNK(getattr(attrs, "st_mode", 0)):
                            raise PathRejected("Remote symlink target rejected")
                        st_mode = stat.S_IMODE(getattr(attrs, "st_mode", 0) or 0)
                        remote_modes[abs_path] = st_mode or 0o644
                    with sftp.open(abs_path, "rb") as fh:
                        prev_data = fh.read(_MAX_DEPLOY_BYTES + 1)
                    if len(prev_data) > _MAX_DEPLOY_BYTES:
                        raise TransportError("FileTooLarge")
                    if op == "create":
                        raise TransportError("FileExistsConflict")
                    before_bytes[abs_path] = prev_data
                except (PathRejected, TransportError):
                    raise
                except OSError:
                    if op == "create":
                        created_paths.append(abs_path)
                    elif op == "write":
                        raise TransportError("FileMissing")

                if abs_path in expected_hashes and expected_hashes[abs_path]:
                    actual_sha = hashlib.sha256(before_bytes.get(abs_path, b"")).hexdigest()
                    if actual_sha != expected_hashes[abs_path]:
                        raise TransportError("StaleFile")

            try:
                for op, abs_path, payload in validated:
                    if op == "delete":
                        if abs_path in before_bytes:
                            if hasattr(sftp, "lstat"):
                                self._check_chain_no_symlink(sftp, posixpath.dirname(abs_path))
                                self._lstat_no_symlink(sftp, abs_path)
                            sftp.remove(abs_path)
                    else:
                        assert payload is not None
                        self._atomic_put(
                            sftp,
                            abs_path,
                            payload,
                            mode=remote_modes.get(abs_path, 0o644),
                        )
                if health_check_fn is not None and not health_check_fn():
                    raise TransportError("PostDeployHealthFailed")
            except Exception as exc:
                # Automatic SFTP rollback on write error or post-deploy HTTP 5xx.
                try:
                    for abs_path, prev in before_bytes.items():
                        self._atomic_put(
                            sftp,
                            abs_path,
                            prev,
                            mode=remote_modes.get(abs_path, 0o644),
                        )
                    for created_abs in created_paths:
                        try:
                            if hasattr(sftp, "lstat"):
                                self._check_chain_no_symlink(sftp, posixpath.dirname(created_abs))
                                self._lstat_no_symlink(sftp, created_abs)
                            sftp.remove(created_abs)
                        except OSError as rm_exc:
                            if not isinstance(rm_exc, FileNotFoundError) and getattr(rm_exc, "errno", None) != errno.ENOENT:
                                raise
                except Exception:
                    try:
                        with self._session() as rb_sftp:
                            for abs_path, prev in before_bytes.items():
                                self._atomic_put(
                                    rb_sftp,
                                    abs_path,
                                    prev,
                                    mode=remote_modes.get(abs_path, 0o644),
                                )
                            for created_abs in created_paths:
                                try:
                                    if hasattr(rb_sftp, "lstat"):
                                        self._check_chain_no_symlink(rb_sftp, posixpath.dirname(created_abs))
                                        self._lstat_no_symlink(rb_sftp, created_abs)
                                    rb_sftp.remove(created_abs)
                                except OSError:
                                    pass
                    except Exception:
                        pass
                if isinstance(exc, (TransportError, PathRejected)):
                    raise
                raise TransportError("SftpDeployFailed") from exc

        return {
            "ok": True,
            "files_written": len(validated),
            "before_bytes": before_bytes,
            "created_paths": created_paths,
        }

    def rollback_bundle(
        self,
        before_files: dict[str, bytes],
        created_paths: list[str],
    ) -> dict[str, Any]:
        """Restore original file bytes and remove created files over SFTP (WSOD-proof)."""
        safe_before = {self._validate_target_path(p): data for p, data in before_files.items()}
        safe_created = [self._validate_target_path(p) for p in created_paths]
        with self._session() as sftp:
            for abs_path, data in safe_before.items():
                mode = 0o644
                if hasattr(sftp, "lstat"):
                    try:
                        attrs = sftp.lstat(abs_path)
                        if attrs is not None and stat.S_ISLNK(getattr(attrs, "st_mode", 0)):
                            raise PathRejected("Remote symlink target rejected")
                        mode = stat.S_IMODE(getattr(attrs, "st_mode", 0) or 0) or 0o644
                    except OSError:
                        pass
                self._atomic_put(sftp, abs_path, data, mode=mode)
            for abs_path in safe_created:
                try:
                    if hasattr(sftp, "lstat"):
                        self._check_chain_no_symlink(sftp, posixpath.dirname(abs_path))
                        self._lstat_no_symlink(sftp, abs_path)
                    sftp.remove(abs_path)
                except PathRejected:
                    raise
                except OSError as exc:
                    if not isinstance(exc, FileNotFoundError) and getattr(exc, "errno", None) != errno.ENOENT:
                        raise TransportError("SftpRollbackFailed") from exc
        return {
            "ok": True,
            "restored": len(safe_before),
            "removed": len(safe_created),
        }

    # --- Out-of-band WSOD plugin / MU-plugin quarantine & debug.log tail --- #

    @staticmethod
    def _ensure_plugin_mutate_allowed() -> None:
        if env_flag("AZOM_WP_DEPLOY_KILL"):
            raise TransportError("DeployKillSwitchActive")
        if env_flag("AZOM_WP_PLUGIN_MUTATE_KILL"):
            raise TransportError("PluginMutateKillSwitchActive")

    @staticmethod
    def _posix_rename_safe(sftp: SFTPClient, src: str, dst: str) -> None:
        try:
            sftp.posix_rename(src, dst)
        except NotImplementedError:
            sftp.rename(src, dst)
        except OSError as exc:
            if getattr(exc, "errno", None) in {
                getattr(errno, "EOPNOTSUPP", -1),
                getattr(errno, "ENOSYS", -1),
            }:
                sftp.rename(src, dst)
            else:
                raise

    @staticmethod
    def _lstat_no_symlink(sftp: SFTPClient, path: str) -> Any:
        attrs = sftp.lstat(path)
        mode = getattr(attrs, "st_mode", None)
        if mode is None or stat.S_ISLNK(mode):
            raise PathRejected("remote symlink")
        return attrs

    def _check_chain_no_symlink(self, sftp: SFTPClient, abs_path: str) -> Any:
        segments = [seg for seg in abs_path.split("/") if seg]
        if not segments:
            raise PathRejected("root")
        cur = ""
        attrs: Any = None
        for idx, seg in enumerate(segments):
            cur = f"{cur}/{seg}"
            attrs = self._lstat_no_symlink(sftp, cur)
            mode = getattr(attrs, "st_mode", 0)
            if idx < len(segments) - 1 and not stat.S_ISDIR(mode):
                raise PathRejected("remote parent")
        return attrs

    @staticmethod
    def _validate_plugin_slug(slug: str) -> str:
        clean = str(slug or "").strip()
        if is_bridge_plugin(clean):
            raise TransportError("BridgeSelfProtectionRefused")
        if not SLUG_RE.fullmatch(clean):
            raise PathRejected("slug")
        return clean

    @staticmethod
    def _validate_mu_target(slug_or_file: str) -> str:
        clean = str(slug_or_file or "").strip()
        if not clean or "/" in clean or "\\" in clean or clean in {".", ".."}:
            raise PathRejected("relative")
        if is_bridge_plugin(clean):
            raise TransportError("BridgeSelfProtectionRefused")
        if sensitive_relative(clean) or _QUARANTINE_MARKER in clean:
            raise PathRejected("segment")
        if clean.lower().endswith(".php"):
            if SEGMENT_RE.fullmatch(clean) is None:
                raise PathRejected("segment")
            return clean
        if not SLUG_RE.fullmatch(clean):
            raise PathRejected("slug")
        return clean

    def _quarantine_entry(self, folder: str, target_name: str, *, reason: str = "") -> dict[str, Any]:
        self._ensure_plugin_mutate_allowed()
        if not files_root_narrow(self.files_root):
            raise PathRejected("root")
        parent = f"{self.files_root}/wp-content/{folder}"
        src = f"{parent}/{target_name}"
        qid = uuid.uuid4().hex[:8]
        q_name = f"{target_name}{_QUARANTINE_MARKER}{qid}"
        dst = f"{parent}/{q_name}"

        with self._session() as sftp:
            self._check_chain_no_symlink(sftp, parent)
            try:
                attrs = self._lstat_no_symlink(sftp, src)
            except PathRejected:
                raise
            except OSError as exc:
                raise TransportError("FileMissing") from exc
            mode = getattr(attrs, "st_mode", 0)
            if folder == "plugins" and not stat.S_ISDIR(mode):
                raise PathRejected("remote directory")
            if folder == "mu-plugins" and not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise PathRejected("remote target")
            self._posix_rename_safe(sftp, src, dst)

        return {
            "ok": True,
            "folder": folder,
            "target": target_name,
            "quarantine_name": q_name,
            "quarantine_id": qid,
            "reason": str(reason or "")[:160],
        }

    def _unquarantine_entry(
        self, folder: str, target_name: str, *, quarantine_name: str = ""
    ) -> dict[str, Any]:
        self._ensure_plugin_mutate_allowed()
        if not files_root_narrow(self.files_root):
            raise PathRejected("root")
        parent = f"{self.files_root}/wp-content/{folder}"
        dst = f"{parent}/{target_name}"

        with self._session() as sftp:
            self._check_chain_no_symlink(sftp, parent)
            try:
                sftp.lstat(dst)
                raise TransportError("FileExistsConflict")
            except TransportError:
                raise
            except OSError:
                pass

            chosen = str(quarantine_name or "").strip()
            prefix = f"{target_name}{_QUARANTINE_MARKER}"
            if chosen:
                if (
                    "/" in chosen
                    or "\\" in chosen
                    or not chosen.startswith(prefix)
                    or _QUARANTINE_NAME_RE.fullmatch(chosen) is None
                ):
                    raise PathRejected("quarantine_name")
            else:
                names = sorted(str(n) for n in sftp.listdir(parent))
                matches = [
                    n for n in names if n.startswith(prefix) and _QUARANTINE_NAME_RE.fullmatch(n)
                ]
                if not matches:
                    raise TransportError("FileMissing")
                chosen = matches[-1]

            src = f"{parent}/{chosen}"
            try:
                self._lstat_no_symlink(sftp, src)
            except PathRejected:
                raise
            except OSError as exc:
                raise TransportError("FileMissing") from exc
            self._posix_rename_safe(sftp, src, dst)

        return {
            "ok": True,
            "folder": folder,
            "target": target_name,
            "restored_from": chosen,
        }

    def quarantine_plugin(self, slug: str, *, reason: str = "") -> dict[str, Any]:
        """Atomically rename ``wp-content/plugins/<slug>`` to quarantine out-of-band."""
        clean = self._validate_plugin_slug(slug)
        return self._quarantine_entry("plugins", clean, reason=reason)

    def unquarantine_plugin(self, slug: str, *, quarantine_name: str = "") -> dict[str, Any]:
        """Atomically restore a quarantined plugin directory back to ``wp-content/plugins/<slug>``."""
        clean = self._validate_plugin_slug(slug)
        return self._unquarantine_entry("plugins", clean, quarantine_name=quarantine_name)

    def quarantine_mu_plugin(self, slug_or_file: str, *, reason: str = "") -> dict[str, Any]:
        """Atomically rename an MU-plugin file/dir so WordPress stops loading it."""
        clean = self._validate_mu_target(slug_or_file)
        return self._quarantine_entry("mu-plugins", clean, reason=reason)

    def unquarantine_mu_plugin(
        self, slug_or_file: str, *, quarantine_name: str = ""
    ) -> dict[str, Any]:
        """Atomically restore a quarantined MU-plugin file/dir."""
        clean = self._validate_mu_target(slug_or_file)
        return self._unquarantine_entry("mu-plugins", clean, quarantine_name=quarantine_name)

    def list_quarantined(self) -> dict[str, Any]:
        """List quarantined plugin directories and MU-plugins without following symlinks."""
        if not files_root_narrow(self.files_root):
            raise PathRejected("root")
        found: dict[str, list[dict[str, str]]] = {"plugins": [], "mu_plugins": []}
        with self._session() as sftp:
            for folder, key in (("plugins", "plugins"), ("mu-plugins", "mu_plugins")):
                parent = f"{self.files_root}/wp-content/{folder}"
                try:
                    self._check_chain_no_symlink(sftp, parent)
                    names = sorted(str(n) for n in sftp.listdir(parent))
                except (OSError, PathRejected):
                    continue
                for name in names:
                    if _QUARANTINE_NAME_RE.fullmatch(name) is None:
                        continue
                    try:
                        self._lstat_no_symlink(sftp, f"{parent}/{name}")
                    except (OSError, PathRejected):
                        continue
                    orig, _, qid = name.partition(_QUARANTINE_MARKER)
                    found[key].append(
                        {
                            "target": orig,
                            "quarantine_name": name,
                            "quarantine_id": qid,
                            "kind": "plugin" if folder == "plugins" else "mu_plugin",
                        }
                    )
        return {"ok": True, **found}

    def read_debug_log_tail(
        self, *, max_lines: int = 80, max_bytes: int = _MAX_DEBUG_LOG_BYTES
    ) -> dict[str, Any]:
        """Read a bounded, secret/path-scrubbed tail of ``wp-content/debug.log`` out-of-band."""
        if not files_root_narrow(self.files_root):
            raise PathRejected("root")
        cap = min(max(int(max_bytes), 1024), _MAX_DEBUG_LOG_BYTES)
        line_cap = min(max(int(max_lines), 1), 200)
        log_path = f"{self.files_root}/wp-content/debug.log"
        with self._session() as sftp:
            try:
                attrs = self._check_chain_no_symlink(sftp, log_path)
            except PathRejected:
                raise
            except OSError:
                return {"ok": True, "exists": False, "lines": [], "truncated": False, "bytes_read": 0}
            mode = getattr(attrs, "st_mode", 0)
            if not stat.S_ISREG(mode):
                raise PathRejected("remote file")
            size = int(getattr(attrs, "st_size", 0) or 0)
            truncated = size > cap
            with sftp.open(log_path, "rb") as fh:
                if truncated and hasattr(fh, "seek"):
                    fh.seek(max(0, size - cap))
                raw = fh.read(cap)
        text = raw.decode("utf-8", errors="replace")
        if truncated and "\n" in text:
            text = text.split("\n", 1)[1]
        scrubbed_lines: list[str] = []
        root_prefix = self.files_root
        for line in text.splitlines()[-line_cap:]:
            clean = line.replace(root_prefix, "<WP_ROOT>")
            clean = redact_secret_text(clean)
            scrubbed_lines.append(clean[:400])
        return {
            "ok": True,
            "exists": True,
            "lines": scrubbed_lines,
            "truncated": truncated or len(text.splitlines()) > line_cap,
            "bytes_read": len(raw),
        }


__all__ = [
    "SshDeployTransport",
    "deploy_ssh_enabled",
    "stored_deploy_key_path",
]
