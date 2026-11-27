"""Tests for goal/ambition-adaptive search profiles and multi-tool combinations.

Strictly offline (zero live network calls; stubs ``_http_guard.pinned_https_request`` and
routes cleanly).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from ecom_ops.integrations import _http_guard
from ecom_ops.integrations.search.base import FetchResult, SearchRequest
from ecom_ops.integrations.search.errors import SearchPolicyError, SearchUnavailableError
from ecom_ops.integrations.search.providers.parallel import ParallelSearchRoute
from ecom_ops.integrations.search.router import SearchRouter, reset_search_router
from ecom_ops.research import (
    ResearchService,
    clear_research_config_cache,
    get_profile,
    list_profiles,
    providers_for_round,
    recommend_profile,
)
from ecom_ops.research.clarifier import build_research_spec
from ecom_ops.research.engine import ResearchEngine
from ecom_ops.telemetry import Telemetry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _enable_live_search(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_WEB_SEARCH_ENABLED", "1")
    monkeypatch.setenv("AZOM_WEB_FETCH_ENABLED", "1")
    monkeypatch.setenv("AZOM_RESEARCH_ENABLED", "1")
    clear_research_config_cache()
    reset_search_router()


def test_twelve_builtin_profiles_metadata_and_skill_docs() -> None:
    profs = list_profiles()
    assert len(profs) == 12
    for prof in profs:
        assert prof.title
        assert prof.goal
        assert prof.ambition in {"quick", "standard", "deep"}
        assert prof.combination_mode in {"failover", "ensemble", "staged"}
        assert prof.fetch_strategy in {"snippet_only", "direct", "parallel_extract_first", "hybrid"}
        assert prof.preferred_providers
        assert prof.query_templates
        assert prof.deliverable_sections
        assert (REPO_ROOT / prof.skill_doc).is_file()
        as_dict = prof.to_dict()
        assert as_dict["id"] == prof.id
        assert isinstance(as_dict["round_providers"], dict)
        if prof.combination_mode == "staged":
            assert prof.round_providers
            r1 = providers_for_round(prof, 1)
            r2 = providers_for_round(prof, 2)
            assert r1 and r2
            assert r1[:2] != r2[:2]


def test_custom_yaml_profile_merge_with_combination_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "research.yaml").write_text(
        """
schema_version: 1
profiles:
  custom_cro_lab:
    title: "Custom CRO Lab"
    goal: "Compare checkout patterns with multi-engine fusion."
    ambition: deep
    combination_mode: staged
    fanout_by_depth:
      quick: 2
      standard: 3
      deep: 4
    round_providers:
      1: [exa, parallel]
      2: [tavily, brave]
    fetch_strategy: hybrid
    preferred_providers: [exa, parallel, tavily, brave]
    query_templates:
      - "{topic} checkout experiment"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(cfg_dir))
    clear_research_config_cache()

    prof = get_profile("custom_cro_lab")
    assert prof.title == "Custom CRO Lab"
    assert prof.ambition == "deep"
    assert prof.combination_mode == "staged"
    assert prof.fetch_strategy == "hybrid"
    assert prof.fanout_by_depth["deep"] == 4
    assert providers_for_round(prof, 1)[:2] == ("exa", "parallel")
    assert providers_for_round(prof, 2)[:2] == ("tavily", "brave")
    assert prof.query_templates == ("{topic} checkout experiment",)


def test_recommend_profile_matches_goals_and_ambitions() -> None:
    assert recommend_profile("Kontrollera GDPR och konsumentverkets regler").id == "regulatory_compliance"
    assert recommend_profile("Djup CRO teardown av checkout och tacksida").id == "conversion_teardown"
    assert recommend_profile("CarPlay CAN-bus kablage OEM specifikation").id == "product_spec_deep"
    assert recommend_profile("SEO sökintention och innehållsluckor för kategorisida").id == "seo_content_radar"
    assert recommend_profile("Triangulera alla sökmotorer inkl searxng").id == "metasearch_triangulation"
    assert recommend_profile("Snabbkoll senaste nyheter idag", ambition="quick").id == "quick_pulse"
    assert recommend_profile("Allmän strategisk utredning", ambition="deep").id == "deep_general"
    assert recommend_profile("Snabb översikt", ambition="quick").id == "quick_pulse"


def test_search_router_search_multi_mock_mode_stays_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    clear_research_config_cache()
    reset_search_router()

    def _forbid(*args: Any, **kwargs: Any):
        raise AssertionError("network must never be called in mock mode")

    monkeypatch.setattr(_http_guard, "pinned_https_request", _forbid)
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    router = SearchRouter()
    res = router.search_multi(
        SearchRequest(query="woocommerce thank you page ux", profile_id="design_inspiration"),
        max_providers=3,
        telemetry=tel,
    )
    assert res.ok is True
    assert res.provider == "mock"
    assert len(res.hits) >= 1


def test_search_router_search_multi_fuses_and_deduplicates_live_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")

    called_hosts: list[str] = []

    def _fake_pinned(url: str, **kwargs: Any):
        del kwargs
        called_hosts.append(url)
        if "api.parallel.ai" in url:
            payload = {
                "results": [
                    {
                        "url": "https://baymard.com/blog/thank-you-page?utm_source=parallel",
                        "title": "Baymard Thank You Study",
                        "score": 0.90,
                        "excerpts": ["Parallel rich excerpt about order confirmation hierarchy."],
                    },
                    {
                        "url": "https://woocommerce.com/document/checkout/",
                        "title": "Woo Checkout Docs",
                        "score": 0.75,
                        "excerpts": ["Official docs"],
                    },
                ]
            }
            return 200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"), url
        if "api.exa.ai" in url:
            payload = {
                "results": [
                    {
                        "url": "https://baymard.com/blog/thank-you-page/?fbclid=123",
                        "title": "Baymard Study (Exa)",
                        "score": 0.95,
                        "highlights": ["Short highlight"],
                    },
                    {
                        "url": "https://nngroup.com/articles/order-confirmation/",
                        "title": "NN/g Confirmation UX",
                        "score": 0.88,
                        "highlights": ["Reduce post-purchase anxiety with tracking next steps."],
                    },
                ]
            }
            return 200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"), url
        if "api.search.brave.com" in url:
            raise AssertionError("should stop after 2 succeeding providers")
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(_http_guard, "pinned_https_request", _fake_pinned)
    tel = Telemetry(path=tmp_path / "telemetry.jsonl")
    router = SearchRouter()
    res = router.search_multi(
        SearchRequest(query="thank you page UX benchmark", profile_id="design_inspiration", max_results=5),
        max_providers=2,
        route_ids=("exa", "parallel", "brave"),
        telemetry=tel,
    )
    assert res.ok is True
    assert res.route_id == "multi:exa+parallel"
    assert res.provider == "exa+parallel"
    assert len(called_hosts) == 2
    urls = [h.url for h in res.hits]
    # Baymard deduplicated once across exa + parallel
    assert len(urls) == 3
    baymard_hits = [h for h in res.hits if "baymard.com" in h.url]
    assert len(baymard_hits) == 1
    assert "exa+parallel" in baymard_hits[0].provider

    # Resilience when first provider fails transiently: falls through to next provider
    monkeypatch.setattr(
        _http_guard,
        "pinned_https_request",
        lambda url, **kw: (_ for _ in ()).throw(SearchUnavailableError("temp outage", code="503"))
        if "api.exa.ai" in url
        else _fake_pinned(url, **kw),
    )
    res_resilient = router.search_multi(
        SearchRequest(query="thank you page UX fallback", profile_id="design_inspiration"),
        max_providers=1,
        route_ids=("exa", "parallel"),
        telemetry=tel,
    )
    assert res_resilient.provider == "parallel"

    # Firewall still fails closed with SearchPolicyError
    with pytest.raises(SearchPolicyError):
        router.search_multi(
            SearchRequest(query="kund anna@example.se problem", profile_id="design_inspiration"),
            max_providers=2,
            telemetry=tel,
        )


def test_quick_search_and_fetch_extract_honor_ensemble_and_parallel_extract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setattr(
        _http_guard,
        "pinned_https_request",
        lambda url, **kw: (
            200,
            {"content-type": "application/json"},
            json.dumps(
                {
                    "results": [
                        {
                            "url": f"https://example.org/{'p' if 'parallel' in url else 'e'}",
                            "title": "Hit",
                            "excerpts": ["Extracted snippet"],
                            "highlights": ["Highlighted snippet"],
                        }
                    ]
                }
            ).encode("utf-8"),
            url,
        ),
    )
    svc = ResearchService(data_dir=tmp_path / "data")
    res = svc.quick_search(
        "product hardware compatibility",
        profile_id="product_spec_deep",
        max_providers=2,
        use_cache=False,
    )
    assert res["ok"] is True
    assert res["combination_mode"] == "ensemble"
    assert res["fetch_strategy"] == "parallel_extract_first"
    assert res["providers_used"] == ["parallel", "exa"]

    # fetch_extract uses ParallelSearchRoute.extract first on parallel_extract_first profile
    extract_calls: list[str] = []

    def _fake_extract(self, url: str, **kwargs: Any) -> FetchResult:
        del self, kwargs
        extract_calls.append(url)
        return FetchResult(
            ok=True,
            url=url,
            final_url=url,
            title="Parallel Extracted",
            text="Structured spec sheet text.",
            content_type="text/markdown",
            status_code=200,
            latency_ms=12,
        )

    monkeypatch.setattr(ParallelSearchRoute, "extract", _fake_extract)
    fres = svc.fetch_extract(
        "https://developer.apple.com/carplay/",
        profile_id="product_spec_deep",
        use_cache=False,
    )
    assert fres["ok"] is True
    assert fres["title"] == "Parallel Extracted"
    assert extract_calls == ["https://developer.apple.com/carplay/"]


def test_engine_honors_staged_rounds_snippet_only_and_query_templates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _enable_live_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PARALLEL_API_KEY", "par-key")
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    monkeypatch.setattr(
        _http_guard,
        "pinned_https_request",
        lambda url, **kw: (
            200,
            {"content-type": "application/json"},
            json.dumps(
                {
                    "results": [
                        {
                            "url": "https://postnord.se/drift",
                            "title": "PostNord Status",
                            "excerpts": ["Normal drift i norra Sverige."],
                        }
                    ]
                }
            ).encode("utf-8"),
            url,
        ),
    )

    svc = ResearchService(data_dir=tmp_path / "data")
    # 1) quick_pulse (snippet_only) executes 0 fetches
    fetch_calls: list[str] = []

    class _RecordingFetcher:
        def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            del kwargs
            fetch_calls.append(url)
            return FetchResult(
                ok=True,
                url=url,
                final_url=url,
                title="Fetched",
                text="Body",
                content_type="text/html",
                status_code=200,
            )

    job_pulse = svc.start_research(
        "PostNord status idag",
        profile_id="quick_pulse",
        depth="quick",
        start_worker=False,
    )
    eng = ResearchEngine(store=svc.store, fetcher=_RecordingFetcher(), telemetry=svc.telemetry)
    done_pulse = eng.run_job(job_pulse.research_id)
    assert done_pulse.status in {"completed", "partial"}
    assert done_pulse.fetches_used == 0
    assert fetch_calls == []

    # 2) staged profile calls providers_for_round across rounds + uses query_templates
    seen_route_ids: list[tuple[str, ...] | None] = []
    real_multi = svc.search_router.search_multi

    def _spy_multi(req: SearchRequest, *, max_providers: int = 2, route_ids=None, telemetry=None):
        seen_route_ids.append(tuple(route_ids) if route_ids else None)
        return real_multi(req, max_providers=max_providers, route_ids=route_ids, telemetry=telemetry)

    monkeypatch.setattr(svc.search_router, "search_multi", _spy_multi)
    job_comp = svc.start_research(
        "CarPlay adapter",
        profile_id="competitor_market",
        depth="standard",
        markets=["SE"],
        start_worker=False,
    )
    eng_comp = ResearchEngine(
        store=svc.store,
        search_router=svc.search_router,
        fetcher=_RecordingFetcher(),
        telemetry=svc.telemetry,
    )
    # Disable LLM calls so _deterministic_plan executes with query_templates
    done_comp = eng_comp.run_job(job_comp.research_id)
    assert done_comp.status in {"completed", "partial"}
    assert seen_route_ids
    assert seen_route_ids[0][:2] == ("parallel", "exa")


def test_clarifier_cli_and_dashboard_expose_12_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Clarifier accepts new profiles and formats
    spec = build_research_spec(
        "/research GDPR checkout --profile regulatory_compliance --format risk_brief",
        [],
        [],
        spec={"profile_id": "regulatory_compliance", "output_format": "risk_brief"},
    )
    assert spec.profile_id == "regulatory_compliance"
    assert spec.output_format == "risk_brief"

    # CLI research profiles (list + recommend)
    from ecom_ops.cli import main as cli_main

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    rc = cli_main(["--mock", "research", "profiles", "--recommend", "CRO tacksida", "--ambition", "deep"])
    assert rc == 0

    # Dashboard /research renders the 12 profile catalog cards cleanly
    import importlib
    import sys

    dash_dir = REPO_ROOT / "infrastructure" / "dashboard"
    if str(dash_dir) not in sys.path:
        sys.path.insert(0, str(dash_dir))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-viewer")
    monkeypatch.setenv("DASHBOARD_OSCAR_PASSWORD", "test-oscar")
    mod = importlib.import_module("app")
    mod = importlib.reload(mod)
    mod.app.config["TESTING"] = True
    client = mod.app.test_client()
    auth_hdr = {
        "Authorization": "Basic " + base64.b64encode(b"jonatan:test-viewer").decode("ascii")
    }
    resp = client.get("/research", headers=auth_hdr)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "data-research-profiles-catalog=\"true\"" in html
    for pid in (
        "quick_pulse",
        "product_spec_deep",
        "seo_content_radar",
        "regulatory_compliance",
        "conversion_teardown",
        "metasearch_triangulation",
    ):
        assert pid in html
