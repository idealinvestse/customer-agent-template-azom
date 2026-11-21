"""Shared pytest fixtures for ecom-ops V2."""

from __future__ import annotations

from pathlib import Path

import pytest

from ecom_ops.escalation import EscalationService
from ecom_ops.integrations.mail import InMemoryMailTransport, MailClient, MailConfig, MailProvider
from ecom_ops.integrations.ssh import LocalMockSSHRunner, SSHClient
from ecom_ops.integrations.woocommerce import InMemoryWooTransport, WooCommerceClient
from ecom_ops.model_router import reset_model_router
from ecom_ops.rbac import clear_rbac_cache
from ecom_ops.telemetry import Telemetry


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_NULL_SEND", "0")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(Path(__file__).resolve().parents[1] / "config"))
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
    ):
        monkeypatch.delenv(name, raising=False)
    reset_model_router()
    clear_rbac_cache()
    yield
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
