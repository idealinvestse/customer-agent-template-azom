"""Named WordPress review checks with honest process evidence.

Only PHP syntax lint runs locally, through the fixed ``php -l`` command (which
parses but does not execute the reviewed PHP). Other executable checks require
the isolated runtime that is not wired yet and stay unavailable.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from ecom_ops.integrations.wp_review_process import run_php_lint

_ORDER = re.compile(r"(?i)(?<![a-z])(?:shipping|stock|inventory|orders?|frakt|lager)(?![a-z])")
JOBS = (
    "php_lint",
    "wpcs_php",
    "plugin_check",
    "phpunit",
    "assets_lint",
    "woo_static",
    "browser_smoke",
    "egress_probe",
)
_UNAVAILABLE = {
    "wpcs_php": "isolated_phpcs_runtime_unavailable",
    "plugin_check": "isolated_plugin_check_runtime_unavailable",
    "phpunit": "isolated_phpunit_runtime_unavailable",
    "assets_lint": "isolated_assets_runtime_unavailable",
    "browser_smoke": "wordpress_browser_runtime_unavailable",
    "egress_probe": "isolated_egress_runtime_unavailable",
}


def _php(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(rel, text) for rel, text in files if rel.lower().endswith(".php")]


def _has_tests(files: list[tuple[str, str]]) -> bool:
    for rel, _text in files:
        name = rel.rsplit("/", 1)[-1]
        if "/tests/" in f"/{rel}" or rel.startswith("tests/") or name.endswith("Test.php"):
            return True
    return False


def requirements(kind: str, files: list[tuple[str, str]], risk: str) -> dict[str, str]:
    has_php = bool(_php(files))
    visual = kind == "theme" and any(rel.endswith((".css", ".php", ".js", ".html")) for rel, _text in files)
    orderish = risk == "high" or any(_ORDER.search(rel) or _ORDER.search(text) for rel, text in files)
    has_lock = any(rel.endswith("package-lock.json") for rel, _text in files)
    return {
        "php_lint": "required" if has_php else "not_applicable",
        "wpcs_php": "required" if has_php else "not_applicable",
        "plugin_check": "required" if kind == "plugin" else "not_applicable",
        "phpunit": "required" if _has_tests(files) else "not_applicable",
        "assets_lint": "optional" if has_lock else "not_applicable",
        "woo_static": "required" if orderish else "not_applicable",
        "browser_smoke": "required" if visual else "not_applicable",
        "egress_probe": "required",
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row(job: str, status: str, **extra: object) -> dict:
    body: dict = {
        "job": job,
        "status": status,
        "summary": f"{job} {status}",
        "command": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "artifact_path": None,
        "executed": False,
        "simulated": False,
    }
    body.update(extra)
    return body


def _unavailable(job: str, reason: str, **extra: object) -> dict:
    return _row(
        job,
        "unavailable",
        availability="unavailable",
        reason=reason,
        summary=f"{job} unavailable: {reason}",
        **extra,
    )


def _artifact(artifact_dir: Path | None, job: str, payload: dict) -> str | None:
    if artifact_dir is None:
        return None
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{job}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _php_lint(files: list[tuple[str, str]], artifact_dir: Path | None) -> dict:
    return run_php_lint(_php(files), artifact_dir)


def _woo_static(files: list[tuple[str, str]], artifact_dir: Path | None) -> dict:
    started = _now()
    joined = "\n".join(text for _rel, text in files)
    findings: list[str] = []
    if "WC tested up to" not in joined:
        findings.append("wc_tested_up_to")
    if "declare_compatibility" not in joined or "custom_order_tables" not in joined:
        findings.append("hpos_declaration")
    if "shop_order" in joined:
        findings.append("shop_order")
    status = "failed" if findings else "passed"
    row = _row(
        "woo_static",
        status,
        command="internal:woo_static",
        started_at=started,
        finished_at=_now(),
        exit_code=1 if findings else 0,
        executed=True,
        simulated=False,
        declaration_is_not_proof=True,
        findings=findings,
        summary=f"Woo static pre-check: {len(findings)} finding(s); not runtime verification",
    )
    row["artifact_path"] = _artifact(artifact_dir, "woo_static", {"job": "woo_static", "findings": findings})
    return row


def run_job(
    job: str,
    kind: str,
    files: list[tuple[str, str]],
    risk: str,
    *,
    artifact_dir: Path | None = None,
) -> dict:
    """Return process evidence. No unavailable or simulated check can pass."""
    if job not in JOBS:
        return _unavailable(job, "unknown_job")
    level = requirements(kind, files, risk)[job]
    if level == "not_applicable":
        return _row(job, "not_applicable", summary=f"{job} not applicable", reason="no_matching_files")
    if job == "php_lint":
        return _php_lint(files, artifact_dir)
    if job == "woo_static":
        return _woo_static(files, artifact_dir)
    return _unavailable(job, _UNAVAILABLE.get(job, "runtime_unavailable"))


def run_catalog(
    kind: str,
    files: list[tuple[str, str]],
    risk: str,
    *,
    artifact_dir: Path | None = None,
) -> list[dict]:
    return [run_job(job, kind, files, risk, artifact_dir=artifact_dir) for job in JOBS]


def suite_status(kind: str, files: list[tuple[str, str]], risk: str, results: list[dict]) -> str:
    """Required jobs must have actual non-simulated process evidence to pass."""
    needed = requirements(kind, files, risk)
    by_job = {row.get("job"): row for row in results if isinstance(row, dict)}
    failed = any(isinstance(row, dict) and row.get("status") == "failed" for row in by_job.values())
    incomplete = False
    for job, level in needed.items():
        row = by_job.get(job)
        if level != "required":
            continue
        if (
            not isinstance(row, dict)
            or row.get("status") != "passed"
            or row.get("executed") is not True
            or row.get("simulated") is not False
            or row.get("exit_code") != 0
            or not isinstance(row.get("command"), (str, list))
            or not row.get("started_at")
            or not row.get("finished_at")
        ):
            incomplete = True
    if failed:
        return "tests_failed"
    if incomplete:
        return "checks_incomplete"
    return "tests_passed"
