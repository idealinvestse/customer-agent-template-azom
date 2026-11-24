"""Fixed, bounded PHP syntax process used by the review check catalog."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _artifact(artifact_dir: Path | None, payload: dict) -> str | None:
    if artifact_dir is None:
        return None
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "php_lint.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def run_php_lint(files: list[tuple[str, str]], artifact_dir: Path | None = None) -> dict:
    php = shutil.which("php")
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
                    run = subprocess.run(
                        command,
                        shell=False,
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                        env={
                            key: value
                            for key, value in os.environ.items()
                            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
                        },
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
    status = "passed" if exit_code == 0 and not reason else "failed"
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
