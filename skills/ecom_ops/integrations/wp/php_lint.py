"""Fixed, bounded PHP syntax process used by the review check catalog."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp.process_env import probe_subprocess_env
from ecom_ops.integrations.wp.process_env import utc_now_iso as _now
from ecom_ops.integrations.wp_runtime_fetch import resolve_php_from_manifest

_FACADE_MODULE = "ecom_ops.integrations.wp_review_process"


def _facade_attr(name: str, default: Any) -> Any:
    facade = sys.modules.get(_FACADE_MODULE)
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)
    return default


def _artifact(artifact_dir: Path | None, payload: dict) -> str | None:
    if artifact_dir is None:
        return None
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "php_lint.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def resolve_php_binary(*, runtime_dir: Path | None = None) -> str | None:
    """Resolve PHP from an explicit runtime manifest when given, else PATH."""
    if runtime_dir is not None:
        resolve_fn = _facade_attr("resolve_php_from_manifest", resolve_php_from_manifest)
        binary = resolve_fn(Path(runtime_dir))
        return str(binary) if binary is not None else None
    shutil_mod = _facade_attr("shutil", shutil)
    return shutil_mod.which("php")


def run_php_lint(
    files: list[tuple[str, str]],
    artifact_dir: Path | None = None,
    *,
    runtime_dir: Path | None = None,
) -> dict:
    resolve_bin_fn = _facade_attr("resolve_php_binary", resolve_php_binary)
    subprocess_mod = _facade_attr("subprocess", subprocess)
    php = resolve_bin_fn(runtime_dir=runtime_dir)
    if not php:
        return {
            "job": "php_lint",
            "status": "unavailable",
            "availability": "unavailable",
            "reason": "php_binary_missing",
            "summary": "php_lint unavailable: php_binary_missing",
            "command": None,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "artifact_path": None,
            "executed": False,
            "simulated": False,
        }
    started = _now()
    results: list[dict] = []
    exit_code = 0
    reason = ""
    php_files = [(relative, text) for relative, text in files if relative.lower().endswith(".php")]
    try:
        with tempfile.TemporaryDirectory(prefix="azom-wp-php-lint-") as folder:
            root = Path(folder)
            for index, (relative, text) in enumerate(php_files):
                candidate = root / f"review-{index}.php"
                candidate.write_text(text, encoding="utf-8")
                command = [php, "-l", str(candidate)]
                try:
                    run = subprocess_mod.run(
                        command,
                        shell=False,
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                        env=probe_subprocess_env(),
                    )
                    results.append(
                        {
                            "file": relative,
                            "exit_code": run.returncode,
                            "output": (run.stdout + run.stderr).strip()[:500],
                        }
                    )
                    if run.returncode != 0:
                        exit_code = run.returncode
                except subprocess.TimeoutExpired:
                    results.append({"file": relative, "exit_code": None, "output": "timeout"})
                    if exit_code == 0:
                        exit_code = 124
                        reason = "timeout"
                    break
    except OSError:
        return {
            "job": "php_lint",
            "status": "unavailable",
            "availability": "unavailable",
            "reason": "php_process_unavailable",
            "summary": "php_lint unavailable: php_process_unavailable",
            "command": [php, "-l", "<reviewed-file>"],
            "started_at": started,
            "finished_at": _now(),
            "exit_code": None,
            "artifact_path": None,
            "executed": False,
            "simulated": False,
        }
    finished = _now()
    if reason == "timeout":
        status = "timeout"
    elif exit_code == 0:
        status = "passed"
    else:
        status = "failed"
    row = {
        "job": "php_lint",
        "status": status,
        "command": [php, "-l", "<reviewed-file>"],
        "started_at": started,
        "finished_at": finished,
        "exit_code": exit_code,
        "executed": True,
        "simulated": False,
        "reason": reason or ("syntax_error" if exit_code else ""),
        "summary": f"php -l: {len(results)} PHP file(s), exit {exit_code}",
    }
    row["artifact_path"] = _artifact(
        artifact_dir, {"job": "php_lint", "files": results, "started_at": started, "finished_at": finished}
    )
    return row


__all__ = [
    "_artifact",
    "_now",
    "resolve_php_binary",
    "run_php_lint",
]
