"""Download the pinned WordPress tree and PHP binary for this machine.

The pins are constants. Callers cannot supply a URL. Linux, macOS, and
Windows on ARM have no official PHP binary in this table, so those machines
fail with php_binary_unpinned before any download. A written manifest means
both archives matched their published checksums, version.php matches, and
php -v reported the pinned version. It does not mean a sandbox is running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

PROFILE = "azom-wp-runtime"
PROFILE_VERSION = "1"

WORDPRESS_VERSION = "7.1.5"
WORDPRESS_URL = "https://wordpress.org/wordpress-7.1.5.zip"
# Official checksum published at wordpress.org/wordpress-7.1.5.zip.sha1.
WORDPRESS_SHA1 = "d7d004a092a4e8f2b28dc8f4089a11fea9a38807"

PHP_VERSION = "8.3.37"
# Windows builds from downloads.php.net/~windows/releases/releases.json, branch 8.3.
# PHP publishes no official Linux or macOS binary. Do not compile one here.
PHP_WINDOWS = {
    "AMD64": {
        "filename": "php-8.3.37-nts-Win32-vs16-x64.zip",
        "sha256": "38808a798485e1c0938dfcce7e942131415fc6c7834f83a5a85a4c4edb5539d6",
        "url": "https://downloads.php.net/~windows/releases/php-8.3.37-nts-Win32-vs16-x64.zip",
    },
    "x86": {
        "filename": "php-8.3.37-nts-Win32-vs16-x86.zip",
        "sha256": "957857b4b5f292f69ba136821a5b39dcdac882b5039795ef7bceaaa74b6d2465",
        "url": "https://downloads.php.net/~windows/releases/php-8.3.37-nts-Win32-vs16-x86.zip",
    },
}

ALLOWED_HOSTS = frozenset({"wordpress.org", "downloads.php.net", "www.php.net"})
MAX_REDIRECTS = 3
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_UNCOMPRESSED = 256 * 1024 * 1024
USER_AGENT = "azom-wp-runtime-fetch/1"
_PROBE_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
_REDIRECTS = {301, 302, 303, 307, 308}
_VERSION_FILE = Path("wordpress") / "wp-includes" / "version.php"


class RuntimeFetchError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def default_dest() -> Path:
    """Return the runtime directory outside the repository."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "azom" / "wp-runtime"
    configured = os.environ.get("XDG_DATA_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "share"
    return base / "azom" / "wp-runtime"


def select_pins(system: str, machine: str) -> dict:
    """Return the WordPress pin and the PHP pin for this OS and CPU."""
    wordpress = {
        "version": WORDPRESS_VERSION,
        "url": WORDPRESS_URL,
        "algo": "sha1",
        "digest": WORDPRESS_SHA1,
        "archive_name": f"wordpress-{WORDPRESS_VERSION}.zip",
    }
    normalized = machine.upper().replace(" ", "")
    windows_arch = {"AMD64": "AMD64", "X86_64": "AMD64", "X86": "x86", "I386": "x86", "I686": "x86"}
    arch = windows_arch.get(normalized)
    if system != "Windows" or arch is None:
        raise RuntimeFetchError("php_binary_unpinned")
    php = PHP_WINDOWS[arch]
    return {
        "wordpress": wordpress,
        "php": {
            "version": PHP_VERSION,
            "url": php["url"],
            "algo": "sha256",
            "digest": php["sha256"],
            "archive_name": php["filename"],
            "binary_name": "php.exe",
        },
    }


def assert_pinned_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or host not in ALLOWED_HOSTS:
        raise RuntimeFetchError("redirect_host_refused")


def download(url: str, dest: Path, *, opener=None, max_bytes: int = MAX_ARCHIVE_BYTES) -> None:
    """Download one pinned HTTPS URL. A foreign redirect is not followed."""
    open_url = opener or _open_pinned
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        assert_pinned_url(current)
        response = open_url(current)
        try:
            status = _status(response)
            headers = getattr(response, "headers", {}) or {}
            if status in _REDIRECTS:
                location = headers.get("Location") or headers.get("location")
                if not location:
                    raise RuntimeFetchError("redirect_host_refused")
                current = urllib.parse.urljoin(current, location)
                continue
            if status != 200:
                raise RuntimeFetchError("download_failed")
            length = headers.get("Content-Length") or headers.get("content-length")
            if length is not None and str(length).strip().isdigit() and int(str(length).strip()) > max_bytes:
                raise RuntimeFetchError("download_failed")
            _write_capped(response, dest, max_bytes)
            return
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    raise RuntimeFetchError("redirect_host_refused")


def extract_zip(archive: Path, dest: Path) -> None:
    """Extract a zip after rejecting links, absolute paths, and `..`."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as handle:
            planned = _plan_members(handle, dest)
            for info, target in planned:
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_member(handle, info, target)
    except zipfile.BadZipFile:
        raise RuntimeFetchError("archive_invalid") from None
    except RuntimeFetchError:
        _discard_files(dest)
        raise


def probe_php(binary: Path, runner=None) -> str:
    """Run php -v with a reduced environment. The banner must name the pin."""
    binary = Path(binary)
    if binary.is_symlink() or not binary.is_file():
        raise RuntimeFetchError("php_probe_failed")
    env = {key: os.environ[key] for key in _PROBE_ENV if key in os.environ}
    run = runner or subprocess.run
    try:
        completed = run(
            [str(binary), "-v"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
            cwd=str(binary.parent),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeFetchError("php_probe_failed") from None
    if completed.returncode != 0:
        raise RuntimeFetchError("php_probe_failed")
    text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    if not _version_banner(text, PHP_VERSION):
        raise RuntimeFetchError("php_probe_failed")
    return text.strip().splitlines()[0][:200]


def install(dest: Path, *, system: str | None = None, machine: str | None = None, fetch=None, probe=None) -> dict:
    """Fetch and check both pins. The manifest is written only after both match."""
    pins = select_pins(system or platform.system(), machine or platform.machine())
    dest = Path(dest)
    _refuse_dest(dest)
    fetch_url = fetch or download
    probe_binary = probe or probe_php
    current = _matching_manifest(dest, pins, probe_binary)
    if current is not None:
        return current
    dest.mkdir(parents=True, exist_ok=True)
    archives = dest / "archives"
    archives.mkdir(exist_ok=True)
    incoming = dest / ".incoming"
    if incoming.exists():
        shutil.rmtree(incoming)
    incoming.mkdir()
    try:
        wordpress_root = _stage_wordpress(incoming, archives, pins["wordpress"], fetch_url)
        php_binary = _stage_php(incoming, archives, pins["php"], fetch_url, probe_binary)
        _swap_dir(wordpress_root, dest / "wordpress")
        _swap_dir(php_binary.parent, dest / "php")
        payload = _manifest(dest, pins)
        _write_json(dest / "runtime.json", payload)
        return payload
    finally:
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)


def check_runtime(dest: Path, *, system: str | None = None, machine: str | None = None, probe=None) -> dict:
    """Re-read a manifest and probe php -v. This does not download."""
    pins = select_pins(system or platform.system(), machine or platform.machine())
    dest = Path(dest)
    _refuse_dest(dest)
    path = dest / "runtime.json"
    if not path.is_file():
        raise RuntimeFetchError("manifest_missing")
    data = _read_manifest(path)
    _assert_manifest(data, dest, pins)
    _assert_wp_version(dest / _VERSION_FILE, pins["wordpress"]["version"], dest / "wordpress")
    binary = dest / "php" / pins["php"]["binary_name"]
    banner = (probe or probe_php)(binary)
    if not _version_banner(banner, pins["php"]["version"]):
        raise RuntimeFetchError("php_probe_failed")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fetch-wp-runtime")
    parser.add_argument("--dest", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.dest is not None and (not str(args.dest).strip() or "://" in str(args.dest)):
        print("dest_refused", file=sys.stderr)
        return 1
    dest = Path(args.dest) if args.dest else default_dest()
    try:
        payload = check_runtime(dest) if args.check else install(dest)
    except RuntimeFetchError as exc:
        print(exc.code, file=sys.stderr)
        if exc.code == "php_binary_unpinned":
            print("PHP publishes no official binary for this OS. No archive was downloaded.", file=sys.stderr)
        elif exc.code == "php_probe_failed":
            print(
                "php -v did not report the pinned version. On Windows the Visual C++ runtime may be missing.",
                file=sys.stderr,
            )
        return 1
    print(json.dumps(payload))
    return 0


def _open_pinned(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_RefuseRedirect())
    try:
        return opener.open(request, timeout=120)
    except urllib.error.HTTPError as exc:
        if exc.code in _REDIRECTS:
            return exc
        raise RuntimeFetchError("download_failed") from None
    except urllib.error.URLError:
        raise RuntimeFetchError("download_failed") from None


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _status(response: object) -> int:
    value = getattr(response, "status", None)
    if value is None:
        value = getattr(response, "code", 0)
    return int(value)


def _write_capped(response: object, dest: Path, max_bytes: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise RuntimeFetchError("download_failed")
                handle.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _plan_members(handle: zipfile.ZipFile, dest: Path) -> list[tuple[zipfile.ZipInfo, Path]]:
    infos = handle.infolist()
    if len(infos) > MAX_MEMBERS:
        raise RuntimeFetchError("zip_slip")
    planned: list[tuple[zipfile.ZipInfo, Path]] = []
    total = 0
    root = dest.resolve()
    for info in infos:
        relative = _safe_member(info)
        if relative is None:
            continue
        total += max(info.file_size, 0)
        if total > MAX_UNCOMPRESSED or info.file_size > MAX_UNCOMPRESSED:
            raise RuntimeFetchError("zip_slip")
        target = root.joinpath(*relative.split("/"))
        try:
            target.relative_to(root)
        except ValueError:
            raise RuntimeFetchError("zip_slip") from None
        planned.append((info, target))
    return planned


def _safe_member(info: zipfile.ZipInfo) -> str | None:
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise RuntimeFetchError("zip_slip")
    raw = info.filename.replace("\\", "/")
    if raw.startswith("/") or raw.startswith("//"):
        raise RuntimeFetchError("zip_slip")
    if len(raw) >= 2 and raw[1] == ":":
        raise RuntimeFetchError("zip_slip")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts:
        return None
    if any(part == ".." for part in parts):
        raise RuntimeFetchError("zip_slip")
    if raw.endswith("/"):
        return None
    return "/".join(parts)


def _write_member(handle: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    remaining = info.file_size
    with handle.open(info, "r") as src, target.open("wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            remaining -= len(chunk)
            if remaining < 0:
                raise RuntimeFetchError("zip_slip")
            out.write(chunk)


def _discard_files(dest: Path) -> None:
    if not dest.exists():
        return
    for child in dest.rglob("*"):
        if child.is_file() and not child.is_symlink():
            child.unlink()


def _stage_wordpress(incoming: Path, archives: Path, pin: dict, fetch) -> Path:
    archive = archives / pin["archive_name"]
    _ensure_archive(archive, pin, fetch)
    extracted = incoming / "wp-extract"
    try:
        extract_zip(archive, extracted)
    except RuntimeFetchError as exc:
        if exc.code == "archive_invalid":
            _unlink(archive)
        raise
    version_file = extracted / _VERSION_FILE
    root = extracted / "wordpress"
    _assert_wp_version(version_file, pin["version"], root)
    staged = incoming / "wordpress"
    root.rename(staged)
    return staged


def _stage_php(incoming: Path, archives: Path, pin: dict, fetch, probe) -> Path:
    archive = archives / pin["archive_name"]
    _ensure_archive(archive, pin, fetch)
    staged = incoming / "php"
    try:
        extract_zip(archive, staged)
    except RuntimeFetchError as exc:
        if exc.code == "archive_invalid":
            _unlink(archive)
        raise
    binary = staged / pin["binary_name"]
    if binary.is_symlink() or not binary.is_file():
        raise RuntimeFetchError("php_probe_failed")
    banner = probe(binary)
    if not _version_banner(banner, pin["version"]):
        raise RuntimeFetchError("php_probe_failed")
    return binary


def _ensure_archive(archive: Path, pin: dict, fetch) -> None:
    if archive.is_file() and _hash(archive, pin["algo"]) == pin["digest"].lower():
        return
    _unlink(archive)
    partial = archive.with_name(archive.name + ".partial")
    _unlink(partial)
    try:
        fetch(pin["url"], partial)
    except RuntimeFetchError:
        _unlink(partial)
        raise
    except OSError:
        _unlink(partial)
        raise RuntimeFetchError("download_failed") from None
    if not partial.is_file() or _hash(partial, pin["algo"]) != pin["digest"].lower():
        _unlink(partial)
        raise RuntimeFetchError("checksum_mismatch")
    os.replace(partial, archive)


def _hash(path: Path, algo: str) -> str:
    if algo not in {"sha1", "sha256"}:
        raise RuntimeFetchError("checksum_mismatch")
    digest = hashlib.new(algo)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_wp_version(version_file: Path, version: str, root: Path) -> None:
    if version_file.is_symlink() or not version_file.is_file():
        raise RuntimeFetchError("wordpress_version_mismatch")
    try:
        version_file.resolve().relative_to(root.resolve())
    except ValueError:
        raise RuntimeFetchError("wordpress_version_mismatch") from None
    text = version_file.read_text(encoding="utf-8", errors="replace")[:65536]
    match = re.search(r"\$wp_version\s*=\s*(['\"])([^'\"]+)\1\s*;", text)
    if match is None or match.group(2) != version:
        raise RuntimeFetchError("wordpress_version_mismatch")


def _version_banner(text: str, version: str) -> bool:
    return re.search(rf"(?m)^PHP {re.escape(version)}(?:\s|\(|$)", text) is not None


def _manifest(dest: Path, pins: dict) -> dict:
    binary_name = pins["php"]["binary_name"]
    return {
        "profile": PROFILE,
        "profile_version": PROFILE_VERSION,
        "wordpress_version": pins["wordpress"]["version"],
        "php_version": pins["php"]["version"],
        "wordpress_path": str((dest / "wordpress").resolve()),
        "php_path": str((dest / "php" / binary_name).resolve()),
        "sandbox_enabled": False,
        "server_started": False,
    }


def _matching_manifest(dest: Path, pins: dict, probe) -> dict | None:
    path = dest / "runtime.json"
    if not path.is_file():
        return None
    try:
        data = _read_manifest(path)
        _assert_manifest(data, dest, pins)
        _assert_wp_version(dest / _VERSION_FILE, pins["wordpress"]["version"], dest / "wordpress")
        binary = dest / "php" / pins["php"]["binary_name"]
        banner = probe(binary)
    except (RuntimeFetchError, OSError):
        return None
    if not _version_banner(banner, pins["php"]["version"]):
        return None
    return data


def _read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeFetchError("manifest_mismatch") from None
    if not isinstance(data, dict):
        raise RuntimeFetchError("manifest_mismatch")
    return data


def _assert_manifest(data: dict, dest: Path, pins: dict) -> None:
    binary = dest / "php" / pins["php"]["binary_name"]
    expected = _manifest(dest, pins)
    if any(data.get(key) != value for key, value in expected.items()):
        raise RuntimeFetchError("manifest_mismatch")
    if Path(str(data.get("php_path"))).resolve() != binary.resolve():
        raise RuntimeFetchError("manifest_mismatch")
    if Path(str(data.get("wordpress_path"))).resolve() != (dest / "wordpress").resolve():
        raise RuntimeFetchError("manifest_mismatch")


def _write_json(path: Path, payload: dict) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)


def _swap_dir(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    trash = dest.parent / f".trash-{dest.name}"
    if trash.exists():
        shutil.rmtree(trash)
    moved = False
    if dest.exists():
        dest.rename(trash)
        moved = True
    try:
        src.rename(dest)
    except OSError:
        if moved:
            trash.rename(dest)
        raise
    if trash.exists():
        shutil.rmtree(trash)


def _refuse_dest(dest: Path) -> None:
    text = str(dest).strip()
    # A Windows Path turns "https://host/x" into "https:\host\x" before this runs.
    if not text or "://" in text or ":" in text[2:]:
        raise RuntimeFetchError("dest_refused")
    try:
        exists = dest.exists()
    except OSError:
        raise RuntimeFetchError("dest_refused") from None
    if exists and not dest.is_dir():
        raise RuntimeFetchError("dest_refused")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
