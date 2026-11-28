"""Wave 1 tests: search providers, SearchRouter, breaker, fetcher, cache, store, evaluator, probes.

Strictly offline (zero live network calls — mocks ``_http_guard.pinned_https_request`` and loopback transport).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.integrations import _http_guard
from ecom_ops.integrations.search import circuit_breaker
from ecom_ops.integrations.search.base import SearchHit, SearchRequest
from ecom_ops.integrations.search.errors import (
    SearchAuthError,
    SearchConfigError,
    SearchPolicyError,
    SearchRateLimitError,
    SearchUnavailableError,
)
from ecom_ops.integrations.search.fetcher import (
    SafePublicHttpClient,
    extract_clean_text_from_html,
)
from ecom_ops.integrations.search.providers.brave import BraveSearchRoute
from ecom_ops.integrations.search.providers.exa import ExaSearchRoute
from ecom_ops.integrations.search.providers.mock import MockSearchRoute
from ecom_ops.integrations.search.providers.parallel import ParallelSearchRoute
from ecom_ops.integrations.search.providers.searxng import SearxngSearchRoute
from ecom_ops.integrations.search.providers.tavily import TavilySearchRoute
from ecom_ops.integrations.search.router import SearchRouter, reset_search_router
from ecom_ops.rbac import AccessDenied
from ecom_ops.research.budget import research_budget_status
from ecom_ops.research.config import clear_research_config_cache
from ecom_ops.research.evaluator import (
    build_citations,
    canonicalize_url,
    classify_source_type,
    evaluate_search_hits,
    verify_citations,
)
from ecom_ops.research.firewall import QueryTaintContext
from ecom_ops.research.models import (
    Citation,
    EvidenceStatus,
    ResearchBrief,
    ResearchSpec,
    SourceType,
)
from ecom_ops.research.profiles import get_profile
from ecom_ops.research.store import (
    ResearchStore,
    SearchCache,
    authorize_research_access,
    authorize_target_ref,
)
from ecom_ops.runtime_env import PROTECTED_OVERLAY_KEYS
from ecom_ops.telemetry import Telemetry

ROOT = Path(__file__).resolve().parents[1]
DASH_DIR = ROOT / "infrastructure" / "dashboard"


def _enable_live_search(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WEB_SEARCH_ENABLED", "1")
    monkeypatch.setenv("AZOM_WEB_FETCH_ENABLED", "1")
    clear_research_config_cache()
    reset_search_router()


def _install_pinned_stub(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_pinned(url: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes, str]:
        call = {"url": url, **kwargs}
        calls.append(call)
        return handler(url, **kwargs)

    monkeypatch.setattr(_http_guard, "pinned_https_request", _fake_pinned)
    return calls


# ---------------------------------------------------------------------------
# 1. Provider request/header/payload contracts & error body sanitization
# ---------------------------------------------------------------------------


def test_parallel_provider_nests_advanced_settings_and_header_only_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-secret-key-123")

    def _handler(url: str, **kwargs: Any):
        del kwargs
        payload = {
            "results": [
                {
                    "url": "https://developer.woocommerce.com/docs/thankyou",
                    "title": "WooCommerce Thankyou Hook",
                    "publish_date": "2025-11-01",
                    "excerpts": ["Use woocommerce_thankyou action cleanly.", "HPOS safe read."],
                }
            ]
        }
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"), url

    calls = _install_pinned_stub(monkeypatch, _handler)
    route = ParallelSearchRoute()
    long_q = (
        "woocommerce thankyou page checkout conversion optimization post purchase "
        "order confirmation hpos block theme hooks best practices nordic ecommerce "
        "car multimedia apple carplay android auto fitment guide warranty trust"
    )
    assert 200 < len(long_q) <= 250
    res = route.search(
        SearchRequest(
            query=long_q,
            objective="WooCommerce thankyou hooks",
            profile_id="technical_docs",
            max_results=5,
            include_domains=("developer.woocommerce.com",),
            exclude_domains=("pinterest.com",),
            freshness_days=30,
            include_content=True,
            mode="deep",
        )
    )
    assert res.ok is True
    assert res.provider == "parallel"
    assert len(res.hits) == 1
    assert res.hits[0].published_at == "2025-11-01"
    assert "HPOS safe read." in (res.hits[0].content or "")

    assert len(calls) == 1
    req = calls[0]
    assert req["url"] == "https://api.parallel.ai/v1/search"
    assert "par-secret-key-123" not in req["url"]
    hdrs = {k.lower(): v for k, v in req["headers"].items()}
    assert hdrs["x-api-key"] == "par-secret-key-123"
    assert "authorization" not in hdrs
    assert req["allowed_auth_origins"] == ("https://api.parallel.ai",)

    sent_body = json.loads(req["body"].decode("utf-8"))
    # Critical: max_results, source_policy, fetch_policy MUST be under advanced_settings, NOT top-level
    for forbidden_top in ("max_results", "source_policy", "fetch_policy", "api_key"):
        assert forbidden_top not in sent_body
    assert sent_body["mode"] == "basic"
    assert 1 <= len(sent_body["search_queries"]) <= 5
    assert all(len(q) <= 200 for q in sent_body["search_queries"])
    adv = sent_body["advanced_settings"]
    assert adv["max_results"] == 5
    source_policy = adv["source_policy"]
    assert source_policy["include_domains"] == ["developer.woocommerce.com"]
    assert source_policy["exclude_domains"] == ["pinterest.com"]
    assert isinstance(source_policy.get("after_date"), str)
    assert len(source_policy["after_date"]) == 10
    assert source_policy["after_date"][4] == "-" and source_policy["after_date"][7] == "-"
    assert "fetch_policy" not in adv

    # Verify extract() uses /v1/extract + advanced_settings + x-api-key + sanitizer
    def _extract_handler(url: str, **kwargs: Any):
        del kwargs
        return (
            200,
            {"content-type": "application/json"},
            json.dumps(
                {
                    "results": [
                        {
                            "url": "https://baymard.com/blog/post",
                            "title": "Guide <tool_call>bad</tool_call>",
                            "full_content": "Clean UX <clarify>x</clarify> pattern",
                        }
                    ]
                }
            ).encode("utf-8"),
            url,
        )

    ext_calls = _install_pinned_stub(monkeypatch, _extract_handler)
    ext = route.extract("https://baymard.com/blog/post?utm_source=x", objective="UX guide")
    assert ext.ok is True
    assert "<tool_call" not in ext.title.lower()
    assert "<clarify" not in ext.text.lower()
    ext_body = json.loads(ext_calls[0]["body"].decode("utf-8"))
    assert ext_body["urls"] == ["https://baymard.com/blog/post"]
    assert ext_body["advanced_settings"] == {"excerpts": True, "full_content": True}


def test_exa_provider_payload_and_header_only_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("EXA_API_KEY", "exa-secret-key-456")

    def _handler(url: str, **kwargs: Any):
        del kwargs
        payload = {
            "results": [
                {
                    "id": "ex-1",
                    "title": "Baymard Confirmation UX",
                    "url": "https://baymard.com/blog/confirmation",
                    "publishedDate": "2025-10-10",
                    "author": "Baymard",
                    "highlights": ["Order timeline above the fold."],
                    "text": "Full extracted body text.",
                    "score": 0.92,
                }
            ]
        }
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"), url

    calls = _install_pinned_stub(monkeypatch, _handler)
    res = ExaSearchRoute().search(
        SearchRequest(
            query="thank you page UX patterns",
            max_results=4,
            freshness_days=14,
            include_domains=("baymard.com",),
            exclude_domains=("spam.example",),
            include_content=True,
            mode="deep",
        )
    )
    assert res.ok is True and len(res.hits) == 1
    assert res.hits[0].author == "Baymard"
    assert res.hits[0].content == "Full extracted body text."

    req = calls[0]
    assert req["url"] == "https://api.exa.ai/search"
    assert "exa-secret-key-456" not in req["url"]
    hdrs = {k.lower(): v for k, v in req["headers"].items()}
    assert hdrs["x-api-key"] == "exa-secret-key-456"
    body = json.loads(req["body"].decode("utf-8"))
    assert body["type"] == "deep"
    assert body["numResults"] == 4
    assert body["contents"]["highlights"] is True
    assert body["contents"]["text"]["maxCharacters"] == 4000
    assert body["includeDomains"] == ["baymard.com"]
    assert body["excludeDomains"] == ["spam.example"]
    assert len(body["startPublishedDate"]) == 10


def test_tavily_provider_uses_bearer_header_never_body_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-789")

    def _handler(url: str, **kwargs: Any):
        del kwargs
        payload = {
            "results": [
                {
                    "url": "https://nngroup.com/articles/confirmation/",
                    "title": "NN/g Confirmation",
                    "content": "Reduce WISMO tickets.",
                    "published_date": "2025-05-01",
                    "score": 0.9,
                }
            ]
        }
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"), url

    calls = _install_pinned_stub(monkeypatch, _handler)
    res = TavilySearchRoute().search(
        SearchRequest(query="confirmation UX", max_results=3, mode="deep", freshness_days=30)
    )
    assert res.ok is True and res.estimated_cost_usd > 0
    req = calls[0]
    assert req["url"] == "https://api.tavily.com/search"
    assert "tvly-secret-789" not in req["url"]
    hdrs = {k.lower(): v for k, v in req["headers"].items()}
    assert hdrs["authorization"] == "Bearer tvly-secret-789"
    body = json.loads(req["body"].decode("utf-8"))
    assert "api_key" not in body
    assert body["search_depth"] == "advanced"


def test_brave_provider_uses_subscription_token_header_and_extra_snippets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAsecretBrave12345")

    def _handler(url: str, **kwargs: Any):
        del kwargs
        payload = {
            "web": {
                "results": [
                    {
                        "url": "https://www.postnord.se/driftinformation",
                        "title": "PostNord Drift",
                        "description": "Normal drift.",
                        "extra_snippets": ["Paketbox ok."],
                        "page_age": "2026-11-20",
                    }
                ]
            }
        }
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"), url

    calls = _install_pinned_stub(monkeypatch, _handler)
    res = BraveSearchRoute().search(
        SearchRequest(query="PostNord drift", country="SE", language="sv", freshness_days=7, max_results=3)
    )
    assert res.ok is True and len(res.hits) == 1
    assert "Paketbox ok." in res.hits[0].snippet

    req = calls[0]
    parsed = urllib.parse.urlsplit(req["url"])
    qs = urllib.parse.parse_qs(parsed.query)
    assert "BSAsecretBrave12345" not in req["url"]
    assert qs["extra_snippets"] == ["true"]
    assert qs["country"] == ["SE"]
    assert qs["search_lang"] == ["sv"]
    assert qs["freshness"] == ["pw"]
    hdrs = {k.lower(): v for k, v in req["headers"].items()}
    assert hdrs["x-subscription-token"] == "BSAsecretBrave12345"


def test_searxng_loopback_isolation_blocks_dashboard_port_and_redirects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    route = SearxngSearchRoute()
    assert route.is_configured() is False

    # Rejects DASHBOARD_PORT (default 8080 and explicit override), AZOM_DASHBOARD_PUBLIC_URL port, and non-loopback HTTP
    monkeypatch.setenv("DASHBOARD_PORT", "8080")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dash.azom.se:8443")
    for bad_url in (
        "http://127.0.0.1:8080",
        "http://[::1]:8080",
        "http://127.0.0.1:8443",
        "http://127.0.0.1",
        "http://localhost:8888",
        "http://10.0.0.5:8888",
        "https://127.0.0.1:8888",
        "http://127.0.0.1:8888/custom",
        "http://user:pass@127.0.0.1:8888",
    ):
        monkeypatch.setenv("SEARXNG_BASE_URL", bad_url)
        assert route.is_configured() is False, f"Should reject {bad_url}"

    # Allows explicit non-dashboard loopback 127.0.0.1 & [::1] with ProxyHandler({}) and max_redirects=0
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://[::1]:8888")
    monkeypatch.setenv("SEARXNG_API_KEY", "sx-bearer-token")
    assert route.is_configured() is True

    seen_handlers: list[tuple[object, ...]] = []
    seen_req: list[urllib.request.Request] = []

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getheader(self, name: str, default: str | None = None) -> str | None:
            if name.lower() == "content-type":
                return "application/json"
            return default

        def read(self, _n: int = -1) -> bytes:
            return json.dumps(
                {"results": [{"url": "https://docs.python.org/3/", "title": "Python", "content": "Docs"}]}
            ).encode("utf-8")

    class _FakeOpener:
        def __init__(self, handlers: tuple[object, ...]) -> None:
            self.handlers = handlers

        def open(self, req: urllib.request.Request, timeout: float = 10.0):
            del timeout
            seen_req.append(req)
            # Verify redirect handler refuses redirects (max_redirects=0)
            redirect_handlers = [
                h for h in self.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
            ]
            assert len(redirect_handlers) == 1
            with pytest.raises(urllib.error.HTTPError):
                redirect_handlers[0].redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8080/")
            return _FakeResp()

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: seen_handlers.append(handlers) or _FakeOpener(handlers),
    )

    res = route.search(SearchRequest(query="python docs", language="en", freshness_days=7))
    assert res.ok is True and len(res.hits) == 1
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in seen_handlers[0])
    assert seen_req[0].full_url.startswith("http://[::1]:8888/search?")
    assert "format=json" in seen_req[0].full_url
    assert "sx-bearer-token" not in seen_req[0].full_url
    assert seen_req[0].get_header("Authorization") == "Bearer sx-bearer-token"


def test_providers_never_log_or_surface_raw_error_bodies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-secret")
    monkeypatch.setenv("EXA_API_KEY", "exa-secret")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAsecretKey12345")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-1234")
    secret_body = b'{"error":"RAW_PROVIDER_BODY_DO_NOT_LEAK_TOKEN_999"}'

    def _error_handler_factory(status: int, hdrs: dict[str, str] | None = None):
        def _handler(url: str, **kwargs: Any):
            del url, kwargs
            return status, (hdrs or {"content-type": "application/json"}), secret_body, "https://api.example"

        return _handler

    _install_pinned_stub(monkeypatch, _error_handler_factory(401))
    with pytest.raises(SearchAuthError) as exc_auth:
        ParallelSearchRoute().search(SearchRequest(query="test"))
    assert "RAW_PROVIDER_BODY_DO_NOT_LEAK" not in str(exc_auth.value)

    _install_pinned_stub(monkeypatch, _error_handler_factory(429, {"retry-after": "25"}))
    with pytest.raises(SearchRateLimitError) as exc_rl:
        ExaSearchRoute().search(SearchRequest(query="test"))
    assert exc_rl.value.retry_after_s == 25.0
    assert "RAW_PROVIDER_BODY_DO_NOT_LEAK" not in str(exc_rl.value)

    _install_pinned_stub(monkeypatch, _error_handler_factory(503))
    with pytest.raises(SearchUnavailableError) as exc_503:
        TavilySearchRoute().search(SearchRequest(query="test"))
    assert "RAW_PROVIDER_BODY_DO_NOT_LEAK" not in str(exc_503.value)

    # Telemetry and breaker JSON must never hold raw error bodies
    tel = Telemetry(path=tmp_path / "data" / "telemetry.jsonl")
    _install_pinned_stub(monkeypatch, _error_handler_factory(403))
    with pytest.raises(SearchUnavailableError):
        SearchRouter().search(SearchRequest(query="test"), telemetry=tel)
    assert "RAW_PROVIDER_BODY_DO_NOT_LEAK" not in tel.path.read_text(encoding="utf-8")
    if circuit_breaker.health_path().is_file():
        assert "RAW_PROVIDER_BODY_DO_NOT_LEAK" not in circuit_breaker.health_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. SearchRouter: mock isolation, precedence, policy no-failover, OpenRouter $100 cap
# ---------------------------------------------------------------------------


def test_router_mock_mode_policy_no_failover_and_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DASHBOARD_PUBLIC_URL", "https://dash.azom.se")
    clear_research_config_cache()
    reset_search_router()

    tel = Telemetry(path=tmp_path / "data" / "telemetry.jsonl")
    router = SearchRouter()

    # Mock mode uses mock fixtures with zero sockets
    res = router.search(
        SearchRequest(query="thank you page UX", profile_id="design_inspiration"),
        telemetry=tel,
    )
    assert res.ok is True
    assert res.provenance == "mock_fixture"
    assert res.route_id == "mock"
    assert len(res.hits) >= 2
    assert tel.sum_cost_usd() == 0.0

    # Switch to live mode
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-k")
    monkeypatch.setenv("EXA_API_KEY", "exa-k")
    monkeypatch.setenv("AZOM_SEARCH_ROUTE_ORDER", "parallel,exa")

    # MockSearchRoute strictly refuses live mode
    with pytest.raises(SearchConfigError, match="mock"):
        MockSearchRoute().search(SearchRequest(query="hello"))

    # SearchPolicyError NEVER fails over to Exa and NEVER trips breaker
    exa_called = {"n": 0}
    router = SearchRouter()

    class _PolicyRaiseRoute:
        route_id = "parallel"

        def is_configured(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"route_id": "parallel", "configured": True}

        def search(self, _req: SearchRequest) -> Any:
            raise SearchPolicyError("OutboundQueryBlocked: tainted")

    class _BackupExa:
        route_id = "exa"

        def is_configured(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"route_id": "exa", "configured": True}

        def search(self, _req: SearchRequest) -> Any:
            exa_called["n"] += 1
            raise AssertionError("SearchPolicyError must NEVER fail over!")

    router._routes["parallel"] = _PolicyRaiseRoute()
    router._routes["exa"] = _BackupExa()
    with pytest.raises(SearchPolicyError):
        router.search(SearchRequest(query="safe words"), telemetry=tel)
    assert exa_called["n"] == 0
    assert circuit_breaker.status_snapshot() == {}

    # Precedence: OutboundQueryFirewall taint blocks even with mock or allowlist
    with pytest.raises(SearchPolicyError, match="OutboundQueryBlocked"):
        router.search(
            SearchRequest(
                query="Svensson order status",
                taint=QueryTaintContext(customer_names=("Anna Svensson",)),
            ),
            telemetry=tel,
        )

    # Precedence: domain_denylist (incl. AZOM_DASHBOARD_PUBLIC_URL) overrides AZOM_SEARCH_ALLOWED_DOMAINS
    monkeypatch.setenv("AZOM_SEARCH_ALLOWED_DOMAINS", "dash.azom.se,baymard.com")
    clear_research_config_cache()
    with pytest.raises(SearchPolicyError, match="domain_denylisted"):
        router.search(
            SearchRequest(query="ux", include_domains=("dash.azom.se",)),
            telemetry=tel,
        )

    # Kill switch overrides everything
    monkeypatch.setenv("AZOM_WEB_SEARCH_KILL", "1")
    with pytest.raises(SearchConfigError, match="kill"):
        router.search(SearchRequest(query="ux"), telemetry=tel)


def test_router_failover_openrouter_cap_isolation_and_isolated_test_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-secret-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-secret-key")
    monkeypatch.setenv("AZOM_SEARCH_ROUTE_ORDER", "parallel,exa")

    tel = Telemetry(path=tmp_path / "data" / "telemetry.jsonl")
    router = SearchRouter()

    par_calls = {"n": 0}
    exa_calls = {"n": 0}

    class _ParFail:
        route_id = "parallel"

        def is_configured(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"route_id": "parallel", "configured": True}

        def search(self, _req: SearchRequest):
            par_calls["n"] += 1
            raise SearchUnavailableError("timeout", code="http_503", http_status=503)

    class _ExaOk:
        route_id = "exa"

        def is_configured(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"route_id": "exa", "configured": True}

        def search(self, _req: SearchRequest):
            exa_calls["n"] += 1
            from ecom_ops.integrations.search.base import SearchResult

            return SearchResult(
                ok=True,
                route_id="exa",
                provider="exa",
                hits=(SearchHit(url="https://baymard.com/ux", title="UX", snippet="Good", provider="exa"),),
                provenance="live_api",
                latency_ms=12,
                estimated_cost_usd=0.005,
            )

    router._routes["parallel"] = _ParFail()
    router._routes["exa"] = _ExaOk()

    # Two failures open parallel breaker; third call skips parallel in 0ms
    for _ in range(3):
        res = router.search(SearchRequest(query="unique query secret"), telemetry=tel)
        assert res.route_id == "exa"
    assert par_calls["n"] == 2
    assert exa_calls["n"] == 3

    # OpenRouter $100 cap strictly isolated (cost_usd == 0.0, meta.estimated_search_usd populated, no raw query logged)
    assert tel.sum_cost_usd() == 0.0
    tel_text = tel.path.read_text(encoding="utf-8")
    assert "unique query secret" not in tel_text
    assert "par-secret-key" not in tel_text
    events = [json.loads(line) for line in tel_text.splitlines() if line.strip()]
    selected = [ev for ev in events if ev["action"] == "web_search_selected"]
    assert len(selected) == 3
    assert all(ev["cost_usd"] == 0.0 for ev in selected)
    assert all(ev["meta"]["estimated_search_usd"] == pytest.approx(0.005) for ev in selected)
    assert research_budget_status(telemetry=tel)["monthly_external_usd"] == pytest.approx(0.015)

    # Isolated test_route: zero fallback (even when Exa is ok), bypasses open parallel breaker
    probe_fail = router.test_route("parallel")
    assert probe_fail["ok"] is False
    assert probe_fail["route_id"] == "parallel"
    assert probe_fail["error_class"] == "SearchUnavailableError"
    assert par_calls["n"] == 3  # bypassed open breaker
    assert exa_calls["n"] == 3  # did NOT fail over to Exa


def test_search_circuit_breaker_open_half_open_and_epoch_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    circuit_breaker.reset_breaker_cache_for_tests()

    # Auth error -> open_auth; cleared immediately on epoch bump
    circuit_breaker.record_failure(
        "parallel",
        SearchAuthError("unauthorized", code="auth", http_status=401),
    )
    allowed, state, _ = circuit_breaker.allow_attempt("parallel")
    assert allowed is False and state == "open_auth"

    circuit_breaker.bump_search_epoch()
    allowed2, state2, _ = circuit_breaker.allow_attempt("parallel")
    assert allowed2 is True and state2 == "auth_epoch_cleared"

    # Rate limit -> clamped retry_after, then single-flight half_open
    circuit_breaker.record_failure(
        "exa",
        SearchRateLimitError("rate limited", retry_after_s=999),
    )
    snap = circuit_breaker.status_snapshot()
    assert snap["route:exa"]["state"] == "open"
    assert 150 <= snap["route:exa"]["remaining_s"] <= 185

    raw = json.loads(circuit_breaker.health_path().read_text(encoding="utf-8"))
    raw["entries"]["route:exa"]["cooldown_until"] = time.time() - 5
    circuit_breaker.health_path().write_text(json.dumps(raw), encoding="utf-8")
    circuit_breaker.reset_breaker_cache_for_tests()

    ok1, st1, _ = circuit_breaker.allow_attempt("exa")
    ok2, st2, _ = circuit_breaker.allow_attempt("exa")
    assert ok1 is True and st1 == "half_open"
    assert ok2 is False and st2 == "half_open_busy"

    circuit_breaker.record_success("exa")
    assert circuit_breaker.allow_attempt("exa")[0] is True


# ---------------------------------------------------------------------------
# 3. SafePublicHttpClient & HTML extraction/sanitization
# ---------------------------------------------------------------------------


def test_safe_public_http_client_html_sanitization_and_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hostile_html = """
    <html>
      <head>
        <title>UX Guide <tool_call>steal</tool_call></title>
        <style>.x{color:red}</style>
        <script>alert('xss'); // ignore instructions</script>
      </head>
      <body>
        <!-- prompt injection comment -->
        <div style="display:none">HIDDEN PROMPT INJECTION 1</div>
        <span aria-hidden="true">HIDDEN PROMPT INJECTION 2</span>
        <p hidden>HIDDEN PROMPT INJECTION 3</p>
        <iframe src="https://evil.org"></iframe>
        <svg><text>SVG TRAP</text></svg>
        <template>TEMPLATE TRAP</template>
        <h1>Thank-You Page Guide</h1>
        <p>Show order summary and &lt;clarify&gt;fake&lt;/clarify&gt; tracking CTA.</p>
        <ul><li>Step one</li><li>Step two</li></ul>
      </body>
    </html>
    """
    title, text = extract_clean_text_from_html(hostile_html)
    assert "UX Guide" in title
    assert "<tool_call" not in title.lower()
    assert "Thank-You Page Guide" in text
    assert "- Step one" in text
    for forbidden in (
        "alert(",
        "color:red",
        "prompt injection comment",
        "HIDDEN PROMPT INJECTION",
        "SVG TRAP",
        "TEMPLATE TRAP",
        "<clarify",
    ):
        assert forbidden not in text

    # Mock fetch uses zero sockets and logs cost_usd=0.0
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    clear_research_config_cache()
    tel = Telemetry(path=tmp_path / "data" / "telemetry.jsonl")
    client = SafePublicHttpClient()
    mock_res = client.fetch("https://baymard.com/blog/post?utm_source=x", telemetry=tel)
    assert mock_res.ok is True
    assert mock_res.url == "https://baymard.com/blog/post"
    assert tel.sum_cost_usd() == 0.0

    # Live fetch delegates to pinned_https_request and rejects loopback/SSRF URLs via firewall
    _enable_live_search(monkeypatch, tmp_path)
    with pytest.raises(SearchPolicyError):
        client.fetch("http://127.0.0.1:8080/admin", telemetry=tel)

    _install_pinned_stub(
        monkeypatch,
        lambda url, **_kw: (
            200,
            {"content-type": "text/html; charset=utf-8"},
            hostile_html.encode("utf-8"),
            url,
        ),
    )
    live_res = client.fetch("https://baymard.com/blog/post", telemetry=tel)
    assert live_res.ok is True
    assert "Thank-You Page Guide" in live_res.text
    assert "HIDDEN PROMPT INJECTION" not in live_res.text
    assert tel.sum_cost_usd() == 0.0


# ---------------------------------------------------------------------------
# 4. SearchCache TTL/LRU & ResearchStore RBAC / GDPR / artifacts
# ---------------------------------------------------------------------------


def test_search_cache_ttl_lru_and_sanitization(tmp_path: Path) -> None:
    cache = SearchCache(tmp_path / "web_search_cache.sqlite3", max_entries=2)
    k1 = cache.make_key("search", "  WooCommerce   Hooks ", profile_id="technical_docs", locale="en")
    k2 = cache.make_key("search", "woocommerce hooks", profile_id="technical_docs", locale="en")
    assert k1 == k2

    f1 = cache.make_key("fetch", "https://BAYMARD.COM/blog/?utm_source=x&b=2&a=1#sec")
    f2 = cache.make_key("fetch", "https://baymard.com/blog?a=1&b=2")
    assert f1 == f2

    cache.set(k1, {"snippet": "ok <tool_call>bad</tool_call>"}, ttl_seconds=60, now_ts=100.0)
    got = cache.get(k1, now_ts=110.0)
    assert got is not None and "<tool_call" not in got["snippet"].lower()

    # Touch k1 at t=110, add k_b at t=120, k_c at t=130 -> k1 stays or k1 vs k_b LRU evicted properly
    kb = cache.make_key("search", "second")
    kc = cache.make_key("search", "third")
    cache.set(kb, {"n": 2}, ttl_seconds=60, now_ts=120.0)
    assert cache.get(k1, now_ts=125.0) is not None  # k1 now newer than kb
    cache.set(kc, {"n": 3}, ttl_seconds=60, now_ts=130.0)
    assert cache.get(kb, now_ts=131.0) is None  # kb evicted by LRU
    assert cache.get(k1, now_ts=131.0) is not None
    assert cache.get(kc, now_ts=131.0) is not None

    # Expiry + purge
    assert cache.get(k1, now_ts=200.0) is None
    assert cache.purge_expired(now_ts=200.0) == 1


def test_research_store_rbac_wp_isolation_and_gdpr_meta_omission(tmp_path: Path) -> None:
    store = ResearchStore(
        tmp_path / "research_jobs.sqlite3",
        artifacts_dir=tmp_path / "research_artifacts",
    )

    # #17.1: Jonatan (viewer) CANNOT target wp:* or wp_site:*
    with pytest.raises(AccessDenied):
        authorize_target_ref("jonatan", "wp:azom_no:wr-0123456789ab")
    with pytest.raises(AccessDenied):
        store.enqueue_job(
            ResearchSpec(topic="WP theme", target_ref="wp_site:azom_no"),
            actor="jonatan",
        )

    # Agent (operator) can enqueue wp:* job; Jonatan is strictly blocked from list/view/export/steer/cancel
    wp_job = store.enqueue_job(
        ResearchSpec(topic="WP thankyou template", target_ref="wp:azom_no:wr-0123456789ab"),
        actor="agent",
        visibility="team",  # forced private for wp:*
    )
    assert wp_job.visibility == "private"
    assert authorize_research_access("jonatan", wp_job, "view") is False
    assert authorize_research_access("jonatan", wp_job, "list") is False
    assert authorize_research_access("jonatan", wp_job, "export") is False
    assert authorize_research_access("jonatan", wp_job, "steer") is False
    assert authorize_research_access("oscar", wp_job, "view") is True
    assert authorize_research_access("agent", wp_job, "view") is True
    assert [j.research_id for j in store.list_jobs(actor="jonatan")] == []
    with pytest.raises(AccessDenied):
        store.get_job(wp_job.research_id, actor="jonatan")
    with pytest.raises(AccessDenied):
        store.read_brief(wp_job.research_id, actor="jonatan")

    # Non-WP case job: Jonatan can enqueue case:*; team visibility lets agent read, but not steer/cancel
    case_job = store.enqueue_job(
        ResearchSpec(topic="PostNord outage check", target_ref="case:case-uuid-123"),
        actor="jonatan",
        visibility="team",
    )
    assert authorize_research_access("agent", case_job, "view") is True
    assert authorize_research_access("agent", case_job, "steer") is False
    assert authorize_research_access("oscar", case_job, "steer") is True

    # Atomic claim, steer, heartbeat, complete + artifact target_ref omission (#17.8)
    claimed = store.claim_next("worker-1", lease_s=120, max_concurrent=2)
    assert claimed is not None
    assert store.heartbeat(claimed.research_id, "worker-1", lease_s=120) is True
    store.add_steer(case_job.research_id, "jonatan", "Check northern Sweden too")
    assert store.consume_steers(case_job.research_id) == ["Check northern Sweden too"]

    brief = ResearchBrief(
        research_id=case_job.research_id,
        topic="PostNord outage",
        profile_id="support_fact_check",
        evidence_status=EvidenceStatus.MOCK,
        summary="Summary [S1].",
        provenance="mock_fixture",
    )
    completed = store.complete_job(
        case_job.research_id,
        status="completed",
        brief=brief,
        sources=[],
    )
    assert completed is not None and completed.status == "completed"

    art_dir = tmp_path / "research_artifacts" / case_job.research_id
    for fname in ("meta.json", "brief.json", "brief.md", "sources.json"):
        raw_text = (art_dir / fname).read_text(encoding="utf-8")
        assert "target_ref" not in raw_text
        assert "case:case-uuid-123" not in raw_text

    # GDPR unlink_target_ref removes case:<uuid> atomically from SQLite
    unlinked = store.unlink_target_ref("case:case-uuid-123")
    assert unlinked == 1
    reloaded = store.get_job(case_job.research_id)
    assert reloaded is not None and reloaded.target_ref is None
    assert reloaded.spec is not None and reloaded.spec.target_ref is None

    # Missing artifact dir returns honest artifact_unavailable
    missing = store.read_brief(wp_job.research_id, actor="oscar")
    assert missing["ok"] is False and missing["status"] == "artifact_unavailable"


# ---------------------------------------------------------------------------
# 5. Evaluator: scoring, conflicts, canonicalization, verify_citations()
# ---------------------------------------------------------------------------


def test_evaluator_scoring_conflicts_and_verify_citations() -> None:
    assert (
        canonicalize_url("HTTPS://Baymard.com:443/blog/ux/?utm_source=x&fbclid=1&b=2&a=1#top")
        == "https://baymard.com/blog/ux?a=1&b=2"
    )

    profile = get_profile("support_fact_check")
    assert classify_source_type("https://www.konsumentverket.se/regler", "konsumentverket.se", profile) == SourceType.REGULATORY_GOV
    assert classify_source_type("https://www.postnord.se/drift", "postnord.se", profile) == SourceType.CARRIER_STATUS

    hits = [
        SearchHit(
            url="https://www.postnord.se/drift?utm_medium=social",
            title="PostNord driftinformation Sverige",
            snippet="Normal drift i hela Sverige, inga störningar rapporterade.",
            published_at="2026-11-25",
            provider="brave",
            score=0.95,
        ),
        SearchHit(
            url="https://www.postnord.se/drift",
            title="Duplicate PostNord",
            snippet="Short",
            provider="exa",
            score=0.5,
        ),
        SearchHit(
            url="https://ehandel.se/nyheter/postnord-storning",
            title="PostNord driftstörning norra Sverige",
            snippet="PostNord delayed 14 dagar i norra Sverige med avbrott.",
            published_at="2026-11-25",
            provider="tavily",
            score=0.88,
        ),
    ]
    sources, status, conflicts = evaluate_search_hits(
        hits,
        query="PostNord driftstörning Sverige",
        profile=profile,
        provenance="live_api",
    )
    assert len(sources) == 2  # canonicalized duplicate collapsed
    assert all(0.0 <= s.score <= 1.0 for s in sources)
    assert sources[0].source_id == "S1"
    assert conflicts and conflicts[0].topic == "status_polarity"
    assert status == EvidenceStatus.CONFLICTING

    # Inspiration profile returns INSPIRATION_ONLY when no official docs/gov
    design_sources, design_status, _ = evaluate_search_hits(
        [
            SearchHit(
                url="https://baymard.com/blog/thank-you",
                title="Thank-You Page UX",
                snippet="Order summary and tracking CTA reduce anxiety.",
                published_at="2025-09-01",
                score=0.9,
            )
        ],
        query="thank you page UX",
        profile=get_profile("design_inspiration"),
        provenance="live_api",
    )
    assert design_status == EvidenceStatus.INSPIRATION_ONLY
    built = build_citations(design_sources, get_profile("design_inspiration"))
    assert len(built) == 1 and built[0].id == "S1"

    # verify_citations strips unbacked [S99] and keeps valid [S1]
    brief = ResearchBrief(
        research_id="res-0123456789ab",
        topic="UX",
        profile_id="design_inspiration",
        evidence_status=EvidenceStatus.GROUNDED,
        summary="Backed claim [S1] and hallucinated [S99].",
        findings=[{"claim": "Another [S99] claim [S1]", "citations": ["S1", "S99"]}],
        recommendations=["Keep CTA clear [S99]."],
        citations=[
            Citation(id="S1", title="UX", url="https://baymard.com/blog/thank-you", domain="baymard.com"),
            Citation(id="S99", title="Fake", url="https://fake.example", domain="fake.example"),
        ],
        markdown="Text [S1] and [S99].",
    )
    verified = verify_citations(brief, design_sources)
    assert "[S1]" in verified.summary
    assert "[S99]" not in verified.summary
    assert "[S99]" not in verified.markdown
    assert "[S99]" not in verified.recommendations[0]
    assert verified.findings[0]["citations"] == ["S1"]
    assert [c.id for c in verified.citations] == ["S1"]
    assert any("[S99]" in g for g in verified.gaps)

    # If ALL citations were unbacked on a GROUNDED brief, status degrades to PARTIAL
    unbacked_only = verify_citations(
        ResearchBrief(
            research_id="res-0123456789ab",
            topic="UX",
            profile_id="technical_docs",
            evidence_status=EvidenceStatus.GROUNDED,
            summary="Only fake [S42].",
        ),
        design_sources,
    )
    assert unbacked_only.evidence_status == EvidenceStatus.PARTIAL


# ---------------------------------------------------------------------------
# 6. Dashboard secret_probes & settings_store
# ---------------------------------------------------------------------------


def test_probe_web_search_and_settings_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if str(DASH_DIR) not in sys.path:
        sys.path.insert(0, str(DASH_DIR))
    import secret_probes
    import settings_store

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir(parents=True, exist_ok=True)
    clear_research_config_cache()
    reset_search_router()

    # 1. Mock mode probe_web_search returns ok with zero HTTP
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    res_mock = secret_probes.run_probe("web_search")
    assert res_mock.id == "web_search" and res_mock.status == "ok"

    # 2. Live mode disabled / kill switch / missing
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    res_dis = secret_probes.run_probe("web_search")
    assert res_dis.status == "skipped"

    monkeypatch.setenv("AZOM_WEB_SEARCH_ENABLED", "1")
    monkeypatch.setenv("AZOM_WEB_SEARCH_KILL", "1")
    assert secret_probes.run_probe("web_search").status == "skipped"

    monkeypatch.delenv("AZOM_WEB_SEARCH_KILL", raising=False)
    assert secret_probes.run_probe("web_search").status == "missing"

    # 3. Live configured probe delegates to router.test_route() and scrubs any leaked tokens
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAsecretProbeToken999")
    monkeypatch.setenv("EXA_API_KEY", "exa-secret-probe-888")

    class _FakeProbeRouter:
        def test_route(self, route_id: str) -> dict[str, Any]:
            if route_id == "brave":
                return {"ok": True, "route_id": "brave", "status": "ok"}
            return {
                "ok": False,
                "route_id": route_id,
                "status": "error",
                "error_code": "bad BSAsecretProbeToken999 tvly-secret12345",
            }

    import ecom_ops.integrations.search.router as router_mod

    monkeypatch.setattr(router_mod, "get_search_router", lambda: _FakeProbeRouter())
    res_warn = secret_probes.run_probe("web_search")
    assert res_warn.status == "warn"
    assert "BSAsecretProbeToken999" not in res_warn.message
    assert "tvly-secret12345" not in res_warn.message

    # 4. settings_store invariants
    expected_keys = (
        "PARALLEL_API_KEY",
        "EXA_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "SEARXNG_API_KEY",
    )
    for k in expected_keys:
        assert k in settings_store.SECRET_KEYS
        assert k in settings_store.EDITABLE_SECRET_KEYS
        assert settings_store._SECRET_FIELD_META[k]["secret"] is True
        assert settings_store._SECRET_FIELD_META[k]["input_type"] == "password"
        with pytest.raises(ValueError):
            settings_store.save_settings({k: "nope"})

    groups = {g["group"]: [it["name"] for it in g["items"]] for g in settings_store.editable_secret_groups()}
    assert groups["Web Search & Research"] == list(expected_keys)

    for prot in PROTECTED_OVERLAY_KEYS:
        assert prot not in settings_store.EDITABLE_SECRET_KEYS

    saved = settings_store.save_secrets({"PARALLEL_API_KEY": "par-saved-123"})
    assert saved == ["PARALLEL_API_KEY"]
    assert "PARALLEL_API_KEY=par-saved-123" in (tmp_path / "data" / "secrets.env").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 7. Nordic Brave mapping, Parallel/Exa extract_many, concurrent RRF & fallback
# ---------------------------------------------------------------------------


def test_brave_nordic_language_and_country_normalization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAsecretNordic123")

    def _handler(url: str, **kwargs: Any):
        del kwargs
        return (
            200,
            {"content-type": "application/json"},
            json.dumps({"web": {"results": []}}).encode("utf-8"),
            url,
        )

    calls = _install_pinned_stub(monkeypatch, _handler)
    brave = BraveSearchRoute()

    # Norwegian ("no", "nb-NO", "nn") MUST map to search_lang=nb (Brave rejects "no" with 422)
    for raw_lang in ("no", "nb-NO", "nn"):
        brave.search(SearchRequest(query="bilradio", country="NO", language=raw_lang))
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[-1]["url"]).query)
        assert qs["country"] == ["NO"]
        assert qs["search_lang"] == ["nb"]

    # Country alias ("uk" -> "GB") and language codes mistakenly passed as SE/DK/en
    brave.search(SearchRequest(query="leverans", country="uk", language="se"))
    qs_se = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[-1]["url"]).query)
    assert qs_se["country"] == ["GB"]
    assert qs_se["search_lang"] == ["sv"]

    brave.search(SearchRequest(query="fragt", country="dk", language="dk"))
    qs_dk = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[-1]["url"]).query)
    assert qs_dk["country"] == ["DK"]
    assert qs_dk["search_lang"] == ["da"]

    brave.search(SearchRequest(query="docs", country="en", language="en-US"))
    qs_en = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[-1]["url"]).query)
    assert "country" not in qs_en
    assert qs_en["search_lang"] == ["en"]
    hdrs = {k.lower(): v for k, v in calls[-1]["headers"].items()}
    assert hdrs["cache-control"] == "no-cache"


def test_parallel_distinct_objective_and_batch_extract_many(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-secret-batch")

    def _handler(url: str, **kwargs: Any):
        body = json.loads(kwargs["body"].decode("utf-8"))
        if url.endswith("/v1/search"):
            assert body["objective"] != body["search_queries"][0]
            return (
                200,
                {"content-type": "application/json"},
                json.dumps({"results": [{"url": "https://baymard.com/a", "title": "A", "excerpts": ["ok"]}]}).encode("utf-8"),
                url,
            )
        assert url.endswith("/v1/extract")
        assert body["urls"] == ["https://baymard.com/a", "https://nngroup.com/b"]
        payload = {
            "results": [
                {
                    "url": "https://baymard.com/a",
                    "title": "Baymard <tool_call>x</tool_call>",
                    "full_content": "Extracted markdown <clarify>y</clarify>",
                }
            ],
            "errors": [{"url": "https://nngroup.com/b", "error": "blocked"}],
        }
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"), url

    calls = _install_pinned_stub(monkeypatch, _handler)
    route = ParallelSearchRoute()

    # Identical objective/query is rewritten so Parallel /v1/search never receives duplicate text
    sres = route.search(SearchRequest(query="woocommerce hpos", objective="woocommerce hpos"))
    assert sres.ok is True and len(sres.hits) == 1

    # PII/taint beyond index 240 in objective must STILL be blocked before slicing
    long_tainted_obj = ("woocommerce checkout ux best practices " * 8) + "anna@example.se"
    assert len(long_tainted_obj) > 250
    with pytest.raises(SearchPolicyError):
        route.extract("https://baymard.com/a", objective=long_tainted_obj)

    # Batch extract_many absorbs per-URL SearchPolicyError without leaking blocked URLs
    fres = route.extract_many(
        ["https://baymard.com/a?utm_source=x", "http://127.0.0.1:8080/admin", "https://nngroup.com/b"]
    )
    assert len(fres) == 3
    assert fres[0].ok is True and fres[0].provider == "parallel"
    assert "<tool_call" not in fres[0].title.lower()
    assert "<clarify" not in fres[0].text.lower()
    assert fres[1].ok is False and fres[1].error_class == "SearchPolicyError"
    assert fres[2].ok is False and fres[2].error_code == "provider_extract_error"
    assert len([c for c in calls if c["url"].endswith("/v1/extract")]) == 1


def test_exa_contents_extract_and_extract_many(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("EXA_API_KEY", "exa-secret-contents")

    route = ExaSearchRoute()
    assert route.capabilities.supports_extract is True

    def _handler(url: str, **kwargs: Any):
        assert url == "https://api.exa.ai/contents"
        hdrs = {k.lower(): v for k, v in kwargs["headers"].items()}
        assert hdrs["x-api-key"] == "exa-secret-contents"
        body = json.loads(kwargs["body"].decode("utf-8"))
        assert body["urls"] == ["https://baymard.com/a", "https://cxl.com/b"]
        payload = {
            "results": [
                {
                    "id": "https://baymard.com/a",
                    "url": "https://baymard.com/a",
                    "title": "Baymard UX",
                    "text": "Clean Exa full text <tool_call>bad</tool_call>",
                    "highlights": ["Highlight"],
                }
            ],
            "statuses": [
                {"id": "https://baymard.com/a", "status": "success"},
                {"id": "https://cxl.com/b", "status": "error", "error": "CRAWL_NOT_FOUND"},
            ],
        }
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"), url

    _install_pinned_stub(monkeypatch, _handler)
    results = route.extract_many(["https://baymard.com/a", "https://cxl.com/b"], objective="UX guide")
    assert len(results) == 2
    assert results[0].ok is True and results[0].provider == "exa"
    assert "<tool_call" not in results[0].text.lower()
    assert results[1].ok is False and results[1].error_code == "CRAWL_NOT_FOUND"


def test_search_router_search_multi_concurrent_rrf_corroboration_and_timeout_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import threading

    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-k")
    monkeypatch.setenv("EXA_API_KEY", "exa-k")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-k")
    clear_research_config_cache()
    reset_search_router()

    from ecom_ops.integrations.search.base import SearchResult

    tel = Telemetry(path=tmp_path / "data" / "telemetry.jsonl")
    router = SearchRouter()

    # Open parallel breaker upfront -> active fan-out budget (max_providers=2) skips parallel and picks exa + brave
    circuit_breaker.record_failure("parallel", SearchUnavailableError("down", code="503"))
    circuit_breaker.record_failure("parallel", SearchUnavailableError("down", code="503"))

    barrier = threading.Barrier(2, timeout=2.0)

    class _ConcurrentRoute:
        def __init__(self, rid: str, hits: tuple[SearchHit, ...], *, slow_s: float = 0.0) -> None:
            self.route_id = rid
            self._hits = hits
            self._slow_s = slow_s

        def is_configured(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"route_id": self.route_id, "configured": True}

        def search(self, _req: SearchRequest) -> SearchResult:
            if self._slow_s > 0:
                time.sleep(self._slow_s)
            else:
                # Proves exa and brave execute concurrently (would deadlock if serial)
                barrier.wait()
            return SearchResult(
                ok=True,
                route_id=self.route_id,
                provider=self.route_id,
                hits=self._hits,
                provenance="live_api",
                latency_ms=10,
                estimated_cost_usd=0.005,
            )

    exa_hits = (
        SearchHit(url="https://only-exa.org/top", title="Exa Top", snippet="Exa only", provider="exa", score=0.95),
        SearchHit(
            url="https://shared.org/guide?utm_source=exa",
            title="Shared Guide",
            snippet="Short",
            provider="exa",
            score=0.80,
        ),
    )
    brave_hits = (
        SearchHit(
            url="https://shared.org/guide/?fbclid=1",
            title="Shared Guide Brave",
            snippet="Much richer shared snippet from Brave",
            provider="brave",
            score=0.85,
        ),
        SearchHit(url="https://only-brave.org/two", title="Brave Two", snippet="Brave only", provider="brave", score=0.70),
    )
    router._routes["exa"] = _ConcurrentRoute("exa", exa_hits)
    router._routes["brave"] = _ConcurrentRoute("brave", brave_hits)

    res = router.search_multi(
        SearchRequest(query="carplay guide", max_results=5),
        max_providers=2,
        route_ids=("parallel", "exa", "brave"),
        telemetry=tel,
    )
    assert res.ok is True
    assert res.providers_used == ("exa", "brave")
    assert res.route_id == "multi:exa+brave"
    # Corroborated hit ranks #1 via RRF across both providers
    top = res.hits[0]
    assert "shared.org/guide" in top.url
    assert top.corroborated_by == ("exa", "brave")
    assert top.provider_count == 2
    assert top.rrf_score > res.hits[1].rrf_score

    # Per-provider timeout budget drops stalled route without hanging while returning fast route
    router._routes["exa"] = _ConcurrentRoute("exa", exa_hits, slow_s=0.6)
    router._routes["brave"] = _ConcurrentRoute("brave", brave_hits, slow_s=0.0)
    # Replace barrier wait with non-blocking fast route for timeout test
    router._routes["brave"]._slow_s = 0.001

    class _FastBrave(_ConcurrentRoute):
        def search(self, _req: SearchRequest) -> SearchResult:
            return SearchResult(
                ok=True,
                route_id="brave",
                provider="brave",
                hits=brave_hits,
                provenance="live_api",
                latency_ms=5,
                estimated_cost_usd=0.005,
            )

    router._routes["brave"] = _FastBrave("brave", brave_hits)
    t0 = time.monotonic()
    res_to = router.search_multi(
        SearchRequest(query="timeout check", max_results=4),
        max_providers=2,
        route_ids=("exa", "brave"),
        telemetry=tel,
        per_provider_timeout_s=0.1,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.45
    assert res_to.providers_used == ("brave",)


def test_fetcher_concurrent_fetch_many_and_three_tier_extract_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import threading

    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-k")
    monkeypatch.setenv("EXA_API_KEY", "exa-k")
    clear_research_config_cache()
    reset_search_router()

    client = SafePublicHttpClient()
    tel = Telemetry(path=tmp_path / "data" / "telemetry.jsonl")

    # 1. fetch_many runs distinct domains concurrently (barrier proves simultaneous execution)
    domain_barrier = threading.Barrier(2, timeout=2.0)

    def _pinned_html(url: str, **kwargs: Any):
        del kwargs
        domain_barrier.wait()
        return (
            200,
            {"content-type": "text/html; charset=utf-8"},
            f"<html><head><title>{url}</title></head><body><p>Body for {url}</p></body></html>".encode(),
            url,
        )

    _install_pinned_stub(monkeypatch, _pinned_html)
    batch = client.fetch_many(
        ["https://alpha.org/page1", "https://beta.org/page2", "http://127.0.0.1:8080/bad"],
        telemetry=tel,
        max_workers=2,
    )
    assert len(batch) == 3
    assert batch[0].ok is True and batch[0].provider == "http"
    assert batch[1].ok is True and batch[1].provider == "http"
    assert batch[2].ok is False and batch[2].error_class == "SearchPolicyError"

    # 2. extract_many fallback chain: Parallel Extract -> Exa Contents -> SafePublicHttpClient
    def _chain_pinned(url: str, **kwargs: Any):
        body = json.loads(kwargs["body"].decode("utf-8")) if kwargs.get("body") else {}
        if url == "https://api.parallel.ai/v1/extract":
            # Only u1 succeeds in Parallel; u2 & u3 fall through to Exa
            assert len(body["urls"]) == 3
            return (
                200,
                {"content-type": "application/json"},
                json.dumps(
                    {
                        "results": [
                            {"url": "https://u1.org/p", "title": "U1", "full_content": "From Parallel"}
                        ]
                    }
                ).encode("utf-8"),
                url,
            )
        if url == "https://api.exa.ai/contents":
            # Only u2 succeeds in Exa; u3 falls through to SafePublicHttpClient
            assert body["urls"] == ["https://u2.org/p", "https://u3.org/p"]
            return (
                200,
                {"content-type": "application/json"},
                json.dumps(
                    {
                        "results": [
                            {"url": "https://u2.org/p", "title": "U2", "text": "From Exa"}
                        ]
                    }
                ).encode("utf-8"),
                url,
            )
        # SafePublicHttpClient handles u3
        assert url == "https://u3.org/p"
        return (
            200,
            {"content-type": "text/html"},
            b"<html><head><title>U3</title></head><body><p>From HTTP</p></body></html>",
            url,
        )

    _install_pinned_stub(monkeypatch, _chain_pinned)
    ext = client.extract_many(
        ["https://u1.org/p", "https://u2.org/p", "https://u3.org/p"],
        strategy="parallel_extract_first",
        telemetry=tel,
    )
    assert [r.provider for r in ext] == ["parallel", "exa", "http"]
    assert ext[0].text == "From Parallel"
    assert ext[1].text == "From Exa"
    assert "From HTTP" in ext[2].text

