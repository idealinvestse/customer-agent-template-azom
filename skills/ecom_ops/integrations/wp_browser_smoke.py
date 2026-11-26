"""Real loopback headless browser smoke and bound vertical theme render.

Uses a local Chromium-based browser (Chrome or Edge) over loopback Chrome
DevTools Protocol (``127.0.0.1`` only) with ``--host-resolver-rules`` blocking
every non-loopback host. Never contacts customer sites or external networks,
and never synthesizes a passing result when a binary, process, or render fails.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp.cdp_browser import (
    DEFAULT_VIEWPORTS as VIEWPORTS,
)
from ecom_ops.integrations.wp.cdp_browser import (
    PNG_MAGIC as _PNG_MAGIC,  # noqa: F401
)
from ecom_ops.integrations.wp.cdp_browser import (
    SANDBOX_CHROME_FLAGS,
    HeadlessBrowserSession,
    assert_loopback_http_url,
    collect_cdp_diagnostics,
    find_browser_binary,
)
from ecom_ops.integrations.wp.cdp_browser import (
    WP_FATAL_MARKERS as _WP_FATAL_MARKERS,
)
from ecom_ops.integrations.wp.cdp_browser import (
    LoopbackCdpClient as _LoopbackCdpClient,  # noqa: F401
)
from ecom_ops.integrations.wp.cdp_browser import (
    is_loopback_or_inline_url as _is_loopback_or_inline_url,  # noqa: F401
)
from ecom_ops.integrations.wp.cdp_browser import (
    png_dimensions as _png_dimensions,
)
from ecom_ops.integrations.wp.cdp_browser import (
    wait_cdp_page_ws as _wait_cdp_page_ws,  # noqa: F401
)
from ecom_ops.integrations.wp.preview_scripts import sandbox_dom_probe_script
from ecom_ops.integrations.wp.process_env import (
    build_php_child_env,  # noqa: F401
    terminate_process_tree,  # noqa: F401
)
from ecom_ops.integrations.wp.process_env import (
    pick_loopback_port as _pick_loopback_port,  # noqa: F401
)
from ecom_ops.integrations.wp.process_env import (
    utc_now_iso as _now,
)
from ecom_ops.integrations.wp_local_runtime import (
    LocalWordPressSandbox,
    make_local_sandbox_gate,
    resolve_local_runtime,
    runtime_dir_from_env,
)
from ecom_ops.integrations.wp_review_process import run_php_lint
from ecom_ops.integrations.wp_runtime_fetch import RuntimeFetchError
from ecom_ops.wp_ops.review_checks import (
    JOBS,
    PROFILE,
    PROFILE_VERSION,
    bind_artifacts,
    requirements,
    run_job,
    stamp_run,
    suite_status,
)
from ecom_ops.wp_ops.review_runner import sandbox_enabled
from ecom_ops.wp_ops.review_workspace import ReviewError, iter_files, tree_hash


def run_browser_render(
    url: str,
    artifact_dir: Path,
    *,
    label: str,
    browser_binary: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Render one loopback URL at desktop and mobile viewports and capture real DOM/PNG evidence."""
    assert_loopback_http_url(url)
    binary = browser_binary or find_browser_binary()
    if not binary:
        return {
            "ok": False,
            "executed": False,
            "simulated": False,
            "availability": "unavailable",
            "reason": "browser_binary_missing",
            "label": label,
            "viewports": {},
        }

    artifact_dir = Path(artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    started = _now()
    viewports_out: dict[str, Any] = {}
    egress_attempts: list[dict[str, str]] = []

    with HeadlessBrowserSession(
        binary,
        extra_flags=SANDBOX_CHROME_FLAGS,
        profile_prefix="azom-wp-browser-",
        timeout_s=timeout_s,
        cdp_timeout_s=timeout_s,
    ) as session:
        cdp = session.cdp
        for name, width, height, is_mobile in VIEWPORTS:
            cdp.events.clear()
            cdp.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": is_mobile},
            )
            cdp.call("Network.setCacheDisabled", {"cacheDisabled": True})
            nav = cdp.call("Page.navigate", {"url": url})
            frame_id = str(nav.get("frameId") or "")
            loaded = cdp.wait_for_event("Page.loadEventFired", timeout_s=min(15.0, timeout_s))
            cdp.drain_events(duration_s=0.4)

            dom_info = cdp.eval_json(sandbox_dom_probe_script())
            outer_html = str(dom_info.get("outerHtml") or "")
            body_text = str(dom_info.get("bodyText") or "")

            shot = cdp.call("Page.captureScreenshot", {"format": "png"})
            png_bytes = base64.b64decode(str(shot.get("data") or ""))
            dims = _png_dimensions(png_bytes)
            shot_path = artifact_dir / f"{label}-{name}.png"
            shot_path.write_bytes(png_bytes)

            diag = collect_cdp_diagnostics(
                cdp.events,
                frame_id=frame_id,
                ignore_blocked_by_client=False,
                track_egress_source=f"browser_{name}",
                max_msg_len=300,
                max_url_len=240,
            )
            doc_status = diag["doc_status"]
            console_errors = diag["console_errors"]
            js_exceptions = diag["js_exceptions"]
            network_errors = diag["network_errors"]
            egress_attempts.extend(diag["egress_attempts"])

            lower_text = f"{body_text}\n{outer_html}".lower()
            fatal_markers = [marker for marker in _WP_FATAL_MARKERS if marker in lower_text]
            viewport_ok = (
                loaded
                and doc_status == 200
                and dims == (width, height)
                and bool(body_text)
                and not console_errors
                and not js_exceptions
                and not network_errors
                and not fatal_markers
            )
            viewports_out[name] = {
                "ok": viewport_ok,
                "loaded": loaded,
                "http_status": doc_status,
                "viewport": {"width": width, "height": height, "mobile": is_mobile},
                "screenshot_path": str(shot_path),
                "screenshot_bytes": len(png_bytes),
                "screenshot_dimensions": list(dims) if dims else None,
                "title": str(dom_info.get("title") or "")[:200],
                "body_text": body_text[:500],
                "body_bg": str(dom_info.get("bodyBg") or "")[:80],
                "theme_marker": str(dom_info.get("marker") or "")[:120],
                "html_bytes": len(outer_html.encode("utf-8")),
                "html_sha256": hashlib.sha256(outer_html.encode("utf-8")).hexdigest(),
                "console_errors": console_errors,
                "js_exceptions": js_exceptions,
                "network_errors": network_errors,
                "fatal_markers": fatal_markers,
            }

    finished = _now()
    all_ok = bool(viewports_out) and all(item.get("ok") is True for item in viewports_out.values())
    return {
        "ok": all_ok,
        "executed": True,
        "simulated": False,
        "label": label,
        "command": [binary, "--headless=new", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"],
        "started_at": started,
        "finished_at": finished,
        "viewports": viewports_out,
        "egress_attempts": egress_attempts,
    }


def _text_files(folder: Path) -> list[tuple[str, str]]:
    return [(rel, data.decode("utf-8", errors="replace")) for rel, data in iter_files(folder)]


def _write_egress_probe(
    artifact_dir: Path,
    all_egress: list[dict[str, str]],
    *,
    started_at: str,
    finished_at: str,
    browser_isolated: bool,
) -> dict[str, Any]:
    egress_artifact = artifact_dir / "egress_probe.json"
    egress_artifact.write_text(
        json.dumps(
            {
                "job": "egress_probe",
                "blocked_attempts": all_egress,
                "wordpress_block_external": True,
                "browser_host_resolver_isolated": browser_isolated,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    egress_ok = len(all_egress) == 0
    return {
        "job": "egress_probe",
        "status": "passed" if egress_ok else "failed",
        "summary": (
            "egress_probe passed: 0 non-loopback requests"
            if egress_ok
            else f"egress_probe failed: {len(all_egress)} blocked external request(s)"
        ),
        "command": "internal:loopback_egress_guard",
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": 0 if egress_ok else 1,
        "artifact_path": str(egress_artifact),
        "executed": True,
        "simulated": False,
    }


def _build_binding(
    *,
    run_id: str,
    site_id: str,
    plan_id: str,
    base_sha256: str,
    work_sha256: str,
    policy: dict[str, str],
    started_at: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "site_id": site_id,
        "plan_id": plan_id,
        "base_sha256": base_sha256,
        "work_sha256": work_sha256,
        "policy": policy,
        "profile": PROFILE,
        "profile_version": PROFILE_VERSION,
        "started_at": started_at,
        "finished_at": _now(),
        "published": False,
    }


def _probe_loopback_http(url: str) -> dict[str, Any]:
    """Perform a strict loopback HTTP GET to verify plugin boot without a browser."""
    assert_loopback_http_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    http_status: int | None = None
    http_body = ""
    http_error_msg: str | None = None
    try:
        with opener.open(url, timeout=10.0) as resp:
            http_status = int(resp.status)
            http_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        try:
            http_body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            http_body = ""
    except (urllib.error.URLError, OSError) as exc:
        http_error_msg = str(exc)[:300]

    lower_body = http_body.lower()
    fatal_markers = [marker for marker in _WP_FATAL_MARKERS if marker in lower_body]
    ok = http_error_msg is None and http_status == 200 and bool(http_body.strip()) and not fatal_markers
    return {
        "ok": ok,
        "http_status": http_status,
        "fatal_markers": fatal_markers,
        "error": http_error_msg,
        "body_bytes": len(http_body.encode("utf-8")),
    }


def _classify_theme_render(
    revisions: dict[str, Any],
    *,
    slug: str,
    php_errors: dict[str, list[str]],
    synthetic_parent: bool = False,
) -> tuple[bool, str | None]:
    """Return ``(representative, reason)`` for a local sandbox theme render."""
    for rev in revisions.values():
        if not isinstance(rev, dict):
            continue
        for vp in (rev.get("viewports") or {}).values():
            if not isinstance(vp, dict):
                continue
            if vp.get("theme_marker") == "azom-plugin-host" and slug != "azom-plugin-host":
                return False, "sandbox_not_representative"
    if synthetic_parent:
        work_errors = "\n".join(str(line) for line in (php_errors.get("work") or []))
        if "Call to undefined function" in work_errors:
            return False, "sandbox_not_representative"
    return True, None


def run_vertical_theme_render(
    *,
    site: object,
    site_id: str,
    plan_id: str,
    slug: str,
    base_dir: Path,
    work_dir: Path,
    artifact_dir: Path,
    risk: str = "low",
    runtime_dir: Path | None = None,
    browser_binary: str | None = None,
    parent_theme_slug: str | None = None,
    parent_theme_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one opt-in, bound vertical render of a synthetic theme (base + work, desktop + mobile)."""
    if not sandbox_enabled():
        raise ReviewError("SandboxDisabled")
    resolved_runtime = runtime_dir_from_env(runtime_dir)
    try:
        manifest = resolve_local_runtime(resolved_runtime)
    except (RuntimeFetchError, OSError) as exc:
        raise ReviewError("SandboxRuntimeUnavailable") from exc

    base_dir = Path(base_dir).resolve()
    work_dir = Path(work_dir).resolve()
    artifact_dir = Path(artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    base_sha256 = tree_hash(base_dir)
    work_sha256 = tree_hash(work_dir)
    work_files = _text_files(work_dir)
    policy = requirements("theme", work_files, risk)
    run_id = "cr-" + secrets.token_hex(6)
    started = _now()

    lint_runtime = None if manifest.get("system_php") else resolved_runtime
    lint_row = (
        run_php_lint(work_files, artifact_dir, runtime_dir=lint_runtime)
        if policy["php_lint"] != "not_applicable"
        else run_job("php_lint", "theme", work_files, risk)
    )

    revisions: dict[str, Any] = {}
    sandbox_records: dict[str, Any] = {}
    all_egress: list[dict[str, str]] = []
    all_php_errors: dict[str, list[str]] = {}

    browser_started = _now()
    for rev_name, rev_dir in (("base", base_dir), ("work", work_dir)):
        sb_dir = Path(tempfile.mkdtemp(prefix=f"azom-wp-sb-{rev_name}-"))
        sandbox = LocalWordPressSandbox(
            plan_id=plan_id,
            sandbox_dir=sb_dir,
            theme_slug=slug,
            theme_dir=rev_dir,
            runtime_dir=resolved_runtime,
            parent_theme_slug=parent_theme_slug,
            parent_theme_dir=parent_theme_dir,
        )
        gate = make_local_sandbox_gate(sandbox)
        record: dict[str, Any] | None = None
        try:
            record = gate.start(site, plan_id, high_risk=(risk == "high"))
            sandbox_records[rev_name] = dict(record)
            render = run_browser_render(
                sandbox.url,
                artifact_dir,
                label=rev_name,
                browser_binary=browser_binary,
            )
            php_errors = sandbox.read_php_errors()
            wp_egress = sandbox.read_egress_attempts()
            all_php_errors[rev_name] = php_errors
            all_egress.extend(wp_egress)
            all_egress.extend(render.get("egress_attempts") or [])
            render["php_errors"] = php_errors
            render["wp_egress_attempts"] = wp_egress
            render["ok"] = bool(render.get("ok")) and not php_errors
            revisions[rev_name] = render
        finally:
            if record is not None:
                sandbox_records[f"{rev_name}_stopped"] = gate.stop(record)
            else:
                sandbox.stop()
            shutil.rmtree(sb_dir, ignore_errors=True)
    browser_finished = _now()

    browser_executed = all(revisions.get(k, {}).get("executed") is True for k in ("base", "work"))
    browser_ok = browser_executed and all(revisions.get(k, {}).get("ok") is True for k in ("base", "work"))
    browser_cmd = revisions.get("work", {}).get("command") or revisions.get("base", {}).get("command")
    representative, non_rep_reason = _classify_theme_render(
        revisions,
        slug=slug,
        php_errors=all_php_errors,
        synthetic_parent=bool(parent_theme_slug is not None and parent_theme_dir is None),
    )

    if not browser_executed:
        browser_row: dict[str, Any] = {
            "job": "browser_smoke",
            "status": "unavailable",
            "availability": "unavailable",
            "reason": "browser_binary_missing",
            "summary": "browser_smoke unavailable: browser_binary_missing",
            "command": None,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "artifact_path": None,
            "executed": False,
            "simulated": False,
        }
    else:
        if not representative:
            for png_name in ("base-desktop.png", "base-mobile.png", "work-desktop.png", "work-mobile.png"):
                png_path = Path(artifact_dir) / png_name
                try:
                    if png_path.is_file() or png_path.is_symlink():
                        png_path.unlink()
                except OSError:
                    pass
        browser_artifact = artifact_dir / "browser_smoke.json"
        browser_artifact.write_text(
            json.dumps(
                {
                    "job": "browser_smoke",
                    "wordpress_version": manifest["wordpress_version"],
                    "php_version": manifest["php_version"],
                    "representative": representative,
                    "reason": non_rep_reason,
                    "revisions": revisions,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if not representative:
            browser_row = {
                "job": "browser_smoke",
                "status": "unavailable",
                "availability": "unavailable",
                "reason": non_rep_reason or "sandbox_not_representative",
                "summary": "browser_smoke unavailable: sandlådan saknar temats föräldratema/plugins (bara på servern); se live-förhandsvisning",
                "command": browser_cmd,
                "started_at": browser_started,
                "finished_at": browser_finished,
                "exit_code": None,
                "artifact_path": str(browser_artifact),
                "executed": True,
                "simulated": False,
            }
        else:
            browser_row = {
                "job": "browser_smoke",
                "status": "passed" if browser_ok else "failed",
                "summary": (
                    "browser_smoke passed: base and work rendered at desktop and mobile"
                    if browser_ok
                    else "browser_smoke failed: render or PHP error detected"
                ),
                "command": browser_cmd,
                "started_at": browser_started,
                "finished_at": browser_finished,
                "exit_code": 0 if browser_ok else 1,
                "artifact_path": str(browser_artifact),
                "executed": True,
                "simulated": False,
            }

    egress_row = _write_egress_probe(
        artifact_dir,
        all_egress,
        started_at=browser_started,
        finished_at=browser_finished,
        browser_isolated=True,
    )

    rows: list[dict[str, Any]] = []
    for job in JOBS:
        if job == "php_lint":
            rows.append(lint_row)
        elif job == "browser_smoke":
            rows.append(browser_row if policy["browser_smoke"] != "not_applicable" else run_job(job, "theme", work_files, risk))
        elif job == "egress_probe":
            rows.append(egress_row)
        else:
            rows.append(run_job(job, "theme", work_files, risk, artifact_dir=artifact_dir))

    stamped = stamp_run(
        rows,
        run_id=run_id,
        site_id=site_id,
        plan_id=plan_id,
        base_sha256=base_sha256,
        work_sha256=work_sha256,
        policy=policy,
        profile=PROFILE,
        profile_version=PROFILE_VERSION,
    )
    status = suite_status("theme", work_files, risk, stamped)
    binding = _build_binding(
        run_id=run_id,
        site_id=site_id,
        plan_id=plan_id,
        base_sha256=base_sha256,
        work_sha256=work_sha256,
        policy=policy,
        started_at=started,
    )
    bind_artifacts(artifact_dir, stamped, binding)

    return {
        "run_id": run_id,
        "status": status,
        "binding": binding,
        "checks": stamped,
        "sandbox_records": sandbox_records,
        "revisions": revisions,
        "egress_attempts": all_egress,
        "php_errors": all_php_errors,
    }


def run_vertical_plugin_smoke(
    *,
    site: object,
    site_id: str,
    plan_id: str,
    slug: str,
    base_dir: Path,
    work_dir: Path,
    artifact_dir: Path,
    risk: str = "low",
    runtime_dir: Path | None = None,
    browser_binary: str | None = None,
) -> dict[str, Any]:
    """Run one opt-in, bound vertical smoke of an isolated plugin on a synthetic host theme."""
    if not sandbox_enabled():
        raise ReviewError("SandboxDisabled")
    resolved_runtime = runtime_dir_from_env(runtime_dir)
    try:
        manifest = resolve_local_runtime(resolved_runtime)
    except (RuntimeFetchError, OSError) as exc:
        raise ReviewError("SandboxRuntimeUnavailable") from exc

    base_dir = Path(base_dir).resolve()
    work_dir = Path(work_dir).resolve()
    artifact_dir = Path(artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    base_sha256 = tree_hash(base_dir)
    work_sha256 = tree_hash(work_dir)
    work_files = _text_files(work_dir)
    policy = requirements("plugin", work_files, risk)
    run_id = "cr-" + secrets.token_hex(6)
    started = _now()

    lint_runtime = None if manifest.get("system_php") else resolved_runtime
    lint_row = (
        run_php_lint(work_files, artifact_dir, runtime_dir=lint_runtime)
        if policy["php_lint"] != "not_applicable"
        else run_job("php_lint", "plugin", work_files, risk)
    )

    sandbox_records: dict[str, Any] = {}
    php_errors: list[str] = []
    all_egress: list[dict[str, str]] = []
    render: dict[str, Any] = {}
    plugin_boot_ok = False

    probe_started = _now()
    sb_dir = Path(tempfile.mkdtemp(prefix="azom-wp-sb-plugin-"))
    sandbox = LocalWordPressSandbox(
        plan_id=plan_id,
        sandbox_dir=sb_dir,
        plugin_slug=slug,
        plugin_dir=work_dir,
        runtime_dir=resolved_runtime,
    )
    gate = make_local_sandbox_gate(sandbox)
    record: dict[str, Any] | None = None
    try:
        record = gate.start(site, plan_id, high_risk=(risk == "high"))
        sandbox_records["work"] = dict(record)
        render = run_browser_render(
            sandbox.url,
            artifact_dir,
            label="work",
            browser_binary=browser_binary,
        )
        if render.get("reason") == "browser_binary_missing":
            http_probe = _probe_loopback_http(sandbox.url)
            render["http_probe"] = http_probe
            boot_signal_ok = bool(http_probe.get("ok"))
        else:
            boot_signal_ok = bool(render.get("ok"))

        php_errors = sandbox.read_php_errors()
        wp_egress = sandbox.read_egress_attempts()
        all_egress = wp_egress + list(render.get("egress_attempts") or [])
        render["php_errors"] = php_errors
        render["wp_egress_attempts"] = wp_egress
        if render.get("executed") is True:
            render["ok"] = bool(render.get("ok")) and not php_errors
        plugin_boot_ok = boot_signal_ok and not php_errors
    finally:
        if record is not None:
            sandbox_records["work_stopped"] = gate.stop(record)
        else:
            sandbox.stop()
        shutil.rmtree(sb_dir, ignore_errors=True)
    probe_finished = _now()

    egress_row = _write_egress_probe(
        artifact_dir,
        all_egress,
        started_at=probe_started,
        finished_at=probe_finished,
        browser_isolated=bool(render.get("executed")),
    )

    rows: list[dict[str, Any]] = []
    for job in JOBS:
        if job == "php_lint":
            rows.append(lint_row)
        elif job == "egress_probe":
            rows.append(egress_row)
        elif job == "browser_smoke":
            rows.append(run_job("browser_smoke", "plugin", work_files, risk))
        else:
            rows.append(run_job(job, "plugin", work_files, risk, artifact_dir=artifact_dir))

    stamped = stamp_run(
        rows,
        run_id=run_id,
        site_id=site_id,
        plan_id=plan_id,
        base_sha256=base_sha256,
        work_sha256=work_sha256,
        policy=policy,
        profile=PROFILE,
        profile_version=PROFILE_VERSION,
    )
    status = suite_status("plugin", work_files, risk, stamped)
    binding = _build_binding(
        run_id=run_id,
        site_id=site_id,
        plan_id=plan_id,
        base_sha256=base_sha256,
        work_sha256=work_sha256,
        policy=policy,
        started_at=started,
    )
    bind_artifacts(artifact_dir, stamped, binding)

    return {
        "run_id": run_id,
        "status": status,
        "plugin_boot_ok": plugin_boot_ok,
        "binding": binding,
        "checks": stamped,
        "sandbox_records": sandbox_records,
        "revisions": {"work": render},
        "egress_attempts": all_egress,
        "php_errors": {"work": php_errors},
    }
