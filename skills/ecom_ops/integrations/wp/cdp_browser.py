"""Shared loopback headless Chrome CDP client, session manager, Fetch interceptor, and diagnostics."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp.http_guard import classify_request
from ecom_ops.integrations.wp.png_composite import PNG_MAGIC, png_dimensions
from ecom_ops.integrations.wp.process_env import (
    build_php_child_env,
    pick_loopback_port,
    terminate_process_tree,
)
from ecom_ops.wp_ops.review_workspace import ReviewError

DEFAULT_VIEWPORTS: tuple[tuple[str, int, int, bool], ...] = (
    ("desktop", 1280, 800, False),
    ("mobile", 390, 844, True),
)
WP_FATAL_MARKERS: tuple[str, ...] = (
    "there has been a critical error on this website",
    "fatal error:",
    "parse error:",
    "uncaught error:",
    "syntax error,",
)
_WINDOWS_BROWSER_PATHS: tuple[Path, ...] = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
_PATH_BROWSER_NAMES: tuple[str, ...] = (
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
    "msedge",
)
_BASE_CHROME_FLAGS: tuple[str, ...] = (
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--metrics-recording-only",
    "--mute-audio",
)
PREVIEW_CHROME_FLAGS: tuple[str, ...] = (
    "--disable-component-update",
    "--no-pings",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--disable-features=Translate,OptimizationHints,MediaRouter,AutofillServerCommunication",
)
SANDBOX_CHROME_FLAGS: tuple[str, ...] = (
    "--disable-sync",
    "--disable-default-apps",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
)


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


def is_loopback_or_inline_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw or raw.startswith(("data:", "about:", "blob:", "chrome:", "devtools:")):
        return True
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


class LoopbackCdpClient:
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
        self.event_hook: Callable[[dict[str, Any]], bool] | None = None
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

    def _record_event(self, data: dict[str, Any]) -> None:
        if self.event_hook is not None and self.event_hook(data):
            return
        self.events.append(data)

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        msg_id = self._next_id
        self._next_id += 1
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode("utf-8")
        self._send_frame(0x1, payload)
        return msg_id

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float = 15.0) -> dict[str, Any]:
        msg_id = self.send(method, params)
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
                self._record_event(data)
        raise TimeoutError(f"CDP timeout waiting for {method}")

    def eval_json(self, expression: str, *, timeout_s: float = 15.0) -> dict[str, Any]:
        """Evaluate a JS expression returning a JSON string and decode it into a dict."""
        res = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout_s=timeout_s,
        )
        raw_val = (res.get("result") or {}).get("value") or "{}"
        if isinstance(raw_val, dict):
            return dict(raw_val)
        if not isinstance(raw_val, str):
            return {}
        try:
            parsed = json.loads(raw_val)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

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
                self._record_event(data)
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
                self._record_event(data)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def wait_cdp_page_ws(cdp_port: int, proc: subprocess.Popen[bytes], *, timeout_s: float = 15.0) -> str:
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


class ReadOnlyFetchInterceptor:
    """CDP ``Fetch.requestPaused`` handler enforcing read-only site/CDN asset allowlists."""

    def __init__(
        self,
        *,
        site_host: str,
        resolved_target: str,
        max_blocked_sample: int = 20,
    ) -> None:
        self.site_host = site_host
        self.resolved_target = resolved_target
        self._max_blocked_sample = max_blocked_sample
        self._cdp: LoopbackCdpClient | None = None
        self.current_html_b64: str = ""
        self.main_doc_pending: bool = False
        self.allowed_count: int = 0
        self.blocked_counts: dict[str, int] = {}
        self.blocked_sample: list[dict[str, str]] = []

    def bind(self, cdp: LoopbackCdpClient) -> None:
        self._cdp = cdp
        cdp.event_hook = self.handle_event

    def set_document_html(self, html_body: str) -> None:
        self.current_html_b64 = base64.b64encode(html_body.encode("utf-8", errors="replace")).decode("ascii")

    def arm_main_document(self) -> None:
        self.main_doc_pending = True

    def handle_event(self, ev: dict[str, Any]) -> bool:
        if ev.get("method") != "Fetch.requestPaused":
            return False
        params = ev.get("params") or {}
        req_id = str(params.get("requestId") or "")
        req = params.get("request") or {}
        req_url = str(req.get("url") or "")
        req_method = str(req.get("method") or "GET").upper()
        res_type = str(params.get("resourceType") or "")
        assert self._cdp is not None

        if (
            self.main_doc_pending
            and res_type == "Document"
            and req_method == "GET"
            and req_url.rstrip("/") == self.resolved_target.rstrip("/")
        ):
            self.main_doc_pending = False
            self._cdp.send(
                "Fetch.fulfillRequest",
                {
                    "requestId": req_id,
                    "responseCode": 200,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=UTF-8"}],
                    "body": self.current_html_b64,
                },
            )
            self.allowed_count += 1
            return True

        ok, reason = classify_request(req_method, req_url, site_host=self.site_host)
        if ok:
            self._cdp.send("Fetch.continueRequest", {"requestId": req_id})
            self.allowed_count += 1
        else:
            self._cdp.send("Fetch.failRequest", {"requestId": req_id, "errorReason": "BlockedByClient"})
            self.blocked_counts[reason] = self.blocked_counts.get(reason, 0) + 1
            if len(self.blocked_sample) < self._max_blocked_sample:
                self.blocked_sample.append({"url": req_url[:200], "reason": reason})
        return True


class HeadlessBrowserSession:
    """Context manager managing headless Chrome process, temp profile, and loopback CDP client."""

    def __init__(
        self,
        binary: str,
        *,
        extra_flags: Sequence[str] = (),
        profile_prefix: str = "azom-wp-browser-",
        timeout_s: float = 30.0,
        cdp_timeout_s: float | None = None,
        enable_log: bool = True,
    ) -> None:
        self.binary = binary
        self.extra_flags = tuple(extra_flags)
        self.profile_prefix = profile_prefix
        self.timeout_s = float(timeout_s)
        self.cdp_timeout_s = float(cdp_timeout_s if cdp_timeout_s is not None else min(20.0, timeout_s))
        self.enable_log = enable_log
        self.user_data_dir: Path | None = None
        self.cdp_port: int | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._cdp: LoopbackCdpClient | None = None

    @property
    def cdp(self) -> LoopbackCdpClient:
        if self._cdp is None:
            raise RuntimeError("HeadlessBrowserSession is not active")
        return self._cdp

    def attach_fetch_interceptor(self, interceptor: ReadOnlyFetchInterceptor) -> None:
        interceptor.bind(self.cdp)
        self.cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

    def __enter__(self) -> HeadlessBrowserSession:
        self.cdp_port = pick_loopback_port()
        env = build_php_child_env()
        self.user_data_dir = Path(tempfile.mkdtemp(prefix=self.profile_prefix))
        command = [
            self.binary,
            *_BASE_CHROME_FLAGS,
            *self.extra_flags,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={self.user_data_dir}",
            "about:blank",
        ]
        popen_kwargs: dict[str, Any] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                command,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                **popen_kwargs,
            )
            ws_url = wait_cdp_page_ws(self.cdp_port, self.proc, timeout_s=min(15.0, self.timeout_s))
            self._cdp = LoopbackCdpClient(ws_url, timeout_s=self.cdp_timeout_s)
            self._cdp.call("Page.enable")
            self._cdp.call("Runtime.enable")
            self._cdp.call("Network.enable")
            if self.enable_log:
                self._cdp.call("Log.enable")
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        cdp = self._cdp
        self._cdp = None
        proc = self.proc
        self.proc = None
        profile = self.user_data_dir
        self.user_data_dir = None
        try:
            if cdp is not None:
                cdp.close()
        finally:
            try:
                terminate_process_tree(proc, timeout_s=5.0)
            finally:
                if profile is not None:
                    shutil.rmtree(profile, ignore_errors=True)

    def __exit__(self, *exc: object) -> None:
        self.close()


def collect_cdp_diagnostics(
    events: Sequence[dict[str, Any]],
    *,
    frame_id: str = "",
    ignore_blocked_by_client: bool = False,
    track_egress_source: str | None = None,
    max_msg_len: int = 240,
    max_url_len: int = 200,
) -> dict[str, Any]:
    """Parse captured CDP events into document status, console/JS/network errors, and egress attempts."""
    doc_status: int | None = None
    console_errors: list[str] = []
    js_exceptions: list[str] = []
    network_errors: list[dict[str, Any]] = []
    egress_attempts: list[dict[str, str]] = []
    request_urls: dict[str, str] = {}

    for ev in events:
        method = ev.get("method")
        params = ev.get("params") or {}
        if method == "Network.requestWillBeSent":
            req_id = str(params.get("requestId") or "")
            req_url = str((params.get("request") or {}).get("url") or "")
            if req_id:
                request_urls[req_id] = req_url
            if track_egress_source is not None and not is_loopback_or_inline_url(req_url):
                parsed_ext = urllib.parse.urlparse(req_url)
                egress_attempts.append(
                    {
                        "url": req_url[:300],
                        "host": (parsed_ext.hostname or "")[:120],
                        "source": track_egress_source,
                    }
                )
        elif method == "Network.responseReceived":
            resp = params.get("response") or {}
            sc = int(resp.get("status") or 0)
            rtype = str(params.get("type") or "")
            rurl = str(resp.get("url") or "")
            if rtype == "Document" and (
                (frame_id and params.get("frameId") == frame_id)
                or (not frame_id and doc_status is None)
            ):
                doc_status = sc
            if sc >= 400:
                network_errors.append({"url": rurl[:max_url_len], "status": sc, "type": rtype})
        elif method == "Network.loadingFailed":
            req_id = str(params.get("requestId") or "")
            req_url = request_urls.get(req_id, "")
            if ignore_blocked_by_client:
                if (
                    req_url
                    and not req_url.endswith("/favicon.ico")
                    and str(params.get("blockedReason") or "") != "inspector"
                ):
                    err_text = str(params.get("errorText") or "")
                    if err_text != "net::ERR_BLOCKED_BY_CLIENT":
                        network_errors.append({"url": req_url[:max_url_len], "error_text": err_text[:100]})
            elif not str(req_url).endswith("/favicon.ico"):
                network_errors.append(
                    {
                        "url": req_url[:max_url_len],
                        "error_text": str(params.get("errorText") or "loading_failed")[:120],
                        "type": str(params.get("type") or ""),
                    }
                )
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            parts = [str(a.get("value") or a.get("description") or "") for a in params.get("args") or []]
            msg = " ".join(parts).strip()
            if msg or not ignore_blocked_by_client:
                console_errors.append(msg[:max_msg_len])
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error":
                txt = str(entry.get("text") or "")
                if "favicon.ico" not in txt and (
                    not ignore_blocked_by_client or "ERR_BLOCKED_BY_CLIENT" not in txt
                ):
                    console_errors.append(txt[:max_msg_len])
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = str(details.get("text") or "Uncaught exception")
            desc = str((details.get("exception") or {}).get("description") or "")
            formatted = (f"{text}: {desc}" if desc else text)[:max_msg_len]
            if ignore_blocked_by_client:
                console_errors.append(text[:max_msg_len])
            else:
                js_exceptions.append(formatted)

    return {
        "doc_status": doc_status,
        "console_errors": console_errors,
        "js_exceptions": js_exceptions,
        "network_errors": network_errors,
        "egress_attempts": egress_attempts,
    }


__all__ = [
    "DEFAULT_VIEWPORTS",
    "PNG_MAGIC",
    "PREVIEW_CHROME_FLAGS",
    "SANDBOX_CHROME_FLAGS",
    "WP_FATAL_MARKERS",
    "HeadlessBrowserSession",
    "LoopbackCdpClient",
    "ReadOnlyFetchInterceptor",
    "assert_loopback_http_url",
    "collect_cdp_diagnostics",
    "find_browser_binary",
    "is_loopback_or_inline_url",
    "png_dimensions",
    "wait_cdp_page_ws",
]
