"""Shared pure helpers for WordPress ops tests under tests/wp/."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.wp_ops.review_checks import JOBS, requirements

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
MOCK_REGISTRY = CONFIG_DIR / "wp_sites.mock.yaml"
LIVE_REGISTRY = CONFIG_DIR / "wp_sites.yaml"
DASH_DIR = REPO_ROOT / "infrastructure" / "dashboard"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)

_DASHBOARD_PASSWORD_VARS = (
    "DASHBOARD_PASSWORD",
    "DASHBOARD_PASSWORD_HASH",
    "DASHBOARD_OSCAR_PASSWORD",
    "DASHBOARD_OSCAR_PASSWORD_HASH",
)

_DEFAULT_CHILD_STYLE_CSS = (
    "/*\nTheme Name: Azom Child\nTemplate: parent-base\n*/\n"
    ".btn { background: #111; color: #fff; }\n"
)


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def make_png(width: int = 2, height: int = 2) -> bytes:
    """Build a minimal valid 8-bit RGB PNG payload."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + (b"\x10\x20\x30" * width) for _ in range(height))
    idat = zlib.compress(raw)
    return PNG_MAGIC + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def make_solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Create a valid 8-bit RGB PNG with given dimensions and solid fill color."""
    r, g, b = rgb
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = bytes([0]) + bytes([r, g, b]) * width
    compressed = zlib.compress(row * height, level=6)
    return (
        PNG_MAGIC
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def tool_call(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    ensure_ascii: bool = True,
) -> str:
    payload = json.dumps({"name": name, "arguments": arguments or {}}, ensure_ascii=ensure_ascii)
    return f"<tool_call>{payload}</tool_call>"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def mock_wp_content(data_dir: Path, site_id: str = "mock-staging") -> Path:
    return data_dir / "wp_files_mock" / site_id / "wp-content"


def write_mock_component(
    data_dir: Path,
    site_id: str,
    kind: str,
    slug: str,
    rel: str,
    content: str,
    *,
    binary: bool = True,
) -> Path:
    bucket = "themes" if kind == "theme" else "plugins"
    target = mock_wp_content(data_dir, site_id) / bucket / slug / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        target.write_bytes(content.encode("utf-8"))
    else:
        target.write_text(content, encoding="utf-8")
    return target


def write_theme_file(
    data_dir: Path,
    site_id: str,
    slug: str,
    rel: str,
    content: str,
    *,
    binary: bool = True,
) -> Path:
    return write_mock_component(
        data_dir,
        site_id,
        "theme",
        slug,
        rel,
        content,
        binary=binary,
    )


def write_plugin_file(
    data_dir: Path,
    site_id: str,
    slug: str,
    rel: str,
    content: str,
    *,
    binary: bool = True,
) -> Path:
    return write_mock_component(
        data_dir,
        site_id,
        "plugin",
        slug,
        rel,
        content,
        binary=binary,
    )


def seed_mock_child_theme(
    data_dir: Path,
    *,
    site_id: str = "mock-staging",
    slug: str = "azom-child",
    style_css: str | None = None,
    functions_php: str = "<?php\n// child theme\n",
) -> Path:
    theme_dir = mock_wp_content(data_dir, site_id) / "themes" / slug
    theme_dir.mkdir(parents=True, exist_ok=True)
    (theme_dir / "style.css").write_text(
        style_css if style_css is not None else _DEFAULT_CHILD_STYLE_CSS,
        encoding="utf-8",
    )
    (theme_dir / "functions.php").write_text(functions_php, encoding="utf-8")
    return theme_dir


def snapshot_tree(folder: Path) -> dict[str, bytes]:
    if not folder.is_dir():
        return {}
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def site_row(site_id: str = "stage", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": site_id,
        "display_name": site_id,
        "canonical_url": f"https://{site_id}.example.test/",
        "environment": "staging",
        "business_owner": "customer",
        "hosting_provider": "unconfigured",
        "wordpress_version": "6.7",
        "php_version": "8.3",
        "woocommerce": True,
        "woo_version": "9.6",
        "critical_urls": [f"https://{site_id}.example.test/"],
        "maintenance_window": "not agreed",
        "secret_ref": "WP_SECRET_SHOP",
        "alert_owner": "unassigned",
        "alert_confirmations": 2,
        "check_interval_seconds": 60,
        "timeout_seconds": 10,
        "dedupe_window_seconds": 30,
        "capabilities": {"uptime": "available"},
        "files_root": "/var/www/shop",
        "ssh_host": "shop.internal",
    }
    row.update(overrides)
    return row


def passing_catalog(
    kind: str,
    files: list[tuple[str, str]],
    risk: str,
    *,
    artifact_dir: Any = None,
) -> list[dict[str, Any]]:
    del artifact_dir
    needed = requirements(kind, files, risk)
    rows: list[dict[str, Any]] = []
    for job in JOBS:
        is_req = needed[job] == "required"
        row: dict[str, Any] = {
            "job": job,
            "status": "passed" if is_req else "not_applicable",
            "summary": f"{job} ok" if is_req else f"{job} not_applicable",
            "command": ["mock", job] if is_req else None,
            "started_at": "2026-11-24T00:00:00+00:00" if is_req else None,
            "finished_at": "2026-11-24T00:00:01+00:00" if is_req else None,
            "exit_code": 0 if is_req else None,
            "artifact_path": None,
            "executed": is_req,
            "simulated": False,
        }
        if job == "woo_static" and is_req:
            row["evidence_class"] = "static_precheck"
            row["runtime_proof"] = False
        rows.append(row)
    return rows


def clear_dashboard_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _DASHBOARD_PASSWORD_VARS:
        monkeypatch.delenv(name, raising=False)


def load_dashboard_app():
    if str(REPO_ROOT / "skills") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "skills"))
    dash_dir = str(DASH_DIR)
    if dash_dir not in sys.path:
        sys.path.insert(0, dash_dir)
    for name in ("azom_dashboard", "wp", "wp_routes", "settings_store", "auth", "support"):
        sys.modules.pop(name, None)
    for key in [
        k for k in list(sys.modules) if k.startswith(("wp.", "infrastructure.dashboard.wp"))
    ]:
        sys.modules.pop(key, None)
    spec = importlib.util.spec_from_file_location("azom_dashboard", DASH_DIR / "app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["azom_dashboard"] = mod
    spec.loader.exec_module(mod)
    mod._configure_secret_key()
    return mod.app


def dashboard_auth_headers(client: Any, user: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{user}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    client.get("/wp", headers=headers)
    with client.session_transaction() as sess:
        csrf = sess.get("csrf_token")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers
