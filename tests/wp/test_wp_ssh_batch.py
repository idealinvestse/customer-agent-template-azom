"""Tests for SFTP open_batch single-socket session reuse."""

from __future__ import annotations

import stat
from dataclasses import replace
from types import SimpleNamespace

import paramiko
from _helpers import site_row

from ecom_ops.integrations.wp_files_ssh import SshFileTransport
from ecom_ops.wp_ops.models import parse_site


def test_ssh_open_batch_opens_single_socket_and_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WP_FILES_SSH", "1")

    private = paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "ssh-keys" / "stage"
    key_path.parent.mkdir(parents=True)
    private.write_private_key_file(str(key_path))
    host_key = f"{private.get_name()} {private.get_base64()}"

    site = replace(
        parse_site(site_row()),
        ssh_host="shop.internal",
        ssh_user="azom-wp-reader",
        ssh_port=2222,
        host_public_key=host_key,
    )

    counts = {"socket": 0, "transport": 0, "sftp": 0, "closed": 0}
    remote_files = {
        "/var/www/shop/wp-content/themes/twentytwentyfive/style.css": b"/* style */\n",
        "/var/www/shop/wp-content/themes/twentytwentyfive/functions.php": b"<?php\n",
    }
    remote_dirs = {
        "/var/www/shop/wp-content/themes/twentytwentyfive": ["style.css", "functions.php"],
        "/var/www/shop/wp-content/themes": ["twentytwentyfive"],
    }

    class FakeSock:
        def close(self) -> None:
            return None

    class FakeFile:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def settimeout(self, _timeout: float) -> None:
            return None

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644,
                st_size=len(self._data),
                st_ino=101,
            )

        def read(self, _n: int) -> bytes:
            return self._data

        def close(self) -> None:
            return None

    class FakeChannel:
        def settimeout(self, _timeout: float) -> None:
            return None

    class FakeSFTP:
        def get_channel(self) -> FakeChannel:
            return FakeChannel()

        def lstat(self, path: str) -> SimpleNamespace:
            if path in remote_files:
                return SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o644,
                    st_size=len(remote_files[path]),
                    st_ino=101,
                )
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=0, st_ino=1)

        def listdir_attr(self, path: str) -> list[SimpleNamespace]:
            names = remote_dirs.get(path, [])
            out = []
            for name in names:
                full = f"{path}/{name}"
                is_file = full in remote_files
                out.append(
                    SimpleNamespace(
                        filename=name,
                        st_mode=(stat.S_IFREG | 0o644) if is_file else (stat.S_IFDIR | 0o755),
                    )
                )
            return out

        def open(self, path: str, _mode: str) -> FakeFile:
            return FakeFile(remote_files[path])

        def close(self) -> None:
            counts["closed"] += 1

    class FakeTransport:
        def __init__(self, _sock: object) -> None:
            counts["transport"] += 1
            self.banner_timeout = 0
            self.auth_timeout = 0

        def set_log_channel(self, _name: str) -> None:
            return None

        def start_client(self, timeout: int | None = None) -> None:
            del timeout

        def get_remote_server_key(self) -> paramiko.RSAKey:
            return private

        def auth_publickey(self, _user: str, _key: object) -> None:
            return None

        def is_authenticated(self) -> bool:
            return True

        def close(self) -> None:
            return None

    def fake_connect(*_args, **_kwargs):
        counts["socket"] += 1
        return FakeSock()

    def fake_from_transport(_transport: object) -> FakeSFTP:
        counts["sftp"] += 1
        return FakeSFTP()

    monkeypatch.setattr("ecom_ops.integrations.wp_files_ssh.socket.create_connection", fake_connect)
    monkeypatch.setattr("ecom_ops.integrations.wp_files_ssh.paramiko.Transport", FakeTransport)
    monkeypatch.setattr(
        "ecom_ops.integrations.wp_files_ssh.paramiko.SFTPClient.from_transport",
        staticmethod(fake_from_transport),
    )

    transport = SshFileTransport(site, tmp_path)
    with transport.open_batch() as batch:
        themes = batch.list_dir("/var/www/shop/wp-content/themes")
        files = batch.list_dir("/var/www/shop/wp-content/themes/twentytwentyfive")
        style_bytes = batch.read_bytes("/var/www/shop/wp-content/themes/twentytwentyfive/style.css")
        func_bytes = batch.read_bytes(
            "/var/www/shop/wp-content/themes/twentytwentyfive/functions.php"
        )

    assert themes == ["twentytwentyfive"]
    assert files == ["functions.php", "style.css"]
    assert style_bytes == b"/* style */\n"
    assert func_bytes == b"<?php\n"
    assert counts["socket"] == 1
    assert counts["transport"] == 1
    assert counts["sftp"] == 1
    assert counts["closed"] == 1
