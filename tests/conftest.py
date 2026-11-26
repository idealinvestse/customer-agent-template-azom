"""Shared pytest fixtures for ecom-ops V2."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ecom_ops.escalation import EscalationService
from ecom_ops.integrations.mail import InMemoryMailTransport, MailClient, MailConfig, MailProvider
from ecom_ops.integrations.ssh import LocalMockSSHRunner, SSHClient
from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient
from ecom_ops.model_router import reset_model_router
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.telemetry import Telemetry


def _reset_research_state() -> None:
    """Best-effort reset of research caches, search router, and workers when installed."""
    import importlib

    targets = (
        ("ecom_ops.research.worker", "shutdown_research_workers"),
        ("ecom_ops.research", "reset_research_service"),
        ("ecom_ops.research.config", "clear_research_config_cache"),
        ("ecom_ops.research.config", "reset_research_config_cache"),
        ("ecom_ops.research.config", "reset_config_cache_for_tests"),
        ("ecom_ops.integrations.search", "reset_search_router"),
        ("ecom_ops.integrations.search.router", "reset_search_router"),
        ("ecom_ops.integrations.search.circuit_breaker", "reset_breaker_cache_for_tests"),
    )
    for mod_name, fn_name in targets:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            fn()


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_NULL_SEND", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(Path(__file__).resolve().parents[1] / "config"))
    dynamic_prefixes = (
        "AZOM_WEB_SEARCH_",
        "AZOM_WEB_FETCH_",
        "AZOM_RESEARCH_",
        "AZOM_SEARCH_",
    )
    dynamic_keys = tuple(k for k in list(os.environ) if k.startswith(dynamic_prefixes))
    for name in (
        "OPENAI_SIWC_CLIENT_ID",
        "OPENAI_SIWC_REDIRECT_URI",
        "OPENAI_SIWC_AUTHORIZATION_URL",
        "OPENAI_SIWC_TOKEN_URL",
        "OPENAI_SIWC_HOST_ID",
        "OPENAI_MODEL",
        "XAI_API_KEY",
        "XAI_GROK_BIN",
        "XAI_MODEL",
        "AZOM_XAI_GROK_BUILD",
        "AZOM_MODEL_ROUTE_ORDER",
        "AZOM_TELEGRAM_MODEL_ROUTE",
        "AZOM_WEB_SEARCH_ENABLED",
        "AZOM_WEB_FETCH_ENABLED",
        "AZOM_RESEARCH_ENABLED",
        "AZOM_WEB_SEARCH_KILL",
        "AZOM_RESEARCH_KILL",
        "AZOM_SEARCH_ALLOWED_DOMAINS",
        "AZOM_SEARCH_ROUTE_ORDER",
        "AZOM_RESEARCH_DEFAULT_COUNTRY",
        "AZOM_RESEARCH_DEFAULT_LANG",
        "PARALLEL_API_KEY",
        "EXA_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "SEARXNG_BASE_URL",
        "SEARXNG_API_KEY",
        *dynamic_keys,
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_research_state()
    reset_model_router()
    clear_rbac_cache()
    yield
    _reset_research_state()
    reset_model_router()
    clear_rbac_cache()


@pytest.fixture
def woo() -> WooCommerceClient:
    return WooCommerceClient(
        base_url="https://mock.local",
        transport=InMemoryWooTransport(),
    )


@pytest.fixture
def ssh_client() -> SSHClient:
    return SSHClient(host="test-host", runner=LocalMockSSHRunner())


@pytest.fixture
def mail_client() -> MailClient:
    return MailClient(
        config=MailConfig(
            provider=MailProvider.GENERIC_IMAP,
            username="mock@azom.se",
            from_addr="support@azom.se",
        ),
        transport=InMemoryMailTransport(),
    )


@pytest.fixture
def telemetry(tmp_path) -> Telemetry:
    return Telemetry(path=tmp_path / "telemetry.jsonl")


@pytest.fixture
def escalation(tmp_path) -> EscalationService:
    return EscalationService(store_path=tmp_path / "escalations.jsonl", notifiers=[])
