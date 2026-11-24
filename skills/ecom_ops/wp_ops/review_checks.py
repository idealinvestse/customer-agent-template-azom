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
PROFILE = "wp-review-catalog"
PROFILE_VERSION = "1"
_POLICY = frozenset({"required", "optional", "not_applicable"})


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
        evidence_class="static_precheck",
        runtime_proof=False,
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


def _passed_row(row: object) -> bool:
    if not isinstance(row, dict) or row.get("status") != "passed":
        return False
    if row.get("executed") is not True or row.get("simulated") is not False or row.get("exit_code") != 0:
        return False
    if not isinstance(row.get("command"), (str, list)) or not row.get("started_at") or not row.get("finished_at"):
        return False
    if row.get("job") == "woo_static":
        if row.get("evidence_class") != "static_precheck" or row.get("runtime_proof") is not False:
            return False
    return True


def suite_status(kind: str, files: list[tuple[str, str]], risk: str, results: list[dict]) -> str:
    """Required jobs need real process evidence. Policy comes from ``requirements``."""
    needed = requirements(kind, files, risk)
    rows = [row for row in results if isinstance(row, dict)]
    jobs = [row.get("job") for row in rows]
    duplicate = len(jobs) != len(set(jobs))
    by_job = {row.get("job"): row for row in rows}
    failed = any(row.get("status") == "failed" for row in rows)
    incomplete = duplicate
    for job, level in needed.items():
        row = by_job.get(job)
        if level == "not_applicable":
            if isinstance(row, dict) and row.get("status") == "passed":
                incomplete = True
            continue
        if level != "required":
            continue
        if not _passed_row(row):
            incomplete = True
    if failed:
        return "tests_failed"
    if incomplete:
        return "checks_incomplete"
    return "tests_passed"


def stamp_run(
    results: list[dict],
    *,
    run_id: str,
    site_id: str,
    plan_id: str,
    base_sha256: str,
    work_sha256: str,
    policy: dict[str, str],
    profile: str,
    profile_version: str,
) -> list[dict]:
    """Copy check rows and bind them to one snapshot. Source text stays out."""
    stamped: list[dict] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        job = str(item.get("job") or "")
        item["run_id"] = run_id
        item["site_id"] = site_id
        item["plan_id"] = plan_id
        item["base_sha256"] = base_sha256
        item["work_sha256"] = work_sha256
        item["policy"] = policy.get(job, "not_applicable")
        item["profile"] = profile
        item["profile_version"] = profile_version
        if job == "woo_static":
            item["evidence_class"] = "static_precheck"
            item["runtime_proof"] = False
        stamped.append(item)
    return stamped


def bind_artifacts(artifact_dir: Path | None, results: list[dict], binding: dict) -> None:
    """Add the run id to JSON artifacts that this run already wrote."""
    if artifact_dir is None:
        return
    root = artifact_dir.resolve()
    extra = {
        key: binding.get(key)
        for key in (
            "run_id",
            "site_id",
            "plan_id",
            "base_sha256",
            "work_sha256",
            "profile",
            "profile_version",
        )
    }
    for row in results:
        raw = row.get("artifact_path")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        if path.parent != root or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload["evidence"] = extra
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_is_publishable(
    kind: str,
    files: list[tuple[str, str]],
    risk: str,
    results: list[dict],
    binding: dict,
) -> bool:
    """True only when the server policy and this snapshot's rows still agree."""
    if suite_status(kind, files, risk, results) != "tests_passed":
        return False
    if not isinstance(binding, dict) or binding.get("published") is not True:
        return False
    run_id = binding.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return False
    if binding.get("profile") != PROFILE or binding.get("profile_version") != PROFILE_VERSION:
        return False
    needed = requirements(kind, files, risk)
    if binding.get("policy") != needed:
        return False
    by_job = {row.get("job"): row for row in results if isinstance(row, dict)}
    for job, level in needed.items():
        if level != "required":
            continue
        row = by_job.get(job)
        if not isinstance(row, dict) or row.get("run_id") != run_id or row.get("policy") != level:
            return False
        for key in ("site_id", "plan_id", "base_sha256", "work_sha256", "profile", "profile_version"):
            if row.get(key) != binding.get(key):
                return False
    return True


def public_policy(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    policy: dict[str, str] = {}
    for key, value in raw.items():
        if key in JOBS and value in _POLICY:
            policy[str(key)] = str(value)
    return policy
