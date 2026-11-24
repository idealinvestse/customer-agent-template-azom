"""Named review jobs. The server owns the catalog. Nothing here runs PHP."""

from __future__ import annotations

import re

SYNTAX_MARKER = "AZOM_FAKE_PHP_SYNTAX_ERROR"
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


def _php(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(rel, text) for rel, text in files if rel.endswith(".php")]


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


_NOT_EXECUTED = {
    "php_lint": "php_binary_not_run",
    "wpcs_php": "phpcs_not_run",
    "plugin_check": "plugin_check_not_run",
    "phpunit": "phpunit_not_run",
    "assets_lint": "assets_lint_not_run",
    "browser_smoke": "browser_not_run",
    "egress_probe": "sandbox_not_run",
}


def _row(job: str, status: str, **extra: object) -> dict:
    availability = extra.get("availability")
    label = f"{status}/{availability}" if status == "not_run" and availability else status
    body: dict = {"job": job, "status": status, "summary": f"{job} {label}"}
    body.update(extra)
    return body


def _not_run(job: str, **extra: object) -> dict:
    reason = str(extra.pop("reason", None) or _NOT_EXECUTED.get(job) or "not_executed")
    return _row(job, "not_run", availability="unavailable", reason=reason, **extra)


def run_job(job: str, kind: str, files: list[tuple[str, str]], risk: str) -> dict:
    """Return a public result. Passed is only for a check that really ran."""
    if job not in JOBS:
        return _not_run(job, reason="unknown_job")
    level = requirements(kind, files, risk)[job]
    if level == "not_applicable":
        return _row(job, "not_applicable")
    if job == "php_lint":
        # Reading the marker is evidence of a bad file. Absence is not php -l.
        failed = any(SYNTAX_MARKER in text for _rel, text in _php(files))
        if failed:
            return _row(job, "failed", reason="syntax_marker")
        return _not_run(job)
    if job == "woo_static":
        joined = "\n".join(text for _rel, text in files)
        findings: list[str] = []
        if "WC tested up to" not in joined:
            findings.append("wc_tested_up_to")
        if "declare_compatibility" not in joined or "custom_order_tables" not in joined:
            findings.append("hpos_declaration")
        if "shop_order" in joined:
            findings.append("shop_order")
        status = "failed" if findings else "passed"
        return _row(job, status, declaration_is_not_proof=True, findings=findings)
    if job == "browser_smoke":
        del risk
        return _not_run(
            job,
            payments_enabled=False,
            checkout_enabled=False,
            desktop_screenshot=None,
            mobile_screenshot=None,
            console_errors=None,
            network_errors=None,
        )
    return _not_run(job)


def run_catalog(kind: str, files: list[tuple[str, str]], risk: str) -> list[dict]:
    return [run_job(job, kind, files, risk) for job in JOBS]


def suite_status(kind: str, files: list[tuple[str, str]], risk: str, results: list[dict]) -> str:
    """Passed only when every required job actually passed.

    A required job that did not run is ``checks_incomplete``. A real
    ``failed`` row is ``tests_failed`` even when another required job did
    not run. ``not_applicable`` and an optional ``not_run`` do not pass a
    required gap and do not by themselves fail the suite.
    """
    needed = requirements(kind, files, risk)
    by_job = {row.get("job"): row for row in results if isinstance(row, dict)}
    failed = any(isinstance(row, dict) and row.get("status") == "failed" for row in by_job.values())
    incomplete = False
    for job, level in needed.items():
        row = by_job.get(job)
        status = row.get("status") if isinstance(row, dict) else ""
        if level == "required" and status != "passed":
            incomplete = True
    if failed:
        return "tests_failed"
    if incomplete:
        return "checks_incomplete"
    return "tests_passed"
