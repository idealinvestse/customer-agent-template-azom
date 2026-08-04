"""Google Ads + GA4 marketing track — mock-first coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecom_ops.actions.marketing import MarketingService
from ecom_ops.cli import main
from ecom_ops.integrations.ga4 import InMemoryGA4Transport
from ecom_ops.integrations.ga4 import client_from_env as ga4_client
from ecom_ops.integrations.google_ads import (
    InMemoryGoogleAdsTransport,
)
from ecom_ops.integrations.google_ads import (
    client_from_env as ads_client,
)
from ecom_ops.marketing.config import clear_marketing_config_cache, load_marketing_config
from ecom_ops.marketing.kill_switch import ads_mutate_allowed, ga_mutate_allowed
from ecom_ops.marketing.suggest_store import MarketingSuggestStore
from ecom_ops.rbac import clear_rbac_cache


@pytest.fixture(autouse=True)
def _mkt_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "AZOM_CONFIG_DIR", str(Path(__file__).resolve().parents[1] / "config")
    )
    monkeypatch.delenv("AZOM_ADS_MUTATE_KILL", raising=False)
    monkeypatch.delenv("AZOM_MP_KILL", raising=False)
    monkeypatch.delenv("AZOM_GA4_PROPERTY_IDS", raising=False)
    monkeypatch.delenv("AZOM_GADS_CUSTOMER_IDS", raising=False)
    clear_marketing_config_cache()
    clear_rbac_cache()
    yield
    clear_marketing_config_cache()
    clear_rbac_cache()


def test_config_mutates_default_off():
    cfg = load_marketing_config()
    assert cfg.ads_mutate_enabled is False
    assert cfg.measurement_protocol_enabled is False


def test_ads_mutate_kill_switch(monkeypatch):
    monkeypatch.setenv("AZOM_ADS_MUTATE_KILL", "1")
    ok, reason = ads_mutate_allowed()
    assert ok is False
    assert reason == "ads_mutate_kill"


def test_ga_mutate_kill_switch(monkeypatch):
    """Reserved GA-admin kill rail — deny when set (no mutate path yet)."""
    monkeypatch.setenv("AZOM_GA_MUTATE_KILL", "1")
    ok, reason = ga_mutate_allowed()
    assert ok is False
    assert reason == "ga_mutate_kill"


def test_ga_mutate_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AZOM_GA_MUTATE_KILL", raising=False)
    ok, reason = ga_mutate_allowed()
    assert ok is False
    assert reason == "ga_mutate_disabled"


def test_ga4_mock_digest():
    client = ga4_client(use_mock=True)
    dig = client.digest(days=7)
    assert dig["source"] == "ga4_data_api"
    assert dig["ecommerce_purchases"] > 0


def test_ads_mock_waste():
    client = ads_client(use_mock=True)
    rows = client.waste_report(days=7, min_cost_micros=1)
    assert rows
    assert all(r["conversions"] == 0 for r in rows)


def test_fail_closed_allowlist_live(monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_GA4_PROPERTY_IDS", "")
    clear_marketing_config_cache()
    # Transport type must NOT bypass allowlist when mock is off
    client = ga4_client(use_mock=False, transport=InMemoryGA4Transport())
    with pytest.raises(PermissionError, match="fail-closed"):
        client.digest(days=1)

    from ecom_ops.integrations.ga4 import GA4Client, LiveGA4Transport

    live = GA4Client(transport=LiveGA4Transport(access_token="x"), property_ids=())
    with pytest.raises(PermissionError, match="fail-closed"):
        live.digest(days=1)


def test_marketing_digest_service():
    svc = MarketingService(use_mock=True)
    result = svc.digest(days=7, actor="agent")
    assert result.ok
    assert "ads" in (result.data or {})
    assert "ga4" in (result.data or {})
    assert "truth_note" in (result.data or {})


def test_consistency_and_mer():
    svc = MarketingService(use_mock=True)
    cons = svc.consistency(days=7, woo_purchases=45, actor="jonatan")
    assert cons.ok
    assert cons.data["divergence"]["woo_vs_ga"] in {"ok", "warn", "error"}
    mer = svc.mer(days=7, woo_revenue=132000, actor="jonatan")
    assert mer.ok
    assert mer.data["mer"] is not None


def test_suggest_queue_and_negative_approve(tmp_path, monkeypatch):
    store = MarketingSuggestStore(path=tmp_path / "suggests.jsonl")
    ads_t = InMemoryGoogleAdsTransport()
    svc = MarketingService(
        use_mock=True,
        ads=ads_client(use_mock=True, transport=ads_t),
        suggests=store,
    )
    built = svc.build_waste_suggests(actor="jonatan")
    assert built.ok
    listed = svc.list_suggests(status="open", actor="jonatan")
    assert listed.ok
    suggests = (listed.data or {}).get("suggests") or []
    negatives = [s for s in suggests if s["kind"] == "negative"]
    assert negatives
    # Mutate still disabled by config
    blocked = svc.approve_and_mutate(negatives[0]["id"], actor="jonatan")
    assert blocked.ok is False
    assert "blocked" in blocked.message.lower() or "disabled" in blocked.message.lower()

    monkeypatch.setattr(
        "ecom_ops.actions.marketing.ads_mutate_allowed",
        lambda config=None: (True, "eligible"),
    )
    applied = svc.approve_and_mutate(negatives[0]["id"], actor="jonatan")
    assert applied.ok is True
    assert ads_t.mutates


def test_mp_queue_not_silent(tmp_path):
    ga_t = InMemoryGA4Transport()
    store = MarketingSuggestStore(path=tmp_path / "s.jsonl")
    svc = MarketingService(
        use_mock=True,
        ga4=ga4_client(use_mock=True, transport=ga_t),
        suggests=store,
    )
    q = svc.queue_mp_event({"name": "purchase", "params": {}}, actor="jonatan")
    assert q.ok
    assert ga_t.mp_sent == []
    # Approve blocked while mp disabled
    sid = q.data["id"]
    blocked = svc.approve_and_mutate(sid, actor="oscar")
    assert blocked.ok is False


def test_merchant_queue_and_write(tmp_path, monkeypatch):
    from ecom_ops.integrations.merchant import InMemoryMerchantTransport, MerchantClient

    mt = InMemoryMerchantTransport()
    store = MarketingSuggestStore(path=tmp_path / "s.jsonl")
    svc = MarketingService(
        use_mock=True,
        merchant=MerchantClient(transport=mt),
        suggests=store,
    )
    q = svc.queue_merchant_write(
        {"offerId": "SKU-X", "title": "X"}, actor="jonatan"
    )
    assert q.ok
    monkeypatch.setattr(
        "ecom_ops.actions.marketing.merchant_write_allowed",
        lambda config=None: (True, "eligible"),
    )
    applied = svc.approve_and_mutate(q.data["id"], actor="oscar")
    assert applied.ok
    assert mt.writes


def test_cli_marketing_digest(capsys):
    code = main(["--mock", "marketing", "digest", "--days", "7"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True


def test_status_includes_ga4_ads(capsys):
    code = main(["--mock", "status"])
    out = json.loads(capsys.readouterr().out)
    assert code in (0, 1)
    assert out.get("ga4") in {"on", "off"}
    assert out.get("ads") in {"on", "off"}


def test_agent_cannot_mutate_budget(tmp_path, monkeypatch):
    store = MarketingSuggestStore(path=tmp_path / "s.jsonl")
    item = store.add(
        kind="budget",
        title="Budget",
        reason="pacing",
        payload={"op": "adjust_budget", "campaign_id": "222"},
    )
    svc = MarketingService(use_mock=True, suggests=store)
    monkeypatch.setattr(
        "ecom_ops.actions.marketing.ads_mutate_allowed",
        lambda config=None: (True, "eligible"),
    )
    denied = svc.approve_and_mutate(item.id, actor="agent")
    assert denied.ok is False


def test_probes_mock_ok():
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[1]
    path = root / "infrastructure" / "dashboard" / "secret_probes.py"
    if str(root / "skills") not in sys.path:
        sys.path.insert(0, str(root / "skills"))
    spec = importlib.util.spec_from_file_location("secret_probes_mkt", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["secret_probes_mkt"] = mod
    spec.loader.exec_module(mod)
    r1 = mod.probe_ga4()
    r2 = mod.probe_google_ads()
    r3 = mod.probe_merchant()
    assert r1.status == "ok"
    assert r2.status == "ok"
    assert r3.status == "ok"


def test_oauth_mock_code_rejected_when_live(monkeypatch, tmp_path):
    from ecom_ops.oauth.google_marketing import (
        GoogleMarketingOAuthStore,
        exchange_code,
    )

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(ValueError, match="Mock OAuth code rejected"):
        exchange_code("mock")
    assert not GoogleMarketingOAuthStore().has_tokens()


def test_oauth_state_mismatch_does_not_wipe(tmp_path, monkeypatch):
    from ecom_ops.oauth.google_marketing import GoogleMarketingOAuthStore

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    store = GoogleMarketingOAuthStore()
    good = store.create_state()
    assert store.consume_state("wrong-state") is False
    assert store.state_path.is_file()
    assert store.validate_state(good) is True
    assert store.consume_state(good) is True
    assert not store.state_path.is_file()


def test_oauth_state_ttl_expires(tmp_path, monkeypatch):
    import json
    import time

    from ecom_ops.oauth.google_marketing import GoogleMarketingOAuthStore

    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    store = GoogleMarketingOAuthStore()
    state = store.create_state()
    payload = json.loads(store.state_path.read_text(encoding="utf-8"))
    payload["expires_at"] = time.time() - 1
    store.state_path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.validate_state(state) is False
    assert store.consume_state(state) is False
    assert not store.state_path.is_file()


def test_waste_suggests_dedupe_open(tmp_path):
    store = MarketingSuggestStore(path=tmp_path / "suggests.jsonl")
    svc = MarketingService(
        use_mock=True,
        ads=ads_client(use_mock=True),
        suggests=store,
    )
    first = svc.build_waste_suggests(actor="jonatan")
    second = svc.build_waste_suggests(actor="jonatan")
    assert first.ok and second.ok
    assert (second.data or {}).get("skipped_duplicates", 0) > 0
    assert (second.data or {}).get("suggests") == []
    open_n = len(store.list(status="open", limit=500))
    assert open_n == len((first.data or {}).get("suggests") or [])


def test_mutate_fail_marks_suggest_failed(tmp_path, monkeypatch):
    class BoomAds:
        customer_ids = ("1234567890",)

        def waste_report(self, *a, **k):
            return []

        def pacing(self):
            return []

        def mutate(self, ops):
            return {"ok": False, "error": "quota"}

    store = MarketingSuggestStore(path=tmp_path / "s.jsonl")
    item = store.add(
        kind="negative",
        title="Neg",
        reason="waste",
        payload={
            "op": "add_negative_keyword",
            "search_term": "free",
            "campaign_id": "1",
        },
    )
    svc = MarketingService(use_mock=True, ads=BoomAds(), suggests=store)
    monkeypatch.setattr(
        "ecom_ops.actions.marketing.ads_mutate_allowed",
        lambda config=None: (True, "eligible"),
    )
    result = svc.approve_and_mutate(item.id, actor="jonatan")
    assert result.ok is False
    assert store.get(item.id).status == "failed"


def test_mutate_op_kind_mismatch(tmp_path, monkeypatch):
    store = MarketingSuggestStore(path=tmp_path / "s.jsonl")
    item = store.add(
        kind="negative",
        title="Bad op",
        reason="x",
        payload={"op": "pause_campaign", "campaign_id": "1"},
    )
    svc = MarketingService(use_mock=True, suggests=store)
    monkeypatch.setattr(
        "ecom_ops.actions.marketing.ads_mutate_allowed",
        lambda config=None: (True, "eligible"),
    )
    result = svc.approve_and_mutate(item.id, actor="jonatan")
    assert result.ok is False
    assert "does not match" in result.message
    assert store.get(item.id).status == "open"


def test_woo_window_passes_after_before(monkeypatch):
    calls: list[dict] = []

    class FakeWoo:
        def list_orders(self, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(
        "ecom_ops.integrations.woocommerce.client_from_env",
        lambda use_mock=None: FakeWoo(),
    )
    svc = MarketingService(use_mock=False)
    monkeypatch.setattr(svc, "_mock_runtime", lambda: False)
    assert svc._woo_purchase_count(days=7) == 0
    assert calls
    assert "after" in calls[0] and "before" in calls[0]
    assert svc._woo_revenue(days=7) == 0.0
    assert len(calls) >= 2
