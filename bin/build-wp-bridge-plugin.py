"""Deterministic ZIP builder and static security auditor for ``azom-ops-bridge``."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_DIR = _REPO_ROOT / "wordpress-plugin" / "azom-ops-bridge"
_REQUIRED_FILES = (
    "azom-ops-bridge.php",
    "includes/class-auth.php",
    "includes/class-health.php",
    "includes/class-translations.php",
    "includes/class-snapshots.php",
    "includes/class-ops.php",
    "includes/class-plugins.php",
    "includes/class-seo.php",
    "readme.txt",
)
_BANNED_PHP_RE = re.compile(
    r"\b(?:eval|shell_exec|exec|system|passthru|proc_open|popen|pcntl_exec|unserialize|assert)\s*\(",
    re.IGNORECASE,
)
_ABSPATH_RE = re.compile(r"defined\s*\(\s*['\"](?:ABSPATH|WP_UNINSTALL_PLUGIN)['\"]\s*\)")
_FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def _strip_php_comments_and_strings(text: str) -> str:
    """Strip PHP comments and string literal interiors so ``https://`` or ``#`` in strings cannot hide code."""
    try:
        from ecom_ops.wp_ops.source_scan import strip_comments_and_find_strings
    except ImportError:
        skills_dir = str(_REPO_ROOT / "skills")
        if skills_dir not in sys.path:
            sys.path.insert(0, skills_dir)
        from ecom_ops.wp_ops.source_scan import strip_comments_and_find_strings

    stripped, in_string, _ = strip_comments_and_find_strings(text, allow_hash=True)
    return "".join(
        " " if is_str and ch != "\n" else ch for ch, is_str in zip(stripped, in_string, strict=True)
    )


def validate_plugin_source(plugin_dir: Path = _PLUGIN_DIR) -> dict[str, Any]:
    """Validate required files, ABSPATH guards, and absence of banned PHP functions."""
    if not plugin_dir.is_dir():
        return {"ok": False, "error": f"Plugin directory missing: {plugin_dir}"}
    missing = [rel for rel in _REQUIRED_FILES if not (plugin_dir / rel).is_file()]
    if missing:
        return {"ok": False, "error": f"Missing required files: {', '.join(missing)}"}

    php_files = sorted(plugin_dir.rglob("*.php"))
    violations: list[str] = []
    for pf in php_files:
        rel = pf.relative_to(plugin_dir).as_posix()
        text = pf.read_text(encoding="utf-8")
        if not _ABSPATH_RE.search(text):
            violations.append(f"{rel}: missing ABSPATH/WP_UNINSTALL_PLUGIN guard")
        code_only = _strip_php_comments_and_strings(text)
        match = _BANNED_PHP_RE.search(code_only)
        if match:
            violations.append(f"{rel}: banned call {match.group(0).strip()}")
    if violations:
        return {"ok": False, "error": "; ".join(violations), "php_files": len(php_files)}
    return {"ok": True, "php_files": len(php_files), "violations": []}


def build_plugin_zip_bytes(plugin_dir: Path = _PLUGIN_DIR) -> tuple[bytes, dict[str, Any]]:
    """Build deterministic ``azom-ops-bridge.zip`` bytes in memory."""
    check = validate_plugin_source(plugin_dir)
    if not check.get("ok"):
        raise ValueError(str(check.get("error") or "Plugin validation failed"))

    files = sorted(
        (
            p
            for p in plugin_dir.rglob("*")
            if p.is_file()
            and not p.name.startswith(".")
            and p.name != "README.md"
            and "__pycache__" not in p.parts
        ),
        key=lambda p: p.relative_to(plugin_dir).as_posix(),
    )
    buf = io.BytesIO()
    included: list[str] = []
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            rel = path.relative_to(plugin_dir).as_posix()
            arcname = f"azom-ops-bridge/{rel}"
            info = zipfile.ZipInfo(arcname, date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
            included.append(arcname)
    raw = buf.getvalue()
    return raw, {
        "ok": True,
        "php_files": check["php_files"],
        "files": included,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build & validate azom-ops-bridge WordPress plugin ZIP.")
    parser.add_argument("--out", default="", help="Optional output ZIP file path")
    parser.add_argument("--check", action="store_true", help="Validate plugin and build in memory only")
    args = parser.parse_args(argv)

    try:
        raw, report = build_plugin_zip_bytes(_PLUGIN_DIR)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    if args.out and not args.check:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(raw)
        report["out"] = str(out_path)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
