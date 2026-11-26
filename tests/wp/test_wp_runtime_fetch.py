"""Pinned WordPress and PHP fetch. These tests do not touch the network."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from ecom_ops.integrations import wp_runtime_fetch as rf
from ecom_ops.integrations.wp_runtime_fetch import (
    RuntimeFetchError,
    check_runtime,
    download,
    extract_zip,
    install,
    main,
    probe_php,
    resolve_php_from_manifest,
    resolve_runtime_manifest,
    select_pins,
)

WP_VERSION_PHP = b"<?php\n$wp_version = '7.1.5';\n"
PHP_BANNER = "PHP 8.3.37 (cli)"


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _use_synthetic_pins(monkeypatch: pytest.MonkeyPatch, wp_bytes: bytes, php_bytes: bytes) -> None:
    monkeypatch.setattr(rf, "WORDPRESS_SHA1", hashlib.sha1(wp_bytes).hexdigest())
    windows = {
        "AMD64": dict(rf.PHP_WINDOWS["AMD64"]),
        "x86": dict(rf.PHP_WINDOWS["x86"]),
    }
    windows["AMD64"]["sha256"] = hashlib.sha256(php_bytes).hexdigest()
    monkeypatch.setattr(rf, "PHP_WINDOWS", windows)


def _bundles() -> tuple[bytes, bytes]:
    wordpress = _zip_bytes({"wordpress/wp-includes/version.php": WP_VERSION_PHP})
    php = _zip_bytes({"php.exe": b"not-a-real-binary"})
    return wordpress, php


def _fetch_factory(wordpress: bytes, php: bytes):
    def fetch(url: str, dest: Path) -> None:
        if url == rf.WORDPRESS_URL:
            dest.write_bytes(wordpress)
            return
        if url == rf.PHP_WINDOWS["AMD64"]["url"]:
            dest.write_bytes(php)
            return
        raise AssertionError(url)

    return fetch


def _probe(binary: Path) -> str:
    assert binary.name == "php.exe"
    assert binary.is_file()
    return PHP_BANNER


def test_pins_match_official_windows_and_wordpress_releases() -> None:
    assert rf.WORDPRESS_VERSION == "7.1.5"
    assert rf.WORDPRESS_URL == "https://wordpress.org/wordpress-7.1.5.zip"
    assert rf.WORDPRESS_SHA1 == "d7d004a092a4e8f2b28dc8f4089a11fea9a38807"
    assert rf.PHP_VERSION == "8.3.37"
    assert rf.PHP_WINDOWS["AMD64"]["sha256"] == (
        "38808a798485e1c0938dfcce7e942131415fc6c7834f83a5a85a4c4edb5539d6"
    )
    assert rf.PHP_WINDOWS["AMD64"]["url"] == (
        "https://downloads.php.net/~windows/releases/php-8.3.37-nts-Win32-vs16-x64.zip"
    )
    assert rf.PHP_WINDOWS["x86"]["sha256"] == (
        "957857b4b5f292f69ba136821a5b39dcdac882b5039795ef7bceaaa74b6d2465"
    )
    assert rf.ALLOWED_HOSTS == frozenset({"wordpress.org", "downloads.php.net", "www.php.net"})
    pins = select_pins("Windows", "AMD64")
    assert pins["php"]["digest"] == rf.PHP_WINDOWS["AMD64"]["sha256"]
    x86 = select_pins("Windows", "x86")
    assert x86["php"]["digest"] == rf.PHP_WINDOWS["x86"]["sha256"]
    assert x86["php"]["archive_name"].endswith("x86.zip")


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Linux", "x86_64"), ("Darwin", "arm64"), ("Windows", "ARM64")],
)
def test_unpinned_platform_downloads_nothing(tmp_path: Path, system: str, machine: str) -> None:
    def fetch(url: str, dest: Path) -> None:
        raise AssertionError(url)

    with pytest.raises(RuntimeFetchError) as raised:
        install(tmp_path, system=system, machine=machine, fetch=fetch, probe=_probe)
    assert raised.value.code == "php_binary_unpinned"
    assert list(tmp_path.iterdir()) == []


def test_checksum_mismatch_deletes_partial_and_skips_php(tmp_path: Path) -> None:
    seen: list[str] = []

    def fetch(url: str, dest: Path) -> None:
        seen.append(url)
        dest.write_bytes(b"not-the-archive")

    with pytest.raises(RuntimeFetchError) as raised:
        install(tmp_path, system="Windows", machine="AMD64", fetch=fetch, probe=_probe)
    assert raised.value.code == "checksum_mismatch"
    assert seen == [rf.WORDPRESS_URL]
    assert not (tmp_path / "wordpress").exists()
    assert not (tmp_path / "runtime.json").exists()
    assert list((tmp_path / "archives").glob("*")) == []


def test_zip_slip_writes_nothing(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("wordpress/wp-includes/version.php", WP_VERSION_PHP)
        handle.writestr("../outside.php", b"<?php echo 1;\n")
    out = tmp_path / "out"
    with pytest.raises(RuntimeFetchError) as raised:
        extract_zip(archive, out)
    assert raised.value.code == "zip_slip"
    assert not (tmp_path / "outside.php").exists()
    assert not any(path.is_file() for path in out.rglob("*"))


@pytest.mark.parametrize(
    "name",
    ["C:/Windows/outside.php", "wordpress/../../outside.php", "/etc/passwd", "\\\\server\\share\\x.php"],
)
def test_zip_member_escapes_are_refused(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(name, b"<?php\n")
    with pytest.raises(RuntimeFetchError) as raised:
        extract_zip(archive, tmp_path / "out")
    assert raised.value.code == "zip_slip"


def test_zip_symlink_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "link.zip"
    info = zipfile.ZipInfo("wordpress/wp-includes/version.php")
    info.external_attr = stat.S_IFLNK << 16
    info.compress_type = zipfile.ZIP_STORED
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(info, "elsewhere")
    with pytest.raises(RuntimeFetchError) as raised:
        extract_zip(archive, tmp_path / "out")
    assert raised.value.code == "zip_slip"


def test_manifest_waits_for_version_and_php_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wordpress, php = _bundles()
    _use_synthetic_pins(monkeypatch, wordpress, php)
    calls: list[str] = []

    def probe(binary: Path) -> str:
        calls.append("probe")
        assert not (tmp_path / "runtime.json").exists()
        return _probe(binary)

    payload = install(
        tmp_path,
        system="Windows",
        machine="AMD64",
        fetch=_fetch_factory(wordpress, php),
        probe=probe,
    )
    assert calls == ["probe"]
    assert payload["profile"] == "azom-wp-runtime"
    assert payload["profile_version"] == "1"
    assert payload["wordpress_version"] == "7.1.5"
    assert payload["php_version"] == "8.3.37"
    assert payload["sandbox_enabled"] is False
    assert payload["server_started"] is False
    assert "port" not in payload
    assert Path(payload["php_path"]) == (tmp_path / "php" / "php.exe").resolve()
    text = (tmp_path / "wordpress" / "wp-includes" / "version.php").read_text(encoding="utf-8")
    assert "$wp_version = '7.1.5'" in text

    def fetch_again(url: str, dest: Path) -> None:
        raise AssertionError(url)

    again = install(
        tmp_path,
        system="Windows",
        machine="AMD64",
        fetch=fetch_again,
        probe=_probe,
    )
    assert again == payload


def test_wrong_wordpress_version_does_not_probe_or_write_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wordpress = _zip_bytes({"wordpress/wp-includes/version.php": b"<?php\n$wp_version = '6.0';\n"})
    php = _zip_bytes({"php.exe": b"x"})
    _use_synthetic_pins(monkeypatch, wordpress, php)
    probed = False

    def probe(binary: Path) -> str:
        nonlocal probed
        probed = True
        return PHP_BANNER

    with pytest.raises(RuntimeFetchError) as raised:
        install(
            tmp_path,
            system="Windows",
            machine="AMD64",
            fetch=_fetch_factory(wordpress, php),
            probe=probe,
        )
    assert raised.value.code == "wordpress_version_mismatch"
    assert probed is False
    assert not (tmp_path / "runtime.json").exists()
    assert not (tmp_path / "wordpress").exists()


def test_php_probe_failure_does_not_write_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wordpress, php = _bundles()
    _use_synthetic_pins(monkeypatch, wordpress, php)

    def probe(binary: Path) -> str:
        return "PHP 8.3.36 (cli)"

    with pytest.raises(RuntimeFetchError) as raised:
        install(
            tmp_path,
            system="Windows",
            machine="AMD64",
            fetch=_fetch_factory(wordpress, php),
            probe=probe,
        )
    assert raised.value.code == "php_probe_failed"
    assert not (tmp_path / "runtime.json").exists()
    assert not (tmp_path / "php").exists()
    assert not (tmp_path / "wordpress").exists()


def test_check_requires_manifest_and_ignores_a_sandbox_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeFetchError) as missing:
        check_runtime(tmp_path, system="Windows", machine="AMD64", probe=_probe)
    assert missing.value.code == "manifest_missing"
    assert list(tmp_path.iterdir()) == []

    wordpress, php = _bundles()
    _use_synthetic_pins(monkeypatch, wordpress, php)
    install(
        tmp_path,
        system="Windows",
        machine="AMD64",
        fetch=_fetch_factory(wordpress, php),
        probe=_probe,
    )
    stored = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    stored["sandbox_enabled"] = True
    (tmp_path / "runtime.json").write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(RuntimeFetchError) as claimed:
        check_runtime(tmp_path, system="Windows", machine="AMD64", probe=_probe)
    assert claimed.value.code == "manifest_mismatch"


def test_download_refuses_foreign_redirect_and_http(tmp_path: Path) -> None:
    def opener(url: str):
        assert url == rf.WORDPRESS_URL

        class Response:
            status = 302
            headers = {"Location": "https://cdn.example/file.zip"}

            def read(self, size: int) -> bytes:
                return b""

            def close(self) -> None:
                return None

        return Response()

    dest = tmp_path / "wordpress.zip"
    with pytest.raises(RuntimeFetchError) as raised:
        download(rf.WORDPRESS_URL, dest, opener=opener)
    assert raised.value.code == "redirect_host_refused"
    assert not dest.exists()

    def unused(url: str):
        raise AssertionError(url)

    with pytest.raises(RuntimeFetchError) as http:
        download("http://wordpress.org/wordpress-7.1.5.zip", tmp_path / "b.zip", opener=unused)
    assert http.value.code == "redirect_host_refused"


def test_download_follows_an_allowed_host_and_caps_bytes(tmp_path: Path) -> None:
    calls: list[str] = []

    class Response:
        def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.status = status
            self.headers = headers
            self._body = body

        def read(self, size: int) -> bytes:
            chunk = self._body[:size]
            self._body = self._body[size:]
            return chunk

        def close(self) -> None:
            return None

    def opener(url: str) -> Response:
        calls.append(url)
        if len(calls) == 1:
            return Response(302, {"Location": "https://downloads.php.net/php.zip"}, b"")
        return Response(200, {}, b"abcd")

    dest = tmp_path / "php.zip"
    download("https://www.php.net/start", dest, opener=opener)
    assert dest.read_bytes() == b"abcd"
    assert calls[1] == "https://downloads.php.net/php.zip"

    def big(url: str) -> Response:
        return Response(200, {}, b"0123456789")

    oversized = tmp_path / "big.zip"
    with pytest.raises(RuntimeFetchError) as raised:
        download(rf.WORDPRESS_URL, oversized, opener=big, max_bytes=4)
    assert raised.value.code == "download_failed"
    assert not oversized.exists()


def test_probe_uses_argv_and_drops_unlisted_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "php.exe"
    binary.write_bytes(b"")
    monkeypatch.setenv("PATH", "C:\\Windows")
    monkeypatch.setenv("SECRET_TOKEN", "nope")
    seen: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "PHP 8.3.37 (cli)\n"
        stderr = ""

    def runner(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return Result()

    line = probe_php(binary, runner=runner)
    assert seen["command"] == [str(binary), "-v"]
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert "SECRET_TOKEN" not in kwargs["env"]
    assert kwargs["env"]["PATH"] == "C:\\Windows"
    assert line.startswith("PHP 8.3.37")


def test_probe_timeout_is_not_success(tmp_path: Path) -> None:
    binary = tmp_path / "php.exe"
    binary.write_bytes(b"")

    def runner(command, **kwargs):
        raise rf.subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(RuntimeFetchError) as raised:
        probe_php(binary, runner=runner)
    assert raised.value.code == "php_probe_failed"


def test_cli_rejects_a_url_and_prints_no_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["--url", "https://example.test/wordpress.zip"])
    assert exited.value.code == 2

    monkeypatch.setattr(rf.platform, "system", lambda: "Windows")
    monkeypatch.setattr(rf.platform, "machine", lambda: "AMD64")
    assert main(["--dest", "https://wordpress.org/latest.zip"]) == 1
    assert "dest_refused" in capsys.readouterr().err

    payload = {"profile": "azom-wp-runtime", "sandbox_enabled": False, "server_started": False}

    def fake_install(dest: Path, **kwargs):
        assert dest == tmp_path
        return payload

    monkeypatch.setattr(rf, "install", fake_install)
    assert main(["--dest", str(tmp_path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["sandbox_enabled"] is False
    assert printed["server_started"] is False


def test_default_dest_stays_outside_the_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(rf.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert rf.default_dest() == tmp_path / "azom" / "wp-runtime"
    monkeypatch.setattr(rf.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert rf.default_dest() == tmp_path / "share" / "azom" / "wp-runtime"


def test_cli_check_missing_manifest_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(rf.platform, "system", lambda: "Windows")
    monkeypatch.setattr(rf.platform, "machine", lambda: "AMD64")
    assert main(["--check", "--dest", str(tmp_path)]) == 1
    assert "manifest_missing" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_explicit_manifest_lookup_returns_php_only_when_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert resolve_runtime_manifest(tmp_path, system="Windows", machine="AMD64", probe=_probe) is None
    assert resolve_php_from_manifest(tmp_path, system="Windows", machine="AMD64", probe=_probe) is None

    wordpress, php = _bundles()
    _use_synthetic_pins(monkeypatch, wordpress, php)
    install(
        tmp_path,
        system="Windows",
        machine="AMD64",
        fetch=_fetch_factory(wordpress, php),
        probe=_probe,
    )
    resolved = resolve_php_from_manifest(tmp_path, system="Windows", machine="AMD64", probe=_probe)
    assert resolved == (tmp_path / "php" / "php.exe").resolve()

    stored = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    stored["php_version"] = "8.3.0"
    (tmp_path / "runtime.json").write_text(json.dumps(stored), encoding="utf-8")
    assert resolve_php_from_manifest(tmp_path, system="Windows", machine="AMD64", probe=_probe) is None
