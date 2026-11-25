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
import os
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp_local_runtime import (
    LocalWordPressSandbox,
    build_php_child_env,
    make_local_sandbox_gate,
    runtime_dir_from_env,
)
from ecom_ops.integrations.wp_review_process import run_php_lint
from ecom_ops.integrations.wp_runtime_fetch import RuntimeFetchError, check_runtime
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

VIEWPORTS: tuple[tuple[str, int, int, bool], ...] = (
    ("desktop", 1280, 800, False),
    ("mobile", 390, 844, True),
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_WP_FATAL_MARKERS = (
    "there has been a critical error on this website",
    "fatal error:",
    "parse error:",
    "uncaught error:",
    "syntax error,",
)
_WINDOWS_BROWSER_PATHS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
_PATH_BROWSER_NAMES = ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def find_browser_binary() -> str | None:
    """Locate a local Chromium/Edge binary. Never reads arbitrary model input."""
    for candidate in _WINDOWS_BROWSER_PATHS:
        if candidate.is_file() and not candidate.is_symlink():
            return str(candidate)
    for name in _PATH_BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def assert_loopback_http_url(url: str) -> urllib.parse.ParseResult:
    """Refuse anything other than an explicit ``http://127.0.0.1:<port>/...`` URL."""
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise ReviewError("NonLoopbackUrlRefused")
    if parsed.username or parsed.password:
        raise ReviewError("NonLoopbackUrlRefused")
    return parsed


def _pick_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(_PNG_MAGIC) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _is_loopback_or_inline_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw or raw.startswith(("data:", "about:", "blob:", "chrome:", "devtools:")):
        return True
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


class _LoopbackCdpClient:
    """Minimal RFC 6455 WebSocket client bound strictly to ``127.0.0.1``."""

    def __init__(self, ws_url: str, timeout_s: float = 15.0) -> None:
        parsed = urllib.parse.urlparse(ws_url)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or not parsed.port:
            raise ReviewError("NonLoopbackUrlRefused")
        self._sock = socket.create_connection(("127.0.0.1", parsed.port), timeout=timeout_s)
        self._sock.settimeout(timeout_s)
        self._buf = bytearray()
        self._next_id = 1
        self.events: list[dict[str, Any]] = []
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        self._handshake(parsed.port, path)

    def _handshake(self, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._sock.sendall(request)
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError("CDP handshake closed early")
            self._buf.extend(chunk)
        sep = self._buf.index(b"\r\n\r\n")
        header_text = self._buf[:sep].decode("latin-1", errors="replace")
        del self._buf[: sep + 4]
        status_line = header_text.splitlines()[0] if header_text else ""
        if " 101 " not in status_line:
            raise OSError(f"CDP upgrade failed: {status_line}")

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise OSError("CDP WebSocket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + mask + masked)

    def _recv_message(self) -> str:
        fragments: list[bytes] = []
        while True:
            first, second = self._read_exact(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise OSError("CDP WebSocket closed by peer")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            fragments.append(payload)
            if fin:
                return b"".join(fragments).decode("utf-8", errors="replace")

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 15.0) -> dict[str, Any]:
        msg_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode("utf-8")
        self._send_frame(0x1, payload)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining = max(0.2, deadline - time.monotonic())
            self._sock.settimeout(remaining)
            raw = self._recv_message()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error for {method}: {data['error']}")
                return dict(data.get("result") or {})
            if "method" in data:
                self.events.append(data)
        raise TimeoutError(f"CDP timeout waiting for {method}")

    def wait_for_event(self, method: str, *, timeout_s: float = 15.0) -> bool:
        for index, ev in enumerate(self.events):
            if ev.get("method") == method:
                del self.events[index]
                return True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            self._sock.settimeout(remaining)
            try:
                raw = self._recv_message()
            except TimeoutError:
                return False
            data = json.loads(raw)
            if data.get("method") == method:
                return True
            if "method" in data:
                self.events.append(data)
        return False

    def drain_events(self, *, duration_s: float = 0.35) -> None:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            self._sock.settimeout(remaining)
            try:
                raw = self._recv_message()
            except TimeoutError:
                return
            data = json.loads(raw)
            if "method" in data:
                self.events.append(data)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _wait_cdp_page_ws(cdp_port: int, proc: subprocess.Popen[bytes], *, timeout_s: float = 15.0) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    list_url = f"http://127.0.0.1:{cdp_port}/json/list"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ReviewError("BrowserProcessExited")
        try:
            with opener.open(list_url, timeout=2.0) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            if isinstance(targets, list):
                for item in targets:
                    if isinstance(item, dict) and item.get("type") == "page" and item.get("webSocketDebuggerUrl"):
                        return str(item["webSocketDebuggerUrl"])
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(0.15)
    raise ReviewError("BrowserCdpUnavailable")


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
    cdp_port = _pick_loopback_port()
    env = build_php_child_env()
    user_data_dir = Path(tempfile.mkdtemp(prefix="azom-wp-browser-"))
    command = [
        binary,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--metrics-recording-only",
        "--mute-audio",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "about:blank",
    ]
    proc: subprocess.Popen[bytes] | None = None
    cdp: _LoopbackCdpClient | None = None
    started = _now()
    viewports_out: dict[str, Any] = {}
    egress_attempts: list[dict[str, str]] = []
    try:
        proc = subprocess.Popen(
            command,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        ws_url = _wait_cdp_page_ws(cdp_port, proc, timeout_s=min(15.0, timeout_s))
        cdp = _LoopbackCdpClient(ws_url, timeout_s=timeout_s)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Network.enable")
        cdp.call("Log.enable")

        for name, width, height, is_mobile in VIEWPORTS:
            cdp.events.clear()
            cdp.call(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": width,
                    "height": height,
                    "deviceScaleFactor": 1,
                    "mobile": is_mobile,
                },
            )
            cdp.call("Network.setCacheDisabled", {"cacheDisabled": True})
            nav = cdp.call("Page.navigate", {"url": url})
            frame_id = str(nav.get("frameId") or "")
            loaded = cdp.wait_for_event("Page.loadEventFired", timeout_s=min(15.0, timeout_s))
            cdp.drain_events(duration_s=0.4)

            eval_res = cdp.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => {"
                        "  const body = document.body;"
                        "  const style = body ? window.getComputedStyle(body) : null;"
                        "  const marker = document.querySelector('[data-azom-theme-marker]');"
                        "  return JSON.stringify({"
                        "    readyState: document.readyState,"
                        "    title: document.title || '',"
                        "    bodyText: body ? (body.innerText || '').trim().slice(0, 2000) : '',"
                        "    outerHtml: document.documentElement ? document.documentElement.outerHTML : '',"
                        "    bodyBg: style ? style.backgroundColor : '',"
                        "    marker: marker ? (marker.getAttribute('data-azom-theme-marker') || '') : '',"
                        "    viewportWidth: window.innerWidth,"
                        "    viewportHeight: window.innerHeight"
                        "  });"
                        "})()"
                    ),
                    "returnByValue": True,
                },
            )
            raw_dom = (eval_res.get("result") or {}).get("value") or "{}"
            dom_info = json.loads(raw_dom) if isinstance(raw_dom, str) else {}
            outer_html = str(dom_info.get("outerHtml") or "")
            body_text = str(dom_info.get("bodyText") or "")

            shot = cdp.call("Page.captureScreenshot", {"format": "png"})
            png_bytes = base64.b64decode(str(shot.get("data") or ""))
            dims = _png_dimensions(png_bytes)
            shot_path = artifact_dir / f"{label}-{name}.png"
            shot_path.write_bytes(png_bytes)

            doc_status: int | None = None
            console_errors: list[str] = []
            js_exceptions: list[str] = []
            network_errors: list[dict[str, Any]] = []
            request_urls: dict[str, str] = {}

            for ev in cdp.events:
                method = ev.get("method")
                params = ev.get("params") or {}
                if method == "Network.requestWillBeSent":
                    req_id = str(params.get("requestId") or "")
                    req_url = str((params.get("request") or {}).get("url") or "")
                    if req_id:
                        request_urls[req_id] = req_url
                    if not _is_loopback_or_inline_url(req_url):
                        parsed_ext = urllib.parse.urlparse(req_url)
                        egress_attempts.append(
                            {
                                "url": req_url[:300],
                                "host": (parsed_ext.hostname or "")[:120],
                                "source": f"browser_{name}",
                            }
                        )
                elif method == "Network.responseReceived":
                    response = params.get("response") or {}
                    status_code = int(response.get("status") or 0)
                    res_type = str(params.get("type") or "")
                    res_url = str(response.get("url") or "")
                    if res_type == "Document" and (not frame_id or params.get("frameId") == frame_id):
                        doc_status = status_code
                    if status_code >= 400:
                        network_errors.append({"url": res_url[:240], "status": status_code, "type": res_type})
                elif method == "Network.loadingFailed":
                    req_id = str(params.get("requestId") or "")
                    req_url = request_urls.get(req_id, "")
                    if not str(req_url).endswith("/favicon.ico"):
                        network_errors.append(
                            {
                                "url": req_url[:240],
                                "error_text": str(params.get("errorText") or "loading_failed")[:120],
                                "type": str(params.get("type") or ""),
                            }
                        )
                elif method == "Runtime.consoleAPICalled":
                    if params.get("type") == "error":
                        parts = [str(arg.get("value") or arg.get("description") or "") for arg in params.get("args") or []]
                        console_errors.append(" ".join(parts).strip()[:300])
                elif method == "Log.entryAdded":
                    entry = params.get("entry") or {}
                    if entry.get("level") == "error":
                        text = str(entry.get("text") or "")
                        if "favicon.ico" not in text:
                            console_errors.append(text[:300])
                elif method == "Runtime.exceptionThrown":
                    details = params.get("exceptionDetails") or {}
                    text = str(details.get("text") or "Uncaught exception")
                    desc = str((details.get("exception") or {}).get("description") or "")
                    js_exceptions.append((f"{text}: {desc}" if desc else text)[:300])

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
    finally:
        if cdp is not None:
            cdp.close()
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
        shutil.rmtree(user_data_dir, ignore_errors=True)

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
) -> dict[str, Any]:
    """Run one opt-in, bound vertical render of a synthetic theme (base + work, desktop + mobile)."""
    if not sandbox_enabled():
        raise ReviewError("SandboxDisabled")
    resolved_runtime = runtime_dir_from_env(runtime_dir)
    try:
        manifest = check_runtime(resolved_runtime)
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

    lint_row = run_php_lint(work_files, artifact_dir, runtime_dir=resolved_runtime)

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
        browser_artifact = artifact_dir / "browser_smoke.json"
        browser_artifact.write_text(
            json.dumps(
                {
                    "job": "browser_smoke",
                    "wordpress_version": manifest["wordpress_version"],
                    "php_version": manifest["php_version"],
                    "revisions": revisions,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
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

    egress_artifact = artifact_dir / "egress_probe.json"
    egress_artifact.write_text(
        json.dumps(
            {
                "job": "egress_probe",
                "blocked_attempts": all_egress,
                "wordpress_block_external": True,
                "browser_host_resolver_isolated": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    egress_ok = len(all_egress) == 0
    egress_row = {
        "job": "egress_probe",
        "status": "passed" if egress_ok else "failed",
        "summary": (
            "egress_probe passed: 0 non-loopback requests"
            if egress_ok
            else f"egress_probe failed: {len(all_egress)} blocked external request(s)"
        ),
        "command": "internal:loopback_egress_guard",
        "started_at": browser_started,
        "finished_at": browser_finished,
        "exit_code": 0 if egress_ok else 1,
        "artifact_path": str(egress_artifact),
        "executed": True,
        "simulated": False,
    }

    rows: list[dict[str, Any]] = []
    for job in JOBS:
        if job == "php_lint":
            rows.append(lint_row if policy["php_lint"] != "not_applicable" else run_job(job, "theme", work_files, risk))
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
    binding = {
        "run_id": run_id,
        "site_id": site_id,
        "plan_id": plan_id,
        "base_sha256": base_sha256,
        "work_sha256": work_sha256,
        "policy": policy,
        "profile": PROFILE,
        "profile_version": PROFILE_VERSION,
        "started_at": started,
        "finished_at": _now(),
        "published": False,
    }
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
