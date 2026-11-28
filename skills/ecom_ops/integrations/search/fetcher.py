"""Safe public HTTPS page fetcher and pure-Python HTML-to-markdown/text extractor."""

from __future__ import annotations

import http.client
import re
import ssl
import threading
import time
import urllib.parse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from html.parser import HTMLParser
from typing import Any

from ecom_ops.integrations import _http_guard
from ecom_ops.integrations.search.base import FetchResult
from ecom_ops.integrations.search.errors import (
    SearchConfigError,
    SearchPolicyError,
    SearchRouteError,
)
from ecom_ops.integrations.search.providers import parse_retry_after_seconds
from ecom_ops.research.budget import can_run_fetch
from ecom_ops.research.config import (
    is_mock_mode,
    kill_switch_active,
    load_research_config,
    web_fetch_enabled,
)
from ecom_ops.research.firewall import OutboundQueryFirewall, QueryTaintContext
from ecom_ops.research.sanitizer import sanitize_untrusted_text
from ecom_ops.telemetry import Telemetry, default_telemetry

ALLOWED_FETCH_MIME_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/markdown",
    }
)
_USER_AGENT = "AzomOps-Research/3.0"
_DROP_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "svg",
        "noscript",
        "template",
    }
)
_VOID_TAGS: frozenset[str] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_HEADING_TAGS: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "blockquote",
        "pre",
        "tr",
        "ul",
        "ol",
        "table",
    }
)
_HIDDEN_STYLE_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|font-size\s*:\s*0(?:px|em|rem|%)?\s*(?:;|$))",
    re.IGNORECASE,
)


def _is_hidden_attrs(attrs: list[tuple[str, str | None]]) -> bool:
    for raw_name, raw_val in attrs:
        name = str(raw_name or "").strip().lower()
        val = "" if raw_val is None else str(raw_val).strip()
        if name == "hidden":
            return True
        if name == "aria-hidden" and val.lower() == "true":
            return True
        if name == "style" and _HIDDEN_STYLE_RE.search(val):
            return True
    return False


class _AllowlistHTMLTextParser(HTMLParser):
    """Pure-Python allowlist HTML->structured-text parser dropping traps & scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tag_stack: list[tuple[str, bool]] = []
        self._skip_depth: int = 0
        self._in_title: bool = False
        self._title_parts: list[str] = []
        self._body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _VOID_TAGS:
            if self._skip_depth == 0:
                if t == "br":
                    self._body_parts.append("\n")
                elif t == "hr":
                    self._body_parts.append("\n---\n")
            return

        skip_this = t in _DROP_TAGS or _is_hidden_attrs(attrs)
        self._tag_stack.append((t, skip_this))
        if skip_this:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if t == "title":
            self._in_title = True
        elif t in _HEADING_TAGS:
            level = int(t[1])
            self._body_parts.append(f"\n\n{'#' * level} ")
        elif t == "li":
            self._body_parts.append("\n- ")
        elif t in _BLOCK_TAGS:
            self._body_parts.append("\n\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _VOID_TAGS:
            self.handle_starttag(t, attrs)
        # Non-void self-closing tags (e.g. <script />) should not leave a dangling skip frame.

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _VOID_TAGS:
            return
        for idx in range(len(self._tag_stack) - 1, -1, -1):
            stacked_tag, skipped = self._tag_stack[idx]
            if stacked_tag == t:
                popped = self._tag_stack[idx:]
                del self._tag_stack[idx:]
                for _, was_skipped in popped:
                    if was_skipped:
                        self._skip_depth = max(0, self._skip_depth - 1)
                break
        if t == "title":
            self._in_title = False
        if self._skip_depth == 0 and (t in _HEADING_TAGS or t in _BLOCK_TAGS):
            self._body_parts.append("\n")

    def handle_comment(self, data: str) -> None:
        # Always drop HTML comments (including potential prompt-injection payloads).
        del data

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._body_parts.append(data)

    def extracted(self) -> tuple[str, str]:
        raw_title = " ".join("".join(self._title_parts).split())
        raw_body = "".join(self._body_parts)
        lines: list[str] = []
        for raw_line in raw_body.splitlines():
            clean_line = re.sub(r"[ \t]+", " ", raw_line).strip()
            lines.append(clean_line)
        normalized = "\n".join(lines)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
        return raw_title, normalized


def _extract_html_parts(html: str, *, max_chars: int = 12_000) -> tuple[str, str, bool]:
    parser = _AllowlistHTMLTextParser()
    parser.feed(str(html or ""))
    parser.close()
    raw_title, raw_text = parser.extracted()
    truncated = max_chars > 0 and len(raw_text) > max_chars
    clean_title = sanitize_untrusted_text(raw_title, max_chars=300)
    clean_text = sanitize_untrusted_text(raw_text, max_chars=max_chars)
    return clean_title, clean_text, truncated


def extract_clean_text_from_html(html: str, *, max_chars: int = 12_000) -> tuple[str, str]:
    """Extract sanitized ``(title, clean_markdown_text)`` using an allowlist HTML parser."""
    title, text, _ = _extract_html_parts(html, max_chars=max_chars)
    return title, text


def _charset_from_content_type(raw_ct: str) -> str:
    match = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", raw_ct or "", re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _is_ssrf_or_policy_error(exc: ValueError) -> bool:
    msg = str(exc)
    return any(
        token in msg
        for token in (
            "Unsafe HTTPS URL",
            "Unsafe host",
            "Non-public IP",
            "No public IPs",
            "No DNS records",
            "Empty URL",
            "Invalid URL",
            "whitespace",
        )
    )


class SafePublicHttpClient:
    """Strictly public HTTPS page fetcher (no loopback exception, DNS-pinned, sanitized)."""

    def __init__(self) -> None:
        self._domain_lock = threading.Lock()
        self._last_fetch_by_domain: dict[str, float] = {}

    def _respect_domain_interval(self, domain: str, min_interval_s: float) -> None:
        if min_interval_s <= 0:
            return
        with self._domain_lock:
            now = time.monotonic()
            last = self._last_fetch_by_domain.get(domain)
            wait_s = (last + min_interval_s - now) if last is not None else 0.0
            if wait_s > 0:
                # Reserve the next slot while releasing lock during sleep or sleep briefly.
                self._last_fetch_by_domain[domain] = now + wait_s
            else:
                self._last_fetch_by_domain[domain] = now
        if wait_s > 0:
            time.sleep(min(wait_s, 10.0))

    def fetch(
        self,
        url: str,
        *,
        taint: QueryTaintContext | None = None,
        telemetry: Telemetry | None = None,
        max_extracted_chars: int | None = None,
        actor: str | None = None,
        site: str = "azom",
        case_id: str | None = None,
    ) -> FetchResult:
        """Fetch a public HTTPS URL and return sanitized extracted text."""
        cfg = load_research_config()
        tel = telemetry or default_telemetry
        fw = OutboundQueryFirewall()
        char_limit = int(
            max_extracted_chars
            if max_extracted_chars is not None
            else cfg.egress.max_extracted_chars
        )

        # 1. Firewall & URL validation (runs in both mock and live modes)
        try:
            clean_url = fw.check_url(url, taint=taint)
        except SearchPolicyError as exc:
            tel.record(
                action="web_fetch_policy_blocked",
                site=site,
                cost_usd=0.0,
                case_id=case_id,
                actor=actor,
                meta={"error_class": "SearchPolicyError", "error_code": str(exc).split(":", 1)[-1].strip()},
            )
            raise

        parsed = urllib.parse.urlsplit(clean_url)
        domain = (parsed.hostname or "").lower()

        # 2. Deterministic offline mock mode (zero sockets)
        if is_mock_mode():
            mock_html = (
                f"<html><head><title>Mock Extract: {domain}</title></head>"
                f"<body><h1>{domain}</h1>"
                f"<p>Deterministic offline mock extract for {clean_url}.</p></body></html>"
            )
            title, clean_text, truncated = _extract_html_parts(mock_html, max_chars=char_limit)
            tel.record(
                action="web_fetch_page",
                site=site,
                units=1,
                unit_type="api_calls",
                cost_usd=0.0,
                case_id=case_id,
                actor=actor,
                meta={
                    "estimated_search_usd": 0.0,
                    "cost_kind": "mock",
                    "domain": domain,
                    "status_code": 200,
                    "content_type": "text/html",
                    "truncated": truncated,
                },
            )
            return FetchResult(
                ok=True,
                url=clean_url,
                final_url=clean_url,
                title=title,
                text=clean_text,
                content_type="text/html",
                status_code=200,
                latency_ms=1,
                truncated=truncated,
                provider="mock",
            )

        # 3. Live gates & daily/monthly budget check
        if kill_switch_active("fetch"):
            raise SearchConfigError("Web fetch disabled by kill switch", code="web_fetch_kill_switch")
        if not web_fetch_enabled():
            raise SearchConfigError(
                "Live web fetch is disabled (requires AZOM_USE_MOCK=0 and AZOM_WEB_FETCH_ENABLED=1)",
                code="web_fetch_disabled",
            )
        budget_ok, budget_reason = can_run_fetch(telemetry=tel, config=cfg)
        if not budget_ok:
            tel.record(
                action="web_fetch_budget_skip",
                site=site,
                cost_usd=0.0,
                case_id=case_id,
                actor=actor,
                meta={"domain": domain, "error_code": budget_reason},
            )
            raise SearchConfigError(f"Fetch budget exhausted ({budget_reason})", code=budget_reason)

        def _validate_hop(hop_parsed: urllib.parse.ParseResult) -> None:
            hop_url = urllib.parse.urlunsplit(
                ("https", hop_parsed.netloc, hop_parsed.path or "/", hop_parsed.query, "")
            )
            fw.check_url(hop_url, taint=taint)

        started = time.monotonic()
        self._respect_domain_interval(domain, float(cfg.egress.min_domain_interval_s))

        def _do_request() -> tuple[int, dict[str, str], bytes, str]:
            return _http_guard.pinned_https_request(
                clean_url,
                method="GET",
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,text/plain,text/markdown;q=0.9",
                },
                connect_timeout=float(cfg.egress.connect_timeout_s),
                read_timeout=float(cfg.egress.fetch_timeout_s),
                max_redirects=int(cfg.egress.max_redirects),
                max_raw_bytes=int(cfg.egress.max_raw_bytes),
                max_decompressed_bytes=int(cfg.egress.max_decompressed_bytes),
                allowed_content_types=ALLOWED_FETCH_MIME_TYPES,
                pre_hop_validator=_validate_hop,
            )

        try:
            status, resp_headers, payload, final_url = _do_request()
            if status in (429, 503):
                ra = parse_retry_after_seconds(resp_headers)
                max_ra = float(cfg.egress.max_retry_after_s)
                if ra is not None and 0 < ra <= max_ra:
                    time.sleep(ra)
                    status, resp_headers, payload, final_url = _do_request()
        except SearchPolicyError:
            raise
        except ValueError as exc:
            if _is_ssrf_or_policy_error(exc):
                raise SearchPolicyError(f"OutboundQueryBlocked: ssrf_refused ({type(exc).__name__})") from exc
            latency_ms = int((time.monotonic() - started) * 1000)
            msg = str(exc)
            code = "disallowed_content_type" if "Content-Type" in msg else "response_invalid"
            return FetchResult(
                ok=False,
                url=clean_url,
                final_url=clean_url,
                title="",
                text="",
                content_type="",
                status_code=0,
                latency_ms=latency_ms,
                error_class="SearchUnavailableError",
                error_code=code,
            )
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return FetchResult(
                ok=False,
                url=clean_url,
                final_url=clean_url,
                title="",
                text="",
                content_type="",
                status_code=0,
                latency_ms=latency_ms,
                error_class="SearchUnavailableError",
                error_code=f"transport_{type(exc).__name__}",
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        raw_ct = str(resp_headers.get("content-type") or "")
        mime = raw_ct.split(";", 1)[0].strip().lower()

        if status == 429:
            return FetchResult(
                ok=False,
                url=clean_url,
                final_url=final_url,
                title="",
                text="",
                content_type=mime,
                status_code=status,
                latency_ms=latency_ms,
                error_class="SearchRateLimitError",
                error_code="rate_limit",
            )
        if status < 200 or status >= 300:
            return FetchResult(
                ok=False,
                url=clean_url,
                final_url=final_url,
                title="",
                text="",
                content_type=mime,
                status_code=status,
                latency_ms=latency_ms,
                error_class="SearchUnavailableError",
                error_code=f"http_{status}",
            )

        charset = _charset_from_content_type(raw_ct)
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")

        if mime in {"text/html", "application/xhtml+xml"}:
            title, clean_text, truncated = _extract_html_parts(decoded, max_chars=char_limit)
        else:
            truncated = char_limit > 0 and len(decoded) > char_limit
            title = ""
            clean_text = sanitize_untrusted_text(decoded, max_chars=char_limit)

        final_domain = (urllib.parse.urlsplit(final_url).hostname or domain).lower()
        tel.record(
            action="web_fetch_page",
            site=site,
            units=1,
            unit_type="api_calls",
            cost_usd=0.0,
            case_id=case_id,
            actor=actor,
            meta={
                "estimated_search_usd": 0.0,
                "cost_kind": "estimated_external",
                "domain": final_domain,
                "status_code": status,
                "content_type": mime,
                "truncated": truncated,
            },
        )
        return FetchResult(
            ok=True,
            url=clean_url,
            final_url=final_url,
            title=title,
            text=clean_text,
            content_type=mime,
            status_code=status,
            latency_ms=latency_ms,
            truncated=truncated,
            provider="http",
        )

    def _validate_batch_urls(
        self,
        urls: Sequence[str],
        *,
        taint: QueryTaintContext | None,
        tel: Telemetry,
        site: str,
        actor: str | None,
        case_id: str | None,
    ) -> tuple[list[str | None], dict[int, FetchResult], list[str]]:
        fw = OutboundQueryFirewall()
        cleaned_slots: list[str | None] = []
        blocked_results: dict[int, FetchResult] = {}
        unique_valid: list[str] = []
        seen_valid: set[str] = set()

        for idx, raw_u in enumerate(urls):
            raw_str = str(raw_u or "")
            try:
                cu = fw.check_url(raw_str, taint=taint)
            except SearchPolicyError as exc:
                code = str(exc).split(":", 1)[-1].strip()
                tel.record(
                    action="web_fetch_policy_blocked",
                    site=site,
                    cost_usd=0.0,
                    case_id=case_id,
                    actor=actor,
                    meta={"error_class": "SearchPolicyError", "error_code": code},
                )
                cleaned_slots.append(None)
                blocked_results[idx] = FetchResult(
                    ok=False,
                    url=raw_str,
                    final_url=raw_str,
                    title="",
                    text="",
                    content_type="",
                    status_code=0,
                    latency_ms=0,
                    error_class="SearchPolicyError",
                    error_code=code,
                    provider="http",
                )
                continue
            cleaned_slots.append(cu)
            if cu not in seen_valid:
                seen_valid.add(cu)
                unique_valid.append(cu)

        return cleaned_slots, blocked_results, unique_valid

    def fetch_many(
        self,
        urls: Sequence[str],
        *,
        taint: QueryTaintContext | None = None,
        telemetry: Telemetry | None = None,
        max_extracted_chars: int | None = None,
        max_workers: int = 4,
        timeout_s: float | None = None,
        actor: str | None = None,
        site: str = "azom",
        case_id: str | None = None,
    ) -> list[FetchResult]:
        """Concurrently fetch multiple public HTTPS URLs with per-domain politeness throttling."""
        raw_urls = [str(u or "") for u in urls]
        if not raw_urls:
            return []
        cfg = load_research_config()
        tel = telemetry or default_telemetry
        cleaned_slots, blocked_results, unique_valid = self._validate_batch_urls(
            raw_urls,
            taint=taint,
            tel=tel,
            site=site,
            actor=actor,
            case_id=case_id,
        )
        if not unique_valid:
            return [blocked_results[i] for i in range(len(raw_urls))]

        by_url: dict[str, FetchResult] = {}
        if is_mock_mode():
            for cu in unique_valid:
                by_url[cu] = self.fetch(
                    cu,
                    taint=taint,
                    telemetry=tel,
                    max_extracted_chars=max_extracted_chars,
                    actor=actor,
                    site=site,
                    case_id=case_id,
                )
        else:
            if kill_switch_active("fetch"):
                raise SearchConfigError("Web fetch disabled by kill switch", code="web_fetch_kill_switch")
            if not web_fetch_enabled():
                raise SearchConfigError(
                    "Live web fetch is disabled (requires AZOM_USE_MOCK=0 and AZOM_WEB_FETCH_ENABLED=1)",
                    code="web_fetch_disabled",
                )
            budget_ok, budget_reason = can_run_fetch(telemetry=tel, config=cfg)
            if not budget_ok:
                tel.record(
                    action="web_fetch_budget_skip",
                    site=site,
                    cost_usd=0.0,
                    case_id=case_id,
                    actor=actor,
                    meta={"error_code": budget_reason},
                )
                raise SearchConfigError(f"Fetch budget exhausted ({budget_reason})", code=budget_reason)

            workers = max(1, min(int(max_workers or 4), len(unique_valid), 8))
            if workers == 1 and timeout_s is None:
                for cu in unique_valid:
                    by_url[cu] = self.fetch(
                        cu,
                        taint=taint,
                        telemetry=tel,
                        max_extracted_chars=max_extracted_chars,
                        actor=actor,
                        site=site,
                        case_id=case_id,
                    )
            else:
                pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="azom-fetch")
                timed_out = False
                try:
                    fut_to_url = {
                        pool.submit(
                            self.fetch,
                            cu,
                            taint=taint,
                            telemetry=tel,
                            max_extracted_chars=max_extracted_chars,
                            actor=actor,
                            site=site,
                            case_id=case_id,
                        ): cu
                        for cu in unique_valid
                    }
                    if timeout_s is not None:
                        done, not_done = wait(fut_to_url.keys(), timeout=max(0.05, float(timeout_s)))
                    else:
                        done, not_done = wait(fut_to_url.keys())
                    for fut in not_done:
                        timed_out = True
                        cu = fut_to_url[fut]
                        fut.cancel()
                        by_url[cu] = FetchResult(
                            ok=False,
                            url=cu,
                            final_url=cu,
                            title="",
                            text="",
                            content_type="",
                            status_code=0,
                            latency_ms=int(max(0.0, float(timeout_s or 0.0)) * 1000),
                            error_class="SearchUnavailableError",
                            error_code="fetch_timeout",
                            provider="http",
                        )
                    for fut in done:
                        cu = fut_to_url[fut]
                        try:
                            by_url[cu] = fut.result()
                        except SearchPolicyError as exc:
                            by_url[cu] = FetchResult(
                                ok=False,
                                url=cu,
                                final_url=cu,
                                title="",
                                text="",
                                content_type="",
                                status_code=0,
                                latency_ms=0,
                                error_class="SearchPolicyError",
                                error_code=str(exc).split(":", 1)[-1].strip(),
                                provider="http",
                            )
                        except SearchRouteError as exc:
                            by_url[cu] = FetchResult(
                                ok=False,
                                url=cu,
                                final_url=cu,
                                title="",
                                text="",
                                content_type="",
                                status_code=int(getattr(exc, "http_status", None) or 0),
                                latency_ms=0,
                                error_class=type(exc).__name__,
                                error_code=getattr(exc, "code", None) or "fetch_error",
                                provider="http",
                            )
                finally:
                    pool.shutdown(wait=not timed_out, cancel_futures=timed_out)

        out: list[FetchResult] = []
        for idx, cu in enumerate(cleaned_slots):
            out.append(blocked_results[idx] if cu is None else by_url[cu])
        return out

    def extract_many(
        self,
        urls: Sequence[str],
        *,
        strategy: str = "parallel_extract_first",
        objective: str | None = None,
        taint: QueryTaintContext | None = None,
        telemetry: Telemetry | None = None,
        max_extracted_chars: int | None = None,
        max_workers: int = 4,
        timeout_s: float | None = None,
        actor: str | None = None,
        site: str = "azom",
        case_id: str | None = None,
        parallel_route: Any | None = None,
        exa_route: Any | None = None,
    ) -> list[FetchResult]:
        """Extract URLs concurrently using ``Parallel Extract -> Exa Contents -> SafePublicHttpClient``."""
        norm_strategy = str(strategy or "parallel_extract_first").strip().lower()
        if norm_strategy == "snippet_only":
            return []
        raw_urls = [str(u or "") for u in urls]
        if not raw_urls:
            return []

        cfg = load_research_config()
        tel = telemetry or default_telemetry
        char_limit = int(
            max_extracted_chars
            if max_extracted_chars is not None
            else cfg.egress.max_extracted_chars
        )

        if is_mock_mode() or norm_strategy == "direct":
            return self.fetch_many(
                raw_urls,
                taint=taint,
                telemetry=tel,
                max_extracted_chars=char_limit,
                max_workers=max_workers,
                timeout_s=timeout_s,
                actor=actor,
                site=site,
                case_id=case_id,
            )

        if kill_switch_active("fetch"):
            raise SearchConfigError("Web fetch disabled by kill switch", code="web_fetch_kill_switch")
        if not web_fetch_enabled():
            raise SearchConfigError(
                "Live web fetch is disabled (requires AZOM_USE_MOCK=0 and AZOM_WEB_FETCH_ENABLED=1)",
                code="web_fetch_disabled",
            )
        budget_ok, budget_reason = can_run_fetch(telemetry=tel, config=cfg)
        if not budget_ok:
            tel.record(
                action="web_fetch_budget_skip",
                site=site,
                cost_usd=0.0,
                case_id=case_id,
                actor=actor,
                meta={"error_code": budget_reason},
            )
            raise SearchConfigError(f"Fetch budget exhausted ({budget_reason})", code=budget_reason)

        cleaned_slots, blocked_results, unique_valid = self._validate_batch_urls(
            raw_urls,
            taint=taint,
            tel=tel,
            site=site,
            actor=actor,
            case_id=case_id,
        )
        if not unique_valid:
            return [blocked_results[i] for i in range(len(raw_urls))]

        def _run_provider_chain(target_urls: list[str], *, use_parallel: bool, use_exa: bool) -> dict[str, FetchResult]:
            resolved: dict[str, FetchResult] = {}
            remaining = list(target_urls)

            if use_parallel and remaining:
                p_route = parallel_route
                if p_route is None:
                    from ecom_ops.integrations.search.providers.parallel import ParallelSearchRoute

                    p_route = ParallelSearchRoute()
                if p_route.is_configured():
                    try:
                        p_results = p_route.extract_many(
                            remaining,
                            taint=taint,
                            objective=objective,
                            max_chars=char_limit,
                            telemetry=tel,
                            actor=actor,
                            site=site,
                            case_id=case_id,
                        )
                        next_rem: list[str] = []
                        for u, fres in zip(remaining, p_results, strict=False):
                            if fres.ok and fres.text:
                                resolved[u] = fres
                            else:
                                next_rem.append(u)
                        remaining = next_rem
                    except SearchRouteError:
                        pass

            if use_exa and remaining:
                e_route = exa_route
                if e_route is None:
                    from ecom_ops.integrations.search.providers.exa import ExaSearchRoute

                    e_route = ExaSearchRoute()
                if e_route.is_configured():
                    try:
                        e_results = e_route.extract_many(
                            remaining,
                            taint=taint,
                            objective=objective,
                            max_chars=char_limit,
                            telemetry=tel,
                            actor=actor,
                            site=site,
                            case_id=case_id,
                        )
                        next_rem = []
                        for u, fres in zip(remaining, e_results, strict=False):
                            if fres.ok and fres.text:
                                resolved[u] = fres
                            else:
                                next_rem.append(u)
                        remaining = next_rem
                    except SearchRouteError:
                        pass

            if remaining:
                http_results = self.fetch_many(
                    remaining,
                    taint=taint,
                    telemetry=tel,
                    max_extracted_chars=char_limit,
                    max_workers=max_workers,
                    timeout_s=timeout_s,
                    actor=actor,
                    site=site,
                    case_id=case_id,
                )
                for u, fres in zip(remaining, http_results, strict=False):
                    resolved[u] = fres

            return resolved

        by_url: dict[str, FetchResult] = {}
        if norm_strategy == "hybrid" and len(unique_valid) > 2:
            head_urls = unique_valid[:2]
            tail_urls = unique_valid[2:]
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="azom-hybrid-extract") as pool:
                head_fut = pool.submit(
                    _run_provider_chain,
                    head_urls,
                    use_parallel=True,
                    use_exa=True,
                )
                tail_fut = pool.submit(
                    self.fetch_many,
                    tail_urls,
                    taint=taint,
                    telemetry=tel,
                    max_extracted_chars=char_limit,
                    max_workers=max_workers,
                    timeout_s=timeout_s,
                    actor=actor,
                    site=site,
                    case_id=case_id,
                )
                by_url.update(head_fut.result())
                for u, fres in zip(tail_urls, tail_fut.result(), strict=False):
                    by_url[u] = fres
        elif norm_strategy == "exa_first":
            by_url.update(_run_provider_chain(unique_valid, use_parallel=False, use_exa=True))
        else:
            by_url.update(_run_provider_chain(unique_valid, use_parallel=True, use_exa=True))

        out: list[FetchResult] = []
        for idx, cu in enumerate(cleaned_slots):
            out.append(blocked_results[idx] if cu is None else by_url[cu])
        return out


__all__ = [
    "ALLOWED_FETCH_MIME_TYPES",
    "SafePublicHttpClient",
    "extract_clean_text_from_html",
]
